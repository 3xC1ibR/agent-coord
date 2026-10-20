from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))

from agent_coord.codex_app_server import BrowserSessions
from agent_coord.hook import handle
from agent_coord.store import CoordinationError, CoordinationStore
from test_codex_app_server import FakeCodex


class ThreadAttentionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.now = 1000.0
        self.store = CoordinationStore(self.root / "coord.sqlite3", clock=lambda: self.now)
        self.store.register(session_id="terminal", client="codex", cwd=str(self.root))
        self.sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(self.sessions.close)

    def start(self, turn="one"):
        self.now += 1
        self.store.threads.start_turn("terminal", prompt="Investigate.", turn_id=turn)
        self.store.touch("terminal", turn_active=True)

    def finish(self, status="completed"):
        self.now += 1
        self.store.threads.finish_turn("terminal", status=status)
        self.store.touch("terminal", turn_active=False)

    def checkpoint(self, phase="investigation", actor="agent"):
        return self.store.threads.checkpoint("terminal", {
            "phase": phase, "summary": "Latest finding.", "next_actor": actor,
            "next_action": "" if actor == "nobody" else "Continue the investigation.",
        })

    def thread(self):
        return self.sessions.work_thread("terminal")

    def test_explicit_delivery_is_done_without_jev_and_stays_visible_after_reading(self):
        self.start()
        self.checkpoint("implementation", "agent")
        self.checkpoint("finished", "nobody")
        self.finish()
        delivered = self.thread()
        self.assertEqual(delivered["attention_reason"], "done")
        self.assertTrue(delivered["needs_attention"])
        read = self.sessions.update_work_thread("terminal", {"seen": True})
        self.assertFalse(read["needs_attention"])
        self.assertEqual(read["work_phase"], "finished")
        self.assertEqual(read["attention"], "now")

    def test_investigation_reply_survives_read_and_restart_until_next_prompt(self):
        self.start()
        self.checkpoint()
        self.finish()
        reply = self.thread()
        self.assertEqual(reply["response_state"], "reply")
        self.assertTrue(reply["needs_attention"])
        self.assertTrue(reply["unread_result"])
        read = self.sessions.update_work_thread("terminal", {"seen": True})
        self.assertEqual(read["response_state"], "reply")
        self.assertFalse(read["unread"])
        reopened = BrowserSessions(CoordinationStore(self.store.database_path), str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.work_thread("terminal")["response_state"], "reply")
        self.start("two")
        self.assertEqual(self.thread()["response_state"], "working")
        self.assertFalse(self.thread()["needs_attention"])
        self.assertIsNone(self.thread()["turn_completion"])
        self.finish()
        self.assertTrue(self.thread()["checkpoint_stale"])
        self.assertEqual(self.thread()["response_state"], "reply")

    def test_reading_progress_does_not_consume_the_final_result(self):
        self.start()
        checkpoint = self.checkpoint("finished", "nobody")["checkpoint"]
        self.assertEqual(self.thread()["response_state"], "working")
        self.sessions.update_work_thread("terminal", {"seen": True})
        self.finish()
        # A delayed receipt from the detail page can only acknowledge what it saw.
        result = self.sessions.update_work_thread("terminal", {
            "seen": True, "seen_checkpoint_id": checkpoint["id"], "seen_completion_id": 0,
        })
        self.assertEqual(result["response_state"], "reply")
        self.assertTrue(result["needs_attention"])
        self.assertTrue(result["unread_result"])
        read = self.sessions.update_work_thread("terminal", {
            "seen": True, "seen_checkpoint_id": checkpoint["id"],
            "seen_completion_id": result["turn_completion"]["id"],
        })
        self.assertFalse(read["unread"])
        self.assertEqual(read["response_state"], "reply")
        self.assertTrue(read["needs_attention"])
        self.assertFalse(self.sessions.update_work_thread("terminal", {
            "seen": True, "seen_checkpoint_id": 0, "seen_completion_id": 0,
        })["unread"], "an older receipt must not regress read state")

    def test_new_question_after_finished_task_is_a_new_conversation_turn(self):
        self.start()
        self.checkpoint("finished", "nobody")
        self.finish()
        self.start("two")
        self.assertFalse(self.thread()["unread"])
        self.assertEqual(self.thread()["response_state"], "working")
        self.finish()
        self.assertEqual(self.thread()["response_state"], "reply")
        self.assertTrue(self.thread()["unread_result"])

    def test_reply_without_checkpoint_and_external_followup_both_surface(self):
        self.start()
        self.finish()
        self.assertEqual(self.thread()["response_state"], "reply")
        self.start("two")
        self.checkpoint("deployment", "external")
        self.finish()
        self.assertEqual(self.thread()["response_state"], "reply")

    def test_progress_and_process_exit_do_not_invent_a_reply(self):
        self.start()
        self.checkpoint()
        self.assertFalse(self.thread()["needs_attention"])
        self.store.end_session("terminal")
        self.assertIsNone(self.thread()["response_state"])
        self.assertIsNone(self.thread()["turn_completion"])

    def test_explicit_user_action_survives_read_and_legacy_missing_completion(self):
        self.checkpoint("planning", "user")
        self.assertEqual(self.thread()["response_state"], "input")
        self.sessions.update_work_thread("terminal", {"seen": True, "attention": "later"})
        self.assertFalse(self.thread()["needs_attention"])
        self.assertEqual(self.thread()["attention"], "later")
        self.sessions.update_work_thread("terminal", {"attention": "archived"})
        self.assertFalse(self.thread()["needs_attention"])

    def test_loaded_session_does_not_mask_live_activity_with_saved_blocker(self):
        thread_id = self.sessions.create({"name": "Calendar editing"})["session"]["thread_id"]
        self.store.threads.checkpoint(thread_id, {
            "phase": "deployment", "summary": "Waiting for credentials.",
            "next_actor": "user", "next_action": "Run aws login.",
        })
        self.assertEqual(self.sessions.work_thread(thread_id)["response_state"], "input")
        # Hooks can report live work before this runtime receives the turn event.
        self.store.touch(thread_id, turn_active=True)
        running = self.sessions.work_thread(thread_id)
        self.assertEqual(running["status"], "running")
        self.assertEqual(running["response_state"], "working")
        self.assertFalse(running["needs_attention"])
        self.assertIsNone(running["attention_reason"])
        # A real pending approval still needs attention even while the turn is active.
        self.sessions._event({"id": 42, "method": "item/commandExecution/requestApproval",
                              "params": {"threadId": thread_id}})
        self.assertEqual(self.sessions.work_thread(thread_id)["response_state"], "input")
        self.assertTrue(self.sessions.work_thread(thread_id)["needs_attention"])
        self.sessions._event({"method": "serverRequest/resolved", "params": {"requestId": 42}})
        self.store.touch(thread_id, turn_active=False)
        self.assertEqual(self.sessions.work_thread(thread_id)["response_state"], "input")

    def test_recovered_running_turn_returns_consistent_work_thread_status(self):
        thread_id = self.sessions.create({"name": "Calendar editing"})["session"]["thread_id"]
        self.sessions.send(thread_id, {"message": "Deploy."})
        self.store.threads.checkpoint(thread_id, {
            "phase": "deployment", "summary": "Waiting for credentials.",
            "next_actor": "user", "next_action": "Run aws login.",
        })
        self.sessions.active.pop(thread_id)
        self.store.touch(thread_id, turn_active=False)
        result = self.sessions.read(thread_id)
        self.assertTrue(result["running"])
        self.assertEqual(result["work_thread"]["response_state"], "working")
        self.assertFalse(result["work_thread"]["needs_attention"])

    def test_resuming_blocked_turn_invalidates_old_request(self):
        self.start()
        self.checkpoint("deployment", "user")
        self.finish()
        self.assertEqual(self.thread()["attention_reason"], "blocked")
        self.start("resumed")
        self.assertTrue(self.thread()["checkpoint_stale"])
        self.assertEqual(self.thread()["response_state"], "working")
        self.assertFalse(self.thread()["needs_attention"])
        self.finish()
        self.assertEqual(self.thread()["response_state"], "reply")
        self.assertNotEqual(self.thread()["attention_reason"], "blocked")

    def test_dismiss_saved_blocker_preserves_action_and_changed_blocker_resurfaces(self):
        for completed in (False, True):
            with self.subTest(completed=completed):
                if completed:
                    self.start()
                self.checkpoint("deployment", "user")
                if completed:
                    self.finish()
                before = self.thread()
                self.assertTrue(before["can_handle_response"])
                receipt = {"handled": True, "handled_checkpoint_id": before["checkpoint"]["id"],
                           "handled_completion_id": (before["turn_completion"] or {}).get("id", 0)}
                dismissed = self.sessions.update_work_thread("terminal", receipt)
                self.assertFalse(dismissed["needs_attention"])
                self.assertFalse(dismissed["can_handle_response"])
                self.assertEqual(dismissed["response_state"], "input")
                self.assertEqual(dismissed["attention_reason"], "blocked")
                self.assertEqual(dismissed["checkpoint"], before["checkpoint"])
                self.assertEqual(dismissed["attention"], "now")
                self.assertEqual(dismissed["work_phase"], "deployment")
                self.assertEqual(self.sessions.rpc.calls, [])
                self.now += 1
                self.checkpoint("deployment", "user")
                self.assertFalse(self.thread()["needs_attention"], "An unchanged heartbeat stays dismissed")
                self.now += 1
                self.store.threads.checkpoint("terminal", {
                    "phase": "deployment", "summary": "A different credential is required.",
                    "next_actor": "user", "next_action": "Sign in to the deployment account.",
                })
                changed = self.thread()
                self.assertTrue(changed["needs_attention"])
                self.assertTrue(changed["can_handle_response"])
                delayed = self.sessions.update_work_thread("terminal", receipt)
                self.assertTrue(delayed["needs_attention"], "An old receipt cannot dismiss a changed blocker")

    def test_changed_completed_checkpoint_resurfaces_after_dismissal(self):
        self.start()
        self.checkpoint()
        self.finish()
        before = self.thread()
        self.sessions.update_work_thread("terminal", {"handled": True,
            "handled_checkpoint_id": before["checkpoint"]["id"],
            "handled_completion_id": before["turn_completion"]["id"]})
        self.now += 1
        self.checkpoint("investigation", "nobody")
        changed = self.thread()
        self.assertTrue(changed["unhandled_response"])
        self.assertTrue(changed["needs_attention"])
        self.assertNotEqual(changed["attention_key"], before["attention_key"], "Roll up must treat the changed result as new")
        self.assertEqual(changed["attention_since"], self.now)

    def test_dismissal_cannot_answer_live_approval_or_resume_running_work(self):
        thread_id = self.sessions.create({"name": "Paused deployment"})["session"]["thread_id"]
        self.sessions.send(thread_id, {"message": "Deploy."})
        self.store.threads.checkpoint(thread_id, {
            "phase": "deployment", "summary": "Waiting for approval.",
            "next_actor": "user", "next_action": "Approve the deployment command.",
        })
        self.sessions._event({"id": 42, "method": "item/commandExecution/requestApproval",
                              "params": {"threadId": thread_id}})
        pending = dict(self.sessions.requests)
        calls = list(self.sessions.rpc.calls)
        before = self.sessions.work_thread(thread_id)
        self.assertTrue(before["needs_attention"])
        self.assertFalse(before["can_handle_response"])
        receipt = {"handled": True, "handled_checkpoint_id": before["checkpoint"]["id"],
                   "handled_completion_id": 0}
        with self.assertRaises(CoordinationError):
            self.sessions.update_work_thread(thread_id, receipt)
        self.assertEqual(self.sessions.requests, pending)
        self.assertEqual(self.sessions.rpc.calls, calls)
        self.assertEqual(self.sessions.work_thread(thread_id), before)
        self.sessions._event({"method": "serverRequest/resolved", "params": {"requestId": 42}})
        self.assertEqual(self.sessions.work_thread(thread_id)["response_state"], "working")
        with self.assertRaises(CoordinationError):
            self.sessions.update_work_thread(thread_id, receipt)

    def test_later_hides_existing_response_but_surfaces_new_result_without_moving(self):
        self.start()
        self.checkpoint()
        self.finish()
        parked = self.sessions.update_work_thread("terminal", {"attention": "later"})
        self.assertFalse(parked["needs_attention"])
        self.assertTrue(parked["unhandled_response"])
        self.start("next")
        self.finish()
        result = self.thread()
        self.assertEqual(result["attention"], "later")
        self.assertTrue(result["needs_attention"])
        dismissed = self.sessions.update_work_thread("terminal", {"handled": True,
            "handled_checkpoint_id": result["checkpoint"]["id"],
            "handled_completion_id": result["turn_completion"]["id"]})
        self.assertFalse(dismissed["needs_attention"])
        self.assertEqual(dismissed["attention"], "later")

    def test_timed_later_keeps_new_result_quiet_and_redeferral_hides_current_result(self):
        self.start()
        self.finish()
        self.sessions.update_work_thread("terminal", {"snoozed_until": self.now + 100})
        self.start("background")
        self.finish()
        self.assertFalse(self.thread()["needs_attention"])
        self.assertTrue(self.thread()["snoozed"])
        self.sessions.update_work_thread("terminal", {"attention": "later"})
        self.assertFalse(self.thread()["needs_attention"])
        self.assertFalse(self.thread()["snoozed"])

    def test_new_question_in_later_surfaces_without_changing_placement(self):
        self.checkpoint("planning", "user")
        self.sessions.update_work_thread("terminal", {"attention": "later"})
        self.now += 1
        self.checkpoint("validation", "user")
        self.assertTrue(self.thread()["needs_attention"])
        self.assertEqual(self.thread()["attention"], "later")

    def test_unread_finished_result_can_be_dismissed_without_opening(self):
        self.start()
        self.checkpoint("implementation", "agent")
        self.checkpoint("finished", "nobody")
        self.finish()
        result = self.thread()
        self.assertEqual(result["response_state"], "update")
        self.assertTrue(result["can_handle_response"])
        dismissed = self.sessions.update_work_thread("terminal", {"handled": True,
            "handled_checkpoint_id": result["checkpoint"]["id"],
            "handled_completion_id": result["turn_completion"]["id"]})
        self.assertFalse(dismissed["needs_attention"])
        self.assertEqual(dismissed["attention"], "now")

    def test_browser_approval_failure_and_interrupt_are_not_task_completion(self):
        thread_id = self.sessions.create({"name": "Browser task"})["session"]["thread_id"]
        self.sessions.send(thread_id, {"message": "Start."})
        self.sessions._event({"id": 42, "method": "item/commandExecution/requestApproval", "params": {"threadId": thread_id}})
        self.assertEqual(self.sessions.work_thread(thread_id)["response_state"], "input")
        turn = self.sessions.rpc.threads[thread_id]["turns"][-1]
        turn["status"] = "failed"
        self.sessions._event({"method": "turn/completed", "params": {"threadId": thread_id, "turn": turn}})
        self.assertEqual(self.sessions.work_thread(thread_id)["response_state"], "failed")
        self.assertTrue(self.sessions.work_thread(thread_id)["needs_attention"])
        self.sessions.send(thread_id, {"message": "Try again."})
        self.sessions.rpc.request("turn/interrupt", {"threadId": thread_id})
        stopped = self.sessions.work_thread(thread_id)
        self.assertEqual(stopped["response_state"], "interrupted")
        self.assertFalse(stopped["needs_attention"])
        self.assertFalse(stopped["unread_result"])

    def test_terminal_hooks_produce_reply_and_clear_it_on_next_user_prompt(self):
        with patch.dict("os.environ", {"AGENT_COORD_CLIENT": "codex", "AGENT_COORD_ZELLIJ_WAKE": "0", "AGENT_COORD_DELEGATION_ID": ""}):
            payload = {"session_id": "terminal", "cwd": str(self.root), "turn_id": "hook-one"}
            handle({**payload, "hook_event_name": "UserPromptSubmit", "prompt": "Investigate."}, self.store)
            self.checkpoint()
            handle({**payload, "hook_event_name": "Stop"}, self.store)
            self.assertEqual(self.thread()["response_state"], "reply")
            handle({**payload, "hook_event_name": "UserPromptSubmit", "turn_id": "hook-two", "prompt": "Continue."}, self.store)
            self.assertEqual(self.thread()["response_state"], "working")

    def test_completion_activity_is_recent_and_duplicate_stop_is_idempotent(self):
        self.start()
        self.finish()
        completed_at = self.thread()["updated_at"]
        self.sessions.update_work_thread("terminal", {"seen": True})
        self.finish()
        self.assertEqual(self.thread()["updated_at"], completed_at)
        self.assertFalse(self.thread()["unread_result"])
        self.assertEqual(self.store.threads.completion_cursor(), 1)

    def test_invalid_read_receipts_cannot_partially_rename_or_move_thread(self):
        self.start()
        self.checkpoint()
        self.finish()
        before = self.thread()
        for value in (-1, True, "1", 10000, []):
            with self.subTest(value=value), self.assertRaises(CoordinationError):
                self.sessions.update_work_thread("terminal", {
                    "seen": True, "seen_completion_id": value, "title": "Wrong", "attention": "later",
                })
            self.assertEqual(self.thread(), before)
        with self.assertRaises(CoordinationError):
            self.sessions.update_work_thread("terminal", {"seen_completion_id": 0})

    def test_migration_preserves_known_read_results_without_suppressing_future_replies(self):
        self.start()
        self.checkpoint("finished", "nobody")
        self.finish()
        self.sessions.update_work_thread("terminal", {"seen": True})
        with self.store._connection() as db:
            db.execute("ALTER TABLE work_threads DROP COLUMN seen_completion_id")
        reopened = CoordinationStore(self.store.database_path, clock=lambda: self.now)
        self.assertFalse(reopened.threads.get("terminal")["unread_result"])
        self.now += 1
        reopened.threads.start_turn("terminal", turn_id="two")
        reopened.threads.finish_turn("terminal")
        self.assertTrue(reopened.threads.get("terminal")["unread_result"])

    @unittest.skipUnless(shutil.which("node"), "Node is needed only for frontend integration")
    def test_dismissed_review_projection_stays_idle_until_a_changed_request(self):
        self.start()
        self.checkpoint("validation", "user")
        self.finish()
        requested = self.thread()
        dismissed = self.sessions.update_work_thread("terminal", {
            "handled": True, "handled_checkpoint_id": requested["checkpoint"]["id"],
            "handled_completion_id": requested["turn_completion"]["id"],
        })
        self.assertEqual(dismissed["checkpoint"], requested["checkpoint"])
        self.now += 1
        self.checkpoint("validation", "user")
        refreshed = self.thread()
        reopened = BrowserSessions(CoordinationStore(self.store.database_path, clock=lambda: self.now),
                                   str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(reopened.close)
        restored = reopened.work_thread("terminal")
        self.store.threads.checkpoint("terminal", {
            "phase": "validation", "summary": "A different review is ready.",
            "next_actor": "user", "next_action": "Check the new Phone board view.",
        })
        changed = self.thread()
        script = """const g = require('./plugins/agent-coord/scripts/agent_coord/web/thread-groups.js');
const w = require('./plugins/agent-coord/scripts/agent_coord/web/working-set.js');
const threads = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
console.log(JSON.stringify(threads.map(t => ({attention:g.awaitsUser(t), level:w.levelOf(t), label:g.status(t).label}))));"""
        result = subprocess.run([shutil.which("node"), "-e", script],
                                input=json.dumps([requested, dismissed, refreshed, restored, changed]),
                                cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True,
                                timeout=10, check=True)
        pending = {"attention": True, "level": 0, "label": "Review requested"}
        idle = {"attention": False, "level": 2, "label": "Validating"}
        self.assertEqual(json.loads(result.stdout), [pending, idle, idle, idle, pending])

    @unittest.skipUnless(shutil.which("node"), "Node is needed only for frontend integration")
    def test_api_finished_and_discussion_responses_both_need_attention_until_handled(self):
        self.start()
        self.checkpoint()
        self.finish()
        reply = self.sessions.update_work_thread("terminal", {"seen": True})
        self.start("two")
        self.checkpoint("finished", "nobody")
        self.finish()
        completed = self.thread()
        handled = self.sessions.update_work_thread("terminal", {
            "handled": True, "handled_checkpoint_id": completed["checkpoint"]["id"],
            "handled_completion_id": completed["turn_completion"]["id"],
        })
        completed["thread_id"] = "completed"
        handled["thread_id"] = "handled"
        script = """const g = require('./plugins/agent-coord/scripts/agent_coord/web/thread-groups.js');
const fs = require('node:fs'); const groups = g.groupThreads(JSON.parse(fs.readFileSync(0, 'utf8')));
console.log(JSON.stringify({reply: groups.priority.map(t=>t.thread_id), stages: groups.phases.flatMap(g=>g.threads).map(t=>t.thread_id)}));"""
        result = subprocess.run([shutil.which("node"), "-e", script], input=json.dumps([reply, completed, handled]),
                                cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True, timeout=10, check=True)
        groups = json.loads(result.stdout)
        self.assertCountEqual(groups["reply"], ["terminal", "completed"])
        self.assertEqual(groups["stages"], ["handled"])
