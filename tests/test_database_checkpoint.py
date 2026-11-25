from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))

from agent_coord.database_checkpoint import (
    AUTOMATIC_CHECKPOINT_PAGES, DatabaseCheckpoint, automatic_checkpoint_pages,
    supports_concurrent_checkpoint,
)
from agent_coord.database_writer import writer_for
from agent_coord.store import CoordinationStore
from agent_coord import desktop_runtime


class DatabaseCheckpointTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name).resolve() / "state.sqlite3"
        self.writer = sqlite3.connect(self.path, isolation_level=None, timeout=.1)
        self.addCleanup(self.writer.close)
        self.writer.execute("PRAGMA journal_mode=WAL")
        self.writer.execute("CREATE TABLE records (value INTEGER)")
        self.writer.execute("INSERT INTO records VALUES (1)")

    def worker(self, **options):
        worker = DatabaseCheckpoint(self.path, **options)
        self.addCleanup(worker.close)
        return worker

    def require_safe_sqlite(self):
        if not supports_concurrent_checkpoint():
            self.skipTest("Concurrent WAL maintenance requires the SQLite WAL-reset repair")

    def test_search_backfill_starts_once_only_for_writable_runtime_owner(self):
        from agent_coord.codex_app_server import BrowserSessions
        from test_browser_providers import FakeCodex
        store = CoordinationStore(self.path)
        sessions = BrowserSessions(store, str(self.path.parent), rpc_factory=FakeCodex)
        self.addCleanup(sessions.close)
        identity = str(self.path)
        self.addCleanup(desktop_runtime._OWNED_DATABASES.discard, identity)
        backfill = Mock()
        with patch.object(store.search_store, "_ensure_initialized", backfill), \
                patch.object(sessions.persistence, "submit") as submit:
            self.assertFalse(sessions.start_search_index())
            desktop_runtime._OWNED_DATABASES.add(identity)
            with patch.object(store, "read_only", True):
                self.assertFalse(sessions.start_search_index())
            with patch.object(sessions, "closed", True):
                self.assertFalse(sessions.start_search_index())
            submit.assert_not_called()
            self.assertTrue(sessions.start_search_index())
            self.assertFalse(sessions.start_search_index())
            submit.assert_called_once()
            self.assertEqual(submit.call_args.kwargs, {"key": ("search_index",), "required": False})
            submit.call_args.args[0]()
            backfill.assert_called_once_with()

    def wait_for(self, predicate):
        deadline = time.monotonic() + 2
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail("Checkpoint worker did not reach the expected state")
            threading.Event().wait(.005)

    def test_only_repaired_sqlite_versions_enable_concurrent_maintenance(self):
        for version in ((3, 44, 6), (3, 44, 7), (3, 50, 7), (3, 50, 8), (3, 51, 3), (3, 53, 4)):
            with self.subTest(version=version):
                self.assertTrue(supports_concurrent_checkpoint(version))
        for version in ((3, 44, 5), (3, 45, 9), (3, 49, 99), (3, 50, 6), (3, 51, 0), (3, 51, 2)):
            with self.subTest(version=version):
                self.assertFalse(supports_concurrent_checkpoint(version))

    def test_unsupported_sqlite_leaves_automatic_checkpoints_and_connections_unchanged(self):
        worker = self.worker()
        before = self.writer.execute("PRAGMA wal_autocheckpoint").fetchone()[0]
        with patch("agent_coord.database_checkpoint.sqlite3.sqlite_version_info", (3, 51, 0)), \
                patch.object(worker, "_connect", side_effect=AssertionError("Worker must not open a connection")):
            self.assertFalse(worker.start())
        self.assertIsNone(worker._thread)
        self.assertEqual(worker.snapshot()["state"], "unsupported_sqlite")
        self.assertEqual(automatic_checkpoint_pages(self.path), AUTOMATIC_CHECKPOINT_PAGES)
        self.assertEqual(self.writer.execute("PRAGMA wal_autocheckpoint").fetchone()[0], before)

    def test_writer_autocheckpoint_changes_only_after_worker_configuration(self):
        self.require_safe_sqlite()
        worker = self.worker()
        entered, release = threading.Event(), threading.Event()
        connect = worker._connect
        started = []

        def delayed_connect():
            entered.set()
            release.wait(2)
            return connect()

        with patch.object(worker, "_connect", side_effect=delayed_connect):
            starter = threading.Thread(target=lambda: started.append(worker.start()))
            starter.start()
            try:
                self.assertTrue(entered.wait(1))
                self.assertEqual(automatic_checkpoint_pages(self.path), AUTOMATIC_CHECKPOINT_PAGES)
            finally:
                release.set()
                starter.join(2)
        self.assertFalse(starter.is_alive())
        self.assertEqual(started, [True])
        self.assertEqual(automatic_checkpoint_pages(self.path), 0)
        self.writer.execute(f"PRAGMA wal_autocheckpoint={automatic_checkpoint_pages(self.path)}")
        self.assertEqual(self.writer.execute("PRAGMA synchronous").fetchone()[0], 2)
        self.assertEqual(self.writer.execute("PRAGMA wal_autocheckpoint").fetchone()[0], 0)
        worker.close()
        self.writer.execute(f"PRAGMA wal_autocheckpoint={automatic_checkpoint_pages(self.path)}")
        self.assertEqual(self.writer.execute("PRAGMA wal_autocheckpoint").fetchone()[0], AUTOMATIC_CHECKPOINT_PAGES)
        self.assertFalse(worker._thread.is_alive())

    def test_only_one_worker_owns_a_canonical_database(self):
        self.require_safe_sqlite()
        first = self.worker()
        self.assertTrue(first.start())
        self.assertTrue(first.start())
        alias = self.path.parent / "database-link.sqlite3"
        alias.symlink_to(self.path)
        second = DatabaseCheckpoint(alias)
        self.addCleanup(second.close)
        self.assertFalse(second.start())
        self.assertEqual(second.snapshot()["state"], "already_owned")
        second.close()
        self.assertEqual(automatic_checkpoint_pages(self.path), 0)
        first.close()
        first.close()
        self.assertEqual(automatic_checkpoint_pages(self.path), AUTOMATIC_CHECKPOINT_PAGES)

    def test_store_reused_writer_restores_automatic_checkpoints_without_changing_readers_or_full_durability(self):
        self.require_safe_sqlite()
        store = CoordinationStore(self.path)
        self.addCleanup(store._idle_connections.holder.discard)
        identity = str(self.path)
        desktop_runtime._OWNED_DATABASES.add(identity)
        self.addCleanup(desktop_runtime._OWNED_DATABASES.discard, identity)
        with store._connection() as writer:
            self.assertEqual(writer.execute("PRAGMA wal_autocheckpoint").fetchone()[0], 1000)
            self.assertEqual(writer.execute("PRAGMA synchronous").fetchone()[0], 1)
        with store.reading(), store._connection() as reader:
            self.assertEqual(reader.execute("PRAGMA wal_autocheckpoint").fetchone()[0], 1000)
        self.addCleanup(store._idle_connections.reader.discard)
        worker = self.worker()
        self.assertTrue(worker.start())
        with store._connection() as reused:
            self.assertIs(reused, writer)
            self.assertEqual(reused.execute("PRAGMA wal_autocheckpoint").fetchone()[0], 0)
            self.assertEqual(reused.execute("PRAGMA synchronous").fetchone()[0], 1)
            reused.execute("INSERT INTO records VALUES (2)")
        with store.reading(), store._connection() as reused_reader:
            self.assertIs(reused_reader, reader)
            self.assertEqual(reused_reader.execute("PRAGMA wal_autocheckpoint").fetchone()[0], 1000)
            self.assertEqual(reused_reader.execute("SELECT count(*) FROM records").fetchone()[0], 2)
        readonly = CoordinationStore(self.path, read_only=True)
        with readonly._connection() as readonly_connection:
            self.assertEqual(readonly_connection.execute("PRAGMA wal_autocheckpoint").fetchone()[0], 1000)
            self.assertEqual(readonly_connection.execute("PRAGMA synchronous").fetchone()[0], 2)
            self.assertEqual(readonly_connection.execute("SELECT count(*) FROM records").fetchone()[0], 2)
        self.addCleanup(readonly._idle_connections.reader.discard)
        worker.close()
        with store._connection() as reused:
            self.assertIs(reused, writer)
            self.assertEqual(reused.execute("PRAGMA wal_autocheckpoint").fetchone()[0], 1000)
            self.assertEqual(reused.execute("PRAGMA synchronous").fetchone()[0], 1)
        with store.reading(), store._connection() as reused_reader:
            self.assertIs(reused_reader, reader)
            self.assertEqual(reused_reader.execute("PRAGMA wal_autocheckpoint").fetchone()[0], 1000)

    def test_failed_startup_does_not_create_database_or_disable_autocheckpoint(self):
        self.require_safe_sqlite()
        missing = self.path.parent / "missing.sqlite3"
        worker = DatabaseCheckpoint(missing)
        self.addCleanup(worker.close)
        self.assertFalse(worker.start())
        self.assertFalse(missing.exists())
        self.assertFalse(worker._thread.is_alive())
        self.assertEqual(worker.snapshot()["state"], "failed")
        self.assertEqual(automatic_checkpoint_pages(missing), AUTOMATIC_CHECKPOINT_PAGES)

    def test_ui_startup_failure_closes_checkpoint_owner_and_worker(self):
        self.require_safe_sqlite()
        from test_codex_app_server import BrowserSessions, FakeCodex
        from agent_coord.ui import make_ui_server

        store = CoordinationStore(self.path)
        self.addCleanup(store._idle_connections.holder.discard)
        identity = str(self.path)
        desktop_runtime._OWNED_DATABASES.add(identity)
        self.addCleanup(desktop_runtime._OWNED_DATABASES.discard, identity)
        sessions = BrowserSessions(store, str(self.path.parent), rpc_factory=FakeCodex)
        self.addCleanup(sessions.close)
        with patch("agent_coord.ui.WebPush.start", side_effect=RuntimeError("Producer startup failed")):
            with self.assertRaisesRegex(RuntimeError, "Producer startup failed"):
                make_ui_server(store, host="127.0.0.1", port=0, browser_sessions=sessions)
        self.assertTrue(sessions.closed)
        self.assertIsNotNone(sessions.database_checkpoint)
        self.assertFalse(sessions.database_checkpoint._thread.is_alive())
        self.assertEqual(sessions.database_checkpoint.snapshot()["state"], "stopped")
        self.assertEqual(automatic_checkpoint_pages(self.path), AUTOMATIC_CHECKPOINT_PAGES)

    def test_component_close_failure_still_joins_checkpoint_worker(self):
        self.require_safe_sqlite()
        from test_codex_app_server import BrowserSessions, FakeCodex

        store = CoordinationStore(self.path)
        self.addCleanup(store._idle_connections.holder.discard)
        identity = str(self.path)
        desktop_runtime._OWNED_DATABASES.add(identity)
        self.addCleanup(desktop_runtime._OWNED_DATABASES.discard, identity)
        sessions = BrowserSessions(store, str(self.path.parent), rpc_factory=FakeCodex)
        self.addCleanup(sessions.close)
        # The injected first failure prevents the ordinary component sequence
        # from finishing; clean the unaffected components after removing it.
        self.addCleanup(sessions._close_components)
        sessions.start_database_checkpoint()
        self.assertTrue(sessions.database_checkpoint.enabled)
        with patch.object(sessions.classifier, "close", side_effect=RuntimeError("Component close failed")):
            with self.assertRaisesRegex(RuntimeError, "Component close failed"):
                sessions.close()
        self.assertTrue(sessions.closed)
        self.assertFalse(sessions.database_checkpoint._thread.is_alive())
        self.assertEqual(sessions.database_checkpoint.snapshot()["state"], "stopped")
        self.assertEqual(automatic_checkpoint_pages(self.path), AUTOMATIC_CHECKPOINT_PAGES)

    def test_checkpoint_io_does_not_hold_writer_admission_and_close_joins_worker(self):
        self.require_safe_sqlite()
        worker = self.worker()
        entered, release, closed = threading.Event(), threading.Event(), threading.Event()
        execute = worker._execute_checkpoint

        def delayed_checkpoint(connection):
            entered.set()
            release.wait(2)
            return execute(connection)

        with patch.object(worker, "_execute_checkpoint", side_effect=delayed_checkpoint):
            self.assertTrue(worker.start())
            self.assertTrue(entered.wait(1))
            closer = threading.Thread(target=lambda: (worker.close(), closed.set()))
            closer.start()
            try:
                self.wait_for(lambda: automatic_checkpoint_pages(self.path) == AUTOMATIC_CHECKPOINT_PAGES)
                self.assertFalse(closed.is_set())
                with writer_for(self.path).reserve(object(), timeout=.1):
                    self.writer.execute("INSERT INTO records VALUES (2)")
                with sqlite3.connect(self.path) as reader:
                    self.assertEqual(reader.execute("SELECT count(*) FROM records").fetchone()[0], 2)
                reader.close()
            finally:
                release.set()
                closer.join(2)
        self.assertFalse(closer.is_alive())
        self.assertTrue(closed.is_set())
        self.assertFalse(worker._thread.is_alive())

    def test_passive_checkpoint_reports_reader_backlog_and_resumes_progress(self):
        self.require_safe_sqlite()
        self.writer.execute("PRAGMA wal_autocheckpoint=0")
        reader = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(reader.close)
        reader.execute("BEGIN")
        reader.execute("SELECT count(*) FROM records").fetchone()
        self.writer.execute("INSERT INTO records VALUES (2)")
        worker = self.worker(interval=.01)
        self.assertTrue(worker.start())
        self.wait_for(lambda: worker.snapshot()["attempts"] > 0)
        blocked = worker.snapshot()
        self.assertGreater(blocked["backlog_frames"], 0)
        self.assertGreater(blocked["backlog_bytes"], 0)
        self.assertEqual(reader.execute("SELECT count(*) FROM records").fetchone()[0], 1)
        self.writer.execute("INSERT INTO records VALUES (3)")
        reader.execute("ROLLBACK")
        self.wait_for(lambda: worker.snapshot()["backlog_frames"] == 0)
        self.assertIsNotNone(worker.snapshot()["last_progress_at"])
        self.assertEqual(worker.snapshot()["backlog_age_seconds"], 0)
        self.assertEqual(self.writer.execute("SELECT count(*) FROM records").fetchone()[0], 3)

    def test_reporting_is_bounded_and_does_not_include_database_content(self):
        worker = self.worker(sample_limit=3)
        with patch.object(worker, "_execute_checkpoint", return_value=(0, 9, 4)):
            for _ in range(10):
                worker._checkpoint(None)
        status = worker.snapshot()
        self.assertEqual(status["attempts"], 10)
        self.assertEqual(status["sample_count"], 3)
        self.assertEqual(status["backlog_frames"], 5)
        with patch.object(worker, "_execute_checkpoint", side_effect=sqlite3.OperationalError("private SQL and path")):
            worker._checkpoint(None)
        status = worker.snapshot()
        self.assertEqual(status["errors"], 1)
        self.assertEqual(status["last_error"], "OperationalError")
        self.assertNotIn("private SQL", json.dumps(status))
        self.assertNotIn(str(self.path), json.dumps(status))
        with patch.object(worker, "_execute_checkpoint", return_value=(0, 9, 9)):
            worker._checkpoint(None)
        self.assertEqual(worker.snapshot()["backlog_frames"], 0)
        self.assertIsNotNone(worker.snapshot()["last_progress_at"])

    def test_busy_checkpoint_is_retried_without_new_wal_writes(self):
        self.require_safe_sqlite()
        worker = self.worker(interval=.01)
        with patch.object(worker, "_execute_checkpoint", side_effect=[(1, -1, -1), (0, 1, 1)]) as execute:
            self.assertTrue(worker.start())
            self.wait_for(lambda: worker.snapshot()["attempts"] >= 2)
            worker.close()
        self.assertEqual(execute.call_count, 2)
        self.assertEqual(worker.snapshot()["busy"], 1)
        self.assertEqual(worker.snapshot()["backlog_frames"], 0)

    def test_fully_checkpointed_new_wal_cycle_refreshes_progress_with_equal_frame_counts(self):
        worker = self.worker()
        with patch.object(worker, "_execute_checkpoint", return_value=(0, 3, 3)), \
                patch.object(worker, "_wal_signature", side_effect=[(10, 1000), (20, 1000)]), \
                patch("agent_coord.database_checkpoint.time.time", side_effect=[100, 200]):
            worker._checkpoint(None)
            worker._checkpoint(None)
        self.assertEqual(worker.snapshot()["last_progress_at"], 200)

    def test_inherited_worker_rejects_use_after_fork(self):
        worker = self.worker()
        with patch("agent_coord.database_checkpoint.os.getpid", return_value=worker._process_id + 1):
            for operation in (worker.start, worker.close, worker.snapshot):
                with self.subTest(operation=operation.__name__), self.assertRaisesRegex(RuntimeError, "process fork"):
                    operation()
