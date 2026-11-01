"""Native background workflows and provider-initiated foreground continuations."""
from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from test_codex_app_server import BrowserSessions, CoordinationStore, FakeCodex
from test_muse import FakeMuse
from agent_coord.desktop_runtime import busy_reasons
from agent_coord.muse_code import MuseRPC, command_id


class MuseWorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = CoordinationStore(self.root / "state.sqlite3")
        self.sessions = self.make_sessions()

    def make_sessions(self):
        sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex,
            muse_factory=lambda store, event: MuseRPC(store, event, connection_factory=FakeMuse))
        self.addCleanup(sessions.close)
        return sessions

    def start(self):
        thread = self.sessions.create({"client": "muse"})["session"]["thread_id"]
        self.sessions.send(thread, {"message": "Research this problem."})
        return thread, self.sessions.muse.connections[thread]

    def event(self, thread, method, **params):
        connection = self.sessions.muse.connections[thread]
        connection.callback({"method": method, "params": {"sessionId": connection.native_id, **params}})

    def workflow(self, thread, turn_id, *, revision=1, status="inProgress", children=None):
        self.event(thread, "item/updated", item={"itemId": "workflow", "turnId": turn_id,
            "kind": "workflow", "revision": revision, "status": status, "workflowRunId": "research-workflow",
            "fallbackText": "Workflow: research", "children": children or []})

    def complete(self, thread, turn_id, text="Research results"):
        self.event(thread, "item/completed", item={"itemId": "answer", "turnId": turn_id,
            "kind": "agentMessage", "revision": 1, "status": "completed", "text": text})
        self.event(thread, "turn/completed", turnId=turn_id, terminal="completed")

    def test_automatic_continuation_streams_and_persists_its_answer(self):
        thread, connection = self.start()
        original = connection.turn
        self.workflow(thread, original)
        self.event(thread, "turn/completed", turnId=original, terminal="completed")
        continuation = command_id()
        self.event(thread, "turn/started", turnId=continuation, commandId=continuation, viewCursor="continuation")
        self.assertEqual(self.sessions.active[thread], continuation)
        self.assertTrue(self.sessions.read(thread)["running"])
        self.event(thread, "item/started", item={"itemId": "answer", "turnId": continuation,
            "kind": "agentMessage", "revision": 1, "status": "inProgress", "text": "Research "})
        self.event(thread, "item/delta", itemId="answer", field="text", delta="results")
        self.event(thread, "item/completed", item={"itemId": "answer", "turnId": continuation,
            "kind": "agentMessage", "revision": 2, "status": "completed", "text": "Research results"})
        self.event(thread, "turn/completed", turnId=continuation, terminal="completed")
        saved = self.sessions.read(thread)["thread"]
        self.assertEqual([t["id"] for t in saved["turns"]], [original, continuation])
        answer = saved["turns"][-1]["items"][-1]
        self.assertEqual(answer["text"], "Research results")
        self.assertEqual(answer["phase"], "final_answer")
        self.sessions.close()
        self.sessions = self.make_sessions()
        self.assertEqual(self.sessions.read(thread)["thread"]["turns"][-1]["items"][-1], answer)

    def test_workflow_progress_survives_foreground_completion_and_new_turn(self):
        thread, connection = self.start()
        original = connection.turn
        self.workflow(thread, original)
        self.event(thread, "turn/completed", turnId=original, terminal="completed")
        history = self.sessions.read(thread)["thread"]
        self.assertEqual(history["turns"][0]["items"][-1]["status"], "inProgress")
        self.assertIn("Muse background workflows", busy_reasons(self.sessions))
        self.assertFalse(self.sessions.muse.release(thread))
        self.assertFalse(connection.closed)
        self.sessions.send(thread, {"message": "Report progress on the existing workflow."})
        current = connection.turn
        self.workflow(thread, original, revision=2, children=[
            {"childId": "first", "attempt": 1, "label": "Check evidence", "status": "started"}])
        history = self.sessions.read(thread)["thread"]
        self.assertEqual([t["id"] for t in history["turns"]], [original, current])
        self.assertIn("Check evidence: started", history["turns"][0]["items"][-1]["result"])
        self.assertEqual(self.sessions.active[thread], current)
        self.workflow(thread, original, revision=3, status="completed")
        self.assertNotIn("Muse background workflows", busy_reasons(self.sessions))
        self.assertEqual(self.sessions.active[thread], current)
        self.complete(thread, current)
        self.assertEqual(self.sessions.read(thread)["thread"]["turns"][0]["items"][-1]["status"], "completed")
        self.assertTrue(self.sessions.muse.release(thread))
        self.assertTrue(connection.closed)

    def test_workflow_failure_settles_without_reopening_the_finished_turn(self):
        thread, connection = self.start()
        original = connection.turn
        self.workflow(thread, original)
        self.event(thread, "turn/completed", turnId=original, terminal="completed")
        self.workflow(thread, original, revision=2, status="failed")
        history = self.sessions.read(thread)["thread"]
        self.assertEqual(history["turns"][0]["items"][-1]["status"], "failed")
        self.assertFalse(self.sessions.read(thread)["running"])
        self.assertFalse(self.sessions.muse.background_active())

    def test_replayed_turns_cannot_reopen_or_replace_a_live_turn(self):
        thread, connection = self.start()
        original = connection.turn
        self.complete(thread, original)
        self.event(thread, "turn/started", turnId=original, viewCursor="different-replay-cursor")
        self.assertFalse(self.sessions.read(thread)["running"])
        continuation = command_id()
        self.event(thread, "turn/started", turnId=continuation)
        self.event(thread, "turn/started", turnId=command_id())
        self.event(thread, "turn/completed", turnId=original, terminal="completed")
        self.assertEqual(self.sessions.active[thread], continuation)
        self.complete(thread, continuation)
        self.sessions.close()
        self.sessions = self.make_sessions()
        self.sessions.send(thread, {"message": "Another question"})
        current = self.sessions.active[thread]
        self.event(thread, "turn/started", turnId=original)
        self.assertEqual(self.sessions.active[thread], current)
        self.complete(thread, current)
        self.event(thread, "turn/started", turnId=continuation)
        self.assertFalse(self.sessions.read(thread)["running"])

    def test_duplicate_native_start_keeps_existing_items_and_approvals(self):
        thread, connection = self.start()
        current = connection.turn
        self.workflow(thread, current)
        self.event(thread, "approval/requested", approvalId="approval", turnId=current,
            currentRequirementId={"approvalId": "approval", "sourceIndex": 0}, toolName="bash",
            subject={"command": "run check"}, availableChoices=[
                {"choiceId": "allow", "decision": "approved", "scope": "once"}])
        pending = self.sessions.pending_requests(thread)[0]
        self.event(thread, "turn/started", turnId=current)
        self.assertEqual(self.sessions.pending_requests(thread)[0]["key"], pending["key"])
        self.assertEqual(self.sessions.read(thread)["thread"]["turns"][-1]["items"][-1]["tool"], "workflow")

    def test_automatic_continuation_uses_native_approval_and_stop_guards(self):
        thread, old = self.start()
        self.complete(thread, old.turn)
        current = command_id()
        self.event(thread, "turn/started", turnId=current)
        self.event(thread, "approval/requested", turnId=current, approvalId="approval",
            currentRequirementId={"approvalId": "approval", "sourceIndex": 0}, toolName="bash",
            availableChoices=[{"choiceId": "allow", "decision": "approved", "scope": "once"}])
        pending = self.sessions.pending_requests(thread)[0]
        self.assertEqual(pending["params"]["turnId"], current)
        self.sessions.answer(thread, pending["key"], {"decision": "accept"})
        self.assertEqual(old.calls[-1][0], "approval/decide")
        self.sessions.interrupt(thread)
        self.assertTrue(old.closed)
        old.callback({"method": "turn/started", "params": {"sessionId": thread, "turnId": command_id()}})
        self.assertFalse(self.sessions.read(thread)["running"])
        self.assertFalse(self.sessions.pending_requests(thread))

    def test_disconnect_and_gap_settle_background_work(self):
        for method in ("disconnected", "view/gap"):
            with self.subTest(method=method):
                thread, connection = self.start()
                self.workflow(thread, connection.turn)
                self.event(thread, "turn/completed", turnId=connection.turn, terminal="completed")
                self.event(thread, method)
                self.assertTrue(connection.closed)
                self.assertFalse(self.sessions.muse.background_active())
                item = self.sessions.read(thread)["thread"]["turns"][-1]["items"][-1]
                self.assertEqual(item["status"], "interrupted")

    def test_startup_replay_does_not_create_a_foreground_turn(self):
        thread, connection = self.start()
        self.complete(thread, connection.turn)
        self.sessions.muse.ready.discard(thread)
        before = copy.deepcopy(self.sessions.read(thread)["thread"])
        self.event(thread, "turn/started", turnId=command_id())
        self.assertEqual(self.sessions.read(thread)["thread"], before)
