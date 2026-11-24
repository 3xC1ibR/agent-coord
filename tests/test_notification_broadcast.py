from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))
from agent_coord.notification_broadcast import NotificationBroadcast
from agent_coord.store import CoordinationStore


class NotificationBroadcastTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = CoordinationStore(Path(self.temp.name) / "state.db")
        self.sessions = Mock(cwd=self.temp.name, events_changed=threading.Condition())
        self.sessions.completions_after.side_effect = lambda cursor: {"seq": cursor, "events": [cursor]}
        self.sessions.approval_notifications.return_value = []
        self.sessions.snooze_notifications.return_value = []
        self.broadcast = NotificationBroadcast(self.store, self.sessions)
        self.broadcast.RECONCILE_SECONDS = 30
        self.addCleanup(self.broadcast.close)

    def wait_result(self, client):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = self.broadcast.take(client)
            if result is not None:
                return result
            time.sleep(.005)
        self.fail("SSE coordination reader did not deliver the subscriber's batch")

    def test_committed_changes_wake_all_windows_and_a_rollback_does_not(self):
        with self.store._connection() as db:
            db.execute("CREATE TABLE probe (id INTEGER)")
        with ExitStack() as stack:
            streams = [stack.enter_context(self.broadcast.subscribe(n)) for n in (7, 7, 42)]
            for stream in streams:
                self.wait_result(stream)
            starts = self.sessions.completions_after.call_count
            observed = self.store._writer.commit_generation
            with self.assertRaises(ValueError), self.store._connection() as db:
                db.execute("INSERT INTO probe VALUES (1)")
                raise ValueError("rollback")
            self.assertEqual(self.store._writer.commit_generation, observed)
            with self.store._connection() as db:
                db.execute("INSERT INTO probe VALUES (2)")
            self.assertEqual([self.wait_result(c)["completions"] for c in streams], [[7], [7], [42]])
            self.assertEqual(self.sessions.completions_after.call_count - starts, 2)

    def test_slow_notification_reads_leave_provider_stream_and_other_consumers_free(self):
        entered, resume = threading.Event(), threading.Event()
        def slow(cursor):
            entered.set()
            resume.wait(3)
            return {"seq": cursor, "events": []}
        self.sessions.completions_after.side_effect = slow
        try:
            with self.broadcast.subscribe(5) as stream:
                self.assertTrue(entered.wait(2))
                self.assertIsNone(self.broadcast.take(stream))
                with self.broadcast.subscribe(8) as other:
                    self.assertIsNone(self.broadcast.take(other))
                    resume.set()
                    self.assertEqual(self.wait_result(other)["completionSeq"], 8)
                self.assertEqual(self.wait_result(stream)["completionSeq"], 5)
        finally:
            resume.set()
