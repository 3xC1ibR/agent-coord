from __future__ import annotations

import copy
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from pathlib import Path

from test_codex_app_server import BrowserSessions, CoordinationStore, FakeCodex, CoordinationError
from agent_coord.reviews import ReviewRuntime
from agent_coord.thread_preview import thread_preview
from test_browser_providers import claude_factory
from test_kimi import FakeKimi, KimiRPC
from test_muse import FakeMuse, MuseRPC
from test_antigravity import FakeACP, AntigravityRPC


class ReviewTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.store = CoordinationStore(self.root / 'state.sqlite3')
        self.sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex,
            claude_factory=claude_factory,
            kimi_factory=lambda s, event: KimiRPC(s, event, connection_factory=FakeKimi),
            muse_factory=lambda s, event: MuseRPC(s, event, connection_factory=FakeMuse),
            antigravity_factory=lambda s, event: AntigravityRPC(s, event, connection_factory=FakeACP))
        self.addCleanup(self.sessions.close)
        self.owner = self.sessions.create({})['session']['thread_id']

    def start(self, **options):
        self.body = {'message': 'Plan an honest comparison; do not implement.', 'review': options, 'requestId': 'request-1'}
        return self.sessions.submit(self.owner, self.body)['review']

    def run_state(self):
        self.sessions.persistence.flush()
        return self.sessions.reviews.list(self.owner)[-1]

    def stage(self, result, status='completed'):
        self.sessions.inbox_wake.process_once()
        self.sessions.persistence.flush()
        run = self.run_state()
        step = run['steps'][-1]
        actor = step['actor_id']
        turn = self.sessions.rpc.threads[actor]['turns'][-1]
        self.assertEqual(step['turn_id'], turn['id'])
        self.assertIn('Review packet:', turn['items'][0]['content'][0]['text'])
        if isinstance(result, dict):
            result = {'run_id': run['id'], 'draft_hash': step['draft_hash'], **result}
            result = json.dumps(result)
        turn['items'].append({'id': 'answer', 'type': 'agentMessage', 'phase': 'final_answer', 'text': result})
        turn['status'] = status
        self.sessions._event({'method': 'turn/completed', 'params': {'threadId': actor, 'turn': copy.deepcopy(turn)}})
        self.sessions.persistence.flush()
        self.sessions.reviews.process_once()
        return self.run_state()

    def report(self, findings=None, **values):
        return {'outcome': 'findings' if findings else 'no_material_findings', 'coverage': 'Text reasoning checked; no external facts verified.', 'findings': findings or [], 'rechecks': [], **values}

    def finding(self):
        return {'id': 'F1', 'issue': 'A constraint is missing', 'evidence': 'The request requires an honest comparison', 'verification': 'checked', 'action': 'Include the comparison limitation'}

    def test_clean_review_completes_once_without_revision(self):
        run = self.start()
        self.assertEqual(run['phase'], 'draft')
        self.assertEqual(self.stage('A careful comparison.')['phase'], 'review')
        run = self.stage(self.report())
        self.assertEqual(run['state'], 'complete')
        self.assertEqual(len(run['steps']), 2)
        self.assertEqual(run['draft'], 'A careful comparison.')
        self.assertIsNotNone(run['completion_id'])
        for _ in range(3):
            self.sessions.reviews.process_once()
            self.sessions.inbox_wake.process_once()
        self.assertEqual(self.run_state(), run)

    def test_revision_rejection_and_targeted_recheck_are_bounded(self):
        self.start()
        self.stage('Draft one')
        self.stage(self.report([self.finding()]))
        run = self.stage({'draft': 'Draft two', 'dispositions': [{'id': 'F1', 'decision': 'reject', 'reason': 'The comparison was already in paragraph two.'}]})
        self.assertEqual(run['phase'], 'recheck')
        run = self.stage(self.report([self.finding()], rechecks=[{'id': 'F1', 'status': 'disputed', 'reason': 'The paragraph is insufficient.'}]))
        self.assertEqual(run['state'], 'complete')
        self.assertEqual(len(run['steps']), 4)
        self.assertNotEqual(run['steps'][1]['draft_hash'], run['steps'][3]['draft_hash'])

    def test_duplicate_submission_uses_same_run_and_rejects_changed_input(self):
        run = self.start()
        self.assertEqual(self.sessions.submit(self.owner, self.body)['review']['id'], run['id'])
        self.assertEqual(len(self.sessions.rpc.threads), 2)
        with self.assertRaises(CoordinationError):
            self.sessions.submit(self.owner, {**self.body, 'message': 'different'})

    def test_critic_model_default_does_not_inherit_incompatible_generator_effort(self):
        run = self.start(model='other-model', effort=None)
        self.assertEqual(run['state'], 'active')
        self.assertEqual(self.sessions._record(run['critic_id'])['model'], 'other-model')

    def test_creation_failure_has_a_result_receipt_before_any_provider_turn(self):
        with patch.object(self.sessions, 'create', side_effect=CoordinationError('Unavailable')):
            run = self.start()
        work = self.sessions.work_thread(self.owner)
        self.assertEqual(run['state'], 'incomplete')
        self.assertEqual(work['review']['state'], 'incomplete')
        self.assertEqual(work['turn_completion']['id'], run['completion_id'])

    def test_stale_report_does_not_release_draft(self):
        self.start(); self.stage('Draft')
        run = self.stage(self.report(draft_hash='stale'))
        self.assertEqual(run['state'], 'incomplete')
        self.assertIn('different request or draft', run['error'])

    def test_malformed_and_failed_turns_do_not_retry(self):
        self.start(); self.stage('Draft')
        run = self.stage('I agree')
        self.assertEqual(run['state'], 'incomplete')
        self.sessions.inbox_wake.process_once()
        self.assertEqual(len(self.run_state()['steps']), 2)

    def test_missing_finding_disposition_stops_revision(self):
        self.start(); self.stage('Draft'); self.stage(self.report([self.finding()]))
        run = self.stage({'draft': 'Revised', 'dispositions': []})
        self.assertEqual(run['state'], 'incomplete')

    def test_cancel_between_stages_suppresses_pending_work(self):
        self.start(); self.stage('Draft')
        self.sessions.interrupt(self.owner)
        self.sessions.inbox_wake.process_once()
        self.assertEqual(self.run_state()['state'], 'cancelled')
        self.assertEqual(self.sessions.rpc.threads[self.run_state()['critic_id']]['turns'], [])

    def test_new_input_interrupts_critic_and_supersedes_review(self):
        self.start(); self.stage('Draft')
        self.sessions.inbox_wake.process_once(); self.sessions.persistence.flush()
        self.sessions.send(self.owner, {'message': 'Updated task'})
        run = self.run_state()
        self.assertEqual(run['state'], 'superseded')
        critic_turn = self.sessions.rpc.threads[run['critic_id']]['turns'][-1]
        self.assertEqual(critic_turn['status'], 'interrupted')

    def test_timeout_is_durable_and_does_not_restart(self):
        run = self.start()
        with self.store._connection() as db:
            db.execute('UPDATE review_runs SET deadline=0 WHERE id=?', (run['id'],))
        ReviewRuntime(self.sessions).process_once()
        self.assertEqual(self.run_state()['state'], 'incomplete')
        self.sessions.inbox_wake.process_once()
        self.assertEqual(self.sessions.rpc.threads[self.owner]['turns'], [])

    def test_restart_after_draft_completion_resumes_exact_stage_once(self):
        self.start(); self.stage('Draft')
        replacement = ReviewRuntime(self.sessions)
        replacement.process_once(); replacement.process_once()
        self.assertEqual(len(self.run_state()['steps']), 2)
        run = self.stage(self.report())
        self.assertEqual(run['state'], 'complete')

    def test_packet_preserves_request_constraints_and_ancestor_instructions(self):
        (self.root / 'AGENTS.md').write_text('Do not invent evidence.')
        self.sessions.send(self.owner, {'message': 'Always describe uncertainty.'})
        turn = self.sessions.rpc.threads[self.owner]['turns'][-1]
        turn['status'] = 'completed'
        self.sessions._event({'method': 'turn/completed', 'params': {'threadId': self.owner, 'turn': copy.deepcopy(turn)}})
        self.sessions.persistence.flush()
        packet = self.start()['packet']
        self.assertEqual(packet['original_request'], self.body['message'])
        self.assertIn('Always describe uncertainty.', packet['prior_user_messages'])
        self.assertEqual(packet['repository_instructions'][-1]['text'], 'Do not invent evidence.')

    def test_draft_notifications_suppressed_and_final_receipt_allowed(self):
        self.start(); self.stage('Draft')
        with self.store._connection() as db:
            rows = db.execute('SELECT * FROM turn_completions').fetchall()
        self.assertTrue(rows)
        self.assertTrue(all(self.sessions.reviews.notification(r) is False for r in rows))
        run = self.stage(self.report())
        with self.store._connection() as db:
            final = db.execute('SELECT * FROM turn_completions WHERE id=?', (run['completion_id'],)).fetchone()
        self.assertTrue(self.sessions.reviews.notification(final))

    def test_each_provider_can_receive_a_critic_stage_without_coordination_tools(self):
        for client in ('claude', 'kimi', 'muse', 'antigravity'):
            with self.subTest(client=client):
                self.owner = self.sessions.create({})['session']['thread_id']
                run = self.start(client=client)
                self.assertEqual(run['state'], 'active', run['error'])
                self.stage('A text draft')
                self.sessions.inbox_wake.process_once(); self.sessions.persistence.flush()
                run = self.run_state()
                self.assertIsNotNone(run['steps'][-1]['turn_id'])
                critic = run['critic_id']
                with self.sessions.lock:
                    history = copy.deepcopy(self.sessions._history(critic))
                prompt = history['turns'][-1]['items'][0]['content'][0]['text']
                self.assertIn(self.body['message'], prompt)
                self.assertIn('A text draft', prompt)
                self.sessions.reviews.cancel(self.owner)

    def test_streamed_and_reloaded_drafts_are_not_public_answers(self):
        self.start(); self.sessions.inbox_wake.process_once(); self.sessions.persistence.flush()
        turn_id = self.run_state()['steps'][0]['turn_id']
        params = {'threadId': self.owner, 'turnId': turn_id, 'item': {'id': 'draft', 'type': 'agentMessage', 'text': 'PROVISIONAL'}}
        self.sessions._event({'method': 'item/completed', 'params': params})
        event = self.sessions.events[-1]
        self.assertEqual(event['params']['item']['type'], 'reviewStage')
        self.assertNotIn('PROVISIONAL', json.dumps(event))
        self.stage('PROVISIONAL')
        detail = self.sessions.read(self.owner)
        self.assertNotIn('PROVISIONAL', json.dumps(detail['thread']))
        self.assertEqual(detail['reviews'][0]['draft'], 'PROVISIONAL')
        self.assertNotIn('PROVISIONAL', thread_preview(self.sessions, self.owner)['latest_message']['text'])

    def test_peer_messages_cannot_claim_a_review_stage(self):
        self.start()
        peer = self.sessions.create({})['session']['thread_id']
        message = self.store.send_message(sender_session_id=peer, recipient_session_id=self.owner,
                                         body='Unrelated task', classification='action_required', reply_required=False)
        self.sessions.inbox_wake.process_once(); self.sessions.persistence.flush()
        with self.store._connection() as db:
            self.assertIsNone(db.execute('SELECT 1 FROM message_wake_attempts WHERE message_id=?', (message['id'],)).fetchone())
        self.stage('Draft'); self.stage(self.report())
        self.sessions.inbox_wake.process_once()
        with self.store._connection() as db:
            self.assertIsNotNone(db.execute('SELECT 1 FROM message_wake_attempts WHERE message_id=?', (message['id'],)).fetchone())

    def test_queue_and_invalid_input_cannot_interrupt_review(self):
        self.start()
        with self.assertRaises(CoordinationError):
            self.sessions.queue.enqueue(self.owner, {'message': 'Queued task'})
        with self.assertRaises(CoordinationError):
            self.sessions.send(self.owner, {'message': ''})
        self.assertEqual(self.run_state()['state'], 'active')

    def test_failed_submission_does_not_retry_after_runtime_replacement(self):
        self.start(); self.sessions.rpc.fail_turn = True
        self.sessions.inbox_wake.process_once()
        self.sessions.persistence.flush()
        ReviewRuntime(self.sessions).process_once()
        self.assertEqual(self.run_state()['state'], 'incomplete')
        self.sessions.rpc.fail_turn = False
        self.sessions.inbox_wake.process_once()
        self.assertEqual(self.sessions.rpc.threads[self.owner]['turns'], [])

    def test_restart_reconstructs_saved_completed_stage(self):
        self.start(); self.stage('Draft')
        self.sessions.persistence.flush()
        native = self.sessions.rpc
        self.sessions.close()
        def restored(store, callback):
            rpc = FakeCodex(store, callback)
            rpc.threads = copy.deepcopy(native.threads); rpc.settings = copy.deepcopy(native.settings)
            return rpc
        self.sessions = BrowserSessions(self.store, str(self.root), rpc_factory=restored)
        self.addCleanup(self.sessions.close)
        self.sessions.reviews.process_once()
        self.assertEqual(len(self.run_state()['steps']), 2)
        self.assertEqual(self.stage(self.report())['state'], 'complete')

    def test_late_interruption_never_steers_new_input_into_review_turn(self):
        self.start(); self.sessions.inbox_wake.process_once(); self.sessions.persistence.flush()
        original = self.sessions.rpc.request
        def delay(method, params=None):
            return {} if method == 'turn/interrupt' else original(method, params)
        with patch.object(self.sessions.rpc, 'request', side_effect=delay):
            with self.assertRaisesRegex(CoordinationError, 'still stopping'):
                self.sessions.send(self.owner, {'message': 'A different task'})
            with self.assertRaisesRegex(CoordinationError, 'still stopping'):
                self.sessions.send(self.owner, {'message': 'A different task'})
        self.assertFalse(any(method == 'turn/steer' for method, _ in self.sessions.rpc.calls))
        self.assertEqual(self.run_state()['state'], 'superseded')

    def test_final_result_and_receipt_are_available_to_normal_read_consumers(self):
        self.start(); self.stage('Final answer'); run = self.stage(self.report())
        detail = self.sessions.read(self.owner)
        result = next(t for t in detail['thread']['turns'] if t['id'] == 'review:' + run['id'])
        self.assertIn('Final answer', result['items'][-1]['text'])
        self.assertIn('Final answer', thread_preview(self.sessions, self.owner)['latest_message']['text'])
        self.assertEqual(detail['work_thread']['turn_completion']['id'], run['completion_id'])
        self.assertEqual(len([i for t in detail['thread']['turns'] for i in t['items'] if i['type'] == 'agentMessage']), 1)
        self.assertEqual(detail['work_thread']['attention_reason'], 'update')
        critic = self.sessions.work_thread(run['critic_id'])
        self.assertFalse(critic['needs_attention'])
        self.assertFalse(critic['unread_result'])
        listed = {t['thread_id']: t for t in self.sessions.list_work_threads()}
        self.assertFalse(listed[run['critic_id']]['needs_attention'])
        self.assertEqual(listed[self.owner]['turn_completion']['id'], run['completion_id'])

    def test_cancel_during_provider_acceptance_binds_and_stops_the_late_turn(self):
        self.start()
        entered, release, cancelled = threading.Event(), threading.Event(), threading.Event()
        original, finish = self.sessions.rpc.request, self.sessions.reviews._finish
        def delayed(method, params=None):
            if method == 'turn/start':
                entered.set(); release.wait(5)
            return original(method, params)
        def finished(*args, **kwargs):
            finish(*args, **kwargs); cancelled.set()
        with patch.object(self.sessions.rpc, 'request', side_effect=delayed), patch.object(self.sessions.reviews, '_finish', side_effect=finished):
            with ThreadPoolExecutor(max_workers=2) as pool:
                wake = pool.submit(self.sessions.inbox_wake.process_once)
                self.assertTrue(entered.wait(5))
                cancel = pool.submit(self.sessions.reviews.cancel, self.owner)
                self.assertTrue(cancelled.wait(5)); release.set()
                wake.result(timeout=10); cancel.result(timeout=10)
        run = self.run_state()
        self.assertEqual(run['state'], 'cancelled')
        self.assertIsNotNone(run['steps'][0]['turn_id'])
        self.assertEqual(self.sessions.rpc.threads[self.owner]['turns'][-1]['status'], 'interrupted')


if __name__ == '__main__':
    unittest.main()
