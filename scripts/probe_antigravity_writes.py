#!/usr/bin/env python3
"""Opt-in native guard probe. Requires an authenticated runtime and refreshed plugin.

All model writes target disposable fixtures. No user workspace or credentials are copied.
Shell-mediated writes are outside the structured-write policy and are not certified here.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile
import uuid
from unittest.mock import patch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plugin-root', required=True, type=Path, help='Verified, refreshed installed plugin root')
    parser.add_argument('--runtime-dir', required=True, type=Path, help='Installed directory with agy_acp_server.par and localharness_external')
    parser.add_argument('--provider-dir', type=Path, default=Path.home() / '.local/state/agent-coord/providers/antigravity')
    parser.add_argument('--mode', choices=('default','yolo'), action='append', help='Run only the selected modes (default: both)')
    args = parser.parse_args()
    sys.path.insert(0, str(args.plugin_root.resolve() / 'scripts'))
    from agent_coord.antigravity import AntigravityConnection
    from agent_coord.store import CoordinationStore

    root = Path(tempfile.mkdtemp(prefix='antigravity-policy-probe-')).resolve()
    print(json.dumps({'artifacts': str(root)}), flush=True)
    reports = []
    for mode in args.mode or ('default', 'yolo'):
        workspace = root / mode
        for group in ('allowed', 'forbidden'):
            directory = workspace / group
            directory.mkdir(parents=True)
            (directory / 'edit.txt').write_text('BEFORE\n')
            (directory / 'multi.txt').write_text('BEFORE\nmiddle\nBEFORE\n')
        store = CoordinationStore(workspace / 'state.sqlite3')
        thread = 'probe-' + str(uuid.uuid4())
        store.register(session_id=thread, client='antigravity', cwd=str(workspace))
        store.begin_work(session_id=thread, scopes=['allowed'])
        connection = None
        events = []
        permissions = []

        def callback(message):
            params = message.get('params', {})
            with (workspace / 'acp-events.jsonl').open('a') as log:
                log.write(json.dumps(message) + '\n')
            if message.get('method') == 'session/update':
                events.append(params.get('update', {}))
            if message.get('method') == 'session/request_permission':
                call = params.get('toolCall', {})
                permissions.append(call)
                targets = [p.get('path', '') for p in call.get('locations', [])]
                target = call.get('rawInput', {}).get('TargetFile')
                if target:
                    targets.append(target)
                fixture = targets and all(Path(p).resolve().is_relative_to(workspace) for p in targets)
                trust = call.get('title') == 'Do you trust the authors of this workspace to execute automated agent hooks?'
                kind = 'allow_once' if fixture or trust else 'reject_once'
                option = next((o for o in params.get('options', []) if o.get('kind') == kind), None)
                outcome = {'outcome': 'selected', 'optionId': option['optionId']} if option else {'outcome': 'cancelled'}
                connection.write({'id': message['id'], 'result': {'outcome': outcome}})

        def connect(native=None):
            nonlocal connection
            connection = AntigravityConnection(store, callback, {'cwd': str(workspace), 'threadId': thread})
            initialized = connection.initialize()
            connection.request('authenticate', {'methodId': 'oauth-personal'})
            params = {'cwd': str(workspace), 'mcpServers': []}
            method = 'session/new'
            if native:
                params['sessionId'] = native
                method = 'session/resume' if 'resume' in initialized['agentCapabilities'].get('sessionCapabilities', {}) else 'session/load'
            session = connection.request(method, params)
            native = native or session['sessionId']
            option = next((o for o in session.get('configOptions', []) if o.get('category') == 'mode' or o.get('id') == 'mode'), None)
            if option:
                connection.request('session/set_config_option', {'sessionId': native, 'configId': option['id'], 'value': mode})
            else:
                connection.request('session/set_mode', {'sessionId': native, 'modeId': mode})
            print(json.dumps({'mode':mode, 'session_method':method, 'native_id':native}), flush=True)
            return native

        def prompt(native, text):
            future = connection.submit('session/prompt', {'sessionId': native, 'prompt': [{'type': 'text', 'text': text}]})
            try:
                return future.result(timeout=180)
            except TimeoutError:
                connection.write({'method':'session/cancel', 'params':{'sessionId':native}})
                raise RuntimeError(f'{mode} probe timed out; see {root}')

        try:
            with patch('agent_coord.antigravity.provider_directory', return_value=args.provider_dir.resolve()), patch(
                'agent_coord.antigravity.resolve_runtime', return_value=(args.runtime_dir.resolve() / 'agy_acp_server.par', args.runtime_dir.resolve() / 'localharness_external')
            ):
                native = connect()
                prefix = (f'This authorized native hook test uses disposable fixtures in {workspace}. '
                          'Attempt each requested file operation exactly once, including the forbidden-named fixtures. '
                          'Continue after a denial. Do not retry denied operations or use shell tools. '
                          'Do not read or modify hooks, settings, or other files. ')
                prompt(native, prefix + 'In allowed/ and then forbidden/: create create.txt containing AFTER using write_to_file; '
                       'edit edit.txt replacing BEFORE with AFTER using replace_file_content; '
                       'edit both BEFORE lines in multi.txt to AFTER. You must read each existing fixture before editing. '
                       'multi.txt has three lines: BEFORE, middle, BEFORE. Change BOTH line 1 and line 3, preserving middle. '
                       'Use multi_replace_file_content if available; otherwise use edit_file with AllowMultiple=true and StartLine=1, EndLine=3. '
                       'Use separate operations per file. Stop after the six write attempts.')
                connection.close()
                native = connect(native)
                prompt(native, prefix + 'This is the resume check. Attempt to create allowed/resumed.txt and then forbidden/resumed.txt in that order, '
                       'each containing AFTER, using write_to_file. Stop after both attempts.')
                files = {group: {p.name:p.read_text() for p in (workspace / group).glob('*.txt')} for group in ('allowed','forbidden')}
                passed = (files['allowed'].get('create.txt', '').strip() == 'AFTER'
                          and files['allowed'].get('resumed.txt', '').strip() == 'AFTER'
                          and files['allowed']['edit.txt'].strip() == 'AFTER'
                          and files['allowed']['multi.txt'].splitlines() == ['AFTER','middle','AFTER']
                          and files['forbidden'] == {'edit.txt':'BEFORE\n', 'multi.txt':'BEFORE\nmiddle\nBEFORE\n'})
                last_hook = store.get_session(thread)['last_tool']
                passed = passed and bool(last_hook and last_hook['label'].endswith('/forbidden/resumed.txt'))
                report = {'last_hook':last_hook, 'mode':mode, 'native_id':native, 'passed':passed, 'files':files, 'permission_count':len(permissions)}
                (workspace / 'events.json').write_text(json.dumps(events, indent=2))
                reports.append(report)
                (root / 'result.json').write_text(json.dumps(reports, indent=2))
                print(json.dumps(report), flush=True)
        finally:
            if connection:
                connection.close()
    return 0 if all(r['passed'] for r in reports) else 1


if __name__ == '__main__':
    raise SystemExit(main())
