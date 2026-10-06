from __future__ import annotations

import copy
import json
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PLUGIN_SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"
sys.path.insert(0, str(PLUGIN_SCRIPTS))

from agent_coord.codex_app_server import BrowserBusyError, BrowserSessions, CodexRPC
from agent_coord.store import CoordinationError, CoordinationStore


class FakeCodex:
    def __init__(self, store, callback):
        self.callback = callback
        self.threads = {}
        self.settings = {}
        self.calls = []
        self.writes = []
        self.fail_turn = False
        self.fast_turn = False
        self.no_history = False
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
        if method in {"thread/start", "thread/read", "thread/resume"}:
            if self.no_history and method == "thread/read" and params.get("includeTurns"):
                raise CoordinationError("list_turns is not supported yet")
            thread = copy.deepcopy(self.threads[thread_id])
            if self.no_history and (method == "thread/read" or params.get("excludeTurns")):
                thread["turns"] = []
            if method in {"thread/start", "thread/resume"}:
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
        if method == "turn/interrupt":
            turn = self.threads[thread_id]["turns"][-1]
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
        with self.assertRaisesRegex(CoordinationError, "creating"):
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

    def test_parallel_sessions_work_but_duplicate_turn_is_rejected(self):
        first, second = self.create(), self.create()
        self.sessions.send(first, {"message": "First"})
        self.sessions.send(second, {"message": "Second"})
        with self.assertRaisesRegex(BrowserBusyError, "already has"):
            self.sessions.send(first, {"message": "Duplicate"})
        self.assertEqual(len(self.sessions.active), 2)
        self.sessions.interrupt(first)
        self.assertNotIn(first, self.sessions.active)
        self.assertIn(second, self.sessions.active)

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
        self.assertTrue(thread["needs_attention"])
        reopened.update_work_thread(thread_id, {"attention": "archived"})
        self.assertEqual(reopened.list_work_threads(), [])
        with self.assertRaisesRegex(CoordinationError, "Reopen"):
            reopened.send(thread_id, {"message": "Work"})
        reopened.update_work_thread(thread_id, {"attention": "now"})
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
