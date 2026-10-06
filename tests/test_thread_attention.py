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
        self.assertEqual(result["response_state"], "completed")
        self.assertFalse(result["needs_attention"])
        self.assertTrue(result["unread_result"])
        read = self.sessions.update_work_thread("terminal", {
            "seen": True, "seen_checkpoint_id": checkpoint["id"],
            "seen_completion_id": result["turn_completion"]["id"],
        })
        self.assertFalse(read["unread"])
        self.assertEqual(read["response_state"], "completed")
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
        self.assertEqual(self.thread()["response_state"], "reply")
        self.sessions.update_work_thread("terminal", {"seen": True, "attention": "later"})
        self.assertTrue(self.thread()["needs_attention"])
        self.assertEqual(self.thread()["attention"], "later")
        self.sessions.update_work_thread("terminal", {"attention": "archived"})
        self.assertFalse(self.thread()["needs_attention"])

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
    def test_api_reply_and_completed_task_land_in_separate_ui_groups(self):
        self.start()
        self.checkpoint()
        self.finish()
        reply = self.sessions.update_work_thread("terminal", {"seen": True})
        self.start("two")
        self.checkpoint("finished", "nobody")
        self.finish()
        completed = self.thread()
        completed["thread_id"] = "completed"
        script = """const g = require('./plugins/agent-coord/scripts/agent_coord/web/thread-groups.js');
const fs = require('node:fs'); const groups = g.groupThreads(JSON.parse(fs.readFileSync(0, 'utf8')));
console.log(JSON.stringify({reply: groups.priority.map(t=>t.thread_id), completed: groups.completed.map(t=>t.thread_id)}));"""
        result = subprocess.run([shutil.which("node"), "-e", script], input=json.dumps([reply, completed]),
                                cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True, timeout=10, check=True)
        self.assertEqual(json.loads(result.stdout), {"reply": ["terminal"], "completed": ["completed"]})
