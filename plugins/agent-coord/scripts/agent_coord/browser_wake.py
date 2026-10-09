"""Wake idle app conversations from the durable coordination inbox.

Claim before submitting a provider turn. An uncertain submission is never
retried automatically, including after a runtime restart.
"""
from __future__ import annotations

import logging
import threading

from .workspaces import matches_workspace
from .zellij_wake import WAKE_PROMPT


class BrowserInboxWake:
    def __init__(self, sessions):
        self.sessions = sessions
        self.store = sessions.store
        self.stopped = threading.Event()
        self.worker = None
        with self.store._connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS browser_inbox_wake (
                thread_id TEXT PRIMARY KEY, paused INTEGER NOT NULL DEFAULT 0,
                dispatching INTEGER NOT NULL DEFAULT 0, error TEXT
            )""")

    def pause(self, thread_id, reason):
        with self.store._connection() as db:
            db.execute("""INSERT INTO browser_inbox_wake (thread_id, paused, error)
                VALUES (?, 1, ?) ON CONFLICT(thread_id) DO UPDATE SET paused = 1, error = excluded.error""",
                (thread_id, reason))

    def resume(self, thread_id):
        """Only accepted user input re-enables wakes after Stop or a failure."""
        with self.store._connection() as db:
            db.execute("UPDATE browser_inbox_wake SET paused = 0, dispatching = 0, error = NULL WHERE thread_id = ?",
                       (thread_id,))

    def _eligible(self, db, thread_id, *, submitting=False):
        if self.stopped.is_set() or self.sessions.closed:
            return False
        row = db.execute("""SELECT s.turn_active,
                w.paused, w.dispatching
            FROM browser_sessions b JOIN work_threads t ON t.thread_id = b.thread_id
            JOIN sessions s ON s.session_id = b.thread_id
            LEFT JOIN browser_inbox_wake w ON w.thread_id = b.thread_id
            WHERE b.thread_id = ?""", (thread_id,)).fetchone()
        if (not row or row["turn_active"] or row["paused"] or (row["dispatching"] and not submitting)):
            return False
        # User follow-ups, including paused ones, take priority over inbox work.
        if db.execute("SELECT 1 FROM browser_message_queue WHERE thread_id = ?", (thread_id,)).fetchone():
            return False
        if db.execute("SELECT 1 FROM sqlite_master WHERE name = 'thread_close_requests'").fetchone():
            if db.execute("""SELECT 1 FROM thread_close_requests WHERE thread_id = ?
                    AND status IN ('queued', 'closing')""", (thread_id,)).fetchone():
                return False
        return True

    def can_submit(self, thread_id):
        with self.store._connection() as db:
            return self._eligible(db, thread_id, submitting=True) and bool(db.execute("""
                SELECT 1 FROM message_wake_attempts a JOIN messages m ON m.id = a.message_id
                WHERE a.session_id = ? AND a.outcome = 'claimed'
                AND m.delivered_at IS NULL AND m.classification = 'action_required'
                """, (thread_id,)).fetchone())

    def _claim(self, thread_id):
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if not self._eligible(db, thread_id):
                return []
            rows = db.execute("""SELECT m.id FROM messages m
                LEFT JOIN message_wake_attempts a ON a.message_id = m.id
                WHERE m.recipient_session_id = ? AND m.delivered_at IS NULL
                AND m.classification = 'action_required' AND a.message_id IS NULL
                ORDER BY m.id""", (thread_id,)).fetchall()
            ids = [row[0] for row in rows]
            if ids:
                db.execute("""INSERT INTO browser_inbox_wake (thread_id, dispatching)
                    VALUES (?, 1) ON CONFLICT(thread_id) DO UPDATE SET dispatching = 1""", (thread_id,))
                db.executemany("""INSERT INTO message_wake_attempts
                    (message_id, session_id, attempted_at, outcome, detail)
                    VALUES (?, ?, ?, 'claimed', 'App inbox wake')""",
                    [(message_id, thread_id, self.store.clock()) for message_id in ids])
            return ids

    def process_once(self):
        # Read just pending recipients, not conversation histories, on each tick.
        with self.store._connection() as db:
            rows = db.execute("""SELECT DISTINCT b.thread_id, b.cwd FROM messages m
                JOIN browser_sessions b ON b.thread_id = m.recipient_session_id
                LEFT JOIN message_wake_attempts a ON a.message_id = m.id
                WHERE m.delivered_at IS NULL AND m.classification = 'action_required'
                AND a.message_id IS NULL""").fetchall()
        for row in rows:
            if self.stopped.is_set() or self.sessions.closed:
                return
            if not matches_workspace(row["cwd"], self.sessions.cwd):
                continue
            try:
                self._dispatch(row["thread_id"])
            except Exception:
                logging.getLogger(__name__).exception("App inbox wake failed for %s", row["thread_id"])

    def _dispatch(self, thread_id):
        from .codex_app_server import BrowserBusyError
        sessions = self.sessions
        with sessions._thread_lock(thread_id):
            with self.store._connection() as db:
                if not self._eligible(db, thread_id):
                    return
            with sessions.lock:
                if thread_id in sessions.active:
                    return
            # Resuming a cold conversation and reading provider status can fail
            # before a turn is sent. Pause instead of repeatedly launching it.
            try:
                detail = sessions.read(thread_id)
                if detail["running"]:
                    return
                turns = detail["thread"].get("turns", [])
                if turns and turns[-1].get("status") in {"interrupted", "failed"}:
                    self.pause(thread_id, "The last turn stopped or failed. Send a message to continue.")
                    return
            except Exception as exc:
                self.pause(thread_id, str(exc))
                return
            ids = self._claim(thread_id)
            if not ids:
                return
            try:
                result = sessions.send(thread_id, {"message": WAKE_PROMPT}, start_only=True, automatic=True)
                turn_id = result.get("turn", {}).get("id")
                if turn_id:
                    with self.store._connection() as db:
                        db.executemany("UPDATE messages SET wake_turn_id = ? WHERE id = ? AND recipient_session_id = ?",
                                       [(turn_id, message_id, thread_id) for message_id in ids])
            except BrowserBusyError:
                # Rejected before RPC: another turn, Stop, closure, or delivery
                # won the race. Leave undelivered work eligible for a later tick.
                with self.store._connection() as db:
                    db.executemany("DELETE FROM message_wake_attempts WHERE message_id = ? AND outcome = 'claimed'",
                                   [(message_id,) for message_id in ids])
            except Exception as exc:
                self.store.complete_wake_attempts(thread_id, ids, outcome="failed", detail=str(exc))
                self.pause(thread_id, "Inbox wake may have started; send a message to continue. " + str(exc))
            else:
                self.store.complete_wake_attempts(thread_id, ids, outcome="sent", detail=WAKE_PROMPT)
            finally:
                with self.store._connection() as db:
                    db.execute("UPDATE browser_inbox_wake SET dispatching = 0 WHERE thread_id = ?", (thread_id,))

    def start(self):
        if self.worker is None:
            self.worker = threading.Thread(target=self._run, name="browser-inbox-wake", daemon=True)
            self.worker.start()

    def _run(self):
        while not self.stopped.is_set() and not self.sessions.closed:
            try:
                self.process_once()
            except Exception:
                logging.getLogger(__name__).exception("App inbox wake worker failed")
            self.stopped.wait(.5)

    def close(self):
        self.stopped.set()
        if self.worker is not None:
            self.worker.join(timeout=5)
