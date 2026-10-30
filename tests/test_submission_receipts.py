from __future__ import annotations

import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))
from agent_coord.store import CoordinationError, CoordinationStore
from agent_coord.submission_receipts import SubmissionReceipts
from agent_coord.codex_app_server import BrowserNotSubmittedError


class SubmissionReceiptTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database = Path(temporary.name) / "state.sqlite3"
        self.store = CoordinationStore(self.database)
        with self.store._connection() as db:
            db.execute("CREATE TABLE browser_sessions(thread_id TEXT PRIMARY KEY)")
            db.execute("INSERT INTO browser_sessions VALUES ('conversation')")
        self.receipts = SubmissionReceipts(self.store)
        self.body = {"requestId": "request-1", "message": "Run the check"}

    def run_request(self, callback, body=None):
        return self.receipts.run("conversation", body or self.body, callback)

    def test_completed_retry_returns_saved_result_without_repeating_effect(self):
        calls = []
        callback = lambda: calls.append(True) or {"turnId": "turn-1"}
        self.assertEqual(self.run_request(callback), {"turnId": "turn-1"})
        changed_context = {**self.body, "windowId": "new-window", "activity": {"id": "new-event"}, "expectedTurnId": "turn-1"}
        self.assertEqual(self.run_request(callback, changed_context), {"turnId": "turn-1"})
        self.assertEqual(calls, [True])

    def test_restart_after_acceptance_never_replays_unknown_effect(self):
        class Terminated(BaseException):
            pass
        with self.assertRaises(Terminated):
            self.run_request(lambda: (_ for _ in ()).throw(Terminated()))
        self.receipts = SubmissionReceipts(CoordinationStore(self.database))
        with self.assertRaisesRegex(CoordinationError, "unconfirmed outcome"), patch.object(self, "run_request", wraps=self.run_request):
            self.run_request(lambda: self.fail("Submission was replayed"))

    def test_concurrent_retry_cannot_repeat_effect_or_wait_for_provider(self):
        entered, release = threading.Event(), threading.Event()
        results = []
        def callback():
            entered.set()
            release.wait(2)
            return {"turnId": "turn-1"}
        worker = threading.Thread(target=lambda: results.append(self.run_request(callback)))
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            with self.assertRaisesRegex(CoordinationError, "unconfirmed outcome"):
                self.run_request(lambda: self.fail("Duplicate external effect"))
        finally:
            release.set()
            worker.join(3)
        self.assertEqual(results, [{"turnId": "turn-1"}])

    def test_provider_callback_runs_after_transaction_commit(self):
        def callback():
            db = sqlite3.connect(self.database, timeout=0)
            try:
                db.execute("BEGIN IMMEDIATE")
                self.assertEqual(db.execute("SELECT state FROM browser_submission_receipts").fetchone()[0], "accepted")
            finally:
                db.rollback()
                db.close()
            return {}
        self.run_request(callback)

    def test_proven_pre_submission_rejection_allows_retry(self):
        with self.assertRaises(BrowserNotSubmittedError):
            self.run_request(lambda: (_ for _ in ()).throw(BrowserNotSubmittedError("Not sent")))
        self.assertEqual(self.run_request(lambda: {"turnId": "retry"}), {"turnId": "retry"})

    def test_unknown_provider_error_remains_uncertain(self):
        with self.assertRaisesRegex(RuntimeError, "connection lost"):
            self.run_request(lambda: (_ for _ in ()).throw(RuntimeError("connection lost")))
        with self.assertRaisesRegex(CoordinationError, "unconfirmed outcome"):
            self.run_request(lambda: self.fail("Uncertain effect was replayed"))

    def test_request_key_cannot_change_input_or_queue_mode(self):
        self.run_request(lambda: {})
        with self.assertRaisesRegex(CoordinationError, "different input"):
            self.run_request(lambda: self.fail("Changed input replayed"), {**self.body, "message": "Different"})
        with self.assertRaisesRegex(CoordinationError, "different input"):
            self.receipts.run("conversation", self.body, lambda: {}, queued=True)

    def test_supplied_invalid_keys_are_rejected(self):
        for key in ("", False, 0, [], "x" * 201):
            with self.subTest(key=key), self.assertRaisesRegex(CoordinationError, "request ID"):
                self.run_request(lambda: self.fail("Invalid key submitted"), {**self.body, "requestId": key})

    def test_receipt_write_failure_does_not_allow_replay(self):
        def callback():
            with self.store._connection() as db:
                db.execute("CREATE TRIGGER reject_receipt BEFORE UPDATE ON browser_submission_receipts BEGIN SELECT RAISE(FAIL, 'disk unavailable'); END")
            return {"turnId": "accepted"}
        with self.assertRaisesRegex(CoordinationError, "may have been received"):
            self.run_request(callback)
        with self.assertRaisesRegex(CoordinationError, "unconfirmed outcome"):
            self.run_request(lambda: self.fail("Effect replayed after receipt failure"))
