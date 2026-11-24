"""Durable identity for a browser session creation whose reply can be lost."""
from __future__ import annotations

import hashlib
import json

from .store import CoordinationError
from .submission_receipts import SubmissionOutcomeUnknownError
from .workspaces import WorkspaceAccessError, matches_workspace


class SessionCreationReceipts:
    def __init__(self, sessions):
        self.sessions = sessions
        self.store = sessions.store
        with self.store.database_operation("session.creation.initialize", priority="background"), self.store._connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS browser_session_creation_receipts (
                request_id TEXT PRIMARY KEY NOT NULL, payload_hash TEXT NOT NULL, launch_cwd TEXT NOT NULL,
                thread_id TEXT, result_json TEXT,
                CHECK ((thread_id IS NULL) = (result_json IS NULL)))""")

    def create(self, body):
        request_id = body["requestId"]
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 200:
            raise CoordinationError("Session creation request ID must contain 1–200 characters.")
        settings = {key: value for key, value in body.items() if key != "requestId"}
        encoded = json.dumps(settings, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        receipt = self._lookup(request_id)
        if receipt is not None:
            return self._replay(receipt, digest)

        # Validation may load provider settings. Complete it before acquiring
        # writer admission, and allow definite validation errors to be corrected.
        record, associations = self.sessions._creation_settings(settings)
        with self.store.database_operation("session.creation.reserve"), self.store._connection() as db:
            reserved = db.execute("""INSERT INTO browser_session_creation_receipts(request_id, payload_hash, launch_cwd)
                VALUES (?, ?, ?) ON CONFLICT(request_id) DO NOTHING""", (request_id, digest, record["cwd"])).rowcount
        if not reserved:
            return self._replay(self._lookup(request_id), digest)

        # A reserved receipt survives coordinator replacement. A failed native
        # call or failed completion write never permits another native start.
        try:
            result = self.sessions._create_session(settings, record, associations)
            thread_id = result["session"]["thread_id"]
            payload = json.dumps(result, separators=(",", ":"))
            with self.store.database_operation("session.creation.complete"), self.store._connection() as db:
                completed = db.execute("""UPDATE browser_session_creation_receipts SET thread_id=?, result_json=?
                    WHERE request_id=? AND thread_id IS NULL""", (thread_id, payload, request_id)).rowcount
                if completed != 1:
                    raise CoordinationError("The session creation receipt could not be completed.")
        except Exception as error:
            raise SubmissionOutcomeUnknownError("The session may have been created, but its result could not be confirmed. "
                                                "Check your conversations before starting another session.") from error
        return result

    def _lookup(self, request_id):
        with self.store.reading(), self.store._connection() as db:
            return db.execute("""SELECT payload_hash, launch_cwd, thread_id, result_json
                FROM browser_session_creation_receipts WHERE request_id=?""", (request_id,)).fetchone()

    def _replay(self, receipt, digest):
        thread_id = receipt["thread_id"]
        if thread_id is not None:
            self.sessions._authorize_history(thread_id)
        elif not matches_workspace(receipt["launch_cwd"], self.sessions.cwd):
            raise WorkspaceAccessError()
        if receipt["payload_hash"] != digest:
            raise CoordinationError("Session creation request ID was already used with different settings.")
        if thread_id is None:
            raise SubmissionOutcomeUnknownError("This session creation has an unconfirmed outcome. "
                                                "Check your conversations before starting another session.")
        try:
            return json.loads(receipt["result_json"])
        except (TypeError, ValueError) as error:
            raise SubmissionOutcomeUnknownError("The session was created, but its saved result could not be read.") from error
