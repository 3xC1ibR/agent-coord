"""Read-only conversation projections of the durable coordination messages."""
import json

from .store import CoordinationError
from .workspaces import matches_workspace


def message_timeline(store, session_id, cwd=None):
    session = store.get_session(session_id)
    if not matches_workspace(session["cwd"], cwd):
        raise CoordinationError("Conversation not found in this workspace.")
    with store._connection() as db:
        creations = {}
        if db.execute("SELECT 1 FROM sqlite_master WHERE name = 'app_control_requests'").fetchone():
            for request in db.execute("""SELECT request_id, sender_session_id, thread_id, result_json
                    FROM app_control_requests WHERE operation = 'create' AND status = 'completed'
                    AND sender_session_id = ?""", (session_id,)):
                try:
                    receipt = json.loads(request["result_json"])
                except (TypeError, ValueError):
                    continue
                if isinstance(receipt, dict) and type(receipt.get("message_id")) is int:
                    creations[(receipt["message_id"], request["sender_session_id"], request["thread_id"])] = request["request_id"]
        rows = db.execute("""SELECT m.*, sender.client AS sender_client, recipient.client AS recipient_client,
            COALESCE(st.title, sender.name, sender.session_id) AS sender_title,
            COALESCE(rt.title, recipient.name, recipient.session_id) AS recipient_title,
            sender.cwd AS sender_cwd, recipient.cwd AS recipient_cwd,
            exchange.closed_at, a.outcome AS wake_outcome,
            (SELECT r.id FROM messages r WHERE r.thread_id = m.thread_id AND r.id > m.id
                AND r.sender_session_id = m.recipient_session_id
                AND r.recipient_session_id = m.sender_session_id
                AND (r.in_reply_to = m.id OR r.in_reply_to IS NULL)
                AND r.classification != 'closure' ORDER BY r.id LIMIT 1) AS reply_id
            FROM messages m JOIN sessions sender ON sender.session_id = m.sender_session_id
            JOIN sessions recipient ON recipient.session_id = m.recipient_session_id
            LEFT JOIN work_threads st ON st.thread_id = m.sender_session_id
            LEFT JOIN work_threads rt ON rt.thread_id = m.recipient_session_id
            LEFT JOIN message_threads exchange ON exchange.thread_id = m.thread_id
            LEFT JOIN message_wake_attempts a ON a.message_id = m.id
            WHERE m.sender_session_id = ? OR m.recipient_session_id = ? ORDER BY m.id""",
            (session_id, session_id)).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        outgoing = item["sender_session_id"] == session_id
        side = "recipient" if outgoing else "sender"
        item["direction"] = "sent" if outgoing else "received"
        item["creation_request_id"] = creations.get((item["id"], item["sender_session_id"], item["recipient_session_id"]))
        item["counterpart"] = dict(session_id=item[side + "_session_id"], title=item[side + "_title"],
                                   client=item[side + "_client"], available=matches_workspace(item[side + "_cwd"], cwd))
        item["turn_id"] = item["sender_turn_id"] if outgoing else item["wake_turn_id"] or item["recipient_turn_id"]
        item["started_turn"] = not outgoing and bool(item["wake_turn_id"])
        item["reply_required"] = bool(item["reply_required"])
        if item["classification"] == "closure" or item["closed_at"] is not None:
            item["status"] = "Exchange closed"
        elif item["suppressed_at"] is not None:
            item["status"] = "Superseded"
        elif item["reply_required"] and item["reply_id"]:
            item["status"] = "Reply received"
        elif item["delivered_at"] is not None:
            item["status"] = "Delivered to agent"
        else:
            item["status"] = "Queued for agent" if item["classification"] == "action_required" else "Sent"
        for key in ("sender_cwd", "recipient_cwd"):
            item.pop(key)
        result.append(item)
    return result


def message_cursor(store):
    with store._connection() as db:
        return db.execute("SELECT COALESCE(MAX(seq), 0) FROM message_changes").fetchone()[0]


def message_changes(store, after, cwd=None):
    with store._connection() as db:
        latest = db.execute("SELECT COALESCE(MAX(seq), 0) FROM message_changes").fetchone()[0]
        rows = db.execute("""SELECT DISTINCT s.session_id, s.cwd FROM sessions s
            JOIN message_changes c ON (s.session_id = c.sender_session_id OR s.session_id = c.recipient_session_id)
            WHERE c.seq > ? AND c.seq <= ?""", (after, latest)).fetchall()
    return latest, [{"method": "coordination/messages", "params": {"threadId": row["session_id"]}}
                    for row in rows if matches_workspace(row["cwd"], cwd)]
