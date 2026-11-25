"""Keep display timing stable across streams, provider reads, and old histories."""
from collections import OrderedDict
import json
import math
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path

_TIMING_INDEX_LOCK = threading.Lock()
_TIMING_INDEX_CACHE: OrderedDict[tuple[str, int, int], tuple[dict, dict, dict]] = OrderedDict()


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


def _timing_index(session_id, client):
    try:
        normalized = str(uuid.UUID(session_id))
    except (ValueError, TypeError):
        return {}, {}, {}
    if client == 'claude':
        root = Path(os.environ.get('CLAUDE_CONFIG_DIR', Path.home() / '.claude'))
        paths = list((root / 'projects').glob(f'*/{normalized}.jsonl'))
    else:
        root = Path(os.environ.get('CODEX_HOME', Path.home() / '.codex'))
        paths = [path for folder in ('sessions', 'archived_sessions')
                 for path in (root / folder).glob(f'**/*-{normalized}.jsonl')]
    if len(paths) != 1:
        return {}, {}, {}
    target = paths[0]
    try:
        st = target.stat()
    except OSError:
        return {}, {}, {}
    cache_key = (str(target), st.st_mtime_ns, st.st_size)
    with _TIMING_INDEX_LOCK:
        cached = _TIMING_INDEX_CACHE.get(cache_key)
        if cached is not None:
            _TIMING_INDEX_CACHE.move_to_end(cache_key)
            return cached
    user_stamps, turn_stamps, tool_stamps = {}, {}, {}
    for record in _records(session_id, client):
        stamp = seconds(record.get('timestamp'))
        if stamp is None:
            continue
        rtype = record.get('type')
        if rtype == 'user' and record.get('uuid'):
            user_stamps.setdefault(record['uuid'], stamp)
        payload = record.get('payload')
        if isinstance(payload, dict) and payload.get('turn_id'):
            tid = payload['turn_id']
            if rtype == 'event_msg' and payload.get('type') == 'task_started':
                turn_stamps.setdefault(tid, seconds(payload.get('started_at')) or stamp)
            elif rtype == 'turn_context':
                turn_stamps.setdefault(tid, stamp)
        message = record.get('message')
        if rtype == 'assistant' and isinstance(message, dict):
            for block in message.get('content', []):
                if isinstance(block, dict) and block.get('type') == 'tool_use' and block.get('id'):
                    tool_stamps.setdefault(block['id'], stamp)
    result = (user_stamps, turn_stamps, tool_stamps)
    with _TIMING_INDEX_LOCK:
        _TIMING_INDEX_CACHE[cache_key] = result
        _TIMING_INDEX_CACHE.move_to_end(cache_key)
        while len(_TIMING_INDEX_CACHE) > 64:
            _TIMING_INDEX_CACHE.popitem(last=False)
    return result


def restore_timing(store, thread):
    """Recover legacy starts by exact turn/user IDs, never by guessing text."""
    turns = [turn for turn in thread.get('turns', []) if turn.get('id')]
    missing = [turn for turn in turns if seconds(turn.get('startedAt')) is None]
    if not missing:
        return
    session_id = thread['id']
    with store.reading(), store._connection() as db:
        session = db.execute('SELECT client FROM sessions WHERE session_id = ?', (session_id,)).fetchone()
        latest = db.execute('SELECT turn_id, turn_started_at FROM work_threads WHERE thread_id = ?', (session_id,)).fetchone()
        ends = dict(db.execute('SELECT turn_key, completed_at FROM turn_completions WHERE thread_id = ?', (session_id,)))
    user_stamps, turn_stamps, tool_stamps = _timing_index(session_id, session['client'] if session else 'codex')
    for turn in turns:
        tid = turn['id']
        if tid in turn_stamps:
            set_time(turn, 'startedAt', turn_stamps[tid])
        for item in turn.get('items', []):
            iid = item.get('id')
            if not iid:
                continue
            if item.get('type') == 'userMessage' and iid in user_stamps:
                set_time(turn, 'startedAt', user_stamps[iid])
                set_time(item, 'timelineAt', user_stamps[iid])
            elif iid in tool_stamps:
                set_time(item, 'timelineAt', tool_stamps[iid])
    for turn in turns:
        if latest and turn['id'] == latest['turn_id'] and latest['turn_started_at'] is not None:
            set_time(turn, 'startedAt', latest['turn_started_at'])
        if turn['id'] in ends:
            set_time(turn, 'completedAt', ends[turn['id']])
        for item in turn.get('items', []):
            if item.get('type') == 'userMessage' and seconds(turn.get('startedAt')) is not None:
                set_time(item, 'timelineAt', turn['startedAt'])
