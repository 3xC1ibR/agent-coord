"""Runtime-owned failure reports for app agents, independent of agent replies."""
from __future__ import annotations

import hashlib
import json


def _notify(store, db, request, *, event_key, stage, error, turn_id=None):
    creator = request["sender_session_id"]
    child = request["thread_id"]
    if child == creator or not db.execute(
            "SELECT 1 FROM sessions WHERE session_id = ?", (creator,)).fetchone():
        return
    key = hashlib.sha256(json.dumps([request["request_id"], event_key]).encode()).hexdigest()
    message_thread = "app-agent-failure:" + key
    # The message is the durable receipt. Keep the check and insert in the same
    # writer transaction, including across callback retries and backend restarts.
    if db.execute("SELECT 1 FROM messages WHERE thread_id = ?", (message_thread,)).fetchone():
        return
    child_row = db.execute("SELECT title FROM work_threads WHERE thread_id = ?", (child,)).fetchone()
    if isinstance(error, dict):
        error = error.get("message") or json.dumps(error, ensure_ascii=True)
    body = json.dumps({
        "event": "app_agent_failed", "source": "runtime", "stage": stage,
        "request_id": request["request_id"], "session_id": child,
        "name": child_row["title"] if child_row else None, "turn_id": turn_id,
        "error": str(error or "The app agent failed without an error description.")[:8000],
        "guidance": "Review the failed agent and decide how to continue the original task. "
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
