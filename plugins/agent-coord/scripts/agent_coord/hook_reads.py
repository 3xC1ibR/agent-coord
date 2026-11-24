"""Read a single committed snapshot when a tool hook cannot reach the runtime."""
from __future__ import annotations

from contextlib import closing, contextmanager, nullcontext
import os
import sqlite3
import time

from .store import CoordinationError, CoordinationStore, DEFAULT_STALE_AFTER_SECONDS


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
        row = self.connection.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        return self._row_to_session(row) if row is not None else None

    def get_session(self, session_id):
        session = self.find_session(session_id)
        if session is None:
            raise CoordinationError(f"Session {session_id} is not registered.")
        return session

    def unregistered_write_reason(self, cwd):
        # SessionStart itself may have arrived during the outage. A new
        # writer needs a declaration only when a live incumbent is active.
        peers = [row for row in self._live_rows(self.connection, cwd) if self._row_has_active_work(row)]
        if peers:
            owners = ", ".join(row["session_id"] for row in peers)
            return f"Active sessions in this workspace ({owners}) require a write scope. Register and run agent-coord begin-work."
        return None

    def pending_message(self, session_id):
        # No delivery reservation is possible without the writer. Return a
        # pointer only, so the same actionable body cannot execute twice.
        row = self.connection.execute("""SELECT m.id FROM messages m
            WHERE m.recipient_session_id=? AND m.delivered_at IS NULL AND m.suppressed_at IS NULL
            AND m.classification IN ('action_required','deferred')
            AND NOT EXISTS (SELECT 1 FROM message_delivery_attempts a WHERE a.message_id=m.id)
            ORDER BY m.id LIMIT 1""", (session_id,)).fetchone()
        return row[0] if row else None


@contextmanager
def hook_snapshot(database):
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=.25)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        yield HookReadSnapshot(db)
