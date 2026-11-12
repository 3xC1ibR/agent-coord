"""Read-only conversation projections of the durable coordination messages."""
import json

from .sqlite_rows import read_rows
from .store import CoordinationError
from .workspaces import matches_workspace


TIMELINE_BATCH_ROWS = 128
_TIMELINE_ROWS = """SELECT m.*, sender.client AS sender_client, recipient.client AS recipient_client,
    COALESCE(st.title, sender.name, sender.session_id) AS sender_title,
    COALESCE(rt.title, recipient.name, recipient.session_id) AS recipient_title,
    sender.cwd AS sender_cwd, recipient.cwd AS recipient_cwd,
    exchange.closed_at, a.outcome AS wake_outcome,
    d.attempted_at AS delivery_attempted_at, d.submitted_at AS delivery_submitted_at,
    d.error AS delivery_error, d.evidence AS delivery_evidence,
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
    LEFT JOIN message_delivery_attempts d ON d.rowid =
        (SELECT rowid FROM message_delivery_attempts WHERE message_id=m.id ORDER BY attempted_at DESC, rowid DESC LIMIT 1)"""
_TIMELINE_SELECT = _TIMELINE_ROWS + " WHERE (m.sender_session_id = ? OR m.recipient_session_id = ?)"


def _timeline_rows(db, session_id):
    """Transfer finite batches without one Python/SQLite handoff per message."""
    rows, columns, after = [], None, None
    while True:
        # Page only IDs through the sender/recipient indexes. Applying LIMIT
        # after the joined projection would repeat its work for later pages.
        query = "SELECT id FROM messages WHERE (sender_session_id = ? OR recipient_session_id = ?)"
        parameters = (session_id, session_id)
        if after is not None:
            query += " AND id > ?"
            parameters += (after,)
        packed = db.execute("SELECT json_group_array(id) FROM (" + query + " ORDER BY id LIMIT ?)",
                            (*parameters, TIMELINE_BATCH_ROWS)).fetchone()[0]
        identities = json.loads(packed)
        if not identities:
            return rows
        batch = read_rows(db, _TIMELINE_ROWS + " WHERE m.id IN (SELECT value FROM json_each(?)) ORDER BY m.id",
                          (packed,), columns=columns)
        rows.extend(batch)
        if batch:
            columns = tuple(batch[0])
        if len(identities) < TIMELINE_BATCH_ROWS:
            return rows
        after = identities[-1]


def message_timeline(store, session_id, cwd=None):
    session = store.get_session(session_id)
    if not matches_workspace(session["cwd"], cwd):
        raise CoordinationError("Conversation not found in this workspace.")
    with store.reading(), store._connection() as db:
        # Reply linkage, titles, creation receipts, and every batch belong to
        # one observation, even if another connection publishes during a read.
        db.execute("BEGIN")
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
        rows = _timeline_rows(db, session_id)
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
        item['reply_status'] = ('received' if item['reply_id'] else
                                'closed' if item['closed_at'] is not None or item['suppressed_at'] is not None else
                                'awaiting_reply') if item['reply_required'] else 'not_requested'
        if item["classification"] == "closure" or item["closed_at"] is not None:
            item["status"] = "Exchange closed"
        elif item["suppressed_at"] is not None:
            item["status"] = "Superseded"
        elif item["reply_required"] and item["reply_id"]:
            item["status"] = "Reply received"
        elif item["delivered_at"] is not None:
            item["status"] = "Delivered to agent"
        elif item['delivery_submitted_at'] is not None:
            item['status'] = 'Message body required'
        elif item['delivery_attempted_at'] is not None:
            item['status'] = 'Delivery uncertain' if item['delivery_error'] else 'Delivery unconfirmed'
        else:
            item["status"] = "Queued for agent" if item["classification"] == "action_required" else "Sent"
        for key in ("sender_cwd", "recipient_cwd"):
            item.pop(key)
        result.append(item)
    return result


def message_cursor(store):
    with store.reading(), store._connection() as db:
        return db.execute("SELECT COALESCE(MAX(seq), 0) FROM message_changes").fetchone()[0]


def message_sends(store, after=None, cwd=None):
    """Body-free live send events. Seed at the current ID on each connection."""
    with store.reading(), store._connection() as db:
        latest = db.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0]
        if after is None or after >= latest:
            return latest, []
        rows = db.execute("""SELECT m.id, m.sender_session_id, m.recipient_session_id,
            sender.cwd AS sender_cwd, recipient.cwd AS recipient_cwd
            FROM messages m JOIN sessions sender ON sender.session_id = m.sender_session_id
            JOIN sessions recipient ON recipient.session_id = m.recipient_session_id
            WHERE m.id > ? AND m.id <= ? ORDER BY m.id""", (after, latest)).fetchall()
    return latest, [{"method": "coordination/messageSent", "params": {
        "threadId": row["sender_session_id"], "recipientId": row["recipient_session_id"],
        "messageId": row["id"]}} for row in rows
        if matches_workspace(row["sender_cwd"], cwd) and matches_workspace(row["recipient_cwd"], cwd)]


def message_changes(store, after, cwd=None):
    with store.reading(), store._connection() as db:
        latest = db.execute("SELECT COALESCE(MAX(seq), 0) FROM message_changes").fetchone()[0]
        rows = db.execute("""SELECT DISTINCT s.session_id, s.cwd FROM sessions s
            JOIN message_changes c ON (s.session_id = c.sender_session_id OR s.session_id = c.recipient_session_id)
            WHERE c.seq > ? AND c.seq <= ?""", (after, latest)).fetchall()
    return latest, [{"method": "coordination/messages", "params": {"threadId": row["session_id"]}}
                    for row in rows if matches_workspace(row["cwd"], cwd)]
