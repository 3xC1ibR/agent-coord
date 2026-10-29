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

from agent_coord.store import CoordinationStore


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


if __name__ == "__main__":
    unittest.main()
