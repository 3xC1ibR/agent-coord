"""Keep display timing stable across streams, provider reads, and old histories."""
import json
import math
import os
import uuid
from datetime import datetime
from pathlib import Path


def seconds(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        stamp = float(value) if isinstance(value, (int, float)) else datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
        return (stamp / 1000 if stamp > 1e12 else stamp) if math.isfinite(stamp) else None
    except (ValueError, TypeError, OverflowError):
        return None


def set_time(record, key, value):
    if seconds(record.get(key)) is None and seconds(value) is not None:
        record[key] = seconds(value)


def preserve_timing(thread, saved):
    """Provider snapshots may omit our timing, but must not erase it."""
    previous = {turn['id']: turn for turn in saved.get('turns', []) if turn.get('id')}
    for turn in thread.get('turns', []):
        if not turn.get('id'):
            continue
        old = previous.get(turn['id'], {})
        for key in ('startedAt', 'completedAt'):
            if seconds(turn.get(key)) is None and seconds(old.get(key)) is not None:
                turn[key] = old[key]
        items = {item['id']: item for item in old.get('items', []) if item.get('id')}
        for item in turn.get('items', []):
            stamp = items.get(item.get('id'), {}).get('timelineAt')
            if seconds(item.get('timelineAt')) is None and seconds(stamp) is not None:
                item['timelineAt'] = stamp


def _records(session_id, client):
    try:
        session_id = str(uuid.UUID(session_id))
    except (ValueError, TypeError):
        return
    if client == 'claude':
        root = Path(os.environ.get('CLAUDE_CONFIG_DIR', Path.home() / '.claude'))
        paths = list((root / 'projects').glob(f'*/{session_id}.jsonl'))
    else:
        root = Path(os.environ.get('CODEX_HOME', Path.home() / '.codex'))
        paths = [path for folder in ('sessions', 'archived_sessions')
                 for path in (root / folder).glob(f'**/*-{session_id}.jsonl')]
    if len(paths) != 1:
        return
    try:
        with paths[0].open(encoding='utf-8') as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if isinstance(record, dict):
                    yield record
    except (OSError, UnicodeError):
        return


def restore_timing(store, thread):
    """Recover legacy starts by exact turn/user IDs, never by guessing text."""
    turns = [turn for turn in thread.get('turns', []) if turn.get('id')]
    missing = [turn for turn in turns if seconds(turn.get('startedAt')) is None]
    if not missing:
        return
    session_id = thread['id']
    with store._connection() as db:
        session = db.execute('SELECT client FROM sessions WHERE session_id = ?', (session_id,)).fetchone()
        latest = db.execute('SELECT turn_id, turn_started_at FROM work_threads WHERE thread_id = ?', (session_id,)).fetchone()
        ends = dict(db.execute('SELECT turn_key, completed_at FROM turn_completions WHERE thread_id = ?', (session_id,)))
    by_id = {turn['id']: turn for turn in turns}
    users = {item['id']: turn for turn in turns for item in turn.get('items', []) if item.get('type') == 'userMessage' and item.get('id')}
    items = {item['id']: item for turn in turns for item in turn.get('items', []) if item.get('id')}
    for record in _records(session_id, session['client'] if session else 'codex'):
        stamp = seconds(record.get('timestamp'))
        if stamp is None:
            continue
        if record.get('type') == 'user' and record.get('uuid') in users:
            turn = users[record['uuid']]
            set_time(turn, 'startedAt', stamp)
            set_time(items[record['uuid']], 'timelineAt', stamp)
        payload = record.get('payload')
        if isinstance(payload, dict) and payload.get('turn_id') in by_id:
            turn = by_id[payload['turn_id']]
            if record.get('type') == 'event_msg' and payload.get('type') == 'task_started':
                set_time(turn, 'startedAt', seconds(payload.get('started_at')) or stamp)
            elif record.get('type') == 'turn_context':
                set_time(turn, 'startedAt', stamp)
        message = record.get('message')
        if record.get('type') == 'assistant' and isinstance(message, dict):
            for block in message.get('content', []):
                if isinstance(block, dict) and block.get('type') == 'tool_use' and block.get('id') in items:
                    set_time(items[block['id']], 'timelineAt', stamp)
    for turn in turns:
        if latest and turn['id'] == latest['turn_id'] and latest['turn_started_at'] is not None:
            set_time(turn, 'startedAt', latest['turn_started_at'])
        if turn['id'] in ends:
            set_time(turn, 'completedAt', ends[turn['id']])
        for item in turn.get('items', []):
            if item.get('type') == 'userMessage' and seconds(turn.get('startedAt')) is not None:
                set_time(item, 'timelineAt', turn['startedAt'])
