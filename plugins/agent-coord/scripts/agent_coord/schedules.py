"""One-time prompts. Persist intent before RPC; never retry an uncertain send."""
from __future__ import annotations

import json
import logging
import math
from pathlib import Path
import threading
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .store import CoordinationError
from .workspaces import matches_workspace

GRACE_SECONDS = 60
RUNTIME_LEASE_SECONDS = 120


class ScheduledPrompts:
    def __init__(self, sessions):
        self.sessions = sessions
        self.store = sessions.store
        self.owner = str(uuid.uuid4())
        self.started_at = self.store.clock()
        self.stopped = threading.Event()
        self.worker = None
        with self.store._connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS scheduled_prompts (
                    id TEXT PRIMARY KEY, cwd TEXT NOT NULL, settings_json TEXT NOT NULL,
                    message TEXT NOT NULL, run_at REAL NOT NULL, timezone TEXT NOT NULL,
                    state TEXT NOT NULL, error TEXT, thread_id TEXT, turn_id TEXT, owner TEXT,
                    version INTEGER NOT NULL DEFAULT 1, request_json TEXT NOT NULL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS scheduled_prompts_due ON scheduled_prompts(state, run_at);
                CREATE TABLE IF NOT EXISTS schedule_runtimes (id TEXT PRIMARY KEY, heartbeat REAL NOT NULL);
            """)

    @staticmethod
    def _describe(row):
        item = dict(row)
        item["settings"] = json.loads(item.pop("settings_json"))
        item.pop("request_json")
        item.pop("owner")
        return item

    def _get(self, db, schedule_id):
        row = db.execute("SELECT * FROM scheduled_prompts WHERE id = ?", (schedule_id,)).fetchone()
        if row is None or not matches_workspace(row["cwd"], self.sessions.cwd):
            raise CoordinationError("Scheduled prompt not found in this workspace.")
        return row

    def list(self):
        with self.store._connection() as db:
            rows = db.execute("SELECT * FROM scheduled_prompts ORDER BY run_at DESC, id").fetchall()
        return [self._describe(row) for row in rows if matches_workspace(row["cwd"], self.sessions.cwd)]

    def _validate(self, body):
        if set(body) - {"id", "version", "message", "run_at", "timezone", "settings"}:
            raise CoordinationError("Unknown scheduled prompt setting.")
        message = self.sessions._text(body.get("message"), "Prompt", 100000)
        if message.startswith("/"):
            raise CoordinationError("Schedule a prompt, not a slash command.")
        run_at = body.get("run_at")
        if (type(run_at) not in (int, float) or not 0 < run_at < 253402300800
                or not math.isfinite(run_at) or run_at <= self.store.clock()):
            raise CoordinationError("Choose a future date and time.")
        timezone = self.sessions._text(body.get("timezone"), "Time zone", 100)
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise CoordinationError("Choose a valid time zone.") from exc
        settings = body.get("settings")
        if not isinstance(settings, dict) or set(settings) - {"cwd", "client", "model", "effort", "yolo", "repository_id", "project_id"}:
            raise CoordinationError("Choose workspace, model, reasoning, and permissions.")
        self.sessions._text(settings.get("cwd"), "Workspace", 4096)
        self.sessions._text(settings.get("model"), "Model", 200)
        record, associations = self.sessions._creation_settings({**settings, "name": message[:80]})
        selected = self.sessions._validate_settings(record["model"], record["effort"], record["client"])
        if record["effort"] is None:
            record["effort"] = selected.get("defaultReasoningEffort")
        return message, run_at, timezone, {**record, **associations}

    def create(self, body):
        try:
            schedule_id = str(uuid.UUID(body.get("id", "")))
        except (ValueError, TypeError, AttributeError) as exc:
            raise CoordinationError("Provide a unique scheduled prompt ID.") from exc
        request = json.dumps(body, sort_keys=True)
        # A lost HTTP response must not create a second job on retry.
        with self.store._connection() as db:
            row = db.execute("SELECT * FROM scheduled_prompts WHERE id = ?", (schedule_id,)).fetchone()
            if row:
                self._get(db, schedule_id)
                if row["request_json"] != request:
                    raise CoordinationError("This prompt was already saved. Reopen it from Scheduled prompts to edit it.")
                return self._describe(row)
        message, run_at, timezone, settings = self._validate(body)
        now = self.store.clock()
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""INSERT OR IGNORE INTO scheduled_prompts
                (id, cwd, settings_json, message, run_at, timezone, state, request_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'scheduled', ?, ?, ?)""",
                (schedule_id, settings["cwd"], json.dumps(settings), message, run_at, timezone, request, now, now))
            row = self._get(db, schedule_id)
            if row["request_json"] != request:
                raise CoordinationError("This scheduled prompt ID has already been used.")
        self._changed()
        return self._describe(row)

    def change(self, schedule_id, body):
        action = body.get("action")
        values = None
        if action == "edit":
            values = self._validate({key: value for key, value in body.items() if key != "action"})
        elif action not in {"cancel", "run_now"} or set(body) - {"action", "version"}:
            raise CoordinationError("Choose edit, cancel, or run now.")
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._get(db, schedule_id)
            if type(body.get("version")) is not int or row["version"] != body["version"]:
                raise CoordinationError("This schedule changed. Reopen Scheduled prompts and try again.")
            if row["state"] not in {"scheduled", "missed"}:
                raise CoordinationError("This prompt has already started or was cancelled. Open its thread to review it.")
            now = self.store.clock()
            if action == "edit":
                message, run_at, timezone, settings = values
                db.execute("""UPDATE scheduled_prompts SET cwd = ?, settings_json = ?, message = ?, run_at = ?,
                    timezone = ?, state = 'scheduled', error = NULL, version = version + 1, updated_at = ? WHERE id = ?""",
                    (settings["cwd"], json.dumps(settings), message, run_at, timezone, now, schedule_id))
            else:
                if action == "run_now" and row["state"] != "missed":
                    raise CoordinationError("Run now is available for missed prompts.")
                db.execute("""UPDATE scheduled_prompts SET state = ?, run_at = ?, error = NULL,
                    version = version + 1, updated_at = ? WHERE id = ?""",
                    ("scheduled" if action == "run_now" else "cancelled", now if action == "run_now" else row["run_at"], now, schedule_id))
            result = self._describe(self._get(db, schedule_id))
        self._changed()
        return result

    def _changed(self):
        self.sessions._publish("browser/schedules", {})

    def _set_state(self, schedule_id, state, error=None):
        with self.store._connection() as db:
            db.execute("""UPDATE scheduled_prompts SET state = ?, error = ?, version = version + 1,
                updated_at = ? WHERE id = ? AND state IN ('starting', 'running', 'review')""",
                (state, error, self.store.clock(), schedule_id))
        self._changed()

    def event(self, method, params):
        if method not in {"turn/started", "turn/completed"}:
            return
        turn, thread_id = params.get("turn", {}), params.get("threadId")
        if not thread_id or not turn.get("id"):
            return
        with self.store._connection() as db:
            row = db.execute("""SELECT * FROM scheduled_prompts WHERE thread_id = ?
                AND state IN ('starting', 'running', 'review')""", (thread_id,)).fetchone()
            if not row or (row["turn_id"] is not None and row["turn_id"] != turn["id"]):
                return
            state = "running" if method == "turn/started" else "completed" if turn.get("status") == "completed" else "failed"
            error = None if state != "failed" else "The scheduled turn failed or was interrupted. Open its thread for details."
            db.execute("""UPDATE scheduled_prompts SET state = ?, error = ?, turn_id = ?,
                version = version + 1, updated_at = ? WHERE id = ?""",
                (state, error, turn["id"], self.store.clock(), row["id"]))
        self._changed()

    def _recover(self, row):
        # Read history without resuming or sending. An unknown outcome stays for
        # review, even if no turn is visible yet; providers can persist late.
        state, error = "review", "The runtime stopped before the result was recorded. Review the thread; this prompt will not be sent again."
        if row["thread_id"]:
            try:
                history = self.sessions._read_thread(row["thread_id"])
                turn = next((t for t in history.get("turns", []) if t["id"] == row["turn_id"]), None)
                if turn and turn.get("status") in {"completed", "failed", "interrupted"}:
                    state = "completed" if turn["status"] == "completed" else "failed"
                    error = None if state == "completed" else "The scheduled turn failed or was interrupted. Open its thread for details."
            except Exception:
                pass  # Unavailable history is not evidence that sending is safe.
        self._set_state(row["id"], state, error)

    def process_once(self):
        if self.stopped.is_set() or self.sessions.closed:
            return
        now = self.store.clock()
        with self.store._connection() as db:
            db.execute("INSERT OR REPLACE INTO schedule_runtimes VALUES (?, ?)", (self.owner, now))
            abandoned = db.execute("""SELECT s.* FROM scheduled_prompts s LEFT JOIN schedule_runtimes r ON r.id = s.owner
                WHERE s.state IN ('starting', 'running') AND s.owner != ?
                AND (r.heartbeat IS NULL OR r.heartbeat < ?)""", (self.owner, now - RUNTIME_LEASE_SECONDS)).fetchall()
        for row in abandoned:
            if matches_workspace(row["cwd"], self.sessions.cwd):
                self._recover(row)
        with self.store._connection() as db:
            due = db.execute("SELECT * FROM scheduled_prompts WHERE state = 'scheduled' AND run_at <= ? ORDER BY run_at, id", (self.store.clock(),)).fetchall()
        for row in due:
            if not matches_workspace(row["cwd"], self.sessions.cwd):
                continue
            item = self._describe(row)
            if self.stopped.is_set() or self.sessions.closed:
                return
            now = self.store.clock()
            if item["state"] != "scheduled" or item["run_at"] > now:
                continue
            missed = item["run_at"] < self.started_at or now - item["run_at"] > GRACE_SECONDS
            with self.store._connection() as db:
                claimed = db.execute("""UPDATE scheduled_prompts SET state = ?, error = ?, owner = ?,
                    version = version + 1, updated_at = ? WHERE id = ? AND state = 'scheduled' AND version = ?""",
                    ("missed" if missed else "starting", "The scheduled time passed while Ribbon Field was unavailable." if missed else None,
                     self.owner, now, item["id"], item["version"])).rowcount
            if not claimed:
                continue
            self._changed()
            if not missed:
                self._dispatch(item)

    def _dispatch(self, item):
        sending = False
        try:
            if self.stopped.is_set() or self.sessions.closed:
                raise CoordinationError("Ribbon Field stopped before this prompt could start.")
            settings = item["settings"]
            # Recheck the saved path, model and effort at launch, without fallback.
            if str(Path(settings["cwd"]).resolve()) != settings["cwd"]:
                raise CoordinationError("The saved workspace path changed. Review this schedule.")
            result = self.sessions.create(settings)
            thread_id = result["session"]["thread_id"]
            with self.sessions._thread_lock(thread_id):
                # A person may open the newly created thread immediately. Never
                # associate their turn with this schedule or append to it.
                with self.sessions.lock:
                    occupied = thread_id in self.sessions.active or bool(self.sessions._history(thread_id).get("turns"))
                if occupied:
                    raise CoordinationError("The new thread received other input before this prompt could start.")
                with self.store._connection() as db:
                    db.execute("UPDATE scheduled_prompts SET thread_id = ? WHERE id = ?", (thread_id, item["id"]))
                self._changed()
                if self.stopped.is_set() or self.sessions.closed:
                    raise CoordinationError("Ribbon Field stopped before this prompt could start.")
                sending = True
                result = self.sessions.send(thread_id, {"message": item["message"]}, start_only=True)
            # Also supports providers that return before emitting turn/started.
            with self.store._connection() as db:
                db.execute("""UPDATE scheduled_prompts SET state = 'running', turn_id = ?, updated_at = ?,
                    version = version + 1 WHERE id = ? AND state = 'starting'""",
                    (result["turn"]["id"], self.store.clock(), item["id"]))
            self._changed()
        except Exception as exc:
            self._set_state(item["id"], "review" if sending else "failed", str(exc) + (
                " Delivery may have started. Open the thread before retrying; this prompt will not be sent again." if sending else ""))

    def start(self):
        if self.worker is None:
            self.started_at = self.store.clock()
            self.worker = threading.Thread(target=self._run, name="scheduled-prompts", daemon=True)
            self.worker.start()

    def _run(self):
        while not self.stopped.is_set() and not self.sessions.closed:
            try:
                self.process_once()
            except Exception:
                logging.getLogger(__name__).exception("Scheduled prompt worker failed")
            self.stopped.wait(1)

    def close(self):
        self.stopped.set()
        if self.worker is not None:
            self.worker.join(timeout=5)
        with self.store._connection() as db:
            db.execute("DELETE FROM schedule_runtimes WHERE id = ?", (self.owner,))
