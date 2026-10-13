from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
from concurrent.futures import Future
from unittest.mock import patch

from test_codex_app_server import FakeCodex, BrowserSessions, CoordinationStore, CoordinationError
from agent_coord.acp import ACPError
from agent_coord.kimi import KimiConnection, KimiRPC
from agent_coord.kimi_setup import ensure_hooks, resolve_runtime, START, END

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"


def settings(model="k3", effort="high", mode="default"):
    options = [{"id": "model", "category": "model", "type": "select", "currentValue": model,
                "options": [{"value": "k3", "name": "K3"}, {"value": "fast", "name": "Fast"}]},
               {"id": "mode", "category": "mode", "type": "select", "currentValue": mode,
                "options": [{"value": x} for x in ("default", "plan", "auto", "yolo")]}]
    if model == "k3":
        options.insert(1, {"id": "thinking", "category": "thought_level", "type": "select", "currentValue": effort,
                           "options": [{"value": x, "name": x.title()} for x in ("low", "high", "max")]})
    return {"configOptions": options}


class FakeKimi:
    def __init__(self, store, callback, options):
        self.callback, self.options = callback, options
        self.process = self
        self.closed = False
        self.calls, self.writes = [], []
        self.future = None
        self.model, self.effort, self.mode = "k3", "high", "default"
        self.native_id = str(uuid.uuid4())
        self.fail_model = None

    def poll(self):
        return 0 if self.closed else None

    def initialize(self):
        return {"agentCapabilities": {"loadSession": True, "promptCapabilities": {"image": True},
                "sessionCapabilities": {"resume": {}, "close": {}, "delete": {}}}}

    def request(self, method, params, **kwargs):
        self.calls.append((method, copy.deepcopy(params)))
        if method in {"session/new", "session/resume", "session/load"}:
            self.native_id = params.get("sessionId", self.native_id)
            if method == "session/load":
                self.callback({"method": "session/update", "params": {"sessionId": self.native_id,
                    "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "old history"}}}})
            return {"sessionId": self.native_id, **settings(self.model, self.effort, self.mode)}
        if method == "session/set_config_option":
            if params["configId"] == "model":
                if params["value"] == self.fail_model:
                    raise CoordinationError("Model configuration failed")
                self.model = params["value"]
            elif params["configId"] == "thinking":
                self.effort = params["value"]
            elif params["configId"] == "mode":
                self.mode = params["value"]
            return settings(self.model, self.effort, self.mode)
        return {}

    def submit(self, method, params):
        self.calls.append((method, copy.deepcopy(params)))
        self.future = Future()
        return self.future

    def write(self, message):
        self.writes.append(copy.deepcopy(message))

    def close(self):
        self.closed = True
        if self.future and not self.future.done():
            self.future.set_exception(CoordinationError("Disconnected"))


class KimiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = CoordinationStore(self.root / "state.sqlite3")
        self.connections = []
        self.sessions = self.make_sessions()

    def factory(self, store, callback, options):
        connection = FakeKimi(store, callback, options)
        self.connections.append(connection)
        return connection

    def make_sessions(self):
        sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex,
            kimi_factory=lambda store, event: KimiRPC(store, event, connection_factory=self.factory))
        self.addCleanup(sessions.close)
        return sessions

    def create(self, **settings):
        return self.sessions.create({"client": "kimi", **settings})["session"]["thread_id"]

    def start(self, thread):
        self.sessions.send(thread, {"message": "Hello"})
        return self.sessions.kimi.connections[thread]

    def update(self, thread, **update):
        connection = self.sessions.kimi.connections[thread]
        connection.callback({"method": "session/update", "params": {"sessionId": connection.native_id, "update": update}})

    def permission(self, thread, *, form=False, question=False):
        connection = self.sessions.kimi.connections[thread]
        params = {"sessionId": connection.native_id}
        if form:
            params.update(mode="form", requestedSchema={"type": "object", "required": ["q0", "q1"], "properties": {
                "q0": {"type": "string", "title": "Framework", "oneOf": [{"const": "Flask", "title": "Flask"}, {"const": "Django"}]},
                "q1": {"type": "array", "title": "Platforms", "minItems": 1,
                       "items": {"anyOf": [{"const": "macOS"}, {"const": "Linux"}]}}}})
        else:
            params.update(toolCall={"toolCallId": "tool-1", "title": "AskUserQuestion" if question else "Write",
                "rawInput": {"path": "file.txt"}, "content": [{"type": "content", "content": {"type": "text", "text": "Choose one"}}]},
                options=[{"optionId": "q0_opt_0" if question else "approve", "kind": "allow_once", "name": "First"},
                         {"optionId": "q0_skip" if question else "reject", "kind": "reject_once", "name": "Skip"}])
        connection.callback({"id": "same-id", "method": "elicitation/create" if form else "session/request_permission", "params": params})
        return self.sessions.pending_requests(thread)[0]

    def test_provider_registration_and_model_specific_efforts(self):
        models = self.sessions.models("kimi")
        self.assertEqual([m["model"] for m in models], ["k3", "fast"])
        self.assertEqual([e["reasoningEffort"] for e in models[0]["supportedReasoningEfforts"]], ["low", "high", "max"])
        self.assertEqual(models[1]["supportedReasoningEfforts"], [])
        probe = self.connections[0]
        self.assertNotIn("threadId", probe.options)
        self.assertTrue(probe.closed)
        self.assertIn("session/delete", [method for method, _ in probe.calls])
        thread = self.create(model="k3", effort="max")
        self.assertEqual(self.store.get_session(thread)["client"], "kimi")
        self.assertEqual(self.sessions.read(thread)["session"]["effort"], "max")
        self.assertFalse(self.sessions.rpc.calls)
        with self.assertRaises(CoordinationError):
            self.create(model="fast", effort="max")
        from agent_coord.cli import _parser
        from agent_coord.app_control import AppControl
        self.assertEqual(AppControl(self.store).request("models", thread, {"client": "kimi"})["status"], "queued")
        parser = _parser()
        self.assertEqual(parser.parse_args(["thread", "models", "--client", "kimi"]).client, "kimi")

    def test_streaming_images_context_and_restart_use_the_same_native_session(self):
        thread = self.create()
        self.sessions.send(thread, {"message": "Describe", "images": [{"name": "image.png", "url": "data:image/png;base64,iVBORw0KGgo="}]})
        connection = self.sessions.kimi.connections[thread]
        prompt = connection.calls[-1][1]["prompt"]
        self.assertIn(thread, prompt[0]["text"])
        self.assertIn("AGENTS.md", prompt[0]["text"])
        self.assertEqual(prompt[-1], {"type": "image", "mimeType": "image/png", "data": "iVBORw0KGgo="})
        self.update(thread, sessionUpdate="agent_thought_chunk", content={"type": "text", "text": "Thinking"})
        for text in ("Hello", " world"):
            self.update(thread, sessionUpdate="agent_message_chunk", content={"type": "text", "text": text})
        self.update(thread, sessionUpdate="tool_call", toolCallId="tool", title="Read", rawInput={"path": "x"}, status="in_progress")
        self.update(thread, sessionUpdate="tool_call_update", toolCallId="tool", content=[{"type": "content", "content": {"type": "text", "text": "file"}}], status="completed")
        connection.future.set_result({"stopReason": "end_turn"})
        before = self.sessions.read(thread)["thread"]["turns"]
        self.assertEqual([i["type"] for i in before[0]["items"]], ["userMessage", "reasoning", "agentMessage", "mcpToolCall"])
        self.assertEqual(before[0]["items"][2]["text"], "Hello world")
        self.sessions.close()
        self.sessions = self.make_sessions()
        self.assertEqual(self.sessions.read(thread)["thread"]["turns"], before)
        self.assertFalse(self.sessions.kimi.connections)
        resumed = self.start(thread)
        self.assertEqual(resumed.native_id, connection.native_id)
        self.assertIn("session/resume", [method for method, _ in resumed.calls])

    def test_load_fallback_ignores_replayed_history(self):
        thread = self.create()
        self.sessions.close()
        self.sessions = self.make_sessions()
        with patch.object(FakeKimi, "initialize", return_value={"agentCapabilities": {"loadSession": True}}):
            connection = self.start(thread)
        self.assertIn("session/load", [method for method, _ in connection.calls])
        self.assertEqual([item["type"] for item in self.sessions.read(thread)["thread"]["turns"][-1]["items"]], ["userMessage"])

    def test_approval_ids_are_scoped_and_only_native_choices_are_accepted(self):
        first, second = self.create(), self.create()
        one, two = self.start(first), self.start(second)
        request = self.permission(first)
        self.permission(second)
        with self.assertRaises(CoordinationError):
            self.sessions.answer(second, request["key"], {"decision": "accept"})
        with self.assertRaises(CoordinationError):
            self.sessions.answer(first, request["key"], {"decision": "acceptForSession"})
        self.sessions.answer(first, request["key"], {"decision": "decline"})
        self.assertEqual(one.writes[-1]["result"]["outcome"], {"outcome": "selected", "optionId": "reject"})
        self.assertFalse(two.writes)

    def test_native_forms_preserve_multiple_questions_and_multiselect_in_yolo(self):
        thread = self.create(yolo=True)
        connection = self.start(thread)
        request = self.permission(thread, form=True)
        self.assertEqual(len(request["params"]["questions"]), 2)
        self.assertTrue(request["params"]["questions"][1]["multiSelect"])
        answers = {"q0": {"answers": ["Flask"]}, "q1": {"answers": ["Linux", "macOS"]}}
        with self.assertRaises(CoordinationError):
            self.sessions.answer(thread, request["key"], {"answers": {**answers, "q0": {"answers": ["invented"]}}})
        self.assertFalse(connection.writes)
        self.sessions.answer(thread, request["key"], {"answers": answers})
        self.assertEqual(connection.writes[-1]["result"], {"action": "accept", "content": {"q0": "Flask", "q1": ["Linux", "macOS"]}})
        request = self.permission(thread, form=True)
        self.sessions.answer(thread, request["key"], {"decision": "cancel"})
        self.assertEqual(connection.writes[-1]["result"], {"action": "cancel"})

    def test_question_permission_fallback_round_trips_opaque_option_ids(self):
        thread = self.create()
        connection = self.start(thread)
        request = self.permission(thread, question=True)
        self.assertEqual(request["params"]["questions"][0]["question"], "Choose one")
        self.sessions.answer(thread, request["key"], {"answers": {"tool-1": {"answers": ["q0_skip"]}}})
        self.assertEqual(connection.writes[-1]["result"]["outcome"]["optionId"], "q0_skip")

    def test_stop_disconnect_and_stale_events_do_not_affect_other_turns(self):
        first, second = self.create(), self.create()
        one, two = self.start(first), self.start(second)
        self.permission(first)
        self.sessions.interrupt(first)
        self.assertTrue(one.closed)
        self.assertIn("session/cancel", [m.get("method") for m in one.writes])
        self.assertFalse(self.sessions.pending_requests(first))
        self.assertTrue(self.sessions.read(second)["running"])
        self.start(first)
        one.callback({"method": "disconnected", "params": {"error": "stale"}})
        self.assertTrue(self.sessions.read(first)["running"])
        self.sessions.rpc.close()
        self.assertTrue(self.sessions.read(second)["running"])
        two.callback({"method": "disconnected", "params": {"error": "Native process crashed"}})
        self.assertEqual(self.sessions.read(second)["thread"]["turns"][-1]["status"], "failed")
        self.assertTrue(self.sessions.read(first)["running"])

    def test_settings_close_and_reopen_preserve_identity_and_workspace(self):
        thread = self.create(model="k3", effort="low")
        one = self.start(thread)
        one.future.set_result({"stopReason": "end_turn"})
        self.sessions.update(thread, {"yolo": True, "effort": "max"})
        two = self.start(thread)
        self.assertTrue(one.closed)
        self.assertEqual(two.native_id, one.native_id)
        self.assertEqual((two.effort, two.mode), ("max", "yolo"))
        two.future.set_exception(CoordinationError("Account quota exhausted"))
        self.assertIn("quota", self.sessions.read(thread)["thread"]["turns"][-1]["error"]["message"])
        self.sessions.close_work_thread(thread)
        self.sessions.reopen_work_thread(thread)
        self.assertEqual(self.start(thread).native_id, one.native_id)
        self.sessions.interrupt(thread)
        (self.root / "other").mkdir()
        with self.assertRaisesRegex(CoordinationError, "original workspace"):
            self.sessions.send(thread, {"message": "/cd other"})
        self.assertEqual(self.sessions.read(thread)["session"]["cwd"], str(self.root))

    def test_failed_settings_never_replace_native_history(self):
        thread = self.create()
        original = self.sessions.kimi.native[thread]
        self.sessions.kimi._shutdown(thread)
        factory = self.sessions.kimi.connection_factory
        def failing(store, callback, options):
            connection = factory(store, callback, options)
            connection.fail_model = "k3"
            return connection
        self.sessions.kimi.connection_factory = failing
        with self.assertRaisesRegex(CoordinationError, "configuration failed"):
            self.start(thread)
        self.sessions.kimi.connection_factory = factory
        connection = self.start(thread)
        self.assertEqual(connection.native_id, original)
        self.assertNotIn("session/new", [method for method, _ in connection.calls])

    def test_unsupported_client_requests_and_steering_are_rejected(self):
        thread = self.create()
        connection = self.start(thread)
        connection.callback({"id": 3, "method": "fs/write_text_file", "params": {
            "sessionId": connection.native_id, "path": str(self.root / "unexpected"), "content": "no"}})
        self.assertEqual(connection.writes[-1]["error"]["code"], -32601)
        self.assertFalse((self.root / "unexpected").exists())
        with self.assertRaisesRegex(CoordinationError, "steering"):
            self.sessions.send(thread, {"message": "steer", "expectedTurnId": self.sessions.active[thread]})

    def test_read_racing_with_completion_keeps_final_status_and_text(self):
        thread = self.create()
        connection = self.start(thread)
        self.update(thread, sessionUpdate="agent_message_chunk", content={"type": "text", "text": "Finished"})
        snapshots = []
        handler = self.sessions.kimi.on_event
        def before_browser_persists(message):
            if message["method"] == "turn/completed":
                snapshots.append(self.sessions.kimi.request("thread/read", {"threadId": thread})["thread"])
            handler(message)
        self.sessions.kimi.on_event = before_browser_persists
        connection.future.set_result({"stopReason": "end_turn"})
        turn = snapshots[0]["turns"][-1]
        self.assertEqual(turn["status"], "completed")
        self.assertEqual(turn["items"][-1]["text"], "Finished")
        self.assertEqual(snapshots[0]["status"]["type"], "idle")

    def test_empty_native_completion_is_a_visible_failure_and_survives_restart(self):
        thread = self.create()
        connection = self.start(thread)
        connection.future.set_result({"stopReason": "end_turn"})
        turn = self.sessions.read(thread)["thread"]["turns"][-1]
        self.assertEqual(turn["status"], "failed")
        self.assertIn("without a response", turn["error"]["message"])
        self.assertIn("hook", turn["error"]["message"])
        self.assertFalse(self.sessions.read(thread)["running"])
        self.sessions.close()
        self.sessions = self.make_sessions()
        self.assertEqual(self.sessions.read(thread)["thread"]["turns"][-1], turn)

    def test_native_refusal_cancellation_and_limits_are_not_success(self):
        for reason, status in (("refusal", "failed"), ("blocked", "failed"),
                               ("max_tokens", "failed"), ("max_turn_requests", "failed"),
                               ("cancelled", "interrupted")):
            with self.subTest(reason=reason):
                thread = self.create()
                connection = self.start(thread)
                connection.future.set_result({"stopReason": reason})
                turn = self.sessions.read(thread)["thread"]["turns"][-1]
                self.assertEqual(turn["status"], status)
                if status == "failed":
                    self.assertIn("error", turn)

    def test_tool_only_completion_is_preserved(self):
        thread = self.create()
        connection = self.start(thread)
        self.update(thread, sessionUpdate="tool_call", toolCallId="tool", title="Read", status="completed")
        connection.future.set_result({"stopReason": "end_turn"})
        self.assertEqual(self.sessions.read(thread)["thread"]["turns"][-1]["status"], "completed")


class KimiTransportAndHookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = CoordinationStore(self.root / "state.sqlite3")
        self.environment = patch.dict(os.environ, {"KIMI_CODE_HOME": str(self.root / "kimi-home"),
            "AGENT_COORD_DB": str(self.store.database_path), "AGENT_COORD_CLIENT": "kimi", "AGENT_COORD_SESSION_ID": "ribbon-thread"})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_hooks_preserve_user_config_and_are_inert_outside_ribbon_processes(self):
        path = self.root / "kimi-home/config.toml"
        path.parent.mkdir()
        original = 'default_model = "my-model"\n\n[[hooks]]\nevent = "Stop"\ncommand = "echo mine"\n'
        path.write_text(original)
        ensure_hooks()
        configured = path.read_text()
        ensure_hooks()
        self.assertEqual(path.read_text(), configured)
        self.assertTrue(configured.startswith(original))
        self.assertEqual(configured.count(START), 1)
        command = json.loads(next(line[10:] for line in configured.splitlines() if line.startswith('command = "if ')))
        env = {**os.environ, "AGENT_COORD_CLIENT": "other"}
        result = subprocess.run(command, shell=True, input="invalid JSON", capture_output=True, text=True, env=env)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        path.write_text(configured.replace(END, ""))
        with self.assertRaises(CoordinationError):
            ensure_hooks()

    def test_missing_packaged_hook_fails_before_changing_user_config(self):
        path = self.root / "kimi-home/config.toml"
        path.parent.mkdir()
        path.write_text('default_model = "my-model"\n')
        with patch("agent_coord.kimi_setup.cli_path", return_value=self.root / "backend/agent-coord"):
            with self.assertRaisesRegex(CoordinationError, "Kimi hook is missing"):
                ensure_hooks()
        self.assertEqual(path.read_text(), 'default_model = "my-model"\n')

    def hook(self, event, **extra):
        result = subprocess.run([sys.executable, str(SCRIPTS / "agent-coord-hook")], text=True, capture_output=True,
            input=json.dumps({"hook_event_name": event, "session_id": "native-kimi-id", "cwd": str(self.root), **extra}), check=True)
        return json.loads(result.stdout)

    def test_native_hook_uses_ribbon_identity_delivers_context_and_guards_writes(self):
        result = self.hook("UserPromptSubmit", prompt="Make a change")
        self.assertIn("agent-coord checkpoint", result["message"])
        self.assertEqual(self.store.get_session("ribbon-thread")["client"], "kimi")
        with self.assertRaises(CoordinationError):
            self.store.get_session("native-kimi-id")
        self.store.register(session_id="peer", client="codex", cwd=str(self.root))
        self.store.begin_work(session_id="peer", scopes=["other/**"])
        denied = self.hook("PreToolUse", tool_name="Write", tool_input={"path": "src/file.py"})
        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
        self.store.begin_work(session_id="ribbon-thread", scopes=["src/**"])
        self.assertNotIn("permissionDecision", self.hook("PreToolUse", tool_name="Write", tool_input={"path": "src/file.py"}).get("hookSpecificOutput", {}))
        denied = self.hook("PreToolUse", tool_name="Edit", tool_input={"path": "other/file.py"})
        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
        self.store.send_message(sender_session_id="peer", recipient_session_id="ribbon-thread", body="Review this change", classification="action_required", reply_required=False)
        for event in ("PreToolUse", "PostToolUse"):
            self.assertNotIn("message", self.hook(event, tool_name="Write", tool_input={"path": "src/file.py"}))
        self.assertIn("Review this change", self.hook("UserPromptSubmit", prompt="Continue")["message"])

    def test_transport_handshake_environment_auth_failure_and_cleanup(self):
        source = '''import json,sys,os
for line in sys.stdin:
 m=json.loads(line)
 if m['method']=='initialize': result={'protocolVersion':1,'agentInfo':{'name':'Kimi Code CLI','version':'2.1.1'},'agentCapabilities':{'loadSession':True}}
 elif m['method']=='environment': result={k:os.environ.get(k) for k in ['AGENT_COORD_SESSION_ID','AGENT_COORD_CLIENT','CODEX_THREAD_ID','AGENT_COORD_DB']}
 else:
  print(json.dumps({'jsonrpc':'2.0','id':m['id'],'error':{'code':-32000,'message':'Authentication required'}}),flush=True);continue
 print(json.dumps({'jsonrpc':'2.0','id':m['id'],'result':result}),flush=True)
'''
        script = self.root / "fake.py"
        script.write_text(source)
        connection = KimiConnection(self.store, lambda message: None, {"cwd": str(self.root), "threadId": "child"}, command=[sys.executable, "-u", str(script)])
        self.addCleanup(connection.close)
        self.assertEqual(connection.initialize()["protocolVersion"], 1)
        env = connection.request("environment", {})
        self.assertEqual(env["AGENT_COORD_SESSION_ID"], "child")
        self.assertIsNone(env["CODEX_THREAD_ID"])
        self.assertEqual(env["AGENT_COORD_CLIENT"], "kimi")
        with self.assertRaisesRegex(ACPError, "kimi login"):
            connection.request("session/new", {})
        self.assertFalse(connection.pending)
        connection.close()
        self.assertIsNotNone(connection.process.poll())

    def test_missing_runtime_reports_the_supported_package(self):
        with patch.dict(os.environ, {"AGENT_COORD_KIMI_BIN": str(self.root / "missing")}):
            with self.assertRaisesRegex(CoordinationError, "@moonshot-ai/kimi-code"):
                resolve_runtime()

    def test_native_install_is_discovered_without_shell_path(self):
        executable = self.root / ".kimi-code/bin/kimi"
        executable.parent.mkdir(parents=True)
        executable.write_text("#!/bin/sh\n")
        executable.chmod(0o755)
        with patch.dict(os.environ, {}, clear=True), \
                patch("agent_coord.kimi_setup.Path.home", return_value=self.root), \
                patch("agent_coord.kimi_setup.shutil.which", return_value=None):
            self.assertEqual(resolve_runtime(), str(executable))
            with patch.dict(os.environ, {"AGENT_COORD_KIMI_BIN": str(self.root / "missing")}):
                with self.assertRaises(CoordinationError):
                    resolve_runtime()


if __name__ == "__main__":
    unittest.main()
