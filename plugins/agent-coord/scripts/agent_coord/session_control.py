"""Owner-routed interactive control and a durable journal of public output.

Local callers identify themselves through their registered coordination session.
A caller needs current supervision (creator provenance by default) or an
explicit local --adopt grant.
Never export reasoning, tool output, or partial agent-message deltas.
"""
from __future__ import annotations

import json
import time

from . import supervision
from .store import CoordinationError


class SessionControl:
    def __init__(self, store):
        self.store = store
        with store._connection() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS interactive_bindings (
                    thread_id TEXT PRIMARY KEY, sender_id TEXT NOT NULL,
                    touched_at REAL NOT NULL, snapshot_json TEXT, refreshed_at REAL
                );
                CREATE TABLE IF NOT EXISTS interactive_events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, thread_id TEXT NOT NULL,
                    event_key TEXT NOT NULL, payload_json TEXT NOT NULL,
                    UNIQUE(thread_id, event_key)
                );
                CREATE INDEX IF NOT EXISTS interactive_events_thread ON interactive_events(thread_id, seq);
                CREATE TABLE IF NOT EXISTS supervisor_interrupts (
                    request_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL, requester TEXT NOT NULL,
                    reason TEXT NOT NULL, requested_at REAL NOT NULL,
                    UNIQUE(thread_id, turn_id)
                );
            """
            )

    def authorize(self, sender, thread):
        self.store.get_session(sender)
        self.store.get_session(thread)
        with self.store._connection() as db:
            # The current supervisor: the creator unless supervision was transferred.
            created = supervision.authorizes(db, sender, thread)
            binding = db.execute(
                "SELECT sender_id FROM interactive_bindings WHERE thread_id=?",
                (thread,),
            ).fetchone()
            # A transfer revokes the retired supervisor's binding.
            stale = bool(binding) and supervision.retired_supervisor(db, binding["sender_id"], thread)
        if binding and binding["sender_id"] != sender and not (stale and created):
            raise CoordinationError(
                "Another interactive controller owns this conversation."
            )
        granted = binding is not None and binding["sender_id"] == sender and not stale
        if not created and not granted:
            raise CoordinationError(
                "Interactive controls require this conversation's current supervisor."
            )

    def adopt(self, sender, thread):
        # Explicit local CLI authority, analogous to sending another app thread
        # a user request. Grants never replace an existing interactive owner.
        self.store.get_session(sender)
        self.store.get_session(thread)
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT sender_id FROM interactive_bindings WHERE thread_id=?",
                (thread,),
            ).fetchone()
            if existing and existing["sender_id"] != sender:
                raise CoordinationError(
                    "Another interactive controller already owns this conversation."
                )
            db.execute(
                "INSERT OR IGNORE INTO interactive_bindings(thread_id,sender_id,touched_at) VALUES (?,?,?)",
                (thread, sender, self.store.clock()),
            )

    def bind(self, sender, thread):
        self.authorize(sender, thread)
        with self.store._connection() as db:
            db.execute(
                """INSERT INTO interactive_bindings(thread_id,sender_id,touched_at) VALUES (?,?,?)
                ON CONFLICT(thread_id) DO UPDATE SET sender_id=excluded.sender_id,
                touched_at=excluded.touched_at""",
                (thread, sender, self.store.clock()),
            )

    def snapshot(self, sender, thread, after=0):
        self.authorize(sender, thread)
        if type(after) is not int or after < 0:
            raise CoordinationError("The event cursor must be a nonnegative integer.")
        with self.store._connection() as db:
            row = db.execute(
                "SELECT * FROM interactive_bindings WHERE thread_id=? AND sender_id=?",
                (thread, sender),
            ).fetchone()
            if not row:
                raise CoordinationError(
                    "Watch this conversation before reading its interactive snapshot."
                )
            db.execute(
                "UPDATE interactive_bindings SET touched_at=? WHERE thread_id=?",
                (self.store.clock(), thread),
            )
            events = db.execute(
                "SELECT * FROM interactive_events WHERE thread_id=? AND seq>? ORDER BY seq LIMIT 100",
                (thread, after),
            ).fetchall()
        snapshot = (
            json.loads(row["snapshot_json"])
            if row["snapshot_json"]
            else {"status": "connecting", "active_turn_id": None}
        )
        return {
            **snapshot,
            "thread_id": thread,
            "refreshed_at": row["refreshed_at"],
            "connected": bool(
                row["refreshed_at"]
                and self.store.clock() - row["refreshed_at"] < 15
                and snapshot.get("status") != "disconnected"
            ),
            "cursor": events[-1]["seq"] if events else after,
            "events": [
                {"seq": e["seq"], **json.loads(e["payload_json"])} for e in events
            ],
        }

    def append(self, thread, key, payload):
        with self.store._connection() as db:
            if not db.execute(
                "SELECT 1 FROM interactive_bindings WHERE thread_id=?", (thread,)
            ).fetchone():
                return
            db.execute(
                "INSERT OR IGNORE INTO interactive_events(thread_id,event_key,payload_json) VALUES (?,?,?)",
                (thread, key, json.dumps(payload)),
            )

    def observe(self, method, params, *, recovered=False):
        thread = params.get("threadId")
        turn = params.get("turn") or {}
        turn_id = params.get("turnId") or turn.get("id")
        if not thread or not turn_id:
            return
        if method == "item/completed":
            item = params.get("item") or {}
            if (
                item.get("type") != "agentMessage"
                or not isinstance(item.get("text"), str)
                or not item.get("id")
            ):
                return
            if item.get("phase") not in (None, "commentary", "final_answer"):
                return
            self.append(
                thread,
                f"{turn_id}:item:{item['id']}",
                {
                    "kind": "message",
                    "turn_id": turn_id,
                    "item_id": item["id"],
                    "phase": item.get("phase"),
                    "text": item["text"][:64000],
                    "at": self.store.clock(),
                    "recovered": recovered,
                },
            )
        elif method == "turn/completed":
            for item in turn.get("items", []):
                self.observe(
                    "item/completed",
                    {"threadId": thread, "turnId": turn_id, "item": item},
                    recovered=recovered,
                )
            self.append(
                thread,
                f"{turn_id}:completed",
                {
                    "kind": "turn_completed",
                    "turn_id": turn_id,
                    "status": turn.get("status"),
                    "at": self.store.clock(),
                    "recovered": recovered,
                },
            )

    def refresh(self, sessions):
        with self.store._connection() as db:
            rows = db.execute(
                "SELECT * FROM interactive_bindings WHERE touched_at>? AND (refreshed_at IS NULL OR refreshed_at<?)",
                (self.store.clock() - 90, self.store.clock() - 1),
            ).fetchall()
        for row in rows:
            tid = row["thread_id"]
            from .workspaces import matches_workspace

            record = self.store.get_session(tid)
            if not matches_workspace(record["cwd"], sessions.cwd):
                continue
            with sessions.lock:
                loaded = tid in sessions.loaded
            if not loaded and record["ended_at"] is None:
                continue  # Another runtime owns the native connection.
            try:
                value = sessions.read(tid)
                turns = value.get("thread", {}).get("turns", [])
                latest = turns[-1] if turns else {}
                snapshot = {
                    "active_turn_id": value.get("activeTurn"),
                    "status": "waiting_user"
                    if value.get("requests")
                    else "running"
                    if value.get("running")
                    else latest.get("status", "idle"),
                    "pending_questions": len(value.get("requests") or []),
                    "last_turn_id": latest.get("id"),
                    "error": None,
                }
                for turn in turns[-10:]:
                    if turn.get("status") in {"completed", "interrupted", "failed"}:
                        self.observe(
                            "turn/completed",
                            {"threadId": tid, "turn": turn},
                            recovered=True,
                        )
            except Exception as exc:
                snapshot = {
                    "active_turn_id": None,
                    "status": "disconnected",
                    "error": str(exc)[:500],
                }
            with self.store._connection() as db:
                db.execute(
                    "UPDATE interactive_bindings SET snapshot_json=?,refreshed_at=? WHERE thread_id=?",
                    (json.dumps(snapshot), self.store.clock(), tid),
                )


def supervisor_interrupted(store, thread_id, turn_id):
    """An immutable provider-boundary receipt, independent of request outcome."""
    with store.reading(), store._connection() as db:
        return db.execute("SELECT 1 FROM supervisor_interrupts WHERE thread_id=? AND turn_id=?",
                          (thread_id, turn_id)).fetchone() is not None


def validate_interrupt(control, sender, thread, payload, request_id):
    if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 160:
        raise CoordinationError("Supervisor interruption requires a stable request ID of 1–160 characters.")
    if set(payload) != {"expected_turn_id", "reason"}:
        raise CoordinationError("Supervisor interruption requires an exact turn ID and reason.")
    for key, limit in (("expected_turn_id", 200), ("reason", 1000)):
        if not isinstance(payload[key], str) or not payload[key].strip() or len(payload[key]) > limit:
            raise CoordinationError(f"{key} must contain 1–{limit} characters.")
    with control.store.reading(), control.store._connection() as db:
        if not supervision.authorizes(db, sender, thread):
            raise CoordinationError("Only this app agent's current supervisor can interrupt it.")
    record = control.settings(thread)
    if record["client"] not in {"codex", "claude"}:
        raise CoordinationError("Supervisor interruption requires a Codex or Claude app conversation.")
    return record["cwd"]


def execute_interrupt(worker, request, payload):
    """Reserve one exact native interrupt before crossing the provider boundary."""
    store, sessions = worker.control.store, worker.sessions
    thread, sender, turn = request["thread_id"], request["sender_session_id"], payload["expected_turn_id"]
    with sessions._thread_lock(thread):
        validate_interrupt(worker.control, sender, thread, payload, request["request_id"])
        detail = sessions.read(thread)
        if detail.get("activeTurn") != turn or not detail.get("running"):
            raise CoordinationError("The active turn changed. Inspect the exact turn before interrupting.")
        with store.database_operation("supervisor.interrupt"), store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if not supervision.authorizes(db, sender, thread):
                raise CoordinationError("Only this app agent's current supervisor can interrupt it.")
            pending_close = (db.execute("SELECT 1 FROM sqlite_master WHERE name='thread_close_requests'").fetchone()
                             and db.execute("SELECT 1 FROM thread_close_requests WHERE thread_id=? AND status IN ('queued','closing')", (thread,)).fetchone())
            if db.execute("SELECT attention FROM work_threads WHERE thread_id=?", (thread,)).fetchone()[0] == "archived" or pending_close:
                raise CoordinationError("The conversation is closed or has a pending close request.")
            if db.execute("SELECT 1 FROM supervisor_interrupts WHERE thread_id=? AND turn_id=?", (thread, turn)).fetchone():
                raise CoordinationError("This turn already has an interrupt receipt. Inspect its request status; never retry native interruption.")
            wake = db.execute("SELECT * FROM browser_inbox_wake WHERE thread_id=?", (thread,)).fetchone()
            user_stop = db.execute("SELECT 1 FROM turn_stop_requests WHERE thread_id=? AND turn_id=?", (thread, turn)).fetchone()
            if (wake and wake["paused"] and wake["pause_kind"] == "stop") or user_stop:
                raise CoordinationError("The user stopped this turn. Supervisor interruption cannot replace user Stop intent.")
            db.execute("INSERT INTO supervisor_interrupts VALUES (?,?,?,?,?,?)",
                       (request["request_id"], thread, turn, sender, payload["reason"], store.clock()))
            sessions.inbox_wake._save_pause(db, thread, "The supervisor interrupted this turn. Inspect outcomes and queue fresh instructions.", "supervisor")
            db.execute("UPDATE browser_inbox_wake SET recovery_after_message_id=(SELECT COALESCE(MAX(id),0) FROM messages) WHERE thread_id=?", (thread,))
        sessions.queue.pause(thread, "The supervisor interrupted this turn. Review queued user input before resuming.")
        try:
            # Never use the UI interrupt path: it deliberately records user Stop.
            native = sessions._rpc(thread).request("turn/interrupt", {"threadId": thread, "turnId": turn})
        except Exception as exc:
            worker.control.complete(request["request_id"], error=str(exc), uncertain=True)
            return None
        return {"thread_id": thread, "interrupted_turn_id": turn, "interrupted_by": sender,
                "request_id": request["request_id"], "native": native}


def validate(control, sender, thread, payload):
    service = SessionControl(control.store)
    action = payload.get("action")
    adopting = action == "watch" and payload.get("adopt") is True
    if not adopting:
        service.authorize(sender, thread)
    if action not in {"watch", "submit", "interrupt"}:
        raise CoordinationError("Choose watch, submit, or interrupt.")
    allowed = (
        {"action"}
        if action == "watch"
        else {"action", "expected_turn_id", "message"}
        if action == "submit"
        else {"action", "expected_turn_id"}
    )
    if adopting:
        allowed = allowed | {"adopt"}
    if set(payload) != allowed:
        raise CoordinationError("Invalid interactive command fields.")
    if action != "watch":
        expected = payload["expected_turn_id"]
        if expected is not None and (
            not isinstance(expected, str) or not expected or len(expected) > 200
        ):
            raise CoordinationError(
                "Expected turn must be an exact turn ID or null for idle."
            )
    if action == "submit":
        text = payload["message"]
        if (
            not isinstance(text, str)
            or not text.strip()
            or len(text) > 100000
            or text.lstrip().startswith("/")
        ):
            raise CoordinationError(
                "Supply 1–100000 characters of user input, not a slash command."
            )
    record = control.settings(thread)
    if record["client"] != "codex":
        raise CoordinationError(
            "Interactive work currently requires a Codex app conversation."
        )
    if adopting and record["archived"]:
        raise CoordinationError("Reopen the conversation before adopting it.")
    return record["cwd"]


def execute(worker, request, payload):
    control, sessions = worker.control, worker.sessions
    service = SessionControl(control.store)
    tid, sender = request["thread_id"], request["sender_session_id"]
    if payload["action"] == "interrupt" and payload.get("expected_turn_id"):
        # A pre-upgrade queued interactive request receives the same current
        # supervisor guards as requests submitted through the new entry point.
        with control.store.reading(), control.store._connection() as db:
            supervised = supervision.authorizes(db, sender, tid)
        if supervised:
            return execute_interrupt(worker, request, {"expected_turn_id": payload["expected_turn_id"],
                "reason": "Current supervisor requested interruption through session-control."})
    if payload["action"] == "watch" and payload.get("adopt") is True:
        service.adopt(sender, tid)
    service.authorize(sender, tid)
    service.bind(sender, tid)
    if payload["action"] == "watch":
        return {"thread_id": tid, "watching": True}
    with sessions._thread_lock(tid):
        value = sessions.read(tid)
        expected = payload["expected_turn_id"]
        if value.get("activeTurn") != expected or (
            expected is None and value.get("running")
        ):
            raise CoordinationError(
                "The active turn changed. Refresh before submitting another command."
            )
        # Record uncertainty at the provider boundary. The command is never replayed.
        try:
            if payload["action"] == "submit":
                body = {"message": payload["message"]}
                if expected is not None:
                    body["expectedTurnId"] = expected
                result = sessions.send(tid, body, start_only=expected is None)
            else:
                sessions.inbox_wake.pause(
                    tid,
                    "Work stopped by its controller. Send a message to continue.",
                    kind="stop",
                )
                sessions.queue.pause(tid, "Work stopped by its controller.")
                result = sessions.interrupt(tid) if expected else {"stopped": True}
        except Exception as exc:
            control.complete(request["request_id"], error=str(exc), uncertain=True)
            return None
        return {"thread_id": tid, "accepted": True, "native": result}
