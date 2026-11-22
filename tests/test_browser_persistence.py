import errno
import sqlite3
import sys
import threading
import tempfile
import unittest
from contextlib import closing, contextmanager
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))

from agent_coord.browser_persistence import BrowserPersistence, database_busy, storage_full


class BrowserPersistenceTests(unittest.TestCase):
    def test_real_storage_full_recovers_without_losing_or_reordering_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "history.sqlite3")
            with closing(sqlite3.connect(path)) as db:
                db.execute("CREATE TABLE history (id INTEGER PRIMARY KEY, payload BLOB)")
                db.commit()
                pages = db.execute("PRAGMA page_count").fetchone()[0]
            persistence = BrowserPersistence(storage_retry_delay=.01)
            self.addCleanup(persistence.close)
            capacity = threading.Event()
            self.addCleanup(capacity.set)
            failed = threading.Event()
            saved = []

            def history():
                with closing(sqlite3.connect(path)) as db, db:
                    db.execute(f"PRAGMA max_page_count = {100 if capacity.is_set() else pages}")
                    try:
                        db.execute("INSERT INTO history VALUES (1, zeroblob(16384))")
                    except sqlite3.OperationalError as error:
                        self.assertTrue(storage_full(error))
                        failed.set()
                        raise
                saved.append("history")

            try:
                with self.assertLogs("agent_coord.browser_persistence", level="WARNING"):
                    persistence.submit(history, key="full")
                    persistence.submit(lambda: saved.append("completed"), key="full")
                    self.assertTrue(failed.wait(1))
                    persistence.submit(lambda: saved.append("independent"), key="other")
                    self.assertTrue(persistence.flush(timeout=1, key="other"))
                    self.assertFalse(persistence.flush(timeout=0, key="full"))
                    self.assertIsNone(persistence.failure("full"))
                    self.assertEqual(saved, ["independent"])
                    capacity.set()
                    self.assertTrue(persistence.flush(timeout=2))
                self.assertEqual(saved, ["independent", "history", "completed"])
                with closing(sqlite3.connect(path)) as db:
                    self.assertEqual(db.execute("SELECT count(*), length(payload) FROM history").fetchone(), (1, 16384))
            finally:
                capacity.set()
                persistence.close()

    def test_storage_full_retries_back_off_without_failing_the_conversation(self):
        persistence = BrowserPersistence()
        self.addCleanup(persistence.close)
        clock = [0.0]
        attempts = []
        completed = threading.Event()
        real_wait = persistence.condition.wait

        def advance_wait(timeout=None):
            if threading.current_thread() is persistence.worker and timeout is not None:
                clock[0] += timeout
                return False
            return real_wait(timeout)

        def history():
            attempts.append(clock[0])
            if len(attempts) <= 7:
                raise sqlite3.OperationalError("database or disk is full")
            completed.set()

        with patch("agent_coord.browser_persistence.time.monotonic", side_effect=lambda: clock[0]), \
                patch.object(persistence.condition, "wait", side_effect=advance_wait):
            persistence.submit(history, key="conversation")
            self.assertTrue(completed.wait(1))
            self.assertTrue(persistence.flush(timeout=1))
        self.assertEqual([b - a for a, b in zip(attempts, attempts[1:])], [1, 2, 4, 8, 16, 30, 30])
        self.assertIsNone(persistence.failure("conversation"))

    def test_storage_full_generator_restarts_before_its_following_lifecycle(self):
        persistence = BrowserPersistence(storage_retry_delay=.01)
        self.addCleanup(persistence.close)
        attempts, finalized, saved = [], [], []

        def history():
            attempt = len(attempts)
            attempts.append(attempt)
            try:
                yield
                if attempt == 0:
                    raise OSError(errno.ENOSPC, "No space left on device")
                saved.append("publish")
            finally:
                finalized.append(attempt)

        persistence.submit(history, key="history")
        persistence.submit(lambda: saved.append("completed"), key="history")
        self.assertTrue(persistence.flush(timeout=2))
        self.assertEqual(attempts, [0, 1])
        self.assertEqual(finalized, attempts)
        self.assertEqual(saved, ["publish", "completed"])

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
        self.assertEqual(persistence.status()["optional_failure_count"], 1)
        self.assertEqual(persistence.status()["failures"], {})

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

    def test_generator_steps_rotate_keys_with_separate_contexts_and_keep_lifecycle_order(self):
        saved, scopes = [], []
        depth = [0]

        @contextmanager
        def context():
            self.assertEqual(depth[0], 0)
            depth[0] = 1
            scopes.append("enter")
            try:
                yield
            finally:
                depth[0] = 0
                scopes.append("exit")

        def record(value):
            self.assertEqual(depth[0], 1)
            saved.append(value)

        def history():
            record("stage")
            yield
            record("publish")

        persistence = BrowserPersistence(context=context)
        self.addCleanup(persistence.close)
        with persistence.condition:
            persistence.submit(history, key="history")
            persistence.submit(lambda: record("lifecycle"), key="history")
            persistence.submit(lambda: record("independent"), key="other")
        self.assertTrue(persistence.flush(timeout=1))
        self.assertEqual(saved, ["stage", "independent", "publish", "lifecycle"])
        self.assertEqual(scopes, ["enter", "exit"] * 4)
        self.assertEqual(depth[0], 0)

    def test_suspended_snapshot_is_not_replaced_by_coalescing(self):
        persistence = BrowserPersistence()
        self.addCleanup(persistence.close)
        holding, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        saved = []

        def history():
            saved.append("original stage")
            yield
            saved.append("original publish")

        def hold():
            holding.set()
            release.wait(2)

        with persistence.condition:
            persistence.submit(history, key="history", coalesce="history")
            original = persistence.pending[0]
            identity = (original.sequence, original.created_at)
            persistence.submit(hold, key="other")
        self.assertTrue(holding.wait(1))
        for value in range(20):
            persistence.submit(lambda value=value: saved.append(value), key="history", coalesce="history")
        persistence.submit(lambda: saved.append("lifecycle"), key="history")
        self.assertEqual(persistence.status(key="history")["pending"], 3)
        self.assertEqual((original.sequence, original.created_at), identity)
        self.assertFalse(persistence.flush(timeout=0, key="history"))
        release.set()
        self.assertTrue(persistence.flush(timeout=1))
        self.assertEqual(saved, ["original stage", "original publish", 19, "lifecycle"])

    def test_busy_generator_restarts_factory_without_satisfying_barrier_early(self):
        persistence = BrowserPersistence(retry_delay=.01)
        self.addCleanup(persistence.close)
        failed, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        attempts, finalized, saved = [], [], []
        blocks = set()

        def history():
            attempt = len(attempts)
            attempts.append(attempt)
            try:
                blocks.add("immutable block")
                yield
                if not release.is_set():
                    failed.set()
                    raise sqlite3.OperationalError("database is locked")
                saved.append("publish")
            finally:
                finalized.append(attempt)

        persistence.submit(history, key="history")
        persistence.submit(lambda: saved.append("lifecycle"), key="history")
        self.assertTrue(failed.wait(1))
        self.assertFalse(persistence.flush(timeout=0, key="history"))
        self.assertEqual(saved, [])
        release.set()
        self.assertTrue(persistence.flush(timeout=2))
        self.assertGreaterEqual(len(attempts), 2)
        self.assertEqual(finalized, attempts)
        self.assertEqual(blocks, {"immutable block"})
        self.assertEqual(saved, ["publish", "lifecycle"])
        self.assertGreaterEqual(persistence.status()["retries"], 1)

    def test_permanent_generator_failure_closes_and_blocks_its_lifecycle(self):
        persistence = BrowserPersistence()
        self.addCleanup(persistence.close)
        saved, closed = [], []

        def invalid():
            try:
                yield
                raise ValueError("invalid snapshot")
            finally:
                closed.append(True)

        with self.assertLogs("agent_coord.browser_persistence", level="ERROR"):
            with persistence.condition:
                persistence.submit(invalid, key="failed")
                persistence.submit(lambda: saved.append("must not run"), key="failed")
                persistence.submit(lambda: saved.append("independent"), key="other")
            self.assertTrue(persistence.flush(timeout=1, key="other"))
            self.assertFalse(persistence.flush(timeout=1, key="failed"))
        self.assertEqual(closed, [True])
        self.assertEqual(saved, ["independent"])
        self.assertIn("invalid snapshot", persistence.failure("failed"))

    def test_shutdown_closes_suspended_generators_on_worker_outside_queue_and_context(self):
        depth = [0]
        closed = []

        @contextmanager
        def context():
            depth[0] += 1
            try:
                yield
            finally:
                depth[0] -= 1

        persistence = BrowserPersistence(context=context)
        self.addCleanup(persistence.close)
        holding, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def history():
            try:
                yield
                self.fail("Shutdown must not resume suspended work")
            finally:
                closed.append((threading.get_ident(), depth[0], persistence.condition._is_owned()))

        def hold():
            holding.set()
            release.wait(2)

        with persistence.condition:
            persistence.submit(history, key="history")
            persistence.submit(hold, key="other")
        self.assertTrue(holding.wait(1))
        # An exhausted shutdown budget stops without waiting for required work.
        with self.assertLogs("agent_coord.browser_persistence", level="WARNING"):
            closer = threading.Thread(target=lambda: persistence.close(timeout=0))
            closer.start()
            try:
                with persistence.condition:
                    self.assertTrue(persistence.condition.wait_for(lambda: persistence.stopped, timeout=1))
            finally:
                release.set()
                closer.join(2)
        self.assertFalse(closer.is_alive())
        self.assertEqual(closed, [(persistence.worker.ident, 0, False)])
        self.assertFalse(persistence.worker.is_alive())
        self.assertFalse(persistence.flush(timeout=0, key="history"))

    def test_close_drains_required_work_submitted_while_closing(self):
        # Production shape: a history save submits its derived search seed and
        # a provider completion while shutdown drains. The seed is briefly busy.
        import time
        persistence = BrowserPersistence(retry_delay=.05)
        self.addCleanup(persistence.close)
        saved, attempts = [], {"search": 0, "completion": 0}

        def search():
            attempts["search"] += 1
            if attempts["search"] < 4:
                raise sqlite3.OperationalError("database is locked")
            saved.append("search")

        def completion():
            attempts["completion"] += 1
            if attempts["completion"] < 3:
                raise sqlite3.OperationalError("database is locked")
            saved.append("completion")

        def history():
            while not persistence.closing:
                time.sleep(.005)  # These writes arrive after shutdown begins.
            yield
            persistence.submit(search, key=("history_search", "conversation"), required=False)
            persistence.submit(completion, key="late-provider-event")
            yield
            saved.append("history")

        persistence.submit(history, key="conversation")
        with self.assertLogs("agent_coord.browser_persistence", level="WARNING") as logs:
            report = persistence.close()
        self.assertNotIn("unsaved", "\n".join(logs.output))
        self.assertTrue(report["clean"], report)
        self.assertIn("completion", saved)
        self.assertEqual(report["required_writes"], 0)
        self.assertEqual(report["submitted_while_closing"], 2)
        self.assertGreaterEqual(report["retries_while_closing"], 2)
        self.assertTrue(report["worker_stopped"])
        self.assertIs(persistence.close(), report)

    def test_close_reports_unsaved_required_work_with_stop_evidence(self):
        persistence = BrowserPersistence(retry_delay=.01)
        self.addCleanup(persistence.close)

        def contended():
            raise sqlite3.OperationalError("database is locked")

        persistence.submit(contended, key="conversation")
        persistence.submit(lambda: None, key=("history_search", "conversation"), required=False)
        observed = []
        with self.assertLogs("agent_coord.browser_persistence", level="WARNING") as logs:
            report = persistence.close(timeout=.2, diagnostics=lambda: observed.append(persistence.stopped) or {"queue_depth": 3})
        self.assertEqual(observed, [False])  # Evidence describes the moment before stop.
        self.assertFalse(report["clean"])
        self.assertEqual(report["required_keys"], ["conversation"])
        self.assertEqual(report["required_writes"], 1)
        self.assertGreater(report["oldest_required_age_seconds"], .1)
        self.assertGreater(report["retries_while_closing"], 0)
        self.assertEqual(report["writer"], {"queue_depth": 3})
        self.assertIn('"required_keys": ["conversation"]', "\n".join(logs.output))

    def test_close_shares_its_caller_deadline_and_ignores_unsaved_optional_work(self):
        import time
        persistence = BrowserPersistence(retry_delay=.01)
        self.addCleanup(persistence.close)
        persistence.submit(lambda: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")),
                           key=("search_substrings",), required=False)
        started = time.monotonic()
        report = persistence.close(timeout=30, deadline=started + .1)
        self.assertLess(time.monotonic() - started, 2)
        self.assertTrue(report["clean"], report)
        self.assertEqual(report["optional_writes_unsaved"], 1)

    def test_busy_codes_and_python_310_fallback(self):
        error = sqlite3.OperationalError("localized error")
        error.sqlite_errorcode = 517  # SQLITE_BUSY_SNAPSHOT
        self.assertTrue(database_busy(error))
        error.sqlite_errorcode = 10  # SQLITE_IOERR is not retryable contention.
        self.assertFalse(database_busy(error))
        self.assertTrue(database_busy(sqlite3.OperationalError("database is locked")))

    def test_storage_full_codes_and_python_310_fallback(self):
        error = sqlite3.OperationalError("localized error")
        error.sqlite_errorcode = 13
        self.assertTrue(storage_full(error))
        error.sqlite_errorcode = 13 | (1 << 8)
        self.assertTrue(storage_full(error))
        error.sqlite_errorcode = 10
        self.assertFalse(storage_full(error))
        self.assertTrue(storage_full(sqlite3.OperationalError("database or disk is full")))
        self.assertFalse(storage_full(sqlite3.OperationalError("database is locked")))
        self.assertTrue(storage_full(OSError(errno.ENOSPC, "localized error")))
        self.assertTrue(storage_full(OSError(errno.EDQUOT, "localized error")))
        self.assertFalse(storage_full(OSError(errno.EIO, "I/O error")))
        self.assertFalse(storage_full(ValueError("database or disk is full")))
