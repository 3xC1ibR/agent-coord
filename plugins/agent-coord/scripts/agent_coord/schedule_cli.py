"""CLI schedule commands share the MCP request contract and caller binding."""
from __future__ import annotations

import json

from .context import read_input
from .store import CoordinationError


def add_parser(commands):
    schedule = commands.add_parser('schedule', help='Create and manage saved Ribbon Field scheduled prompts.')
    actions = schedule.add_subparsers(dest='schedule_action', required=True)
    for action in ('create', 'list', 'read', 'edit', 'pause', 'resume', 'cancel', 'run-now'):
        command = actions.add_parser(action, help={
            'create': 'Save a prompt; new-agent settings require cwd, client and model.',
            'list': 'List schedules created by or targeting the caller.',
            'read': 'Read one schedule and its current version.',
            'edit': 'Edit selected fields with the current version.',
            'pause': 'Pause future recurring runs.', 'resume': 'Resume a paused recurrence, skipping missed runs.',
            'cancel': 'Cancel a prompt or stop future recurring runs.',
            'run-now': 'Run a missed prompt once; delivery uncertainty still requires review.',
        }[action])
        command.add_argument('--from-session', help='Caller identity assertion; defaults to the inferred caller.')
        command.add_argument('--request-id', help='Stable idempotency key; reuse with identical arguments on retry.')
        command.add_argument('--wait', type=float, default=5, help='Wait 0–30 seconds for the app receipt (default 5).')
        if action not in {'create', 'list'}:
            command.add_argument('--schedule-id', required=True)
            command.add_argument('--authorized-schedule-id', help='Use only with explicit user instruction naming this exact schedule ID.')
            if action != 'read':
                command.add_argument('--version', type=int, required=True, help='Current version from list/read.')
        if action == 'create':
            command.add_argument('--mode', choices=('self', 'new'), required=True)
        if action in {'create', 'edit'}:
            command.add_argument('--message', required=action == 'create', help='Prompt text; - reads stdin.')
            command.add_argument('--run-at', type=float, required=action == 'create', help='Future Unix timestamp in seconds.')
            command.add_argument('--timezone', required=action == 'create', help='IANA time zone.')
            command.add_argument('--recurrence', help='JSON recurrence object; null removes repetition when editing.')
            command.add_argument('--settings', help='JSON provider settings; edits accept partial settings, including yolo.')
        if action in {'create', 'edit', 'resume', 'run-now'}:
            command.add_argument('--yolo-authorized', action='store_true', help='Confirm explicit user authorization for the resulting YOLO execution.')


def execute(arguments, store):
    from .checkpoint_mcp import schedule_prompt
    payload = {'action': arguments.schedule_action.replace('-', '_'), 'session_id': arguments.from_session,
               'wait_seconds': arguments.wait}
    for key in ('request_id', 'schedule_id', 'authorized_schedule_id', 'version', 'mode',
                'message', 'run_at', 'timezone', 'recurrence', 'settings'):
        value = getattr(arguments, key, None)
        if value is None:
            continue
        if key == 'message' and value == '-':
            value = read_input(100001)
        elif key in {'settings', 'recurrence'}:
            try:
                value = json.loads(value)
            except ValueError as exc:
                raise CoordinationError(f'{key} must be valid JSON.') from exc
        payload[key] = value
    if getattr(arguments, 'yolo_authorized', False):
        payload['yolo_authorized'] = True
    return schedule_prompt(payload, store=store)
