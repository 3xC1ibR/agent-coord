from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins/agent-coord/scripts'))
from agent_coord import desktop_runtime
from agent_coord.store import CoordinationStore
from agent_coord.store import CoordinationError
from agent_coord.database_writer import DatabaseWriter
from unittest.mock import patch


class DatabaseWriterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / 'state.sqlite3'
        self.store = CoordinationStore(self.path)
        with self.store._connection() as db:
            db.execute('CREATE TABLE probe (value INTEGER)')
        self.identity = str(self.path.resolve())
        desktop_runtime._OWNED_DATABASES.add(self.identity)
        self.addCleanup(desktop_runtime._OWNED_DATABASES.discard, self.identity)

    def test_runtime_writers_share_admission_across_store_instances(self):
        other = CoordinationStore(self.path)
        self.assertIs(other._writer, self.store._writer)
        start = threading.Barrier(8)
        active, maximum = 0, 0
        guard = threading.Lock()
        def write(index):
            nonlocal active, maximum
            start.wait()
            store = self.store if index % 2 else other
            with store.database_operation('test.write'), store._connection() as db:
                with guard:
                    active += 1
                    maximum = max(maximum, active)
                db.execute('SELECT count(*) FROM probe').fetchone()
                time.sleep(.003)
                db.execute('INSERT INTO probe VALUES (?)', (index,))
                with guard:
                    active -= 1
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(write, range(8)))
        self.assertEqual(maximum, 1)
        with self.store.reading(), self.store._connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM probe').fetchone()[0], 8)
        self.assertEqual(self.store.database_metrics()['admission_timeouts'], 0)

    def test_readers_bypass_writer_and_zero_timeout_does_not_leave_queue_entry(self):
        entered, release = threading.Event(), threading.Event()
        def hold():
            with self.store._connection() as db:
                db.execute('INSERT INTO probe VALUES (1)')
                entered.set()
                release.wait(3)
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(hold)
            self.assertTrue(entered.wait(2))
            try:
                with self.store.reading(), self.store._connection() as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM probe').fetchone()[0], 0)
                with self.assertRaises(sqlite3.OperationalError) as caught:
                    with self.store.connection_timeout(0), self.store._connection():
                        self.fail('contending writer admitted')
                self.assertEqual(caught.exception.sqlite_errorcode, sqlite3.SQLITE_BUSY)
                self.assertEqual(self.store.database_metrics()['queue_depth'], 0)
            finally:
                release.set()
            future.result()

    def test_nested_reads_work_and_nested_writes_fail_without_waiting(self):
        with self.store._connection() as outer:
            outer.execute('INSERT INTO probe VALUES (1)')
            with self.store._connection() as inner:
                self.assertEqual(inner.execute('SELECT count(*) FROM probe').fetchone()[0], 0)
                with self.assertRaisesRegex(sqlite3.OperationalError, 'readonly'):
                    inner.execute('INSERT INTO probe VALUES (2)')
            self.assertTrue(outer.in_transaction)

    def test_commit_failure_releases_admission(self):
        with self.store._connection() as db:
            db.executescript('CREATE TABLE parent (id INTEGER PRIMARY KEY); '
                             'CREATE TABLE child (parent INTEGER REFERENCES parent(id) DEFERRABLE INITIALLY DEFERRED);')
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store._connection() as db:
                db.execute('INSERT INTO child VALUES (42)')
        with ThreadPoolExecutor(max_workers=1) as executor:
            def write():
                with self.store._connection() as db:
                    db.execute('INSERT INTO parent VALUES (42)')
            executor.submit(write).result(timeout=2)
        self.assertIsNone(self.store.database_metrics()['active_operation'])

    def test_scripts_ctes_and_returning_are_covered_by_scope(self):
        with self.store._connection() as db:
            db.executescript('INSERT INTO probe VALUES (1); INSERT INTO probe VALUES (2);')
            self.assertEqual(db.execute('WITH n(v) AS (SELECT 3) INSERT INTO probe SELECT v FROM n RETURNING value').fetchall()[0][0], 3)
            with self.assertRaisesRegex(sqlite3.OperationalError, 'readonly'):
                with self.store._connection() as nested:
                    nested.executescript('INSERT INTO probe VALUES (4);')
        with self.store.reading(), self.store._connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM probe').fetchone()[0], 3)

    def test_metrics_are_bounded_and_include_request_errors_without_input(self):
        for value in range(2050):
            self.store._writer.record_request('send_acceptance', value / 10, 503 if value == 0 else 200)
        metrics = self.store.database_metrics()
        self.assertEqual(metrics['requests']['send_acceptance']['count'], 2050)
        self.assertEqual(metrics['requests']['send_acceptance']['errors'], 1)
        self.assertEqual(metrics['requests']['send_acceptance']['samples'], 2048)
        self.assertGreater(metrics['requests']['send_acceptance']['p99_ms'], 200)
        with self.assertRaises(ValueError), self.store._writer.measure_request('send.execution'):
            raise ValueError('private-request-content')
        phases = self.store.database_metrics()['requests']
        self.assertEqual(phases['send.execution']['count'], 1)
        self.assertEqual(phases['send.execution']['errors'], 1)
        self.assertNotIn('private-request-content', repr(phases))
        for value in range(100):
            self.store._writer.record_request('operation_' + str(value), 1, 200)
        self.assertLessEqual(len(self.store.database_metrics()['requests']), 65)

    def test_background_writer_progresses_during_foreground_queue(self):
        writer = DatabaseWriter()
        order = []
        def enter(name, priority):
            with writer.reserve(object(), priority=priority):
                order.append(name)
        with ThreadPoolExecutor(max_workers=11) as executor:
            with writer.reserve(object()):
                pending = [executor.submit(enter, 'background', 'background')]
                pending += [executor.submit(enter, str(index), 'foreground') for index in range(10)]
                deadline = time.monotonic() + 2
                while writer.snapshot()['queue_depth'] < 11 and time.monotonic() < deadline:
                    time.sleep(.001)
                self.assertEqual(writer.snapshot()['queue_depth'], 11)
            for future in pending:
                future.result(timeout=2)
        self.assertGreater(order.index('background'), 0)
        self.assertLessEqual(order.index('background'), 8)
        self.assertEqual(writer.snapshot()['queue_depth'], 0)

    def test_named_latency_metrics_survive_many_startup_operations(self):
        writer = DatabaseWriter()
        for index in range(100):
            with writer.reserve(object(), operation='initialization_' + str(index), named=False):
                pass
        with writer.reserve(object(), operation='history.stage'):
            pass
        self.assertEqual(writer.snapshot()['operations']['history.stage']['count'], 1)
        for index in range(100):
            with writer.reserve(object(), operation='named_' + str(index)):
                pass
        self.assertLessEqual(len(writer.snapshot()['operations']), 129)

    def test_cancelled_waiter_does_not_block_next_writer(self):
        writer = DatabaseWriter()
        class Cancelled(BaseException):
            pass
        def cancelled():
            with patch.object(writer.condition, 'wait', side_effect=Cancelled):
                with self.assertRaises(Cancelled):
                    with writer.reserve(object()):
                        self.fail('cancelled waiter admitted')
        with ThreadPoolExecutor(max_workers=1) as executor:
            with writer.reserve(object()):
                executor.submit(cancelled).result(timeout=2)
                self.assertEqual(writer.snapshot()['queue_depth'], 0)
        with writer.reserve(object()):
            pass
        self.assertEqual(writer.snapshot()['completed_scopes'], 2)

    @unittest.skipUnless(hasattr(os, 'fork'), 'requires fork')
    def test_fork_rejects_inherited_store_and_does_not_inherit_runtime_ownership(self):
        with self.store._connection():
            pid = os.fork()
            if pid == 0:
                try:
                    if self.identity in desktop_runtime._OWNED_DATABASES:
                        os._exit(3)
                    try:
                        with self.store._connection():
                            pass
                    except CoordinationError:
                        os._exit(0)
                    os._exit(4)
                except BaseException:
                    os._exit(5)
            _, status = os.waitpid(pid, 0)
            self.assertEqual(os.waitstatus_to_exitcode(status), 0)


if __name__ == '__main__':
    unittest.main()
