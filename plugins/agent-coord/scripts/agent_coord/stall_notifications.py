"""Durable, exact-turn stall incidents and recurring coordination/user alerts.

This module records observations; it never probes providers, delivers provider
input, cancels tools or infers completion from silence. Times are Unix seconds.
The writer transaction commits each alert, its inbox message and its audit
receipt together. Notification transports retain their own delivery receipts.
"""
from __future__ import annotations

import json
import math
from urllib.parse import quote

from . import supervision
from .workspaces import matches_workspace

CLASSIFICATIONS = {'stalled', 'unverified_tool', 'long_running_tool', 'waiting_for_approval'}
MAX_INTERVAL = 2400


def _exists(db):
    return db.execute("SELECT 1 FROM sqlite_master WHERE name='app_agent_stall_incidents'").fetchone() is not None


def _tables(db):
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def initialize(db):
    # Individual statements preserve the caller's transaction; executescript commits it.
    db.execute('''CREATE TABLE IF NOT EXISTS app_agent_stall_incidents (
        id INTEGER PRIMARY KEY AUTOINCREMENT, thread_id TEXT NOT NULL, turn_id TEXT NOT NULL,
        progress_at REAL NOT NULL, classification TEXT NOT NULL, detail TEXT NOT NULL,
        created_at REAL NOT NULL, next_alert_at REAL NOT NULL, alert_count INTEGER NOT NULL DEFAULT 0,
        last_alert_at REAL, last_user_alert_at REAL, resolved_at REAL, resolution TEXT)''')
    db.execute('''CREATE UNIQUE INDEX IF NOT EXISTS app_agent_stall_open
        ON app_agent_stall_incidents(thread_id, turn_id) WHERE resolved_at IS NULL''')
    db.execute('''CREATE TABLE IF NOT EXISTS app_agent_stall_progress (
        thread_id TEXT NOT NULL, turn_id TEXT NOT NULL, progress_at REAL NOT NULL,
        closed INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(thread_id, turn_id))''')
    db.execute('''CREATE TABLE IF NOT EXISTS app_agent_stall_alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, incident_id INTEGER NOT NULL,
        sequence INTEGER NOT NULL, created_at REAL NOT NULL, supervisor_id TEXT, message_id INTEGER,
        user_escalated INTEGER NOT NULL, claimed_at REAL, body_json TEXT NOT NULL,
        UNIQUE(incident_id, sequence))''')


# Completion and Stop are authoritative even if an observer's clear callback was lost.
_LIVE = '''i.resolved_at IS NULL AND t.turn_id=i.turn_id AND t.attention!='archived'
    AND NOT EXISTS (SELECT 1 FROM turn_completions c WHERE c.thread_id=i.thread_id AND c.turn_key=t.turn_key)
    AND NOT EXISTS (SELECT 1 FROM turn_stop_requests s WHERE s.thread_id=i.thread_id AND s.turn_id=i.turn_id)'''

# Fold the inventory fields into its existing indexed base query. This also
# keeps detail/list projections identical without another round trip per row.
_INCIDENT_FIELDS = ('id', 'thread_id', 'turn_id', 'progress_at', 'classification', 'detail',
                    'created_at', 'next_alert_at', 'alert_count', 'last_alert_at', 'last_user_alert_at')
THREAD_FIELDS_SQL = '''(SELECT json_object('count', COUNT(*), 'oldest', MIN(created_at))
    FROM messages WHERE recipient_session_id=t.thread_id AND classification='action_required'
    AND delivered_at IS NULL AND suppressed_at IS NULL) AS stall_inbox_json,
    (SELECT json_object(''' + ', '.join(f"'{field}', i.{field}" for field in _INCIDENT_FIELDS) + ''')
    FROM app_agent_stall_incidents i WHERE i.thread_id=t.thread_id AND i.alert_count>0
    AND ''' + _LIVE + ') AS stall_incident_json'


def projected_fields(row, now):
    inbox = json.loads(row.pop('stall_inbox_json'))
    incident = row.pop('stall_incident_json')
    return {'actionable_inbox_count': inbox['count'], 'oldest_actionable_at': inbox['oldest'],
            'oldest_actionable_age_seconds': max(0, now - inbox['oldest']) if inbox['oldest'] is not None else None,
            'stall_incident': json.loads(incident) if incident else None}


def _live_turn(db, thread_id, turn_id):
    return db.execute('''SELECT t.* FROM work_threads t WHERE t.thread_id=? AND t.turn_id=?
        AND t.attention!='archived'
        AND NOT EXISTS (SELECT 1 FROM turn_completions c WHERE c.thread_id=t.thread_id AND c.turn_key=t.turn_key)
        AND NOT EXISTS (SELECT 1 FROM turn_stop_requests s WHERE s.thread_id=t.thread_id AND s.turn_id=t.turn_id)''',
        (thread_id, turn_id)).fetchone()


def _suppress(db, incident_id, now):
    db.execute('''UPDATE messages SET suppressed_at=?, delivered_at=? WHERE id IN (
        SELECT message_id FROM app_agent_stall_alerts WHERE incident_id=? ORDER BY sequence DESC LIMIT 1)
        AND delivered_at IS NULL AND suppressed_at IS NULL''', (now, now, incident_id))


def clear_in_transaction(db, thread_id, turn_id, *, now, reason):
    """Lifecycle integration: use the caller's connection, without schema creation."""
    if not turn_id or not _exists(db):
        return
    rows = db.execute('''SELECT id FROM app_agent_stall_incidents
        WHERE thread_id=? AND turn_id=? AND resolved_at IS NULL''', (thread_id, turn_id)).fetchall()
    for row in rows:
        _suppress(db, row['id'], now)
    db.execute('''UPDATE app_agent_stall_incidents SET resolved_at=?, resolution=?
        WHERE thread_id=? AND turn_id=? AND resolved_at IS NULL''', (now, reason, thread_id, turn_id))
    db.execute('UPDATE app_agent_stall_progress SET closed=1 WHERE thread_id=? AND turn_id=?', (thread_id, turn_id))


def clear_stall(store, thread_id, turn_id, *, reason):
    with store.database_operation('stall.clear', priority='background'), store._connection() as db:
        db.execute('BEGIN IMMEDIATE')
        clear_in_transaction(db, thread_id, turn_id, now=store.clock(), reason=str(reason)[:200])


def record_progress(store, thread_id, turn_id, *, progress_at):
    """Close an episode only on newer confirmed progress; fence delayed observations."""
    if progress_at is None or not math.isfinite(progress_at):
        return
    with store.database_operation('stall.progress', priority='background'), store._connection() as db:
        db.execute('BEGIN IMMEDIATE')
        if not _exists(db):
            return
        prior = db.execute('SELECT progress_at FROM app_agent_stall_progress WHERE thread_id=? AND turn_id=?',
                           (thread_id, turn_id)).fetchone()
        if prior and progress_at <= prior['progress_at']:
            return
        clear_in_transaction(db, thread_id, turn_id, now=store.clock(), reason='progress')
        db.execute('''INSERT INTO app_agent_stall_progress VALUES (?, ?, ?, 0)
            ON CONFLICT(thread_id, turn_id) DO UPDATE SET progress_at=excluded.progress_at, closed=0''',
            (thread_id, turn_id, progress_at))


def _supervisor(db, store, thread_id):
    recipient = supervision.effective_supervisor(db, thread_id)
    if not recipient or recipient == thread_id or supervision.passing_from(db, recipient, thread_id):
        return None, False
    tables = _tables(db)
    pause = ("EXISTS (SELECT 1 FROM browser_inbox_wake w WHERE w.thread_id=s.session_id AND w.paused=1)"
             if 'browser_inbox_wake' in tables else '0')
    browser = ("EXISTS (SELECT 1 FROM browser_sessions b WHERE b.thread_id=s.session_id)"
               if 'browser_sessions' in tables else '0')
    row = db.execute(f'''SELECT s.ended_at, s.last_seen_at, t.attention, {pause} AS paused, {browser} AS browser,
        c.status AS completion_status, s.turn_active FROM sessions s JOIN work_threads t ON t.thread_id=s.session_id
        LEFT JOIN turn_completions c ON c.thread_id=t.thread_id AND c.turn_key=t.turn_key WHERE s.session_id=?''',
        (recipient,)).fetchone()
    available = bool(row and row['attention'] != 'archived' and not row['paused']
                     and (row['turn_active'] or row['completion_status'] not in {'failed', 'interrupted'})
                     and (row['browser'] or row['ended_at'] is None and row['last_seen_at'] >= store.clock() - store.stale_after_seconds))
    return recipient, available


def inbox_fields(db, thread_ids, now):
    """Indexed, batched aggregates; never load message bodies into the inventory."""
    result = {identity: {'actionable_inbox_count': 0, 'oldest_actionable_at': None,
                         'oldest_actionable_age_seconds': None} for identity in thread_ids}
    identities = list(result)
    for offset in range(0, len(identities), 400):
        batch = identities[offset:offset + 400]
        rows = db.execute('''SELECT recipient_session_id, COUNT(*) AS count, MIN(created_at) AS oldest
            FROM messages WHERE recipient_session_id IN (''' + ','.join('?' for _ in batch) + ''')
            AND classification='action_required' AND delivered_at IS NULL AND suppressed_at IS NULL
            GROUP BY recipient_session_id''', batch)
        for row in rows:
            result[row['recipient_session_id']] = {'actionable_inbox_count': row['count'],
                'oldest_actionable_at': row['oldest'], 'oldest_actionable_age_seconds': max(0, now - row['oldest'])}
    return result


def record_stall(store, thread_id, turn_id, *, progress_at, detail, classification):
    """Record an observation and atomically return a due alert, or None.

    None progress retains an already recorded epoch after restart. Notifications
    repeat at a maximum interval of 40 minutes, without a finite attempt limit.
    A late poll creates one reminder, then schedules from now (no catch-up burst).
    """
    if classification not in CLASSIFICATIONS:
        raise ValueError('Unknown stall classification')
    now = store.clock()
    if progress_at is not None and (not math.isfinite(progress_at) or progress_at > now):
        raise ValueError('progress_at must be a finite Unix timestamp no later than now')
    detail = (detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=True))[:4000]
    allowance = 1200 if classification in {'long_running_tool', 'unverified_tool'} else 600
    with store.database_operation('stall.record', priority='background'), store._connection() as db:
        db.execute('BEGIN IMMEDIATE')
        if not _live_turn(db, thread_id, turn_id):
            clear_in_transaction(db, thread_id, turn_id, now=now, reason='turn_inactive')
            return None
        incident = db.execute('''SELECT * FROM app_agent_stall_incidents
            WHERE thread_id=? AND turn_id=? AND resolved_at IS NULL''', (thread_id, turn_id)).fetchone()
        if progress_at is None:
            if not incident:
                return None
            progress_at = incident['progress_at']
        floor = db.execute('SELECT * FROM app_agent_stall_progress WHERE thread_id=? AND turn_id=?',
                           (thread_id, turn_id)).fetchone()
        if floor and (progress_at < floor['progress_at'] or floor['closed'] and progress_at <= floor['progress_at']):
            return None
        if incident and progress_at > incident['progress_at']:
            clear_in_transaction(db, thread_id, turn_id, now=now, reason='progress')
            incident = None
        if not incident:
            db.execute('''INSERT INTO app_agent_stall_progress VALUES (?, ?, ?, 0)
                ON CONFLICT(thread_id, turn_id) DO UPDATE SET progress_at=excluded.progress_at, closed=0''',
                (thread_id, turn_id, progress_at))
            identifier = db.execute('''INSERT INTO app_agent_stall_incidents
                (thread_id, turn_id, progress_at, classification, detail, created_at, next_alert_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)''',
                (thread_id, turn_id, progress_at, classification, detail, now, progress_at + allowance)).lastrowid
            incident = db.execute('SELECT * FROM app_agent_stall_incidents WHERE id=?', (identifier,)).fetchone()
        due_at = incident['next_alert_at'] if incident['alert_count'] else progress_at + allowance
        db.execute('UPDATE app_agent_stall_incidents SET classification=?, detail=?, next_alert_at=? WHERE id=?',
                   (classification, detail, due_at, incident['id']))
        if now < due_at:
            return None
        sequence = incident['alert_count'] + 1
        interval = min(MAX_INTERVAL, allowance * 2 ** min(sequence - 1, 2))
        supervisor_id, available = _supervisor(db, store, thread_id)
        escalated = not available or sequence >= 2 or incident['last_user_alert_at'] is not None
        counts = inbox_fields(db, [thread_id], now)[thread_id]
        body = {'event': 'app_agent_unattended_approval' if classification == 'waiting_for_approval' else 'app_agent_stalled',
                'source': 'runtime', 'stage': 'watchdog', 'session_id': thread_id, 'turn_id': turn_id,
                'incident_id': incident['id'], 'sequence': sequence, 'progress_at': progress_at,
                'inactivity_seconds': max(0, now - progress_at), 'classification': classification,
                'detail': detail, 'error': detail, 'next_alert_at': now + interval,
                'supervisor_id': supervisor_id, 'supervisor_available': available,
                'user_escalated': escalated, 'url': '/#' + quote(thread_id, safe=''), **counts,
                'guidance': 'Review this exact turn and its last confirmed progress. Inspect outstanding tools or approvals, '
                            'then use the supported supervisor recovery controls if appropriate. Confirm termination before '
                            'transferring write scope. No tool or original prompt was replayed. No reply is required.'}
        serialized = json.dumps(body, ensure_ascii=True)
        _suppress(db, incident['id'], now)  # Keep one actionable reminder; retain every audit receipt.
        message_id = None
        if available:
            message, _ = store._insert_message(db, sender_session_id=thread_id, recipient_session_id=supervisor_id,
                body=serialized, classification='action_required',
                thread_id=f"app-agent-stall:{incident['id']}:{sequence}", reply_required=False, now=now)
            message_id = message['id']
        alert_id = db.execute('''INSERT INTO app_agent_stall_alerts
            (incident_id, sequence, created_at, supervisor_id, message_id, user_escalated, body_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)''',
            (incident['id'], sequence, now, supervisor_id, message_id, int(escalated), serialized)).lastrowid
        db.execute('''UPDATE app_agent_stall_incidents SET alert_count=?, last_alert_at=?, next_alert_at=?,
            last_user_alert_at=CASE WHEN ? THEN ? ELSE last_user_alert_at END WHERE id=?''',
            (sequence, now, now + interval, escalated, now, incident['id']))
        if 'browser_turn_health' in _tables(db):
            db.execute('''INSERT INTO browser_turn_health (thread_id, turn_id, alerted_at, detail)
                VALUES (?, ?, ?, ?) ON CONFLICT(thread_id, turn_id) DO UPDATE SET
                alerted_at=COALESCE(alerted_at, excluded.alerted_at), detail=excluded.detail''',
                (thread_id, turn_id, now, detail))
        return {'id': alert_id, 'message_id': message_id, 'created_at': now, **body}


def snapshot(store, thread_id, turn_id=None):
    with store.database_operation('stall.snapshot', priority='background'), store.reading(), store._connection() as db:
        if not _exists(db):
            return None
        row = db.execute('''SELECT i.* FROM app_agent_stall_incidents i
            JOIN work_threads t ON t.thread_id=i.thread_id WHERE ''' + _LIVE +
            ' AND i.thread_id=? AND (? IS NULL OR i.turn_id=?)', (thread_id, turn_id, turn_id)).fetchone()
        return dict(row) if row else None


def apply_attention(thread, *, waiting=False):
    """Project durable health after ordinary attention policy, without provider IO."""
    incident = thread.get('stall_incident')
    if not incident:
        return
    classification = incident['classification']
    state = ('waiting' if classification == 'waiting_for_approval' else
             'long_running_tool' if classification == 'long_running_tool' else 'stalled')
    thread['turn_health'] = {**(thread.get('turn_health') or {}), 'state': state,
        'turn_id': incident['turn_id'], 'detail': incident['detail'],
        'progress_at': incident['progress_at'], 'last_alert_at': incident['last_alert_at'],
        'next_alert_at': incident['next_alert_at'], 'alert_count': incident['alert_count']}
    if state == 'stalled' and not waiting:
        thread.update(status='stalled', response_state='action')
    if incident['last_user_alert_at'] is None or thread['attention'] == 'archived' or thread['snoozed']:
        return
    action = ('Review the unattended approval' if classification == 'waiting_for_approval' else
              'Review the outstanding tool' if classification == 'long_running_tool' else 'Review the stalled agent')
    count = thread.get('actionable_inbox_count', 0)
    if count:
        age = int(thread.get('oldest_actionable_age_seconds') or 0)
        action += f"; {count} queued request{'s' if count != 1 else ''}, oldest {age // 60} min"
    thread.update(needs_attention=True, attention_reason='blocked', attention_key=f"stall:{incident['id']}",
                  attention_since=incident['created_at'], attention_action=action,
                  attention_owner_thread_id=thread['thread_id'], can_handle_response=False)


def notifications(store, *, include_claimed=False, cwd=None):
    with store.database_operation('stall.notifications', priority='background'), store.reading(), store._connection() as db:
        if not _exists(db):
            return []
        rows = db.execute('''SELECT a.id, a.created_at, a.claimed_at, i.thread_id, i.turn_id, i.classification,
            t.title, s.client, s.cwd, p.name AS project_name, r.name AS repository_name
            FROM app_agent_stall_alerts a JOIN app_agent_stall_incidents i ON i.id=a.incident_id
            JOIN work_threads t ON t.thread_id=i.thread_id JOIN sessions s ON s.session_id=i.thread_id
            LEFT JOIN thread_organization o ON o.thread_id=t.thread_id
            LEFT JOIN named_projects p ON p.id=o.project_id LEFT JOIN work_repositories r ON r.id=o.repository_id
            WHERE ''' + _LIVE + ''' AND a.sequence=i.alert_count AND a.user_escalated=1
            AND (t.snoozed_until IS NULL OR t.snoozed_until<=?) AND (? OR a.claimed_at IS NULL)
            ORDER BY a.id''', (store.clock(), include_claimed)).fetchall()
    return [dict(row, request_key='stall:' + str(row['id']), status='stalled',
                 body=('Review the unattended approval' if row['classification'] == 'waiting_for_approval'
                       else 'Review the agent without confirmed progress'))
            for row in rows if matches_workspace(row['cwd'], cwd)]


def claim_notification(store, key, *, cwd=None):
    if not isinstance(key, str) or not key.startswith('stall:') or not key[6:].isdigit():
        return False
    identifier = int(key[6:])
    # Eligibility and claim share one transaction, including Stop/completion races.
    with store.database_operation('stall.claim'), store._connection() as db:
        db.execute('BEGIN IMMEDIATE')
        if not _exists(db):
            return False
        row = db.execute('''SELECT a.id, s.cwd FROM app_agent_stall_alerts a
            JOIN app_agent_stall_incidents i ON i.id=a.incident_id JOIN work_threads t ON t.thread_id=i.thread_id
            JOIN sessions s ON s.session_id=i.thread_id WHERE ''' + _LIVE + ''' AND a.id=?
            AND a.sequence=i.alert_count AND a.user_escalated=1 AND a.claimed_at IS NULL
            AND (t.snoozed_until IS NULL OR t.snoozed_until<=?)''', (identifier, store.clock())).fetchone()
        if not row or not matches_workspace(row['cwd'], cwd):
            return False
        return db.execute('UPDATE app_agent_stall_alerts SET claimed_at=? WHERE id=? AND claimed_at IS NULL',
                          (store.clock(), identifier)).rowcount == 1
