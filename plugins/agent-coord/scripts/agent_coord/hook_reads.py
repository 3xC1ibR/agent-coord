"""Read a single committed snapshot for PreToolUse checks without runtime roundtrips."""
from __future__ import annotations

from contextlib import closing, contextmanager, nullcontext
import os
import sqlite3
import time

from .store import CoordinationError, CoordinationStore, DEFAULT_STALE_AFTER_SECONDS


def find_session(store, session_id):
    """Look up a registered session without raising when absent."""
    if not isinstance(session_id, str) or not session_id:
        return None
    try:
        with store.reading(), store._connection() as connection:
            row = connection.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
            return store._row_to_session(row) if row is not None else None
    except sqlite3.OperationalError as error:
        if "no such table" in str(error).lower():
            return None
        raise


def unregistered_write_reason(store, cwd):
    """Require a write scope only when another live session has active work in cwd."""
    try:
        with store.reading(), store._connection() as connection:
            peers = [row for row in store._live_rows(connection, cwd) if store._row_has_active_work(row)]
    except sqlite3.OperationalError as error:
        if "no such table" in str(error).lower():
            return None
        raise
    if peers:
        owners = ", ".join(row["session_id"] for row in peers)
        return f"Active sessions in this workspace ({owners}) require a write scope. Register and run agent-coord begin-work."
    return None


class HookReadSnapshot(CoordinationStore):
    """Run the existing lease queries on one read-only connection, without migrations."""

    clock = staticmethod(time.time)
    reading = staticmethod(nullcontext)

    def __init__(self, connection):
        self.connection = connection
        self.stale_after_seconds = int(os.environ.get("AGENT_COORD_STALE_AFTER_SECONDS")
                                       or DEFAULT_STALE_AFTER_SECONDS)

    def _connection(self):
        return nullcontext(self.connection)

    def find_session(self, session_id):
        return find_session(self, session_id)

    def get_session(self, session_id):
        session = self.find_session(session_id)
        if session is None:
            raise CoordinationError(f"Session {session_id} is not registered.")
        return session

    def unregistered_write_reason(self, cwd):
        return unregistered_write_reason(self, cwd)

    def pending_message(self, session_id):
        # No delivery reservation is possible without the writer. Return a
        # pointer only, so the same actionable body cannot execute twice.
        if not isinstance(session_id, str) or not session_id:
            return None
        try:
            row = self.connection.execute("""SELECT m.id FROM messages m
                WHERE m.recipient_session_id=? AND m.delivered_at IS NULL AND m.suppressed_at IS NULL
                AND m.classification IN ('action_required','deferred')
                AND NOT EXISTS (SELECT 1 FROM message_delivery_attempts a WHERE a.message_id=m.id)
                ORDER BY m.id LIMIT 1""", (session_id,)).fetchone()
        except sqlite3.OperationalError as error:
            if "no such table" in str(error).lower():
                return None
            raise
        return row[0] if row else None


@contextmanager
def hook_snapshot(database):
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=.25)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA busy_timeout=250")
        db.execute("BEGIN")
        try:
            yield HookReadSnapshot(db)
        finally:
            if db.in_transaction:
                db.rollback()
