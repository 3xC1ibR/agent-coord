"""Runtime-owned failure reports for app agents, independent of agent replies."""
from __future__ import annotations

import hashlib
import json


def _receipt_thread(request_id, event_key):
    return "app-agent-failure:" + hashlib.sha256(json.dumps([request_id, event_key]).encode()).hexdigest()


def turn_failure_message(db, creator, child, turn_id):
    """Find the existing runtime report for a subscribed failed turn."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name = 'app_control_requests'").fetchone():
        return None
    request = db.execute("""SELECT request_id FROM app_control_requests
        WHERE operation = 'create' AND thread_id = ? AND sender_session_id = ?
        ORDER BY created_at, request_id LIMIT 1""", (child, creator)).fetchone()
    if request is None:
        return None
    row = db.execute("SELECT id FROM messages WHERE thread_id = ?",
                     (_receipt_thread(request["request_id"], ["turn", turn_id]),)).fetchone()
    return row["id"] if row else None


def _notify(store, db, request, *, event_key, stage, error, turn_id=None, event="app_agent_failed", guidance=None):
    creator = request["sender_session_id"]
    child = request["thread_id"]
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
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name = 'app_control_requests'").fetchone():
            return
        request = db.execute("""SELECT * FROM app_control_requests
            WHERE operation = 'create' AND thread_id = ?
            AND status IN ('running', 'completed', 'uncertain')
            ORDER BY created_at, request_id LIMIT 1""", (thread_id,)).fetchone()
        if request:
            _notify(store, db, request, event_key=event_key, stage=stage, error=error, turn_id=turn_id)


def notify_agent_stall(store, thread_id, *, turn_id, error):
    with store._connection() as db:
        db.execute("BEGIN IMMEDIATE")
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name = 'app_control_requests'").fetchone():
            return
        request = db.execute("""SELECT * FROM app_control_requests WHERE operation = 'create'
            AND thread_id = ? AND status = 'completed' ORDER BY created_at, request_id LIMIT 1""", (thread_id,)).fetchone()
        if request:
            _notify(store, db, request, event_key=["stall", turn_id], stage="watchdog", error=error,
                    turn_id=turn_id, event="app_agent_stalled", guidance=
                    "Inspect this exact turn and continue coordinating the original assignment. "
                    "The runtime attempted state reconciliation and, when supported and safe, one same-turn continuation. "
                    "Silence is not proof that the agent or its tools stopped. Confirm termination before transferring its write scope. "
                    "No tool or original prompt was replayed. No reply is required.")
