from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins/agent-coord/scripts'))
from agent_coord.message_delivery import MessageDelivery, MAX_CHARS
from agent_coord.store import CoordinationStore, CoordinationError
from agent_coord.cli import _parser, run


class MessageDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = CoordinationStore(self.root / 'state.sqlite3')
        for identity in ('sender', 'recipient', 'outsider'):
            self.store.register(session_id=identity, client='codex', cwd=str(self.root))
        self.delivery = MessageDelivery(self.store)

    def send(self, body='A correction', **kwargs):
        return self.store.send_message(sender_session_id='sender', recipient_session_id='recipient',
                                       body=body, **kwargs)['id']

    def test_receipts_are_distinct_and_recover_across_restarts(self):
        ident = self.send()
        self.assertEqual(self.delivery.read('sender', ident)['delivery']['state'], 'queued')
        batch = self.delivery.prepare('recipient', 'turn1')
        self.assertIsNone(self.delivery.read('sender', ident)['delivered_at'])
        self.delivery.attempted('turn1', batch)
        self.assertIsNone(self.delivery.prepare('recipient', 'turn1'))
        self.assertEqual(self.delivery.read('sender', ident)['delivery']['state'], 'submitting')
        self.delivery.submitted('recipient', 'turn1', batch)
        self.assertEqual(self.delivery.read('sender', ident)['delivery']['state'], 'submitted')
        self.assertIsNone(self.delivery.read('sender', ident)['acknowledged_at'])
        reopened = MessageDelivery(CoordinationStore(self.store.database_path))
        self.assertIsNone(reopened.prepare('recipient', 'turn1'))
        self.assertEqual(reopened.prepare('recipient', 'turn2')['ids'], [ident])
        self.store.acknowledge('recipient', ident)
        self.assertEqual(reopened.read('sender', ident)['delivery']['state'], 'acknowledged')
        self.assertIsNone(reopened.prepare('recipient', 'turn2'))

    def test_reply_stops_redelivery_of_original_but_delivers_response(self):
        ident = self.send()
        batch = self.delivery.prepare('recipient', 'turn1')
        self.delivery.attempted('turn1', batch)
        self.delivery.submitted('recipient', 'turn1', batch)
        reply = self.store.reply_message(sender_session_id='recipient', message_id=ident, body='Answer')
        reopened = MessageDelivery(CoordinationStore(self.store.database_path))
        self.assertEqual(reopened.read('sender', ident)['delivery']['state'], 'acknowledged')
        self.assertIsNone(reopened.prepare('recipient', 'turn2'))
        self.assertEqual(reopened.prepare('sender', 'turn2')['ids'], [reply['id']])

    def test_batches_preserve_complete_bodies_and_leave_excess_queued(self):
        ids = [self.send('line\n' * 600 + str(n)) for n in range(12)]
        batch = self.delivery.prepare('recipient', 'turn')
        self.assertLess(len(batch['text']), MAX_CHARS + 700)
        self.assertTrue(all(batch['full']))
        self.assertLess(len(batch['ids']), len(ids))
        self.delivery.attempted('turn', batch)
        self.delivery.submitted('recipient', 'turn', batch)
        untouched = set(ids) - set(batch['ids'])
        for ident in untouched:
            self.assertIsNone(self.delivery.read('sender', ident)['delivered_at'])
        self.assertTrue(set(self.delivery.prepare('recipient', 'turn')['ids']).issubset(untouched))

    def test_oversized_message_is_paged_without_false_delivery(self):
        body = ('☃\\\n' * 10000).strip()
        ident = self.send(body)
        batch = self.delivery.prepare('recipient', 'turn')
        self.assertEqual(batch['full'], [False])
        self.assertIn(f'agent-coord message --message-id {ident}', batch['text'])
        self.delivery.attempted('turn', batch)
        self.delivery.submitted('recipient', 'turn', batch)
        self.assertEqual(self.delivery.read('sender', ident)['delivery']['state'], 'body_required')
        rebuilt, offset = '', 0
        while offset is not None:
            page = self.delivery.read('recipient', ident, offset=offset)
            self.assertLessEqual(len(page['body']), 8000)
            rebuilt += page['body']
            offset = page['next_offset']
        self.assertEqual(rebuilt, body)
        self.assertIsNone(page['delivered_at'])
        self.assertIsNone(page['acknowledged_at'])

    def test_uncertain_attempt_is_visible_and_can_retry_in_later_turn(self):
        ident = self.send()
        batch = self.delivery.prepare('recipient', 'turn')
        self.delivery.attempted('turn', batch)
        self.delivery.failed('turn', batch, 'Cancellation timed out')
        status = self.delivery.read('sender', ident)['delivery']
        self.assertEqual(status['state'], 'uncertain')
        self.assertIn('timed out', status['error'])
        self.assertGreaterEqual(status['pending_seconds'], 0)
        self.assertEqual(self.delivery.prepare('recipient', 'next')['ids'], [ident])

    def test_nonactionable_and_superseded_messages_do_not_interrupt(self):
        self.send(classification='informational')
        message = self.store.send_message(sender_session_id='sender', recipient_session_id='recipient', body='old')
        self.store.send_message(sender_session_id='sender', recipient_session_id='recipient', body='closed',
                                classification='closure', thread_id=message['thread_id'])
        self.assertIsNone(self.delivery.prepare('recipient', 'turn'))

    def test_message_cli_pages_and_checks_participants(self):
        ident = self.send('abcdef')
        arguments = _parser().parse_args(['--db', str(self.store.database_path), 'message',
            '--session-id', 'sender', '--message-id', str(ident), '--offset', '2', '--limit', '3'])
        self.assertEqual(run(arguments)['body'], 'cde')
        for kwargs in ({'offset': -1}, {'limit': 9000}):
            with self.assertRaises(CoordinationError):
                self.delivery.read('sender', ident, **kwargs)
        with self.assertRaises(CoordinationError):
            self.delivery.read('outsider', ident)
