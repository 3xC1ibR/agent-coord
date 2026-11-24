#!/usr/bin/env python3
"""Serve Ribbon Field's real HTTP application against a disposable visual workload."""

from __future__ import annotations

import argparse
from contextlib import nullcontext
from http import HTTPStatus
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
STATES = ("default", "empty", "streaming", "long-error", "interactions")
ANCHOR = 1795526100  # Fixed clock for capture labels and stored transcript dates.
ASK_TITLE = "Which inspection interval should the maintenance guide recommend?"
OTHER_ASK_TITLE = "What limits should the sample certificate record?"
OPENUI = """```openui
root = Tabs([summary, readings])
summary = TabItem("summary", "Summary", [TextContent("Three assemblies passed inspection.")])
readings = TabItem("readings", "Readings", [TextContent("Bore diameter: 24.01 mm")])
```"""
LONG_TITLE = ("Final inspection of assemblies from the northern fabrication line "
              "with replacement fasteners and revised acceptance limits")
POST_PREFIXES = ("/api/browser/asks/", "/api/browser/threads/", "/api/browser/sessions/",
                 "/api/browser/activity/", "/api/browser/workspaces/open",
                 "/api/browser/notifications/", "/api/browser/events/", "/api/browser/schedules")


def make_turn(thread, index, text, *, status="completed", request=None):
    identity = f"{thread}-turn-{index:04}"
    return {
        "id": identity, "status": status,
        "startedAt": ANCHOR - 1000 + index, "completedAt": ANCHOR - 999 + index,
        "items": [
            {"id": identity + "-request", "type": "userMessage",
             "content": [{"type": "text", "text": request or f"Inspection request {index:04}."}]},
            {"id": identity + "-response", "type": "agentMessage", "text": text},
        ],
    }


def seed(root, workspace, sessions, state):
    from agent_coord.app_control import AppControl
    from agent_coord.attention import AttentionStore

    store = sessions.store
    AppControl(store)
    remote, pair, six, vacant = (root / name for name in ("Instrumentation", "Bench", "Assembly", "Receiving"))
    for directory in (workspace, remote, pair, six, vacant):
        directory.mkdir(exist_ok=True)
    for directory in (workspace, remote):
        (directory / ".git").mkdir()
    catalog = {}

    def add(key, title, *, directory=workspace, history=8, parent=None, domain="inspection"):
        thread = sessions.create({"name": title, "cwd": str(directory)})["session"]["thread_id"]
        catalog[key] = {"id": thread, "title": title, "cwd": str(directory),
                        "latest": f"{title} · inspection record {history:04}." if history else None}
        store.threads.update(thread, domain=domain)
        turns = []
        for index in range(1, history + 1):
            content = f"{title} · inspection record {index:04}."
            if index < history and history > 100:
                content += " The recorded clearance remained within the reference tolerance." * 14
            if index == history and key in {"supervisor", "other"}:
                content += "\n\n```python\nrecheck = measured_value > acceptance_limit\n```\n\n" + OPENUI
            turns.append(make_turn(thread, index, content))
        original = sessions.rpc.threads[thread]
        original["turns"] = turns
        with sessions.lock:
            sessions._remember_thread(original)
        if parent:
            with store._connection() as db:
                db.execute("""INSERT INTO app_control_requests
                    (request_id, operation, sender_session_id, cwd, thread_id, payload_json, status, created_at, updated_at)
                    VALUES (?, 'create', ?, ?, ?, '{"mode":"orchestration"}', 'completed', ?, ?)""",
                           (f"interface-{key}", catalog[parent]["id"], str(directory), thread, ANCHOR, ANCHOR))
        return thread

    def completion(thread, status, age):
        turn = thread + "-fixture-completion"
        store.threads.start_turn(thread, prompt="Check the assembly dimensions.", turn_id=turn,
                                 occurred_at=ANCHOR - age - 120)
        store.threads.finish_turn(thread, turn_id=turn, status=status, occurred_at=ANCHOR - age)

    def streaming(thread):
        turn = make_turn(thread, 999, "Streaming inspection update · comparing the second assembly.",
                         status="inProgress", request="Compare the diameter readings as they arrive.")
        turn.pop("completedAt")
        sessions.rpc.threads[thread]["turns"].append(turn)
        sessions._event({"method": "turn/started", "params": {"threadId": thread, "turn": turn}})
        sessions._event({"method": "item/started", "params": {"threadId": thread, "turnId": turn["id"],
                                                            "item": turn["items"][-1]}})
        return {"thread": thread, "turn": turn["id"], "item": turn["items"][-1]["id"]}

    def ask(thread, title, *, blocking=False, **fields):
        return store.asks.create(thread, title, blocking=blocking,
                                 context="The assembly report is ready. The inspection guide can use the current limits.",
                                 **fields)["id"]

    stream = None
    project_id = None
    asks = {}
    if state != "empty":
        supervisor = add("supervisor", "Assembly inspection", history=320 if state == "interactions" else 5)
        lead = add("lead", "Metrology lead", parent="supervisor", domain="measurement")
        add("specialist", "Gauge calibration", directory=remote, parent="lead", domain="measurement")
        add("sibling", "Certificate editor", parent="lead", domain="documentation")
        other = add("other", "Fixture drawings", history=140 if state == "interactions" else 7, domain="drawings")
        failed = add("failed", LONG_TITLE if state == "long-error" else "Fastener tolerance review",
                     domain="engineering")
        approval = add("approval", "Coordinate-measuring machine access", domain="measurement")
        blocker = add("blocking", "Surface treatment decision", domain="engineering")
        dispatcher = add("dispatcher", "Fabrication dispatcher", history=4, domain="operations")
        independent = add("remote", "Independent instrument report", directory=remote, domain="measurement")
        supervisor_history = sessions.rpc.threads[supervisor]
        supervisor_history["turns"][-1]["items"][-1]["text"] += (
            f"\n\n[Independent instrument report](agentcoord://thread/{independent})")
        sessions._remember_thread(supervisor_history)
        if state == "interactions":
            service = add("background", "Measurements in progress", domain="measurement")
            stream = streaming(service)
            stalled = add("stalled", "Spindle alignment review", domain="measurement")
            streaming(stalled)
            with sessions.lock:
                sessions.watchdog.observed[stalled].update(
                    state="stalled", detail="The fixture spindle has not reported progress.",
                    classification="silent")
            update = add("update", "Inspection schedule revision", domain="operations")
            completion(update, "completed", 120)
            classification = AttentionStore(store)
            thread = store.threads.get(update)
            token = classification.claim(thread)
            classification.save(thread, token, {"choice": "update", "confidence": .99,
                                                 "probabilities": {"update": .99}})
            worker = add("managed", "Managed fixture worker", history=1, domain="operations")
            delegation = store.create_delegation(parent_session_id=supervisor, cwd=str(workspace),
                scopes=["fixture.txt"], instructions="Read the disposable fixture.", mode="reviewed")
            store.attach_delegation(delegation["delegation_id"], worker)
            metadata = add("metadata", "Inspection drawing changes", domain="drawings")
            store.threads.update(metadata, pinned=True)
            asks["metadata"] = ask(metadata, "Which drawing revision should the inspector use?",
                                   options=["Revision C", "Revision D"])
        sessions.dispatch.configure(dispatcher, {"enabled": True})
        project = store.threads.organization.create_project("Assembly qualification", str(workspace))
        project_id = project["id"]
        for thread in (supervisor, lead, independent):
            store.threads.update(thread, project_id=project["id"])
        store.threads.update(other, pinned=True)
        completion(supervisor, "completed", 90)
        completion(failed, "failed", 600)
        completion(blocker, "completed", 180)
        asks["blocking"] = ask(blocker, "Which surface treatment should fabrication use?", blocking=True,
                               options=["Anodize", "Passivate"])
        asks["conversation"] = ask(supervisor, ASK_TITLE, blocking=False,
                                   options=["One week", "One month", "One quarter"])
        asks["queue"] = ask(other, OTHER_ASK_TITLE, options=["Drawing limits", "Process limits"])
        ask(approval, "May the inspection run use the reference plate after calibration?",
            kind="approval", unblocks="The tolerance assessment.")
        sessions.schedules.create({
            "id": "41308a97-510d-46da-88b1-c032e7c95bc8", "target_thread_id": other,
            "message": "Review the next lot of inspection records.",
            "run_at": ANCHOR + 7 * 86400, "timezone": "America/Chicago",
        })
        stored = ask(lead, "Which reference plate should be retained?", options=["Plate A", "Plate B"])
        store.asks.resolve(stored, "answered", option="Plate A")
        sessions._event({
            "id": 601, "method": "item/commandExecution/requestApproval",
            "params": {"threadId": approval, "command": "inspect gauge-reference.txt",
                       "reason": "Read the disposable fixture reference."},
        })
        if state == "streaming":
            stream = streaming(supervisor)
        if state == "long-error":
            original = sessions.rpc.threads[failed]
            failed_turn = make_turn(failed, 999, "The fixture rejected the recorded measurement. " * 38,
                                    status="failed", request="Review the long inspection record.")
            failed_turn["error"] = {"message": "Cutoff length exceeds the stated inspection limit."}
            original["turns"].append(failed_turn)
            sessions._remember_thread(original)
            ask(failed, "Which signed tolerance correction should be carried forward into the "
                "manufacturing release, maintenance guide and calibration certificate?", blocking=True,
                options=["Hold for drawing revision", "Apply the existing engineering waiver"])
    add("empty-conversation", "Receiving inspection", directory=vacant, history=0)
    if state == "empty":
        dispatcher = add("dispatcher", "Receiving dispatcher", directory=vacant, history=0, domain="operations")
        sessions.dispatch.configure(dispatcher, {"enabled": True})
    for index in range(2):
        thread = add(f"pair-{index}", ("Incoming dimensions", "Acceptance readings")[index],
                     directory=pair, history=0 if state == "empty" else 4)
        if index == 0 and state == "streaming":
            streaming(thread)
    if state == "interactions":
        for index in range(6):
            add(f"pane-{index}", f"Assembly station {index + 1}", directory=six, history=12)
    if not sessions.persistence.flush(timeout=20):
        raise RuntimeError("The fixture did not persist its conversation histories.")
    return {"state": state, "workspace": str(workspace), "vacant": str(vacant), "pair": str(pair),
            "six": str(six), "threads": catalog, "asks": asks, "stream": stream, "anchor": ANCHOR,
            "project": project_id}


def fixture_handler(parent, sessions, fixture, stop):
    class FixtureHandler(parent):
        def do_GET(self):
            parsed = urlsplit(self.path)
            if parsed.path == "/fixture/document":
                self._send(HTTPStatus.OK, "text/html; charset=utf-8",
                           b"<!doctype html><title>Fixture holding page</title><p>Fixture holding page</p>")
                return
            if parsed.path == "/fixture/advance" and fixture["stream"]:
                stream = fixture["stream"]
                sessions._event({"method": "item/agentMessage/delta", "params": {
                    "threadId": stream["thread"], "turnId": stream["turn"], "itemId": stream["item"],
                    "delta": " Applied the final calibration sample.",
                }})
                self._json(HTTPStatus.OK, {"advanced": True})
                return
            super().do_GET()

        def do_POST(self):
            path = urlsplit(self.path).path
            if not path.startswith(POST_PREFIXES):
                self._json(HTTPStatus.FORBIDDEN, {"error": "The interface fixture does not enable this operation."})
                self.close_connection = True
                return
            if (fixture["state"] == "long-error" and path.startswith("/api/browser/asks/")
                    and self.headers.get("X-Interface-Fixture-Ask-Error") == "1"):
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {
                    "error": "Inspection queue temporarily unavailable. Retry the request.",
                })
                self.close_connection = True
                return
            if stop.is_set():
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "The interface fixture is shutting down."})
                self.close_connection = True
                return
            super().do_POST()

    return FixtureHandler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT, help="Checkout whose backend serves the fixture.")
    parser.add_argument("--web-root", type=Path, help="Use a separately built web asset directory.")
    parser.add_argument("--state", choices=STATES, required=True)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--scope", choices=("root", "workspace"), default="root")
    parser.add_argument("--state-directory", type=Path, help="Use an external owner-managed disposable directory.")
    parser.add_argument("--resume", action="store_true", help="Reopen a previously seeded disposable fixture.")
    args = parser.parse_args()
    scripts = args.source.resolve() / "plugins/agent-coord/scripts"
    if not (scripts / "agent_coord/ui.py").is_file():
        parser.error(f"No Ribbon Field backend under {scripts}.")
    if args.web_root is not None and not (args.web_root / "index.html").is_file():
        parser.error("--web-root must contain index.html.")
    if args.resume and args.state_directory is None:
        parser.error("--resume requires --state-directory.")
    if args.state_directory is not None:
        contents = {path.name for path in args.state_directory.iterdir()}
        marker = "interface-fixture.json"
        if args.resume and marker not in contents or not args.resume and contents:
            parser.error("--state-directory must be empty for seeding or contain this fixture's resume marker.")
    sys.path.insert(0, str(scripts))
    for key in ("AGENT_COORD_DB", "AGENT_COORD_CLIENT", "AGENT_COORD_SESSION_ID", "CODEX_THREAD_ID", "CLAUDECODE"):
        os.environ.pop(key, None)
    from agent_coord import ui
    from agent_coord.codex_app_server import BrowserSessions
    from agent_coord.history_persistence import read_history
    from agent_coord.jev_settings import JevSettings
    from agent_coord.store import CoordinationStore
    from test_codex_app_server import FakeCodex

    class FixtureProvider(FakeCodex):
        def __init__(self, store, callback):
            super().__init__(store, callback)
            self.fast_turn = True

    if args.web_root:
        ui._WEB_ROOT = args.web_root.resolve()
    stop = threading.Event()

    def terminate(_signum, _frame):
        stop.set()

    signal.signal(signal.SIGINT, terminate)
    signal.signal(signal.SIGTERM, terminate)
    caller = os.getppid()
    directory_owner = (nullcontext(args.state_directory) if args.state_directory is not None
                       else tempfile.TemporaryDirectory(prefix="ribbon-interface-"))
    with directory_owner as directory:
        root = Path(directory).resolve()
        workspace = root / "Fabrication"
        origin = time.monotonic()
        store = CoordinationStore(root / "fixture.sqlite3", clock=lambda: ANCHOR + time.monotonic() - origin)
        JevSettings(store.database_path).configure({"action": "dismiss"})
        sessions = BrowserSessions(store, str(root), rpc_factory=FixtureProvider, claude_factory=FixtureProvider,
                                   antigravity_factory=FixtureProvider, kimi_factory=FixtureProvider, muse_factory=FixtureProvider)
        server = None
        try:
            if args.resume:
                fixture = json.loads((root / marker).read_text())
                for thread in fixture["threads"].values():
                    identity = thread["id"]
                    sessions.rpc.threads[identity] = read_history(store, identity) or {
                        "id": identity, "cwd": thread["cwd"], "turns": [],
                    }
            else:
                fixture = seed(root, workspace, sessions, args.state)
                if args.state_directory is not None:
                    (root / marker).write_text(json.dumps(fixture))
            if args.scope == "workspace":
                sessions.cwd = str(workspace)
            server = ui.make_ui_server(store, host="127.0.0.1", port=args.port, cwd=sessions.cwd,
                                       browser_sessions=sessions)
            for component in (server.app_control, sessions.inbox_wake, sessions.sidecars, server.stoic_integration):
                if component is not None:
                    component.close()
            server.RequestHandlerClass = fixture_handler(server.RequestHandlerClass, sessions, fixture, stop)
            server.timeout = .2
            print(json.dumps({**fixture, "base": f"http://127.0.0.1:{server.server_port}",
                              "pid": os.getpid()}), flush=True)
            while not stop.is_set() and os.getppid() == caller:
                server.handle_request()
        finally:
            if server is not None:
                server.server_close()
            else:
                sessions.close()


if __name__ == "__main__":
    main()
