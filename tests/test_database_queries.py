from __future__ import annotations

import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))

from agent_coord.store import CoordinationError, CoordinationStore
from agent_coord.thread_control import ThreadControl
from agent_coord.thread_inventory import listed_threads


class DatabaseQueryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / ".git").mkdir()
        self.store = CoordinationStore(self.root / "state.sqlite3", clock=lambda: 1000)
        self.store.register(session_id="conversation", client="codex", cwd=str(self.root))
        self.control = ThreadControl(self.store)

    def read_only_connections(self):
        return self.store.reading()

    def test_status_and_existing_thread_ensure_are_read_only(self):
        # The same read-only connection rejects an explicit mutation, proving
        # the fixture enforces the boundary rather than merely tracing SQL.
        with self.read_only_connections():
            self.assertEqual(ThreadControl(self.store).status("conversation")["status"], "none")
            self.assertEqual(self.store.threads.ensure("conversation")["thread_id"], "conversation")
            with self.assertRaises(sqlite3.OperationalError):
                self.control.request_close("conversation")

    def test_reads_complete_while_another_connection_holds_the_writer(self):
        with sqlite3.connect(self.store.database_path) as writer:
            writer.execute("BEGIN IMMEDIATE")
            try:
                start = time.monotonic()
                self.assertEqual(ThreadControl(self.store).status("conversation")["status"], "none")
                self.assertEqual(self.store.threads.ensure("conversation")["thread_id"], "conversation")
                self.assertLess(time.monotonic() - start, 1.0)
            finally:
                writer.rollback()

    def test_stale_close_is_projected_without_mutating_recovery_state(self):
        request = self.control.request_close("conversation")
        self.assertTrue(self.control.claim(request["request_id"], "expired-runtime"))
        with self.store._connection() as connection:
            connection.execute("UPDATE thread_close_requests SET updated_at = 0")
        with self.read_only_connections():
            status = ThreadControl(self.store).status("conversation")
            self.assertEqual(status["status"], "failed")
            self.assertIn("runtime stopped", status["error"])
            with self.store._connection() as connection:
                self.assertEqual(connection.execute("SELECT status FROM thread_close_requests").fetchone()[0], "closing")
        # Recovery is persisted by the lifecycle worker's queue pass.
        self.control.queued()
        with self.store._connection() as connection:
            self.assertEqual(connection.execute("SELECT status FROM thread_close_requests").fetchone()[0], "failed")

    def test_new_turn_cancellation_is_projected_then_reconciled_by_worker(self):
        self.control.request_close("conversation", after_turn=True)
        with self.store._connection() as connection:
            connection.execute("UPDATE work_threads SET turn_key = 'new-turn' WHERE thread_id = 'conversation'")
        with self.read_only_connections():
            self.assertEqual(self.control.status("conversation")["status"], "cancelled")
            with self.store._connection() as connection:
                self.assertEqual(connection.execute("SELECT status FROM thread_close_requests").fetchone()[0], "queued")
        self.assertEqual(self.control.queued(), [])
        with self.store._connection() as connection:
            self.assertEqual(connection.execute("SELECT status FROM thread_close_requests").fetchone()[0], "cancelled")

    def test_due_snooze_and_missing_association_do_not_make_detail_or_inventory_write(self):
        self.store.threads.update("conversation", snoozed_until=1100)
        self.store.clock = lambda: 1200
        with self.store._connection() as connection:
            connection.execute("DELETE FROM thread_organization WHERE thread_id = 'conversation'")
        with sqlite3.connect(self.store.database_path) as writer:
            writer.execute("BEGIN IMMEDIATE")
            try:
                with self.read_only_connections():
                    start = time.monotonic()
                    detail = self.store.threads.get("conversation")
                    inventory = self.store.threads.list()
                    with self.store._connection() as connection:
                        listed = listed_threads(self.store.threads, connection)
                        self.assertEqual(connection.execute("SELECT attention FROM work_threads").fetchone()[0], "later")
                        self.assertEqual(connection.execute("SELECT count(*) FROM thread_organization").fetchone()[0], 0)
                    self.assertLess(time.monotonic() - start, 1.0)
                self.assertEqual(detail["attention"], "now")
                self.assertTrue(detail["snooze_due"])
                self.assertEqual(detail["repository_root"], str(self.root))
                self.assertEqual(inventory[0]["repository_id"], detail["repository_id"])
                self.assertEqual(listed[0]["repository_id"], detail["repository_id"])
                self.assertEqual(listed[0]["attention"], "now")
            finally:
                writer.rollback()

    def test_existing_bead_link_does_not_require_repeated_writes(self):
        with self.store._connection() as connection:
            connection.execute("UPDATE sessions SET bead_id = 'test-issue' WHERE session_id = 'conversation'")
        self.store.threads.ensure("conversation")
        with self.read_only_connections():
            thread = self.store.threads.ensure("conversation")
        self.assertEqual(thread["links"][0]["target"], "test-issue")

    def test_deferred_start_cannot_replace_a_newer_turn_or_cancel_its_close(self):
        self.store.threads.start_turn("conversation", turn_id="new-turn", prompt="Current request", occurred_at=1100)
        request = self.control.request_close("conversation", after_turn=True)
        self.store.threads.start_turn("conversation", turn_id="old-turn", prompt="Previous request", occurred_at=1050,
                                     notification_context={"origin": "coordination", "owner_thread_id": None})
        thread = self.store.threads.get("conversation")
        self.assertEqual(thread["turn_id"], "new-turn")
        self.assertEqual(thread["turn_started_at"], 1100)
        self.assertEqual(thread["original_request"], "Current request")
        self.assertEqual(self.control.status("conversation")["request_id"], request["request_id"])
        self.assertEqual(self.control.status("conversation")["status"], "queued")
        with self.store._connection() as connection:
            self.assertIsNone(connection.execute("SELECT 1 FROM turn_notification_context WHERE turn_key = 'old-turn'").fetchone())

    def test_read_only_status_handles_a_database_that_never_used_close_requests(self):
        store = CoordinationStore(self.root / "unused-lifecycle.sqlite3")
        store.register(session_id="unused", client="codex", cwd=str(self.root))
        with store.reading():
            self.assertEqual(ThreadControl(store).status("unused")["status"], "none")
            with store._connection() as connection:
                self.assertIsNone(connection.execute("SELECT 1 FROM sqlite_master WHERE name = 'thread_close_requests'").fetchone())

    def test_after_turn_close_does_not_enqueue_before_its_conversation_is_persisted(self):
        observed = []

        def pending(identity):
            observed.append(identity)
            return False

        self.store.conversation_barrier = pending
        with sqlite3.connect(self.store.database_path) as writer:
            writer.execute("BEGIN IMMEDIATE")
            try:
                start = time.monotonic()
                with self.assertRaisesRegex(CoordinationError, "still being saved"):
                    self.control.request_close("conversation", after_turn=True)
                self.assertLess(time.monotonic() - start, 1.0)
            finally:
                writer.rollback()
        self.assertEqual(observed, ["conversation"])
        self.assertEqual(self.control.status("conversation")["status"], "none")
        self.store.conversation_barrier = lambda identity: True
        self.assertEqual(self.control.request_close("conversation", after_turn=True)["status"], "queued")


if __name__ == "__main__":
    unittest.main()
