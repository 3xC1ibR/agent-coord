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
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins/agent-coord/scripts'))
from agent_coord import desktop_runtime
from agent_coord import store as store_module
from agent_coord import database_writer as writer_module
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
            with self.store.database_operation('test.holder'), self.store._connection() as db:
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
                metrics = self.store.database_metrics()
                self.assertEqual(metrics['queue_depth'], 0)
                record = metrics['admission_timeout_records'][-1]
                self.assertEqual(record['active_operation'], 'test.holder')
                self.assertEqual(record['active_scope_id'], metrics['active_scope_id'])
                self.assertGreaterEqual(record['active_duration_ms'], 0)
                self.assertEqual(record['queue_depth'], 1)
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

    def test_slow_statement_and_scope_are_correlated_without_statement_content(self):
        clock = [100.0]
        def advance(value):
            clock[0] += .08
            return value
        with patch.object(time, 'perf_counter', side_effect=lambda: clock[0]), \
                patch.object(time, 'monotonic', side_effect=lambda: clock[0]):
            with self.store.database_operation('test.statement'), self.store._connection() as db:
                db.create_function('private_function', 1, advance)
                cursor = db.cursor().execute('SELECT private_function(?) AS private_alias', ('private-value',))
                self.assertEqual(cursor.fetchone()[0], 'private-value')
        records = self.store.database_metrics()['slow_operations']
        statement = next(item for item in records
                         if item['phase'] == 'statement.execute' and item['operation'] == 'test.statement')
        scope = next(item for item in records if item['phase'] == 'scope' and item['operation'] == 'test.statement')
        self.assertEqual(statement['scope_id'], scope['scope_id'])
        self.assertEqual(statement['operation'], 'test.statement')
        self.assertEqual(statement['duration_ms'], 80)
        self.assertEqual(statement['failed'], 0)
        self.assertNotIn('private', repr(records))

    def test_slow_bulk_fetch_and_explicit_cursor_factory_keep_results(self):
        class CustomCursor(sqlite3.Cursor):
            pass
        clock = [100.0]
        def advance(value):
            clock[0] += .04
            return value
        with self.store.database_operation('test.fetch'), self.store._connection() as db:
            db.executemany('INSERT INTO probe VALUES (?)', [(1,), (2,), (3,)])
            db.create_function('advance', 1, advance)
            with patch.object(time, 'perf_counter', side_effect=lambda: clock[0]):
                cursor = db.execute('SELECT advance(value) FROM probe')
                self.assertEqual([row[0] for row in cursor.fetchall()], [1, 2, 3])
            custom = db.cursor(factory=CustomCursor)
            self.assertIs(type(custom), CustomCursor)
            self.assertEqual(custom.execute('SELECT COUNT(*) FROM probe').fetchone()[0], 3)
        fetches = [item for item in self.store.database_metrics()['slow_operations']
                   if item['phase'] == 'statement.fetchall']
        self.assertEqual(len(fetches), 1)
        self.assertEqual(fetches[0]['duration_ms'], 80)

    def test_slow_commit_failure_preserves_sqlite_automatic_rollback(self):
        with self.store._connection() as db:
            db.executescript('CREATE TABLE parent (id INTEGER PRIMARY KEY); '
                             'CREATE TABLE child (parent INTEGER REFERENCES parent(id) DEFERRABLE INITIALLY DEFERRED);')
        clock = [100.0]
        def authorize(action, first, *unused):
            if action == sqlite3.SQLITE_TRANSACTION and first == 'COMMIT':
                clock[0] += .08
            return sqlite3.SQLITE_OK
        with patch.object(time, 'perf_counter', side_effect=lambda: clock[0]):
            with self.assertRaises(sqlite3.IntegrityError) as caught:
                with self.store.database_operation('test.commit'), self.store._connection() as db:
                    db.set_authorizer(authorize)
                    db.execute('INSERT INTO child VALUES (42)')
        with self.store.reading(), self.store._connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM child').fetchone()[0], 0)
        record = next(item for item in self.store.database_metrics()['slow_operations']
                      if item['phase'] == 'transaction.commit')
        self.assertEqual(record['duration_ms'], 80)
        self.assertEqual(record['failed'], 1)
        self.assertEqual(record['error_code'], caught.exception.sqlite_errorcode)
        self.assertEqual(record['transaction_open'], 1)
        self.assertIsNone(self.store.database_metrics()['active_operation'])

    def test_slow_rollback_preserves_original_failure_and_discards_writes(self):
        clock = [100.0]
        def authorize(action, first, *unused):
            if action == sqlite3.SQLITE_TRANSACTION and first == 'ROLLBACK':
                clock[0] += .08
            return sqlite3.SQLITE_OK
        with patch.object(time, 'perf_counter', side_effect=lambda: clock[0]):
            with self.assertRaisesRegex(ValueError, 'private-failure'):
                with self.store.database_operation('test.rollback'), self.store._connection() as db:
                    db.set_authorizer(authorize)
                    db.execute('INSERT INTO probe VALUES (1)')
                    raise ValueError('private-failure')
        with self.store.reading(), self.store._connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM probe').fetchone()[0], 0)
        records = self.store.database_metrics()['slow_operations']
        record = next(item for item in records if item['phase'] == 'transaction.rollback')
        self.assertEqual(record['duration_ms'], 80)
        self.assertEqual(record['failed'], 0)  # Rollback itself succeeded.
        self.assertNotIn('private-failure', repr(records))

    def test_read_only_slow_diagnostics_refresh_when_connection_is_reused(self):
        before = self.store.database_metrics()['completed_scopes']
        connections = []
        for operation in ('test.first_read', 'test.second_read'):
            with self.store.database_operation(operation), self.store.reading(), self.store._connection() as db:
                connections.append(db)
                with patch.object(time, 'perf_counter', side_effect=[100, 100.08]):
                    self.assertEqual(db.execute('SELECT 1').fetchone()[0], 1)
                with self.assertRaises(sqlite3.OperationalError):
                    db.execute('INSERT INTO probe VALUES (1)')
        self.assertIs(connections[0], connections[1])
        metrics = self.store.database_metrics()
        self.assertEqual(metrics['completed_scopes'], before)
        records = [item for item in metrics['slow_operations'] if item['phase'] == 'statement.execute']
        self.assertEqual([item['operation'] for item in records], ['test.first_read', 'test.second_read'])
        self.assertTrue(all(item['read_only'] == 1 and item['scope_id'] == 0 for item in records))

    def test_slow_diagnostics_are_bounded_and_fast_path_does_not_lock(self):
        writer = DatabaseWriter()
        with patch.object(writer, 'condition', None):
            writer.record_slow_operation('statement.execute', 'test.fast', 49.9)
        for index in range(40):
            writer.record_slow_operation('statement.execute', 'test.slow', 80, connection_id=index)
        snapshot = writer.snapshot()
        self.assertEqual(snapshot['slow_operation_count'], 40)
        self.assertEqual(len(snapshot['slow_operations']), 32)
        self.assertEqual(snapshot['slow_operations'][0]['connection_id'], 8)
        snapshot['slow_operations'][0]['operation'] = 'changed'
        self.assertEqual(writer.snapshot()['slow_operations'][0]['operation'], 'test.slow')
        allowed_strings = {'phase', 'operation'}
        for record in writer.snapshot()['slow_operations']:
            self.assertTrue(all(isinstance(value, (int, float)) for key, value in record.items()
                                if key not in allowed_strings))

    def test_slow_diagnostic_failure_does_not_change_statement_result(self):
        with self.store._connection() as db:
            with patch.object(self.store._writer, 'record_slow_operation', side_effect=RuntimeError('metrics failed')):
                with patch.object(time, 'perf_counter', side_effect=[100, 100.08]):
                    self.assertEqual(db.execute('SELECT 42').fetchone()[0], 42)

    def test_generic_slow_read_identifies_caller_without_tracing_fast_reads(self):
        with self.store.reading(), self.store._connection() as db:
            with patch.object(store_module, '_slow_read_operation', side_effect=AssertionError('fast read traced')):
                with patch.object(time, 'perf_counter', return_value=100):
                    self.assertEqual(db.execute('SELECT 1').fetchone()[0], 1)
            with patch.object(time, 'perf_counter', side_effect=[100, 100.08]):
                self.assertEqual(db.execute('SELECT 2').fetchone()[0], 2)
        record = self.store.database_metrics()['slow_operations'][-1]
        self.assertEqual(record['operation'],
                         'test_database_writer.test_generic_slow_read_identifies_caller_without_tracing_fast_reads')
        self.assertEqual(record['read_only'], 1)

    def test_slow_read_caller_inference_has_a_fixed_frame_bound(self):
        caller = SimpleNamespace(f_code=SimpleNamespace(co_filename='/private/source/reader.py', co_name='load'), f_back=None)
        frame = caller
        for _ in range(15):
            frame = SimpleNamespace(f_code=SimpleNamespace(co_filename='/source/contextlib.py', co_name='enter'), f_back=frame)
        with patch.object(store_module.sys, '_getframe', return_value=frame):
            self.assertEqual(store_module._slow_read_operation(), 'reader.load')
        frame = SimpleNamespace(f_code=SimpleNamespace(co_filename='/source/store.py', co_name='read'), f_back=frame)
        with patch.object(store_module.sys, '_getframe', return_value=frame):
            self.assertEqual(store_module._slow_read_operation(), 'database.read')

    def test_read_flood_does_not_evict_slow_writer_scope_or_its_phase_summary(self):
        writer, clock = DatabaseWriter(), [100.0]
        with patch.object(time, 'monotonic', side_effect=lambda: clock[0]):
            with writer.reserve(object(), operation='history.stage'):
                scope_id = writer.active_scope_id
                writer.record_slow_operation('statement.execute', 'history.stage', 120, scope_id=scope_id)
                clock[0] += .12
                writer.record_slow_operation('transaction.commit', 'history.stage', 180, scope_id=scope_id)
                clock[0] += .18
        for _ in range(100):
            writer.record_slow_operation('statement.execute', 'test.read', 1000, read_only=1)
        metrics = writer.snapshot()
        self.assertEqual(metrics['slow_operation_count'], 103)
        self.assertTrue(all(item['read_only'] == 1 for item in metrics['slow_operations']))
        self.assertEqual(len(metrics['slow_writer_scopes']), 1)
        retained = metrics['slow_writer_scopes'][0]
        self.assertEqual(retained['duration_ms'], 300)
        self.assertEqual(retained['scope_id'], scope_id)
        self.assertEqual(retained['slow_phase_durations_ms'], {'statement.execute': 120, 'transaction.commit': 180})
        retained['slow_phase_durations_ms']['statement.execute'] = 999
        self.assertEqual(writer.snapshot()['slow_writer_scopes'][0]['slow_phase_durations_ms']['statement.execute'], 120)

    def test_slow_writer_scope_retention_is_bounded_to_longest_scopes(self):
        writer = DatabaseWriter()
        for index in range(40):
            writer.record_slow_operation('scope', 'test.scope', 100 + index, scope_id=index + 1, thread_cpu_ms=index)
        metrics = writer.snapshot()
        self.assertEqual(metrics['slow_operation_count'], 40)
        self.assertEqual(len(metrics['slow_writer_scopes']), 16)
        self.assertEqual([record['duration_ms'] for record in metrics['slow_writer_scopes']], list(range(139, 123, -1)))
        self.assertTrue(all(record['off_cpu_ms'] == 100 for record in metrics['slow_writer_scopes']))

    def test_slow_connection_open_is_outside_writer_scope_without_path_or_sql(self):
        self.store._idle_connections.holder.discard()
        clock = [100.0]
        connect = sqlite3.connect

        def delayed_connect(*args, **kwargs):
            connection = connect(*args, **kwargs)
            clock[0] += .08
            return connection

        with patch.object(time, 'perf_counter', side_effect=lambda: clock[0]), \
                patch.object(time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(sqlite3, 'connect', side_effect=delayed_connect):
            with self.store.database_operation('test.open'), self.store._connection() as connection:
                self.assertEqual(connection.execute("SELECT 'private-query-text'").fetchone()[0], 'private-query-text')
        metrics = self.store.database_metrics()
        opened = next(record for record in metrics['slow_operations']
                      if record['phase'] == 'connection.open' and record['operation'] == 'test.open')
        self.assertEqual(opened['duration_ms'], 80)
        self.assertEqual(opened['operation'], 'test.open')
        self.assertEqual(opened['connection_id'], id(connection))
        self.assertEqual(opened['scope_id'], 0)
        self.assertEqual(metrics['operations']['test.open']['max_ms'], 0)
        self.assertFalse(any(record['operation'] == 'test.open' for record in metrics['slow_writer_scopes']))
        self.assertNotIn('private-query-text', repr(metrics))
        self.assertNotIn(str(self.path), repr(metrics))

    def test_slow_failed_connection_open_keeps_original_error_and_releases_writer(self):
        self.store._idle_connections.holder.discard()
        clock = [100.0]
        error = sqlite3.OperationalError('private database path')
        error.sqlite_errorcode = sqlite3.SQLITE_CANTOPEN

        def failed_connect(*args, **kwargs):
            clock[0] += .08
            raise error

        with patch.object(time, 'perf_counter', side_effect=lambda: clock[0]), \
                patch.object(sqlite3, 'connect', side_effect=failed_connect):
            with self.assertRaises(sqlite3.OperationalError) as caught:
                with self.store.database_operation('test.open_failure'), self.store._connection():
                    self.fail('Failed open returned a connection')
        self.assertIs(caught.exception, error)
        metrics = self.store.database_metrics()
        opened = next(record for record in metrics['slow_operations'] if record['phase'] == 'connection.open')
        self.assertEqual(opened['failed'], 1)
        self.assertEqual(opened['error_code'], sqlite3.SQLITE_CANTOPEN)
        self.assertEqual(opened['connection_id'], 0)
        self.assertIsNone(metrics['active_scope_id'])
        self.assertNotIn('private database path', repr(metrics))

    def test_read_only_connection_open_is_reported_without_writer_admission(self):
        holder = getattr(self.store._idle_connections, 'reader', None)
        if holder is not None:
            holder.discard()
        before = self.store.database_metrics()['completed_scopes']
        clock = [100.0]
        connect = sqlite3.connect

        def delayed_connect(*args, **kwargs):
            connection = connect(*args, **kwargs)
            clock[0] += .08
            return connection

        with patch.object(time, 'perf_counter', side_effect=lambda: clock[0]), \
                patch.object(sqlite3, 'connect', side_effect=delayed_connect):
            with self.store.database_operation('test.read_open'), self.store.reading(), self.store._connection() as connection:
                self.assertEqual(connection.execute('SELECT 1').fetchone()[0], 1)
        metrics = self.store.database_metrics()
        opened = next(record for record in metrics['slow_operations'] if record['phase'] == 'connection.open')
        self.assertEqual(opened['read_only'], 1)
        self.assertEqual(opened['scope_id'], 0)
        self.assertEqual(metrics['completed_scopes'], before)

    def test_connection_open_diagnostic_failure_does_not_change_result(self):
        clock = [100.0]
        connect = sqlite3.connect

        def delayed_connect(*args, **kwargs):
            connection = connect(*args, **kwargs)
            clock[0] += .08
            return connection

        with patch.object(time, 'perf_counter', side_effect=lambda: clock[0]), \
                patch.object(sqlite3, 'connect', side_effect=delayed_connect), \
                patch.object(self.store._writer, 'record_slow_operation', side_effect=RuntimeError('Metric unavailable')):
            connection = self.store._connect()
            try:
                self.assertEqual(connection.execute('SELECT 42').fetchone()[0], 42)
            finally:
                connection.close()

    def test_slow_scope_cpu_measurement_covers_nested_scopes_once(self):
        writer, clock = DatabaseWriter(), [100.0]
        with patch.object(time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(time, 'thread_time', side_effect=[20.0, 20.04]) as cpu:
            with writer.reserve(object(), operation='test.outer'):
                with writer.reserve(object(), operation='test.inner'):
                    clock[0] += .2
        self.assertEqual(cpu.call_count, 2)
        metrics = writer.snapshot()
        self.assertEqual(len(metrics['slow_writer_scopes']), 1)
        scope = metrics['slow_writer_scopes'][0]
        self.assertEqual(scope['duration_ms'], 200)
        self.assertEqual(scope['thread_cpu_ms'], 40)
        self.assertEqual(scope['off_cpu_ms'], 160)
        self.assertIn('without attribution', metrics['scope_off_cpu_definition'])

    def test_unavailable_thread_cpu_clock_does_not_block_writer_cleanup(self):
        writer, clock = DatabaseWriter(), [100.0]
        with patch.object(time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(time, 'thread_time', side_effect=OSError('Unavailable clock')):
            with writer.reserve(object()):
                clock[0] += .2
        self.assertIsNone(writer.owner)
        scope = writer.snapshot()['slow_writer_scopes'][0]
        self.assertIsNone(scope['thread_cpu_ms'])
        self.assertIsNone(scope['off_cpu_ms'])

    def test_host_and_process_metrics_have_fixed_fields_and_platform_correct_rss_bytes(self):
        usage = SimpleNamespace(RUSAGE_SELF=0, getrusage=lambda unused: SimpleNamespace(ru_maxrss=123))
        for platform, maximum in (('darwin', 123), ('linux', 123 * 1024), ('unknown', None)):
            with self.subTest(platform=platform), patch.object(writer_module, 'resource', usage), \
                    patch.object(sys, 'platform', platform), patch.object(os, 'cpu_count', return_value=8), \
                    patch.object(os, 'getloadavg', return_value=(1.12345, 2.5, 3.75)), \
                    patch.object(time, 'process_time', return_value=12.3456789):
                metrics = writer_module.system_performance_snapshot()
            self.assertEqual(metrics, {
                'host': {'logical_cpu_count': 8, 'load_average': {
                    'one_minute': 1.123, 'five_minutes': 2.5, 'fifteen_minutes': 3.75}},
                'process': {'cpu_seconds': 12.345679, 'peak_resident_memory_bytes': maximum}})

    def test_unavailable_host_metrics_remain_null_without_exposing_errors(self):
        with patch.object(writer_module, 'resource', None), \
                patch.object(os, 'getloadavg', side_effect=OSError('private host detail')), \
                patch.object(os, 'cpu_count', return_value=None), \
                patch.object(time, 'process_time', side_effect=OSError('private process detail')):
            metrics = writer_module.system_performance_snapshot()
        self.assertEqual(metrics, {'host': {'logical_cpu_count': None, 'load_average': None},
                                  'process': {'cpu_seconds': None, 'peak_resident_memory_bytes': None}})
        self.assertNotIn('private', repr(metrics))

    def test_performance_snapshot_includes_system_metrics_without_failure_content(self):
        sessions = SimpleNamespace(store=self.store, history_persistence=SimpleNamespace(status=lambda: {}),
                                   persistence=SimpleNamespace(status=lambda: {'failures': {'private-thread': 'private failure'}}))
        system = {'host': {'logical_cpu_count': 8}, 'process': {'cpu_seconds': 1.5}}
        with patch.object(writer_module, 'system_performance_snapshot', return_value=system), \
                patch('agent_coord.conversation_search.search_metrics', return_value={}):
            metrics = writer_module.performance_snapshot(sessions)
        self.assertEqual(metrics['host'], system['host'])
        self.assertEqual(metrics['process'], system['process'])
        self.assertEqual(metrics['persistence']['failure_count'], 1)
        self.assertNotIn('private', repr(metrics))

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
