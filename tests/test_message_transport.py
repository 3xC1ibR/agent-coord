"""Runtime receipt, race, retry and batching guarantees without model bookkeeping."""
import json
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins/agent-coord/scripts'))
from agent_coord.store import CoordinationStore, CoordinationError
from agent_coord.message_delivery import MessageDelivery, MAX_CHARS, MAX_MESSAGES
from agent_coord.cli import _parser, run


class MessageTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = CoordinationStore(Path(self.temp.name) / 'state.sqlite3')
        for name in ('sender', 'recipient'):
            self.store.register(session_id=name, client='codex', cwd=self.temp.name)
        self.delivery = MessageDelivery(self.store)

    def send(self, body='Rerun the tests', **kwargs):
        return self.store.send_message(sender_session_id='sender', recipient_session_id='recipient', body=body, **kwargs)

    def test_send_retry_survives_lost_response_restart_and_concurrency(self):
        original = self.send(request_id='request-1')
        reopened = CoordinationStore(self.store.database_path)
        def retry(_):
            return reopened.send_message(sender_session_id='sender', recipient_session_id='recipient',
                                         body='Rerun the tests', request_id='request-1')
        with ThreadPoolExecutor(max_workers=6) as workers:
            results = list(workers.map(retry, range(12)))
        self.assertEqual({r['id'] for r in results}, {original['id']})
        self.assertTrue(all(r['idempotent'] for r in results))
        self.assertEqual(len(self.store.inbox('recipient', include_delivered=True, mark_delivered=False)), 1)
        with self.assertRaisesRegex(CoordinationError, 'different message arguments'):
            self.send('Changed instruction', request_id='request-1')
        self.assertNotEqual(self.send(request_id='request-2')['id'], original['id'])
        self.assertNotEqual(self.send()['id'], original['id'])

    def test_reply_retry_preserves_one_answer_and_original_obligation(self):
        original = self.send(reply_required=True)
        batch = self.delivery.prepare('recipient', 'turn')
        self.delivery.attempted('turn', batch)
        self.delivery.submitted('recipient', 'turn', batch)
        from agent_coord.message_timeline import message_timeline
        self.assertNotEqual(message_timeline(self.store, 'sender')[0]['status'], 'Reply received')
        arguments = dict(sender_session_id='recipient', message_id=original['id'], body='Tests passed.', request_id='answer-1')
        reply = self.store.reply_message(**arguments)
        self.assertEqual(self.store.reply_message(**arguments)['id'], reply['id'])
        self.assertFalse(reply['reply_required'])
        self.assertEqual(message_timeline(self.store, 'sender')[0]['status'], 'Reply received')
        self.assertIsNone(self.store.inbox('recipient', include_delivered=True, mark_delivered=False)[0]['acknowledged_at'])
        with self.assertRaisesRegex(CoordinationError, 'different message arguments'):
            self.store.reply_message(**{**arguments, 'body': 'Different answer'})

    def test_reservation_is_atomic_and_uncertain_delivery_is_not_replayed(self):
        message = self.send()
        a, b = self.delivery.prepare('recipient', 'a'), self.delivery.prepare('recipient', 'b')
        self.delivery.attempted('a', a)
        with self.assertRaises(CoordinationError):
            self.delivery.attempted('b', b)
        self.delivery.failed('a', a, 'Lost provider connection')
        reopened = MessageDelivery(CoordinationStore(self.store.database_path))
        self.assertIsNone(reopened.prepare('recipient', 'new-turn'))
        self.assertEqual(reopened.read('sender', message['id'])['delivery']['state'], 'uncertain')
        self.assertEqual(reopened.metrics('recipient')['counts']['uncertain'], 1)
        # Explicit retrieval is the recovery path, rather than speculative execution.
        self.assertEqual(reopened.read('recipient', message['id'], consume=True)['delivery']['state'], 'delivered')

    def test_proven_pre_submission_rejection_releases_for_retry(self):
        self.send()
        batch = self.delivery.prepare('recipient', 'a')
        self.delivery.attempted('a', batch)
        self.delivery.release('a')
        self.assertEqual(self.delivery.prepare('recipient', 'b')['ids'], batch['ids'])

    def test_hook_confirmation_requires_completion_of_the_receiving_turn(self):
        message = self.send()
        text = self.delivery.hook_context('recipient', 'turn-1', boundary_id='tool-1')
        self.assertIn(message['body'], text)
        self.assertNotIn('agent-coord ack', text)
        self.delivery.hook_context('recipient', 'turn-1', boundary_id='parallel-tool')
        self.assertEqual(self.delivery.read('sender', message['id'])['delivery']['state'], 'unconfirmed')
        self.delivery.finish_turn('recipient', 'turn-other', completed=True)
        self.assertEqual(self.delivery.read('sender', message['id'])['delivery']['state'], 'unconfirmed')
        self.delivery.finish_turn('recipient', 'turn-1', completed=True)
        receipt = self.delivery.read('sender', message['id'])
        self.assertEqual(receipt['delivery']['state'], 'delivered')
        self.assertEqual(receipt['delivery']['evidence'], 'turn_completed')
        self.assertIsNone(self.delivery.prepare('recipient', 'next'))

    def test_hook_failure_and_success_have_distinct_receipts(self):
        message = self.send()
        self.delivery.hook_context('recipient', 'turn')
        self.delivery.finish_turn('recipient', 'turn', completed=False)
        self.assertEqual(self.delivery.read('sender', message['id'])['delivery']['state'], 'uncertain')
        second = self.send('New instruction')
        self.delivery.hook_context('recipient', 'next')
        self.delivery.finish_turn('recipient', 'next', completed=True)
        self.assertEqual(self.delivery.read('sender', second['id'])['delivery']['state'], 'delivered')

    def test_bounded_batches_drain_in_order_without_ack_or_polling(self):
        ids = [self.send(('line\n' * 700) + str(i))['id'] for i in range(23)]
        delivered = []
        for turn in range(30):
            batch = self.delivery.prepare('recipient', str(turn))
            if batch is None:
                break
            self.assertLessEqual(len(batch['text']), MAX_CHARS)
            self.assertLessEqual(len(batch['ids']), MAX_MESSAGES)
            self.delivery.attempted(str(turn), batch)
            self.delivery.submitted('recipient', str(turn), batch)
            delivered.extend(batch['ids'])
        self.assertEqual(delivered, ids)
        self.assertEqual(self.store.inbox('recipient', unread_only=True), [])
        self.assertEqual(self.delivery.metrics('recipient')['counts']['delivered'], 23)

    def test_page_gaps_and_sender_inspection_cannot_confirm_full_delivery(self):
        message = self.send('x' * 20000)
        batch = self.delivery.prepare('recipient', 'turn')
        self.delivery.attempted('turn', batch)
        self.delivery.submitted('recipient', 'turn', batch)
        for offset in (0, 8000, 16000):
            self.delivery.read('sender', message['id'], offset=offset)
        for offset in (16000, 0):
            result = self.delivery.read('recipient', message['id'], offset=offset, consume=True)
            self.assertEqual(result['delivery']['state'], 'body_required')
        result = self.delivery.read('recipient', message['id'], offset=8000, consume=True)
        self.assertEqual(result['delivery']['state'], 'delivered')
        self.assertIsNone(result['acknowledged_at'])

    def test_legacy_ack_command_is_noop_and_cannot_discard_pending_work(self):
        message = self.send()
        args = _parser().parse_args(['--db', str(self.store.database_path), 'ack', '--session-id', 'recipient', '--all-unread'])
        self.assertEqual(run(args)['status'], 'obsolete')
        self.assertEqual(self.delivery.prepare('recipient', 'turn')['ids'], [message['id']])

    def test_cli_request_id_and_metrics(self):
        args = _parser().parse_args(['--db', str(self.store.database_path), 'send', '--from-session', 'sender',
            '--session', 'recipient', '--no-reply-required', '--request-id', 'cli-send', 'Do the work'])
        self.assertEqual(run(args)['id'], run(args)['id'])
        metrics = _parser().parse_args(['--db', str(self.store.database_path), 'message-metrics', '--session-id', 'recipient'])
        self.assertEqual(run(metrics)['counts']['queued'], 1)
