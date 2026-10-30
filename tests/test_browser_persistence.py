import sqlite3
import sys
import threading
import unittest
from contextlib import contextmanager
from unittest.mock import patch
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
            persistence.submit(invalid, key="failed")
            self.assertFalse(persistence.flush(timeout=1, key="failed"))
        self.assertEqual(calls, [1])
        self.assertIn("no such table", persistence.failure("failed"))

    def test_submit_never_executes_on_the_provider_reader(self):
        persistence = BrowserPersistence()
        self.addCleanup(persistence.close)
        executing, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        identities = []

        def write():
            identities.append(threading.get_ident())
            executing.set()
            release.wait(2)

        persistence.submit(write, key="conversation")
        self.assertTrue(executing.wait(1))
        self.assertNotEqual(identities, [threading.get_ident()])
        self.assertFalse(persistence.flush(timeout=.01, key="conversation"))
        release.set()
        self.assertTrue(persistence.flush(timeout=1, key="conversation"))

    def test_contended_conversation_yields_without_reordering_its_lifecycle(self):
        persistence = BrowserPersistence(retry_delay=.01)
        self.addCleanup(persistence.close)
        release = threading.Event()
        self.addCleanup(release.set)
        saved = []

        def first():
            if not release.is_set():
                raise sqlite3.OperationalError("database is locked")
            saved.append("first")

        persistence.submit(first, key="blocked")
        persistence.submit(lambda: saved.append("second"), key="blocked")
        persistence.submit(lambda: saved.append("independent"), key="other")
        self.assertTrue(persistence.flush(timeout=1, key="other"))
        self.assertEqual(saved, ["independent"])
        self.assertFalse(persistence.flush(timeout=0, key="blocked"))
        release.set()
        self.assertTrue(persistence.flush(timeout=1))
        self.assertEqual(saved, ["independent", "first", "second"])

    def test_busy_retry_starts_promptly_and_backs_off_for_sustained_contention(self):
        persistence = BrowserPersistence()
        self.addCleanup(persistence.close)
        clock = [0.0]
        attempts, following = [], []
        completed = threading.Event()
        real_wait = persistence.condition.wait

        def advance_wait(timeout=None):
            if threading.current_thread() is persistence.worker and timeout is not None:
                clock[0] += timeout
                return False
            return real_wait(timeout)

        def sustained():
            attempts.append(clock[0])
            if len(attempts) <= 7:
                raise sqlite3.OperationalError("database is locked")

        def brief():
            following.append(clock[0])
            if len(following) == 1:
                raise sqlite3.OperationalError("database is locked")
            completed.set()

        with patch("agent_coord.browser_persistence.time.monotonic", side_effect=lambda: clock[0]), \
                patch.object(persistence.condition, "wait", side_effect=advance_wait):
            persistence.submit(sustained, key="conversation")
            persistence.submit(brief, key="conversation")
            self.assertTrue(completed.wait(1))
            self.assertTrue(persistence.flush(timeout=1))
        intervals = [later - earlier for earlier, later in zip(attempts, attempts[1:])]
        self.assertLessEqual(intervals[0], .02)
        self.assertEqual(intervals, sorted(intervals))
        self.assertAlmostEqual(intervals[-1], .25)
        self.assertTrue(all(interval <= .25 + 1e-9 for interval in intervals))
        self.assertAlmostEqual(following[1] - following[0], .01)

    def test_coalesced_snapshots_keep_bounded_work_and_latest_value(self):
        persistence = BrowserPersistence()
        self.addCleanup(persistence.close)
        executing, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        saved = []

        def first():
            executing.set()
            release.wait(2)

        persistence.submit(first, key="conversation")
        self.assertTrue(executing.wait(1))
        for value in range(2000):
            persistence.submit(lambda value=value: saved.append(value), key="conversation", coalesce="history")
        self.assertEqual(persistence.status()["pending"], 2)
        release.set()
        self.assertTrue(persistence.flush(timeout=1))
        self.assertEqual(saved, [1999])

    def test_barrier_does_not_wait_for_later_submissions(self):
        persistence = BrowserPersistence()
        self.addCleanup(persistence.close)
        first, second = threading.Event(), threading.Event()
        self.addCleanup(first.set)
        self.addCleanup(second.set)
        waiting = threading.Event()
        wait_for = persistence.condition.wait_for

        def observed_wait(predicate, timeout=None):
            waiting.set()
            return wait_for(predicate, timeout=timeout)

        persistence.submit(lambda: first.wait(2), key="conversation")
        results = []
        with patch.object(persistence.condition, "wait_for", side_effect=observed_wait):
            barrier = threading.Thread(target=lambda: results.append(persistence.flush(timeout=1, key="conversation")))
            barrier.start()
            self.assertTrue(waiting.wait(1))
            persistence.submit(lambda: second.wait(2), key="conversation")
            first.set()
            barrier.join(1)
        self.assertEqual(results, [True])
        self.assertFalse(persistence.flush(timeout=0, key="conversation"))
        second.set()

    def test_snapshot_coalescing_never_crosses_required_lifecycle_boundary(self):
        persistence = BrowserPersistence()
        self.addCleanup(persistence.close)
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        saved = []

        def hold():
            started.set()
            release.wait(2)

        persistence.submit(hold, key="conversation")
        self.assertTrue(started.wait(1))
        persistence.submit(lambda: saved.append("prior history"), key="conversation", coalesce="history")
        persistence.submit(lambda: saved.append("turn started"), key="conversation")
        persistence.submit(lambda: saved.append("intermediate history"), key="conversation", coalesce="history")
        persistence.submit(lambda: saved.append("latest history"), key="conversation", coalesce="history")
        persistence.submit(lambda: saved.append("turn completed"), key="conversation")
        release.set()
        self.assertTrue(persistence.flush(timeout=1))
        self.assertEqual(saved, ["prior history", "turn started", "latest history", "turn completed"])

    def test_permanent_failure_blocks_only_its_required_sequence(self):
        persistence = BrowserPersistence()
        self.addCleanup(persistence.close)
        saved = []

        def invalid():
            raise ValueError("Invalid lifecycle record")

        with self.assertLogs("agent_coord.browser_persistence", level="ERROR"):
            persistence.submit(invalid, key="failed")
            persistence.submit(lambda: saved.append("must not overtake"), key="failed")
            persistence.submit(lambda: saved.append("independent"), key="other")
            self.assertTrue(persistence.flush(timeout=1, key="other"))
            self.assertFalse(persistence.flush(timeout=1, key="failed"))
        self.assertEqual(saved, ["independent"])
        self.assertIn("failed", persistence.status()["failures"])

    def test_optional_failure_releases_following_lifecycle_work(self):
        persistence = BrowserPersistence()
        self.addCleanup(persistence.close)
        saved = []

        def invalid():
            raise ValueError("Invalid telemetry")

        with self.assertLogs("agent_coord.browser_persistence", level="ERROR"):
            persistence.submit(invalid, key="conversation", required=False)
            persistence.submit(lambda: saved.append("complete"), key="conversation")
            self.assertTrue(persistence.flush(timeout=1, key="conversation"))
        self.assertEqual(saved, ["complete"])
        self.assertIsNone(persistence.failure("conversation"))

    def test_context_is_entered_and_released_on_worker(self):
        observations = []

        @contextmanager
        def context():
            observations.append(("enter", threading.get_ident()))
            yield
            observations.append(("exit", threading.get_ident()))

        persistence = BrowserPersistence(context=context)
        self.addCleanup(persistence.close)
        persistence.submit(lambda: observations.append(("write", threading.get_ident())))
        self.assertTrue(persistence.flush(timeout=1))
        self.assertEqual([item[0] for item in observations], ["enter", "write", "exit"])
        self.assertEqual(len({item[1] for item in observations}), 1)
        self.assertNotEqual(observations[0][1], threading.get_ident())

    def test_lifecycle_backlog_fails_closed_at_capacity(self):
        persistence = BrowserPersistence(max_pending_per_key=2)
        self.addCleanup(persistence.close)
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def blocked():
            started.set()
            release.wait(2)

        persistence.submit(blocked, key="conversation")
        self.assertTrue(started.wait(1))
        persistence.submit(lambda: None, key="conversation")
        with self.assertLogs("agent_coord.browser_persistence", level="ERROR"):
            for _ in range(100):
                persistence.submit(lambda: None, key="conversation")
        self.assertEqual(persistence.status()["pending"], 2)
        self.assertFalse(persistence.flush(timeout=0, key="conversation"))
        self.assertIn("capacity", persistence.failure("conversation"))

    def test_busy_codes_and_python_310_fallback(self):
        error = sqlite3.OperationalError("localized error")
        error.sqlite_errorcode = 517  # SQLITE_BUSY_SNAPSHOT
        self.assertTrue(database_busy(error))
        error.sqlite_errorcode = 10  # SQLITE_IOERR is not retryable contention.
        self.assertFalse(database_busy(error))
        self.assertTrue(database_busy(sqlite3.OperationalError("database is locked")))
