"""One-time and recurring prompts; never retry an uncertain delivery."""
from __future__ import annotations

from datetime import datetime
import json
import logging
import math
from pathlib import Path
import threading
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .store import CoordinationError
from .schedule_recurrence import next_occurrence, validate_recurrence
from .thread_mentions import thread_mentions
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
                CREATE TABLE IF NOT EXISTS scheduled_prompt_runs (
                    id INTEGER PRIMARY KEY, schedule_id TEXT NOT NULL, run_at REAL NOT NULL,
                    state TEXT NOT NULL, error TEXT, thread_id TEXT, turn_id TEXT, finished_at REAL NOT NULL,
                    UNIQUE(schedule_id, run_at)
                );
            """)

            columns = {row[1] for row in db.execute("PRAGMA table_info(scheduled_prompts)")}
            if "mentions_json" not in columns:
                db.execute("ALTER TABLE scheduled_prompts ADD COLUMN mentions_json TEXT NOT NULL DEFAULT '[]'")
            if "target_thread_id" not in columns:
                db.execute("ALTER TABLE scheduled_prompts ADD COLUMN target_thread_id TEXT")
            for name, definition in {
                "recurrence_json": "TEXT", "recurrence_status": "TEXT", "anchor_at": "REAL",
            }.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE scheduled_prompts ADD COLUMN {name} {definition}")

    @staticmethod
    def _describe(row):
        item = dict(row)
        item["settings"] = json.loads(item.pop("settings_json"))
        item["mentions"] = json.loads(item.pop("mentions_json"))
        item["recurrence"] = json.loads(item.pop("recurrence_json") or "null")
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
            latest = {row["schedule_id"]: dict(row) for row in db.execute("""SELECT r.* FROM scheduled_prompt_runs r
                WHERE r.id = (SELECT MAX(p.id) FROM scheduled_prompt_runs p WHERE p.schedule_id = r.schedule_id)""")}
        return [{**self._describe(row), "last_run": latest.get(row["id"])}
                for row in rows if matches_workspace(row["cwd"], self.sessions.cwd)]

    def _validate(self, body):
        if set(body) - {"id", "version", "message", "run_at", "timezone", "settings", "mentions", "target_thread_id", "recurrence"}:
            raise CoordinationError("Unknown scheduled prompt setting.")
        message = body.get("message")
        self.sessions._text(message, "Prompt", 100000)
        mentions = thread_mentions(message, body, self.sessions)
        if message.lstrip().startswith("/"):
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
        recurrence = validate_recurrence(body.get("recurrence"))
        if recurrence:
            try:
                if recurrence["frequency"] == "weekdays" and datetime.fromtimestamp(run_at, ZoneInfo(timezone)).weekday() >= 5:
                    raise CoordinationError("Choose a weekday for the first run.")
                next_occurrence(run_at, run_at, timezone, recurrence)
            except (ValueError, OverflowError) as exc:
                raise CoordinationError("Choose a date with room for the next recurring run.") from exc
        target = body.get("target_thread_id")
        if target is not None:
            target = self.sessions._text(target, "Destination thread", 200)
            record = self.sessions._record(target)
            if self.store.threads.get(target)["attention"] == "archived":
                raise CoordinationError("Reopen this thread before scheduling a message.")
            if "settings" in body:
                raise CoordinationError("Existing-thread schedules use the thread's current settings.")
            settings = {key: record[key] for key in ("cwd", "client", "model", "effort", "yolo")}
            return message, run_at, timezone, settings, mentions, target, recurrence
        settings = body.get("settings")
        if not isinstance(settings, dict) or set(settings) - {"cwd", "client", "model", "effort", "yolo", "repository_id", "project_id"}:
            raise CoordinationError("Choose workspace, model, reasoning, and permissions.")
        self.sessions._text(settings.get("cwd"), "Workspace", 4096)
        self.sessions._text(settings.get("model"), "Model", 200)
        record, associations = self.sessions._creation_settings({**settings, "name": message[:80]})
        selected = self.sessions._validate_settings(record["model"], record["effort"], record["client"])
        if record["effort"] is None:
            record["effort"] = selected.get("defaultReasoningEffort")
        return message, run_at, timezone, {**record, **associations}, mentions, None, recurrence

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
        message, run_at, timezone, settings, mentions, target, recurrence = self._validate(body)
        now = self.store.clock()
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""INSERT OR IGNORE INTO scheduled_prompts
                (id, cwd, settings_json, message, run_at, timezone, state, request_json, created_at, updated_at, mentions_json,
                 target_thread_id, thread_id, recurrence_json, recurrence_status, anchor_at)
                VALUES (?, ?, ?, ?, ?, ?, 'scheduled', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (schedule_id, settings["cwd"], json.dumps(settings), message, run_at, timezone, request, now, now, json.dumps(mentions),
                 target, target, json.dumps(recurrence) if recurrence else None, "active" if recurrence else None, run_at))
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
        elif action not in {"cancel", "run_now", "pause", "resume"} or set(body) - {"action", "version"}:
            raise CoordinationError("Choose edit, cancel, run now, pause, or resume.")
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._get(db, schedule_id)
            if type(body.get("version")) is not int or row["version"] != body["version"]:
                raise CoordinationError("This schedule changed. Reopen Scheduled prompts and try again.")
            recurring = bool(row["recurrence_json"])
            active = row["state"] in {"starting", "running"}
            if action in {"pause", "resume"} or (action == "cancel" and recurring):
                if not recurring or row["recurrence_status"] == "cancelled":
                    raise CoordinationError("This recurring schedule is no longer active.")
                if action == "resume" and (row["recurrence_status"] != "paused" or row["state"] == "review"):
                    raise CoordinationError("Only a paused schedule with a known delivery outcome can resume.")
                if action == "pause" and row["recurrence_status"] != "active":
                    raise CoordinationError("This schedule is already paused.")
                status = {"pause": "paused", "resume": "active", "cancel": "cancelled"}[action]
                state, run_at = row["state"], row["run_at"]
                if not active:
                    if action == "resume":
                        run_at = next_occurrence(row["anchor_at"], self.store.clock(), row["timezone"], json.loads(row["recurrence_json"]))
                        state = "scheduled"
                    elif state != "review" or action == "cancel":
                        state = status
                db.execute("""UPDATE scheduled_prompts SET recurrence_status = ?, state = ?, run_at = ?,
                    version = version + 1, updated_at = ? WHERE id = ?""", (status, state, run_at, self.store.clock(), schedule_id))
                if action == "resume" and not active:
                    db.execute("UPDATE scheduled_prompts SET turn_id = NULL, owner = NULL, thread_id = target_thread_id, error = NULL WHERE id = ?", (schedule_id,))
                result = self._describe(self._get(db, schedule_id))
            elif row["state"] not in {"scheduled", "missed", "waiting", "paused"}:
                raise CoordinationError("This prompt has already started or was cancelled. Open its thread to review it.")
            elif action == "edit":
                message, run_at, timezone, settings, mentions, target, recurrence = values
                if target != row["target_thread_id"]:
                    raise CoordinationError("A schedule's destination cannot be changed. Create a new schedule instead.")
                db.execute("""UPDATE scheduled_prompts SET cwd = ?, settings_json = ?, message = ?, run_at = ?,
                    timezone = ?, mentions_json = ?, state = ?, error = NULL, recurrence_json = ?, recurrence_status = ?,
                    anchor_at = ?, owner = NULL, thread_id = ?, turn_id = NULL,
                    version = version + 1, updated_at = ? WHERE id = ?""",
                    (settings["cwd"], json.dumps(settings), message, run_at, timezone, json.dumps(mentions),
                     "paused" if recurrence and row["recurrence_status"] == "paused" else "scheduled",
                     json.dumps(recurrence) if recurrence else None,
                     ("paused" if row["recurrence_status"] == "paused" else "active") if recurrence else None,
                     run_at, target, self.store.clock(), schedule_id))
            else:
                if action == "run_now" and row["state"] != "missed":
                    raise CoordinationError("Run now is available for missed prompts.")
                db.execute("""UPDATE scheduled_prompts SET state = ?, run_at = ?, error = NULL,
                    version = version + 1, updated_at = ? WHERE id = ?""",
                    ("scheduled" if action == "run_now" else "cancelled", self.store.clock() if action == "run_now" else row["run_at"], self.store.clock(), schedule_id))
            result = self._describe(self._get(db, schedule_id))
        self._changed()
        return result

    def _changed(self):
        self.sessions._publish("browser/schedules", {})

    def _set_state(self, schedule_id, state, error=None, *, run_at, version=None):
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute("""UPDATE scheduled_prompts SET state = ?, error = ?, version = version + 1,
                updated_at = ? WHERE id = ? AND run_at = ? AND (? IS NULL OR version = ?)
                AND state IN ('waiting', 'starting', 'running', 'review')""",
                (state, error, self.store.clock(), schedule_id, run_at, version, version)).rowcount
            if changed:
                self._record_run(db, schedule_id)
        self._changed()

    def _record_run(self, db, schedule_id):
        db.execute("""INSERT INTO scheduled_prompt_runs (schedule_id, run_at, state, error, thread_id, turn_id, finished_at)
            SELECT id, run_at, state, error, thread_id, turn_id, updated_at FROM scheduled_prompts
            WHERE id = ? AND recurrence_json IS NOT NULL AND state IN ('completed', 'failed', 'missed', 'review')
            ON CONFLICT(schedule_id, run_at) DO UPDATE SET state = excluded.state, error = excluded.error,
                turn_id = excluded.turn_id, finished_at = excluded.finished_at""", (schedule_id,))

    def _advance_recurring(self):
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("""SELECT * FROM scheduled_prompts WHERE recurrence_json IS NOT NULL
                AND state IN ('completed', 'failed', 'missed')""").fetchall()
            for row in rows:
                if not matches_workspace(row["cwd"], self.sessions.cwd):
                    continue
                self._record_run(db, row["id"])
                state = row["recurrence_status"]
                run_at = row["run_at"]
                if state == "active":
                    try:
                        run_at = next_occurrence(row["anchor_at"], max(self.store.clock(), row["run_at"]),
                                                 row["timezone"], json.loads(row["recurrence_json"]))
                        state = "scheduled"
                    except (ValueError, OverflowError, CoordinationError):
                        db.execute("""UPDATE scheduled_prompts SET recurrence_status = 'paused', state = 'paused',
                            error = ?, version = version + 1, updated_at = ? WHERE id = ?""",
                            ("No upcoming occurrence could be calculated. Edit this schedule.", self.store.clock(), row["id"]))
                        continue
                db.execute("""UPDATE scheduled_prompts SET state = ?, run_at = ?, owner = NULL,
                    thread_id = target_thread_id, turn_id = NULL, error = NULL, version = version + 1,
                    updated_at = ? WHERE id = ?""", (state, run_at, self.store.clock(), row["id"]))
        if rows:
            self._changed()

    def event(self, method, params):
        if method not in {"turn/started", "turn/completed"}:
            return
        turn, thread_id = params.get("turn", {}), params.get("threadId")
        if not thread_id or not turn.get("id"):
            return
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT * FROM scheduled_prompts WHERE thread_id = ?
                AND state IN ('starting', 'running', 'review')
                AND (turn_id = ? OR (state = 'starting' AND turn_id IS NULL AND ?))""",
                (thread_id, turn["id"], method == "turn/started")).fetchone()
            if not row or (row["turn_id"] is not None and row["turn_id"] != turn["id"]):
                return
            state = "running" if method == "turn/started" else "completed" if turn.get("status") == "completed" else "failed"
            error = None if state != "failed" else "The scheduled turn failed or was interrupted. Open its thread for details."
            if row["state"] == "review" and method == "turn/started":
                state, error = "review", row["error"]
            db.execute("""UPDATE scheduled_prompts SET state = ?, error = ?, turn_id = ?,
                version = version + 1, updated_at = ? WHERE id = ? AND version = ?""",
                (state, error, turn["id"], self.store.clock(), row["id"], row["version"]))
            self._record_run(db, row["id"])
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
        self._set_state(row["id"], state, error, run_at=row["run_at"], version=row["version"])

    def process_once(self):
        if self.stopped.is_set() or self.sessions.closed:
            return
        now = self.store.clock()
        with self.store._connection() as db:
            db.execute("INSERT OR REPLACE INTO schedule_runtimes VALUES (?, ?)", (self.owner, now))
            abandoned = db.execute("""SELECT s.* FROM scheduled_prompts s LEFT JOIN schedule_runtimes r ON r.id = s.owner
                WHERE s.state IN ('waiting', 'starting', 'running') AND s.owner != ?
                AND (r.heartbeat IS NULL OR r.heartbeat < ?)""", (self.owner, now - RUNTIME_LEASE_SECONDS)).fetchall()
        for row in abandoned:
            if matches_workspace(row["cwd"], self.sessions.cwd):
                if row["state"] == "waiting":
                    # No send was attempted, so another runtime can safely
                    # reclaim this job after its previous owner's lease ends.
                    with self.store._connection() as db:
                        db.execute("""UPDATE scheduled_prompts SET owner = ? WHERE id = ?
                            AND state = 'waiting' AND owner = ?""", (self.owner, row["id"], row["owner"]))
                else:
                    self._recover(row)
        self._advance_recurring()
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
                db.execute("BEGIN IMMEDIATE")
                claimed = db.execute("""UPDATE scheduled_prompts SET state = ?, error = ?, owner = ?,
                    version = version + 1, updated_at = ? WHERE id = ? AND state = 'scheduled' AND version = ?""",
                    ("missed" if missed else "waiting" if item["target_thread_id"] else "starting", "The scheduled time passed while Ribbon Field was unavailable." if missed else None,
                     self.owner, now, item["id"], item["version"])).rowcount
                if claimed and missed:
                    self._record_run(db, item["id"])
            if not claimed:
                continue
            self._changed()
            if not missed and not item["target_thread_id"]:
                self._dispatch(item)
        with self.store._connection() as db:
            waiting = db.execute("SELECT * FROM scheduled_prompts WHERE state = 'waiting' AND owner = ? ORDER BY run_at, id", (self.owner,)).fetchall()
        for row in waiting:
            if matches_workspace(row["cwd"], self.sessions.cwd):
                self._dispatch(self._describe(row))
        self._advance_recurring()

    def _dispatch(self, item):
        sending = False
        try:
            if self.stopped.is_set() or self.sessions.closed:
                raise CoordinationError("Ribbon Field stopped before this prompt could start.")
            target = item["target_thread_id"]
            if target:
                thread_id = target
            else:
                settings = item["settings"]
                # Recheck the saved path, model and effort at launch, without fallback.
                if str(Path(settings["cwd"]).resolve()) != settings["cwd"]:
                    raise CoordinationError("The saved workspace path changed. Review this schedule.")
                result = self.sessions.create(settings)
                thread_id = result["session"]["thread_id"]
            with self.sessions._thread_lock(thread_id):
                if target:
                    self.sessions._record(thread_id)
                    if self.store.threads.get(thread_id)["attention"] == "archived":
                        raise CoordinationError("The destination thread is closed. Reopen it before sending the message.")
                    detail = self.sessions.read(thread_id)
                    if detail["running"] or detail["queuedMessages"]:
                        return
                    # Prior turn events must finish before this schedule can
                    # claim an unbound starting turn. Otherwise an older start
                    # notification could attach itself to the new schedule.
                    self.sessions._await_persistence(thread_id)
                    with self.store._connection() as db:
                        claimed = db.execute("""UPDATE scheduled_prompts SET state = 'starting', version = version + 1,
                            updated_at = ? WHERE id = ? AND state = 'waiting' AND owner = ? AND version = ?
                            AND NOT EXISTS (SELECT 1 FROM scheduled_prompts WHERE thread_id = ?
                                AND state IN ('starting', 'running'))""",
                            (self.store.clock(), item["id"], self.owner, item["version"], thread_id)).rowcount
                    if not claimed:
                        return
                else:
                    # A person may open the newly created thread immediately.
                    with self.sessions.lock:
                        occupied = thread_id in self.sessions.active or bool(self.sessions._history(thread_id).get("turns"))
                    if occupied:
                        raise CoordinationError("The new thread received other input before this prompt could start.")
                with self.store._connection() as db:
                    db.execute("UPDATE scheduled_prompts SET thread_id = ? WHERE id = ? AND run_at = ?", (thread_id, item["id"], item["run_at"]))
                self._changed()
                if self.stopped.is_set() or self.sessions.closed:
                    raise CoordinationError("Ribbon Field stopped before this prompt could start.")
                sending = True
                result = self.sessions.send(thread_id, {"message": item["message"], "mentions": item["mentions"]}, start_only=True, preserve_text=True)
            # Also supports providers that return before emitting turn/started.
            with self.store._connection() as db:
                db.execute("""UPDATE scheduled_prompts SET state = 'running', turn_id = ?, updated_at = ?,
                    version = version + 1 WHERE id = ? AND run_at = ? AND owner = ? AND state = 'starting'""",
                    (result["turn"]["id"], self.store.clock(), item["id"], item["run_at"], self.owner))
            self._changed()
        except Exception as exc:
            self._set_state(item["id"], "review" if sending else "failed", str(exc) + (
                " Delivery may have started. Open the thread before retrying; this prompt will not be sent again." if sending else ""), run_at=item["run_at"])

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
