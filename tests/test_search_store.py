from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins/agent-coord/scripts'))
from agent_coord.search_store import SearchStore
from agent_coord.store import CoordinationStore
from agent_coord.thread_search import search_threads


def history(text, role='user', status='completed'):
    item = ({'type': 'userMessage', 'content': [{'type': 'text', 'text': text}]}
            if role == 'user' else {'type': 'agentMessage', 'text': text})
    return {'turns': [{'id': 'turn', 'status': status, 'items': [{'id': 'item', **item}]}]}


class SearchStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = CoordinationStore(self.root / 'state.sqlite3')
        self.index = SearchStore.for_store(self.store)
        self.addCleanup(self.index.close)

    def record(self, identity, text, *, title='Unrelated title', role='user', revision=1):
        self.store.register(session_id=identity, client='codex', cwd=str(self.root), name=title)
        return {'thread_id': identity, 'history': history(text, role), 'revision': revision,
                'metadata': {'title': title}}

    def activate(self, records):
        list(self.index.rebuild_steps(records))
        self.assertEqual(self.index.verify(records)['failures'], [])
        self.index.activate()

    def test_normal_search_unicode_identifiers_short_queries_role_and_scope(self):
        records = [self.record('a', 'Straße β API::get_user x%_ "quoted" 日本語'),
                   self.record('b', 'assistant-only API::get_user', role='assistant'),
                   self.record('c', 'something else', title='API::get_user discussion')]
        self.activate(records)
        for query in ('strasse', 'β', 'x%_', '"quoted"', '日本', 'get_user'):
            with self.subTest(query=query):
                self.assertIn('a', [r['thread_id'] for r in search_threads(self.store, query)['items']])
        self.assertEqual([r['thread_id'] for r in search_threads(self.store, 'get_user', my_messages=True)['items']], ['a'])
        self.assertEqual(search_threads(self.store, 'get_user', thread_ids=['c'], source='messages')['items'], [])
        self.assertEqual([r['thread_id'] for r in search_threads(self.store, 'get_user', source='context')['items']], ['c'])

    def test_active_search_never_requests_coordination_writer(self):
        record = self.record('a', 'needle')
        self.activate([record])
        with patch.object(self.store, 'database_operation', side_effect=AssertionError('writer admission')):
            self.assertEqual(search_threads(self.store, 'needle')['items'][0]['thread_id'], 'a')
        with self.store.reading(), self.store._connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'conversation_search_%'").fetchone()[0], 0)

    def test_revision_fencing_idempotency_restart_and_delete(self):
        record = self.record('a', 'new content', revision=2)
        self.activate([record])
        self.assertFalse(self.index.enqueue_history('a', history('old'), revision=1).result())
        self.assertTrue(self.index.enqueue_history('a', history('new content'), revision=2).result())
        reopened = SearchStore(self.index.path)
        try:
            self.assertTrue(reopened.active)
            self.assertEqual(reopened.verify([record])['failures'], [])
        finally:
            reopened.close()
        self.index.delete('a').result()
        self.assertEqual(search_threads(self.store, 'content')['items'], [])

    def test_failed_background_write_is_reported(self):
        self.index.enqueue_history('a', history('first'), revision=1).result()
        self.index.enqueue_history('a', history('different'), revision=1)
        with self.assertRaisesRegex(ValueError, 'revision reused'):
            self.index.flush()

    def test_resume_rebuild_verification_and_active_turn_exclusion(self):
        records = [self.record('a', 'first'), self.record('b', 'second')]
        next(self.index.rebuild_steps(records))
        self.assertEqual(self.index.verify(records)['failures'], ['b'])
        with self.assertRaises(ValueError):
            self.index.activate()
        self.activate(records)
        self.index.enqueue_history('a', history('streaming needle', status='inProgress'), revision=2).result()
        self.assertEqual(search_threads(self.store, 'needle')['items'], [])
        self.assertEqual(self.index.coverage()['messages'], 1)

    def test_metadata_changes_and_empty_history_context(self):
        record = self.record('a', 'saved prose')
        self.activate([record])
        self.index.enqueue_metadata('a', {'title': 'New title', 'domain': 'database'}).result()
        self.assertEqual(search_threads(self.store, 'new title', source='context')['items'][0]['thread_id'], 'a')
        self.assertEqual(search_threads(self.store, 'unrelated', source='context')['items'], [])
        self.assertEqual(search_threads(self.store, 'database', source='context')['items'][0]['thread_id'], 'a')

    def test_external_and_legacy_results_match(self):
        records = [self.record('a', 'database lock database writer'),
                   self.record('b', 'database writer'), self.record('c', 'writer delayed database')]
        with self.store._connection() as db:
            db.execute('CREATE TABLE browser_history(thread_id TEXT PRIMARY KEY,history_json TEXT NOT NULL)')
            for record in records:
                db.execute('INSERT INTO browser_history VALUES (?,?)',
                           (record['thread_id'], json.dumps(record['history'])))
        legacy = search_threads(self.store, 'database writer')
        self.activate(records)
        external = search_threads(self.store, 'database writer')
        # Numeric scores intentionally use native BM25 after activation.
        def semantic_results(page):
            return {row['thread_id']: (row['message_count'], {match['item_id'] for match in row['matches']})
                    for row in page['items']}
        self.assertEqual(semantic_results(external), semantic_results(legacy))
        self.assertEqual(external['coverage'], legacy['coverage'])

    def test_native_ranking_pagination_and_no_python_measurement(self):
        self.activate([self.record('a', 'database writer'),
                       self.record('b', 'writer delayed database'),
                       self.record('c', 'database writer extra material')])
        with patch('agent_coord.thread_search._relevance', side_effect=AssertionError('Python proximity')):
            page = search_threads(self.store, 'database writer', limit=1)
            identities = [page['items'][0]['thread_id']]
            while page['next_cursor']:
                page = search_threads(self.store, 'database writer', limit=1, cursor=page['next_cursor'])
                identities.extend(row['thread_id'] for row in page['items'])
        self.assertEqual(set(identities), {'a', 'b', 'c'})
        self.assertEqual(len(identities), 3)
        self.assertEqual(identities[0], 'a')

    def test_incremental_prefix_and_verification_invalidated_by_new_write(self):
        record = self.record('a', 'first')
        list(self.index.rebuild_steps([record]))
        self.assertEqual(self.index.verify([record])['failures'], [])
        with self.index.connection() as db:
            original = db.execute('SELECT id FROM search_turns').fetchone()[0]
        updated = history('first')
        updated['turns'].extend(history('second')['turns'])
        self.index.enqueue_history('a', updated, revision=2).result()
        with self.assertRaises(ValueError):
            self.index.activate()
        with self.index.connection() as db:
            self.assertEqual(db.execute('SELECT id FROM search_turns WHERE turn_seq=0').fetchone()[0], original)
        self.assertEqual(self.index.coverage()['messages'], 2)

    def test_index_writer_proceeds_while_state_transaction_is_open(self):
        record = self.record('a', 'first')
        self.activate([record])
        with self.store._connection() as db:
            db.execute("UPDATE work_threads SET title='Pending title' WHERE thread_id='a'")
            accepted = self.index.enqueue_history('a', history('independent writer'), revision=2).result(timeout=2)
            self.assertTrue(accepted)
        self.assertEqual(search_threads(self.store, 'independent')['items'][0]['thread_id'], 'a')

    def test_metadata_only_thread_and_browser_saved_scope(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from agent_coord.thread_search import search_browser_threads
        records = [self.record('archived', 'needle'), self.record('current', 'needle')]
        self.store.threads.update('archived', attention='archived')
        self.activate(records)
        sessions = SimpleNamespace(cwd=str(self.root), persistence=Mock())
        query = {'q': ['needle'], 'scope': ['current'], 'show': ['archived']}
        result = search_browser_threads(self.store, sessions, query)
        self.assertEqual([row['thread_id'] for row in result['items']], ['archived'])
        sessions.persistence.submit.assert_not_called()
        self.store.register(session_id='metadata', client='codex', cwd=str(self.root), name='Metadata needle')
        self.index.enqueue_metadata('metadata', {'title': 'Metadata needle'}).result()
        result = search_threads(self.store, 'needle', source='context')
        self.assertEqual([row['thread_id'] for row in result['items']], ['metadata'])
        self.assertEqual(result['coverage']['missing_histories'], 1)

    def test_reopened_reader_does_not_initialize_schema(self):
        from agent_coord.search_store import active_search
        self.activate([self.record('a', 'needle')])
        self.index.close()
        with patch('agent_coord.search_store._SCHEMA', 'INVALID SCHEMA'):
            reader = active_search(self.store)
            self.addCleanup(reader.close)
            self.assertTrue(reader.active)
            self.assertEqual(search_threads(self.store, 'needle')['items'][0]['thread_id'], 'a')

    def test_lone_surrogate_is_explicitly_rejected_without_replacing_saved_prose(self):
        self.activate([self.record('a', 'valid prose')])
        self.index.enqueue_history('a', history('invalid \ud800 text'), revision=2)
        with self.assertRaisesRegex(ValueError, 'unpaired Unicode surrogate'):
            self.index.flush()
        self.assertEqual(search_threads(self.store, 'valid')['items'][0]['thread_id'], 'a')
        from agent_coord.store import CoordinationError
        with self.assertRaisesRegex(CoordinationError, 'valid Unicode'):
            search_threads(self.store, '\ud800')

    def test_routing_snapshot_survives_removal_of_legacy_storage(self):
        from agent_coord.dispatch_snapshot import read_snapshot
        from agent_coord.conversation_search import routing_history
        record = self.record('a', 'first')
        record['history']['turns'] = [{'id': f'turn-{i}', 'items': [{'id': 'duplicate',
            'type': 'agentMessage', 'text': f'message {i}'}]} for i in range(205)]
        with self.store._connection() as db:
            db.execute('CREATE TABLE browser_sessions(thread_id TEXT PRIMARY KEY,cwd TEXT)')
            db.execute('INSERT INTO browser_sessions VALUES (?,?)', ('a',str(self.root)))
        self.activate([record])
        snapshot = read_snapshot(self.store.database_path, now=10)
        actual = snapshot['threads'][0]
        expected = routing_history(record['history'])
        self.assertEqual({key: actual[key] for key in expected}, expected)
        self.assertEqual(len(actual['messages']), 200)

    def test_native_exact_page_matches_complete_order_with_filters(self):
        records = [self.record(f'thread-{i:02}',
                   'database writer ' + ('extra text ' * (i % 7)),
                   title='Database analysis' if i % 3 else 'Unrelated title',
                   role='user' if i % 2 else 'assistant') for i in range(30)]
        self.activate(records)
        for filters in ({}, {'my_messages': True}, {'source': 'context'},
                        {'thread_ids': [r['thread_id'] for r in records[::3]]}):
            expected = search_threads(self.store, 'database', limit=50, **filters)
            actual, cursor = [], None
            while True:
                page = search_threads(self.store, 'database', limit=4, cursor=cursor, **filters)
                actual.extend((row['thread_id'], row['score'], row['message_count']) for row in page['items'])
                cursor = page['next_cursor']
                if cursor is None:
                    break
            self.assertEqual(actual, [(row['thread_id'], row['score'], row['message_count']) for row in expected['items']])

    def test_literal_nul_does_not_become_fts_syntax(self):
        self.activate([self.record('a', 'prefix\0needle'), self.record('b', 'prefix needle')])
        self.assertEqual([r['thread_id'] for r in search_threads(self.store, 'prefix\0needle')['items']], ['a'])


if __name__ == '__main__':
    unittest.main()
