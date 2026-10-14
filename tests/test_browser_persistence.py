import sqlite3
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))

from agent_coord.browser_persistence import BrowserPersistence, database_busy


class BrowserPersistenceTests(unittest.TestCase):
    def test_retries_in_order_and_stops_its_worker(self):
        persistence = BrowserPersistence(retry_delay=.01)
        self.addCleanup(persistence.close)
        unlocked = threading.Event()
        saved = []

        def first():
            if not unlocked.is_set():
                raise sqlite3.OperationalError("database is locked")
            saved.append("first")

        persistence.submit(first)
        persistence.submit(lambda: saved.append("second"))
        self.assertFalse(persistence.flush(timeout=.02))
        self.assertEqual(saved, [])
        unlocked.set()
        self.assertTrue(persistence.flush(timeout=2))
        self.assertEqual(saved, ["first", "second"])
        persistence.close()
        self.assertFalse(persistence.worker.is_alive())

    def test_non_lock_error_is_reported_without_retrying_forever(self):
        persistence = BrowserPersistence()
        self.addCleanup(persistence.close)
        calls = []

        def invalid():
            calls.append(1)
            raise sqlite3.OperationalError("no such table: missing")

        with self.assertLogs("agent_coord.browser_persistence", level="ERROR"):
            persistence.submit(invalid)
        self.assertTrue(persistence.flush(timeout=.01))
        self.assertEqual(calls, [1])
        self.assertIsNone(persistence.worker)

    def test_busy_codes_and_python_310_fallback(self):
        error = sqlite3.OperationalError("localized error")
        error.sqlite_errorcode = 517  # SQLITE_BUSY_SNAPSHOT
        self.assertTrue(database_busy(error))
        error.sqlite_errorcode = 10  # SQLITE_IOERR is not retryable contention.
        self.assertFalse(database_busy(error))
        self.assertTrue(database_busy(sqlite3.OperationalError("database is locked")))
