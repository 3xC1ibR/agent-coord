"""Durable composer acceptance and duplicate detection around provider effects.

An interrupted acceptance is retained as uncertain, never replayed. Provider
history remains the authority for whether a submitted turn actually started.
"""
from __future__ import annotations

import hashlib
import json
import uuid

from .store import CoordinationError


class SubmissionReceipts:
    def __init__(self, store):
        self.store = store
        with store._connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS browser_submission_receipts (
                thread_id TEXT NOT NULL REFERENCES browser_sessions(thread_id),
                request_id TEXT NOT NULL, payload_hash TEXT NOT NULL,
                payload_json TEXT NOT NULL, state TEXT NOT NULL,
                result_json TEXT, error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                PRIMARY KEY(thread_id, request_id)
            )""")

    def run(self, thread_id, body, callback, *, queued=False):
        key = body.get("requestId")
        if key is None:
            key = str(uuid.uuid4())
        if not isinstance(key, str) or not key.strip() or len(key) > 200:
            raise CoordinationError("Submission request ID must contain 1–200 characters.")
        # Navigation/telemetry metadata may change during a manual retry. The
        # requested message, images, settings and queue mode must stay identical.
        semantic = {k: v for k, v in body.items() if k not in {"requestId", "windowId", "activity", "expectedTurnId"}}
        encoded = json.dumps({"body": semantic, "queued": queued}, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        now = self.store.clock()
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM browser_submission_receipts WHERE thread_id=? AND request_id=?",
                             (thread_id, key)).fetchone()
            if row:
                if row["payload_hash"] != digest:
                    raise CoordinationError("Submission request ID was already used for different input.")
                if row["state"] == "completed":
                    return json.loads(row["result_json"])
                if row["state"] != "rejected":
                    raise CoordinationError("This submission has an unconfirmed outcome. Check the conversation before sending again; it has not been replayed.")
                db.execute("UPDATE browser_submission_receipts SET state='accepted', error=NULL, updated_at=? WHERE thread_id=? AND request_id=?",
                           (now, thread_id, key))
            else:
                db.execute("INSERT INTO browser_submission_receipts VALUES (?, ?, ?, ?, 'accepted', NULL, NULL, ?, ?)",
                           (thread_id, key, digest, encoded, now, now))
        # No database transaction spans the external operation.
        try:
            result = callback()
        except Exception as error:
            from .codex_app_server import BrowserBusyError, BrowserNotSubmittedError
            state = "rejected" if isinstance(error, (BrowserBusyError, BrowserNotSubmittedError)) else "uncertain"
            try:
                with self.store._connection() as db:
                    db.execute("UPDATE browser_submission_receipts SET state=?, error=?, updated_at=? WHERE thread_id=? AND request_id=?",
                               (state, str(error)[:2000], self.store.clock(), thread_id, key))
            except Exception:
                # The durable accepted record already prevents a replay even
                # when failure bookkeeping cannot be committed.
                pass
            raise
        try:
            with self.store._connection() as db:
                db.execute("UPDATE browser_submission_receipts SET state='completed', result_json=?, updated_at=? WHERE thread_id=? AND request_id=?",
                           (json.dumps(result), self.store.clock(), thread_id, key))
        except Exception as error:
            raise CoordinationError("Your submission may have been received, but its receipt could not be saved. Check the conversation before sending again.") from error
        return result
