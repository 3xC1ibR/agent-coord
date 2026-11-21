"""Agent schedule operations exercised against real storage and fake providers."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins/agent-coord/scripts'))
from agent_coord.app_control import AppControlWorker
from agent_coord.checkpoint_mcp import schedule_prompt, dispatch
from agent_coord.codex_app_server import BrowserSessions
from agent_coord.schedules import ScheduledPrompts
from agent_coord.store import CoordinationStore, CoordinationError
from test_codex_app_server import FakeCodex
from test_browser_providers import claude_factory

ROOT = Path(__file__).resolve().parents[1]


class ScheduleControlTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.now = 1800000000.
        self.store = CoordinationStore(self.root / 'state.sqlite3', clock=lambda: self.now)
        self.sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex, claude_factory=claude_factory)
        self.addCleanup(self.sessions.close)
        self.worker = AppControlWorker(self.sessions)
        self.addCleanup(self.worker.close)
        for name in ('creator', 'other'):
            self.store.register(session_id=name, client='codex', cwd=str(self.root))
        self.serial = 0

    def request(self, *, sender='creator', **payload):
        self.serial += 1
        args = {'session_id': sender, 'wait_seconds': 0, 'request_id': f'operation-{self.serial}', **payload}
        receipt = schedule_prompt(args, store=self.store)
        self.worker.process_once()
        return self.worker.control.status(receipt['request_id'])

    def create(self, **fields):
        receipt = self.request(mode='new', message='Scheduled work', run_at=self.now + 600, timezone='UTC',
            settings={'cwd': str(self.root), 'client': 'codex', 'model': 'available-model', 'yolo': False}, **fields)
        self.assertEqual(receipt['status'], 'completed', receipt)
        return receipt['result']

    def change(self, item, action, **fields):
        receipt = self.request(action=action, schedule_id=item['id'], version=item['version'], **fields)
        self.assertEqual(receipt['status'], 'completed', receipt)
        return receipt['result']

    def test_list_read_partial_edit_and_one_time_cancel(self):
        item = self.create()
        read = self.request(action='read', schedule_id=item['id'])['result']
        self.assertEqual(read['creator_session_id'], 'creator')
        self.assertFalse(read['settings']['yolo'])
        self.assertEqual(read['message_preview'], 'Scheduled work')
        self.assertEqual(read['next_run_at'], item['run_at'])
        self.assertEqual(self.request(action='list')['result']['schedules'][0]['id'], item['id'])
        self.assertEqual(self.request(sender='other', action='list')['result']['schedules'], [])
        edited = self.change(item, 'edit', message='Revised work', run_at=self.now + 900, timezone='America/Chicago',
                             settings={'effort': 'high'}, recurrence={'frequency': 'daily'})
        self.assertEqual(edited['settings']['model'], 'available-model')
        self.assertEqual(edited['settings']['effort'], 'high')
        self.assertEqual(edited['recurrence'], {'frequency': 'daily'})
        edited = self.change(edited, 'edit', recurrence=None)
        self.assertIsNone(edited['recurrence'])
        self.assertEqual(self.change(edited, 'cancel')['state'], 'cancelled')

    def test_recurring_pause_resume_cancel_and_stale_version(self):
        item = self.create(recurrence={'frequency': 'interval', 'minutes': 5})
        paused = self.change(item, 'pause')
        self.assertEqual((paused['state'], paused['recurrence_status']), ('paused', 'paused'))
        conflict = self.request(action='resume', schedule_id=item['id'], version=item['version'])
        self.assertEqual(conflict['status'], 'failed')
        self.assertIn('changed', conflict['error'])
        self.now += 3600
        edited = self.change(paused, 'edit', message='Still paused', run_at=self.now + 300)
        self.assertEqual(edited['recurrence_status'], 'paused')
        resumed = self.change(edited, 'resume')
        self.assertEqual((resumed['state'], resumed['recurrence_status']), ('scheduled', 'active'))
        self.assertGreater(resumed['run_at'], self.now)
        cancelled = self.change(resumed, 'cancel')
        self.assertEqual((cancelled['state'], cancelled['recurrence_status']), ('cancelled', 'cancelled'))
        self.assertEqual(self.request(action='resume', schedule_id=item['id'], version=cancelled['version'])['status'], 'failed')

    def test_unauthorized_and_explicit_schedule_authorization(self):
        item = self.create()
        for action in ('read', 'edit', 'pause', 'resume', 'cancel', 'run_now'):
            with self.subTest(action=action), self.assertRaisesRegex(CoordinationError, 'creator'):
                self.request(sender='other', action=action, schedule_id=item['id'],
                             **({} if action == 'read' else {'version': item['version']}),
                             **({'message': 'Unauthorized'} if action == 'edit' else {}))
        with self.assertRaisesRegex(CoordinationError, 'exact schedule'):
            self.request(sender='other', action='read', schedule_id=item['id'], authorized_schedule_id='another')
        authorized = self.change(item, 'edit', sender='other', authorized_schedule_id=item['id'], message='User instruction')
        self.assertEqual(authorized['message'], 'User instruction')
        with patch.dict(os.environ, {'AGENT_COORD_SESSION_ID': 'creator'}):
            with self.assertRaisesRegex(CoordinationError, 'identity mismatch'):
                self.request(sender='other', action='list')

    def test_target_read_access_does_not_grant_mutation_access(self):
        target = self.sessions.create({'cwd': str(self.root), 'model': 'available-model'})['session']['thread_id']
        item = self.sessions.schedules.create({'id': '631a8c0a-aef7-4e45-a61e-32757fc886c5', 'message': 'Target work',
            'run_at': self.now + 600, 'timezone': 'UTC', 'target_thread_id': target}, creator_session_id='creator')
        self.assertEqual(self.request(sender=target, action='read', schedule_id=item['id'])['status'], 'completed')
        self.assertEqual(len(self.request(sender=target, action='list')['result']['schedules']), 1)
        with self.assertRaisesRegex(CoordinationError, 'creator'):
            self.change(item, 'cancel', sender=target)
        edited = self.change(item, 'edit', message='Updated target work')
        self.assertEqual(edited['target_thread_id'], target)
        failed = self.request(action='edit', schedule_id=item['id'], version=edited['version'], settings={'yolo': True}, yolo_authorized=True)
        self.assertEqual(failed['status'], 'failed')

    def test_yolo_escalation_requires_explicit_consent_in_both_paths(self):
        settings = {'cwd': str(self.root), 'client': 'codex', 'model': 'available-model', 'yolo': True}
        with self.assertRaisesRegex(CoordinationError, 'yolo_authorized'):
            self.request(mode='new', message='Work', run_at=self.now + 600, timezone='UTC', settings=settings)
        receipt = self.request(mode='new', message='Work', run_at=self.now + 600, timezone='UTC', settings=settings, yolo_authorized=True)
        self.assertEqual(receipt['status'], 'completed')
        item = self.create()
        denied = self.request(action='edit', schedule_id=item['id'], version=item['version'], settings={'yolo': True})
        self.assertEqual(denied['status'], 'failed')
        self.assertIn('yolo_authorized', denied['error'])
        enabled = self.change(item, 'edit', settings={'yolo': True}, yolo_authorized=True)
        self.assertTrue(enabled['settings']['yolo'])
        denied = self.request(action='edit', schedule_id=enabled['id'], version=enabled['version'], message='Keep permissions')
        self.assertEqual(denied['status'], 'failed')
        retained = self.change(enabled, 'edit', message='Keep permissions', yolo_authorized=True)
        self.assertTrue(retained['settings']['yolo'])
        self.assertFalse(self.change(retained, 'edit', settings={'yolo': False})['settings']['yolo'])

    def test_change_receipt_is_atomic_and_retry_ignores_later_versions(self):
        item = self.create(recurrence={'frequency': 'daily'})
        args = {'action': 'pause', 'schedule_id': item['id'], 'version': item['version'], 'request_id': 'pause-retry'}
        original = self.worker.control.complete
        def lose_completion(key, **fields):
            if key == 'pause-retry':
                raise RuntimeError('lost final response')
            return original(key, **fields)
        schedule_prompt({'session_id': 'creator', 'wait_seconds': 0, **args}, store=self.store)
        with patch.object(self.worker.control, 'complete', side_effect=lose_completion):
            with self.assertRaisesRegex(RuntimeError, 'lost final response'):
                self.worker.process_once()
        saved = self.worker.control.status('pause-retry')
        self.assertEqual(saved['status'], 'completed')
        self.change(saved['result'], 'resume')
        retry = schedule_prompt({'session_id': 'creator', 'wait_seconds': 0, **args}, store=self.store)
        self.assertEqual(retry['result'], saved['result'])
        with self.assertRaisesRegex(CoordinationError, 'different app request'):
            schedule_prompt({'session_id': 'creator', 'wait_seconds': 0, **args, 'action': 'cancel'}, store=self.store)

    def test_run_now_preserves_existing_missed_only_semantics(self):
        item = self.create()
        failed = self.request(action='run_now', schedule_id=item['id'], version=item['version'])
        self.assertEqual(failed['status'], 'failed')
        self.now += 1000
        self.sessions.schedules.process_once()
        missed = self.request(action='read', schedule_id=item['id'])['result']
        self.assertEqual(missed['state'], 'missed')
        ready = self.change(missed, 'run_now')
        self.assertEqual(ready['state'], 'scheduled')
        self.assertEqual(ready['run_at'], self.now)

    def test_old_creator_is_backfilled_and_ui_schedule_requires_authorization(self):
        item = self.create()
        with self.store._connection() as db:
            db.execute('DROP INDEX scheduled_prompts_creator')
            db.execute('ALTER TABLE scheduled_prompts DROP COLUMN creator_session_id')
        ScheduledPrompts(self.sessions)
        self.assertEqual(self.request(action='read', schedule_id=item['id'])['result']['creator_session_id'], 'creator')
        ui = self.sessions.schedules.create({'id': '42780a91-3e29-4e06-9904-06d95691feef', 'message': 'UI schedule',
            'run_at': self.now + 1000, 'timezone': 'UTC', 'settings': {'cwd': str(self.root), 'model': 'available-model'}})
        with self.assertRaisesRegex(CoordinationError, 'creator'):
            self.request(action='read', schedule_id=ui['id'])
        self.assertEqual(self.request(action='read', schedule_id=ui['id'], authorized_schedule_id=ui['id'])['status'], 'completed')

    def test_mcp_metadata_binds_management_caller(self):
        item = self.create()
        with patch('agent_coord.checkpoint_mcp.CoordinationStore', return_value=self.store):
            reply = dispatch({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
                'name': 'schedule_prompt', '_meta': {'threadId': 'other'},
                'arguments': {'action': 'read', 'schedule_id': item['id'], 'wait_seconds': 0}}})
        self.assertTrue(reply['result']['isError'])

    def test_cli_create_and_every_management_action_use_shared_contract(self):
        def cli(action, **fields):
            command = [sys.executable, str(ROOT / 'plugins/agent-coord/scripts/agent-coord'),
                       '--db', str(self.store.database_path), 'schedule', action, '--wait', '0']
            self.serial += 1
            command += ['--request-id', f'cli-{self.serial}']
            for key, value in fields.items():
                if value is True:
                    command.append('--' + key.replace('_', '-'))
                else:
                    command += ['--' + key.replace('_', '-'), json.dumps(value) if isinstance(value, dict) else str(value)]
            env = {**os.environ, 'AGENT_COORD_SESSION_ID': 'creator'}
            completed = subprocess.run(command, capture_output=True, text=True, env=env, timeout=15)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            receipt = json.loads(completed.stdout)
            self.worker.process_once()
            result = self.worker.control.status(receipt['request_id'])
            self.assertEqual(result['status'], 'completed', result)
            return result['result']
        item = cli('create', mode='new', message='CLI scheduled work', run_at=self.now + 600, timezone='UTC',
                   settings={'cwd': str(self.root), 'client': 'codex', 'model': 'available-model'},
                   recurrence={'frequency': 'daily'})
        self.assertEqual(cli('list')['schedules'][0]['id'], item['id'])
        self.assertEqual(cli('read', schedule_id=item['id'])['version'], item['version'])
        edited = cli('edit', schedule_id=item['id'], version=item['version'], message='CLI edit')
        paused = cli('pause', schedule_id=item['id'], version=edited['version'])
        resumed = cli('resume', schedule_id=item['id'], version=paused['version'])
        cancelled = cli('cancel', schedule_id=item['id'], version=resumed['version'])
        self.assertEqual(cancelled['recurrence_status'], 'cancelled')
        missed = self.create()
        self.now += 1000
        self.sessions.schedules.process_once()
        missed = self.request(action='read', schedule_id=missed['id'])['result']
        self.assertEqual(cli('run-now', schedule_id=missed['id'], version=missed['version'])['state'], 'scheduled')

    def test_bad_request_shapes_do_not_queue_mutations(self):
        item = self.create()
        cases = [dict(action='cancel', schedule_id=item['id'], version=True),
                 dict(action='edit', schedule_id=item['id'], version=1),
                 dict(action='read', schedule_id=item['id'], settings={}),
                 dict(action='list', authorized_schedule_id=item['id']),
                 dict(action=[]), dict(action='list', request_id=''),
                 dict(action='cancel', schedule_id=item['id'], version=0)]
        for fields in cases:
            with self.subTest(fields=fields), self.assertRaises(CoordinationError):
                self.request(**fields)

    def test_cli_forwards_stdin_and_waits_outside_runtime_request(self):
        import contextlib
        import io
        from agent_coord import cli, desktop_runtime, runtime_requests
        from agent_coord.context import request_ownership
        descriptor = desktop_runtime.lock_file(desktop_runtime.runtime_directory(self.store.database_path) / 'owner.lock')
        self.addCleanup(os.close, descriptor)
        observed = []
        def control(record, action, **values):
            request = values['request']
            observed.append(request)
            token = request_ownership.set(frozenset({(os.getpid(), str(self.store.database_path))}))
            try:
                response = runtime_requests.execute_request(self.store, request)
                self.worker.process_once()
                return response
            finally:
                request_ownership.reset(token)
        argv = ['--db', str(self.store.database_path), 'schedule', 'create', '--mode', 'new',
                '--message', '-', '--run-at', str(self.now + 600), '--timezone', 'UTC',
                '--settings', json.dumps({'cwd': str(self.root), 'client': 'codex', 'model': 'available-model'}),
                '--request-id', 'forwarded-stdin', '--wait', '2']
        output = io.StringIO()
        with patch.dict(os.environ, {'AGENT_COORD_SESSION_ID': 'creator'}), \
                patch('agent_coord.desktop_runtime.discover', return_value={'client_requests': 1}), \
                patch('agent_coord.desktop_runtime.control', side_effect=control), \
                patch('sys.stdin', io.StringIO('  Exact caller input\n\n  preserved.')), contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(argv), 0)
        receipt = json.loads(output.getvalue())
        self.assertEqual(receipt['status'], 'completed', receipt)
        self.assertEqual(receipt['result']['message'], '  Exact caller input\n\n  preserved.')
        submitted = next(request for request in observed if request['kind'] == 'cli')
        self.assertTrue(submitted['payload']['defer_wait'])
        self.assertEqual(submitted['payload']['stdin'], receipt['result']['message'])

    def test_edits_of_existing_yolo_content_require_consent_and_record_actor(self):
        item = self.change(self.create(), 'edit', settings={'yolo': True}, yolo_authorized=True)
        for sender in ('creator', 'other'):
            for changes in ({'message': 'Changed full-access work'},
                            {'recurrence': {'frequency': 'interval', 'minutes': 1}},
                            {'settings': {'model': 'available-model'}},
                            {'settings': {'client': 'codex', 'cwd': str(self.root)}},
                            {'run_at': self.now + 900}, {'timezone': 'America/Chicago'}):
                fields = {'authorized_schedule_id': item['id']} if sender == 'other' else {}
                denied = self.request(sender=sender, action='edit', schedule_id=item['id'],
                                      version=item['version'], **fields, **changes)
                self.assertEqual(denied['status'], 'failed', denied)
                self.assertIn('yolo_authorized', denied['error'])
        changed = self.change(item, 'edit', sender='other', authorized_schedule_id=item['id'],
                              yolo_authorized=True, message='User-authorized changed full-access work')
        self.assertEqual(changed['authorization'], {'sender_session_id': 'other',
                         'authorized_schedule_id': item['id'], 'yolo_authorized': True})
        with self.store.reading(), self.store._connection() as db:
            row = db.execute('SELECT * FROM app_control_requests WHERE request_id = ?',
                             (f'operation-{self.serial}',)).fetchone()
        self.assertEqual(row['sender_session_id'], 'other')
        self.assertEqual(json.loads(row['payload_json'])['authorized_schedule_id'], item['id'])
        self.assertTrue(json.loads(row['payload_json'])['yolo_authorized'])
        self.assertEqual(json.loads(row['result_json'])['authorization'], changed['authorization'])
        disabled = self.change(changed, 'edit', settings={'yolo': False})
        edited = self.change(disabled, 'edit', message='Default-permission replacement')
        denied = self.request(action='edit', schedule_id=item['id'], version=edited['version'], settings={'yolo': True})
        self.assertEqual(denied['status'], 'failed')
        self.assertTrue(self.change(edited, 'edit', settings={'yolo': True}, yolo_authorized=True)['settings']['yolo'])

    def test_target_yolo_is_checked_at_execution_instead_of_enqueue(self):
        target = self.sessions.create({'cwd': str(self.root), 'model': 'available-model'})['session']['thread_id']
        item = self.request(sender=target, mode='self', message='Original prompt', run_at=self.now + 600,
                            timezone='UTC')['result']
        queued = schedule_prompt({'session_id': target, 'wait_seconds': 0, 'action': 'edit',
            'schedule_id': item['id'], 'version': item['version'], 'message': 'Queued replacement',
            'request_id': 'target-edit-before-yolo'}, store=self.store)
        self.sessions.update(target, {'yolo': True})
        self.worker.process_once()
        denied = self.worker.control.status(queued['request_id'])
        self.assertEqual(denied['status'], 'failed', denied)
        self.assertIn('yolo_authorized', denied['error'])
        for changes in ({'message': 'Full-access replacement'}, {'recurrence': {'frequency': 'daily'}}):
            denied = self.request(sender=target, action='edit', schedule_id=item['id'], version=item['version'], **changes)
            self.assertEqual(denied['status'], 'failed', denied)
        changed = self.change(item, 'edit', sender=target, message='Authorized full-access replacement', yolo_authorized=True)
        self.assertTrue(changed['settings']['yolo'])
        self.assertEqual(changed['authorization']['sender_session_id'], target)

    def test_schedule_yolo_change_after_guard_still_conflicts_on_version(self):
        item = self.create()
        original = self.sessions.schedules.change
        def concurrent_yolo_change(schedule_id, body, **keywords):
            with self.store._connection() as db:
                db.execute("UPDATE scheduled_prompts SET settings_json = json_set(settings_json, '$.yolo', json('true')), version = version + 1 WHERE id = ?", (schedule_id,))
            return original(schedule_id, body, **keywords)
        with patch.object(self.sessions.schedules, 'change', side_effect=concurrent_yolo_change):
            denied = self.request(action='edit', schedule_id=item['id'], version=item['version'], message='Concurrent edit')
        self.assertEqual(denied['status'], 'failed')
        self.assertIn('changed', denied['error'])
        self.assertEqual(self.request(action='read', schedule_id=item['id'])['result']['message'], item['message'])

    def test_cross_creator_resume_and_run_now_require_yolo_assertion(self):
        recurring = self.change(self.create(recurrence={'frequency': 'daily'}), 'edit', settings={'yolo': True}, yolo_authorized=True)
        paused = self.change(recurring, 'pause')
        denied = self.request(sender='other', action='resume', schedule_id=paused['id'], version=paused['version'], authorized_schedule_id=paused['id'])
        self.assertEqual(denied['status'], 'failed')
        resumed = self.change(paused, 'resume', sender='other', authorized_schedule_id=paused['id'], yolo_authorized=True)
        self.assertTrue(resumed['authorization']['yolo_authorized'])
        self.change(resumed, 'cancel')
        item = self.change(self.create(), 'edit', settings={'yolo': True}, yolo_authorized=True)
        self.now += 1000
        self.sessions.schedules.process_once()
        missed = self.request(action='read', schedule_id=item['id'])['result']
        denied = self.request(sender='other', action='run_now', schedule_id=item['id'], version=missed['version'], authorized_schedule_id=item['id'])
        self.assertEqual(denied['status'], 'failed')
        ready = self.change(missed, 'run_now', sender='other', authorized_schedule_id=item['id'], yolo_authorized=True)
        self.assertEqual(ready['state'], 'scheduled')

    def test_create_crash_after_insert_preserves_completed_receipt_through_reconcile(self):
        original = self.sessions.schedules.create
        def crash_after_create(*args, **fields):
            original(*args, **fields)
            raise RuntimeError('crash after durable create')
        with patch.object(self.sessions.schedules, 'create', side_effect=crash_after_create):
            receipt = self.request(mode='new', message='Durable creation', run_at=self.now + 600, timezone='UTC',
                settings={'cwd': str(self.root), 'client': 'codex', 'model': 'available-model'})
        self.assertEqual(receipt['status'], 'completed', receipt)
        self.assertEqual(receipt['result']['message'], 'Durable creation')
        self.assertEqual(receipt['result']['authorization']['sender_session_id'], 'creator')
        self.now += 120
        self.worker.control._reconcile()
        self.assertEqual(self.worker.control.status(receipt['request_id']), receipt)
        self.worker.control.complete(receipt['request_id'], error='Late worker error must not replace success')
        self.assertEqual(self.worker.control.status(receipt['request_id']), receipt)
        self.assertEqual(len(self.sessions.schedules.list()), 1)

    def test_existing_create_id_completes_once_without_overwriting_saved_receipt(self):
        import uuid
        key = 'existing-create-id'
        payload = {'mode': 'new', 'message': 'Existing creation', 'run_at': self.now + 600, 'timezone': 'UTC',
                   'settings': {'cwd': str(self.root), 'client': 'codex', 'model': 'available-model'}}
        queued = schedule_prompt({'session_id': 'creator', 'wait_seconds': 0, 'request_id': key, **payload}, store=self.store)
        body = {name: payload[name] for name in ('message', 'run_at', 'timezone', 'settings')}
        body['id'] = str(uuid.uuid5(uuid.NAMESPACE_URL, 'agent-coord:schedule:' + key))
        self.sessions.schedules.create(body, creator_session_id='creator')
        self.assertTrue(self.worker.control.claim(key, self.worker.owner))
        with self.assertRaisesRegex(CoordinationError, 'already saved'):
            self.sessions.schedules.create({**body, 'message': 'Conflicting body'}, creator_session_id='creator', request_id=key)
        self.assertEqual(self.worker.control.status(key)['status'], 'running')
        self.assertIsNone(self.worker.control.status(key)['result'])
        item = self.sessions.schedules.create(body, creator_session_id='creator', request_id=key,
                  authorization={'sender_session_id': 'creator', 'authorized_schedule_id': None, 'yolo_authorized': False})
        saved = self.worker.control.status(key)
        self.assertEqual(saved['status'], 'completed')
        self.assertEqual(saved['result'], item)
        self.now += 900
        self.sessions.schedules.process_once()
        with self.assertRaisesRegex(CoordinationError, 'no longer running'):
            self.sessions.schedules.create(body, creator_session_id='creator', request_id=key)
        self.assertEqual(self.worker.control.status(key), saved)
        retry = schedule_prompt({'session_id': 'creator', 'wait_seconds': 0, 'request_id': key, **payload}, store=self.store)
        self.assertEqual(retry['result'], saved['result'])
        self.assertEqual(len(self.sessions.schedules.list()), 1)

    def test_create_failure_before_receipt_rolls_back_insert(self):
        with patch.object(self.sessions.schedules, '_complete_agent_request', side_effect=RuntimeError('receipt failure before commit')):
            failed = self.request(mode='new', message='Must roll back', run_at=self.now + 600, timezone='UTC',
                settings={'cwd': str(self.root), 'client': 'codex', 'model': 'available-model'})
        self.assertEqual(failed['status'], 'failed')
        self.assertIn('receipt failure before commit', failed['error'])
        self.assertEqual(self.sessions.schedules.list(), [])
