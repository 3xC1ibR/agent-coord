"""Production projection budgets for resident streaming conversations."""
import sqlite3
import unittest
from unittest.mock import patch

import test_coordinator_projection as fixtures


class StreamProjectionTests(unittest.TestCase):
    setUp = fixtures.ProjectionTests.setUp
    event = fixtures.ProjectionTests.event

    def test_canonical_reasoning_and_tool_deltas_preserve_history(self):
        for item, method, extra, field in (
            ({'id': 'reasoning', 'type': 'reasoning', 'summary': ['']},
             'item/reasoning/summaryTextDelta', {'summaryIndex': 0}, 'summary'),
            ({'id': 'tool', 'type': 'mcpToolCall', 'visibleOutput': ''},
             'item/mcpToolCall/outputDelta', {}, 'visibleOutput')):
            self.sessions._event({'method':'item/started', 'params':{
                'threadId': self.identity, 'turnId':self.turn_id, 'item':item}})
            for text in ('first', ' second'):
                self.sessions._event({'method':method, 'params':{
                    'threadId':self.identity, 'turnId':self.turn_id,
                    'itemId':item['id'], 'delta':text, **extra}})
            recorded = next(value for value in self.sessions.histories[self.identity]['turns'][-1]['items']
                            if value['id'] == item['id'])
            self.assertEqual(recorded[field], ['first second'] if field == 'summary' else 'first second')

    def test_native_callback_close_waits_for_commit_and_browser_unlock(self):
        closed = []
        original_call = self.client.call
        def call(action, **values):
            if action == 'command' and values.get('operation') == 'close':
                self.assertFalse(self.sessions.lock._is_owned())
                closed.append(values['command_id'])
            return original_call(action, **values)
        self.projection.channels['codex']['callback'] = lambda message: self.projection.close_channel('codex')
        event = self.event(1, 'native/failure')
        with patch.object(self.client, 'call', side_effect=call):
            with patch.object(self.projection, '_save', side_effect=sqlite3.OperationalError('failed commit')):
                with self.assertRaisesRegex(sqlite3.OperationalError, 'failed commit'):
                    self.projection.project(event)
            self.assertEqual(closed, [])
            self.assertFalse(self.projection.channels['codex'].get('closed'))
            self.projection.project(event)
        self.assertEqual(len(closed), 1)
        self.assertTrue(self.projection.channels['codex']['closed'])

    def test_token_delta_does_not_snapshot_or_checkpoint_main_state(self):
        self.projection.project(self.event(1, 'item/started', item={
            'id': 'output', 'type': 'agentMessage', 'text': ''}))
        with patch.object(self.projection, 'state', side_effect=AssertionError('whole-state snapshot on delta')), \
                patch.object(self.projection, '_save', side_effect=AssertionError('main-state write on delta')):
            self.projection.project(self.event(2, 'item/agentMessage/delta', itemId='output', delta='once'))
        self.assertEqual(self.sessions.histories[self.identity]['turns'][-1]['items'][-1]['text'], 'once')


    def test_stream_journal_precedes_apply_and_never_holds_browser_lock(self):
        self.projection.project(self.event(1, 'item/started', item={
            'id': 'output', 'type': 'agentMessage', 'text': ''}))
        original = self.projection.stream_journal.append_live
        def append(identity, frame):
            self.assertFalse(self.sessions.lock._is_owned())
            self.assertEqual(self.projection.cursor, frame['sequence'] - 1)
            self.assertEqual(frame['source_instance'], self.client.record['instance'])
            self.assertEqual(frame['epoch'], self.client.epoch)
            self.assertEqual(frame['pid'], 123)
            return original(identity, frame)
        delta = self.event(2, 'item/agentMessage/delta', itemId='output', delta='once')
        with patch.object(self.projection.stream_journal, 'append_live', side_effect=OSError('fsync failed')):
            with self.assertRaises(OSError):
                self.projection.project(delta)
        self.assertEqual(self.projection.cursor, 1)
        with patch.object(self.projection.stream_journal, 'append_live', side_effect=append):
            self.projection.project(delta)
        frames = list(self.projection.stream_journal.replay_live(self.identity, after_seq=1))
        self.assertEqual([frame['sequence'] for frame in frames], [2])
        self.assertEqual(self.projection.checkpoint_cursor, 1)

    def test_stream_callbacks_do_no_database_work_under_browser_lock(self):
        self.projection.project(self.event(1, 'item/started', item={
            'id': 'output', 'type': 'agentMessage', 'text': ''}))
        connection = self.store._connection
        before = self.store._writer.snapshot()['completed_scopes']
        def checked_connection(*args, **kwargs):
            self.assertFalse(self.sessions.lock._is_owned(), 'SQLite under browser lock')
            return connection(*args, **kwargs)
        with patch.object(self.store, '_connection', side_effect=checked_connection), \
                patch('agent_coord.coordinator_projection.copy.deepcopy', side_effect=AssertionError('delta deepcopy')):
            for sequence in range(2, 102):
                self.projection.project(self.event(sequence, 'item/agentMessage/delta', itemId='output', delta='x'))
        self.assertEqual(self.store._writer.snapshot()['completed_scopes'], before)
        self.assertEqual(self.projection.stream_metrics['events'], 100)
        self.assertEqual(self.client.commands[-1][1]['retain_after'], 1)

    def test_lost_acknowledgement_retries_without_reapplying_delta(self):
        self.projection.project(self.event(1, 'item/started', item={
            'id': 'output', 'type': 'agentMessage', 'text': ''}))
        delta = self.event(2, 'item/agentMessage/delta', itemId='output', delta='once')
        call = self.client.call
        attempts = []
        def lost_once(action, **values):
            if action == 'acknowledge_events':
                attempts.append(values)
                if len(attempts) == 1:
                    raise OSError('lost response')
            return call(action, **values)
        with patch.object(self.client, 'call', side_effect=lost_once):
            with self.assertRaises(OSError):
                self.projection.project(delta)
            self.projection.project(delta)
        self.assertEqual(attempts[0], attempts[1])
        self.assertEqual(self.sessions.histories[self.identity]['turns'][-1]['items'][-1]['text'], 'once')

    def test_channel_failure_undoes_its_item_and_preserves_healthy_progress(self):
        self.projection.project(self.event(1, 'item/started', item={
            'id': 'output', 'type': 'agentMessage', 'text': ''}))
        def failing(message):
            self.sessions._event(message)
            raise ValueError('bad native callback')
        self.projection.attach('bad', 124, failing, provider='codex')
        bad = {**self.event(2, 'item/agentMessage/delta', itemId='output', delta='discard'), 'channel': 'bad'}
        healthy = self.event(3, 'item/agentMessage/delta', itemId='output', delta='retained')
        self.projection.project_batch([bad, healthy])
        self.assertEqual(self.projection.cursor, 3)
        self.assertEqual(self.sessions.histories[self.identity]['turns'][-1]['items'][-1]['text'], 'retained')
        self.assertEqual(self.projection.quarantined['bad']['first_sequence'], 2)
        self.projection.flush_stream()
        self.projection._acknowledge()
        self.assertEqual(self.client.commands[-1][1]['retain_after'], 1)
        self.projection.load()
        self.assertEqual(self.projection.quarantined['bad']['first_sequence'], 2)

    def test_validated_delta_does_not_enter_raw_diagnostic_or_authorization_handlers(self):
        self.projection.project(self.event(1, 'item/started', item={
            'id': 'output', 'type': 'agentMessage', 'text': ''}))
        self.projection.channels['codex']['stdout_callback'] = lambda raw: self.fail('Raw handler under delta lock')
        self.projection.project(self.event(2, 'item/agentMessage/delta', itemId='output', delta='https://example.test/oauth'))
        self.assertEqual(self.projection.stream_metrics['events'], 1)

    def test_native_claude_token_updates_mirror_without_sqlite_copies_or_noop_bookkeeping(self):
        item = {'id': 'output', 'type': 'agentMessage', 'text': ''}
        self.projection.project(self.event(1, 'item/started', item=item))
        self.sessions.claude.turns[self.identity] = {'id': self.turn_id, 'status': 'inProgress', 'items': [item]}
        self.sessions.claude.blocks[self.identity] = {0: item}
        self.projection.attach('claude', 123, lambda message: self.sessions.claude._message(self.identity, message),
                               provider='claude', thread_id=self.identity)
        import json
        native = {'sequence': 2, 'channel': 'claude', 'stream': 'stdout', 'data': json.dumps({
            'type': 'stream_event', 'event': {'type': 'content_block_delta', 'index': 0,
                'delta': {'type': 'text_delta', 'text': 'once'}}})}
        database = self.store._connection
        def checked(*args, **kwargs):
            self.assertFalse(self.sessions.lock._is_owned())
            return database(*args, **kwargs)
        with patch.object(self.store, '_connection', side_effect=checked), \
                patch('agent_coord.coordinator_projection.copy.deepcopy', side_effect=AssertionError('native Claude copy')):
            self.projection.project(native)
        self.assertEqual(item['text'], 'once')
        self.assertEqual(self.sessions.histories[self.identity]['turns'][-1]['items'][-1]['text'], 'once')
        self.assertEqual(self.projection.stream_writes, [])

    def test_idle_checkpoint_cannot_rewind_a_concurrently_advanced_cursor(self):
        self.projection.project(self.event(1, 'item/started', item={
            'id': 'output', 'type': 'agentMessage', 'text': ''}))
        self.projection.project(self.event(2, 'item/agentMessage/delta', itemId='output', delta='first'))
        commit = self.projection._commit_through_reply
        def advanced(decoded, **options):
            self.projection.project(self.event(3, 'item/agentMessage/delta', itemId='output', delta=' second'))
            return commit(decoded, **options)
        with patch.object(self.projection, '_commit_through_reply', side_effect=advanced):
            self.projection.flush_stream()
        self.assertEqual(self.projection.cursor, 3)
        self.assertEqual(self.projection.checkpoint_cursor, 3)
        self.assertEqual(self.sessions.histories[self.identity]['turns'][-1]['items'][-1]['text'], 'first second')
