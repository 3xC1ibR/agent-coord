#!/usr/bin/env python3
"""Exercise packaged Python children without relying on inherited bytecode flags."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile


PROBE = r'''
import json, os, shlex, subprocess, sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
resources, directory = map(Path, sys.argv[1:])
sys.path.insert(0, str(resources / 'backend'))
from agent_coord import antigravity, coordination_metrics, desktop_runtime, jobs, kimi_setup, managed_pty, muse_setup, zellij_wake
from agent_coord.store import CoordinationStore
home = directory / 'home'
home.mkdir()
workspace = directory / 'workspace'
workspace.mkdir()
environment = {'HOME': str(home), 'PATH': os.defpath, 'XDG_CONFIG_HOME': str(home / 'config')}
os.environ.clear()
os.environ.update(environment, KIMI_CODE_HOME=str(home / 'kimi'))
store = CoordinationStore(directory / 'state.sqlite3')
results = []
def run(name, command, *, env=None, data=None):
    outcome = subprocess.run(command, shell=isinstance(command, str), cwd=workspace,
        env=environment if env is None else env, input=data, capture_output=True, text=True, timeout=30)
    results.append({'name': name, 'returncode': outcome.returncode,
        'stdout': outcome.stdout[:1000], 'stderr': outcome.stderr[:1000]})
for provider in ('muse', 'kimi', 'antigravity'):
    session = 'signed-resource-' + provider
    store.register(session_id=session, client=provider, cwd=str(workspace))
    if provider == 'muse':
        hooks = json.loads(muse_setup.hook_config(store, session).removeprefix('hooks='))
        command = hooks['UserPromptSubmit'][-1]['hooks'][0]['command']
        env = environment
        event, tool = 'UserPromptSubmit', 'read_file'
    elif provider == 'kimi':
        kimi_setup.ensure_hooks()
        configured = (home / 'kimi/config.toml').read_text()
        command = json.loads(next(line[len('command = '):] for line in configured.splitlines() if line.startswith('command = "if ')))
        env = dict(environment, AGENT_COORD_CLIENT=provider, AGENT_COORD_SESSION_ID=session, AGENT_COORD_DB=str(store.database_path))
        event, tool = 'UserPromptSubmit', 'ReadFile'
    else:
        profile = directory / 'antigravity'
        path = antigravity.ensure_coordination_hooks(profile)
        command = json.loads(path.read_text())['ribbon-field-coordination']['PreToolUse'][0]['hooks'][0]['command']
        env = dict(environment, AGENT_COORD_SESSION_ID=session, AGENT_COORD_DB=str(store.database_path))
        event, tool = 'PreToolUse', 'view_file'
    payload = json.dumps({'hook_event_name': event, 'session_id': 'private-native-id',
        'cwd': str(workspace), 'prompt': 'Private signature regression', 'tool_name': tool,
        'tool_input': {'file_path': str(workspace / 'probe.txt')},
        'toolCall': {'name': 'write_to_file', 'args': {'TargetFile': str(workspace / 'probe.txt')}}})
    run(provider + '-generated-hook', command, env=env, data=payload)
    results[-1]['explicit_bytecode_flag'] = '-B' in shlex.split(command)
    response = json.loads(results[-1]['stdout']) if results[-1]['returncode'] == 0 else {}
    results[-1]['hook_accepted'] = response.get('decision') != 'deny' and response.get('hookSpecificOutput', {}).get('permissionDecision') != 'deny'
# Execute real packaged supervisor entry points, requesting help only. This
# catches shell launchers accidentally passed to Python without starting work.
command = managed_pty.build_supervisor_command(store, 'unused')
script = next(i for i, value in enumerate(command[1:], 1) if not value.startswith('-'))
run('managed-supervisor-entry', command[:script + 1] + ['--help'])
results[-1]['explicit_bytecode_flag'] = '-B' in command[:script]
command = jobs.build_supervisor_command(store, 'unused', 'private-token')
module = command.index('-m')
run('job-supervisor-entry', command[:module + 2] + ['--help'], env=dict(environment, PYTHONPATH=str(resources / 'backend')))
results[-1]['explicit_bytecode_flag'] = '-B' in command[:module]
# Capture real watcher/worker commands while substituting process creation.
# Only their harmless entry points execute; no watcher, metric scan or native
# provider is started by this resource-integrity check.
captured = {}
def watcher(command, **options):
    captured['watcher'] = command
    return SimpleNamespace(pid=2147483647)
store.register(session_id='signed-resource-watcher', client='codex', cwd=str(workspace))
with patch.object(zellij_wake.shutil, 'which', return_value='/private/probe/zellij'):
    zellij_wake.enable_zellij_wake(store, 'signed-resource-watcher',
        zellij_session='private-probe', pane_id='1', popen=watcher)
command = captured['watcher']
script = next(i for i, value in enumerate(command[1:], 1) if not value.startswith('-'))
run('zellij-supervisor-entry', command[:script + 1] + ['--help'])
results[-1]['explicit_bytecode_flag'] = '-B' in command[:script]
def worker(command, **options):
    captured['worker'] = command
    return SimpleNamespace(returncode=0, poll=lambda: 0)
service = coordination_metrics.CoordinationMetrics(directory / 'metrics.sqlite3',
    python=sys.executable, codex_home=home / 'codex', popen=worker)
service._run()
service.close()
command = captured['worker']
command = command[command.index(sys.executable):]  # Preserve the interpreter prefix below taskpolicy.
module = command.index('-m')
run('metrics-worker-entry', command[:module + 2] + ['--help'], env=dict(environment, PYTHONPATH=str(resources / 'backend')))
results[-1]['explicit_bytecode_flag'] = '-B' in command[:module]
def browser(connection, callback, **options):
    captured['browser'] = options['env']['BROWSER']
    connection.channel = None
with patch.object(antigravity, 'resolve_runtime', return_value=(directory / 'native', directory / 'harness')), \
        patch.object(antigravity.ACPConnection, '__init__', browser):
    connection = antigravity.AntigravityConnection(store, lambda event: None, {'cwd': str(workspace)})
try:
    command = shlex.split(captured['browser'])
    run('antigravity-browser-helper', command[:-1] + ['https://accounts.google.com/private-probe'])
    results[-1]['explicit_bytecode_flag'] = '-B' in command
finally:
    connection.temporary.cleanup()
# A backend helper is observed through its actual interpreter prefix, replacing
# its body with an import probe so no runtime or provider process is started.
command = desktop_runtime.payload_command(resources)
script = next(i for i, value in enumerate(command[1:], 1) if not value.startswith('-'))
run('backend-child-interpreter', command[:script] + ['-c',
    'import json,sys,ssl,sqlite3; print(json.dumps({"executable":sys.executable,"bytecode_disabled":sys.dont_write_bytecode}))'])
probe = json.loads(results[-1]['stdout']) if results[-1]['returncode'] == 0 else {}
results[-1]['bytecode_disabled'] = probe.get('bytecode_disabled', False)
results[-1]['normal_executable'] = Path(probe.get('executable', '/missing')).resolve() == Path(sys.executable).resolve()
print(json.dumps({'executable': sys.executable, 'cases': results}))
'''


def snapshot(resources: Path) -> dict[str, str]:
    result = {}
    for path in sorted(resources.rglob('*')):
        if path.is_symlink():
            result[str(path.relative_to(resources))] = 'link:' + os.readlink(path)
        elif path.is_file():
            digest = hashlib.sha256()
            with path.open('rb') as content:
                for block in iter(lambda: content.read(1024 * 1024), b''):
                    digest.update(block)
            result[str(path.relative_to(resources))] = digest.hexdigest()
    return result


def signature(app: Path) -> dict:
    checked = subprocess.run(['codesign', '--verify', '--deep', '--strict', str(app)],
        capture_output=True, text=True)
    return {'valid': checked.returncode == 0, 'returncode': checked.returncode,
        'output': checked.stdout + checked.stderr}


def validate(app: Path, *, report: Path | None = None) -> dict:
    app = app.resolve()
    resources = app / 'Contents/Resources'
    python = resources / 'runtime/bin/python3'
    result = {'app': str(app), 'signature_before': signature(app)}
    if not result['signature_before']['valid']:
        raise RuntimeError('Refusing to exercise a package with an invalid initial signature.')
    before = snapshot(resources)
    with tempfile.TemporaryDirectory(prefix='python-resource-probe-', dir=app.parent) as temporary:
        environment = {'HOME': temporary, 'PATH': os.defpath, 'TMPDIR': temporary}
        driver = subprocess.run([str(python), '-I', '-B', '-c', PROBE, str(resources), temporary],
            env=environment, cwd=temporary, capture_output=True, text=True, timeout=180)
        result['driver_returncode'] = driver.returncode
        if driver.returncode == 0:
            result.update(json.loads(driver.stdout))
        else:
            result['driver_error'] = driver.stderr
    after = snapshot(resources)
    result['resources_added'] = sorted(after.keys() - before.keys())
    result['resources_removed'] = sorted(before.keys() - after.keys())
    result['resources_changed'] = sorted(key for key in before.keys() & after.keys() if before[key] != after[key])
    result['signature_after'] = signature(app)
    expected = {'muse-generated-hook', 'kimi-generated-hook', 'antigravity-generated-hook',
        'managed-supervisor-entry', 'job-supervisor-entry', 'zellij-supervisor-entry',
        'metrics-worker-entry', 'antigravity-browser-helper', 'backend-child-interpreter'}
    result['passed'] = (driver.returncode == 0 and result['signature_after']['valid']
        and {case['name'] for case in result.get('cases', [])} == expected
        and not any(result[name] for name in ('resources_added', 'resources_removed', 'resources_changed'))
        and all(case['returncode'] == 0 and case.get('explicit_bytecode_flag', True)
            and case.get('bytecode_disabled', True) and case.get('normal_executable', True)
            and case.get('hook_accepted', True)
            for case in result.get('cases', [])))
    if report:
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(result, indent=2) + '\n')
    if not result['passed']:
        raise RuntimeError('Packaged Python child launches changed signed resources or failed their regression checks.')
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', required=True, type=Path)
    parser.add_argument('--report', required=True, type=Path)
    options = parser.parse_args()
    try:
        validate(options.app, report=options.report)
    except RuntimeError as error:
        parser.exit(1, str(error) + '\n')
    print(options.report)


if __name__ == '__main__':
    main()
