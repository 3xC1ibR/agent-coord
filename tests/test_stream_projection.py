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
