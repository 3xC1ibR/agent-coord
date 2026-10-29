from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from test_codex_app_server import BrowserSessions, CoordinationStore, FakeCodex, PLUGIN_SCRIPTS
from test_browser_providers import claude_factory
from agent_coord.app_control import AppControl, AppControlWorker
from agent_coord.checkpoint_mcp import move_thread
from agent_coord.store import CoordinationError
from agent_coord.thread_control import ThreadControl
from agent_coord.thread_move import move_work_thread
from agent_coord.ui import make_ui_server


class ThreadMoveTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.a, self.b = self.root / 'A', self.root / 'B'
        self.a.mkdir()
        self.b.mkdir()
        (self.a / '.git').mkdir()
        (self.b / '.git').mkdir()
        self.store = CoordinationStore(self.root / 'state.sqlite3')
        self.store.register(session_id='requester', client='codex', cwd=str(self.b))
        self.sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex, claude_factory=claude_factory)
        self.addCleanup(self.sessions.close)
        self.worker = AppControlWorker(self.sessions)
        self.addCleanup(self.worker.close)
        self.control = self.worker.control

    def create(self, client='codex'):
        return self.sessions.create({'cwd': str(self.b), 'client': client})['session']['thread_id']

    def finish(self, thread):
        turn = self.sessions.rpc.threads[thread]['turns'][-1]
        turn['status'] = 'completed'
        self.sessions._event({'method': 'turn/completed', 'params': {'threadId': thread, 'turn': copy.deepcopy(turn)}})

    def request(self, thread, **kwargs):
        return self.control.request('move', 'requester', {'cwd': str(self.a)}, thread_id=thread, **kwargs)

    def assert_directory(self, thread, directory):
        self.assertEqual(self.sessions._record(thread)['cwd'], str(directory))
        self.assertEqual(self.store.get_session(thread)['cwd'], str(directory))
        self.assertEqual(self.store.threads.get(thread)['project_cwd'], str(directory))

    def test_move_preserves_identity_history_links_checkpoint_and_placement(self):
        thread = self.create()
        self.sessions.rpc.fast_turn = True
        self.sessions.send(thread, {'message': 'Original request'})
        self.store.threads.checkpoint(thread, {'phase': 'investigation', 'summary': 'Investigated.'})
        self.store.threads.add_link(thread, {'kind': 'document', 'target': 'notes.md'})
        group = self.store.threads.organization.create_project('Old group', str(self.b))
        self.store.threads.update(thread, project_id=group['id'], attention='later', title='Keep title')
        before = self.store.threads.get(thread, history=True)
        history = self.sessions.read(thread)['thread']['turns']
        moved = move_work_thread(self.sessions, thread, str(self.a))
        self.assert_directory(thread, self.a)
        self.assertEqual(moved['thread_id'], thread)
        self.assertEqual(moved['attention'], 'later')
        self.assertEqual(moved['title'], 'Keep title')
        self.assertIsNone(moved['project_id'])
        self.assertEqual(moved['repository_root'], str(self.a))
        after = self.store.threads.get(thread, history=True)
        self.assertEqual(after['links'], before['links'])
        self.assertEqual(after['checkpoints'], before['checkpoints'])
        self.assertEqual(self.sessions.read(thread)['thread']['turns'], history)
        self.sessions.send(thread, {'message': 'Continue in A'})
        params = [p for m, p in self.sessions.rpc.calls if m == 'turn/start'][-1]
        self.assertEqual(params['cwd'], str(self.a))
        self.assertEqual(params['input'], [{'type': 'text', 'text': 'Continue in A'}])

    def test_self_move_queues_until_turn_finishes_then_retries_idempotently(self):
        thread = self.create()
        self.sessions.send(thread, {'message': 'Work'})
        with patch.dict(os.environ, {'AGENT_COORD_SESSION_ID': thread}):
            receipt = move_thread({'cwd': str(self.a), 'request_id': 'self-move'}, store=self.store)
        self.worker.process_once()
        self.assertEqual(self.control.status(receipt['request_id'])['status'], 'queued')
        self.assert_directory(thread, self.b)
        self.finish(thread)
        self.worker.process_once()
        self.assertEqual(self.control.status(receipt['request_id'])['status'], 'completed')
        self.assert_directory(thread, self.a)
        with patch.dict(os.environ, {'AGENT_COORD_SESSION_ID': thread}):
            retry = move_thread({'cwd': str(self.a), 'request_id': 'self-move'}, store=self.store)
        self.assertEqual(retry['status'], 'completed')

    def test_active_turn_rejected_without_partial_move(self):
        thread = self.create()
        self.sessions.send(thread, {'message': 'Work'})
        with self.assertRaisesRegex(CoordinationError, 'running turn'):
            move_work_thread(self.sessions, thread, str(self.a))
        self.assert_directory(thread, self.b)
        self.finish(thread)

    def test_saved_active_flag_prevents_worker_claim(self):
        thread = self.create()
        receipt = self.request(thread)
        with self.store._connection() as db:
            db.execute('UPDATE sessions SET turn_active = 1 WHERE session_id = ?', (thread,))
        self.worker.process_once()
        self.assertEqual(self.control.status(receipt['request_id'])['status'], 'queued')
        self.assert_directory(thread, self.b)

    def test_queued_user_input_runs_before_agent_move(self):
        thread = self.create()
        self.sessions.send(thread, {'message': 'Current turn'})
        self.sessions.queue.enqueue(thread, {'message': 'A queued follow-up'})
        self.sessions.queue.pause(thread, 'Wait for review')
        receipt = self.request(thread)
        self.finish(thread)
        self.worker.process_once()
        self.assertEqual(self.control.status(receipt['request_id'])['status'], 'queued')
        self.assert_directory(thread, self.b)
        queued = self.sessions.queue.list(thread)[0]
        self.sessions.queue.change(thread, {'action': 'cancel', 'id': queued['id']})
        self.worker.process_once()
        self.assertEqual(self.control.status(receipt['request_id'])['status'], 'completed')
        self.assert_directory(thread, self.a)

    def test_other_runtime_cannot_claim_a_loaded_conversation(self):
        thread = self.create()
        receipt = self.request(thread)
        other = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex, claude_factory=claude_factory)
        self.addCleanup(other.close)
        worker = AppControlWorker(other)
        self.addCleanup(worker.close)
        worker.process_once()
        self.assertEqual(self.control.status(receipt['request_id'])['status'], 'queued')
        self.assert_directory(thread, self.b)
        self.worker.process_once()
        self.assertEqual(self.control.status(receipt['request_id'])['status'], 'completed')
        self.assert_directory(thread, self.a)

    def test_stale_request_cannot_overwrite_another_move(self):
        thread = self.create()
        receipt = self.request(thread)
        elsewhere = self.root / 'C'
        elsewhere.mkdir()
        move_work_thread(self.sessions, thread, str(elsewhere))
        self.worker.process_once()
        self.assertEqual(self.control.status(receipt['request_id'])['status'], 'failed')
        self.assert_directory(thread, elsewhere)

    def test_invalid_deleted_and_outside_destinations_leave_everything_unchanged(self):
        thread = self.create()
        for target in ('', 'relative', str(self.root / 'missing'), str(self.root / 'state.sqlite3'), str(self.root.parent)):
            with self.subTest(target=target), self.assertRaises(CoordinationError):
                move_work_thread(self.sessions, thread, target)
            self.assert_directory(thread, self.b)
        receipt = self.request(thread)
        (self.a / '.git').rmdir()
        self.a.rmdir()
        self.worker.process_once()
        self.assertEqual(self.control.status(receipt['request_id'])['status'], 'failed')
        self.assert_directory(thread, self.b)

    def test_scope_and_pending_close_rejected(self):
        thread = self.create()
        self.store.begin_work(session_id=thread, scopes=['src/**'])
        with self.assertRaisesRegex(CoordinationError, 'work scope'):
            move_work_thread(self.sessions, thread, str(self.a))
        self.assert_directory(thread, self.b)
        self.store.end_work(thread)
        ThreadControl(self.store).request_close(thread)
        with self.assertRaisesRegex(CoordinationError, 'pending close'):
            move_work_thread(self.sessions, thread, str(self.a))
        self.assert_directory(thread, self.b)

    def test_closed_terminal_and_fixed_provider_rejected(self):
        thread = self.create()
        self.sessions.close_work_thread(thread)
        with self.assertRaisesRegex(CoordinationError, 'Reopen'):
            move_work_thread(self.sessions, thread, str(self.a))
        self.assert_directory(thread, self.b)
        with self.assertRaisesRegex(CoordinationError, 'app conversation'):
            move_work_thread(self.sessions, 'requester', str(self.a))
        thread = self.create()
        with self.store._connection() as db:
            db.execute("UPDATE browser_sessions SET client = 'kimi' WHERE thread_id = ?", (thread,))
        with self.assertRaisesRegex(CoordinationError, 'original workspace'):
            self.request(thread)
        self.assert_directory(thread, self.b)

    def test_failed_transaction_rolls_back_all_directory_updates(self):
        thread = self.create()
        with self.store._connection() as db:
            db.execute("CREATE TRIGGER fail_move BEFORE UPDATE OF project_cwd ON work_threads BEGIN SELECT RAISE(ABORT, 'forced failure'); END")
        receipt = self.request(thread)
        self.worker.process_once()
        self.assertEqual(self.control.status(receipt['request_id'])['status'], 'failed')
        self.assert_directory(thread, self.b)

    def test_claude_resumes_in_new_directory(self):
        thread = self.create('claude')
        self.sessions.send(thread, {'message': 'First'})
        connection = self.sessions.claude.connections[thread]
        connection.callback({'type': 'result', 'subtype': 'success'})
        move_work_thread(self.sessions, thread, str(self.a))
        self.sessions.send(thread, {'message': 'Next'})
        self.assertEqual(self.sessions.claude.options[thread]['cwd'], str(self.a))
        self.assertIsNot(self.sessions.claude.connections[thread], connection)
        self.sessions.claude.connections[thread].callback({'type': 'result', 'subtype': 'success'})

    def test_cli_queues_move_for_explicit_and_inherited_target_and_can_cancel(self):
        thread = self.create()
        env = dict(os.environ, AGENT_COORD_SESSION_ID=thread)
        command = [str(PLUGIN_SCRIPTS / 'agent-coord'), '--db', str(self.store.database_path), 'thread']
        result = subprocess.run([*command, 'move', '--cwd', str(self.a), '--request-id', 'cli-move', '--wait', '0'],
                                env=env, text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt['thread_id'], thread)
        self.assertEqual(receipt['status'], 'queued')
        self.control.cancel(receipt['request_id'])
        self.worker.process_once()
        self.assert_directory(thread, self.b)
        # Explicit target management uses the requester's actual identity,
        # rather than overriding the target's inherited caller identity.
        env['AGENT_COORD_SESSION_ID'] = 'requester'
        result = subprocess.run([*command, 'move', '--session-id', thread, '--from-session', 'requester',
                                 '--cwd', str(self.a), '--wait', '0'], env=env, text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.worker.process_once()
        self.assert_directory(thread, self.a)

    def test_http_move_uses_shared_validation_and_returns_destination(self):
        thread = self.create()
        server = make_ui_server(self.store, host='127.0.0.1', port=0, cwd=str(self.root), browser_sessions=self.sessions)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(worker.join)
        self.addCleanup(server.shutdown)
        base = 'http://127.0.0.1:' + str(server.server_address[1]) + '/api/browser/'
        with urllib.request.urlopen(base + 'config') as response:
            token = json.load(response)['token']
        def request(body):
            return urllib.request.Request(base + 'threads/' + thread + '/move', data=json.dumps(body).encode(),
                headers={'Content-Type': 'application/json', 'X-Agent-Coord-Token': token})
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request({'cwd': str(self.a), 'extra': True}))
        self.assertEqual(error.exception.code, 400)
        error.exception.close()
        self.assert_directory(thread, self.b)
        with urllib.request.urlopen(request({'cwd': str(self.a)})) as response:
            moved = json.load(response)
        self.assertEqual(moved['project_cwd'], str(self.a))
        self.assert_directory(thread, self.a)

    def test_mcp_stdio_advertises_move_tool_and_queues_request(self):
        thread = self.create()
        messages = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {
                'name': 'move_thread', 'arguments': {'cwd': str(self.a), 'request_id': 'stdio-move'}}},
        ]
        result = subprocess.run([sys.executable, str(PLUGIN_SCRIPTS / 'agent-coord-mcp')],
            input='\n'.join(map(json.dumps, messages)) + '\n', text=True, capture_output=True, timeout=10,
            env=dict(os.environ, AGENT_COORD_DB=str(self.store.database_path), AGENT_COORD_SESSION_ID=thread))
        self.assertEqual(result.returncode, 0, result.stderr)
        catalog, called = map(json.loads, result.stdout.splitlines())
        self.assertIn('move_thread', [tool['name'] for tool in catalog['result']['tools']])
        receipt = json.loads(called['result']['content'][0]['text'])
        self.assertEqual(receipt['status'], 'queued')
        self.assertEqual(receipt['thread_id'], thread)
        self.worker.process_once()
        self.assert_directory(thread, self.a)


if __name__ == '__main__':
    unittest.main()
