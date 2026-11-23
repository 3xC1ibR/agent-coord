"""Runtime-owned failure reports for app agents, independent of agent replies."""
from __future__ import annotations

import hashlib
import json

from . import supervision
from .workspaces import matches_workspace


FOLLOWUP_SECONDS = 600
QUEUED_WORK_ALERT_SECONDS = 1800


def _track_followup(store, db, request, turn_id):
    """A terminal turn is no longer watched by the active-turn watchdog."""
    if not turn_id:
        return
    db.execute("""CREATE TABLE IF NOT EXISTS app_agent_followups (
        thread_id TEXT NOT NULL, turn_id TEXT NOT NULL, request_id TEXT NOT NULL,
        created_at REAL NOT NULL, due_at REAL NOT NULL, escalated_at REAL,
        resolved_at REAL, resolution TEXT, queued_alerted_at REAL, queued_alert_message_id INTEGER,
        PRIMARY KEY(thread_id, turn_id)
    )""")
    db.execute("""CREATE INDEX IF NOT EXISTS app_agent_followups_due ON app_agent_followups(due_at)
        WHERE resolved_at IS NULL AND escalated_at IS NULL""")
    now = store.clock()
    db.execute("""INSERT OR IGNORE INTO app_agent_followups
        (thread_id, turn_id, request_id, created_at, due_at) VALUES (?, ?, ?, ?, ?)""",
        (request["thread_id"], turn_id, request["request_id"], now, now + FOLLOWUP_SECONDS))


def _ensure_queued_work_columns(db):
    """Columns added after the table first shipped; older databases gain them in place."""
    present = {row[1] for row in db.execute("PRAGMA table_info(app_agent_followups)")}
    for name, kind in (("queued_alerted_at", "REAL"), ("queued_alert_message_id", "INTEGER")):
        if name not in present:
            db.execute(f"ALTER TABLE app_agent_followups ADD COLUMN {name} {kind}")


def _resolution(db, row):
    """Return why a failure deadline no longer applies, or None while the agent is still unrecovered."""
    tid, turn = row["thread_id"], row["turn_id"]
    current = db.execute("SELECT turn_key, attention FROM work_threads WHERE thread_id = ?", (tid,)).fetchone()
    wake = db.execute("SELECT paused, pause_kind FROM browser_inbox_wake WHERE thread_id = ?", (tid,)).fetchone()
    if not current or current["attention"] == "archived":
        return "closed"
    if current["turn_key"] != turn:
        return "new_turn"
    if wake and wake["paused"] and wake["pause_kind"] == "stop":
        return "user_stop"
    if db.execute("SELECT 1 FROM handoffs WHERE sender_session_id = ? AND created_at >= ?",
                  (tid, row["created_at"])).fetchone():
        return "handoff"
    if db.execute("SELECT 1 FROM sqlite_master WHERE name = 'thread_close_requests'").fetchone() and db.execute(
            "SELECT 1 FROM thread_close_requests WHERE thread_id = ? AND status IN ('queued', 'closing')", (tid,)).fetchone():
        return "closing"
    return None


def _report_queued_work(store, db, row, now):
    """Tell the supervisor when new actionable work waits behind an unrecovered failure.

    A paused wake never delivers this work, and the overdue reminder is sent
    once. Without this report, work routed later waits with no further signal.
    """
    tid, turn = row["thread_id"], row["turn_id"]
    wake = db.execute("SELECT paused, pause_kind FROM browser_inbox_wake WHERE thread_id = ?", (tid,)).fetchone()
    if not wake or not wake["paused"] or wake["pause_kind"] != "failure":
        return
    boundary = row["queued_alerted_at"] or row["escalated_at"]
    if now - boundary < QUEUED_WORK_ALERT_SECONDS:
        return
    pending = db.execute("""SELECT m.id, m.created_at, m.reply_required, s.name AS sender_name FROM messages m
        LEFT JOIN sessions s ON s.session_id = m.sender_session_id
        WHERE m.recipient_session_id = ? AND m.sender_session_id != ? AND m.delivered_at IS NULL
        AND m.suppressed_at IS NULL AND m.classification = 'action_required'
        AND m.thread_id NOT LIKE 'event:%' AND m.created_at >= ? ORDER BY m.id""",
        (tid, tid, row["created_at"])).fetchall()
    reported = row["queued_alert_message_id"] or 0
    if not any(p["id"] > reported and p["created_at"] > boundary for p in pending):
        return  # Only work queued after the last report starts another one.
    newest = pending[-1]
    request = db.execute("SELECT * FROM app_control_requests WHERE request_id = ?", (row["request_id"],)).fetchone()
    if not request:
        return
    replies = sum(1 for p in pending if p["reply_required"])
    _notify(store, db, request, event_key=["queued_work", turn, newest["id"]], stage="queued_work", turn_id=turn,
            error=(f"{len(pending)} actionable message(s), {replies} requiring a reply, are queued for this agent, "
                   f"which has not run since its failed turn. Newest: #{newest['id']} from "
                   f"{newest['sender_name'] or 'an unnamed session'}."),
            event="app_agent_work_queued", guidance=
            "These messages will not wake the agent. Recover it with a fresh actionable continuation and "
            "agent-coord thread resume, or hand its scope to an available worker. Receiving this alert is "
            "not recovery. This report repeats at most every 30 minutes, and only when new work is queued; "
            "no reply is required.")
    db.execute("""UPDATE app_agent_followups SET queued_alerted_at = ?, queued_alert_message_id = ?
        WHERE thread_id = ? AND turn_id = ?""", (now, newest["id"], tid, turn))


def process_failure_followups(store, cwd=None):
    """Escalate unresolved work without running or transferring it.

    The overdue reminder is sent once. Later actionable work queued behind the
    same failure is reported separately, at most every 30 minutes.
    """
    with store._connection() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name = 'app_agent_followups'").fetchone():
            return
        now = store.clock()
        db.execute("BEGIN IMMEDIATE")
        _ensure_queued_work_columns(db)
        escalated = db.execute("""SELECT f.*, b.cwd FROM app_agent_followups f
            JOIN browser_sessions b ON b.thread_id = f.thread_id
            WHERE f.resolved_at IS NULL AND f.escalated_at IS NOT NULL""").fetchall()
        for row in escalated:
            if not matches_workspace(row["cwd"], cwd):
                continue
            resolution = _resolution(db, row)
            if resolution:
                db.execute("UPDATE app_agent_followups SET resolved_at = ?, resolution = ? WHERE thread_id = ? AND turn_id = ?",
                           (now, resolution, row["thread_id"], row["turn_id"]))
                continue
            _report_queued_work(store, db, row, now)
        rows = db.execute("""SELECT f.*, b.cwd FROM app_agent_followups f
            JOIN browser_sessions b ON b.thread_id = f.thread_id
            WHERE f.resolved_at IS NULL AND f.escalated_at IS NULL AND f.due_at <= ?""", (now,)).fetchall()
        for row in rows:
            if not matches_workspace(row["cwd"], cwd):
                continue
            tid, turn = row["thread_id"], row["turn_id"]
            resolution = _resolution(db, row)
            if resolution:
                db.execute("UPDATE app_agent_followups SET resolved_at = ?, resolution = ? WHERE thread_id = ? AND turn_id = ?",
                           (now, resolution, tid, turn))
                continue
            request = db.execute("SELECT * FROM app_control_requests WHERE request_id = ?", (row["request_id"],)).fetchone()
            if request:
                _notify(store, db, request, event_key=["unresolved", turn], stage="recovery_deadline", turn_id=turn,
                        error="No newer turn or recorded scope handoff was observed within ten minutes of the failure report.",
                        event="app_agent_recovery_overdue", guidance=
                        "Inspect the native agent and existing tool/job results, then recover with fresh instructions or "
                        "record a scope handoff to an available worker. For a usage limit, choose an available provider "
                        "or an authorized continuation after quota resets; do not repeatedly retry the exhausted provider. "
                        "Receiving this alert is not recovery. No prompt was replayed and no scope was released. "
                        "This reminder is sent once; no reply is required.")
                db.execute("UPDATE app_agent_followups SET escalated_at = ? WHERE thread_id = ? AND turn_id = ?", (now, tid, turn))


def _receipt_thread(request_id, event_key):
    return "app-agent-failure:" + hashlib.sha256(json.dumps([request_id, event_key]).encode()).hexdigest()


def turn_failure_message(db, creator, child, turn_id):
    """Find the existing runtime report for a subscribed failed turn."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name = 'app_control_requests'").fetchone():
        return None
    current = supervision.override(db, child)
    if supervision.supervised_by_user(db, child):
        return None
    if current is not None and current != creator:
        return None  # Reports for a transferred agent belong to its current supervisor.
    if current == creator:
        # The report was routed to the current supervisor under the creation's receipt.
        request = db.execute("""SELECT request_id FROM app_control_requests
            WHERE operation = 'create' AND thread_id = ? AND status IN ('running', 'completed', 'uncertain')
            ORDER BY created_at, request_id LIMIT 1""", (child,)).fetchone()
    else:
        request = db.execute("""SELECT request_id FROM app_control_requests
            WHERE operation = 'create' AND thread_id = ? AND sender_session_id = ?
            ORDER BY created_at, request_id LIMIT 1""", (child, creator)).fetchone()
    if request is None:
        return None
    row = db.execute("SELECT id FROM messages WHERE thread_id = ?",
                     (_receipt_thread(request["request_id"], ["turn", turn_id]),)).fetchone()
    return row["id"] if row else None


def _notify(store, db, request, *, event_key, stage, error, turn_id=None, event="app_agent_failed", guidance=None):
    child = request["thread_id"]
    if supervision.supervised_by_user(db, child):
        return  # The app shows the failure to its supervising user; a retired session is not woken.
    # Reports go to the current supervisor; creation failures precede any transfer.
    creator = supervision.override(db, child) or request["sender_session_id"]
    if supervision.passing_from(db, creator, child):
        return  # A closed supervisor is not woken; the app shows the failure until the move completes.
    if child == creator or not db.execute(
            "SELECT 1 FROM sessions WHERE session_id = ?", (creator,)).fetchone():
        return
    message_thread = _receipt_thread(request["request_id"], event_key)
    # The message is the durable receipt. Keep the check and insert in the same
    # writer transaction, including across callback retries and backend restarts.
    if db.execute("SELECT 1 FROM messages WHERE thread_id = ?", (message_thread,)).fetchone():
        return
    if event == "app_agent_failed" and turn_id and db.execute("SELECT 1 FROM sqlite_master WHERE name = 'event_subscription_items'").fetchone():
        # The completion matcher may win the race with failure persistence.
        # Both paths use a writer transaction, so only one wakes the creator.
        existing = db.execute("""SELECT 1 FROM event_subscription_items i
            JOIN event_subscriptions s ON s.id = i.subscription_id
            JOIN messages m ON m.id = COALESCE(i.message_id, s.message_id)
            WHERE i.kind = 'message' AND i.session_id = ? AND i.turn_id = ?
            AND s.owner_session_id = ? AND m.suppressed_at IS NULL""",
            (child, turn_id, creator)).fetchone()
        if existing:
            return
    child_row = db.execute("SELECT title FROM work_threads WHERE thread_id = ?", (child,)).fetchone()
    if isinstance(error, dict):
        error = error.get("message") or json.dumps(error, ensure_ascii=True)
    result = {"checkpoint": None, "context_lifecycle": None}
    if turn_id:
        from .event_subscriptions import result_snapshot
        assignment = db.execute("""SELECT id FROM messages WHERE sender_session_id = ?
            AND recipient_session_id = ? AND classification = 'action_required'
            AND COALESCE(recipient_turn_id, wake_turn_id) = ? ORDER BY id DESC LIMIT 1""",
            (creator, child, turn_id)).fetchone()
        if assignment:
            result = {"assignment_message_id": assignment["id"],
                      **result_snapshot(db, child, turn_id, assignment["id"])}
    if stage == "wake":
        # A transport launch can fail before the provider assigns a turn.
        pause = db.execute("SELECT * FROM browser_inbox_wake WHERE thread_id=? AND paused=1", (child,)).fetchone()
        message_id = dict(pause).get("wake_message_id") if pause else None
        if message_id:
            result["wake_message_id"] = message_id
            guidance = (f"Inspect the inbox pause on this agent. Later work is queued. After reviewing the failure, "
                        f"run agent-coord thread resume --session-id {child} --expected-message-id {message_id} "
                        "--wait 30. The user can also choose Resume inbox in the conversation.")
    body = json.dumps({
        "event": event, "source": "runtime", "stage": stage,
        "request_id": request["request_id"], "session_id": child,
        "name": child_row["title"] if child_row else None, "turn_id": turn_id,
        "error": str(error or "The app agent failed without an error description.")[:8000],
        **result,
        "guidance": guidance or "Review the failed agent and decide how to continue the original task. "
                    "This is an automatic failure report, not a result from the agent. "
                    "No reply is required. The failed work has not been retried.",
    }, ensure_ascii=True)
    sender = child if child_row else creator
    store._insert_message(db, sender_session_id=sender, recipient_session_id=creator,
                          body=body, classification="action_required", thread_id=message_thread,
                          reply_required=False, now=store.clock())


def notify_creation_failure(store, db, request):
    """Called in the transaction that makes creation failed or uncertain."""
    if request["operation"] == "create":
        _notify(store, db, request, event_key="creation", stage=request["status"], error=request["error"])


def notify_agent_failure(store, thread_id, *, event_key, stage, error=None, turn_id=None):
    with store._connection() as db:
        db.execute("BEGIN IMMEDIATE")
        if turn_id and db.execute("SELECT 1 FROM turn_stop_requests WHERE thread_id = ? AND turn_id = ?",
                                  (thread_id, turn_id)).fetchone():
            # A late callback cannot turn user intent into a failure or recovery deadline.
            return
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name = 'app_control_requests'").fetchone():
            return
        request = db.execute("""SELECT * FROM app_control_requests
            WHERE operation = 'create' AND thread_id = ?
            AND status IN ('running', 'completed', 'uncertain')
            ORDER BY created_at, request_id LIMIT 1""", (thread_id,)).fetchone()
        if request:
            _track_followup(store, db, request, turn_id)
            _notify(store, db, request, event_key=event_key, stage=stage, error=error, turn_id=turn_id)


def notify_agent_stall(store, thread_id, *, turn_id, error, progress_at=None, classification="stalled"):
    """Compatibility entry point; new observers supply their confirmed progress epoch."""
    from .stall_notifications import record_stall, snapshot
    if progress_at is None:
        current = snapshot(store, thread_id, turn_id)
        # Legacy callers invoke this only after their inactivity threshold. Keep
        # an established epoch so subsequent calls retain the durable cadence.
        allowance = 1200 if classification in {"long_running_tool", "unverified_tool"} else 600
        progress_at = current["progress_at"] if current else store.clock() - allowance
    return record_stall(store, thread_id, turn_id, progress_at=progress_at,
                        detail=error, classification=classification)
