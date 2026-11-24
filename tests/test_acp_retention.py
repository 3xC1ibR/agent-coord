"""Five real child processes survive an actual coordinator process exit.

Providers are deterministic protocol peers, not authenticated model workloads.
The test exercises production transport, projection, adapter and persistence code.
"""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

SCRIPTS = Path(os.environ.get("AGENT_COORD_RETENTION_TEST_SCRIPTS",
    str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts")))
sys.path.insert(0, str(SCRIPTS))


def provider_process(root, provider):
    approval = threading.Event()
    lock = threading.Lock()
    def emit(message):
        with lock:
            print(json.dumps(message), flush=True)
    def run(message):
        params = message["params"]
        identity, turn = params["thread"], params["turn"]
        def event(method, **values):
            emit({"method": method, "params": {"threadId": identity, "turnId": turn, "sessionId": identity, **values}})
        if provider == "claude":
            emit({"type": "stream_event", "event": {"type": "content_block_start", "index": 0,
                "content_block": {"type": "text", "text": ""}}})
        elif provider == "codex":
            event("item/started", item={"id": "answer", "type": "agentMessage", "text": ""})
        elif provider == "muse":
            event("item/started", item={"itemId": "answer", "kind": "agentMessage", "text": "",
                                       "status": "inProgress", "revision": 1, "turnId": turn})
        for index in range(20):
            if index == 10 and provider != "muse":
                if provider == "kimi":
                    emit({"id": 77, "method": "session/request_permission", "params": {"sessionId": identity,
                        "toolCall": {"toolCallId": "tool-1", "title": "Approval across coordinator replacement"},
                        "options": [{"optionId": "allow", "kind": "allow_once", "name": "Allow"}]}})
                (root / (provider + ".ready")).write_text("ready")
                while not (root / "continue").exists():
                    time.sleep(.01)
            token = f"{index:02d}|"
            if provider == "codex":
                event("item/agentMessage/delta", itemId="answer", delta=token)
            elif provider == "claude":
                emit({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
                    "delta": {"type": "text_delta", "text": token}}})
            elif provider == "muse":
                event("item/delta", itemId="answer", field="text", delta=token)
            else:
                event("session/update", update={"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": token}})
            time.sleep(.005)
        if provider == "kimi":
            approval.wait(20)
        if provider == "muse":
            event("item/started", item={"itemId": "workflow", "kind": "workflow", "status": "inProgress",
                "revision": 1, "turnId": turn, "workflowRunId": "workflow-1"})
            event("turn/completed", terminal="completed")
            (root / "muse.ready").write_text("ready")
            while not (root / "continue").exists():
                time.sleep(.01)
            event("item/completed", item={"itemId": "workflow", "kind": "workflow", "status": "completed",
                "revision": 2, "turnId": turn, "workflowRunId": "workflow-1"})
        elif provider == "codex":
            event("turn/completed", turn={"id": turn, "status": "completed", "items": []})
        elif provider == "claude":
            emit({"type": "result", "subtype": "success"})
        else:
            emit({"id": message["id"], "result": {"stopReason": "end_turn"}})
    for line in sys.stdin:
        message = json.loads(line)
        if message.get("id") == 77 and "result" in message:
            with (root / "approval-receipts").open("a") as receipt:
                receipt.write("accepted\n")
            approval.set()
        elif message.get("method") in {"fixture/start", "session/prompt"}:
            threading.Thread(target=run, args=(message,), daemon=True).start()


def coordinator_process(root, phase):
    from agent_coord.acp import ACPConnection
    from agent_coord.claude_code import ClaudeConnection
    from agent_coord.codex_app_server import BrowserSessions
    from agent_coord.coordinator_projection import CoordinatorProjection
    from agent_coord.provider_transport import Client
    from agent_coord.store import CoordinationStore
    from test_coordinator_projection import ProjectionCodex
    store = CoordinationStore(root / "state.sqlite3")
    sessions = BrowserSessions(store, str(root), rpc_factory=ProjectionCodex)
    client = Client.connect(root / "transport", "coordinator-" + phase)
    projection = CoordinatorProjection(sessions, client)
    sessions.projection = store.provider_projection = projection
    providers = ("codex", "claude", "antigravity", "kimi", "muse")
    if phase == "first":
        identities = {}
        for provider in providers:
            identity = sessions.create({})["session"]["thread_id"]
            sessions.send(identity, {"message": "retention test"})
            identities[provider] = identity
        sessions.persistence.flush()
        for provider, identity in identities.items():
            turn_id = sessions.active[identity]
            turn = copy.deepcopy(sessions.histories[identity]["turns"][-1])
            sessions.loaded_clients[identity] = provider
            command = [sys.executable, "-u", __file__, "provider", str(root), provider]
            if provider == "codex":
                sessions.rpc.channel = provider
                sessions.rpc.process = projection.open(provider, command, dict(os.environ), str(root), sessions.rpc._message, provider=provider)
                connection = None
            elif provider == "claude":
                adapter = sessions.claude
                adapter.turns[identity] = turn
                adapter.blocks[identity] = {}
                adapter.confirmed_blocks[identity] = set()
                process = projection.open(provider, command, dict(os.environ), str(root), lambda _: None, provider=provider, thread_id=identity)
                connection = ClaudeConnection.attach(store, lambda message, identity=identity, adapter=adapter: adapter._message(identity, message), identity, provider, process.pid)
                adapter.connections[identity] = connection
            else:
                adapter = getattr(sessions, provider)
                adapter.turns[identity] = turn
                adapter.native[identity] = identity
                adapter.options[identity] = {"cwd": str(root)}
                if provider == "antigravity":
                    callback = lambda message, adapter=adapter, identity=identity: adapter._message(identity, message)
                else:
                    generation = object()
                    adapter.generations[identity] = generation
                    callback = lambda message, adapter=adapter, identity=identity, generation=generation: adapter._message(identity, generation, message)
                if provider == "muse":
                    adapter.ready.add(identity)
                    adapter.items[identity] = {}
                    adapter.seen[identity] = set()
                connection = ACPConnection(callback, command=command, cwd=str(root), env=dict(os.environ),
                                           store=store, provider=provider, thread_id=identity)
                adapter.connections[identity] = connection
            params = {"thread": identity, "turn": turn_id, "sessionId": identity}
            if provider in {"kimi", "antigravity"}:
                connection.submit("session/prompt", params)
            else:
                channel = connection.channel if connection else provider
                projection.write(channel, {"method": "fixture/start", "params": params})
        (root / "identities.json").write_text(json.dumps(identities))
        projection.start()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if all((root / (p + ".ready")).exists() for p in providers) and sessions.muse.background_active() and sessions.kimi.requests:
                break
            time.sleep(.02)
        else:
            raise RuntimeError("Providers did not reach replacement boundary: " + str(projection.error))
        # Request approval before crossing the boundary, while other streams wait.
        # Let Kimi produce its remaining events without releasing all providers.
        projection.checkpoint()
        print(json.dumps({"pids": {entry["provider"]: entry["process"].pid for entry in projection.channels.values()},
                          "workflow": sessions.muse.background_active(), "approval": bool(sessions.kimi.requests)}), flush=True)
        while True:
            time.sleep(1)
    identities = json.loads((root / "identities.json").read_text())
    projection.load()
    before = {entry["provider"]: entry["process"].pid for entry in projection.channels.values()}
    projection.start()
    (root / "continue").write_text("continue")
    answered = False
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if sessions.kimi.requests and not answered:
            sessions.kimi.write({"id": next(iter(sessions.kimi.requests)), "result": {"decision": "accept"}})
            answered = True
        if not sessions.active and not sessions.muse.background_active():
            break
        time.sleep(.02)
    else:
        raise RuntimeError("Retained turns did not finish: " + str(projection.error) + " " + repr(sessions.active))
    projection.checkpoint()
    histories = {provider: sessions._history(identity) for provider, identity in identities.items()}
    print(json.dumps({"pids": before, "histories": histories, "answered": answered,
                      "workflow": sessions.muse.background_active()}), flush=True)
    sessions.transport_handover = True
    projection.stopped.set()
    sessions.close()


class ProcessReplacementTests(unittest.TestCase):
    def test_five_original_pids_and_lossless_output_survive_coordinator_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = {**os.environ, "PYTHONPATH": str(SCRIPTS) + os.pathsep + str(Path(__file__).parent)}
            env.pop("AGENT_COORD_SESSION_ID", None)
            env.pop("CODEX_THREAD_ID", None)
            first = None
            try:
                with (root / "first.log").open("w+") as errors:
                    first = subprocess.Popen([sys.executable, "-u", __file__, "coordinator", str(root), "first"],
                                             stdout=subprocess.PIPE, stderr=errors, text=True, env=env)
                    # Reader deadline avoids a hung test concealing diagnostics.
                    lines = []
                    reader = threading.Thread(target=lambda: lines.append(first.stdout.readline()), daemon=True)
                    reader.start(); reader.join(25)
                    if not lines or not lines[0]:
                        errors.seek(0)
                        self.fail(errors.read()[-10000:])
                    original = json.loads(lines[0])
                    self.assertTrue(original["workflow"])
                    self.assertTrue(original["approval"])
                    self.assertEqual(len(set(original["pids"].values())), 5)
                    first.kill(); first.wait(timeout=5); first.stdout.close()
                second = subprocess.run([sys.executable, "-u", __file__, "coordinator", str(root), "second"],
                                        capture_output=True, text=True, env=env, timeout=30)
                self.assertEqual(second.returncode, 0, second.stderr[-10000:])
                restored = json.loads(second.stdout)
                self.assertEqual(restored["pids"], original["pids"])
                self.assertTrue(restored["answered"])
                self.assertFalse(restored["workflow"])
                expected = "".join(f"{index:02d}|" for index in range(20))
                for provider, history in restored["histories"].items():
                    self.assertEqual(len(history["turns"]), 1, provider)
                    turn = history["turns"][0]
                    self.assertEqual(turn["status"], "completed", provider)
                    text = "".join(item.get("text", "") for item in turn["items"] if item["type"] == "agentMessage")
                    self.assertEqual(text, expected, provider)
                self.assertEqual((root / "approval-receipts").read_text(), "accepted\n")
            finally:
                if first is not None:
                    if first.poll() is None:
                        first.kill(); first.wait(timeout=5)
                    first.stdout.close()
                if (root / "transport/owner.json").exists():
                    from agent_coord.provider_transport import Client
                    client = Client.connect(root / "transport", "test-cleanup")
                    client.call("stop")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "provider":
        provider_process(Path(sys.argv[2]), sys.argv[3])
    elif len(sys.argv) > 1 and sys.argv[1] == "coordinator":
        coordinator_process(Path(sys.argv[2]), sys.argv[3])
    else:
        unittest.main()
