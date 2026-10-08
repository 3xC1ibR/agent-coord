from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))

from agent_coord.codex_app_server import BrowserSessions
from agent_coord.store import CoordinationStore
from test_codex_app_server import FakeCodex


class RollUpAttentionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.now = 1000.0
        self.store = CoordinationStore(self.root / "state.sqlite3", clock=lambda: self.now)
        self.sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(self.sessions.close)
        for thread in ("older-session", "newer-session"):
            self.store.register(session_id=thread, client="codex", cwd=str(self.root))
            self.now += 10

    def result(self, thread):
        self.store.threads.start_turn(thread, prompt="Investigate", turn_id=str(self.now))
        self.store.threads.finish_turn(thread)
        self.store.touch(thread, turn_active=False)
        return self.sessions.work_thread(thread)

    def test_waiting_age_comes_from_response_not_session_or_metadata(self):
        self.now = 2000
        first = self.result("newer-session")
        self.now = 3000
        second = self.result("older-session")
        self.assertLess(first["attention_since"], second["attention_since"])
        self.assertEqual(first["attention_since"], 2000)
        self.now = 4000
        self.sessions.update_work_thread("newer-session", {"title": "Changed", "seen": True, "pinned": True})
        current = self.sessions.work_thread("newer-session")
        self.assertEqual(current["attention_since"], first["attention_since"])
        self.assertEqual(current["attention_key"], first["attention_key"])
        self.assertTrue(current["needs_attention"])

    def test_approval_age_is_receipt_time_and_remaining_requests_keep_attention(self):
        self.now = 2000
        self.sessions._event({"id": 1, "method": "item/commandExecution/requestApproval", "params": {"threadId": "older-session"}})
        first = self.sessions.work_thread("older-session")
        self.assertEqual(first["attention_since"], 2000)
        self.now = 3000
        self.sessions._event({"id": 2, "method": "item/tool/requestUserInput", "params": {"threadId": "older-session"}})
        self.assertEqual(self.sessions.work_thread("older-session")["attention_since"], 2000)
        self.sessions._event({"method": "serverRequest/resolved", "params": {"requestId": 1}})
        remaining = self.sessions.work_thread("older-session")
        self.assertEqual(remaining["attention_since"], 3000)
        self.assertTrue(remaining["needs_attention"])
        self.assertNotEqual(first["attention_key"], remaining["attention_key"])

    def test_explicit_checkpoint_uses_original_creation_time(self):
        body = {"phase": "discussion", "summary": "Choose an option", "next_actor": "user", "next_action": "Choose A or B"}
        self.now = 2000
        self.store.threads.checkpoint("older-session", body)
        first = self.sessions.work_thread("older-session")
        self.now = 3000
        self.store.threads.checkpoint("older-session", body)
        current = self.sessions.work_thread("older-session")
        self.assertEqual(first["attention_since"], 2000)
        self.assertEqual(current["attention_since"], 2000)
        self.assertEqual(current["attention_key"], first["attention_key"])

    def test_new_response_has_new_identity_and_age(self):
        self.now = 2000
        first = self.result("older-session")
        self.now = 3000
        second = self.result("older-session")
        self.assertNotEqual(first["attention_key"], second["attention_key"])
        self.assertEqual(second["attention_since"], 3000)

    def test_handled_and_parked_threads_have_no_queue_timestamp(self):
        self.now = 2000
        first = self.result("older-session")
        handled = self.sessions.update_work_thread("older-session", {"handled": True,
            "handled_checkpoint_id": 0, "handled_completion_id": first["turn_completion"]["id"]})
        self.assertIsNone(handled["attention_since"])
        self.assertIsNone(handled["attention_key"])
        self.result("newer-session")
        parked = self.sessions.update_work_thread("newer-session", {"attention": "later"})
        self.assertIsNone(parked["attention_since"])


if __name__ == "__main__":
    unittest.main()
