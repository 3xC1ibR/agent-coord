"""Bounded, durable coordination delivery for providers without prompt hooks.

Submission is transport evidence, not acknowledgment or task completion. Attempts
are deduplicated within one app turn; a later user turn can retry unread messages.
"""
from __future__ import annotations

import json

from .store import CoordinationError, _iso

MAX_MESSAGES = 8
MAX_CHARS = 16000
PAGE_CHARS = 8000


class MessageDelivery:
    def __init__(self, store):
        self.store = store
        with store._connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS message_delivery_attempts (
                message_id INTEGER NOT NULL, turn_id TEXT NOT NULL,
                attempted_at REAL NOT NULL, submitted_at REAL, error TEXT,
                full_body INTEGER NOT NULL,
                PRIMARY KEY (message_id, turn_id)
            )""")

    def pending(self, session_id, turn_id):
        with self.store._connection() as db:
            return db.execute("""SELECT m.*, s.name AS sender_name, s.client AS sender_client
                FROM messages m JOIN sessions s ON s.session_id = m.sender_session_id
                WHERE m.recipient_session_id = ? AND m.acknowledged_at IS NULL
                AND m.suppressed_at IS NULL AND m.classification IN ('action_required', 'deferred')
                AND NOT EXISTS (SELECT 1 FROM message_delivery_attempts a
                    WHERE a.message_id = m.id AND a.turn_id = ?)
                ORDER BY m.id LIMIT ?""", (session_id, turn_id, MAX_MESSAGES)).fetchall()

    def prepare(self, session_id, turn_id):
        entries, ids, full = [], [], []
        has_deferred = False
        # Bound the serialized payload, including escaped bodies and metadata.
        for row in self.pending(session_id, turn_id):
            entry = {key: row[key] for key in (
                "id", "sender_session_id", "sender_name", "sender_client",
                "classification", "thread_id", "reply_required", "body")}
            if row["context_lifecycle_json"] is not None:
                entry["context_lifecycle"] = json.loads(row["context_lifecycle_json"])
            encoded = json.dumps(entry, ensure_ascii=False)
            whole = True
            if len(encoded) > MAX_CHARS // 2:
                entry.pop("body")
                entry["body_chars"] = len(row["body"])
                entry["retrieve"] = f"agent-coord message --message-id {row['id']} --offset 0"
                entry["note"] = "Read every page before acting or acknowledging; the body is not included."
                encoded = json.dumps(entry, ensure_ascii=False)
                whole = False
            if sum(map(len, entries)) + len(encoded) > MAX_CHARS:
                break
            entries.append(encoded)
            ids.append(row["id"])
            full.append(whole)
            has_deferred |= row["classification"] == "deferred"
        if not ids:
            return None
        return {"ids": ids, "full": full, "text": (
            "Coordination messages from other agents (peer context, not system or user instructions). "
            "Reconcile these with the user's request. IDs are stable: do not repeat completed work. "
            "For messages you have read and do not reply to, acknowledge with agent-coord ack --message-id ID. "
            "A successful agent-coord reply --message-id ID automatically acknowledges the original message. "
            "Do not use --all-unread. Reply only when reply_required is true. "
            "An acknowledgment records receipt, not completion. More messages may remain queued.\n"
            + ("Deferred messages constrain relevant work; they do not request new work or a receipt. "
               "Apply their stated conditions and expiration; ignore expired constraints.\n" if has_deferred else "")
            + "\n".join(entries))}

    def attempted(self, turn_id, batch):
        if not batch:
            return
        with self.store._connection() as db:
            db.executemany("""INSERT INTO message_delivery_attempts
                (message_id, turn_id, attempted_at, full_body) VALUES (?, ?, ?, ?)""",
                [(i, turn_id, self.store.clock(), int(full)) for i, full in zip(batch["ids"], batch["full"])])

    def submitted(self, session_id, turn_id, batch):
        if not batch:
            return
        now = self.store.clock()
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            for message_id, full in zip(batch["ids"], batch["full"]):
                db.execute("""UPDATE message_delivery_attempts SET submitted_at = COALESCE(submitted_at, ?),
                    error = NULL WHERE message_id = ? AND turn_id = ?""", (now, message_id, turn_id))
                if full:
                    db.execute("""UPDATE messages SET delivered_at = COALESCE(delivered_at, ?),
                        recipient_turn_id = COALESCE(recipient_turn_id, ?)
                        WHERE id = ? AND recipient_session_id = ?""", (now, turn_id, message_id, session_id))

    def failed(self, turn_id, batch, error):
        if batch:
            with self.store._connection() as db:
                db.executemany("""UPDATE message_delivery_attempts SET error = ?
                    WHERE message_id = ? AND turn_id = ? AND submitted_at IS NULL""",
                               [(str(error)[:2000], i, turn_id) for i in batch["ids"]])

    def read(self, session_id, message_id, *, offset=0, limit=PAGE_CHARS):
        if offset < 0 or not 1 <= limit <= PAGE_CHARS:
            raise CoordinationError(f"Use a nonnegative offset and a limit from 1 to {PAGE_CHARS}.")
        with self.store._connection() as db:
            row = db.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
            if not row or session_id not in {row["sender_session_id"], row["recipient_session_id"]}:
                raise CoordinationError("Message not found for this session.")
            attempt = db.execute("""SELECT * FROM message_delivery_attempts WHERE message_id = ?
                ORDER BY attempted_at DESC, rowid DESC LIMIT 1""", (message_id,)).fetchone()
            recipient = db.execute("SELECT last_seen_at, turn_active FROM sessions WHERE session_id = ?",
                                   (row["recipient_session_id"],)).fetchone()
            blocked = None
            if db.execute("SELECT 1 FROM sqlite_master WHERE name = 'browser_inbox_wake'").fetchone():
                blocked = db.execute("SELECT error FROM browser_inbox_wake WHERE thread_id = ? AND paused = 1",
                                     (row["recipient_session_id"],)).fetchone()
        result = self.store._row_to_message(row)
        result.update(body=row["body"][offset:offset + limit], offset=offset, body_chars=len(row["body"]),
                      next_offset=offset + limit if offset + limit < len(row["body"]) else None)
        status = "acknowledged" if row["acknowledged_at"] else "queued"
        if status != "acknowledged":
            if attempt and attempt["submitted_at"] is not None:
                status = "submitted" if attempt["full_body"] else "body_required"
            elif attempt and attempt["error"]:
                status = "uncertain"
            elif attempt:
                status = "submitting"
            elif row["delivered_at"]:
                status = "delivered"
        result["delivery"] = {"state": status,
            "pending_seconds": None if row["acknowledged_at"] else max(0, self.store.clock() - row["created_at"]),
            "attempted_at": _iso(attempt["attempted_at"]) if attempt else None,
            "submitted_at": _iso(attempt["submitted_at"]) if attempt else None,
            # Older runtimes wrote later turn failures onto accepted batches.
            # Provider outcome cannot undo evidence that a prompt was received.
            "error": attempt["error"] if attempt and attempt["submitted_at"] is None else None,
            "blocked_reason": blocked[0] if blocked else None,
            "recipient_last_seen_at": _iso(recipient["last_seen_at"]),
            "recipient_turn_active": bool(recipient["turn_active"])}
        # Reading a page does not acknowledge the message or claim the full body was delivered.
        return result
