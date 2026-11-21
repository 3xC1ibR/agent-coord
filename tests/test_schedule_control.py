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
        retained = self.change(enabled, 'edit', message='Keep permissions')
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
