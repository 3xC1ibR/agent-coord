"""Bounded runtime-owned delivery receipts, independent of replies and task outcomes.

An attempt reserves its messages before crossing a provider boundary. Ambiguous
attempts are retained, never automatically replayed. Confirmation records the
specific transport evidence; it does not assert that work was completed.
"""
from __future__ import annotations

import json
import uuid

from .store import CoordinationError, _iso

MAX_MESSAGES = 8
MAX_CHARS = 16000
PAGE_CHARS = 8000
DELIVERY_LABEL = "[Automatic Agent Coord message delivery — not a human message]"
DELIVERY_GUIDANCE = ("Handle the coordination messages supplied here. "
                     "If their bodies are missing, retrieve them with agent-coord inbox --unread. "
                     "Delivery receipts are automatic. Reply only when reply_required=true.")


class MessageDelivery:
    def __init__(self, store):
        self.store = store

    def pending(self, session_id, turn_id=None, *, message_ids=None):
        # Any attempt excludes automatic replay, including across restarts.
        # Failed submissions known not to have crossed the boundary are released.
        restriction, args = "", [session_id]
        if message_ids is not None:
            restriction = " AND m.id IN (SELECT value FROM json_each(?))"
            args.append(json.dumps(message_ids))
        with self.store.reading(), self.store._connection() as db:
            return db.execute("""SELECT m.*, s.name AS sender_name, s.client AS sender_client
                FROM messages m JOIN sessions s ON s.session_id = m.sender_session_id
                WHERE m.recipient_session_id = ? AND m.delivered_at IS NULL
                AND m.suppressed_at IS NULL AND m.classification IN ('action_required', 'deferred')
                AND NOT EXISTS (SELECT 1 FROM message_delivery_attempts a WHERE a.message_id = m.id)
                """ + restriction + " ORDER BY m.id LIMIT ?", (*args, MAX_MESSAGES)).fetchall()

    def prepare(self, session_id, turn_id, *, message_ids=None):
        entries, ids, full = [], [], []
        size = len(DELIVERY_LABEL) + len(DELIVERY_GUIDANCE) + 100
        for row in self.pending(session_id, turn_id, message_ids=message_ids):
            entry = {key: row[key] for key in (
                "id", "sender_session_id", "sender_name", "classification", "thread_id", "reply_required", "body")}
            entry['reply_required'] = bool(entry['reply_required'])
            if row["context_lifecycle_json"] is not None:
                entry["context_lifecycle"] = json.loads(row["context_lifecycle_json"])
            encoded = json.dumps(entry, ensure_ascii=False)
            whole = True
            if len(encoded) > MAX_CHARS // 2:
                entry.pop("body")
                entry.pop("context_lifecycle", None)
                entry["body_chars"] = len(row["body"])
                entry["retrieve"] = f"agent-coord message --message-id {row['id']} --offset 0"
                entry["note"] = "Read every page before acting; the body is not included."
                encoded = json.dumps(entry, ensure_ascii=False)
                whole = False
            if size + len(encoded) + 1 > MAX_CHARS:
                break
            entries.append(encoded)
            ids.append(row["id"])
            full.append(whole)
            size += len(encoded) + 1
        if not ids:
            return None
        return {"ids": ids, "full": full, "text": DELIVERY_LABEL + "\n" + DELIVERY_GUIDANCE + "\n" + "\n".join(entries)}

    def attempted(self, turn_id, batch, *, channel="prompt", provider_turn_id=None, boundary_id=None):
        if not batch:
            return
        with self.store.database_operation("message.delivery.reserve"), self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            for ident, full in zip(batch['ids'], batch['full']):
                inserted = db.execute("""INSERT INTO message_delivery_attempts
                    (message_id, turn_id, attempted_at, full_body, channel, provider_turn_id, boundary_id)
                    SELECT id, ?, ?, ?, ?, ?, ? FROM messages
                    WHERE id=? AND delivered_at IS NULL AND suppressed_at IS NULL
                    AND NOT EXISTS (SELECT 1 FROM message_delivery_attempts WHERE message_id=messages.id)""",
                    (turn_id, self.store.clock(), int(full), channel, provider_turn_id, boundary_id, ident)).rowcount
                if not inserted:
                    raise CoordinationError("Message delivery changed before submission; prepare a new batch.")

    def submitted(self, session_id, turn_id, batch, *, evidence="provider_response", provider_turn_id=None):
        if not batch:
            return
        with self.store.database_operation("message.delivery.confirm"), self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self.confirm_batch(db, session_id, turn_id, batch["ids"], evidence=evidence, provider_turn_id=provider_turn_id)

    def confirm_batch(self, db, session_id, turn_id, message_ids, *, evidence, provider_turn_id):
        """Confirm the reserved inputs within the caller's write transaction."""
        now = self.store.clock()
        for message_id in message_ids:
            attempt = db.execute("""SELECT a.* FROM message_delivery_attempts a JOIN messages m ON m.id=a.message_id
                WHERE a.message_id=? AND a.turn_id=? AND m.recipient_session_id=?""",
                (message_id, turn_id, session_id)).fetchone()
            if not attempt:
                continue
            db.execute("""UPDATE message_delivery_attempts SET submitted_at=COALESCE(submitted_at, ?), error=NULL,
                evidence=COALESCE(evidence, ?), provider_turn_id=COALESCE(provider_turn_id, ?)
                WHERE message_id=? AND turn_id=?""", (now, evidence, provider_turn_id, message_id, turn_id))
            if attempt['full_body']:
                db.execute("""UPDATE messages SET delivered_at=COALESCE(delivered_at, ?),
                    recipient_turn_id=COALESCE(recipient_turn_id, ?) WHERE id=?""",
                    (now, provider_turn_id or attempt['provider_turn_id'] or turn_id, message_id))

    def release(self, turn_id):
        """Only for a proven rejection before provider submission."""
        with self.store._connection() as db:
            db.execute("DELETE FROM message_delivery_attempts WHERE turn_id=? AND submitted_at IS NULL", (turn_id,))

    def bind(self, turn_id, provider_turn_id):
        with self.store._connection() as db:
            db.execute("UPDATE message_delivery_attempts SET provider_turn_id=? WHERE turn_id=?",
                       (provider_turn_id, turn_id))

    def failed(self, turn_id, batch, error):
        if batch:
            with self.store._connection() as db:
                db.executemany("""UPDATE message_delivery_attempts SET error=?
                    WHERE message_id=? AND turn_id=? AND submitted_at IS NULL""",
                    [(str(error)[:2000], i, turn_id) for i in batch['ids']])

    def _confirm_hooks(self, session_id, provider_turn_id):
        if not provider_turn_id:
            return
        with self.store.reading(), self.store._connection() as db:
            rows = db.execute("""SELECT a.* FROM message_delivery_attempts a JOIN messages m ON m.id=a.message_id
                WHERE m.recipient_session_id=? AND a.channel='hook' AND a.provider_turn_id=?
                AND a.submitted_at IS NULL AND a.error IS NULL""",
                (session_id, provider_turn_id)).fetchall()
        for row in rows:
            self.submitted(session_id, row['turn_id'], {'ids': [row['message_id']]},
                           evidence="turn_completed", provider_turn_id=provider_turn_id)

    def finish_turn(self, session_id, provider_turn_id, *, completed):
        if not provider_turn_id:
            return
        if completed:
            self._confirm_hooks(session_id, provider_turn_id)
        else:
            with self.store._connection() as db:
                db.execute("""UPDATE message_delivery_attempts SET error=COALESCE(error, 'Turn ended before delivery confirmation')
                    WHERE provider_turn_id=? AND submitted_at IS NULL AND message_id IN
                    (SELECT id FROM messages WHERE recipient_session_id=?)""", (provider_turn_id, session_id))

    def hook_context(self, session_id, provider_turn_id, *, boundary_id=None):
        token = 'hook:' + str(uuid.uuid4())
        batch = self.prepare(session_id, token)
        if not batch:
            return ''
        self.attempted(token, batch, channel='hook', provider_turn_id=provider_turn_id, boundary_id=boundary_id)
        return batch['text']

    def read(self, session_id, message_id, *, offset=0, limit=PAGE_CHARS, consume=False):
        if offset < 0 or not 1 <= limit <= PAGE_CHARS:
            raise CoordinationError(f"Use a nonnegative offset and a limit from 1 to {PAGE_CHARS}.")
        with self.store.reading(), self.store._connection() as db:
            row = db.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
            if not row or session_id not in {row['sender_session_id'], row['recipient_session_id']}:
                raise CoordinationError("Message not found for this session.")
            attempt = db.execute("SELECT * FROM message_delivery_attempts WHERE message_id=? ORDER BY attempted_at DESC, rowid DESC LIMIT 1",
                                 (message_id,)).fetchone()
            recipient = db.execute("SELECT last_seen_at, turn_active FROM sessions WHERE session_id=?",
                                   (row['recipient_session_id'],)).fetchone()
            blocked = None
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='browser_inbox_wake'").fetchone():
                blocked = db.execute("SELECT error FROM browser_inbox_wake WHERE thread_id=? AND paused=1",
                                     (row['recipient_session_id'],)).fetchone()
        # Runtime accounts for complete retrieval; an old manual ack is unnecessary.
        # Sender inspection never consumes the recipient's body.
        if consume and session_id == row['recipient_session_id'] and not row['delivered_at'] and offset < len(row['body']):
            with self.store._connection() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("""INSERT INTO message_body_reads VALUES (?, ?, ?) ON CONFLICT(message_id, start_offset)
                    DO UPDATE SET end_offset=MAX(end_offset, excluded.end_offset)""",
                    (message_id, offset, min(offset + limit, len(row['body']))))
                covered = 0
                for page in db.execute("SELECT start_offset, end_offset FROM message_body_reads WHERE message_id=? ORDER BY start_offset", (message_id,)):
                    if page['start_offset'] > covered:
                        break
                    covered = max(covered, page['end_offset'])
                if covered >= len(row['body']):
                    db.execute("UPDATE messages SET delivered_at=COALESCE(delivered_at, ?) WHERE id=?", (self.store.clock(), message_id))
                    row = db.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
        result = self.store._row_to_message(row)
        result.update(body=row['body'][offset:offset + limit], offset=offset, body_chars=len(row['body']),
                      next_offset=offset + limit if offset + limit < len(row['body']) else None)
        state, evidence = 'queued', None
        if row['delivered_at'] is not None:
            state = 'delivered'
            evidence = (attempt['evidence'] if attempt and attempt['full_body'] else 'body_retrieved') if attempt else 'inbox_retrieved'
        elif attempt:
            state = 'body_required' if attempt['submitted_at'] is not None else 'uncertain' if attempt['error'] else 'unconfirmed'
            evidence = attempt['evidence']
        result['delivery'] = {'state': state, 'evidence': evidence,
            'pending_seconds': None if row['delivered_at'] is not None else max(0, self.store.clock() - row['created_at']),
            'attempted_at': _iso(attempt['attempted_at']) if attempt else None,
            'submitted_at': _iso(attempt['submitted_at']) if attempt else None,
            'error': attempt['error'] if attempt and attempt['submitted_at'] is None else None,
            'blocked_reason': blocked[0] if blocked else None,
            'recipient_last_seen_at': _iso(recipient['last_seen_at']),
            'recipient_turn_active': bool(recipient['turn_active'])}
        return result

    def metrics(self, session_id):
        """Bounded, content-free observations; no model inference or transcript scan."""
        with self.store.reading(), self.store._connection() as db:
            rows = db.execute("""SELECT m.id, m.created_at, m.delivered_at, a.channel, a.attempted_at,
                a.submitted_at, a.error FROM messages m LEFT JOIN message_delivery_attempts a ON a.message_id=m.id
                WHERE m.recipient_session_id=? AND m.classification IN ('action_required', 'deferred')
                AND m.suppressed_at IS NULL ORDER BY m.id DESC LIMIT 1024""", (session_id,)).fetchall()
        counts = {'queued': 0, 'delivered': 0, 'uncertain': 0, 'unconfirmed': 0, 'body_required': 0}
        samples = {'hook': [], 'prompt': [], 'retrieval': []}
        oldest = 0
        for row in rows:
            if row['delivered_at'] is not None:
                counts['delivered'] += 1
                samples[row['channel'] or 'retrieval'].append(max(0, row['delivered_at'] - row['created_at']) * 1000)
            elif row['attempted_at'] is None:
                counts['queued'] += 1
            else:
                state = 'body_required' if row['submitted_at'] is not None else 'uncertain' if row['error'] else 'unconfirmed'
                counts[state] += 1
                oldest = max(oldest, self.store.clock() - row['created_at'])
        return {'sample_limit': 1024, 'counts': counts, 'oldest_unconfirmed_seconds': oldest,
                'delivery_latency_ms': {key: {'samples': len(values), 'p95': sorted(values)[min(len(values)-1, int(len(values)*.95))] if values else None}
                                        for key, values in samples.items()}}
