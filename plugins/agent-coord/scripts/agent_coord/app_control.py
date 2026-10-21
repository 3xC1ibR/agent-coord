"""Durable agent requests executed by the app that owns provider connections."""
from __future__ import annotations

import json
import logging
from pathlib import Path
import threading
import time
import uuid

from .navigation import NavigationStore
from .app_agent_failures import notify_creation_failure
from .store import CoordinationError
from .workspaces import matches_workspace

LEASE_SECONDS = 60
TERMINAL = {"completed", "failed", "uncertain", "cancelled"}
SETTINGS = ("cwd", "model", "effort", "yolo")


class AppControl:
    def __init__(self, store):
        self.store = store
        with store._connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS app_control_requests (
                    request_id TEXT PRIMARY KEY, operation TEXT NOT NULL,
                    sender_session_id TEXT NOT NULL, cwd TEXT NOT NULL,
                    thread_id TEXT, payload_json TEXT NOT NULL,
                    status TEXT NOT NULL, owner TEXT, result_json TEXT,
                    error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS app_control_runtimes (
                    id TEXT PRIMARY KEY, heartbeat REAL NOT NULL
                );
            """)

    def settings(self, thread_id):
        self.store.get_session(thread_id)
        with self.store._connection() as db:
            row = None
            if db.execute("SELECT 1 FROM sqlite_master WHERE name = 'browser_sessions'").fetchone():
                row = db.execute("SELECT * FROM browser_sessions WHERE thread_id = ?", (thread_id,)).fetchone()
        if not row:
            raise CoordinationError("This is a terminal session. Settings controls require an app conversation.")
        result = dict(row)
        result["yolo"] = bool(result["yolo"])
        result["archived"] = bool(result["archived"])
        result["session_id"] = thread_id
        result["url"] = NavigationStore(self.store).link(thread=thread_id)["url"]
        return result

    def request(self, operation, sender_session_id, payload, *, thread_id=None, request_id=None):
        self.store.get_session(sender_session_id)
        if operation not in {"create", "settings", "models", "move", "resume"}:
            raise CoordinationError("Unknown app control operation.")
        payload = dict(payload)
        if operation in {"settings", "move", "resume"}:
            record = self.settings(thread_id)
            cwd = record["cwd"]
            if operation == "resume":
                expected = payload.get("expected_turn_id")
                if set(payload) != {"expected_turn_id"} or not isinstance(expected, str) or not expected.strip() or len(expected) > 200:
                    raise CoordinationError("Recovery requires the expected failed turn ID.")
                with self.store._connection() as db:
                    if not db.execute("""SELECT 1 FROM app_control_requests WHERE operation = 'create'
                            AND thread_id = ? AND sender_session_id = ? AND status = 'completed'""",
                            (thread_id, sender_session_id)).fetchone():
                        raise CoordinationError("Only this app agent's creating orchestrator can recover it.")
            elif operation == "move":
                from .thread_move import destination_directory
                if set(payload) != {"cwd"}:
                    raise CoordinationError("Moving a thread accepts its destination cwd only.")
                payload["cwd"] = destination_directory(payload["cwd"])
                if record["client"] not in {"codex", "claude"}:
                    raise CoordinationError("This provider keeps its original workspace. Start a new conversation in the destination project.")
            elif not payload or set(payload) - {"model", "effort", "yolo"}:
                raise CoordinationError("Choose model, effort, or permissions to change.")
        else:
            cwd = str(Path(payload.get("cwd") or self.store.get_session(sender_session_id)["cwd"]).expanduser().resolve())
            if not Path(cwd).is_dir():
                raise CoordinationError("Choose an existing workspace directory.")
            payload["cwd"] = cwd
            if payload.get("client", "codex") not in {"codex", "claude", "antigravity", "kimi", "muse"}:
                raise CoordinationError("Choose Codex, Claude Code, Google Antigravity, or Kimi Code as the session provider.")
        if operation == "create":
            if payload.get("mode") not in (None, "orchestration"):
                raise CoordinationError("Creation mode must be orchestration or omitted.")
            if payload.get("mode") is None:
                payload.pop("mode", None)
            prompt = payload.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 100000:
                raise CoordinationError("The initial request must contain 1–100000 characters.")
            if not isinstance(payload.get("reply_required", False), bool):
                raise CoordinationError("Initial request reply_required must be a boolean.")
            # Keep the legacy payload shape so opt-out and pre-upgrade retries match.
            if not payload.get("reply_required", False):
                payload.pop("reply_required", None)
            if not isinstance(payload.get("subscribe", True), bool):
                raise CoordinationError("Initial request subscribe must be a boolean.")
            # Omitted and explicitly enabled subscriptions share a retry key.
            if payload.get("subscribe", True):
                payload.pop("subscribe", None)
        identifier = request_id or str(uuid.uuid4())
        if not isinstance(identifier, str) or not identifier.strip() or len(identifier) > 160:
            raise CoordinationError("Request ID must contain 1–160 characters.")
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT * FROM app_control_requests WHERE request_id = ?", (identifier,)).fetchone()
            if previous:
                old_payload = json.loads(previous["payload_json"])
                old_payload.pop("expected_settings", None)
                if operation == "create" and old_payload.get("reply_required") is False:
                    old_payload.pop("reply_required")
                if operation == "create" and old_payload.get("subscribe") is True:
                    old_payload.pop("subscribe")
                # thread_id on create is filled in as soon as the provider replies.
                if (previous["operation"] != operation or previous["sender_session_id"] != sender_session_id
                        or old_payload != payload or (operation != "move" and previous["cwd"] != cwd)
                        or (operation in {"settings", "move", "resume"} and previous["thread_id"] != thread_id)):
                    raise CoordinationError("Request ID already belongs to a different app request.")
            else:
                if operation in {"settings", "move"}:
                    payload["expected_settings"] = {key: record[key] for key in SETTINGS}
                now = self.store.clock()
                # Pre-recovery app workers treat unknown queued operations as
                # creation. Keep recovery invisible to their queued-only scan.
                queued = "queued_resume" if operation == "resume" else "queued"
                db.execute("""INSERT INTO app_control_requests
                    (request_id, operation, sender_session_id, cwd, thread_id, payload_json,
                     status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (identifier, operation, sender_session_id, cwd, thread_id, json.dumps(payload, sort_keys=True), queued, now, now))
        return self.status(identifier)

    def _reconcile(self, db):
        if not db.in_transaction:
            db.execute("BEGIN IMMEDIATE")
        cutoff = self.store.clock() - LEASE_SECONDS
        # Never replay provider creation or a possibly applied settings change.
        stale = db.execute("""SELECT request_id FROM app_control_requests
            WHERE status = 'running' AND updated_at < ? AND NOT EXISTS
            (SELECT 1 FROM app_control_runtimes r WHERE r.id = app_control_requests.owner AND r.heartbeat >= ?)""",
            (cutoff, cutoff)).fetchall()
        db.execute("""UPDATE app_control_requests SET status = 'uncertain',
            error = 'The app runtime stopped before confirming the result. Inspect the conversation before retrying.',
            updated_at = ? WHERE status = 'running' AND updated_at < ? AND NOT EXISTS
            (SELECT 1 FROM app_control_runtimes r WHERE r.id = app_control_requests.owner AND r.heartbeat >= ?)""",
            (self.store.clock(), cutoff, cutoff))
        for row in stale:
            notify_creation_failure(self.store, db, db.execute(
                "SELECT * FROM app_control_requests WHERE request_id = ?", (row["request_id"],)).fetchone())

    def status(self, request_id, *, wait=0):
        if not isinstance(wait, (int, float)) or not 0 <= wait <= 30:
            raise CoordinationError("Wait must be between 0 and 30 seconds.")
        deadline = time.monotonic() + wait
        while True:
            with self.store._connection() as db:
                self._reconcile(db)
                row = db.execute("SELECT * FROM app_control_requests WHERE request_id = ?", (request_id,)).fetchone()
            if not row:
                raise CoordinationError("App request not found.")
            result = {key: row[key] for key in ("request_id", "operation", "status", "thread_id", "error")}
            if result["status"] == "queued_resume":
                result["status"] = "queued"
            result["result"] = json.loads(row["result_json"]) if row["result_json"] else None
            if result["status"] == "queued":
                result["message"] = "Queued for Ribbon Field. The app must be running in this workspace; settings and project moves wait for idle."
                if row["operation"] == "resume":
                    result["message"] = "Recovery queued for an updated Ribbon Field runtime in this workspace. No failed prompt has been replayed."
            if row["status"] in TERMINAL or time.monotonic() >= deadline:
                return result
            time.sleep(min(.1, max(0, deadline - time.monotonic())))

    def cancel(self, request_id):
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM app_control_requests WHERE request_id = ?", (request_id,)).fetchone()
            if not row:
                raise CoordinationError("App request not found.")
            if row[0] == "running":
                raise CoordinationError("The app request is already executing. Inspect request-status before acting again.")
            db.execute("UPDATE app_control_requests SET status = 'cancelled', updated_at = ? WHERE request_id = ? AND status IN ('queued', 'queued_resume')",
                       (self.store.clock(), request_id))
        return self.status(request_id)

    def pending(self):
        with self.store._connection() as db:
            self._reconcile(db)
            return [dict(row) for row in db.execute("""SELECT * FROM app_control_requests
                WHERE status = 'queued' OR (operation = 'resume' AND status = 'queued_resume') ORDER BY created_at, rowid""")]

    def claim(self, request_id, owner):
        with self.store._connection() as db:
            return bool(db.execute("""UPDATE app_control_requests SET status = 'running', owner = ?, updated_at = ?
                WHERE request_id = ? AND (status = 'queued' OR (operation = 'resume' AND status = 'queued_resume'))""",
                                   (owner, self.store.clock(), request_id)).rowcount)

    def identified(self, request_id, thread_id):
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT thread_id FROM app_control_requests WHERE request_id = ?", (request_id,)).fetchone()
            db.execute("UPDATE app_control_requests SET thread_id = ?, updated_at = ? WHERE request_id = ? AND status = 'running'",
                       (thread_id, self.store.clock(), request_id))
            # Normally identified before registration; ensure_thread inherits then.
            # Also cover an already registered child without resetting later edits.
            if previous and previous["thread_id"] is None:
                from .organization import app_agent_creators
                creator = app_agent_creators(db).get(thread_id)
                if creator and creator["mode"] == "orchestration":
                    self.store.threads.organization.inherit_group(db, thread_id, creator["thread_id"])

    def complete(self, request_id, *, result=None, error=None, uncertain=False):
        status = "uncertain" if uncertain else "failed" if error is not None else "completed"
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute("UPDATE app_control_requests SET status = ?, result_json = ?, error = ?, updated_at = ? WHERE request_id = ? AND status = 'running'",
                       (status, json.dumps(result) if result is not None else None, error, self.store.clock(), request_id))
            if changed.rowcount and status in {"failed", "uncertain"}:
                notify_creation_failure(self.store, db, db.execute(
                    "SELECT * FROM app_control_requests WHERE request_id = ?", (request_id,)).fetchone())
            # The message can refresh the timeline before creation is confirmed.
            if changed.rowcount and status == "completed" and isinstance(result, dict) and type(result.get("message_id")) is int:
                db.execute("""INSERT INTO message_changes(sender_session_id, recipient_session_id)
                    SELECT m.sender_session_id, m.recipient_session_id FROM messages m
                    JOIN app_control_requests r ON r.sender_session_id = m.sender_session_id
                        AND r.thread_id = m.recipient_session_id
                    WHERE r.request_id = ? AND r.operation = 'create' AND m.id = ?""",
                    (request_id, result["message_id"]))


class AppControlWorker:
    def __init__(self, sessions):
        self.sessions = sessions
        self.control = AppControl(sessions.store)
        from .event_subscriptions import EventSubscriptions
        self.subscriptions = EventSubscriptions(sessions.store)
        self.owner = str(uuid.uuid4())
        self.stopped = threading.Event()
        self.worker = None
        self.heartbeat = None

    def _heartbeat(self):
        with self.control.store._connection() as db:
            db.execute("INSERT OR REPLACE INTO app_control_runtimes VALUES (?, ?)", (self.owner, self.control.store.clock()))

    def process_once(self):
        self._heartbeat()
        for request in self.control.pending():
            if self.stopped.is_set() or self.sessions.closed:
                return
            if not matches_workspace(request["cwd"], self.sessions.cwd):
                continue
            thread_id = request["thread_id"]
            if request["operation"] in {"settings", "move", "resume"}:
                with self.sessions._thread_lock(thread_id):
                    self.control.settings(thread_id)
                    closed = self.control.store.threads.get(thread_id)["attention"] == "archived"
                    with self.sessions.lock:
                        loaded = thread_id in self.sessions.loaded
                        busy = thread_id in self.sessions.active
                    session = self.control.store.get_session(thread_id)
                    # Do not resume a thread owned by another running app-server.
                    if not closed and not loaded and session["ended_at"] is None:
                        continue
                    if not closed and (busy or session["turn_active"] or
                            (request["operation"] != "resume" and self.sessions.queue.list(thread_id))):
                        continue
                    self._execute(request)
            else:
                self._execute(request)

    def _execute(self, request):
        if not self.control.claim(request["request_id"], self.owner):
            return
        payload = json.loads(request["payload_json"])
        provider_started = False
        try:
            if request["operation"] == "models":
                result = {"client": payload.get("client", "codex"), "models": self.sessions.models(payload.get("client", "codex"))}
            elif request["operation"] == "resume":
                result = self.sessions.inbox_wake.recover(request["thread_id"], payload["expected_turn_id"], request["sender_session_id"])
            elif request["operation"] == "move":
                from .thread_move import move_work_thread
                thread_id = request["thread_id"]
                move_work_thread(self.sessions, thread_id, payload["cwd"], expected_settings=payload["expected_settings"])
                result = {**self.control.settings(thread_id), "project_cwd": payload["cwd"]}
            elif request["operation"] == "settings":
                thread_id = request["thread_id"]
                expected = payload.pop("expected_settings")
                current = self.control.settings(thread_id)
                if any(current[key] != expected[key] for key in SETTINGS):
                    raise CoordinationError("Conversation settings changed after this request was queued. Inspect settings before retrying.")
                with self.control.store._connection() as db:
                    if db.execute("SELECT 1 FROM sqlite_master WHERE name = 'thread_close_requests'").fetchone() and db.execute(
                            "SELECT 1 FROM thread_close_requests WHERE thread_id = ? AND status IN ('queued', 'closing')", (thread_id,)).fetchone():
                        raise CoordinationError("The conversation has a pending close request.")
                self.sessions.read(thread_id)  # Refresh native activity before permissions change.
                self.sessions.update(thread_id, payload)
                result = self.control.settings(thread_id)
            elif request["operation"] != "create":
                raise CoordinationError("Unknown app control operation.")
            else:
                reply_required = payload.pop("reply_required", False)
                subscribe = payload.pop("subscribe", True)
                mode = payload.pop("mode", None)
                # Fail invalid settings before entering the uncertain provider boundary.
                self.sessions._creation_settings(payload)
                provider_started = True
                created = self.sessions.create(payload, on_created=lambda tid: self.control.identified(request["request_id"], tid))
                thread_id = created["session"]["thread_id"]
                self.control.store.threads.capture_request(thread_id, payload["prompt"])
                reporting = (
                    "Return the requested result to the creating agent using agent-coord reply --message-id "
                    "with this message's ID from the delivered header. Include useful artifacts, validation, "
                    "and unresolved limitations. A final answer in this conversation alone does not deliver that reply. "
                    if reply_required else
                    "Answer the user directly in this conversation and accept their follow-ups here. "
                    "This initial request does not require a reply to its sender. "
                )
                if subscribe:
                    reporting += "The runtime will notify the creating agent when this request's turn ends. "
                    if not reply_required:
                        reporting += "No separate completion message is needed. "
                from .context_lifecycle import REPORTING_INSTRUCTIONS
                reporting += REPORTING_INSTRUCTIONS + " "
                body = (reporting +
                          "You are an independent app agent; specialist describes your role. "
                          "Follow repository work rules, release scopes after assignments, and keep this conversation "
                          "available for direct user follow-ups and later assignments. "
                          "Use Agent Coord messaging when coordination is needed.\n\n"
                          "User request:\n" + payload["prompt"])
                # The message cannot wake a fast worker until its subscription
                # is durable. Roll back both if registration fails.
                with self.control.store._connection() as db:
                    db.execute("BEGIN IMMEDIATE")
                    message, _ = self.control.store._insert_message(
                        db, sender_session_id=request["sender_session_id"], recipient_session_id=thread_id,
                        classification="action_required", reply_required=reply_required,
                        thread_id=f"app-agent-request:{request['request_id']}", body=body,
                        now=self.control.store.clock())
                    subscription_id = self.subscriptions.create_for_message(db, message) if subscribe else None
                result = {**self.control.settings(thread_id), "message_id": message["id"],
                          "reply_required": bool(message["reply_required"]), "mode": mode,
                          "subscribe": subscribe, "subscription_id": subscription_id}
            self.control.complete(request["request_id"], result=result)
        except Exception as exc:
            self.control.complete(request["request_id"], error=str(exc), uncertain=provider_started)

    def start(self):
        self.worker = threading.Thread(target=self._run, name="app-control", daemon=True)
        self.heartbeat = threading.Thread(target=self._pulse, name="app-control-heartbeat", daemon=True)
        self.heartbeat.start()
        self.worker.start()

    def _pulse(self):
        while not self.stopped.is_set():
            try:
                self._heartbeat()
            except Exception:
                logging.getLogger(__name__).exception("App control heartbeat failed")
            self.stopped.wait(10)

    def _run(self):
        while not self.stopped.is_set() and not self.sessions.closed:
            try:
                self.process_once()
            except Exception:
                logging.getLogger(__name__).exception("App control request failed")
            self.stopped.wait(.25)

    def close(self):
        self.stopped.set()
        for worker in (self.worker, self.heartbeat):
            if worker is not None:
                worker.join(timeout=5)
        with self.control.store._connection() as db:
            db.execute("DELETE FROM app_control_runtimes WHERE id = ?", (self.owner,))
