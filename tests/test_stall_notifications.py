"""Regression tests for durable stall incidents, independent of provider processes."""
import json
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins/agent-coord/scripts'))
from agent_coord.store import CoordinationStore
from agent_coord import supervision, stall_notifications as stalls


class StallNotificationsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.now = 10000.0
        self.progress = self.now
        self.store = CoordinationStore(self.root / 'coord.sqlite3', clock=lambda: self.now)
        for identity in ('child', 'parent'):
            self.store.register(session_id=identity, client='codex', cwd=str(self.root), name=identity)
        self.store.threads.start_turn('child', turn_id='turn', prompt='Gallery work')
        with self.store._connection() as db:
            supervision.ensure_schema(db)
            db.execute("INSERT INTO agent_supervisors (thread_id, supervisor_session_id, transfer_id, updated_at) VALUES ('child', 'parent', 'test', ?)", (self.now,))

    def at(self, age):
        self.now = self.progress + age
        with self.store._connection() as db:
            db.execute("UPDATE sessions SET last_seen_at=? WHERE session_id='parent'", (self.now,))

    def record(self, **kwargs):
        return stalls.record_stall(self.store, 'child', 'turn', progress_at=kwargs.pop('progress_at', self.progress),
                                  detail='No confirmed progress', classification=kwargs.pop('classification', 'stalled'), **kwargs)

    def alerts(self):
        with self.store.reading(), self.store._connection() as db:
            return [dict(r) for r in db.execute('SELECT * FROM app_agent_stall_alerts ORDER BY id')]

    def test_repeat_interval_is_capped_without_limiting_alert_count(self):
        self.at(599)
        self.assertIsNone(self.record())
        for sequence, age in enumerate((600, 1200, 2400, 4800, 7200, 9600), 1):
            self.at(age)
            event = self.record()
            self.assertEqual(event['sequence'], sequence)
            self.assertIsNone(self.record())
            self.at(age + 1)
            self.assertIsNone(self.record())
        self.assertEqual(len(self.alerts()), 6)

    def test_restart_retains_epoch_receipts_and_avoids_catchup_burst(self):
        self.at(600)
        first = self.record()
        self.store = CoordinationStore(self.store.database_path, clock=lambda: self.now)
        self.assertEqual(stalls.snapshot(self.store, 'child', 'turn')['progress_at'], self.progress)
        self.assertIsNone(self.record(progress_at=None))
        self.at(9000)
        second = self.record(progress_at=None)
        self.assertEqual(second['sequence'], 2)
        self.assertNotEqual(first['id'], second['id'])
        self.assertIsNone(self.record(progress_at=None))
        self.assertGreater(stalls.snapshot(self.store, 'child')['next_alert_at'], self.now)

    def test_progress_resets_and_rejects_stale_observations(self):
        self.at(600)
        self.record()
        self.at(650)
        stalls.record_progress(self.store, 'child', 'turn', progress_at=self.now)
        self.assertIsNone(stalls.snapshot(self.store, 'child'))
        self.assertIsNone(self.record())
        self.assertEqual(stalls.notifications(self.store, include_claimed=True), [])
        self.at(1250)
        restarted = self.record(progress_at=self.progress + 650)
        self.assertEqual(restarted['sequence'], 1)
        self.assertEqual(len(self.alerts()), 2)

    def test_finish_stop_new_turn_and_explicit_clear_close_incident(self):
        for resolution in ('finish', 'stop', 'new_turn', 'clear'):
            with self.subTest(resolution=resolution):
                turn = resolution
                self.store.threads.start_turn('child', turn_id=turn)
                event = stalls.record_stall(self.store, 'child', turn, progress_at=self.now - 600,
                                           detail='Silent', classification='stalled')
                self.assertIsNotNone(event)
                if resolution == 'finish':
                    self.store.threads.finish_turn('child', turn_id=turn)
                elif resolution == 'stop':
                    self.store.threads.request_stop('child', turn)
                elif resolution == 'new_turn':
                    self.store.threads.start_turn('child', turn_id='replacement')
                else:
                    stalls.clear_stall(self.store, 'child', turn, reason='reconciled')
                self.assertIsNone(stalls.snapshot(self.store, 'child', turn))
                self.now += 4000
                self.assertIsNone(stalls.record_stall(self.store, 'child', turn, progress_at=self.now - 4600,
                                                    detail='Late observation', classification='stalled'))

    def test_unsupervised_alert_is_immediate_user_escalation_and_claim_is_durable(self):
        with self.store._connection() as db:
            db.execute("UPDATE agent_supervisors SET supervisor_kind='user' WHERE thread_id='child'")
        self.at(600)
        event = self.record()
        self.assertTrue(event['user_escalated'])
        self.assertIsNone(event['message_id'])
        notices = stalls.notifications(self.store)
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]['status'], 'stalled')
        self.assertEqual(notices[0]['thread_id'], 'child')
        self.assertIn('Review', notices[0]['body'])
        key = notices[0]['request_key']
        self.assertTrue(stalls.claim_notification(self.store, key))
        self.store = CoordinationStore(self.store.database_path, clock=lambda: self.now)
        self.assertFalse(stalls.claim_notification(self.store, key))
        self.assertEqual(stalls.notifications(self.store), [])
        self.assertEqual(len(stalls.notifications(self.store, include_claimed=True)), 1)

    def test_busy_supervisor_gets_actionable_message_then_bounded_user_escalation(self):
        with self.store._connection() as db:
            db.execute("UPDATE sessions SET turn_active=1 WHERE session_id='parent'")
        self.at(600)
        first = self.record()
        self.assertFalse(first['user_escalated'])
        message = self.store.inbox('parent', mark_delivered=False)[0]
        self.assertEqual(message['classification'], 'action_required')
        self.assertFalse(message['reply_required'])
        self.store.inbox('parent')
        self.at(1200)
        self.assertTrue(self.record()['user_escalated'])
        self.assertEqual(len(stalls.notifications(self.store)), 1)

    def test_absent_paused_or_closed_supervisor_escalates_to_user(self):
        for state in ('absent', 'paused', 'closed'):
            with self.subTest(state=state):
                turn = state
                self.store.threads.start_turn('child', turn_id=turn)
                with self.store._connection() as db:
                    db.execute("UPDATE sessions SET ended_at=?, last_seen_at=? WHERE session_id='parent'",
                               (self.now if state == 'absent' else None, self.now))
                    db.execute("UPDATE work_threads SET attention=? WHERE thread_id='parent'", ('archived' if state == 'closed' else 'now',))
                    if state == 'paused':
                        db.execute('CREATE TABLE IF NOT EXISTS browser_inbox_wake (thread_id TEXT PRIMARY KEY, paused INTEGER)')
                        db.execute("INSERT OR REPLACE INTO browser_inbox_wake VALUES ('parent', 1)")
                event = stalls.record_stall(self.store, 'child', turn, progress_at=self.now - 600,
                                           detail='Silent', classification='stalled')
                self.assertTrue(event['user_escalated'])

    def test_content_reports_actionable_count_oldest_age_and_no_prompt_replay(self):
        self.store.send_message(sender_session_id='parent', recipient_session_id='child', body='Secret instruction',
                                classification='action_required', reply_required=True)
        self.at(600)
        event = self.record()
        body = json.loads(self.store.inbox('parent', mark_delivered=False)[0]['body'])
        self.assertEqual(body['event'], 'app_agent_stalled')
        self.assertEqual(body['actionable_inbox_count'], 1)
        self.assertEqual(body['oldest_actionable_age_seconds'], 600)
        self.assertEqual(body['turn_id'], 'turn')
        self.assertIn('progress', body['guidance'])
        self.assertNotIn('Secret instruction', json.dumps(event))
        self.assertIn('child', body['url'])

    def test_tool_allowance_and_waiting_for_approval_classification(self):
        for classification in ('long_running_tool', 'unverified_tool', 'waiting_for_approval'):
            turn = classification
            self.store.threads.start_turn('child', turn_id=turn)
            self.at(600)
            args = dict(progress_at=self.progress, detail='Awaiting response', classification=classification)
            early = stalls.record_stall(self.store, 'child', turn, **args)
            if classification == 'waiting_for_approval':
                self.assertEqual(early['classification'], classification)
            else:
                self.assertIsNone(early)
                self.at(1200)
                self.assertEqual(stalls.record_stall(self.store, 'child', turn, **args)['classification'], classification)

    def test_concurrent_observers_commit_one_receipt(self):
        self.at(600)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.record(), range(4)))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(len(self.alerts()), 1)

    def test_live_projection_and_policy_preserve_unresolved_user_attention(self):
        from agent_coord import notification_policy
        self.at(1200)
        self.record()
        self.at(1800)
        self.record()
        thread = self.store.threads.get('child', history=False)
        self.assertEqual(thread['stall_incident']['alert_count'], 2)
        self.assertEqual(thread['actionable_inbox_count'], 0)
        thread.update(automatic_turn=True, creation_mode='orchestration', created_by={'thread_id': 'parent'},
                      notification_context={'origin': 'coordination', 'owner_thread_id': 'parent'})
        policy = notification_policy.decide(thread, owners={'parent'})
        self.assertTrue(policy['notify'])
        self.assertEqual(policy['owner_thread_id'], 'child')


    def test_legacy_once_only_receipt_does_not_suppress_new_schedule(self):
        with self.store._connection() as db:
            db.execute("CREATE TABLE browser_turn_health (thread_id TEXT, turn_id TEXT, recovery_at REAL, alerted_at REAL, detail TEXT, PRIMARY KEY(thread_id,turn_id))")
            db.execute("INSERT INTO browser_turn_health VALUES ('child', 'turn', 100, 200, 'Old alert')")
        self.at(600)
        event = self.record()
        self.assertEqual(event['next_alert_at'], self.now + 600)
        self.assertEqual(event['error'], event['detail'])
        with self.store.reading(), self.store._connection() as db:
            row = db.execute("SELECT * FROM browser_turn_health").fetchone()
            self.assertEqual(row['alerted_at'], 200)
            self.assertEqual(row['detail'], 'No confirmed progress')

    def test_supervisor_transfer_routes_repeat_without_reviving_former_supervisor(self):
        self.at(600)
        first = self.record()
        self.store.register(session_id='successor', client='codex', cwd=str(self.root))
        with self.store._connection() as db:
            db.execute("UPDATE agent_supervisors SET supervisor_session_id='successor' WHERE thread_id='child'")
            db.execute("UPDATE work_threads SET attention='archived' WHERE thread_id='parent'")
        self.at(1200)
        with self.store._connection() as db:
            db.execute("UPDATE sessions SET last_seen_at=? WHERE session_id='successor'", (self.now,))
        second = self.record()
        self.assertEqual(second['supervisor_id'], 'successor')
        self.assertEqual(len(self.store.inbox('successor', mark_delivered=False)), 1)
        self.assertEqual(self.store.threads.get('parent')['attention'], 'archived')
        self.assertEqual(self.store.inbox('parent', mark_delivered=False), [])
        self.assertNotEqual(first['message_id'], second['message_id'])

    def test_receipt_and_message_roll_back_together_on_write_failure(self):
        self.at(600)
        original = self.store._insert_message
        def fail_after_insert(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError('simulated interruption before receipt')
        with patch.object(self.store, '_insert_message', side_effect=fail_after_insert):
            with self.assertRaisesRegex(RuntimeError, 'simulated interruption'):
                self.record()
        self.assertEqual(self.alerts(), [])
        self.assertEqual(self.store.inbox('parent', mark_delivered=False), [])
        self.assertEqual(self.record()['sequence'], 1)
        self.assertEqual(len(self.alerts()), 1)

    def test_projection_rehydrates_distinctions_and_survives_read_receipts(self):
        for classification in ('stalled', 'long_running_tool', 'waiting_for_approval'):
            with self.subTest(classification=classification):
                turn = classification
                self.store.threads.start_turn('child', turn_id=turn)
                with self.store._connection() as db:
                    db.execute("UPDATE agent_supervisors SET supervisor_kind='user'")
                stalls.record_stall(self.store, 'child', turn, progress_at=self.now - 1200,
                                   detail='Pending work', classification=classification)
                thread = self.store.threads.get('child', history=False)
                thread.update(status='running', needs_attention=False, response_state='working')
                stalls.apply_attention(thread, now=self.now, waiting=classification == 'waiting_for_approval')
                self.assertTrue(thread['needs_attention'])
                self.assertFalse(thread['can_handle_response'])
                self.assertEqual(thread['attention_owner_thread_id'], 'child')
                if classification != 'stalled':
                    self.assertNotEqual(thread['status'], 'stalled')
                key = stalls.notifications(self.store)[0]['request_key']
                self.assertTrue(stalls.claim_notification(self.store, key))
                again = self.store.threads.get('child', history=False)
                stalls.apply_attention(again, now=self.now)
                self.assertTrue(again['needs_attention'])

    def test_inventory_is_stable_while_attention_reports_current_queue_age(self):
        self.store.send_message(sender_session_id='parent', recipient_session_id='child',
                                body='Progress check', classification='action_required', reply_required=False)
        self.at(600)
        self.record()
        thread = self.store.threads.get('child', history=False)
        self.now += 120
        again = self.store.threads.get('child', history=False)
        self.assertEqual(again, thread)
        stalls.apply_attention(again, now=self.now)
        self.assertIn('1 queued request, oldest 12 min', again['attention_action'])

    def test_inventory_stall_fields_keep_existing_fifteen_query_budget(self):
        self.at(600)
        self.record()
        for i in range(20):
            self.store.register(session_id=f'other-{i}', client='codex', cwd=str(self.root))
        queries = []
        original = self.store._connect
        def traced():
            connection = original()
            connection.set_trace_callback(queries.append)
            return connection
        with patch.object(self.store, '_connect', side_effect=traced):
            threads = self.store.threads.list()
        self.assertLessEqual(len(queries), 15)
        child = next(t for t in threads if t['thread_id'] == 'child')
        self.assertEqual(child['stall_incident'], self.store.threads.get('child')['stall_incident'])
        self.assertFalse(any(q.lstrip().startswith(('INSERT', 'UPDATE', 'DELETE')) for q in queries))


class StallPushTests(unittest.TestCase):
    def fixture(self, native=False):
        import test_web_push as web
        from agent_coord.native_push import NativePush
        from test_native_push import registration
        f = web.WebPushTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        if native:
            f.push = NativePush(f.store, f.sessions, f.remote, transport=f.transport, clock=lambda: f.now)
            self.addCleanup(f.push.close)
        f.push.subscribe(f.device, registration() if native else web.subscription())
        f.store.register(session_id='worker', client='codex', cwd=f.temp.name, name='Gallery')
        f.store.threads.start_turn('worker', turn_id='silent')
        f.now += 600
        return f

    def record(self, f, classification='stalled'):
        return stalls.record_stall(f.store, 'worker', 'silent', progress_at=1000,
                                  detail='PRIVATE tool arguments', classification=classification)

    def test_web_and_native_push_restart_deduplication_is_independent_of_desktop_claim(self):
        from agent_coord.web_push import WebPush
        from agent_coord.native_push import NativePush
        for native in (False, True):
            with self.subTest(native=native):
                f = self.fixture(native)
                first = self.record(f)
                key = 'stall:' + str(first['id'])
                self.assertTrue(stalls.claim_notification(f.store, key))
                f.push.tick()
                self.assertEqual(len(f.transport.sent), 1)
                notice = f.transport.sent[0]['payload']
                self.assertIn('progress', notice['title'])
                self.assertIn('Review', notice['body'])
                self.assertEqual(notice['url'], '/#worker')
                self.assertNotIn('PRIVATE', json.dumps(notice))
                factory = NativePush if native else WebPush
                restarted = factory(f.store, f.sessions, f.remote, transport=f.transport, clock=lambda: f.now)
                self.addCleanup(restarted.close)
                f.now += 2 * 86400
                restarted.tick()
                self.assertEqual(len(f.transport.sent), 1)
                self.record(f)
                restarted.tick()
                self.assertEqual(len(f.transport.sent), 2)

    def test_progress_cancels_failed_delivery_retry(self):
        f = self.fixture()
        self.record(f)
        f.transport.response = (503, '30')
        f.push.tick()
        self.assertEqual(len(f.transport.sent), 1)
        f.now += 10
        stalls.record_progress(f.store, 'worker', 'silent', progress_at=f.now)
        f.now += 31
        f.transport.response = (201, None)
        f.push.tick()
        self.assertEqual(len(f.transport.sent), 1)

    def test_claimed_original_approval_does_not_suppress_audited_reminders(self):
        f = self.fixture()
        thread, request_key = f.approval()
        self.assertTrue(f.sessions.claim_approval_notification(request_key))
        f.store.threads.start_turn(thread, turn_id='approval-turn')
        first = stalls.record_stall(f.store, thread, 'approval-turn', progress_at=1000,
                                   detail='Awaiting user decision', classification='waiting_for_approval')
        self.assertTrue(stalls.claim_notification(f.store, 'stall:' + str(first['id'])))
        f.push.tick()
        reminders = [item for item in f.transport.sent if 'still waiting' in item['payload']['title']]
        self.assertEqual(len(reminders), 1)
        f.now += 600
        second = stalls.record_stall(f.store, thread, 'approval-turn', progress_at=1000,
                                    detail='Awaiting user decision', classification='waiting_for_approval')
        self.assertNotEqual(first['id'], second['id'])
        f.push.tick()
        reminders = [item for item in f.transport.sent if 'still waiting' in item['payload']['title']]
        self.assertEqual(len(reminders), 2)

    def test_all_user_payload_classes_preserve_audited_queue_counts_on_web_and_native_push(self):
        expected = {
            'stalled': ('Agent needs progress review', 'No confirmed progress.'),
            'unverified_tool': ('Tool status unverified', 'Tool execution could not be verified.'),
            'long_running_tool': ('Tool still running', 'A tool is confirmed running.'),
            'waiting_for_approval': ('Approval still waiting', 'An approval remains unanswered.'),
        }
        for native in (False, True):
            for classification, (label, explanation) in expected.items():
                with self.subTest(native=native, classification=classification):
                    f = self.fixture(native)
                    f.store.register(session_id='sender', client='codex', cwd=f.temp.name)
                    for age in (0, 300):
                        f.now = 1600 + age
                        f.store.send_message(sender_session_id='sender', recipient_session_id='worker',
                                             body='PRIVATE queued instruction', classification='action_required',
                                             reply_required=False)
                    f.now = 2200
                    receipt = self.record(f, classification)
                    # Delivery after observation must not rewrite an occurrence's audit facts.
                    f.store.inbox('worker')
                    notice = stalls.notifications(f.store)[0]
                    self.assertEqual(notice['request_key'], 'stall:' + str(receipt['id']))
                    self.assertEqual(notice['label'], label)
                    self.assertEqual(notice['actionable_inbox_count'], 2)
                    self.assertEqual(notice['oldest_actionable_age_seconds'], 600)
                    self.assertIn(explanation, notice['body'])
                    self.assertIn('2 queued actionable requests; oldest 10 min', notice['body'])
                    self.assertNotIn('PRIVATE', json.dumps(notice))
                    f.push.tick()
                    self.assertEqual(len(f.transport.sent), 1)
                    payload = f.transport.sent[0]['payload']
                    self.assertTrue(payload['title'].endswith(label))
                    self.assertIn(explanation, payload['body'])
                    self.assertIn('2 queued actionable requests; oldest 10 min', payload['body'])
                    self.assertNotIn('PRIVATE', json.dumps(payload))

    def test_user_notification_reports_no_queued_work_and_respects_push_unsubscribe(self):
        for native in (False, True):
            with self.subTest(native=native):
                f = self.fixture(native)
                self.record(f)
                notice = stalls.notifications(f.store)[0]
                self.assertIn('0 queued actionable requests', notice['body'])
                self.assertNotIn('oldest', notice['body'])
                f.push.unsubscribe(f.device)
                f.push.tick()
                self.assertEqual(f.transport.sent, [])
