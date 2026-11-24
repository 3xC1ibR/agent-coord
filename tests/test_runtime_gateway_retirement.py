"""Legacy intake is predecessor-owned; ordinary updates use the stable edge."""
from pathlib import Path
from contextlib import redirect_stdout
from io import StringIO
import hashlib
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_coord import desktop_runtime as runtime
from agent_coord import runtime_gateway_retirement as retirement


PREDECESSOR = '''import json, os
from pathlib import Path

def status(database, result):
    directory = Path(database).parent / ('.' + Path(database).name + '.runtime') / 'interaction-gateway'
    record = json.loads((directory / 'gateway.json').read_text())
    return {**result, 'interaction_url': record['url'], 'interaction_payload': record['payload'],
            'interaction_settled': False, 'retained_helper': __file__,
            'authorization': json.loads((directory / 'activation-request.json').read_text())['request_id']}

def retire(database):
    raise RuntimeError('Unsettled accepted input cannot be retired')
'''


class GatewayRetirementTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.database = str(self.root / 'state.sqlite3')
        self.directory = retirement.gateway_directory(self.database)

    def recorded_predecessor(self):
        resources = self.root / 'predecessor'
        package = resources / 'backend/agent_coord'
        package.mkdir(parents=True)
        (package / '__init__.py').write_text('')
        (package / 'runtime_gateway_lifecycle.py').write_text(PREDECESSOR)
        for name in ('desktop_runtime', 'runtime_interaction_gateway'):
            (package / (name + '.py')).write_text('# exact recorded predecessor source\n')
        runtime.atomic_json(resources / 'backend.json', {**runtime.compatibility_metadata('0.1.34+98'), 'python': sys.executable})
        manifest = {str(path.relative_to(resources / 'backend')): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in package.glob('*.py')}
        runtime.atomic_json(resources / 'backend-snapshot.json', manifest)
        payload = runtime.stage_payload(resources, self.directory.parent)
        self.directory.mkdir(mode=0o700)
        runtime.atomic_json(self.directory / 'gateway.json', {
            'database':self.database, 'payload':str(payload), 'url':'http://127.0.0.1:23456/',
            'instance':'recorded-owner', 'control_token':'private-token'})
        runtime.atomic_json(self.directory / 'activation-request.json', {'request_id':'accepted-update'})
        (self.directory / 'message-intake.sqlite3').write_bytes(b'accepted input and original receipts')
        return payload

    def test_absent_service_does_not_start_a_gateway_or_create_an_intake(self):
        with patch.object(retirement.subprocess, 'run', side_effect=AssertionError('Started a gateway')):
            self.assertEqual(retirement.status(self.database, {'status':'serving'}), {'status':'serving'})
            self.assertIsNone(retirement.retire(self.database))
        self.assertFalse(self.directory.exists())

    def test_status_uses_exact_recorded_helper_and_preserves_authorization_receipts(self):
        payload = self.recorded_predecessor()
        intake = self.directory / 'message-intake.sqlite3'
        original = intake.read_bytes()
        result = retirement.status(self.database, {'status':'serving'})
        self.assertEqual(result['interaction_payload'], str(payload))
        self.assertEqual(result['authorization'], 'accepted-update')
        self.assertTrue(result['retained_helper'].startswith(str(payload)))
        self.assertEqual(intake.read_bytes(), original)
        self.assertFalse(result['interaction_settled'])
        with self.assertRaisesRegex(RuntimeError, 'preserve the predecessor'):
            retirement.retire(self.database)
        self.assertEqual(intake.read_bytes(), original)

    def test_unsettled_attach_does_not_select_a_different_candidate(self):
        self.recorded_predecessor()
        source = {'database':self.database, 'instance':'existing-owner'}
        with patch.object(runtime, 'control', return_value={'status':'serving'}) as control:
            result = retirement.attach(self.database, source, self.root / 'replacement', True)
        self.assertFalse(result['interaction_settled'])
        control.assert_called_once_with(source, 'status')

    def test_tampered_or_outside_helper_is_rejected_before_execution(self):
        payload = self.recorded_predecessor()
        (payload / 'backend/agent_coord/runtime_interaction_gateway.py').write_text('changed')
        with patch.object(retirement.subprocess, 'run', side_effect=AssertionError('Executed changed helper')):
            with self.assertRaisesRegex(RuntimeError, 'manifest'):
                retirement.status(self.database, {})
        record = runtime.read_json(self.directory / 'gateway.json')
        runtime.atomic_json(self.directory / 'gateway.json', {**record, 'payload':str(self.root / 'outside')})
        with self.assertRaisesRegex(RuntimeError, 'exact retained payload'):
            retirement.status(self.database, {})

    def test_missing_identity_cannot_abandon_existing_intake(self):
        self.directory.mkdir(mode=0o700)
        (self.directory / 'message-intake.sqlite3').write_bytes(b'accepted')
        with self.assertRaisesRegex(RuntimeError, 'no verified helper identity'):
            retirement.status(self.database, {})
        self.assertTrue((self.directory / 'message-intake.sqlite3').exists())

    def test_legacy_service_entrypoint_cannot_create_new_intake(self):
        with patch.object(retirement.os, 'execve', side_effect=AssertionError('Created new intake')):
            with self.assertRaisesRegex(RuntimeError, 'cannot be created'):
                retirement.serve_existing(self.database, None, None)

    def test_normal_product_does_not_ship_predecessor_engines(self):
        package = Path(runtime.__file__).parent
        for name in ('runtime_gateway_lifecycle', 'runtime_interaction_gateway', 'runtime_saved_views', 'runtime_message_intake'):
            self.assertFalse((package / (name + '.py')).exists(), name)

    def test_stable_only_status_cannot_bypass_unsettled_predecessor_intake(self):
        from agent_coord import runtime_edge
        record = {'database': self.database, 'instance':'serving-owner', 'payload':str(self.root), 'stable_origin':1}
        existing = {'interaction_url':'http://127.0.0.1:23456/', 'interaction_settled':False}
        output = StringIO()
        with patch.object(runtime, 'discover', return_value=record), \
                patch.object(runtime, 'control', return_value={'status':'serving'}), \
                patch.object(retirement, 'status', return_value=existing), \
                patch.object(runtime_edge, 'ensure', side_effect=AssertionError('Bypassed accepted predecessor input')), \
                redirect_stdout(output):
            self.assertEqual(runtime.main(['--db', self.database, '--resources', str(self.root),
                                          '--runtime-command', 'status', '--runtime-stable-origin']), 0)
        self.assertEqual(json.loads(output.getvalue())['interaction_url'], existing['interaction_url'])
