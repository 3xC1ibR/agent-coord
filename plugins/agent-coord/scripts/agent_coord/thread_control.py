"""Durable lifecycle requests shared by the CLI and the owning UI runtime."""
from __future__ import annotations

import logging
import threading
import uuid

from .store import CoordinationError
from .workspaces import matches_workspace

RUNTIME_LEASE_SECONDS = 60


def cancel_pending(db, thread_id: str, now: float) -> None:
    """Called inside the transaction accepting new user input or a new turn."""
    if db.execute("SELECT 1 FROM sqlite_master WHERE name = 'thread_close_requests'").fetchone():
        db.execute("""UPDATE thread_close_requests SET status = 'cancelled', updated_at = ?,
                      error = 'New input superseded the pending close request.'
                      WHERE thread_id = ? AND status = 'queued'""", (now, thread_id))


def finish_close(store, thread_id: str) -> None:
    """Shared final state transition, after the runtime has stopped execution."""
    store.disable_wake(thread_id)
    store.end_work(thread_id)
    store.end_session(thread_id)
    store.threads.update(thread_id, attention="archived")


class ThreadControl:
    def __init__(self, store):
        self.store = store
        with store._connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS thread_close_requests (
                    request_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL REFERENCES work_threads(thread_id),
                    turn_key TEXT, after_turn INTEGER NOT NULL,
                    status TEXT NOT NULL, error TEXT, owner TEXT,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_pending_thread_close
                    ON thread_close_requests(thread_id) WHERE status IN ('queued', 'closing');
                CREATE TABLE IF NOT EXISTS thread_control_runtimes (
                    id TEXT PRIMARY KEY, heartbeat REAL NOT NULL
                );
            """)

    def _reconcile(self, db):
        now = self.store.clock()
        # A new turn also cancels requests from clients predating cancel_pending.
        db.execute("""UPDATE thread_close_requests SET status = 'cancelled', updated_at = ?,
                      error = 'A new turn superseded the pending close request.'
                      WHERE status = 'queued' AND turn_key IS NOT
                      (SELECT turn_key FROM work_threads WHERE thread_id = thread_close_requests.thread_id)""", (now,))
        abandoned = db.execute("""SELECT r.* FROM thread_close_requests r
            LEFT JOIN thread_control_runtimes runtime ON runtime.id = r.owner
            WHERE r.status = 'closing' AND r.updated_at < ?
            AND (runtime.heartbeat IS NULL OR runtime.heartbeat < ?)""",
            (now - RUNTIME_LEASE_SECONDS, now - RUNTIME_LEASE_SECONDS)).fetchall()
        for request in abandoned:
            thread = db.execute("""SELECT t.attention, s.ended_at FROM work_threads t
                JOIN sessions s ON s.session_id = t.thread_id WHERE t.thread_id = ?""", (request["thread_id"],)).fetchone()
            closed = thread["attention"] == "archived" and thread["ended_at"] is not None
            db.execute("UPDATE thread_close_requests SET status = ?, error = ?, updated_at = ? WHERE request_id = ?",
                       ("closed" if closed else "failed", None if closed else
                        "The closing runtime stopped. Inspect the thread and retry close.", now, request["request_id"]))

    def _describe(self, db, row):
        result = dict(row)
        result["after_turn"] = bool(result["after_turn"])
        result.pop("owner", None)
        result["waiting_for"] = None
        if result["status"] == "queued":
            session = db.execute("SELECT turn_active FROM sessions WHERE session_id = ?", (result["thread_id"],)).fetchone()
            if result["after_turn"] and session["turn_active"]:
                result["waiting_for"] = "turn"
            else:
                result["waiting_for"] = "runtime"
            result["message"] = "Queued for the Ribbon Field runtime. If it is not running, start or restart the updated app."
        return result

    def request_close(self, thread_id: str, *, after_turn=False) -> dict:
        self.store.threads.ensure(thread_id)
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._reconcile(db)
            thread = db.execute("""SELECT t.turn_key, t.attention, s.turn_active, s.ended_at
                FROM work_threads t JOIN sessions s ON s.session_id = t.thread_id
                WHERE t.thread_id = ?""", (thread_id,)).fetchone()
            if thread["attention"] == "archived" and thread["ended_at"] is not None:
                return {"thread_id": thread_id, "status": "closed", "request_id": None}
            if after_turn and thread["turn_active"] and not thread["turn_key"]:
                raise CoordinationError("The active turn has no recorded identity; cannot safely queue an after-turn close.")
            existing = db.execute("SELECT * FROM thread_close_requests WHERE thread_id = ? AND status IN ('queued', 'closing')", (thread_id,)).fetchone()
            if existing:
                if bool(existing["after_turn"]) != bool(after_turn):
                    raise CoordinationError("A different close is already pending. Use thread cancel-close before changing its mode.")
                request_id = existing["request_id"]
            else:
                request_id = str(uuid.uuid4())
                now = self.store.clock()
                db.execute("""INSERT INTO thread_close_requests
                    (request_id, thread_id, turn_key, after_turn, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 'queued', ?, ?)""",
                    (request_id, thread_id, thread["turn_key"], int(after_turn), now, now))
        # An already-ended terminal needs no running provider to release its
        # declaration and close its thread. Browser archival belongs to its runtime.
        if thread["ended_at"] is not None and not self.store.threads.is_browser_session(thread_id):
            if self.claim(request_id, "offline"):
                try:
                    finish_close(self.store, thread_id)
                except Exception as exc:
                    self.complete(request_id, error=str(exc))
                else:
                    self.complete(request_id)
        return self.status(thread_id)

    def status(self, thread_id: str) -> dict:
        self.store.threads.ensure(thread_id)
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._reconcile(db)
            row = db.execute("SELECT * FROM thread_close_requests WHERE thread_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1", (thread_id,)).fetchone()
            if row:
                return self._describe(db, row)
            thread = db.execute("""SELECT t.attention, s.ended_at FROM work_threads t
                JOIN sessions s ON s.session_id = t.thread_id WHERE t.thread_id = ?""", (thread_id,)).fetchone()
            closed = thread["attention"] == "archived" and thread["ended_at"] is not None
            return {"thread_id": thread_id, "status": "closed" if closed else "none", "request_id": None}

    def cancel(self, thread_id: str) -> dict:
        self.store.threads.ensure(thread_id)
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._reconcile(db)
            if db.execute("SELECT 1 FROM thread_close_requests WHERE thread_id = ? AND status = 'closing'", (thread_id,)).fetchone():
                raise CoordinationError("Close is already executing. Wait for its result before reopening the thread.")
            db.execute("""UPDATE thread_close_requests SET status = 'cancelled', error = 'Cancelled explicitly.',
                          updated_at = ? WHERE thread_id = ? AND status = 'queued'""", (self.store.clock(), thread_id))
        return self.status(thread_id)

    def queued(self) -> list[dict]:
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._reconcile(db)
            return [dict(row) for row in db.execute("SELECT * FROM thread_close_requests WHERE status = 'queued' ORDER BY created_at, rowid")]

    def claim(self, request_id: str, owner: str) -> bool:
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._reconcile(db)
            row = db.execute("""SELECT r.*, s.turn_active, s.ended_at FROM thread_close_requests r
                JOIN sessions s ON s.session_id = r.thread_id WHERE request_id = ?""", (request_id,)).fetchone()
            if not row or row["status"] != "queued":
                return False
            if row["after_turn"] and row["ended_at"] is None:
                if row["turn_active"]:
                    return False
                if row["turn_key"] and not db.execute("SELECT 1 FROM turn_completions WHERE thread_id = ? AND turn_key = ?",
                                                      (row["thread_id"], row["turn_key"])).fetchone():
                    return False
            return bool(db.execute("""UPDATE thread_close_requests SET status = 'closing', owner = ?, updated_at = ?
                WHERE request_id = ? AND status = 'queued'""", (owner, self.store.clock(), request_id)).rowcount)

    def complete(self, request_id: str, *, error=None) -> None:
        with self.store._connection() as db:
            db.execute("""UPDATE thread_close_requests SET status = ?, error = ?, updated_at = ?
                          WHERE request_id = ? AND status = 'closing'""",
                       ("failed" if error is not None else "closed", error, self.store.clock(), request_id))


class ThreadControlWorker:
    """Run close requests outside agent sandboxes and the provider reader thread."""
    def __init__(self, sessions):
        self.sessions = sessions
        self.control = ThreadControl(sessions.store)
        self.owner = str(uuid.uuid4())
        self.stopped = threading.Event()
        self.worker = None

    def start(self):
        self.worker = threading.Thread(target=self._run, name="thread-control", daemon=True)
        self.worker.start()

    def _heartbeat(self):
        with self.sessions.store._connection() as db:
            db.execute("INSERT OR REPLACE INTO thread_control_runtimes VALUES (?, ?)",
                       (self.owner, self.sessions.store.clock()))

    def process_once(self):
        sessions = self.sessions
        self._heartbeat()
        for request in self.control.queued():
            if self.stopped.is_set() or sessions.closed:
                return
            thread_id = request["thread_id"]
            session = sessions.store.get_session(thread_id)
            if not matches_workspace(session["cwd"], sessions.cwd):
                continue
            with sessions._thread_lock(thread_id):
                with sessions.lock:
                    loaded = thread_id in sessions.loaded
                    running = thread_id in sessions.active
                browser = sessions.store.threads.is_browser_session(thread_id)
                # A different app-server must not take over an active/loaded
                # browser session. Ended browser sessions may be archived cold.
                if browser and not loaded and session["ended_at"] is None:
                    continue
                if request["after_turn"] and running:
                    continue
                if not self.control.claim(request["request_id"], self.owner):
                    continue
                try:
                    sessions.close_work_thread(thread_id)
                except Exception as exc:
                    self.control.complete(request["request_id"], error=str(exc))
                else:
                    self.control.complete(request["request_id"])

    def _run(self):
        while not self.stopped.is_set() and not self.sessions.closed:
            try:
                self.process_once()
            except Exception:
                logging.getLogger(__name__).exception("Thread lifecycle worker failed")
            self.stopped.wait(.25)

    def close(self):
        self.stopped.set()
        if self.worker is not None:
            self.worker.join(timeout=5)
        with self.sessions.store._connection() as db:
            db.execute("DELETE FROM thread_control_runtimes WHERE id = ?", (self.owner,))
