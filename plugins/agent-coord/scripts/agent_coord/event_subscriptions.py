"""Durable one-shot turn/job events delivered through the ordinary inbox.

Matching and message receipts are atomic. Provider execution is deliberately left
to the inbox wake transport (including its Stop and uncertain-send protections).
"""
from __future__ import annotations

import json
import logging
import math
import threading
import uuid

from .store import CoordinationError
from .workspaces import matches_workspace

JOB_TERMINAL = {"completed", "failed", "cancelled", "timed_out", "unknown"}


def result_snapshot(db, session_id, turn_id, assignment_message_id):
    """Read the checkpoint frozen at completion and the matching lifecycle report."""
    from .context_lifecycle import snapshot_for_assignment
    row = db.execute("""SELECT c.* FROM turn_notification_context n
        JOIN thread_checkpoints c ON c.id = n.checkpoint_id
        WHERE n.thread_id = ? AND n.turn_key = ? AND c.thread_id = n.thread_id""",
        (session_id, turn_id)).fetchone()
    checkpoint = None if row is None else {key: row[key] for key in (
        "id", "phase", "summary", "next_action", "next_actor", "turn_id", "created_at")}
    lifecycle = snapshot_for_assignment(db, session_id, assignment_message_id, turn_id,
                                       checkpoint_id=checkpoint["id"]) if checkpoint is not None else None
    return {"checkpoint": checkpoint, "context_lifecycle": lifecycle}


class EventSubscriptions:
    def __init__(self, store, cwd=None):
        self.store = store
        self.cwd = cwd
        self.worker = None
        self.stopped = threading.Event()
        self._managed_jobs = None
        with store._connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS event_subscriptions (
                    id TEXT PRIMARY KEY, owner_session_id TEXT NOT NULL REFERENCES sessions(session_id),
                    cwd TEXT NOT NULL, mode TEXT NOT NULL, continuation TEXT NOT NULL,
                    state TEXT NOT NULL, deadline REAL, message_id INTEGER REFERENCES messages(id),
                    request_json TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS event_subscription_items (
                    subscription_id TEXT NOT NULL REFERENCES event_subscriptions(id),
                    id TEXT NOT NULL, kind TEXT NOT NULL, session_id TEXT, turn_id TEXT, job_id TEXT,
                    state TEXT NOT NULL DEFAULT 'pending', outcome_json TEXT,
                    message_id INTEGER REFERENCES messages(id), PRIMARY KEY(subscription_id, id)
                );
                CREATE INDEX IF NOT EXISTS event_subscriptions_pending ON event_subscriptions(state);
            """)
            db.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in db.execute("PRAGMA table_info(event_subscription_items)")}
            if "assignment_message_id" not in columns:
                db.execute("ALTER TABLE event_subscription_items ADD COLUMN assignment_message_id INTEGER REFERENCES messages(id)")

    def create_for_message(self, db, message):
        """Subscribe atomically with an app agent's initial inbox message.

        The first provider turn does not exist yet. Delivery/wake evidence later
        binds this item to that message's turn, never the session's latest turn.
        The caller owns the transaction and has already initialized this schema.
        """
        owner = self._owner(db, message["sender_session_id"])
        identifier = f"app-agent:{message['id']}"
        now = self.store.clock()
        db.execute("""INSERT INTO event_subscriptions
            (id, owner_session_id, cwd, mode, continuation, state, request_json, created_at, updated_at)
            VALUES (?, ?, ?, 'all', ?, 'pending', ?, ?, ?)""",
            (identifier, message["sender_session_id"], owner["cwd"],
             "Read the app agent's result and checkpoint, evaluate any context_lifecycle advice, "
             "and continue the original task. A turn ending does not establish assignment success. "
             "Acknowledge this event after handling it; no reply is required.",
             json.dumps({"assignment_message_id": message["id"]}), now, now))
        db.execute("""INSERT INTO event_subscription_items
            (subscription_id, id, kind, session_id, assignment_message_id)
            VALUES (?, '1', 'message', ?, ?)""", (identifier, message["recipient_session_id"], message["id"]))
        return identifier

    def _jobs(self):
        if self._managed_jobs is None:
            from .jobs import ManagedJobs
            self._managed_jobs = ManagedJobs(self.store)
        return self._managed_jobs

    @staticmethod
    def _text(value, label):
        if not isinstance(value, str) or not value.strip() or len(value) > 200 or "\0" in value:
            raise CoordinationError(f"{label} must be nonempty text of at most 200 characters.")
        return value

    def _owner(self, db, session_id):
        row = db.execute("SELECT cwd FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        if row is None or not matches_workspace(row["cwd"], self.cwd):
            raise CoordinationError("Subscription owner not found in this workspace.")
        return row

    def _get(self, db, session_id, subscription_id):
        self._owner(db, session_id)
        row = db.execute("SELECT * FROM event_subscriptions WHERE id = ? AND owner_session_id = ?",
                         (subscription_id, session_id)).fetchone()
        if row is None or not matches_workspace(row["cwd"], self.cwd):
            raise CoordinationError("Event subscription not found.")
        return row

    @staticmethod
    def _describe(db, row):
        result = dict(row)
        result.pop("request_json")
        result["subscription_id"] = result["id"]
        result["items"] = []
        for item in db.execute("SELECT * FROM event_subscription_items WHERE subscription_id = ? ORDER BY id", (row["id"],)):
            target = dict(item)
            outcome = target.pop("outcome_json")
            target["outcome"] = json.loads(outcome) if outcome else None
            result["items"].append(target)
        result["message_ids"] = sorted({item["message_id"] for item in result["items"] if item["message_id"] is not None}
                                       | ({row["message_id"]} if row["message_id"] is not None else set()))
        return result

    def create(self, session_id, *, sessions=None, turns=None, jobs=None, mode="all",
               continuation="", timeout=None, subscription_id=None):
        self._text(session_id, "Owner session")
        if not isinstance(mode, str) or mode not in {"each", "any", "all"}:
            raise CoordinationError("Event mode must be each, any, or all.")
        if not isinstance(continuation, str) or len(continuation) > 16000:
            raise CoordinationError("Continuation must be text of at most 16000 characters.")
        if timeout is not None:
            try:
                valid_timeout = type(timeout) in (float, int) and math.isfinite(timeout) and timeout > 0
            except OverflowError:
                valid_timeout = False
            if not valid_timeout:
                raise CoordinationError("Timeout must be a finite positive number of seconds.")
        targets = [([] if value is None else value) for value in (sessions, turns, jobs)]
        if any(not isinstance(value, list) for value in targets) or not 0 < sum(map(len, targets)) <= 100:
            raise CoordinationError("Choose between 1 and 100 explicit event targets in lists.")
        sessions, turns, jobs = targets
        for target in sessions:
            self._text(target, "Watched session")
            if target == session_id:
                raise CoordinationError("Cannot watch your own session or turn.")
        for target in turns:
            if not isinstance(target, dict) or set(target) != {"session_id", "turn_id"}:
                raise CoordinationError("Each turn requires session_id and turn_id.")
            self._text(target["session_id"], "Watched session")
            self._text(target["turn_id"], "Watched turn")
            if target["session_id"] == session_id:
                raise CoordinationError("Cannot watch your own session or turn.")
        for job in jobs:
            self._text(job, "Job ID")
        subscription_id = self._text(str(uuid.uuid4()) if subscription_id is None else subscription_id, "Subscription ID")
        request = json.dumps(dict(sessions=sessions, turns=turns, jobs=jobs, mode=mode,
                                  continuation=continuation, timeout=timeout), sort_keys=True)
        if jobs:
            self._jobs()  # Initialize schema before taking the subscription write lock.
            for job in jobs:
                self._jobs().get(session_id, job)  # May recover stale jobs; never nest its writer.
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            owner = self._owner(db, session_id)
            existing = db.execute("SELECT * FROM event_subscriptions WHERE id = ?", (subscription_id,)).fetchone()
            if existing:
                existing = self._get(db, session_id, subscription_id)
                if existing["request_json"] != request:
                    raise CoordinationError("Subscription ID already has a different request.")
                return self._describe(db, existing)
            items = set()
            for watched in sessions:
                row = db.execute("SELECT turn_key FROM work_threads WHERE thread_id = ?", (watched,)).fetchone()
                if row is None or not row["turn_key"]:
                    raise CoordinationError("Watched session has no retained turn.")
                items.add(("turn", watched, row["turn_key"], None))
            for target in turns:
                if not db.execute("SELECT 1 FROM sessions WHERE session_id = ?", (target["session_id"],)).fetchone():
                    raise CoordinationError("Watched session not found.")
                items.add(("turn", target["session_id"], target["turn_id"], None))
            for job in jobs:
                if not db.execute("SELECT 1 FROM jobs WHERE id = ? AND owner_session_id = ?", (job, session_id)).fetchone():
                    raise CoordinationError("Command job not found for this owner.")
                items.add(("job", None, None, job))
            now = self.store.clock()
            deadline = now + timeout if timeout is not None else None
            if deadline is not None and not math.isfinite(deadline):
                raise CoordinationError("Timeout deadline is out of range.")
            db.execute("""INSERT INTO event_subscriptions
                (id, owner_session_id, cwd, mode, continuation, state, deadline, request_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)""",
                       (subscription_id, session_id, owner["cwd"], mode, continuation, deadline, request, now, now))
            for index, (kind, watched, turn, job) in enumerate(sorted(items, key=repr)):
                db.execute("""INSERT INTO event_subscription_items
                    (subscription_id, id, kind, session_id, turn_id, job_id) VALUES (?, ?, ?, ?, ?, ?)""",
                           (subscription_id, str(index + 1), kind, watched, turn, job))
            return self._describe(db, self._get(db, session_id, subscription_id))

    def list(self, session_id):
        with self.store._connection() as db:
            self._owner(db, session_id)
            return [self._describe(db, row) for row in db.execute(
                "SELECT * FROM event_subscriptions WHERE owner_session_id = ? ORDER BY created_at, id", (session_id,))
                    if matches_workspace(row["cwd"], self.cwd)]

    def get(self, session_id, subscription_id):
        with self.store._connection() as db:
            return self._describe(db, self._get(db, session_id, subscription_id))

    def cancel(self, session_id, subscription_id):
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._get(db, session_id, subscription_id)
            now = self.store.clock()
            # Pending inbox delivery can still be cancelled after matching. An
            # already delivered/provider-accepted event cannot be recalled.
            record = self._describe(db, row)
            for message_id in record["message_ids"]:
                db.execute("""UPDATE messages SET delivered_at = ?, suppressed_at = ?
                              WHERE id = ? AND delivered_at IS NULL AND thread_id LIKE 'event:%'""", (now, now, message_id))
            db.execute("UPDATE event_subscriptions SET state = 'cancelled', updated_at = ? WHERE id = ?", (now, row["id"]))
            db.execute("UPDATE event_subscription_items SET state = 'cancelled' WHERE subscription_id = ? AND message_id IS NULL", (row["id"],))
            return self._describe(db, self._get(db, session_id, subscription_id))

    def _outcome(self, db, owner, item, deadline):
        if item["kind"] in {"turn", "message"}:
            turn_id = item["turn_id"]
            if item["kind"] == "message" and not turn_id:
                message = db.execute("""SELECT COALESCE(recipient_turn_id, wake_turn_id) AS turn_id
                    FROM messages WHERE id = ? AND recipient_session_id = ?""",
                    (item["assignment_message_id"], item["session_id"])).fetchone()
                if not message or not message["turn_id"]:
                    return None
                turn_id = message["turn_id"]
                db.execute("UPDATE event_subscription_items SET turn_id = ? WHERE subscription_id = ? AND id = ?",
                           (turn_id, item["subscription_id"], item["id"]))
            row = db.execute("""SELECT id, status, completed_at FROM turn_completions
                              WHERE thread_id = ? AND turn_key = ?""", (item["session_id"], turn_id)).fetchone()
            if row is None or (deadline is not None and row["completed_at"] > deadline):
                return None
            result = {"kind": "turn", "session_id": item["session_id"], "turn_id": turn_id,
                    "state": row["status"], "completion_id": row["id"], "completed_at": row["completed_at"],
                    "result_ref": {"session_id": item["session_id"], "turn_id": turn_id}}
            if item["kind"] == "message":
                result["assignment_message_id"] = item["assignment_message_id"]
                result.update(result_snapshot(db, item["session_id"], turn_id, item["assignment_message_id"]))
            return result
        row = db.execute("SELECT * FROM jobs WHERE id = ? AND owner_session_id = ?", (item["job_id"], owner)).fetchone()
        if row is None or row["state"] not in JOB_TERMINAL:
            return None
        row = dict(row)
        # Job records may expose a completion timestamp; use it when available.
        finished = row.get("finished_at")
        if isinstance(finished, (int, float)) and deadline is not None and finished > deadline:
            return None
        return {"kind": "job", "job_id": item["job_id"], "state": row["state"],
                "finished_at": finished,
                "completed_at": finished if isinstance(finished, (float, int)) else self.store.clock(),
                "exit_code": row.get("exit_code"), "output_path": str(row.get("output_path") or "")[:4096],
                "error": str(row.get("error") or "")[:2000],
                "result_ref": {"job_id": item["job_id"]}}

    def _deliver(self, db, subscription, target, outcomes, now, *, timeout=False):
        if not timeout and len(outcomes) == 1 and outcomes[0].get("assignment_message_id") and outcomes[0]["state"] in {"failed", "interrupted"}:
            from .app_agent_failures import turn_failure_message
            outcome = outcomes[0]
            existing = turn_failure_message(db, subscription["owner_session_id"], outcome["session_id"], outcome["turn_id"])
            if existing is not None:
                return existing  # The automatic failure report already wakes the creator.
        body = json.dumps({"event": "subscription_timeout" if timeout else "subscription_matched",
                           "subscription_id": subscription["id"], "mode": subscription["mode"],
                           "target": target, "outcomes": outcomes, "continuation": subscription["continuation"],
                           "meaning": "Turn completion is not assignment completion."}, ensure_ascii=True)
        message, _ = self.store._insert_message(
            db, sender_session_id=subscription["owner_session_id"], recipient_session_id=subscription["owner_session_id"],
            body=body, classification="action_required", thread_id=f"event:{subscription['id']}:{target}",
            reply_required=False, now=now)
        return message["id"]

    def process_once(self):
        # Ensure job schema outside the write transaction; a job worker owns the
        # job lifecycle. Read jobs on the same connection while matching to avoid
        # nested write transactions in the public API's stale-job recovery.
        with self.store._connection() as db:
            jobs = db.execute("""SELECT DISTINCT s.cwd, s.owner_session_id, i.job_id
                FROM event_subscription_items i JOIN event_subscriptions s ON s.id = i.subscription_id
                WHERE s.state = 'pending' AND i.state = 'pending' AND i.kind = 'job'""").fetchall()
        for job in jobs:
            if matches_workspace(job["cwd"], self.cwd):
                try:
                    self._jobs().get(job["owner_session_id"], job["job_id"])
                except CoordinationError:
                    # A removed/inaccessible job must not prevent other events
                    # or subscription timeouts from progressing.
                    logging.getLogger(__name__).warning("Watched command job is unavailable: %s", job["job_id"])
        emitted = []
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            now = self.store.clock()
            subscriptions = db.execute("""SELECT s.*, t.attention FROM event_subscriptions s
                JOIN work_threads t ON t.thread_id = s.owner_session_id WHERE s.state = 'pending'
                ORDER BY s.created_at, s.id""").fetchall()
            for sub in subscriptions:
                if not matches_workspace(sub["cwd"], self.cwd):
                    continue
                items = db.execute("SELECT * FROM event_subscription_items WHERE subscription_id = ? ORDER BY id", (sub["id"],)).fetchall()
                for item in items:
                    if item["state"] != "pending":
                        continue
                    outcome = self._outcome(db, sub["owner_session_id"], item, sub["deadline"])
                    if outcome is not None:
                        db.execute("""UPDATE event_subscription_items SET state = 'matched', outcome_json = ?
                            WHERE subscription_id = ? AND id = ?""", (json.dumps(outcome), sub["id"], item["id"]))
                record = self._describe(db, sub)
                matched = [item for item in record["items"] if item["outcome"] is not None]
                expired = sub["deadline"] is not None and now >= sub["deadline"]
                ready = bool(matched) if sub["mode"] in {"any", "each"} else len(matched) == len(items)
                if sub["attention"] == "archived":
                    continue  # Persist matches but never reopen a closed owner.
                if sub["mode"] == "each":
                    for item in matched:
                        if item["message_id"] is None:
                            receipt = self._deliver(db, sub, item["id"], [item["outcome"]], now)
                            emitted.append(receipt)
                            db.execute("""UPDATE event_subscription_items SET state = 'fired', message_id = ?
                                WHERE subscription_id = ? AND id = ?""", (receipt, sub["id"], item["id"]))
                    ready = len(matched) == len(items)
                elif ready:
                    # any reports the first durable match, all reports every terminal outcome.
                    outcomes = [min(matched, key=lambda item: (item["outcome"]["completed_at"], item["id"]))] if sub["mode"] == "any" else matched
                    receipt = self._deliver(db, sub, "group", [item["outcome"] for item in outcomes], now)
                    emitted.append(receipt)
                    db.execute("UPDATE event_subscriptions SET message_id = ? WHERE id = ?", (receipt, sub["id"]))
                if ready:
                    db.execute("UPDATE event_subscriptions SET state = 'fired', updated_at = ? WHERE id = ?", (now, sub["id"]))
                elif expired:
                    receipt = self._deliver(db, sub, "timeout", [item["outcome"] for item in matched], now, timeout=True)
                    emitted.append(receipt)
                    db.execute("UPDATE event_subscriptions SET state = 'timed_out', message_id = ?, updated_at = ? WHERE id = ?", (receipt, now, sub["id"]))
                    db.execute("UPDATE event_subscription_items SET state = 'timed_out' WHERE subscription_id = ? AND state = 'pending'", (sub["id"],))
        return emitted

    def start(self):
        if self.worker is None or not self.worker.is_alive():
            self.stopped.clear()
            self.worker = threading.Thread(target=self._run, name="event-subscriptions", daemon=True)
            self.worker.start()

    def _run(self):
        while not self.stopped.is_set():
            try:
                self.process_once()
            except Exception:
                logging.getLogger(__name__).exception("Event subscription worker failed")
            self.stopped.wait(1)

    def close(self):
        self.stopped.set()
        if self.worker is not None:
            self.worker.join(timeout=5)
