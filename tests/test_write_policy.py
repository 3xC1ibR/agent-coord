from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins/agent-coord/scripts'))
from agent_coord.hook import ANTIGRAVITY_WRITE_TOOLS, _extract_paths, handle_antigravity, main
from agent_coord.store import CoordinationError, CoordinationStore, normalize_scope, normalize_target_path
from agent_coord.write_policy import check_write


class WritePolicyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.store = CoordinationStore(self.root / 'state.sqlite3')
        self.store.register(session_id='google', client='antigravity', cwd=str(self.root))
        env = patch.dict(os.environ, {'AGENT_COORD_CLIENT': 'antigravity', 'AGENT_COORD_SESSION_ID': 'google'})
        env.start()
        self.addCleanup(env.stop)

    def native(self, name, target):
        return handle_antigravity({'conversationId': 'native-not-coordination-id', 'toolCall': {
            'name': name, 'args': {'TargetFile': str(self.root / target)}}}, self.store)

    def test_all_native_file_tool_aliases_use_shared_policy(self):
        self.store.begin_work(session_id='google', scopes=['allowed'])
        for name in ANTIGRAVITY_WRITE_TOOLS:
            with self.subTest(name=name):
                self.assertEqual(self.native(name, 'allowed/new.txt'), {'decision': 'allow'})
                result = self.native(name, 'forbidden/new.txt')
                self.assertEqual(result['decision'], 'deny')
                self.assertIn('outside the declared scope', result['reason'])
        with self.assertRaises(CoordinationError):
            self.store.get_session('native-not-coordination-id')

    def test_solo_then_concurrent_requires_scope_and_validation_denies_writes(self):
        self.assertIsNone(check_write(self.store, 'google', ['x']))
        self.store.register(session_id='peer', client='codex', cwd=str(self.root))
        self.store.touch('peer', 'implementing', turn_active=True)
        self.assertIn('Another active session', check_write(self.store, 'google', ['x']))
        self.assertTrue(self.store.get_session('peer')['scope_required'])
        self.store.begin_work(session_id='peer', scopes=['peer'])
        self.store.begin_work(session_id='google', scopes=['allowed'], lease_mode='validation', activity='validating')
        self.assertIn('validation lease', self.native('create_file', 'allowed/x')['reason'])

    def test_unknown_targets_activity_and_conflicts_fail_closed(self):
        self.store.begin_work(session_id='google', scopes=['allowed'])
        self.assertIn('could not be determined', check_write(self.store, 'google', []))
        self.store.touch('google', 'waiting')
        self.assertIn('activity is waiting', self.native('create_file', 'allowed/x')['reason'])
        self.store.touch('google', 'implementing')
        with patch.object(self.store, 'check_conflicts', return_value=[{'name':'peer', 'session_id':'peer'}]):
            self.assertIn('conflicts', self.native('edit_file', 'allowed/x')['reason'])

    def test_outside_repository_passes_through(self):
        self.store.begin_work(session_id='google', scopes=['allowed'])
        self.assertEqual(self.native('create_file', '../external.txt'), {'decision':'allow'})

    def test_malformed_native_hook_and_missing_identity_emit_native_denial(self):
        for payload in ({}, {'toolCall': {'name':'create_file', 'args':{}}}):
            output = io.StringIO()
            with patch('agent_coord.hook.CoordinationStore', return_value=self.store), patch('sys.stdin', io.StringIO(json.dumps(payload))), patch('sys.stdout', output):
                self.assertEqual(main(), 0)
            self.assertEqual(json.loads(output.getvalue())['decision'], 'deny')
        with patch.dict(os.environ, {'AGENT_COORD_SESSION_ID':''}):
            with self.assertRaisesRegex(CoordinationError, 'identity'):
                self.native('create_file', 'x')

    def test_patch_rename_checks_both_source_and_destination(self):
        paths, outside = _extract_paths({'tool_input': {'command': '*** Begin Patch\n*** Update File: allowed/old\n*** Move to: forbidden/new\n@@\n-old\n+new\n*** End Patch'}}, str(self.root))
        self.assertEqual(paths, ['allowed/old', 'forbidden/new'])
        self.assertFalse(outside)
        self.store.begin_work(session_id='google', scopes=['allowed'])
        self.assertIn('forbidden/new', check_write(self.store, 'google', paths))

    def test_scope_list_error_is_atomic_but_real_comma_targets_are_valid(self):
        self.store.begin_work(session_id='google', scopes=['allowed'])
        with self.assertRaisesRegex(CoordinationError, 'Repeat --scope'):
            self.store.begin_work(session_id='google', scopes=['new', 'src/a,src/b'])
        self.assertEqual(self.store.get_session('google')['write_scope'], ['allowed'])
        self.assertEqual(normalize_target_path('allowed/a,b.txt', str(self.root)), 'allowed/a,b.txt')
        for value in ('src/a,src/b', 'a, b', str(self.root / 'a,b')):
            with self.assertRaises(CoordinationError):
                normalize_scope(value, str(self.root))
        self.assertEqual(normalize_scope('src/**', str(self.root)), 'src/**')
