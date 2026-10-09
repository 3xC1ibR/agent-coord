from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from test_codex_app_server import BrowserSessions, CoordinationStore, FakeCodex
from agent_coord.message_timeline import message_changes, message_cursor, message_timeline
from agent_coord.store import CoordinationError
from agent_coord.ui import make_ui_server
from agent_coord.app_control import AppControl


class MessageTimelineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = CoordinationStore(self.root / 'state.sqlite3')
        for session in ('sender', 'recipient'):
            self.store.register(session_id=session, client='codex', cwd=str(self.root), name=session.title())

    def send(self, **kwargs):
        return self.store.send_message(sender_session_id='sender', recipient_session_id='recipient',
                                       body='<script>message</script>', **kwargs)

    def active(self, session, turn):
        self.store.threads.start_turn(session, turn_id=turn)
        self.store.touch(session, turn_active=True)

    def test_read_only_feed_has_same_identity_on_both_sides_without_delivery(self):
        sent = self.send()
        cursor = message_cursor(self.store)
        outgoing = message_timeline(self.store, 'sender')[0]
        incoming = message_timeline(self.store, 'recipient')[0]
        self.assertEqual(outgoing['id'], incoming['id'])
        self.assertEqual(outgoing['id'], sent['id'])
        self.assertEqual(outgoing['direction'], 'sent')
        self.assertEqual(incoming['direction'], 'received')
        self.assertEqual(incoming['counterpart']['session_id'], 'sender')
        self.assertEqual(outgoing['status'], 'Queued for agent')
        self.assertIsNone(incoming['delivered_at'])
        self.assertIsNone(incoming['acknowledged_at'])
        self.assertFalse(incoming['started_turn'])
        self.assertEqual(message_cursor(self.store), cursor)
        self.assertEqual(self.store.inbox('recipient')[0]['id'], sent['id'])
        self.assertEqual(message_timeline(self.store, 'sender')[0]['status'], 'Delivered to agent')
        # UI fields do not change the public send/inbox contract.
        self.assertNotIn('sender_turn_id', sent)
        self.assertNotIn('counterpart', sent)

    def test_turn_attribution_is_captured_once_and_survives_reopen(self):
        self.active('sender', 'send-turn')
        self.send()
        self.active('sender', 'next-send-turn')
        self.active('recipient', 'receive-turn')
        self.store.inbox('recipient')
        self.active('recipient', 'next-receive-turn')
        self.store.inbox('recipient', include_delivered=True)
        reopened = CoordinationStore(self.store.database_path)
        self.assertEqual(message_timeline(reopened, 'sender')[0]['turn_id'], 'send-turn')
        self.assertEqual(message_timeline(reopened, 'recipient')[0]['turn_id'], 'receive-turn')
        self.assertFalse(message_timeline(reopened, 'recipient')[0]['started_turn'])

    def test_idle_sender_does_not_inherit_stale_turn(self):
        self.active('sender', 'old-turn')
        self.store.touch('sender', turn_active=False)
        self.send()
        self.assertIsNone(message_timeline(self.store, 'sender')[0]['turn_id'])

    def test_reply_requires_reverse_direction_and_matching_exchange(self):
        original = self.send(reply_required=True)
        self.store.send_message(sender_session_id='recipient', recipient_session_id='sender', body='Unrelated')
        self.assertEqual(message_timeline(self.store, 'sender')[0]['status'], 'Queued for agent')
        reply = self.store.send_message(sender_session_id='recipient', recipient_session_id='sender',
                                       body='Done', classification='informational', thread_id=original['thread_id'])
        result = message_timeline(self.store, 'sender')[0]
        self.assertEqual(result['status'], 'Reply received')
        self.assertEqual(result['reply_id'], reply['id'])

    def test_closure_suppression_remains_honest_after_exchange_reopens(self):
        original = self.send()
        self.send(classification='closure', thread_id=original['thread_id'])
        self.assertEqual(message_timeline(self.store, 'sender')[0]['status'], 'Exchange closed')
        self.send(thread_id=original['thread_id'])
        self.assertEqual(message_timeline(self.store, 'sender')[0]['status'], 'Superseded')

    def test_explicit_reply_answers_only_its_original_request_and_survives_restart(self):
        first = self.send()
        second = self.send(thread_id=first['thread_id'])
        reply = self.store.reply_message(sender_session_id='recipient', message_id=second['id'], body='Done with second')
        reopened = CoordinationStore(self.store.database_path)
        messages = message_timeline(reopened, 'sender')
        self.assertIsNone(messages[0]['reply_id'])
        self.assertEqual(messages[0]['status'], 'Queued for agent')
        self.assertEqual(messages[1]['reply_id'], reply['id'])
        self.assertEqual(messages[1]['status'], 'Reply received')
        self.assertEqual(messages[2]['in_reply_to'], second['id'])
        self.assertFalse(messages[2]['reply_required'])
        self.assertEqual(messages[2]['status'], 'Queued for agent')
        self.store.inbox('sender')
        self.assertEqual(message_timeline(reopened, 'sender')[2]['status'], 'Delivered to agent')

    def test_external_writes_and_delivery_emit_scoped_notifications(self):
        cursor = message_cursor(self.store)
        other = CoordinationStore(self.store.database_path)
        other.send_message(sender_session_id='sender', recipient_session_id='recipient', body='External writer')
        latest, events = message_changes(self.store, cursor, str(self.root))
        self.assertGreater(latest, cursor)
        self.assertEqual({event['params']['threadId'] for event in events}, {'sender', 'recipient'})
        self.assertEqual(message_changes(self.store, latest)[1], [])
        other.inbox('recipient')
        self.assertEqual(len(message_changes(self.store, latest)[1]), 2)
        self.assertEqual(message_changes(self.store, cursor, str(self.root.with_name(self.root.name + '-elsewhere')))[1], [])

    def test_unconfirmed_or_mismatched_creation_receipts_do_not_relabel_messages(self):
        message = self.send()
        control = AppControl(self.store)
        request = control.request('create', 'sender', dict(cwd=str(self.root), prompt='Create an agent'))
        control.claim(request['request_id'], 'test-runtime')
        control.identified(request['request_id'], 'recipient')
        for status, operation, sender, target, receipt in (
            ('queued', 'create', 'sender', 'recipient', {'message_id': message['id']}),
            ('running', 'create', 'sender', 'recipient', {'message_id': message['id']}),
            ('failed', 'create', 'sender', 'recipient', {'message_id': message['id']}),
            ('uncertain', 'create', 'sender', 'recipient', {'message_id': message['id']}),
            ('completed', 'settings', 'sender', 'recipient', {'message_id': message['id']}),
            ('completed', 'create', 'recipient', 'recipient', {'message_id': message['id']}),
            ('completed', 'create', 'sender', 'sender', {'message_id': message['id']}),
            ('completed', 'create', 'sender', 'recipient', {'message_id': message['id'] + 1}),
            ('completed', 'create', 'sender', 'recipient', None),
            ('completed', 'create', 'sender', 'recipient', []),
        ):
            with self.subTest(status=status, operation=operation, sender=sender, target=target, receipt=receipt):
                with self.store._connection() as db:
                    db.execute('UPDATE app_control_requests SET status=?, operation=?, sender_session_id=?, thread_id=?, result_json=?',
                               (status, operation, sender, target, json.dumps(receipt)))
                self.assertIsNone(message_timeline(self.store, 'sender')[0]['creation_request_id'])

    def test_workspace_boundary_and_unavailable_counterpart(self):
        self.store.register(session_id='elsewhere', client='claude', cwd=str(self.root.with_name(self.root.name + '-elsewhere')))
        self.store.send_message(sender_session_id='elsewhere', recipient_session_id='recipient', body='Cross workspace')
        row = message_timeline(self.store, 'recipient', str(self.root))[0]
        self.assertFalse(row['counterpart']['available'])
        with self.assertRaises(CoordinationError):
            message_timeline(self.store, 'elsewhere', str(self.root))

    def test_legacy_rows_migrate_without_invented_attribution(self):
        self.send()
        with self.store._connection() as db:
            db.execute('DROP TRIGGER message_inserted')
            db.execute('DROP TRIGGER message_updated')
            db.execute('DROP TABLE message_changes')
            for column in ('sender_turn_id', 'recipient_turn_id', 'wake_turn_id', 'suppressed_at'):
                db.execute('ALTER TABLE messages DROP COLUMN ' + column)
        reopened = CoordinationStore(self.store.database_path)
        row = message_timeline(reopened, 'recipient')[0]
        self.assertIsNone(row['turn_id'])
        self.assertFalse(row['started_turn'])
        self.assertIsNone(row['delivered_at'])

    def test_http_feed_is_read_only_and_serves_card_assets(self):
        sent = self.send(classification='informational')
        sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        server = make_ui_server(self.store, port=0, cwd=str(self.root), browser_sessions=sessions)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            base = 'http://127.0.0.1:' + str(server.server_address[1])
            with urllib.request.urlopen(base + '/api/browser/threads/recipient/messages') as response:
                row = json.load(response)['data'][0]
            self.assertEqual(row['id'], sent['id'])
            self.assertIsNone(row['delivered_at'])
            for asset in ('agent-messages.js', 'agent-messages.css'):
                with urllib.request.urlopen(base + '/' + asset) as response:
                    self.assertEqual(response.status, 200)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(2)
