"""Sanitized execution evidence for metadata reads; never load a provider.

An active turn is a lifecycle fact, not proof that a model or tool is working.
Only the owning runtime's live request map establishes a native approval wait.
"""
from __future__ import annotations

import json
import os
import uuid

from .store import _iso

RUNTIME_STALE_SECONDS = 180
APPROVAL_METHODS = frozenset({
    "item/commandExecution/requestApproval", "item/fileChange/requestApproval",
    "item/permissions/requestApproval",
})
ANSWER_METHODS = frozenset({"item/tool/requestUserInput", "mcpServer/elicitation/request"})
TERMINAL_STATES = frozenset({"idle", "failed", "stopped", "interrupted"})


def initialize(db):
    db.execute("""CREATE TABLE IF NOT EXISTS execution_runtimes (
        runtime_id TEXT PRIMARY KEY, pid INTEGER NOT NULL,
        heartbeat_at REAL NOT NULL, closed_at REAL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS execution_snapshots (
        thread_id TEXT PRIMARY KEY REFERENCES sessions(session_id),
        runtime_id TEXT NOT NULL, turn_id TEXT, state TEXT NOT NULL,
        source TEXT NOT NULL, observed_at REAL NOT NULL,
        pending_json TEXT NOT NULL, permissions_json TEXT NOT NULL)""")


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (ProcessLookupError, ValueError, OverflowError):
        return False


def read_many(store, db, thread_ids):
    """One indexed batch for session and thread lists, including durable asks.

    Snapshot age is not wait age: a permission request may legitimately remain
    unanswered for hours. Runtime heartbeat and process liveness bound freshness.
    """
    rows = db.execute("""SELECT s.session_id, s.turn_active, s.last_seen_at, s.ended_at,
        t.turn_id AS current_turn_id, t.turn_started_at,
        c.status AS completion_status, c.completed_at,
        stop.requested_at AS stop_at,
        x.*, r.pid, r.heartbeat_at, r.closed_at,
        a.id AS ask_id, a.created_at AS ask_created_at
        FROM json_each(?) ids JOIN sessions s ON s.session_id = ids.value
        LEFT JOIN work_threads t ON t.thread_id = s.session_id
        LEFT JOIN turn_completions c ON c.thread_id = t.thread_id AND c.turn_key = t.turn_key
        LEFT JOIN turn_stop_requests stop ON stop.thread_id = t.thread_id AND stop.turn_id = t.turn_id
        LEFT JOIN execution_snapshots x ON x.thread_id = s.session_id
        LEFT JOIN execution_runtimes r ON r.runtime_id = x.runtime_id
        LEFT JOIN asks a ON a.id = (SELECT id FROM asks WHERE session_id = s.session_id
            AND status = 'open' AND blocking = 1 ORDER BY id LIMIT 1)""",
        (json.dumps(list(thread_ids)),)).fetchall()
    now, processes, result = store.clock(), {}, {}
    for row in rows:
        value = {"state": "unknown", "source": "unavailable", "freshness": "unknown",
                 "observed_at": None, "turn_id": row["current_turn_id"],
                 "runtime_id": row["runtime_id"], "runtime_heartbeat_at": _iso(row["heartbeat_at"]),
                 "stale_after_seconds": RUNTIME_STALE_SECONDS, "pending_requests": []}
        value["permissions"] = json.loads(row["permissions_json"]) if row["permissions_json"] else None
        matching = row["state"] is not None and row["turn_id"] == row["current_turn_id"]
        if matching:
            pid = row["pid"]
            if pid not in processes:
                processes[pid] = bool(pid and _alive(pid))
            live = (processes[pid] and row["closed_at"] is None
                    and row["heartbeat_at"] is not None
                    and 0 <= now - row["heartbeat_at"] <= RUNTIME_STALE_SECONDS)
            recorded = row["state"] in TERMINAL_STATES
            value.update(source=row["source"], observed_at=_iso(row["observed_at"]), turn_id=row["turn_id"],
                         freshness="live" if live else "recorded" if recorded else "stale")
            if live or recorded:
                value["state"] = row["state"]
            if live:
                value["pending_requests"] = json.loads(row["pending_json"])
        # Exact-turn durable completion/Stop evidence also works after restart,
        # and takes precedence over an older pending snapshot.
        completed = row["completion_status"]
        if completed and (not matching or not row["turn_active"] or row["completed_at"] >= row["observed_at"]):
            state = ("stopped" if row["stop_at"] is not None and completed != "completed" else
                     "idle" if completed == "completed" else
                     "failed" if completed == "failed" else "interrupted")
            value.update(state=state, source="user_stop" if state == "stopped" else "turn_completion",
                         freshness="recorded", observed_at=_iso(row["completed_at"]),
                         turn_id=row["current_turn_id"], runtime_id=None, runtime_heartbeat_at=None, pending_requests=[])
        # A durable blocking ask remains open independently of native execution.
        # Do not override a deliberate Stop or a native approval still pending.
        if row["ask_id"] and value["state"] not in {"stopped", "failed", "waiting_approval", "interrupted"}:
            value.update(state="waiting_answer", source="blocking_ask", freshness="recorded",
                         ask_id=row["ask_id"], observed_at=_iso(row["ask_created_at"]), runtime_id=None,
                         runtime_heartbeat_at=None)
        result[row["session_id"]] = value
    return result


def permission_settings(db, record, execution):
    """Separate the saved selection, pending mutations, and native observation."""
    from .permission_modes import selected_mode
    result = {"selected_mode": selected_mode(record), "saved_yolo": bool(record.get("yolo")),
              "last_native_observation": execution.get("permissions"),
              "execution_freshness": execution["freshness"], "pending_changes": []}
    for row in db.execute("""SELECT * FROM app_control_requests
        WHERE thread_id = ? AND operation = 'settings' AND status IN ('queued', 'running')
        ORDER BY created_at, request_id""", (record["thread_id"],)):
        payload = json.loads(row["payload_json"])
        requested = {k: payload[k] for k in ("yolo", "permission_mode") if k in payload}
        if requested:
            result["pending_changes"].append({"request_id": row["request_id"],
                "status": row["status"], "requested_at": _iso(row["created_at"]), **requested})
    return result


class RuntimeExecution:
    """Transition-only bookkeeping from BrowserSessions' authoritative map."""

    def __init__(self, sessions):
        self.sessions = sessions
        self.store = sessions.store
        self.runtime_id = str(uuid.uuid4())
        self.permissions = {}
        with self.store._connection() as db:
            initialize(db)
            db.execute("INSERT INTO execution_runtimes VALUES (?, ?, ?, NULL)",
                       (self.runtime_id, os.getpid(), self.store.clock()))

    def heartbeat(self):
        with self.store._connection() as db:
            db.execute("UPDATE execution_runtimes SET heartbeat_at = ? WHERE runtime_id = ? AND closed_at IS NULL",
                       (self.store.clock(), self.runtime_id))

    def observe_permissions(self, thread_id, record):
        """Called on native settings observation or accepted sticky override."""
        self.permissions[thread_id] = {
            "selected_mode": record.get("permission_mode"), "saved_yolo": bool(record.get("yolo")),
            "effective_mode": record.get("permission_mode_effective"),
            "recorded_at": _iso(self.store.clock()), "runtime_id": self.runtime_id}

    def capture(self, thread_id, *, state=None, source="provider_event", turn_id=None):
        if not thread_id:
            return
        sessions = self.sessions
        # Caller holds the same reentrant lock used by request receipt/removal.
        with sessions.lock:
            turn_id = turn_id or sessions.active.get(thread_id)
            pending = []
            for key, request in sessions.requests.items():
                params, method = request.get("params") or {}, request.get("method", "")
                if params.get("threadId") != thread_id:
                    continue
                if turn_id and params.get("turnId") and params["turnId"] != turn_id:
                    continue
                kind = ("approval" if method in APPROVAL_METHODS else
                        "answer" if method in ANSWER_METHODS else "unsupported")
                # Whitelist only transport metadata. Tool arguments, commands,
                # answers, elicitation schemas and credentials stay private.
                pending.append({"request_key": key, "method": method, "kind": kind,
                                "received_at": _iso(request.get("received_at"))})
            if pending:
                state = ("waiting_approval" if any(p["kind"] == "approval" for p in pending) else
                         "waiting_answer" if any(p["kind"] == "answer" for p in pending) else "unknown")
                source = "provider_request"
            state = state or ("running" if thread_id in sessions.active else "idle")
            observed = self.store.clock()
            permissions = self.permissions.get(thread_id)
            payload = (thread_id, self.runtime_id, turn_id, state, source, observed, json.dumps(pending), json.dumps(permissions))

        def persist():
            with self.store._connection() as db:
                # Optional status bookkeeping must not hold the provider event
                # lock for the store's normal five-second writer timeout.
                # BrowserPersistence retries contention in transition order.
                db.execute("PRAGMA busy_timeout = 0")
                # Ignore late retries from a previous runtime after a new owner
                # has supplied newer evidence. BrowserPersistence orders this
                # runtime's own writes without replaying provider operations.
                db.execute("""INSERT INTO execution_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(thread_id) DO UPDATE SET runtime_id=excluded.runtime_id,
                    turn_id=excluded.turn_id, state=excluded.state, source=excluded.source,
                    observed_at=excluded.observed_at, pending_json=excluded.pending_json,
                    permissions_json=excluded.permissions_json
                    WHERE execution_snapshots.observed_at <= excluded.observed_at""", payload)
        sessions.persistence.submit(persist)

    def close(self):
        with self.store._connection() as db:
            db.execute("UPDATE execution_runtimes SET closed_at = ? WHERE runtime_id = ?",
                       (self.store.clock(), self.runtime_id))
