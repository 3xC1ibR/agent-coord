"""Bounded same-model retries of Codex capacity rejections with zero model effects.

Only newly captured requests qualify. A retry removes the exact rejected empty
turn before submitting its original input; it never replays a tool or silently
changes models. A crash across either provider boundary is uncertain, not a
reason to send again.
"""
from __future__ import annotations

import copy
import json
import logging
import math
import random
import threading
import uuid

from .app_agent_failures import notify_agent_failure
from .workspaces import matches_workspace

MAX_RETRIES = 4
BASE_DELAY = 5.0
MAX_ELAPSED = 300.0
ACTIVE = {"waiting", "reverting", "submitting", "running"}


def safe_capacity_failure(turn):
    error = turn.get("error") or {}
    termination = turn.get("termination") or {}
    items = turn.get("items") or []
    return (turn.get("status") == "failed" and isinstance(error, dict)
            and error.get("codexErrorInfo") == "serverOverloaded"
            and termination.get("origin") != "user" and termination.get("execution") != "unknown"
            and turn.get("itemsView", "full") == "full"
            and len(items) == 1 and items[0].get("type") == "userMessage")


def retry_delay(error, attempt, jitter):
    delay = BASE_DELAY * 2 ** (attempt - 1) * (1 + .25 * jitter())
    # Codex currently exposes no Retry-After header in TurnError. Honor typed
    # delay fields when provided by a compatible transport, never parse prose.
    for value in (error, error.get("additionalDetails")):
        if not isinstance(value, dict):
            continue
        for key, scale in (("retryAfterMs", .001), ("retryAfter", 1), ("retry_after", 1)):
            hint = value.get(key)
            if type(hint) in {int, float} and math.isfinite(hint) and hint >= 0:
                delay = max(delay, hint * scale)
    return delay


def retained_context(store, thread_id):
    """Rehydrate the same logical inbox context without changing delivery receipts."""
    with store._connection() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='capacity_retries'").fetchone():
            return []
        row = db.execute("""SELECT context_ids_json FROM capacity_retries WHERE thread_id=?
            AND state IN ('submitting','running') ORDER BY created_at DESC LIMIT 1""", (thread_id,)).fetchone()
        ids = json.loads(row[0]) if row else []
    if not ids:
        return []
    return [m for m in store.inbox(thread_id, include_delivered=True, mark_delivered=False,
                                  classifications={"action_required"}) if m["id"] in ids]


class CapacityRetries:
    def __init__(self, sessions, *, jitter=random.random):
        self.sessions, self.store, self.jitter = sessions, sessions.store, jitter
        self.preparing = {}
        self.owner = uuid.uuid4().hex
        self.stopped = threading.Event()
        self.ready = threading.Event()
        self.worker = None
        with self.store._connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS capacity_turn_requests (
                    turn_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, root_turn_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS capacity_retries (
                    root_turn_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, current_turn_id TEXT NOT NULL,
                    state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, due_at REAL,
                    deadline REAL NOT NULL, created_at REAL NOT NULL, error TEXT NOT NULL,
                    payload_json TEXT NOT NULL, context_ids_json TEXT NOT NULL DEFAULT '[]',
                    owner TEXT, detail TEXT
                );
                CREATE INDEX IF NOT EXISTS capacity_retries_due ON capacity_retries(state,due_at);
            """)
            # Waiting is replay-safe after restart; an interrupted RPC claim is not.
            recovered = [dict(row) for row in db.execute("SELECT * FROM capacity_retries WHERE state IN ('reverting','submitting')")]
            db.execute("""UPDATE capacity_retries SET state='uncertain',due_at=NULL,
                detail='The runtime stopped during capacity retry delivery. Review the conversation before continuing.'
                WHERE state IN ('reverting','submitting')""")
        for row in recovered:
            self.sessions.inbox_wake.pause(row["thread_id"],
                "The runtime stopped during capacity retry delivery. Review the conversation before continuing.", kind="unknown")
            notify_agent_failure(self.store, row["thread_id"], event_key=["turn", row["current_turn_id"]],
                                 stage="capacity_retry", error="Capacity retry delivery was interrupted; execution needs review.",
                                 turn_id=row["current_turn_id"])

    def prepare(self, thread_id, params, context, *, root=None, settings=None, replace=None):
        self.preparing[thread_id] = {"params": copy.deepcopy(params), "context": copy.deepcopy(context),
                                    "root": root, "replace": replace, "settings": settings or self.settings(thread_id)}

    def settings(self, thread_id):
        record = self.sessions._record(thread_id)
        return {k: record.get(k) for k in ("client", "cwd", "model", "effort", "service_tier", "yolo")}

    def starting(self, thread_id):
        return copy.deepcopy(self.preparing.get(thread_id))

    def started(self, thread_id, turn_id, payload):
        if not payload:
            # The response may have captured this before a delayed notification.
            return
        root = payload.get("root") or turn_id
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO capacity_turn_requests VALUES (?,?,?,?)",
                       (turn_id, thread_id, root, json.dumps(payload)))
            row = db.execute("SELECT * FROM capacity_retries WHERE root_turn_id=?", (root,)).fetchone()
            if row and row["state"] in {"submitting", "reverting"}:
                old = row["current_turn_id"]
                db.execute("UPDATE capacity_retries SET state='running',current_turn_id=? WHERE root_turn_id=?",
                           (turn_id, root))
                # Existing receipts and subscriptions follow this logical request;
                # no new inbox message, wake attempt or subscription is created.
                db.execute("UPDATE messages SET wake_turn_id=? WHERE recipient_session_id=? AND wake_turn_id=?", (turn_id, thread_id, old))
                db.execute("UPDATE messages SET recipient_turn_id=? WHERE recipient_session_id=? AND recipient_turn_id=?", (turn_id, thread_id, old))
                db.execute("""UPDATE event_subscription_items SET turn_id=? WHERE session_id=?
                    AND turn_id=? AND state='pending'""", (turn_id, thread_id, old))

    def _turn(self, thread_id, incoming):
        with self.sessions.lock:
            saved = next((t for t in self.sessions._history(thread_id).get("turns", [])
                          if t.get("id") == incoming.get("id")), {})
            turn = {**copy.deepcopy(saved), **copy.deepcopy(incoming)}
            # A thin completion event cannot hide streamed model/tool effects.
            items = {i.get("id"): i for i in saved.get("items", [])}
            items.update({i.get("id"): i for i in incoming.get("items", [])})
            turn["items"] = list(items.values())
            return turn

    def completed(self, thread_id, incoming, *, deliberate=False):
        """True means pending retry replaces terminal-failure bookkeeping."""
        turn = self._turn(thread_id, incoming)
        with self.store._connection() as db:
            request = db.execute("SELECT * FROM capacity_turn_requests WHERE turn_id=? AND thread_id=?",
                                 (turn.get("id"), thread_id)).fetchone()
            if not request:
                return False  # Historical failures and unrecorded requests never qualify.
            root = request["root_turn_id"]
            row = db.execute("SELECT * FROM capacity_retries WHERE root_turn_id=?", (root,)).fetchone()
            if row and row["current_turn_id"] != turn.get("id"):
                return False
            if row and row["state"] in {"cancelled", "completed"}:
                return True  # This logical request already has its final receipt.
            if row and row["state"] == "waiting" and safe_capacity_failure(turn):
                return True  # Duplicate terminal notification, one durable schedule.
            if turn.get("status") == "completed":
                if row:
                    db.execute("UPDATE capacity_retries SET state='completed',due_at=NULL WHERE root_turn_id=?", (root,))
                    self._unpause(db, thread_id)
                return False
            if deliberate or self.sessions.closed or not safe_capacity_failure(turn):
                if row:
                    db.execute("UPDATE capacity_retries SET state='failed',due_at=NULL WHERE root_turn_id=?", (root,))
                return False
            if row and row["state"] not in {"running", "waiting"}:
                return False
            payload = json.loads(request["payload_json"])
            if payload["settings"]["client"] != "codex" or not payload["params"].get("model"):
                return False
            pause = db.execute("SELECT paused,pause_kind FROM browser_inbox_wake WHERE thread_id=?", (thread_id,)).fetchone()
            if pause and pause["paused"] and pause["pause_kind"] != "capacity_retry":
                return False
            if self._superseded(db, thread_id, turn["id"]):
                return False
            now = self.store.clock()
            attempts = row["attempts"] if row else 0
            deadline = row["deadline"] if row else now + MAX_ELAPSED
            due = now + retry_delay(turn["error"], attempts + 1, self.jitter)
            if attempts >= MAX_RETRIES or due > deadline:
                if row:
                    db.execute("UPDATE capacity_retries SET state='exhausted',due_at=NULL WHERE root_turn_id=?", (root,))
                return False
            ids = [r[0] for r in db.execute("""SELECT id FROM messages WHERE recipient_session_id=?
                AND (recipient_turn_id=? OR wake_turn_id=?) AND acknowledged_at IS NULL
                AND suppressed_at IS NULL AND classification='action_required'""", (thread_id, turn["id"], turn["id"]))]
            if row:
                ids = sorted(set(ids) | set(json.loads(row["context_ids_json"])))
            db.execute("""INSERT INTO capacity_retries
                (root_turn_id,thread_id,current_turn_id,state,attempts,due_at,deadline,created_at,error,payload_json,context_ids_json)
                VALUES (?,?,?,'waiting',?,?,?,?,?,?,?) ON CONFLICT(root_turn_id) DO UPDATE SET
                state='waiting',due_at=excluded.due_at,error=excluded.error,context_ids_json=excluded.context_ids_json""",
                (root, thread_id, turn["id"], attempts, due, deadline, now,
                 turn["error"]["message"], request["payload_json"], json.dumps(ids)))
            db.execute("""INSERT INTO browser_inbox_wake(thread_id,paused,pause_kind,error,paused_at)
                VALUES (?,1,'capacity_retry',?,?) ON CONFLICT(thread_id) DO UPDATE SET paused=1,
                pause_kind='capacity_retry',error=excluded.error,paused_at=excluded.paused_at""",
                (thread_id, "Waiting to retry model capacity.", now))
        self.store.touch(thread_id, "waiting", turn_active=False)
        self.ready.set()
        self.sessions._publish("browser/changed", {"threadId": thread_id})
        return True

    @staticmethod
    def _unpause(db, thread_id):
        db.execute("""UPDATE browser_inbox_wake SET paused=0,pause_kind=NULL,error=NULL,paused_at=NULL
            WHERE thread_id=? AND pause_kind='capacity_retry'""", (thread_id,))

    def _superseded(self, db, thread_id, expected):
        row = db.execute("SELECT turn_id,attention FROM work_threads WHERE thread_id=?", (thread_id,)).fetchone()
        if not row or row["turn_id"] != expected or row["attention"] == "archived":
            return True
        if db.execute("SELECT 1 FROM browser_message_queue WHERE thread_id=?", (thread_id,)).fetchone():
            return True
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='thread_close_requests'").fetchone() and db.execute(
                "SELECT 1 FROM thread_close_requests WHERE thread_id=? AND status IN ('queued','closing')", (thread_id,)).fetchone():
            return True
        return any(r["params"].get("threadId") == thread_id for r in self.sessions.requests.values())

    def snapshot(self, thread_id):
        with self.store._connection() as db:
            row = db.execute("""SELECT * FROM capacity_retries WHERE thread_id=?
                ORDER BY created_at DESC LIMIT 1""", (thread_id,)).fetchone()
        if not row or row["state"] in {"completed", "cancelled", "failed"}:
            return None
        return {"state": row["state"], "attempt": min(MAX_RETRIES, row["attempts"] + (row["state"] == "waiting")),
                "max_attempts": MAX_RETRIES, "next_at": row["due_at"], "reason": row["error"],
                "turn_id": row["current_turn_id"], "root_turn_id": row["root_turn_id"],
                "detail": row["detail"], "cancellable": row["state"] == "waiting"}

    def cancel(self, thread_id, reason):
        with self.store._connection() as db:
            if not db.execute("SELECT 1 FROM capacity_retries WHERE thread_id=? AND state IN ('waiting','uncertain','exhausted')", (thread_id,)).fetchone():
                return False
            db.execute("BEGIN IMMEDIATE")
            rows = self.cancel_in(db, thread_id, reason)
        self.finish_cancelled(rows)
        return bool(rows)

    def cancel_in(self, db, thread_id, reason):
        """Cancel in the transaction that durably accepts queued input."""
        rows = [dict(row) for row in db.execute("SELECT * FROM capacity_retries WHERE thread_id=? AND state='waiting'", (thread_id,))]
        db.execute("UPDATE capacity_retries SET state='cancelled',due_at=NULL,detail=? WHERE thread_id=? AND state IN ('waiting','uncertain','exhausted')", (reason, thread_id))
        self._unpause(db, thread_id)
        return rows

    def finish_cancelled(self, rows):
        for row in rows:
            self.store.threads.finish_turn(row["thread_id"], turn_id=row["current_turn_id"], status="interrupted")

    def _finish(self, row, state, detail):
        with self.store._connection() as db:
            current = db.execute("SELECT * FROM capacity_retries WHERE root_turn_id=?", (row["root_turn_id"],)).fetchone()
            row = dict(current) if current else row
            db.execute("UPDATE capacity_retries SET state=?,due_at=NULL,detail=? WHERE root_turn_id=?", (state, detail, row["root_turn_id"]))
        self.sessions.inbox_wake.pause(row["thread_id"], detail, kind="unknown" if state == "uncertain" else "failure")
        if state != "uncertain":
            self.store.threads.finish_turn(row["thread_id"], turn_id=row["current_turn_id"], status="failed")
        notify_agent_failure(self.store, row["thread_id"], event_key=["turn", row["current_turn_id"]],
                             stage="capacity_retry", error=detail, turn_id=row["current_turn_id"])
        self.sessions._publish("browser/changed", {"threadId": row["thread_id"]})

    def process_once(self):
        with self.store._connection() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM capacity_retries WHERE state='waiting' AND due_at<=?", (self.store.clock(),))]
        for row in rows:
            if self.stopped.is_set() or self.sessions.closed:
                return
            try:
                self._dispatch(row)
            except Exception:
                logging.getLogger(__name__).exception("Capacity retry worker failed")

    def _dispatch(self, row):
        sessions, tid = self.sessions, row["thread_id"]
        with sessions._thread_lock(tid):
            if not sessions.persistence.flush():
                return
            payload = json.loads(row["payload_json"])
            if not matches_workspace(payload["settings"]["cwd"], sessions.cwd):
                return
            with self.store._connection() as db:
                current = db.execute("SELECT * FROM capacity_retries WHERE root_turn_id=?", (row["root_turn_id"],)).fetchone()
                if not current or current["state"] != "waiting" or current["due_at"] > self.store.clock():
                    return
                pause = db.execute("SELECT paused,pause_kind FROM browser_inbox_wake WHERE thread_id=?", (tid,)).fetchone()
                invalid = self._superseded(db, tid, row["current_turn_id"])
                invalid = invalid or not pause or not pause["paused"] or pause["pause_kind"] != "capacity_retry"
            if invalid or self.settings(tid) != payload["settings"]:
                self.cancel(tid, "New input, Stop, closure or changed settings superseded this retry.")
                return
            if self.store.clock() > row["deadline"]:
                self._finish(row, "exhausted", "The capacity retry budget expired. Send a new message when ready.")
                return
            try:
                detail = sessions.read(tid)
            except Exception:
                self._finish(row, "uncertain", "Native execution could not be verified. The capacity request was not replayed.")
                return
            if not sessions.persistence.flush():
                return
            turns = sessions._history(tid).get("turns", [])
            if detail["thread"].get("historyUnavailable"):
                self._finish(row, "uncertain", "Native turn history is unavailable. The capacity request was not replayed.")
                return
            if detail["running"] or not turns or turns[-1].get("id") != row["current_turn_id"] or not safe_capacity_failure(turns[-1]):
                self.cancel(tid, "The native turn changed; the rejected request was not replayed.")
                return
            if self.settings(tid) != payload["settings"]:
                self.cancel(tid, "The native model settings changed; the rejected request was not replayed.")
                return
            with self.store._connection() as db:
                db.execute("BEGIN IMMEDIATE")
                pause = db.execute("SELECT paused,pause_kind FROM browser_inbox_wake WHERE thread_id=?", (tid,)).fetchone()
                if (self._superseded(db, tid, row["current_turn_id"]) or not pause or not pause["paused"]
                        or pause["pause_kind"] != "capacity_retry"):
                    return
                claimed = db.execute("""UPDATE capacity_retries SET state='reverting',attempts=attempts+1,owner=?
                    WHERE root_turn_id=? AND current_turn_id=? AND state='waiting'""",
                    (self.owner, row["root_turn_id"], row["current_turn_id"])).rowcount
            if not claimed:
                return
            try:
                rpc = sessions._rpc(tid)
                retained_ids = [t["id"] for t in turns[:-1]]
                reverted = rpc.request("thread/revert", {"threadId": tid, "beforeTurnId": row["current_turn_id"]})
                if (reverted.get("thread") or {}).get("id") != tid:
                    raise RuntimeError("The provider did not confirm the reverted thread.")
                # Revert returns metadata with an intentionally empty turns list.
                # Read native history directly, without the display mirror or its
                # fallback, to prove removal and preservation of every earlier turn.
                native = sessions._query_thread(rpc, tid, include_turns=True)
                if (native.get("id") != tid or not isinstance(native.get("turns"), list)
                        or [t.get("id") for t in native["turns"]] != retained_ids
                        or native.get("status", {}).get("type") in {"active", "systemError"}):
                    raise RuntimeError("Native history did not confirm removal of only the empty rejected turn.")
                with self.store._connection() as db:
                    if not db.execute("UPDATE capacity_retries SET state='submitting' WHERE root_turn_id=? AND state='reverting' AND owner=?", (row["root_turn_id"], self.owner)).rowcount:
                        return
                self.prepare(tid, payload["params"], payload["context"], root=row["root_turn_id"],
                             settings=payload["settings"], replace=row["current_turn_id"])
                with sessions.lock:
                    sessions.starting_notification_context[tid] = payload["context"]
                result = rpc.request("turn/start", payload["params"])
                if not result.get("turn", {}).get("id"):
                    raise RuntimeError("The provider did not confirm a retry turn.")
                captured = self.starting(tid)
                sessions.persistence.submit(lambda: self.started(tid, result["turn"]["id"], captured))
            except Exception as exc:
                self._finish(row, "uncertain", "Capacity retry delivery needs review; it will not be sent again. " + str(exc))
            finally:
                self.preparing.pop(tid, None)
                with sessions.lock:
                    sessions.starting_notification_context.pop(tid, None)

    def start(self):
        if self.worker is None:
            self.worker = threading.Thread(target=self._run, name="capacity-retries", daemon=True)
            self.worker.start()

    def _run(self):
        while not self.stopped.is_set() and not self.sessions.closed:
            try:
                self.process_once()
            except Exception:
                logging.getLogger(__name__).exception("Capacity retry scheduler failed")
            self.ready.wait(.5)
            self.ready.clear()

    def close(self):
        self.stopped.set()
        self.ready.set()
        if self.worker is not None:
            self.worker.join(timeout=6)
