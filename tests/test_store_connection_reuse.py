from __future__ import annotations

import gc
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))

from agent_coord.store import CoordinationStore, _StoreConnection
from agent_coord import desktop_runtime


class StoreConnectionReuseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "coord.sqlite3"
        self.store = CoordinationStore(self.path)
        with self.store._connection() as db:
            db.execute("CREATE TABLE probe (value INTEGER)")
        self.addCleanup(self.release)

    def release(self):
        self.store = None
        gc.collect()

    def counted(self):
        connect, calls = self.store._connect, []

        def factory():
            calls.append(1)
            return connect()
        return factory, calls

    def outside(self):
        return sqlite3.connect(self.path, timeout=5)

    def replace_database(self):
        replacement = self.path.with_name("replacement.sqlite3")
        with sqlite3.connect(replacement) as other:
            other.execute("CREATE TABLE probe (value INTEGER)")
            other.execute("INSERT INTO probe VALUES (2)")
        other.close()
        replacement.replace(self.path)
        for suffix in ("-wal", "-shm"):
            Path(str(self.path) + suffix).unlink(missing_ok=True)

    def test_sequential_uses_on_one_thread_share_one_connection(self):
        factory, calls = self.counted()
        with patch.object(self.store, "_connect", side_effect=factory):
            seen = set()
            for value in range(3):
                with self.store._connection() as db:
                    db.execute("INSERT INTO probe VALUES (?)", (value,))
                    seen.add(id(db))
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(seen), 1)
        with self.outside() as other:
            self.assertEqual(other.execute("SELECT COUNT(*) FROM probe").fetchone()[0], 3)
        other.close()

    def test_nested_use_cannot_commit_the_outer_transaction(self):
        with self.store._connection() as outer:
            outer.execute("INSERT INTO probe VALUES (1)")
            with self.store._connection() as inner:
                self.assertIsNot(inner, outer)
                inner.execute("SELECT COUNT(*) FROM probe").fetchone()
            other = self.outside()
            self.assertEqual(other.execute("SELECT COUNT(*) FROM probe").fetchone()[0], 0)
            other.close()
        other = self.outside()
        self.assertEqual(other.execute("SELECT COUNT(*) FROM probe").fetchone()[0], 1)
        other.close()

    def test_read_only_connections_reuse_separately_from_writers(self):
        factory, calls = self.counted()
        with patch.object(self.store, "_connect", side_effect=factory):
            for _ in range(3):
                with self.store.reading(), self.store._connection() as db:
                    reader = db
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM probe").fetchone()[0], 0)
            self.assertEqual(len(calls), 1)
            with self.store._connection() as db:
                self.assertIsNot(db, reader)
                db.execute("INSERT INTO probe VALUES (1)")
            with self.store.reading(), self.store._connection() as db:
                self.assertIs(db, reader)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM probe").fetchone()[0], 1)
                with self.assertRaises(sqlite3.OperationalError):
                    db.execute("INSERT INTO probe VALUES (2)")
            self.assertEqual(len(calls), 2)

    def test_failed_use_rolls_back_and_is_not_reused(self):
        factory, calls = self.counted()
        with patch.object(self.store, "_connect", side_effect=factory):
            with self.assertRaises(RuntimeError), self.store._connection() as db:
                db.execute("INSERT INTO probe VALUES (1)")
                failed = db
                raise RuntimeError("stop")
            with self.store._connection() as db:
                self.assertIsNot(db, failed)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM probe").fetchone()[0], 0)
        self.assertEqual(len(calls), 2)

    def test_unfinished_cursor_cannot_pin_an_old_snapshot(self):
        with self.store._connection() as db:
            db.executemany("INSERT INTO probe VALUES (?)", [(1,), (2,), (3,)])
        with self.store._connection() as db:
            cursor = db.execute("SELECT value FROM probe ORDER BY value")
            self.assertEqual(cursor.fetchone()[0], 1)
        other = self.outside()
        with other:
            other.execute("INSERT INTO probe VALUES (4)")
        other.close()
        with self.store._connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM probe").fetchone()[0], 4)
            db.execute("INSERT INTO probe VALUES (5)")
        with self.assertRaises(sqlite3.ProgrammingError):
            cursor.fetchone()

    def test_reuse_restores_connection_settings(self):
        with self.store._connection() as db:
            db.execute("PRAGMA busy_timeout = 0")
            db.execute("PRAGMA foreign_keys = OFF")
            db.row_factory = None
        with self.store._connection() as db:
            self.assertEqual(db.execute("PRAGMA busy_timeout").fetchone()["timeout"], 5000)
            self.assertEqual(db.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_threads_use_separate_connections_closed_when_they_end(self):
        with self.store._connection() as db:
            main = db
        used = []

        def work():
            with self.store._connection() as db:
                db.execute("INSERT INTO probe VALUES (1)")
                used.append(db)

        worker = threading.Thread(target=work)
        worker.start()
        worker.join()
        gc.collect()
        self.assertIsNot(used[0], main)
        with self.assertRaises(sqlite3.ProgrammingError):
            used[0].execute("SELECT 1")
        with self.store._connection() as db:
            self.assertIs(db, main)

    def test_replaced_factory_is_used_instead_of_an_idle_connection(self):
        with self.store._connection() as db:
            idle = db
        factory, calls = self.counted()
        with patch.object(self.store, "_connect", side_effect=factory), self.store._connection() as db:
            self.assertIsNot(db, idle)
        self.assertEqual(len(calls), 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            idle.execute("SELECT 1")

    def test_database_replacement_releases_cached_reader_and_writer(self):
        with self.store._connection() as db:
            writer = db
            db.execute("INSERT INTO probe VALUES (1)")
        with self.store.reading(), self.store._connection() as db:
            reader = db
            self.assertEqual(db.execute("SELECT value FROM probe").fetchone()[0], 1)

        self.replace_database()

        with self.store.reading(), self.store._connection() as db:
            self.assertIsNot(db, reader)
            self.assertEqual(db.execute("SELECT value FROM probe").fetchone()[0], 2)
        for old in (reader, writer):
            with self.assertRaises(sqlite3.ProgrammingError):
                old.execute("SELECT 1")
        with self.store._connection() as db:
            self.assertIsNot(db, writer)
            db.execute("INSERT INTO probe VALUES (3)")
        with self.outside() as other:
            self.assertEqual(other.execute("SELECT value FROM probe ORDER BY value").fetchall(), [(2,), (3,)])
        other.close()

    def test_removed_database_releases_both_idle_connection_slots(self):
        with self.store._connection() as db:
            writer = db
        with self.store.reading(), self.store._connection() as db:
            reader = db
        self.path.unlink()
        with self.assertRaises(sqlite3.OperationalError), self.store.reading(), self.store._connection():
            self.fail("A removed database must not be read through a cached handle")
        for old in (reader, writer):
            with self.assertRaises(sqlite3.ProgrammingError):
                old.execute("SELECT 1")

    def test_replacement_during_use_closes_handle_after_use(self):
        with self.store.reading(), self.store._connection() as db:
            previous = db
            self.replace_database()
            self.assertFalse(db.in_transaction)
        with self.assertRaises(sqlite3.ProgrammingError):
            previous.execute("SELECT 1")
        with self.store.reading(), self.store._connection() as db:
            self.assertEqual(db.execute("SELECT value FROM probe").fetchone()[0], 2)

    def test_delayed_open_does_not_hold_writer_admission(self):
        opened, ready, proceed, release, completed = (threading.Event() for _ in range(5))
        failures = []
        connect = self.store._connect

        def factory():
            if threading.current_thread() is delayed:
                opened.set()
                if not release.wait(5):
                    raise TimeoutError("Delayed connection was not released")
            return connect()

        def warm_writer():
            try:
                with self.store._connection() as db:
                    db.execute("SELECT 1").fetchone()
                ready.set()
                if not proceed.wait(5):
                    raise TimeoutError("Warm writer was not released")
                with self.store._connection() as db:
                    db.execute("INSERT INTO probe VALUES (1)")
                completed.set()
            except BaseException as error:
                failures.append(error)

        def delayed_writer():
            try:
                with self.store._connection() as db:
                    db.execute("INSERT INTO probe VALUES (2)")
            except BaseException as error:
                failures.append(error)

        warm = threading.Thread(target=warm_writer)
        delayed = threading.Thread(target=delayed_writer)
        with patch.object(self.store, "_connect", side_effect=factory), \
                patch.object(desktop_runtime, "_OWNED_DATABASES", {self.store._database_identity}):
            try:
                warm.start()
                self.assertTrue(ready.wait(3))
                delayed.start()
                self.assertTrue(opened.wait(3))
                proceed.set()
                self.assertTrue(completed.wait(2), "A cold open blocked an already connected writer")
            finally:
                proceed.set()
                release.set()
                warm.join(5)
                if delayed.ident is not None:
                    delayed.join(5)
        self.assertFalse(warm.is_alive())
        self.assertFalse(delayed.is_alive())
        self.assertEqual(failures, [])
        with self.store.reading(), self.store._connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM probe").fetchone()[0], 2)

    def test_commit_and_rollback_retain_writer_admission(self):
        transactions = []

        def authorize(action, first, *unused):
            if action == sqlite3.SQLITE_TRANSACTION and first in {"COMMIT", "ROLLBACK"}:
                transactions.append((first, self.store._writer.owner == threading.get_ident()))
            return sqlite3.SQLITE_OK

        with self.store._connection() as db:
            db.set_authorizer(authorize)
            db.execute("INSERT INTO probe VALUES (1)")
        with self.assertRaisesRegex(ValueError, "rollback"), self.store._connection() as db:
            db.set_authorizer(authorize)
            db.execute("INSERT INTO probe VALUES (2)")
            raise ValueError("rollback")
        self.assertEqual(transactions, [("COMMIT", True), ("ROLLBACK", True)])
        with self.store.reading(), self.store._connection() as db:
            self.assertEqual(db.execute("SELECT value FROM probe").fetchall()[0][0], 1)

    def test_autocommit_returning_statement_finishes_before_releasing_admission(self):
        completion = []
        finish = _StoreConnection.end_statements

        def observed_finish(connection):
            completion.append((connection.in_transaction, self.store._writer.owner == threading.get_ident()))
            return finish(connection)

        with patch.object(_StoreConnection, "end_statements", observed_finish):
            with self.store._connection() as db:
                cursor = db.execute("WITH values_to_add(value) AS (VALUES (1), (2), (3)) "
                                    "INSERT INTO probe SELECT value FROM values_to_add RETURNING value")
                self.assertEqual(cursor.fetchone()[0], 1)
                self.assertFalse(db.in_transaction)
                other = sqlite3.connect(self.path, timeout=0)
                try:
                    with self.assertRaises(sqlite3.OperationalError):
                        other.execute("INSERT INTO probe VALUES (4)")
                finally:
                    other.close()
        self.assertEqual(completion, [(False, True)])
        with self.assertRaises(sqlite3.ProgrammingError):
            cursor.fetchone()
        other = sqlite3.connect(self.path, timeout=0)
        try:
            with other:
                other.execute("INSERT INTO probe VALUES (4)")
            self.assertEqual(other.execute("SELECT COUNT(*) FROM probe").fetchone()[0], 4)
        finally:
            other.close()

    def test_failed_returning_scope_cannot_leave_a_write_lock_after_close(self):
        completion = []
        finish = _StoreConnection.end_statements

        def observed_finish(connection):
            completion.append(self.store._writer.owner == threading.get_ident())
            return finish(connection)

        with patch.object(_StoreConnection, "end_statements", observed_finish):
            with self.assertRaisesRegex(ValueError, "stop"), self.store._connection() as db:
                cursor = db.execute("WITH values_to_add(value) AS (VALUES (1), (2), (3)) "
                                    "INSERT INTO probe SELECT value FROM values_to_add RETURNING value")
                self.assertEqual(cursor.fetchone()[0], 1)
                self.assertFalse(db.in_transaction)
                raise ValueError("stop")
        self.assertEqual(completion, [True])
        with self.assertRaises(sqlite3.ProgrammingError):
            cursor.fetchone()
        other = sqlite3.connect(self.path, timeout=0)
        try:
            with other:
                other.execute("INSERT INTO probe VALUES (4)")
        finally:
            other.close()


if __name__ == "__main__":
    unittest.main()
