#!/usr/bin/env python3
"""Exercise bounded messaging under retries, restarts and ambiguous submission.

This is a deterministic local transport workload, not a model/provider benchmark.
The report records content-free queue/delivery measurements and correctness gates.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'plugins/agent-coord/scripts'))
from agent_coord.store import CoordinationStore
from agent_coord.message_delivery import MessageDelivery, MAX_CHARS, MAX_MESSAGES


def measure(count=100):
    with tempfile.TemporaryDirectory(prefix='agent-message-measurement-') as directory:
        database = Path(directory) / 'state.sqlite3'
        store = CoordinationStore(database)
        for identity in ('sender', 'recipient'):
            store.register(session_id=identity, client='codex', cwd=directory)
        delivery = MessageDelivery(store)
        ids, failures, seen, sizes = [], [], [], []
        for number in range(count):
            arguments = dict(sender_session_id='sender', recipient_session_id='recipient',
                             body=('Test instruction.\n' * (1 + number % 50)) + str(number),
                             reply_required=False, request_id=f'request-{number}')
            result = store.send_message(**arguments)
            ids.append(result['id'])
            if store.send_message(**arguments)['id'] != result['id']:
                failures.append('retry_created_duplicate')
        batches = 0
        while batch := delivery.prepare('recipient', f'prompt-{batches}'):
            token = f'prompt-{batches}'
            channel = 'hook' if batches % 2 else 'prompt'
            delivery.attempted(token, batch, channel=channel, provider_turn_id=token)
            # First submission is rejected before dispatch: release then retry.
            if batches == 0:
                delivery.release(token)
                delivery.attempted(token, batch, channel=channel, provider_turn_id=token)
            if channel == 'hook':
                delivery.finish_turn('recipient', token, completed=True)
            else:
                delivery.submitted('recipient', token, batch, provider_turn_id=token)
            seen.extend(batch['ids'])
            sizes.append(len(batch['text']))
            if len(batch['ids']) > MAX_MESSAGES or sizes[-1] > MAX_CHARS:
                failures.append('batch_limit_exceeded')
            batches += 1
        if seen != ids:
            failures.append('missing_duplicate_or_reordered_delivery')
        ambiguous = store.send_message(sender_session_id='sender', recipient_session_id='recipient', body='Inspect before retrying', reply_required=False)
        batch = delivery.prepare('recipient', 'uncertain')
        delivery.attempted('uncertain', batch)
        delivery.failed('uncertain', batch, 'Simulated lost provider response')
        reopened = MessageDelivery(CoordinationStore(database))
        if reopened.prepare('recipient', 'after-restart') is not None:
            failures.append('uncertain_submission_replayed')
        if reopened.read('sender', ambiguous['id'])['delivery']['state'] != 'uncertain':
            failures.append('uncertainty_not_preserved')
        metrics = reopened.metrics('recipient')
        if store.inbox('recipient', unread_only=True, mark_delivered=False)[0]['id'] != ambiguous['id']:
            failures.append('recovery_body_missing')
        revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
        return {'revision': revision, 'worktree_modified': bool(subprocess.check_output(['git', 'diff', '--name-only'], cwd=ROOT, text=True)),
                'workload': {'messages': count, 'send_attempts': count * 2, 'paths': ['prompt', 'hook'],
                             'pre_submission_rejections': 1, 'ambiguous_submissions': 1, 'restarts': 1,
                             'providers': 'simulated; no model turns'},
                'batches': batches, 'maximum_batch_characters': max(sizes, default=0),
                'duplicate_deliveries': len(seen) - len(set(seen)),
                'missing_deliveries': len(set(ids) - set(seen)), 'receipt_commands_required': 0,
                'metrics': metrics, 'failures': failures}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    result = measure()
    encoded = json.dumps(result, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + '\n')
    print(encoded)
    return int(args.check and bool(result['failures']))


if __name__ == '__main__':
    raise SystemExit(main())
