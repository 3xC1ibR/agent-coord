from __future__ import annotations

import json
import math
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from test_codex_app_server import BrowserSessions, CoordinationStore, FakeCodex
from agent_coord.message_timeline import message_changes, message_cursor, message_timeline, message_sends
from agent_coord.sqlite_rows import NATIVE_JSON_REAL_ROUND_TRIP
from agent_coord.store import CoordinationError
from agent_coord.ui import make_ui_server
from agent_coord.app_control import AppControl
import agent_coord.message_timeline as timeline


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

    def test_batched_rows_preserve_complete_fields_types_and_exact_float_values(self):
        original = self.send()
        control = AppControl(self.store)
        request = control.request('create', 'sender', dict(cwd=str(self.root), prompt='Create an agent'))
        control.claim(request['request_id'], 'test-runtime')
        control.identified(request['request_id'], 'recipient')
        count = timeline.TIMELINE_BATCH_ROWS * 2 + 3
        values = (None, 2 ** 62 + 1, math.nextafter(1.0, math.inf), 'text\x00雪', b'\x00\xff')
        stamp = math.nextafter(1793753480.570122, math.inf)
        with self.store._connection() as db:
            db.execute('ALTER TABLE messages ADD COLUMN extension')
            db.execute("UPDATE app_control_requests SET status='completed', result_json=? WHERE request_id=?",
                       (json.dumps({'message_id': original['id']}), request['request_id']))
            db.executemany('''INSERT INTO messages
                (sender_session_id, recipient_session_id, body, created_at, delivered_at,
                 acknowledged_at, classification, thread_id, reply_required, in_reply_to,
                 sender_turn_id, recipient_turn_id, wake_turn_id, suppressed_at, context_lifecycle_json, extension)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''', [
                ('sender' if i % 2 else 'recipient', 'recipient' if i % 2 else 'sender',
                 f'Complete body {i}:\n' + '雪\\\x00' * 400, stamp,
                 stamp if i % 3 == 0 else None, stamp if i % 7 == 0 else None,
                 'closure' if i % 11 == 0 else 'action_required', original['thread_id'], i % 2,
                 original['id'] if i % 3 == 0 else None, 'sender-turn', 'recipient-turn',
                 'wake-turn' if i % 4 == 0 else None, stamp if i % 13 == 0 else None,
                 '{"complete":true}', values[i % len(values)]) for i in range(count)])

        def ordinary_rows(db, session_id):
            return [dict(row) for row in db.execute(timeline._TIMELINE_SELECT + ' ORDER BY m.id',
                                                  (session_id, session_id))]

        with patch.object(timeline, '_timeline_rows', ordinary_rows):
            expected = message_timeline(self.store, 'sender')
        transferred = []
        connect = self.store._connect

        def observed_connection():
            db = connect()
            def row_factory(cursor, row):
                transferred.append(1)
                return sqlite3.Row(cursor, row)
            db.row_factory = row_factory
            return db

        with patch.object(self.store, '_connect', observed_connection):
            actual = message_timeline(self.store, 'sender')
        self.assertEqual(actual, expected)
        self.assertEqual(actual[0]['creation_request_id'], request['request_id'])
        self.assertEqual(len(actual), count + 1)
        # The exact-float fallback transfers each message as an ordinary SQLite row.
        ordinary_rows_expected = 0 if NATIVE_JSON_REAL_ROUND_TRIP else len(actual)
        self.assertLess(len(transferred) - ordinary_rows_expected, 50, 'Excess timeline row transfers')
        for reference, row in zip(expected, actual):
            self.assertEqual(set(row), set(reference))
            for key, value in reference.items():
                self.assertIs(type(row[key]), type(value), key)
                if isinstance(value, float):
                    self.assertEqual(row[key].hex(), value.hex(), key)

    def test_timeline_batches_keep_one_snapshot_during_concurrent_updates(self):
        with self.store._connection() as db:
            db.executemany('''INSERT INTO messages
                (sender_session_id, recipient_session_id, body, created_at, thread_id)
                VALUES ('sender', 'recipient', ?, 1.0, 'snapshot')''',
                [(f'Original {i}',) for i in range(timeline.TIMELINE_BATCH_ROWS + 1)])
        expected = message_timeline(self.store, 'sender')
        read_rows = timeline.read_rows
        batches = 0

        def change():
            with self.store._connection() as db:
                db.execute("UPDATE messages SET body='Changed' WHERE id=(SELECT MAX(id) FROM messages)")
                db.execute("UPDATE work_threads SET title='Changed title' WHERE thread_id='recipient'")

        def concurrent_read(db, query, parameters=(), **options):
            nonlocal batches
            rows = read_rows(db, query, parameters, **options)
            batches += 1
            if batches == 1:
                with ThreadPoolExecutor(max_workers=1) as workers:
                    workers.submit(change).result(timeout=2)
            return rows

        with patch.object(timeline, 'read_rows', concurrent_read):
            self.assertEqual(message_timeline(self.store, 'sender'), expected)
        self.assertGreater(batches, 1)
        refreshed = message_timeline(self.store, 'sender')
        self.assertEqual(refreshed[-1]['body'], 'Changed')
        self.assertTrue(all(row['counterpart']['title'] == 'Changed title' for row in refreshed))

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

    def test_live_sends_skip_history_and_status_changes_and_preserve_direction(self):
        self.send()
        cursor, events = message_sends(self.store)
        self.assertEqual(events, [])
        other = CoordinationStore(self.store.database_path)
        first = other.send_message(sender_session_id='sender', recipient_session_id='recipient', body='Private body')
        second = other.send_message(sender_session_id='recipient', recipient_session_id='sender', body='Reply')
        latest, events = message_sends(self.store, cursor, str(self.root))
        self.assertEqual(events, [
            {'method': 'coordination/messageSent', 'params': {
                'messageId': first['id'], 'threadId': 'sender', 'recipientId': 'recipient'}},
            {'method': 'coordination/messageSent', 'params': {
                'messageId': second['id'], 'threadId': 'recipient', 'recipientId': 'sender'}}])
        other.inbox('recipient')
        other.acknowledge('recipient', message_id=first['id'])
        self.assertEqual(message_sends(self.store, latest), (latest, []))
        self.assertEqual(message_sends(self.store, cursor, str(self.root.with_name(self.root.name + '-elsewhere')))[1], [])

    def test_live_sends_do_not_expose_cross_workspace_endpoints(self):
        cursor, _ = message_sends(self.store)
        self.store.register(session_id='elsewhere', client='claude', cwd=str(self.root.with_name(self.root.name + '-elsewhere')))
        self.store.send_message(sender_session_id='elsewhere', recipient_session_id='recipient', body='Incoming')
        self.store.send_message(sender_session_id='sender', recipient_session_id='elsewhere', body='Outgoing')
        latest, events = message_sends(self.store, cursor, str(self.root))
        self.assertGreater(latest, cursor)
        self.assertEqual(events, [])

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
            db.execute('DROP INDEX messages_wake_turn_idx')  # Added with wake_turn_id's first use in lists.
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
