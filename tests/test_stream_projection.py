"""Production projection budgets for resident streaming conversations."""
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
