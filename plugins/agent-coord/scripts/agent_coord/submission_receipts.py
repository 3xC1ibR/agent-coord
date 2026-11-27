"""Durable composer acceptance and duplicate detection around provider effects.

An interrupted acceptance is retained as uncertain, never replayed. Provider
history remains the authority for whether a submitted turn actually started.
"""
from __future__ import annotations

import hashlib
import json
import threading
import uuid

from .store import CoordinationError


class SubmissionUncertainError(CoordinationError):
    """Provider acceptance cannot be confirmed; repeating input is unsafe."""


class SubmissionReceipts:
    def __init__(self, store):
        self.store = store
        self._local = threading.local()
        with store._connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS browser_submission_receipts (
                thread_id TEXT NOT NULL REFERENCES browser_sessions(thread_id),
                request_id TEXT NOT NULL, payload_hash TEXT NOT NULL,
                payload_json TEXT NOT NULL, state TEXT NOT NULL,
                result_json TEXT, error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                PRIMARY KEY(thread_id, request_id)
            )""")

    def defer_db_effect(self, db_callback, after_commit=None) -> bool:
        effects = getattr(self._local, "effects", None)
        if effects is None:
            return False
        effects.append((db_callback, after_commit))
        return True

    def run(self, thread_id, body, callback, *, queued=False, on_complete_db=None):
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
        with self.store.database_operation("submission_receipts.reserve", priority="foreground"), self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            inserted = db.execute(
                "INSERT OR IGNORE INTO browser_submission_receipts VALUES (?, ?, ?, ?, 'accepted', NULL, NULL, ?, ?)",
                (thread_id, key, digest, encoded, now, now),
            )
            if inserted.rowcount == 0:
                row = db.execute(
                    "SELECT * FROM browser_submission_receipts WHERE thread_id=? AND request_id=?",
                    (thread_id, key),
                ).fetchone()
                if row:
                    if row["payload_hash"] != digest:
                        raise CoordinationError("Submission request ID was already used for different input.")
                    if row["state"] == "completed":
                        return json.loads(row["result_json"])
                    if row["state"] != "rejected":
                        raise SubmissionUncertainError("This submission has an unconfirmed outcome. Check the conversation before sending again; it has not been replayed.")
                    db.execute(
                        "UPDATE browser_submission_receipts SET state='accepted', error=NULL, updated_at=? WHERE thread_id=? AND request_id=?",
                        (now, thread_id, key),
                    )
        # No database transaction spans the external operation.
        outermost = getattr(self._local, "effects", None) is None
        if outermost:
            self._local.effects = []
        try:
            result = callback()
            effects = list(self._local.effects) if outermost else []
        except Exception as error:
            from .codex_app_server import BrowserBusyError, BrowserNotSubmittedError
            state = "rejected" if isinstance(error, (BrowserBusyError, BrowserNotSubmittedError)) else "uncertain"
            try:
                with self.store.database_operation("submission_receipts.fail", priority="foreground"), self.store._connection() as db:
                    db.execute("UPDATE browser_submission_receipts SET state=?, error=?, updated_at=? WHERE thread_id=? AND request_id=?",
                               (state, str(error)[:2000], self.store.clock(), thread_id, key))
            except Exception:
                # The durable accepted record already prevents a replay even
                # when failure bookkeeping cannot be committed.
                pass
            if state == "uncertain" and not str(body.get("message", "")).lstrip().startswith("/"):
                raise SubmissionUncertainError(str(error)) from error
            raise
        finally:
            if outermost:
                self._local.effects = None
        effect_failure: list[Exception] = []
        try:
            if result.get("acceptedMessage"):
                result = {**result, "acceptedMessage": {**result["acceptedMessage"],
                          "threadId": thread_id, "requestId": key}}
            with self.store.database_operation("submission_receipts.complete", priority="foreground"), self.store._connection() as db:
                self.store._writer.record_scope_search_thread(thread_id)
                db.execute("BEGIN IMMEDIATE")
                with self.store.sharing_transaction(db):
                    if effects:
                        try:
                            for db_callback, _ in effects:
                                db_callback(db)
                        except Exception as effect_error:
                            db.rollback()
                            db.execute("BEGIN IMMEDIATE")
                            from .codex_app_server import BrowserBusyError, BrowserNotSubmittedError
                            state = "rejected" if isinstance(effect_error, (BrowserBusyError, BrowserNotSubmittedError)) else "uncertain"
                            try:
                                db.execute(
                                    "UPDATE browser_submission_receipts SET state=?, error=?, updated_at=? WHERE thread_id=? AND request_id=?",
                                    (state, str(effect_error)[:2000], self.store.clock(), thread_id, key),
                                )
                            except Exception:
                                pass
                            effect_failure.append(effect_error)
                    if not effect_failure:
                        db.execute("UPDATE browser_submission_receipts SET state='completed', result_json=?, updated_at=? WHERE thread_id=? AND request_id=?",
                                   (json.dumps(result), self.store.clock(), thread_id, key))
                        if on_complete_db is not None:
                            on_complete_db(db, result)
        except Exception as error:
            raise SubmissionUncertainError("Your submission may have been received, but its receipt could not be saved. Check the conversation before sending again.") from error
        if effect_failure:
            if state == "uncertain":
                raise SubmissionUncertainError(str(effect_failure[0])) from effect_failure[0]
            raise effect_failure[0]
        for _, after_commit in effects:
            if after_commit is not None:
                after_commit()
        return result

    def accepted(self, thread_id, history, *, active_turn=None):
        """Restore provider acknowledgements for the visible canonical turns.

        The existing receipt is the durable authority. Reserved, uncertain,
        rejected, queued and command submissions never become accepted echoes.
        Select only the small projection, not input images or provider results.
        """
        turns = [turn["id"] for turn in history.get("turns", []) if turn.get("id")]
        if active_turn and active_turn not in turns:
            turns.append(active_turn)
        if not turns:
            return []
        with self.store.database_operation("send.read_accepted", priority="foreground"), self.store.reading(), self.store._connection() as db:
            rows = db.execute("""SELECT json_extract(result_json, '$.acceptedMessage') AS message
                FROM browser_submission_receipts WHERE thread_id=? AND state='completed'
                AND json_extract(result_json, '$.acceptedMessage.turnId') IN (SELECT value FROM json_each(?))
                ORDER BY updated_at, rowid""", (thread_id, json.dumps(turns))).fetchall()
        return [json.loads(row["message"]) for row in rows]
