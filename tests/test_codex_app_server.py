from __future__ import annotations

import copy
import json
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

PLUGIN_SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"
sys.path.insert(0, str(PLUGIN_SCRIPTS))

from agent_coord.codex_app_server import BrowserBusyError, BrowserSessions, CodexRPC
from agent_coord.store import CoordinationError, CoordinationStore


EMPTY_ROLLOUT_ERROR = (
    "failed to read thread: thread-store internal error: failed to read session metadata "
    "/sessions/rollout.jsonl: rollout at /sessions/rollout.jsonl is empty"
)


class FakeCodex:
    def __init__(self, store, callback):
        self.callback = callback
        self.threads = {}
        self.settings = {}
        self.calls = []
        self.writes = []
        self.fail_turn = False
        self.fail_steer = False
        self.fast_turn = False
        self.no_history = False
        self.empty_rollout_reads = {}
        self.closed = False

    def request(self, method, params=None):
        params = params or {}
        self.calls.append((method, params))
        thread_id = params.get("threadId")
        if method == "model/list":
            return {"data": [
                {"model": "available-model", "defaultReasoningEffort": "medium", "supportedReasoningEfforts": [{"reasoningEffort": "medium"}, {"reasoningEffort": "high"}]},
                {"model": "other-model", "defaultReasoningEffort": "low", "supportedReasoningEfforts": [{"reasoningEffort": "low"}]},
            ]}
        if method == "thread/start":
            thread_id = "thread-" + str(len(self.threads) + 1)
            self.threads[thread_id] = {"id": thread_id, "cwd": params["cwd"], "turns": []}
        if method == "thread/fork":
            parent = thread_id
            thread_id = "thread-" + str(len(self.threads) + 1)
            self.threads[thread_id] = {**copy.deepcopy(self.threads[parent]), "id": thread_id,
                                       "sessionId": parent, "forkedFromId": parent}
            self.settings[thread_id] = copy.deepcopy(self.settings.get(parent, {"model": "available-model", "reasoningEffort": "medium"}))
        if method in {"thread/start", "thread/fork", "thread/read", "thread/resume"}:
            if method == "thread/read" and self.empty_rollout_reads.get(params.get("includeTurns"), 0):
                self.empty_rollout_reads[params.get("includeTurns")] -= 1
                raise CoordinationError(EMPTY_ROLLOUT_ERROR)
            if self.no_history and method == "thread/read" and params.get("includeTurns"):
                raise CoordinationError("list_turns is not supported yet")
            if thread_id not in self.threads:
                raise CoordinationError("Thread not found.")
            thread = copy.deepcopy(self.threads[thread_id])
            if params.get("excludeTurns") or (method == "thread/read" and not params.get("includeTurns")):
                thread["turns"] = []
            if method in {"thread/start", "thread/fork", "thread/resume"}:
                settings = self.settings.setdefault(thread_id, {"model": "available-model", "reasoningEffort": "medium"})
                settings["model"] = params.get("model", settings["model"])
                settings["reasoningEffort"] = params.get("config", {}).get("model_reasoning_effort", settings["reasoningEffort"])
                return {"thread": thread, **settings}
            return {"thread": thread}
        if method == "turn/start":
            if self.fail_turn:
                raise CoordinationError("Provider unavailable.")
            turn = {"id": "turn-" + str(len(self.threads[thread_id]["turns"])), "status": "inProgress", "items": [{"id": "user", "type": "userMessage", "content": params["input"]}]}
            self.threads[thread_id]["turns"].append(turn)
            self.callback({"method": "turn/started", "params": {"threadId": thread_id, "turn": turn}})
            if self.fast_turn:
                turn["status"] = "completed"
                self.callback({"method": "turn/completed", "params": {"threadId": thread_id, "turn": turn}})
            return {"turn": copy.deepcopy(turn)}
        if method == "turn/steer":
            if self.fail_steer:
                raise CoordinationError("Steering unavailable.")
            turn = self.threads[thread_id]["turns"][-1]
            if turn["status"] != "inProgress" or turn["id"] != params["expectedTurnId"]:
                raise CoordinationError("Active turn mismatch.")
            item = {"id": "steer-" + str(len(turn["items"])), "type": "userMessage", "content": params["input"]}
            turn["items"].append(item)
            self.callback({"method": "item/completed", "params": {"threadId": thread_id, "turnId": turn["id"], "item": item}})
            return {"turnId": turn["id"]}
        if method == "turn/interrupt":
            turn = self.threads[thread_id]["turns"][-1]
            if params.get("turnId") and (turn["status"] != "inProgress" or turn["id"] != params["turnId"]):
                raise CoordinationError(f"expected active turn id {turn['id']} but found {params['turnId']}")
            turn["status"] = "interrupted"
            self.callback({"method": "turn/completed", "params": {"threadId": thread_id, "turn": turn}})
        return {}

    def write(self, message):
        self.writes.append(message)

    def close(self):
        self.closed = True
        self.callback({"method": "bridge/disconnected", "params": {}})


class BrowserSessionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = CoordinationStore(self.root / "coord.sqlite3")
        self.sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(self.sessions.close)

    def create(self, **values):
        return self.sessions.create({"name": "Feature discussion", **values})["session"]["thread_id"]

    def test_resolved_defaults_are_visible_and_restored(self):
        thread_id = self.create()
        record = self.sessions.read(thread_id)["session"]
        self.assertEqual((record["model"], record["effort"]), ("available-model", "medium"))
        self.assertFalse(record["yolo"])
        # Old records with no saved settings get Codex's effective values on resume.
        with self.store._connection() as db:
            db.execute("UPDATE browser_sessions SET model = NULL, effort = NULL WHERE thread_id = ?", (thread_id,))
        self.sessions.loaded.discard(thread_id)
        record = self.sessions.read(thread_id)["session"]
        self.assertEqual((record["model"], record["effort"]), ("available-model", "medium"))

    def test_fork_copies_history_and_settings_but_starts_independent_state(self):
        parent = self.create(yolo=True, model="available-model", effort="high")
        self.sessions.rpc.fast_turn = True
        self.sessions.send(parent, {"message": "Explore the original approach"})
        project = self.store.threads.organization.create_project("Fork project")
        self.store.threads.update(parent, attention="later", pinned=True, project_id=project["id"], repository_id=None)
        self.store.threads.checkpoint(parent, {"phase": "planning", "summary": "Original plan",
                                             "next_action": "Review", "next_actor": "user"})
        self.store.begin_work(session_id=parent, scopes=["src/**"])
        before = self.store.threads.get(parent, history=True)
        original_history = copy.deepcopy(self.sessions.rpc.threads[parent])
        fork = self.sessions.fork_work_thread(parent)
        child = fork["thread_id"]
        self.assertNotEqual(child, parent)
        self.assertEqual(fork["forked_from"], {"thread_id": parent, "title": before["title"]})
        self.assertEqual(fork["forked_from_turn_id"], "turn-0")
        self.assertEqual(fork["project_id"], project["id"])
        self.assertIsNone(fork["repository_id"])
        self.assertEqual(fork["attention"], "now")
        self.assertFalse(fork["pinned"])
        self.assertIsNone(fork["checkpoint"])
        self.assertIsNone(fork["turn_completion"])
        self.assertFalse(fork["unread"])
        self.assertEqual(fork["original_request"], before["original_request"])
        self.assertEqual(self.store.get_session(child)["write_scope"], [])
        self.assertIsNone(self.store.get_session(child)["bead_id"])
        self.assertEqual(self.sessions.queue.list(child), [])
        self.assertEqual(self.sessions.pending_requests(child), [])
        self.assertEqual(self.store.threads.get(parent, history=True), before)
        self.assertEqual(self.sessions.rpc.threads[parent], original_history)
        self.assertEqual({t["thread_id"] for t in self.sessions.list_work_threads()}, {parent, child})
        detail = self.sessions.read(child)
        self.assertEqual(detail["thread"]["turns"], original_history["turns"])
        self.assertEqual((detail["session"]["model"], detail["session"]["effort"], detail["session"]["yolo"]),
                         ("available-model", "high", 1))
        resume = next(p for m, p in self.sessions.rpc.calls if m == "thread/resume")
        self.assertEqual(resume["threadId"], child)
        self.assertIn("checkpoint --session-id " + child, resume["developerInstructions"])
        self.assertNotIn("checkpoint --session-id " + parent, resume["developerInstructions"])
        self.assertEqual(resume["sandbox"], "danger-full-access")
        self.assertEqual(sum(m == "turn/start" for m, _ in self.sessions.rpc.calls), 1)
        self.sessions.send(child, {"message": "Try another approach"})
        self.assertEqual(self.sessions.rpc.threads[parent], original_history)
        self.assertEqual(self.store.threads.get(parent, history=True), before)

    def test_fork_survives_restart_and_resume_failure_without_another_fork(self):
        parent = self.create()
        self.sessions.rpc.fast_turn = True
        self.sessions.send(parent, {"message": "Original"})
        child = self.sessions.fork_work_thread(parent)["thread_id"]
        with patch.object(self.sessions.rpc, "request", side_effect=CoordinationError("Connection lost")):
            with self.assertRaisesRegex(CoordinationError, "Connection lost"):
                self.sessions.read(child)
        self.sessions.close()
        reopened = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(reopened.close)
        reopened.rpc.threads = self.sessions.rpc.threads
        detail = reopened.read(child)
        self.assertEqual(detail["work_thread"]["forked_from_thread_id"], parent)
        self.assertEqual(len(detail["thread"]["turns"]), 1)
        self.assertFalse(detail["work_thread"]["unread"])
        self.assertFalse(any(m == "thread/fork" for m, _ in reopened.rpc.calls))

    def test_fork_rejects_busy_closed_claude_and_online_terminal_sources(self):
        parent = self.create()
        self.sessions.send(parent, {"message": "Working"})
        self.assertFalse(self.sessions.work_thread(parent)["can_fork"])
        with self.assertRaises(BrowserBusyError):
            self.sessions.fork_work_thread(parent)
        self.sessions.interrupt(parent)
        self.sessions.requests["pending"] = {"params": {"threadId": parent}}
        with self.assertRaises(BrowserBusyError):
            self.sessions.fork_work_thread(parent)
        self.sessions.requests.clear()
        self.store.threads.update(parent, attention="archived")
        with self.assertRaises(CoordinationError):
            self.sessions.fork_work_thread(parent)
        for client in ("claude", "codex"):
            self.store.register(session_id=client, client=client, cwd=str(self.root))
            with self.assertRaises(CoordinationError):
                self.sessions.fork_work_thread(client)
        self.assertFalse(any(m == "thread/fork" for m, _ in self.sessions.rpc.calls))

    def test_fork_rechecks_native_activity_and_rpc_failure_leaves_parent_intact(self):
        parent = self.create()
        before = self.store.threads.get(parent, history=True)
        self.sessions.rpc.threads[parent]["status"] = {"type": "active"}
        with self.assertRaises(BrowserBusyError):
            self.sessions.fork_work_thread(parent)
        self.sessions.rpc.threads[parent]["status"] = {"type": "idle"}
        request = self.sessions.rpc.request
        def fail_fork(method, params=None):
            if method == "thread/fork":
                raise CoordinationError("Fork unavailable")
            return request(method, params)
        with patch.object(self.sessions.rpc, "request", side_effect=fail_fork):
            with self.assertRaisesRegex(CoordinationError, "Fork unavailable"):
                self.sessions.fork_work_thread(parent)
        self.assertEqual([t["thread_id"] for t in self.sessions.list_work_threads()], [parent])
        self.assertEqual(self.store.threads.get(parent, history=True), before)

    def test_saved_terminal_can_fork_without_resuming_parent(self):
        self.store.register(session_id="terminal", client="codex", cwd=str(self.root))
        self.store.end_session("terminal")
        self.sessions.rpc.threads["terminal"] = {"id": "terminal", "turns": []}
        child = self.sessions.fork_work_thread("terminal")["thread_id"]
        self.assertTrue(self.sessions.work_thread(child)["browser_session"])
        self.assertFalse(self.sessions.work_thread("terminal")["browser_session"])
        self.assertEqual(self.store.get_session("terminal")["presence"], "offline")
        self.assertFalse(any(m == "thread/resume" for m, _ in self.sessions.rpc.calls))
        params = next(p for m, p in self.sessions.rpc.calls if m == "thread/fork")
        self.assertEqual(params["sandbox"], "workspace-write")
        self.assertEqual(params["approvalPolicy"], "on-request")

    def test_fork_is_workspace_scoped(self):
        parent = self.create()
        subdir = self.root / "subdir"
        subdir.mkdir()
        filtered = BrowserSessions(self.store, str(subdir), rpc_factory=FakeCodex)
        self.addCleanup(filtered.close)
        with self.assertRaisesRegex(CoordinationError, "workspace"):
            filtered.fork_work_thread(parent)
        self.assertEqual(filtered.rpc.calls, [])

    def test_slash_commands_change_next_turn_without_sending_prompt(self):
        thread_id = self.create()
        self.assertIn("other-model", self.sessions.send(thread_id, {"message": "/model"})["command"]["message"])
        self.assertIn("high", self.sessions.send(thread_id, {"message": "/effort"})["command"]["message"])
        self.assertIn("/model", self.sessions.send(thread_id, {"message": "/help"})["command"]["message"])
        self.sessions.send(thread_id, {"message": "/effort high"})
        switched = self.sessions.send(thread_id, {"message": "/model other-model"})
        self.assertEqual((switched["session"]["model"], switched["session"]["effort"]), ("other-model", "low"))
        self.assertFalse(any(method == "turn/start" for method, _ in self.sessions.rpc.calls))
        self.assertFalse(self.store.threads.get(thread_id)["original_request"])
        self.sessions.send(thread_id, {"message": "Real prompt"})
        turn = self.sessions.rpc.calls[-1][1]
        self.assertEqual((turn["model"], turn["effort"]), ("other-model", "low"))
        self.assertEqual(turn["input"][0]["text"], "Real prompt")

    def test_model_settings_persist_across_restart_and_reject_invalid_or_busy_changes(self):
        thread_id = self.create()
        for message in ("/model missing", "/effort ultra", "/model other-model high", "/effort high extra", "/model other-model low extra"):
            with self.assertRaises(CoordinationError):
                self.sessions.send(thread_id, {"message": message})
        self.assertEqual(self.sessions._record(thread_id)["model"], "available-model")
        self.sessions.send(thread_id, {"message": "/model other-model low"})
        self.sessions.close()
        reopened = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(reopened.close)
        reopened.rpc.threads = self.sessions.rpc.threads
        record = reopened.read(thread_id)["session"]
        self.assertEqual((record["model"], record["effort"]), ("other-model", "low"))
        reopened.send(thread_id, {"message": "Start work"})
        with self.assertRaises(BrowserBusyError):
            reopened.send(thread_id, {"message": "/model available-model"})
        with self.assertRaises(BrowserBusyError):
            reopened.update(thread_id, {"effort": "low"})
        self.assertEqual(reopened._record(thread_id)["model"], "other-model")

    def test_yolo_is_explicit_persists_and_applies_to_start_resume_and_turn(self):
        for invalid in ("true", 1, None):
            with self.assertRaisesRegex(CoordinationError, "YOLO"):
                self.create(yolo=invalid)
        thread_id = self.create(yolo=True)
        options = next(params for method, params in reversed(self.sessions.rpc.calls) if method == "thread/start")
        self.assertEqual(options["approvalPolicy"], "never")
        self.assertEqual(options["sandbox"], "danger-full-access")
        self.sessions.close()
        reopened = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(reopened.close)
        reopened.rpc.threads = self.sessions.rpc.threads
        self.assertTrue(reopened.read(thread_id)["session"]["yolo"])
        options = next(params for method, params in reopened.rpc.calls if method == "thread/resume")
        self.assertEqual(options["approvalPolicy"], "never")
        self.assertEqual(options["sandbox"], "danger-full-access")
        reopened.send(thread_id, {"message": "Work"})
        options = reopened.rpc.calls[-1][1]
        self.assertEqual(options["approvalPolicy"], "never")
        self.assertEqual(options["sandboxPolicy"], {"type": "dangerFullAccess"})
        with self.assertRaises(BrowserBusyError):
            reopened.update(thread_id, {"yolo": False})
        reopened.interrupt(thread_id)
        self.assertFalse(reopened.update(thread_id, {"yolo": False})["yolo"])
        reopened.send(thread_id, {"message": "Safer work"})
        options = reopened.rpc.calls[-1][1]
        self.assertEqual(options["approvalPolicy"], "on-request")
        self.assertEqual(options["sandboxPolicy"], {
            "type": "workspaceWrite", "writableRoots": [str(self.root)], "networkAccess": False,
        })

    def test_yolo_can_be_enabled_on_an_existing_idle_session(self):
        thread_id = self.create()
        for invalid in ("true", 1, None):
            with self.assertRaisesRegex(CoordinationError, "YOLO"):
                self.sessions.update(thread_id, {"yolo": invalid})
        self.assertFalse(self.sessions._record(thread_id)["yolo"])
        self.assertTrue(self.sessions.update(thread_id, {"yolo": True})["yolo"])
        self.sessions.rpc.fast_turn = True
        self.sessions.send(thread_id, {"message": "Work"})
        options = self.sessions.rpc.calls[-1][1]
        self.assertEqual(options["approvalPolicy"], "never")
        self.assertEqual(options["sandboxPolicy"], {"type": "dangerFullAccess"})
        self.sessions.close()
        reopened = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(reopened.close)
        reopened.rpc.threads = self.sessions.rpc.threads
        self.assertTrue(reopened.read(thread_id)["session"]["yolo"])
        options = next(params for method, params in reopened.rpc.calls if method == "thread/resume")
        self.assertEqual(options["approvalPolicy"], "never")
        self.assertEqual(options["sandbox"], "danger-full-access")
        reopened.update(thread_id, {"archived": True})
        with self.assertRaisesRegex(CoordinationError, "Restore"):
            reopened.update(thread_id, {"yolo": False})

    def test_existing_database_migrates_yolo_to_off(self):
        self.sessions.close()
        with self.store._connection() as db:
            db.execute("DROP TABLE browser_sessions")
            db.execute("CREATE TABLE browser_sessions (thread_id TEXT PRIMARY KEY, cwd TEXT NOT NULL, name TEXT NOT NULL, model TEXT, effort TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL, archived INTEGER NOT NULL DEFAULT 0)")
        for _ in range(2):
            reopened = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
            try:
                tid = reopened.create({"name": "Migrated"})["session"]["thread_id"]
                self.assertFalse(reopened._record(tid)["yolo"])
            finally:
                reopened.close()
            with self.store._connection() as db:
                db.execute("DELETE FROM browser_sessions")

    def test_independent_session_registers_without_bead_and_preserves_settings(self):
        thread_id = self.create(model="available-model", effort="high")
        session = self.store.get_session(thread_id)
        self.assertIsNone(session["bead_id"])
        self.assertEqual(session["write_scope"], [])
        self.assertEqual(session["name"], "Feature discussion")
        options = self.sessions.rpc.calls[-1][1]
        self.assertEqual(options["approvalPolicy"], "on-request")
        self.assertEqual(options["approvalsReviewer"], "user")
        self.assertEqual(options["sandbox"], "workspace-write")
        self.assertEqual(options["config"]["model_reasoning_effort"], "high")

    def test_history_and_names_survive_service_restart(self):
        thread_id = self.create()
        self.sessions.rpc.fast_turn = True
        self.sessions.send(thread_id, {"message": "Continue the project."})
        self.sessions.update(thread_id, {"name": "Project"})
        self.sessions.close()
        reopened = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(reopened.close)
        reopened.rpc.threads = self.sessions.rpc.threads
        self.assertEqual(reopened.list_sessions()[0]["status"], "saved")
        result = reopened.read(thread_id)
        self.assertEqual(result["session"]["name"], "Project")
        self.assertEqual(result["thread"]["turns"][0]["items"][0]["content"][0]["text"], "Continue the project.")
        self.assertEqual(reopened.rpc.calls[0][0], "thread/resume")

    def test_parallel_sessions_steer_without_starting_duplicate_turns(self):
        first, second = self.create(), self.create()
        self.sessions.send(first, {"message": "First"})
        self.sessions.send(second, {"message": "Second"})
        result = self.sessions.send(first, {"message": "Focus on the UI"})
        self.assertEqual(result, {"turnId": self.sessions.active[first]})
        self.assertEqual(len(self.sessions.rpc.threads[first]["turns"]), 1)
        self.assertEqual(len(self.sessions.active), 2)
        self.sessions.interrupt(first)
        self.assertNotIn(first, self.sessions.active)
        self.assertIn(second, self.sessions.active)

    def test_stop_targets_latest_turn_despite_old_in_progress_history(self):
        thread_id = self.create()
        turn = self.sessions.send(thread_id, {"message": "Current work"}, start_only=True)["turn"]
        self.sessions.rpc.threads[thread_id]["turns"].insert(0, {"id": "stale", "status": "inProgress", "items": []})
        self.assertEqual(self.sessions.read(thread_id)["activeTurn"], turn["id"])
        self.sessions.interrupt(thread_id)
        self.assertEqual(self.sessions.rpc.calls[-1], ("turn/interrupt", {"threadId": thread_id, "turnId": turn["id"]}))
        self.assertFalse(self.sessions.read(thread_id)["running"])

    def test_old_in_progress_history_does_not_resurrect_after_latest_completion(self):
        thread_id = self.create()
        self.sessions.rpc.threads[thread_id]["turns"] = [
            {"id": "stale", "status": "inProgress", "items": []},
            {"id": "finished", "status": "completed", "items": []},
        ]
        self.assertFalse(self.sessions.read(thread_id)["running"])
        self.sessions.rpc.fast_turn = True
        self.sessions.send(thread_id, {"message": "Next work"}, start_only=True)
        self.assertEqual(len(self.sessions.rpc.threads[thread_id]["turns"]), 3)

    def test_stop_uses_current_turn_with_separate_stored_history_reader(self):
        thread_id = self.create()
        turn = self.sessions.send(thread_id, {"message": "Current work"})["turn"]
        self.sessions.rpc.no_history = True
        self.history_reader(thread_id, [
            {"id": "stale", "status": "inProgress", "items": []}, turn,
        ])
        self.assertEqual(self.sessions.read(thread_id)["activeTurn"], turn["id"])
        self.sessions.interrupt(thread_id)
        self.assertFalse(self.sessions.read(thread_id)["running"])

    def test_read_keeps_new_turn_started_while_history_response_is_in_flight(self):
        thread_id = self.create()
        old = self.sessions.send(thread_id, {"message": "Old work"})["turn"]
        original = self.sessions.rpc.request

        def request(method, params=None):
            result = original(method, params)
            if method == "thread/read":
                original("turn/interrupt", {"threadId": thread_id, "turnId": old["id"]})
                original("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": "New work"}]})
            return result

        with patch.object(self.sessions.rpc, "request", side_effect=request):
            detail = self.sessions.read(thread_id)
        current = self.sessions.rpc.threads[thread_id]["turns"][-1]["id"]
        self.assertEqual(detail["activeTurn"], current)
        self.sessions.interrupt(thread_id)

    def test_read_does_not_resurrect_turn_completed_during_history_request(self):
        thread_id = self.create()
        turn = self.sessions.send(thread_id, {"message": "Work"})["turn"]
        original = self.sessions.rpc.request

        def request(method, params=None):
            result = original(method, params)
            if method == "thread/read":
                original("turn/interrupt", {"threadId": thread_id, "turnId": turn["id"]})
            return result

        with patch.object(self.sessions.rpc, "request", side_effect=request):
            self.assertFalse(self.sessions.read(thread_id)["running"])

    def test_incomplete_history_does_not_replace_streamed_active_turn(self):
        thread_id = self.create()
        turn = self.sessions.send(thread_id, {"message": "Current work"})["turn"]
        stale = {"id": thread_id, "status": {"type": "active"}, "turns": [
            {"id": "stale", "status": "inProgress", "items": []},
        ]}
        with patch.object(self.sessions, "_read_thread", return_value=stale):
            self.assertEqual(self.sessions.read(thread_id)["activeTurn"], turn["id"])

    def test_steering_uses_active_turn_and_preserves_history_and_original_request(self):
        thread_id = self.create()
        self.sessions.rpc.no_history = True
        turn = self.sessions.send(thread_id, {"message": "Original request"})["turn"]
        result = self.sessions.send(thread_id, {"message": "New direction", "expectedTurnId": turn["id"]})
        self.assertEqual(result, {"turnId": turn["id"]})
        self.assertEqual(self.sessions.rpc.calls[-1], ("turn/steer", {
            "threadId": thread_id, "expectedTurnId": turn["id"], "input": [{"type": "text", "text": "New direction"}],
        }))
        self.assertEqual(self.store.threads.get(thread_id)["original_request"], "Original request")
        detail = self.sessions.read(thread_id)
        self.assertTrue(detail["running"])
        self.assertEqual(len(detail["thread"]["turns"]), 1)
        self.assertEqual(detail["thread"]["turns"][0]["items"][-1]["content"][0]["text"], "New direction")
        with self.store._connection() as db:
            history = json.loads(db.execute("SELECT history_json FROM browser_history WHERE thread_id = ?", (thread_id,)).fetchone()[0])
        self.assertEqual(history["turns"][0]["items"][-1]["content"][0]["text"], "New direction")

    def test_stale_steering_does_not_start_or_target_another_turn(self):
        thread_id = self.create()
        turn = self.sessions.send(thread_id, {"message": "Start"})["turn"]
        self.sessions.interrupt(thread_id)
        with self.assertRaisesRegex(BrowserBusyError, "changed or finished"):
            self.sessions.send(thread_id, {"message": "Too late", "expectedTurnId": turn["id"]})
        self.assertEqual(len(self.sessions.rpc.threads[thread_id]["turns"]), 1)
        self.sessions.send(thread_id, {"message": "Next turn"})
        with self.assertRaisesRegex(BrowserBusyError, "changed or finished"):
            self.sessions.send(thread_id, {"message": "Wrong turn", "expectedTurnId": turn["id"]})
        self.assertFalse(any(method == "turn/steer" for method, _ in self.sessions.rpc.calls))

    def test_failed_steering_preserves_active_turn_and_can_retry(self):
        thread_id = self.create()
        turn = self.sessions.send(thread_id, {"message": "Start"})["turn"]
        self.sessions.rpc.fail_steer = True
        with self.assertRaisesRegex(CoordinationError, "Steering unavailable"):
            self.sessions.send(thread_id, {"message": "Adjust"})
        self.assertEqual(self.sessions.active[thread_id], turn["id"])
        self.assertEqual(len(self.sessions.rpc.threads[thread_id]["turns"][0]["items"]), 1)
        self.sessions.rpc.fail_steer = False
        self.assertEqual(self.sessions.send(thread_id, {"message": "Adjust"}), {"turnId": turn["id"]})

    def test_steering_rejects_invalid_expected_turn_ids(self):
        thread_id = self.create()
        for value in (None, "", [], 1):
            with self.assertRaisesRegex(CoordinationError, "Expected turn ID"):
                self.sessions.send(thread_id, {"message": "Adjust", "expectedTurnId": value})
        self.assertFalse(any(method in {"turn/start", "turn/steer"} for method, _ in self.sessions.rpc.calls))

    def test_completion_during_steering_does_not_restart_the_turn(self):
        thread_id = self.create()
        turn = self.sessions.send(thread_id, {"message": "Start"})["turn"]
        request = self.sessions.rpc.request

        def complete_before_steer(method, params=None):
            if method == "turn/steer":
                request("turn/interrupt", {"threadId": thread_id, "turnId": turn["id"]})
            return request(method, params)

        self.sessions.rpc.request = complete_before_steer
        with self.assertRaisesRegex(CoordinationError, "Active turn mismatch"):
            self.sessions.send(thread_id, {"message": "Too late", "expectedTurnId": turn["id"]})
        self.assertNotIn(thread_id, self.sessions.active)
        self.assertEqual(len(self.sessions.rpc.threads[thread_id]["turns"]), 1)

    def test_streamed_history_survives_when_codex_cannot_query_stored_turns(self):
        thread_id = self.create()
        self.sessions.rpc.no_history = True
        self.assertEqual(self.sessions.read(thread_id)["thread"]["turns"], [])
        turn = self.sessions.send(thread_id, {"message": "Remember this"})["turn"]
        params = {"threadId": thread_id, "turnId": turn["id"]}
        self.sessions._event({"method": "item/started", "params": {**params, "item": {"id": "answer", "type": "agentMessage", "text": ""}}})
        for text in ["Hello", " again"]:
            self.sessions._event({"method": "item/agentMessage/delta", "params": {**params, "itemId": "answer", "delta": text}})
        self.sessions._event({"method": "turn/completed", "params": {"threadId": thread_id, "turn": {"id": turn["id"], "status": "completed", "items": []}}})
        self.assertEqual(self.sessions.read(thread_id)["thread"]["turns"][0]["items"][-1]["text"], "Hello again")
        self.sessions.close()
        reopened = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(reopened.close)
        reopened.rpc.threads = self.sessions.rpc.threads
        reopened.rpc.no_history = True
        thread = reopened.read(thread_id)["thread"]
        self.assertEqual(thread["turns"][0]["items"][-1]["text"], "Hello again")
        self.assertEqual(thread["turns"][0]["status"], "completed")

    def history_reader(self, thread_id, turns):
        reader = FakeCodex(self.store, lambda event: None)
        reader.threads[thread_id] = {"id": thread_id, "turns": turns, "status": {"type": "notLoaded"}}
        self.sessions.history_rpc = reader
        return reader

    def test_first_message_read_recovers_when_rollout_metadata_is_delayed(self):
        thread_id = self.create()
        turn = self.sessions.send(thread_id, {"message": "First message"})["turn"]
        self.sessions.rpc.empty_rollout_reads = {True: 2}
        with patch("agent_coord.codex_app_server.time.sleep") as sleep:
            result = self.sessions.read(thread_id)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(result["thread"]["turns"], [turn])
        self.assertEqual(result["activeTurn"], turn["id"])
        self.assertTrue(result["running"])
        self.assertFalse(result["thread"]["historyUnavailable"])
        self.assertEqual(sum(method == "turn/start" for method, _ in self.sessions.rpc.calls), 1)

    def test_summary_read_retries_empty_rollout_with_unsupported_loaded_history(self):
        thread_id = self.create()
        turn = self.sessions.send(thread_id, {"message": "First message"})["turn"]
        self.sessions.rpc.no_history = True
        self.sessions.rpc.empty_rollout_reads = {False: 1}
        self.history_reader(thread_id, [turn])
        with patch("agent_coord.codex_app_server.time.sleep") as sleep:
            result = self.sessions.read(thread_id)
        self.assertEqual(sleep.call_count, 1)
        self.assertEqual(result["thread"]["turns"], [turn])
        self.assertTrue(result["running"])
        self.assertFalse(result["thread"]["historyUnavailable"])

    def test_separate_history_reader_retries_delayed_metadata(self):
        thread_id = self.create()
        turn = self.sessions.send(thread_id, {"message": "First message"})["turn"]
        self.sessions.rpc.no_history = True
        reader = self.history_reader(thread_id, [turn])
        reader.empty_rollout_reads = {True: 2}
        with patch("agent_coord.codex_app_server.time.sleep") as sleep:
            result = self.sessions.read(thread_id)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(result["thread"]["turns"], [turn])
        self.assertFalse(result["thread"]["historyUnavailable"])
        self.assertFalse(reader.closed)

    def test_persistently_empty_rollout_still_fails_without_losing_the_active_turn(self):
        thread_id = self.create()
        turn = self.sessions.send(thread_id, {"message": "First message"})["turn"]
        self.sessions.rpc.empty_rollout_reads = {True: 100}
        with patch("agent_coord.codex_app_server.time.sleep") as sleep:
            with self.assertRaises(CoordinationError) as caught:
                self.sessions.read(thread_id)
        self.assertEqual(str(caught.exception), EMPTY_ROLLOUT_ERROR)
        self.assertGreater(sleep.call_count, 0)
        self.assertLessEqual(sum(call.args[0] for call in sleep.call_args_list), 1)
        self.assertEqual(self.sessions.active[thread_id], turn["id"])
        self.assertEqual(self.sessions._history(thread_id)["turns"], [turn])
        self.sessions.rpc.empty_rollout_reads.clear()
        self.assertEqual(self.sessions.read(thread_id)["thread"]["turns"], [turn])
        self.assertEqual(sum(method == "turn/start" for method, _ in self.sessions.rpc.calls), 1)

    def test_thread_read_does_not_retry_unrelated_storage_or_transport_errors(self):
        thread_id = self.create()
        for error in ("Thread not found.", "Connection to Codex app-server was lost.",
                      EMPTY_ROLLOUT_ERROR.replace("is empty", "contains invalid JSON"),
                      "failed to read thread: permission denied"):
            with self.subTest(error=error):
                with patch.object(self.sessions.rpc, "request", side_effect=CoordinationError(error)) as request:
                    with patch("agent_coord.codex_app_server.time.sleep") as sleep:
                        with self.assertRaises(CoordinationError) as caught:
                            self.sessions.read(thread_id)
                self.assertEqual(str(caught.exception), error)
                self.assertEqual(request.call_count, 1)
                sleep.assert_not_called()

    def test_resumed_terminal_history_is_recovered_without_resuming_the_history_reader(self):
        thread_id = "terminal"
        self.store.register(session_id=thread_id, client="codex", cwd=str(self.root))
        self.store.end_session(thread_id)
        turns = [{"id": "old-turn", "status": "completed", "items": [
            {"id": "user", "type": "userMessage", "content": [{"type": "text", "text": "Original request"}]},
            {"id": "answer", "type": "agentMessage", "text": "Saved answer"}]}]
        self.sessions.rpc.threads[thread_id] = {"id": thread_id, "turns": turns, "status": {"type": "idle"}}
        self.sessions.rpc.no_history = True
        reader = self.history_reader(thread_id, turns)
        self.sessions.resume_work_thread(thread_id)
        result = self.sessions.read(thread_id)
        self.assertEqual(result["thread"]["turns"], turns)
        self.assertFalse(result["thread"]["historyUnavailable"])
        self.assertEqual(result["thread"]["status"], {"type": "idle"})
        self.assertEqual(reader.calls, [("thread/read", {"threadId": thread_id, "includeTurns": True})])
        with self.store._connection() as db:
            saved = json.loads(db.execute("SELECT history_json FROM browser_history WHERE thread_id = ?", (thread_id,)).fetchone()[0])
        self.assertEqual(saved["turns"], turns)
        self.sessions.close()
        self.assertTrue(reader.closed)

    def test_one_unsupported_thread_does_not_disable_history_for_other_threads(self):
        first, second = self.create(), self.create()
        self.sessions.rpc.no_history = True
        self.assertTrue(self.sessions.read(first)["thread"]["historyUnavailable"])
        self.sessions.rpc.no_history = False
        turns = [{"id": "stored", "status": "completed", "items": [
            {"id": "answer", "type": "agentMessage", "text": "Other thread history"}]}]
        self.sessions.rpc.threads[second]["turns"] = turns
        result = self.sessions.read(second)["thread"]
        self.assertEqual(result["turns"], turns)
        self.assertFalse(result["historyUnavailable"])

    def test_failed_history_read_keeps_cached_messages_and_can_recover(self):
        thread_id = self.create()
        turns = [{"id": "cached", "status": "completed", "items": [
            {"id": "answer", "type": "agentMessage", "text": "Cached answer"}]}]
        self.sessions._remember_thread({"id": thread_id, "turns": turns})
        self.sessions.rpc.no_history = True
        failed = self.history_reader(thread_id, turns)
        failed.no_history = True
        result = self.sessions.read(thread_id)["thread"]
        self.assertTrue(result["historyUnavailable"])
        self.assertEqual(result["turns"], turns)
        self.assertTrue(failed.closed)
        self.assertIsNone(self.sessions.history_rpc)
        self.assertFalse(self.sessions.rpc.closed)
        reader = self.history_reader(thread_id, turns)
        recovered = self.sessions.read(thread_id)["thread"]
        self.assertFalse(recovered["historyUnavailable"])
        self.assertEqual(recovered["turns"], turns)
        self.assertFalse(reader.closed)

    def test_stored_history_does_not_overwrite_live_deltas_or_running_state(self):
        thread_id = self.create()
        turn = self.sessions.send(thread_id, {"message": "Continue"})["turn"]
        older = {"id": "older", "status": "completed", "items": [
            {"id": "old-answer", "type": "agentMessage", "text": "Before browser resume"}]}
        persisted = copy.deepcopy(turn)
        persisted["items"].append({"id": "answer", "type": "agentMessage", "text": "Partial"})
        self.sessions.rpc.threads[thread_id]["status"] = {"type": "active"}
        self.sessions.rpc.no_history = True
        reader = self.history_reader(thread_id, [older, persisted])
        params = {"threadId": thread_id, "turnId": turn["id"]}
        self.sessions._event({"method": "item/started", "params": {**params, "item": {"id": "answer", "type": "agentMessage", "text": "Partial"}}})
        self.sessions._event({"method": "item/agentMessage/delta", "params": {**params, "itemId": "answer", "delta": " live reply"}})
        result = self.sessions.read(thread_id)
        self.assertEqual([item["id"] for item in result["thread"]["turns"]], ["older", turn["id"]])
        self.assertEqual(result["thread"]["turns"][-1]["items"][-1]["text"], "Partial live reply")
        self.assertEqual(result["activeTurn"], turn["id"])
        self.assertTrue(result["running"])
        self.sessions._event({"method": "turn/completed", "params": {"threadId": thread_id, "turn": {"id": turn["id"], "status": "completed", "items": []}}})
        self.sessions.rpc.threads[thread_id]["status"] = {"type": "idle"}
        # Persisted history still has an in-progress snapshot after completion.
        result = self.sessions.read(thread_id)
        self.assertEqual(result["thread"]["turns"][-1]["status"], "completed")
        self.assertFalse(result["running"])
        self.assertTrue(all(method == "thread/read" for method, _ in reader.calls))

    def test_archived_history_recovery_does_not_resume_or_reopen_thread(self):
        thread_id = self.create()
        self.sessions.update(thread_id, {"archived": True})
        self.sessions.rpc.no_history = True
        turns = [{"id": "old", "status": "completed", "items": [
            {"id": "answer", "type": "agentMessage", "text": "Archived answer"}]}]
        reader = self.history_reader(thread_id, turns)
        self.sessions.rpc.calls.clear()
        result = self.sessions.read(thread_id)
        self.assertEqual(result["thread"]["turns"], turns)
        self.assertEqual(result["work_thread"]["attention"], "archived")
        self.assertTrue(all(method == "thread/read" for method, _ in self.sessions.rpc.calls + reader.calls))

    def test_fast_turn_completion_is_not_overwritten_by_start_response(self):
        thread_id = self.create()
        self.sessions.rpc.fast_turn = True
        self.sessions.send(thread_id, {"message": "Instant response"})
        self.assertNotIn(thread_id, self.sessions.active)
        self.assertFalse(self.store.get_session(thread_id)["turn_active"])

    def test_failed_turn_can_be_retried(self):
        thread_id = self.create()
        self.sessions.rpc.fail_turn = True
        with self.assertRaisesRegex(CoordinationError, "Provider unavailable"):
            self.sessions.send(thread_id, {"message": "Test"})
        self.sessions.rpc.fail_turn = False
        self.sessions.send(thread_id, {"message": "Retry"})
        self.assertIn(thread_id, self.sessions.active)

    def test_scope_state_survives_turn_completion(self):
        thread_id = self.create()
        self.store.begin_work(session_id=thread_id, scopes=["src/**"])
        self.sessions.rpc.fast_turn = True
        self.sessions.send(thread_id, {"message": "Keep work reserved"})
        state = self.store.get_session(thread_id)
        self.assertEqual(state["activity"], "waiting")
        self.assertEqual(state["write_scope"], ["src/**"])

    def test_archive_stop_restore_lifecycle(self):
        thread_id = self.create()
        self.sessions.send(thread_id, {"message": "Work"})
        with self.assertRaisesRegex(BrowserBusyError, "Stop"):
            self.sessions.update(thread_id, {"archived": True})
        self.sessions.interrupt(thread_id)
        self.sessions.update(thread_id, {"archived": True})
        self.assertEqual(self.sessions.list_sessions(), [])
        self.assertEqual(len(self.sessions.list_sessions(archived=True)), 1)
        self.assertEqual(self.store.get_session(thread_id)["presence"], "offline")
        with self.assertRaisesRegex(CoordinationError, "Reopen"):
            self.sessions.send(thread_id, {"message": "Cannot send"})
        self.sessions.update(thread_id, {"archived": False})
        self.sessions.send(thread_id, {"message": "Continue"})

    def test_repository_filter_applies_to_read_mutations_and_creation(self):
        thread_id = self.create()
        other = self.root / "other"
        other.mkdir()
        isolated = BrowserSessions(self.store, str(other), rpc_factory=FakeCodex)
        self.addCleanup(isolated.close)
        self.assertEqual(isolated.list_sessions(), [])
        for operation in [lambda: isolated.read(thread_id), lambda: isolated.send(thread_id, {"message": "No"}), lambda: isolated.update(thread_id, {"archived": True}), lambda: isolated.create({"cwd": str(self.root)})]:
            with self.assertRaises(CoordinationError):
                operation()
        self.assertEqual(isolated.rpc.calls, [])

    def test_invalid_model_effort_or_directory_never_creates_a_thread(self):
        for values in [{"model": "unknown"}, {"effort": "high"}, {"model": "available-model", "effort": "invalid"}, {"cwd": str(self.root / "missing")}]:
            with self.assertRaises(CoordinationError):
                self.create(**values)
        self.assertFalse(any(call[0] == "thread/start" for call in self.sessions.rpc.calls))

    def test_parent_workspace_can_create_read_and_list_child_repository_sessions(self):
        repo = self.root / "repo"
        (repo / ".git").mkdir(parents=True)
        thread_id = self.create(cwd=str(repo))
        self.assertEqual(self.sessions.rpc.calls[-1][1]["cwd"], str(repo))
        self.assertEqual(self.sessions.read(thread_id)["session"]["cwd"], str(repo))
        self.assertEqual([item["thread_id"] for item in self.sessions.list_sessions()], [thread_id])
        self.assertEqual([item["thread_id"] for item in self.sessions.list_work_threads()], [thread_id])
        self.sessions.send(thread_id, {"message": "Work inside the selected repository."})
        self.sessions.interrupt(thread_id)
        self.sessions.update_work_thread(thread_id, {"attention": "later"})
        self.assertEqual(self.sessions.work_thread(thread_id)["attention"], "later")

    def test_workspace_choices_include_previous_workspaces_without_starting_codex(self):
        nested = self.root / "group/project"
        nested.mkdir(parents=True)
        self.store.register(session_id="previous", client="codex", cwd=str(nested))
        self.store.end_session("previous")
        choices = self.sessions.list_workspaces()
        self.assertIn(str(nested), [item["cwd"] for item in choices])
        self.assertEqual(self.sessions.rpc.calls, [])

    def test_creation_rejects_paths_and_symlinks_outside_the_workspace(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        outside = Path(temporary.name).resolve()
        (self.root / "escape").symlink_to(outside, target_is_directory=True)
        for path in (outside, self.root / ".." / outside.name, self.root / "escape"):
            with self.subTest(path=path), self.assertRaises(CoordinationError):
                self.create(cwd=str(path))
        self.assertEqual(self.sessions.rpc.calls, [])

    def test_repository_filter_preserves_sessions_in_linked_worktrees(self):
        repo = self.root / "repo"
        gitdir = repo / ".git/worktrees/feature"
        gitdir.mkdir(parents=True)
        (gitdir / "commondir").write_text("../..")
        worktree = self.root / "feature"
        worktree.mkdir()
        (worktree / ".git").write_text("gitdir: " + str(gitdir))
        first = self.create(cwd=str(repo))
        second = self.create(cwd=str(worktree))
        filtered = BrowserSessions(self.store, str(repo), rpc_factory=FakeCodex)
        self.addCleanup(filtered.close)
        filtered.rpc.threads = self.sessions.rpc.threads
        self.assertEqual({item["thread_id"] for item in filtered.list_sessions()}, {first, second})
        self.assertEqual({item["thread_id"] for item in filtered.list_work_threads()}, {first, second})
        self.assertEqual(filtered.read(second)["session"]["cwd"], str(worktree))

    def pending(self, thread_id, method, **params):
        self.sessions._event({"id": 42, "method": method, "params": {"threadId": thread_id, **params}})
        return self.sessions.pending_requests(thread_id)[0]["key"]

    def test_approvals_are_scoped_and_only_answered_once(self):
        first, second = self.create(), self.create()
        key = self.pending(first, "item/commandExecution/requestApproval", command="touch project.txt")
        with self.assertRaises(CoordinationError):
            self.sessions.answer(second, key, {"decision": "accept"})
        with self.assertRaises(CoordinationError):
            self.sessions.answer(first, key, {"decision": "arbitrary"})
        self.sessions.answer(first, key, {"decision": "decline"})
        self.assertEqual(self.sessions.rpc.writes[-1], {"id": 42, "result": {"decision": "decline"}})
        with self.assertRaises(CoordinationError):
            self.sessions.answer(first, key, {"decision": "accept"})

    def test_permissions_never_grant_more_than_requested(self):
        thread_id = self.create()
        permissions = {"network": {"enabled": True}}
        key = self.pending(thread_id, "item/permissions/requestApproval", permissions=permissions)
        self.sessions.answer(thread_id, key, {"decision": "accept", "permissions": {"fileSystem": "everything"}})
        self.assertEqual(self.sessions.rpc.writes[-1]["result"], {"permissions": permissions, "scope": "turn"})

    def test_question_answers_and_unknown_request_cancellation(self):
        thread_id = self.create()
        key = self.pending(thread_id, "item/tool/requestUserInput", questions=[{"id": "choice", "question": "Which?"}])
        with self.assertRaises(CoordinationError):
            self.sessions.answer(thread_id, key, {"answers": {}})
        self.sessions.answer(thread_id, key, {"answers": {"choice": {"answers": ["First"]}}})
        self.assertEqual(self.sessions.rpc.writes[-1]["result"]["answers"]["choice"]["answers"], ["First"])
        key = self.pending(thread_id, "future/request")
        self.sessions.answer(thread_id, key, {"decision": "cancel"})
        self.assertEqual(self.sessions.rpc.writes[-1]["error"]["code"], -32601)

    def test_event_replay_reports_gaps_and_close_unblocks_waiter(self):
        self.create()
        for index in range(1505):
            self.sessions._publish("progress", {"index": index})
        result = self.sessions.events_after(0, timeout=0)
        self.assertTrue(result["reset"])
        self.assertEqual(len(result["events"]), 1500)
        with ThreadPoolExecutor() as executor:
            waiter = executor.submit(self.sessions.events_after, result["seq"], 5)
            self.sessions.close()
            self.assertTrue(all(event["method"] == "bridge/disconnected" for event in waiter.result(timeout=2)["events"]))

    def test_browser_launch_supplies_checkpoint_instructions_and_captures_intent(self):
        thread_id = self.create()
        options = next(params for method, params in self.sessions.rpc.calls if method == "thread/start")
        self.assertIn("checkpoint --session-id", options["developerInstructions"])
        self.assertIn(str(self.store.database_path), options["developerInstructions"])
        self.sessions.rpc.fast_turn = True
        self.sessions.send(thread_id, {"message": "Investigate database performance."})
        self.sessions.send(thread_id, {"message": "Consider another option."})
        self.assertEqual(self.sessions.work_thread(thread_id)["original_request"], "Investigate database performance.")

    def test_later_and_checkpoint_survive_server_restart_and_archive_restore(self):
        thread_id = self.create()
        self.sessions.checkpoint_work_thread(thread_id, {"phase": "investigation", "summary": "Review the options.", "next_action": "Choose an approach.", "next_actor": "user"})
        self.sessions.update_work_thread(thread_id, {"attention": "later"})
        self.sessions.close()
        reopened = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(reopened.close)
        reopened.rpc.threads = self.sessions.rpc.threads
        thread = reopened.list_work_threads()[0]
        self.assertEqual(thread["attention"], "later")
        self.assertEqual(thread["checkpoint"]["author"], "user")
        self.assertFalse(thread["needs_attention"])
        self.assertEqual(thread["response_state"], "input")
        reopened.update_work_thread(thread_id, {"attention": "archived"})
        self.assertEqual(reopened.list_work_threads(), [])
        with self.assertRaisesRegex(CoordinationError, "Reopen"):
            reopened.send(thread_id, {"message": "Work"})
        self.assertTrue(reopened.update_work_thread(thread_id, {"attention": "now"})["needs_attention"])
        reopened.send(thread_id, {"message": "Continue."})

    def test_terminal_threads_are_visible_without_starting_codex_and_only_resume_offline(self):
        self.store.register(session_id="terminal", client="codex", cwd=str(self.root))
        self.store.threads.start_turn("terminal", prompt="Investigate something.")
        thread = self.sessions.list_work_threads()[0]
        self.assertFalse(thread["browser_session"])
        self.assertFalse(thread["can_resume"])
        self.assertEqual(self.sessions.rpc.calls, [])
        with self.assertRaises(CoordinationError):
            self.sessions.resume_work_thread("terminal")
        self.store.end_session("terminal")
        self.sessions.rpc.threads["terminal"] = {"id": "terminal", "turns": []}
        self.assertTrue(self.sessions.resume_work_thread("terminal")["browser_session"])
        self.assertEqual(self.sessions.read("terminal")["work_thread"]["original_request"], "Investigate something.")

    def test_global_thread_list_covers_multiple_projects_and_isolated_mutations_fail(self):
        first = self.create()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        other = Path(temporary.name).resolve()
        self.store.register(session_id="other-session", client="claude", cwd=str(other))
        global_sessions = BrowserSessions(self.store, rpc_factory=FakeCodex)
        self.addCleanup(global_sessions.close)
        self.assertEqual({t["thread_id"] for t in global_sessions.list_work_threads()}, {first, "other-session"})
        self.assertEqual([t["thread_id"] for t in self.sessions.list_work_threads()], [first])
        for operation in [lambda: self.sessions.work_thread("other-session"),
                          lambda: self.sessions.update_work_thread("other-session", {"attention": "later"}),
                          lambda: self.sessions.link_work_thread("other-session", {"kind": "issue", "target": "one"}),
                          lambda: self.sessions.checkpoint_work_thread("other-session", {"phase": "discussion", "summary": "No."})]:
            with self.assertRaises(CoordinationError):
                operation()

    def test_existing_browser_history_backfills_original_request(self):
        thread_id = self.create()
        self.sessions.rpc.fast_turn = True
        self.sessions.send(thread_id, {"message": "The earlier request."})
        with self.store._connection() as db:
            db.execute("DELETE FROM turn_completions WHERE thread_id = ?", (thread_id,))
            db.execute("DELETE FROM work_threads WHERE thread_id = ?", (thread_id,))
        reopened = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.work_thread(thread_id)["original_request"], "The earlier request.")

    def test_delegated_children_remain_under_the_parent_thread(self):
        parent = self.create()
        self.store.create_delegation(parent_session_id=parent, cwd=str(self.root), bead_id="work-a", scopes=["src/**"], instructions="Investigate a subproblem.", mode="reviewed", delegation_id="delegated")
        self.store.register(session_id="child", client="codex", cwd=str(self.root))
        self.store.attach_delegation("delegated", "child")
        self.assertEqual([t["thread_id"] for t in self.sessions.list_work_threads()], [parent])
        self.assertEqual([t["thread_id"] for t in self.sessions.work_thread(parent)["children"]], ["child"])


class RPCProcessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = CoordinationStore(Path(temporary.name) / "state.sqlite3")

    def client(self, script):
        events = []
        client = CodexRPC(self.store, events.append, command=[sys.executable, "-u", "-c", script])
        self.addCleanup(client.close)
        return client, events

    def test_process_interleaves_server_requests_and_client_responses(self):
        client, events = self.client('''
import sys,json
for line in sys.stdin:
 m=json.loads(line)
 if 'id' not in m: continue
 if m.get('method') != 'initialize':
  print(json.dumps({'id':'approval','method':'item/fileChange/requestApproval','params':{'threadId':'t'}}),flush=True)
 print(json.dumps({'id':m['id'],'result':{'method':m.get('method')}}),flush=True)
''')
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda method: client.request(method), ["first", "second"]))
        self.assertEqual({r["method"] for r in results}, {"first", "second"})
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["id"], "approval")

    def test_exit_fails_pending_requests_without_waiting_for_timeout(self):
        client, _ = self.client('''
import sys,json
for line in sys.stdin:
 m=json.loads(line)
 if m.get('method')=='initialize': print(json.dumps({'id':m['id'],'result':{}}),flush=True)
 elif m.get('method')=='die': sys.exit(1)
''')
        with self.assertRaisesRegex(CoordinationError, "disconnected"):
            client.request("die", timeout=2)

    def test_timeout_cleans_pending_requests_and_close_reaps_child(self):
        client, _ = self.client('''
import sys,json
for line in sys.stdin:
 m=json.loads(line)
 if m.get('method')=='initialize': print(json.dumps({'id':m['id'],'result':{}}),flush=True)
''')
        with self.assertRaisesRegex(CoordinationError, "did not respond"):
            client.request("ignored", timeout=.03)
        self.assertEqual(client.pending, {})
        client.close()
        self.assertIsNotNone(client.process.poll())
        self.assertTrue(all(not reader.is_alive() for reader in client.readers))


if __name__ == "__main__":
    unittest.main()
