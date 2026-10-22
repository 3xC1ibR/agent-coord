"""Wake idle app conversations from the durable coordination inbox.

Claim before submitting a provider turn. An uncertain submission is never
retried automatically, including after a runtime restart.
"""
from __future__ import annotations

import logging
import threading

from .workspaces import matches_workspace
from .app_agent_failures import notify_agent_failure
from .store import CoordinationError
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
            db.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in db.execute("PRAGMA table_info(browser_inbox_wake)")}
            for column in ("pause_kind", "recovery_turn_id"):
                if column not in columns:
                    db.execute(f"ALTER TABLE browser_inbox_wake ADD COLUMN {column} TEXT")

    def pause(self, thread_id, reason, *, kind="unknown"):
        with self.store._connection() as db:
            db.execute("""INSERT INTO browser_inbox_wake (thread_id, paused, error, pause_kind)
                VALUES (?, 1, ?, ?) ON CONFLICT(thread_id) DO UPDATE SET paused = 1,
                error = CASE WHEN pause_kind = 'stop' THEN error ELSE excluded.error END,
                pause_kind = CASE WHEN pause_kind = 'stop' THEN 'stop' ELSE excluded.pause_kind END,
                recovery_turn_id = NULL""", (thread_id, reason, kind))

    def resume(self, thread_id):
        """Accepted user input re-enables wakes, including after deliberate Stop."""
        with self.store._connection() as db:
            db.execute("""UPDATE browser_inbox_wake SET paused = 0, dispatching = 0,
                error = NULL, pause_kind = NULL, recovery_turn_id = NULL WHERE thread_id = ?""",
                       (thread_id,))

    def recover(self, thread_id, expected_turn_id, requester):
        """Let the creating orchestrator authorize one continuation after failure.

        This unpauses fresh inbox work; it never replays the failed provider
        prompt or resets uncertain wake attempts. Stop/Close remain user gates.
        """
        if not self.sessions.persistence.flush():
            raise CoordinationError("Conversation state is still being saved. Try again shortly.")
        with self.sessions._thread_lock(thread_id):
            detail = self.sessions.read(thread_id)
            turns = detail["thread"].get("turns", [])
            if (detail["running"] or not turns or turns[-1].get("id") != expected_turn_id
                    or turns[-1].get("status") != "failed"):
                raise CoordinationError("The expected failed turn changed or is running. Inspect the conversation before recovery.")
            with self.store._connection() as db:
                db.execute("BEGIN IMMEDIATE")
                if not db.execute("""SELECT 1 FROM app_control_requests WHERE operation = 'create'
                        AND thread_id = ? AND sender_session_id = ? AND status = 'completed'""",
                        (thread_id, requester)).fetchone():
                    raise CoordinationError("Only this app agent's creating orchestrator can recover it.")
                if self.store.threads.get(thread_id)["attention"] == "archived":
                    raise CoordinationError("Recovery cannot reopen a closed conversation.")
                if db.execute("SELECT 1 FROM sqlite_master WHERE name = 'thread_close_requests'").fetchone() and db.execute(
                        "SELECT 1 FROM thread_close_requests WHERE thread_id = ? AND status IN ('queued', 'closing')",
                        (thread_id,)).fetchone():
                    raise CoordinationError("The conversation has a pending close request.")
                if db.execute("SELECT 1 FROM browser_message_queue WHERE thread_id = ?", (thread_id,)).fetchone():
                    raise CoordinationError("Queued user input takes priority. Review it in the app before recovery.")
                wake = db.execute("SELECT * FROM browser_inbox_wake WHERE thread_id = ?", (thread_id,)).fetchone()
                if not wake or not wake["paused"] or wake["pause_kind"] != "failure" or wake["dispatching"]:
                    raise CoordinationError("Only a confirmed failure pause can be recovered. Stop or uncertain wake needs direct user input.")
                if not db.execute("""SELECT 1 FROM messages m
                        LEFT JOIN message_wake_attempts a ON a.message_id = m.id
                        WHERE m.recipient_session_id = ? AND m.delivered_at IS NULL
                        AND m.classification = 'action_required' AND m.suppressed_at IS NULL
                        AND a.message_id IS NULL AND m.thread_id NOT LIKE 'event:%'""", (thread_id,)).fetchone():
                    raise CoordinationError("Queue a fresh actionable continuation before recovery; uncertain submissions are never replayed.")
                db.execute("""UPDATE browser_inbox_wake SET paused = 0, error = NULL,
                    pause_kind = NULL, recovery_turn_id = ? WHERE thread_id = ?""", (expected_turn_id, thread_id))
            return {"session_id": thread_id, "resumed_after_turn_id": expected_turn_id, "state": "ready"}

    def _eligible(self, db, thread_id, *, submitting=False):
        if self.stopped.is_set() or self.sessions.closed:
            return False
        from .sidecars import discard_stale_messages
        discard_stale_messages(db, thread_id, self.store.clock())
        row = db.execute("""SELECT s.turn_active, t.attention,
                w.paused, w.dispatching
            FROM browser_sessions b JOIN work_threads t ON t.thread_id = b.thread_id
            JOIN sessions s ON s.session_id = b.thread_id
            LEFT JOIN browser_inbox_wake w ON w.thread_id = b.thread_id
            WHERE b.thread_id = ?""", (thread_id,)).fetchone()
        if (not row or row["turn_active"] or row["paused"] or (row["dispatching"] and not submitting)):
            return False
        if row["attention"] == "archived" and not db.execute("""SELECT 1 FROM messages
                WHERE recipient_session_id = ? AND delivered_at IS NULL
                AND classification = 'action_required' AND thread_id NOT LIKE 'event:%'""", (thread_id,)).fetchone():
            # Stored subscriptions wait for explicit reopening. Avoid even
            # loading/unarchiving a cold provider conversation for these events.
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
                AND (m.thread_id NOT LIKE 'event:%' OR EXISTS (
                    SELECT 1 FROM work_threads t WHERE t.thread_id = a.session_id AND t.attention != 'archived'))
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
                AND (m.thread_id NOT LIKE 'event:%' OR EXISTS (
                    SELECT 1 FROM work_threads t WHERE t.thread_id = m.recipient_session_id AND t.attention != 'archived'))
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
                    with self.store._connection() as db:
                        recovery = db.execute("SELECT recovery_turn_id FROM browser_inbox_wake WHERE thread_id = ?",
                                              (thread_id,)).fetchone()
                    if not (turns[-1].get("status") == "failed" and recovery and recovery[0] == turns[-1].get("id")):
                        self.pause(thread_id, "The last turn stopped or failed. Send a message to continue.",
                                   kind="failure" if turns[-1].get("status") == "failed" else "stop")
                        return
            except Exception as exc:
                self.pause(thread_id, str(exc))
                self._report_failure(thread_id, str(exc))
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
                self._report_failure(thread_id, str(exc))
            else:
                self.store.complete_wake_attempts(thread_id, ids, outcome="sent", detail=WAKE_PROMPT)
            finally:
                with self.store._connection() as db:
                    db.execute("UPDATE browser_inbox_wake SET dispatching = 0 WHERE thread_id = ?", (thread_id,))

    def _report_failure(self, thread_id, error):
        self.sessions.persistence.submit(lambda: self._persist_failure(thread_id, error))

    def _persist_failure(self, thread_id, error):
        with self.store._connection() as db:
            ids = [row[0] for row in db.execute("""SELECT id FROM messages
                WHERE recipient_session_id = ? AND delivered_at IS NULL
                AND classification = 'action_required' ORDER BY id""", (thread_id,))]
        notify_agent_failure(self.store, thread_id, event_key=["wake", ids], stage="wake", error=error)

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
