"""Individual thread detail reads need only the referenced notification owners."""
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins/agent-coord/scripts'))

from agent_coord.notification_policy import available_owners
from agent_coord.store import CoordinationStore


class NotificationOwnerReadTests(unittest.TestCase):
    def test_targeted_owners_preserve_live_and_retired_owner_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            store = CoordinationStore(Path(directory) / 'state.sqlite3')
            for identity in ('active', 'retired', 'unrelated'):
                store.register(session_id=identity, client='codex', cwd=directory)
            with store._connection() as db:
                db.execute("UPDATE sessions SET ended_at=1 WHERE session_id='retired'")
            with store.reading(), store._connection() as db:
                all_owners = available_owners(db, store)
                self.assertIn('active', all_owners)
                self.assertNotIn('retired', all_owners)
                candidates = ['active', 'retired', 'missing', None, 'active']
                self.assertEqual(available_owners(db, store, thread_ids=candidates),
                                 all_owners.intersection(candidates))
                self.assertEqual(available_owners(db, store, thread_ids=['retired']), set())

    def test_unowned_thread_does_not_query_other_threads(self):
        database, store = Mock(), Mock()
        self.assertEqual(available_owners(database, store, thread_ids=[None, None]), set())
        database.execute.assert_not_called()

    def test_individual_thread_detail_uses_targeted_owner_query(self):
        from test_codex_app_server import FakeCodex
        from agent_coord.codex_app_server import BrowserSessions
        with tempfile.TemporaryDirectory() as directory:
            store = CoordinationStore(Path(directory) / 'state.sqlite3')
            sessions = BrowserSessions(store, directory, rpc_factory=FakeCodex)
            try:
                identity = sessions.create({})['session']['thread_id']
                with patch('agent_coord.notification_policy.available_owners', wraps=available_owners) as owners:
                    sessions.work_thread(identity)
                self.assertEqual(owners.call_count, 1)
                self.assertEqual(owners.call_args.kwargs['thread_ids'], (None, None))
            finally:
                sessions.close()
