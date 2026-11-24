"""Foreground projections must not repeat metadata checkout or execution joins."""
from contextlib import contextmanager
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins/agent-coord/scripts'))
from test_codex_app_server import FakeCodex
from agent_coord.codex_app_server import BrowserSessions
from agent_coord.store import CoordinationStore


class ForegroundProjectionTests(unittest.TestCase):
    def test_detail_metadata_uses_one_read_only_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            store = CoordinationStore(Path(directory) / 'state.sqlite3')
            sessions = BrowserSessions(store, directory, rpc_factory=FakeCodex)
            try:
                identity = sessions.create({})['session']['thread_id']
                self.assertTrue(sessions.persistence.flush())
                # Warm shared creator metadata outside the counted observation.
                expected = sessions.work_thread(identity, history=False)
                original = store._connection
                observations = []
                @contextmanager
                def observe():
                    with original() as db:
                        observations.append(db.execute('PRAGMA query_only').fetchone()[0])
                        yield db
                completed = store.database_metrics()['completed_scopes']
                with patch.object(store, '_connection', observe):
                    actual = sessions.work_thread(identity, history=False)
                self.assertEqual(actual, expected)
                self.assertEqual(observations, [1])
                self.assertEqual(store.database_metrics()['completed_scopes'], completed)
            finally:
                sessions.close()

    def test_usage_recovery_remains_outside_metadata_read_transaction(self):
        with tempfile.TemporaryDirectory() as directory:
            store = CoordinationStore(Path(directory) / 'state.sqlite3')
            sessions = BrowserSessions(store, directory, rpc_factory=FakeCodex)
            try:
                identity = sessions.create({})['session']['thread_id']
                sessions.usage_recovery_attempts.discard(identity)
                summary = store.usage.summary(identity)
                summary['observed_at'] = None
                def recover(thread_id):
                    self.assertFalse(getattr(store._connection_options, 'reading', False))
                    with store._connection() as db:
                        self.assertFalse(db.in_transaction)
                    return summary
                with patch.object(store.usage, 'summary', return_value=summary), patch.object(store.usage, 'backfill', side_effect=recover) as recovery:
                    sessions.work_thread(identity)
                recovery.assert_called_once_with(identity)
            finally:
                sessions.close()
