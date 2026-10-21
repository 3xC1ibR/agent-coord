"""Local browser conversations, backed by Codex's stdio app-server protocol.

Codex owns conversation history. Our small index associates browser-created
threads with repositories; coordination state continues to live in the store.
"""
from __future__ import annotations

import json
import copy
import os
import shlex
import sqlite3
import subprocess
import threading
import time
import uuid
from collections import deque
from concurrent.futures import Future, TimeoutError
from pathlib import Path
from typing import Any, Callable

from .context import client_environment
from .store import CoordinationError, CoordinationStore
from .browser_queue import BrowserMessageQueue
from .browser_persistence import BrowserPersistence
from .thread_usage import codex_totals
from .app_agent_failures import notify_agent_failure
from .browser_wake import BrowserInboxWake
from .message_timeline import message_timeline
from .timeline_clock import preserve_timing, restore_timing, set_time
from .schedules import ScheduledPrompts
from .claude_code import ClaudeRPC
from .antigravity import AntigravityRPC
from .kimi import KimiRPC
from .muse_code import MuseRPC
from .attention import AttentionClassifier
from .thread_mentions import thread_mentions, provider_message
from .image_inputs import message_images
from .navigation import NavigationStore, window_id
from .organization import app_agent_creators
from . import notification_policy
from .session_close import is_external_codex_session, stop_terminal_session
from .workspaces import matches_workspace, workspace_choices


class BrowserBusyError(CoordinationError):
    """A browser mutation conflicts with an active turn."""


class CodexRPC:
    """Multiplex requests, notifications, and approvals without blocking stdout."""

    def __init__(self, store: CoordinationStore, on_event: Callable, *, command=None):
        self.store = store
        self.on_event = on_event
        self.command = command or ["codex", "app-server"]
        self.process = None
        self.pending: dict[int, Future] = {}
        self.lock = threading.RLock()
        self.start_lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.next_id = 0
        self.readers: list[threading.Thread] = []
        self.errors: deque[str] = deque(maxlen=12)
        self.closed = False
        self.ready = False

    def start(self) -> None:
        with self.start_lock:
            if self.closed:
                raise CoordinationError("Codex connection is closed. Restart the UI.")
            if self.ready:
                return
            if self.process is not None:
                raise CoordinationError("Codex app-server stopped. Restart the UI.")
            env = dict(os.environ)
            for key in list(env):
                if key.startswith("ZELLIJ") or key in {
                    "AGENT_COORD_DELEGATION_ID", "AGENT_COORD_ZELLIJ_WAKE"
                }:
                    env.pop(key)
            env = client_environment(env, self.store.database_path, "codex")
            try:
                self.process = subprocess.Popen(
                    self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                    bufsize=1, env=env, start_new_session=True,
                )
            except OSError as exc:
                raise CoordinationError(f"Cannot start Codex: {exc}") from exc
            self.readers = [
                threading.Thread(target=self._read, daemon=True),
                threading.Thread(target=self._read_errors, daemon=True),
            ]
            for reader in self.readers:
                reader.start()
            try:
                self.request("initialize", {"clientInfo": {
                    "name": "agent_coord_ui", "title": "Ribbon Field", "version": "0.1.0"
                }}, start=False)
                self.write({"method": "initialized", "params": {}})
                self.ready = True
            except Exception:
                self.close()
                raise

    def write(self, message: dict) -> None:
        with self.write_lock:
            if self.closed or self.process is None or self.process.poll() is not None:
                raise CoordinationError("Codex app-server is unavailable. Restart the UI.")
            try:
                self.process.stdin.write(json.dumps(message) + "\n")
                self.process.stdin.flush()
            except (OSError, ValueError) as exc:
                raise CoordinationError("Connection to Codex app-server was lost.") from exc

    def request(self, method: str, params: dict | None = None, *, start=True, timeout=30) -> Any:
        if start:
            self.start()
        future: Future = Future()
        with self.lock:
            self.next_id += 1
            request_id = self.next_id
            self.pending[request_id] = future
        try:
            self.write({"id": request_id, "method": method, "params": params or {}})
            return future.result(timeout=timeout)
        except TimeoutError as exc:
            raise CoordinationError(f"Codex did not respond to {method}; refresh before retrying.") from exc
        finally:
            with self.lock:
                self.pending.pop(request_id, None)

    def _read_errors(self) -> None:
        for line in self.process.stderr:
            self.errors.append(line.rstrip()[-1000:])

    def _read(self) -> None:
        try:
            for line in self.process.stdout:
                try:
                    message = json.loads(line)
                    if not isinstance(message, dict):
                        continue
                    if "method" in message:
                        self.on_event(message)
                    else:
                        with self.lock:
                            future = self.pending.get(message.get("id"))
                            if future and not future.done():
                                if "error" in message:
                                    future.set_exception(CoordinationError(
                                        str(message["error"].get("message", "Codex request failed."))
                                    ))
                                else:
                                    future.set_result(message.get("result"))
                except (ValueError, KeyError, TypeError, CoordinationError, sqlite3.Error) as exc:
                    self.errors.append(str(exc)[-1000:])
        finally:
            self.ready = False
            with self.lock:
                for future in self.pending.values():
                    if not future.done():
                        future.set_exception(CoordinationError("Codex app-server disconnected."))
            try:
                self.on_event({"method": "bridge/disconnected", "params": {}})
            except sqlite3.Error as exc:
                self.errors.append(str(exc)[-1000:])

    def close(self) -> None:
        self.closed = True
        self.ready = False
        if self.process is None:
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        for reader in self.readers:
            if reader is not threading.current_thread():
                reader.join(timeout=3)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            stream.close()


class BrowserSessions:
    def __init__(self, store: CoordinationStore, cwd: str | None = None, *, rpc_factory=CodexRPC, claude_factory=ClaudeRPC, antigravity_factory=AntigravityRPC, kimi_factory=KimiRPC, muse_factory=MuseRPC):
        self.store = store
        self.cwd = str(Path(cwd).expanduser().resolve()) if cwd else None
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.events: deque[dict] = deque(maxlen=1500)
        self.sequence = 0
        self.turn_revisions: dict[str, int] = {}
        self.requests: dict[str, dict] = {}
        self.active: dict[str, str | None] = {}
        self.loaded: set[str] = set()
        self.loaded_clients: dict[str, str] = {}
        self.thread_locks: dict[str, threading.RLock] = {}
        self.starting_notification_context: dict[str, dict] = {}
        self.histories: dict[str, dict] = {}
        self.dirty_histories: set[str] = set()
        self.history_revisions: dict[str, int] = {}
        self.usage_recovery_attempts: set[str] = set()
        self.persistence = BrowserPersistence()
        self.history_lock = threading.Lock()
        self.desktop_project_windows: dict[str, dict] = {}
        self.desktop_project_revision = 0
        self.history_rpc = None
        self.rpc_factory = rpc_factory
        self.closed = False
        with store._connection() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS browser_sessions (
                thread_id TEXT PRIMARY KEY, cwd TEXT NOT NULL, name TEXT NOT NULL,
                model TEXT, effort TEXT, created_at REAL NOT NULL,
                updated_at REAL NOT NULL, archived INTEGER NOT NULL DEFAULT 0
            )""")
            columns = {row[1] for row in connection.execute("PRAGMA table_info(browser_sessions)")}
            if "client" not in columns:
                connection.execute("ALTER TABLE browser_sessions ADD COLUMN client TEXT NOT NULL DEFAULT 'codex'")
            if "yolo" not in columns:
                connection.execute("ALTER TABLE browser_sessions ADD COLUMN yolo INTEGER NOT NULL DEFAULT 0")
            connection.execute("""CREATE TABLE IF NOT EXISTS browser_history (
                thread_id TEXT PRIMARY KEY, history_json TEXT NOT NULL
            )""")
        self.navigation = NavigationStore(store)
        self.queue = BrowserMessageQueue(self)
        self.rpc = rpc_factory(store, self._event)
        self.claude = claude_factory(store, self._event)
        self.antigravity = antigravity_factory(store, self._event)
        self.kimi = kimi_factory(store, self._event)
        self.muse = muse_factory(store, self._event)
        # Upgrade browser conversations created before work threads existed.
        threads = self.store.threads
        with self.store._connection() as connection:
            previous = connection.execute("""SELECT b.*, t.thread_id AS existing_work_thread
                FROM browser_sessions b LEFT JOIN work_threads t ON t.thread_id = b.thread_id""").fetchall()
        for row in previous:
            thread = threads.ensure(row["thread_id"])
            # A routed message can reopen UI placement while the provider is
            # still archived. Only migrate placement for a newly created row.
            if row["archived"] and row["existing_work_thread"] is None:
                threads.update(row["thread_id"], attention="archived")
            if not thread["original_request"]:
                history = self._history(row["thread_id"])
                first = next((item for turn in history.get("turns", []) for item in turn.get("items", []) if item.get("type") == "userMessage"), None)
                if first:
                    self.store.threads.capture_request(row["thread_id"], "\n".join(c.get("text", "") for c in first.get("content", [])))
        self.classifier = AttentionClassifier(self)
        self.schedules = ScheduledPrompts(self)
        self.inbox_wake = BrowserInboxWake(self)
        from .event_subscriptions import EventSubscriptions
        self.event_subscriptions = EventSubscriptions(store, cwd=self.cwd)
        from .dispatch_runtime import DispatchRuntime
        self.dispatch = DispatchRuntime(self)

    def _thread_lock(self, thread_id: str):
        with self.lock:
            return self.thread_locks.setdefault(thread_id, threading.RLock())

    def _record(self, thread_id: str) -> dict:
        with self.store._connection() as connection:
            row = connection.execute(
                "SELECT * FROM browser_sessions WHERE thread_id = ?", (thread_id,)
            ).fetchone()
        if row is None or not matches_workspace(row["cwd"], self.cwd):
            raise CoordinationError("Browser session not found in this workspace.")
        return dict(row)

    def _provider(self, client: str):
        if not isinstance(client, str) or client not in {"codex", "claude", "antigravity", "kimi", "muse"}:
            raise CoordinationError("Choose Codex, Claude Code, Google Antigravity, Kimi Code, or Muse Code as the session provider.")
        return {"codex": self.rpc, "claude": self.claude, "antigravity": self.antigravity, "kimi": self.kimi, "muse": self.muse}[client]

    def _rpc(self, thread_id: str):
        # Saved terminal Codex threads can be read before browser adoption/fork.
        session = self.store.get_session(thread_id)
        if not matches_workspace(session["cwd"], self.cwd):
            raise CoordinationError("Session not found in this workspace.")
        return self._provider(session["client"])

    def _publish(self, method: str, params: dict, **extra) -> None:
        with self.changed:
            self.sequence += 1
            self.events.append({"seq": self.sequence, "method": method, "params": params, **extra})
            self.changed.notify_all()

    def _event(self, message: dict) -> None:
        method, params = message["method"], message.get("params") or {}
        thread_id = params.get("threadId")
        with self.lock:
            if thread_id and method in {"turn/started", "turn/completed", "thread/closed"}:
                self.turn_revisions[thread_id] = self.turn_revisions.get(thread_id, 0) + 1
            if method == "bridge/disconnected":
                for loaded_id in self.loaded:
                    if self.loaded_clients.get(loaded_id) != "codex":
                        continue
                    self.turn_revisions[loaded_id] = self.turn_revisions.get(loaded_id, 0) + 1
            if thread_id in self.loaded:
                self._capture_history(method, params)
            if (method == "item/completed" and params.get("item", {}).get("type") == "userMessage"
                    and self.starting_notification_context.get(thread_id, {}).get("origin") == "user"
                    and params.get("turnId")):
                accepted_turn = params["turnId"]
                self.persistence.submit(lambda: self.store.threads.user_turn_context(thread_id, accepted_turn))
            if "id" in message:
                key = str(uuid.uuid4())
                self.requests[key] = {**message, "key": key, "received_at": self.store.clock()}
                self._publish(method, params, requestKey=key)
                return
            if method == "serverRequest/resolved":
                self.requests = {k: v for k, v in self.requests.items() if v["id"] != params.get("requestId")}
            if method == "turn/started" and thread_id:
                self.active[thread_id] = params["turn"]["id"]
            if method == "turn/completed" and thread_id:
                self.active.pop(thread_id, None)
                self.requests = {k: v for k, v in self.requests.items() if v["params"].get("threadId") != thread_id}
            disconnected = set()
            disconnected_turns = {}
            if method == "thread/closed" and thread_id:
                self._save_history(thread_id)
                disconnected.add(thread_id)
            if method == "bridge/disconnected":
                disconnected = {t for t in self.loaded if self.loaded_clients.get(t) == "codex"}
                for loaded_id in disconnected:
                    self._save_history(loaded_id)
            if disconnected:
                if not self.closed:
                    disconnected_turns = {t: self.active[t] for t in disconnected if self.active.get(t)}
                # This must not depend on a database write succeeding. Otherwise
                # a disconnected turn stays running and blocks idle/deploy gates.
                self.loaded.difference_update(disconnected)
                for identity in disconnected:
                    self.loaded_clients.pop(identity, None)
                self.active = {t: v for t, v in self.active.items() if t not in disconnected}
                self.requests = {k: v for k, v in self.requests.items() if v["params"].get("threadId") not in disconnected}
            self._publish(method, params)
            if method in {"turn/started", "turn/completed", "thread/closed", "bridge/disconnected", "thread/tokenUsage/updated"}:
                saved_params = copy.deepcopy(params)
                saved_params["usage_observed_at"] = self.store.clock()
                if method == "turn/started":
                    saved_params["notification_context"] = self.starting_notification_context.get(thread_id)
                self.persistence.submit(lambda: self._persist_event(method, saved_params, disconnected, disconnected_turns))

    def _persist_event(self, method, params, disconnected, disconnected_turns=None):
        """Idempotent bookkeeping only: never replay a streamed event or prompt."""
        thread_id = params.get("threadId")
        if method == "thread/tokenUsage/updated" and thread_id:
            usage = params.get("tokenUsage") or {}
            # The provider notification's total is cumulative, while last is
            # one model request (not a user turn). Never add either repeatedly.
            if self.store.get_session(thread_id)["client"] == "codex":
                totals = codex_totals(usage.get("total"))
                if totals:
                    self.store.usage.record_snapshot(
                        thread_id, provider="codex", native_id=thread_id,
                        source_key="codex-cumulative", totals=totals, raw=usage.get("total"),
                        coverage="complete", observed_at=params.get("usage_observed_at"))
                last = codex_totals(usage["last"]) if isinstance(usage.get("last"), dict) else {}
                if last or usage.get("modelContextWindow") is not None:
                    self.store.usage.record_context(
                        thread_id, provider="codex", native_id=thread_id,
                        used=last.get("total_tokens"), limit=usage.get("modelContextWindow"),
                        observed_at=params.get("usage_observed_at"))
                self._publish("thread/usage/updated", {"threadId": thread_id,
                              "usage": self.store.usage.summary(thread_id)})
            return
        if hasattr(self, "schedules"):
            self.schedules.event(method, params)
        if method == "turn/started" and thread_id:
            self.store.touch(thread_id, turn_active=True)
            prompt = next(("\n".join(c.get("text", "") for c in item.get("content", []))
                           for item in params["turn"].get("items", []) if item.get("type") == "userMessage"), None)
            self.store.threads.start_turn(thread_id, prompt=prompt, turn_id=params["turn"]["id"],
                                         notification_context=params.get("notification_context"))
        if method == "turn/completed" and thread_id:
            turn = params.get("turn", {})
            self.store.threads.finish_turn(thread_id, turn_id=turn.get("id"), status=turn.get("status", "completed"))
            if turn.get("status") == "failed":
                notify_agent_failure(self.store, thread_id, event_key=["turn", turn.get("id")],
                                     stage="turn", error=turn.get("error"), turn_id=turn.get("id"))
            session = self.store.get_session(thread_id)
            unfinished = session["write_scope"] or session["scope_required"] or session["activity"] in {"implementing", "validating", "planning", "waiting"}
            self.store.touch(thread_id, "waiting" if unfinished else "idle", turn_active=False)
            if turn.get("status") == "completed":
                self.queue.wake()
                self.classifier.wake()
            else:
                self.queue.pause(thread_id, "The turn stopped or failed. Resume queued messages when ready.")
                self.inbox_wake.pause(thread_id, "The turn stopped or failed. Send a message to continue.",
                                     kind="failure" if turn.get("status") == "failed" else "stop")
        for identity in disconnected:
            reason = ("Codex disconnected. Review queued messages before resuming."
                      if method == "bridge/disconnected" else "The thread closed. Reopen it to resume queued messages.")
            self.queue.pause(identity, reason)
            if identity in (disconnected_turns or {}):
                turn_id = disconnected_turns[identity]
                self.store.threads.finish_turn(identity, turn_id=turn_id, status="failed")
                self.inbox_wake.pause(identity, reason)
                notify_agent_failure(self.store, identity, event_key=["turn", turn_id],
                                     stage="disconnect", error=reason, turn_id=turn_id)
            self.store.end_session(identity)

    def _history(self, thread_id: str) -> dict:
        if thread_id not in self.histories:
            with self.store._connection() as connection:
                row = connection.execute("SELECT history_json FROM browser_history WHERE thread_id = ?", (thread_id,)).fetchone()
            self.histories[thread_id] = json.loads(row[0]) if row else {"id": thread_id, "turns": []}
            restore_timing(self.store, self.histories[thread_id])
            if row is None:
                self._dirty_history(thread_id)
        return self.histories[thread_id]

    def _dirty_history(self, thread_id):
        self.dirty_histories.add(thread_id)
        self.history_revisions[thread_id] = self.history_revisions.get(thread_id, 0) + 1

    def _save_history(self, thread_id: str) -> None:
        self._dirty_history(thread_id)
        self.persistence.submit(lambda: self._persist_history(thread_id))

    def _persist_history(self, thread_id):
        with self.lock:
            # Several item completions may queue while SQLite is busy. The first
            # successful retry saves the latest mirror for all of them.
            if thread_id not in self.dirty_histories:
                return
            history = json.dumps(self._history(thread_id))
            revision = self.history_revisions[thread_id]
        with self.store._connection() as connection:
            connection.execute("INSERT OR REPLACE INTO browser_history VALUES (?, ?)", (thread_id, history))
        with self.lock:
            if self.history_revisions[thread_id] == revision:
                self.dirty_histories.discard(thread_id)

    def _remember_thread(self, thread: dict) -> dict:
        with self.lock:
            saved = self._history(thread["id"])
            preserve_timing(thread, saved)
            # Recover timing only for previously unseen history, not on every poll.
            known_turns = {turn.get("id") for turn in saved.get("turns", [])}
            if any(turn.get("id") not in known_turns for turn in thread.get("turns", [])):
                restore_timing(self.store, thread)
            for key, value in thread.items():
                if key != "turns" and (key not in saved or saved[key] != value):
                    saved[key] = copy.deepcopy(value)
                    self._dirty_history(thread["id"])
            if thread.get("turns"):
                if saved.get("turns") != thread["turns"]:
                    saved["turns"] = copy.deepcopy(thread["turns"])
                    self._dirty_history(thread["id"])
            elif thread.get("status", {}).get("type") in {"idle", "notLoaded"} and thread["id"] not in self.active:
                for turn in saved.get("turns", []):
                    if turn.get("status") == "inProgress":
                        turn["status"] = "interrupted"
                        self._dirty_history(thread["id"])
            # Reads still reconcile provider history, but an unchanged transcript
            # need not be copied into the mirror or rewritten to SQLite.
            if thread["id"] in self.dirty_histories:
                self._save_history(thread["id"])
            return copy.deepcopy(saved)

    def _capture_history(self, method: str, params: dict) -> None:
        """Keep a display mirror for Codex builds without stored-turn queries."""
        if not method.startswith(("turn/", "item/")):
            return
        thread = self._history(params["threadId"])
        incoming_turn = params.get("turn", {})
        turn_id = params.get("turnId") or incoming_turn.get("id")
        if not turn_id:
            return
        self._dirty_history(params["threadId"])
        turns = thread.setdefault("turns", [])
        turn = next((t for t in turns if t["id"] == turn_id), None)
        if turn is None:
            turn = {"id": turn_id, "items": [], "status": "inProgress"}
            turns.append(turn)
        now = self.store.clock()
        if method == "turn/started":
            set_time(incoming_turn, "startedAt", turn.get("startedAt") or now)
        if method == "turn/completed":
            set_time(incoming_turn, "completedAt", turn.get("completedAt") or now)
        if turn.get("startedAt") is not None:
            set_time(incoming_turn, "startedAt", turn["startedAt"])
        turn.update({key: value for key, value in incoming_turn.items() if key != "items"})
        params["turnStartedAt"] = turn.get("startedAt")
        items = list(incoming_turn.get("items") or [])
        if params.get("item"):
            items.append(params["item"])
        for item in items:
            existing = next((i for i in turn["items"] if i["id"] == item["id"]), None)
            set_time(item, "timelineAt", (existing or {}).get("timelineAt") or now)
            if existing is None:
                turn["items"].append(copy.deepcopy(item))
                self._record_item_tool(params["threadId"], item)
            else:
                existing.update(copy.deepcopy(item))
        item = next((i for i in turn["items"] if i["id"] == params.get("itemId")), None)
        if item and isinstance(params.get("delta"), str):
            field = {"item/agentMessage/delta": "text", "item/commandExecution/outputDelta": "aggregatedOutput"}.get(method)
            if field:
                item[field] = item.get(field, "") + params["delta"]
        if method in {"turn/started", "turn/completed"}:
            thread["status"] = {"type": "active" if method == "turn/started" else "idle"}
        if method in {"item/completed", "turn/completed"}:
            self._save_history(params["threadId"])

    TOOL_ITEMS = {"commandExecution": "run", "fileChange": "edit", "mcpToolCall": "tool", "webSearch": "read"}

    def _record_item_tool(self, thread_id: str, item: dict) -> None:
        """Remember what a Codex turn is doing from its streamed items; best effort."""
        kind = self.TOOL_ITEMS.get(item.get("type")) if isinstance(item, dict) else None
        if not kind:
            return
        try:
            if item["type"] == "commandExecution":
                command = item.get("command")
                label = " ".join(str(part) for part in command) if isinstance(command, list) else str(command or "")
            elif item["type"] == "fileChange":
                changes = item.get("changes") or []
                label = ", ".join(str(change.get("path")) for change in changes[:3]
                                  if isinstance(change, dict) and change.get("path"))
            elif item["type"] == "mcpToolCall":
                label = ".".join(str(item.get(key)) for key in ("server", "tool") if item.get(key))
            else:
                label = str(item.get("query") or "")
            item_type = item["type"]
            self.persistence.submit(lambda: self.store.record_tool(thread_id, item_type, label, kind=kind))
        except Exception:  # noqa: BLE001 -- activity must never break history capture.
            return

    @staticmethod
    def _query_thread(rpc: CodexRPC, thread_id: str, *, include_turns: bool) -> dict:
        # Codex can acknowledge the first turn after creating the rollout but
        # before writing its metadata. Retry only that read failure, for at most
        # 750 ms of backoff; never replay the accepted turn or mask a lasting error.
        for delay in (0.05, 0.1, 0.2, 0.4, None):
            try:
                return rpc.request("thread/read", {"threadId": thread_id, "includeTurns": include_turns})["thread"]
            except CoordinationError as exc:
                message = str(exc)
                empty_rollout = (message.startswith("failed to read thread:")
                                 and "failed to read session metadata " in message
                                 and ": rollout at " in message and message.endswith(" is empty"))
                if delay is None or not empty_rollout:
                    raise
                time.sleep(delay)

    def _stored_thread(self, thread_id: str) -> dict:
        # Some Codex builds cannot list turns for a loaded paginated thread,
        # but can read its persisted history on a connection that never resumes it.
        with self.history_lock:
            if self.closed:
                raise CoordinationError("Browser sessions are closed.")
            if self.history_rpc is None:
                self.history_rpc = self.rpc_factory(self.store, lambda event: None)
            try:
                return self._query_thread(self.history_rpc, thread_id, include_turns=True)
            except CoordinationError:
                self.history_rpc.close()
                self.history_rpc = None
                raise

    def _read_thread(self, thread_id: str) -> dict:
        try:
            thread = self._query_thread(self._rpc(thread_id), thread_id, include_turns=True)
        except CoordinationError as exc:
            if "list_turns is not supported" not in str(exc):
                raise
            # Failure is local to this read, not a server-wide capability switch.
            thread = self._query_thread(self._rpc(thread_id), thread_id, include_turns=False)
            try:
                stored = self._stored_thread(thread_id)
            except CoordinationError:
                thread["historyUnavailable"] = True
                return self._remember_thread(thread)
            with self.lock:
                turns = copy.deepcopy(stored.get("turns", []))
                by_id = {turn["id"]: turn for turn in turns}
                for cached in self._history(thread_id).get("turns", []):
                    persisted = by_id.get(cached["id"])
                    if persisted is None:
                        turns.append(copy.deepcopy(cached))
                    elif persisted.get("status") == "inProgress" or self.active.get(thread_id) == cached["id"]:
                        # Disk can lag behind a streamed item or completion.
                        items = {item["id"]: item for item in persisted.get("items", [])}
                        items.update({item["id"]: copy.deepcopy(item) for item in cached.get("items", [])})
                        persisted.update(copy.deepcopy(cached))
                        persisted["items"] = list(items.values())
                thread["turns"] = turns
                thread["historyUnavailable"] = False
                return self._remember_thread(thread)
        thread["historyUnavailable"] = False
        return self._remember_thread(thread)

    def events_after(self, sequence: int, timeout=15) -> dict:
        with self.changed:
            if self.sequence <= sequence and not self.closed:
                self.changed.wait(timeout)
            reset = sequence > self.sequence or bool(self.events and sequence < self.events[0]["seq"] - 1)
            return {"seq": self.sequence, "reset": reset, "events": [e for e in self.events if e["seq"] > sequence]}

    def completions_after(self, sequence: int) -> dict:
        batch = self.store.threads.completions_after(sequence)
        batch["events"] = [event for event in batch["events"]
                           if event["attention"] != "archived" and matches_workspace(event["cwd"], self.cwd)
                           and not (event["snoozed_until"] and event["snoozed_until"] > self.store.clock())
                           and self.completion_notification_allowed(event["id"])]
        return batch

    def completion_notification_allowed(self, completion_id: int) -> bool:
        if type(completion_id) is not int or completion_id <= 0:
            raise CoordinationError("Choose a valid turn completion.")
        with self.store._connection() as db:
            row = db.execute("SELECT * FROM turn_completions WHERE id = ?", (completion_id,)).fetchone()
            context = db.execute("SELECT * FROM turn_notification_context WHERE thread_id = ? AND turn_key = ?",
                                 (row["thread_id"], row["turn_key"])).fetchone() if row else None
            checkpoint = db.execute("SELECT * FROM thread_checkpoints WHERE id = ?", (context["checkpoint_id"],)).fetchone() if context else None
            owners = notification_policy.available_owners(db, self.store)
            automatic = bool(row and db.execute("SELECT 1 FROM messages WHERE recipient_session_id = ? AND wake_turn_id = ? LIMIT 1",
                                                (row["thread_id"], row["turn_key"])).fetchone())
        if row is None:
            raise CoordinationError("Turn completion not found.")
        thread = self.work_thread(row["thread_id"], history=False)
        if (context and context["origin"] == "coordination" and checkpoint and checkpoint["next_actor"] == "user"
                and (thread["turn_key"] != row["turn_key"] or
                     row["id"] <= thread["handled_completion_id"] and checkpoint["id"] <= thread["handled_checkpoint_id"])):
            return False  # The user already answered or handled this escalation.
        # Evaluate the completed turn, not a newer user follow-up or checkpoint.
        if context:
            thread.update(notification_context=dict(context), checkpoint=dict(checkpoint) if checkpoint else None,
                          checkpoint_stale=False)
        elif thread["turn_key"] != row["turn_key"]:
            thread.update(notification_context=None, checkpoint=None, checkpoint_stale=True)
        thread["turn_completion"] = dict(row)
        thread["automatic_turn"] = automatic
        policy = notification_policy.decide(thread, owners=owners)
        if (context and context["origin"] == "coordination" and context["owner_thread_id"] == row["thread_id"]
                and policy["needs_user"]):
            # An automatic parent summary of live child needs shares their
            # existing alerts. Distinct provider requests still have their own keys.
            grouped = next((t for t in self.list_work_threads() if t["thread_id"] == row["thread_id"]), {})
            if any(item["thread_id"] != row["thread_id"] for item in grouped.get("attention_items", [])):
                return False
        return thread["attention"] != "archived" and not thread["snoozed"] and policy["notify"]

    def claim_notification(self, completion_id: int) -> bool:
        return self.completion_notification_allowed(completion_id) and self.store.threads.claim_notification(completion_id)

    def snooze_notifications(self, *, include_claimed: bool = False) -> list[dict]:
        return [event for event in self.store.threads.snooze_notifications(include_claimed=include_claimed)
                if matches_workspace(event["cwd"], self.cwd)]

    def claim_snooze_notification(self, notification_id: int) -> bool:
        if type(notification_id) is not int or notification_id <= 0:
            raise CoordinationError("Choose a valid snooze notification.")
        return (any(event["id"] == notification_id for event in self.snooze_notifications())
                and self.store.threads.claim_snooze_notification(notification_id))

    @staticmethod
    def _approval_request(request: dict) -> bool:
        return request["method"] in {
            "item/commandExecution/requestApproval", "item/fileChange/requestApproval",
            "item/permissions/requestApproval",
        }

    @classmethod
    def _input_request(cls, request: dict) -> bool:
        return cls._approval_request(request) or request["method"] in {
            "item/tool/requestUserInput", "mcpServer/elicitation/request",
        }

    def approval_notifications(self, *, include_claimed: bool = False) -> list[dict]:
        """Include still-pending approvals on fresh streams and reconnects."""
        with self.lock:
            notifications = []
            for key, request in self.requests.items():
                if not self._input_request(request) or (request.get("notification_claimed") and not include_claimed):
                    continue
                try:
                    thread_id = request["params"].get("threadId")
                    self._record(thread_id)
                    thread = self.store.threads.get(thread_id, history=False)
                except CoordinationError:
                    continue
                if thread["attention"] != "archived" and not thread["snoozed"]:
                    notifications.append({"request_key": key, "thread_id": thread_id,
                                          "status": "approval" if self._approval_request(request) else "question",
                                          "client": self._record(thread_id)["client"],
                                          "title": thread["title"], "project_name": thread["project_name"],
                                          "repository_name": thread["repository_name"]})
            return notifications

    def claim_approval_notification(self, key: str) -> bool:
        """Atomically claim a live approval across tabs without answering it."""
        if not isinstance(key, str) or not key.strip():
            raise CoordinationError("Choose a valid approval request key.")
        with self.lock:
            request = self.requests.get(key)
            if not request or not self._input_request(request) or request.get("notification_claimed"):
                return False
            thread_id = request["params"].get("threadId")
            self._record(thread_id)
            thread = self.store.threads.get(thread_id, history=False)
            if thread["attention"] == "archived" or thread["snoozed"]:
                return False
            request["notification_claimed"] = True
            return True

    def list_workspaces(self) -> list[dict]:
        with self.store._connection() as connection:
            known = [row[0] for row in connection.execute(
                "SELECT DISTINCT cwd FROM sessions UNION SELECT root FROM work_projects"
            )]
        return workspace_choices(self.cwd or os.getcwd(), known, workspace=self.cwd)

    def desktop_projects(self) -> dict:
        """Return the live union of project tabs reported by native windows."""
        with self.lock:
            projects: list[str] = []
            active = ""
            for window in sorted(self.desktop_project_windows.values(), key=lambda item: item["revision"]):
                for path in window["projects"]:
                    if path not in projects:
                        projects.append(path)
                if window["active"] in window["projects"]:
                    active = window["active"]
            if active not in projects:
                active = projects[0] if projects else ""
            return {"projects": projects, "active": active}

    def update_desktop_projects(self, window_id: str, projects: list[str], active: str) -> dict:
        """Replace one native window's project tabs and return their live union."""
        if not isinstance(window_id, str) or not 0 < len(window_id) <= 200:
            raise CoordinationError("Choose a valid desktop window.")
        if not isinstance(projects, list) or len(projects) > 128 or any(
            not isinstance(path, str) or not path for path in projects
        ):
            raise CoordinationError("Desktop projects must be a list of directory paths.")
        projects = list(dict.fromkeys(projects))
        if not isinstance(active, str) or (active and active not in projects):
            raise CoordinationError("The active desktop project must be one of its open tabs.")
        with self.lock:
            self.desktop_project_revision += 1
            if projects:
                self.desktop_project_windows[window_id] = {
                    "projects": projects, "active": active, "revision": self.desktop_project_revision,
                }
            else:
                self.desktop_project_windows.pop(window_id, None)
            return self.desktop_projects()

    def list_sessions(self, *, archived=False) -> list[dict]:
        with self.store._connection() as connection:
            rows = connection.execute(
                """SELECT b.* FROM browser_sessions b JOIN work_threads t ON t.thread_id = b.thread_id
                   WHERE (t.attention = 'archived') = ? ORDER BY b.updated_at DESC""",
                (int(archived),),
            ).fetchall()
        with self.lock:
            waiting = {r["params"].get("threadId") for r in self.requests.values()}
            return [{**dict(row), "status": "needs input" if row["thread_id"] in waiting else
                     "running" if row["thread_id"] in self.active else
                     "idle" if row["thread_id"] in self.loaded else "saved"}
                    for row in rows if matches_workspace(row["cwd"], self.cwd)]

    def pending_requests(self, thread_id: str) -> list[dict]:
        self._record(thread_id)
        with self.lock:
            return [{k: v for k, v in request.items() if k not in {"id", "notification_claimed"}}
                    for request in self.requests.values() if request["params"].get("threadId") == thread_id]

    def _app_agent_creators(self, *, db=None) -> dict:
        if db is None:
            with self.store._connection() as connection:
                creators = app_agent_creators(connection)
        else:
            creators = app_agent_creators(db)
        for creator in creators.values():
            cwd = creator.pop("cwd")
            creator["available"] = bool(cwd and matches_workspace(cwd, self.cwd))
        return creators

    def work_thread(self, thread_id: str, *, history=True, _creators=None, _inventory=None) -> dict:
        thread = (self.store.threads.get(thread_id, history=history) if _inventory is None
                  else _inventory.projected[thread_id])
        if not matches_workspace(thread["cwd"], self.cwd):
            raise CoordinationError("Work thread not found in this workspace.")
        creators = self._app_agent_creators() if _creators is None else _creators
        thread["created_by"] = creators.get(thread_id)
        thread["created_by_thread_id"] = thread["created_by"]["thread_id"] if thread["created_by"] else None
        thread["creation_mode"] = thread["created_by"]["mode"] if thread["created_by"] else None
        if _inventory is None:
            with self.store._connection() as db:
                thread["browser_session"] = db.execute("SELECT thread_id FROM browser_sessions WHERE thread_id = ?", (thread_id,)).fetchone() is not None
                owners = notification_policy.available_owners(db, self.store)
            thread["dispatcher"] = self.dispatch.settings(thread_id)["enabled"] if hasattr(self, "dispatch") else False
            session = self.store.get_session(thread_id)
        else:
            owners = _inventory.notification_owners
            thread["browser_session"] = thread_id in _inventory.browser
            thread["dispatcher"] = thread_id in _inventory.dispatchers
            session = _inventory.sessions[thread_id]
        thread["last_tool"] = session.get("last_tool")
        with self.lock:
            waiting = {key: r for key, r in self.requests.items() if r["params"].get("threadId") == thread_id}
            # Loading provider context does not establish idleness. A live hook
            # heartbeat can precede this runtime's turn notification.
            running = thread_id in self.active or (session["turn_active"] and session["presence"] == "online")
            thread["status"] = ("needs input" if waiting else "running" if running
                                else "idle" if thread_id in self.loaded else "saved" if session["presence"] == "offline"
                                else session["presence"])
        checkpoint = thread["checkpoint"] if not thread["checkpoint_stale"] else None
        completion = thread["turn_completion"]
        classification = thread["response_classification"]
        handoff = classification["choice"] if classification and classification["status"] == "classified" else None
        checkpoint_input = bool(checkpoint and checkpoint["next_actor"] == "user" and checkpoint["next_action"])
        unhandled_input = checkpoint_input and checkpoint["id"] > thread["handled_checkpoint_id"]
        if (handoff is None and (not classification or classification["status"] != "uncertain")
                and checkpoint and checkpoint["phase"] == "finished" and checkpoint["next_actor"] == "nobody"
                and thread["work_phase"] == "finished" and completion and completion["status"] == "completed"):
            handoff = "done"
        # Input and explicit handoffs are authoritative. A missing, uncertain,
        # or stale classification keeps the response visible for consideration.
        thread["response_state"] = (
            "input" if waiting else "working" if thread["status"] == "running"
            else "input" if checkpoint_input
            else "interrupted" if completion and completion["status"] == "interrupted"
            else "failed" if completion and completion["status"] == "failed" and thread["unhandled_response"]
            else "update" if thread["unhandled_response"] and handoff in {"update", "findings", "done"} and thread["unread_result"]
            else "available" if thread["unhandled_response"] and handoff in {"update", "findings", "done"}
            else "action" if handoff == "blocked" or thread["unhandled_response"] and handoff == "review"
            else "reply" if thread["unhandled_response"]
            else "available" if completion or (checkpoint and not thread["turn_key"]) else None)
        # Later preserves placement, but a new result or question can still
        # need attention. A timed deferral suppresses both until its reminder.
        deferred_at = thread.get("deferred_at")
        new_later_activity = (thread["attention"] == "later" and not thread["snoozed"]
                              and deferred_at is not None and (
                                  (completion and completion["completed_at"] > deferred_at)
                                  or (checkpoint and checkpoint["created_at"] > deferred_at)
                                  or any(r.get("received_at", 0) > deferred_at for r in waiting.values())))
        # Acknowledging a saved blocker clears attention without resolving it.
        # Live provider requests stay pending independently of response receipts.
        pending_response = bool(thread["unhandled_response"] or unhandled_input)
        thread["needs_attention"] = ((thread["attention"] == "now" or new_later_activity) and
                                     (thread["snooze_due"] or bool(waiting) or pending_response and
                                      thread["response_state"] in {"input", "action", "reply", "failed", "update"}))
        input_reason = handoff if handoff in {"blocked", "review"} else (
            "review" if thread["work_phase"] in {"investigation", "planning", "validation"} else "blocked")
        thread["attention_reason"] = (
            "blocked" if waiting or thread["response_state"] == "failed"
            else input_reason if thread["response_state"] == "input"
            else handoff or "reply" if thread["response_state"] in {"action", "reply", "update"}
            else "snooze" if thread["snooze_due"] else None)
        # Queue age describes the outstanding request/result, never metadata edits
        # such as reading, renaming, pinning, or a checkpoint heartbeat.
        thread["attention_since"] = thread["attention_key"] = None
        if thread["needs_attention"]:
            if waiting:
                thread["attention_since"] = min(r.get("received_at", thread["created_at"]) for r in waiting.values())
                thread["attention_key"] = "requests:" + ",".join(sorted(waiting))
            elif checkpoint and thread["response_state"] == "input":
                thread["attention_since"] = checkpoint["created_at"]
                thread["attention_key"] = f"checkpoint:{checkpoint['id']}"
            elif thread["snooze_due"]:
                thread["attention_since"] = thread["snoozed_until"]
                thread["attention_key"] = f"snooze:{thread['snoozed_until']}"
            elif completion:
                thread["attention_since"] = max(completion["completed_at"], checkpoint["created_at"] if checkpoint else 0)
                thread["attention_key"] = f"completion:{completion['id']}:checkpoint:{checkpoint['id'] if checkpoint else 0}"
            elif checkpoint:
                thread["attention_since"] = checkpoint["created_at"]
                thread["attention_key"] = f"checkpoint:{checkpoint['id']}"
        thread["can_handle_response"] = (thread["attention"] != "archived" and pending_response
                                         and thread["status"] not in {"running", "needs input"}
                                         and thread["response_state"] in {"input", "action", "reply", "failed", "update"})
        policy = notification_policy.decide(thread, waiting=bool(waiting), owners=owners)
        thread["attention_owner_thread_id"] = policy["owner_thread_id"]
        thread["attention_action"] = ("Approval or answer needed" if waiting else
                                      checkpoint["next_action"] if checkpoint_input else
                                      "Review the failed agent" if thread["response_state"] == "failed" else "")
        if policy["quiet"] and not thread["snooze_due"]:
            thread.update(needs_attention=False, attention_reason=None, attention_key=None, attention_since=None,
                          can_handle_response=False)
        thread["can_resume"] = bool(not thread["browser_session"] and thread["client"] == "codex" and session["presence"] == "offline")
        fork_supported = thread["client"] == "codex" or (thread["client"] == "claude" and thread["browser_session"])
        thread["can_fork"] = bool(fork_supported and thread["attention"] != "archived"
                                  and thread["status"] in {"idle", "saved"}
                                  and (thread["browser_session"] or session["presence"] == "offline"))
        thread["forked_from"] = None
        if thread["forked_from_thread_id"]:
            source_id = thread["forked_from_thread_id"]
            source_title = (self.store.threads.get(source_id)["title"] if _inventory is None
                            else _inventory.titles[source_id])
            thread["forked_from"] = {"thread_id": source_id, "title": source_title}
        if _inventory is None:
            with self.store._connection() as db:
                thread["forked_sessions"] = [dict(row) for row in db.execute(
                    """SELECT thread_id, title, created_at, forked_from_turn_id FROM work_threads
                       WHERE forked_from_thread_id = ? ORDER BY created_at, thread_id""", (thread_id,))]
        else:
            thread["forked_sessions"] = _inventory.forks[thread_id]
        if history:
            usage = self.store.usage.summary(thread_id)
            # Detail-only, bounded, no-model recovery. Durable notes prevent
            # repeated failed scans after restart; overview never reads logs.
            if usage["observed_at"] is None and thread_id not in self.usage_recovery_attempts:
                self.usage_recovery_attempts.add(thread_id)
                try:
                    usage = self.store.usage.backfill(thread_id)
                except Exception:
                    # An adapter can be unavailable while plugin versions are
                    # changing; usage must never prevent opening a conversation.
                    pass
            thread["usage"] = usage
            with self.store._connection() as db:
                children = db.execute("SELECT DISTINCT d.child_session_id FROM delegations d JOIN work_threads t ON t.thread_id = d.child_session_id WHERE d.parent_session_id = ?", (thread_id,)).fetchall()
            thread["children"] = [self.store.threads.get(row["child_session_id"]) for row in children]
        return thread

    def list_work_threads(self, *, archived=False) -> list[dict]:
        from .thread_inventory import ThreadInventory
        with self.store._connection() as db:
            inventory = ThreadInventory(self.store.threads, db, archived=archived)
            inventory.load_runtime(db)
            inventory.creators = self._app_agent_creators(db=db)
            inventory.projected = {row["thread_id"]: inventory.read(db, row) for row in inventory.rows
                                   if row["thread_id"] not in inventory.delegated and matches_workspace(row["cwd"], self.cwd)}
        return notification_policy.group_attention([
            self.work_thread(thread_id, history=False, _creators=inventory.creators, _inventory=inventory)
            for thread_id in inventory.projected])

    def update_work_thread(self, thread_id: str, body: dict) -> dict:
        with self._thread_lock(thread_id):
            return self._update_work_thread(thread_id, body)

    def _update_work_thread(self, thread_id: str, body: dict) -> dict:
        thread = self.work_thread(thread_id)
        if set(body) - {"attention", "title", "domain", "pinned", "seen", "seen_checkpoint_id", "seen_completion_id",
                        "handled", "handled_checkpoint_id", "handled_completion_id", "repository_id", "project_id",
                        "snoozed_until", "resume_snooze"}:
            raise CoordinationError("Thread update accepts attention, title, domain, pinned, read and handled markers, snoozed_until, resume_snooze, repository_id, and project_id.")
        associations = {key: body[key] for key in ("repository_id", "project_id") if key in body}
        self.store.threads.organization.validate_assignments(**associations)
        if "attention" in body and not isinstance(body["attention"], str):
            raise CoordinationError("Attention must be now, later, or archived.")
        if "seen" in body and not isinstance(body["seen"], bool):
            raise CoordinationError("Seen must be true or false.")
        if "handled" in body and not isinstance(body["handled"], bool):
            raise CoordinationError("Handled must be true or false.")
        if body.get("handled") and thread["status"] in {"running", "needs input"}:
            raise BrowserBusyError("Answer or cancel live input and wait for the running turn before dismissing its response.")
        if "pinned" in body and not isinstance(body["pinned"], bool):
            raise CoordinationError("Pinned must be true or false.")
        if "title" in body:
            self._text(body["title"], "Thread title", 160)
        if body.get("attention") is not None and body["attention"] not in {"now", "later", "archived"}:
            raise CoordinationError("Attention must be now, later, or archived.")
        if body.get("attention") == "archived" and thread["status"] in {"running", "needs input"}:
            raise BrowserBusyError("Stop the running turn before archiving this thread.")
        if thread["browser_session"] and body.get("attention") in {"now", "later"} and self._record(thread_id)["archived"]:
            self.update(thread_id, {"archived": False})
        self.store.threads.update(thread_id, title=body.get("title"), attention=body.get("attention"), seen=body.get("seen", False),
                                  seen_checkpoint_id=body.get("seen_checkpoint_id"), seen_completion_id=body.get("seen_completion_id"),
                                  handled=body.get("handled", False), handled_checkpoint_id=body.get("handled_checkpoint_id"),
                                  handled_completion_id=body.get("handled_completion_id"),
                                  **({"domain": body["domain"]} if "domain" in body else {}),
                                  **{key: body[key] for key in ("snoozed_until", "resume_snooze") if key in body},
                                  **({"pinned": body["pinned"]} if "pinned" in body else {}), **associations)
        self._publish("browser/changed", {"threadId": thread_id})
        return self.work_thread(thread_id)

    def close_work_thread(self, thread_id: str, *, stop_timeout: float = 10) -> dict:
        """Stop owned execution, or hide an externally owned conversation.

        The persisted `archived` value remains the compatibility representation
        of Closed. A closed thread is retained, not marked task-complete.
        """
        with self._thread_lock(thread_id):
            thread = self.work_thread(thread_id)
            external = False
            if thread["browser_session"]:
                record = self._record(thread_id)
                self.queue.pause(thread_id, "The thread closed. Reopen it to resume queued messages.")
                with self.lock:
                    running = thread_id in self.active
                if running:
                    self.interrupt(thread_id)
                    with self.changed:
                        if not self.changed.wait_for(lambda: thread_id not in self.active, timeout=stop_timeout):
                            raise BrowserBusyError("The turn is still stopping. Retry Close when it has stopped.")
                if not record["archived"]:
                    # Codex archives the rollout and unloads this thread; the
                    # shared app-server stays available to other conversations.
                    self._rpc(thread_id).request("thread/archive", {"threadId": thread_id})
                    with self.store._connection() as db:
                        db.execute("UPDATE browser_sessions SET archived = 1 WHERE thread_id = ?", (thread_id,))
                with self.lock:
                    self._save_history(thread_id)
                    self.loaded.discard(thread_id)
                    self.active.pop(thread_id, None)
                    self.requests = {k: v for k, v in self.requests.items() if v["params"].get("threadId") != thread_id}
            else:
                session = self.store.get_session(thread_id)
                external = is_external_codex_session(session)
                if not external:
                    stop_terminal_session(session)
                if thread["turn_active"] and not external:
                    self.store.threads.finish_turn(thread_id, turn_id=thread["turn_id"], status="interrupted")
            if external:
                # This runtime does not own the provider's execution. Preserve
                # live activity and reservations, and do not invent a completion.
                self.store.disable_wake(thread_id)
                self.store.threads.update(thread_id, attention="archived")
            else:
                from .thread_control import finish_close
                finish_close(self.store, thread_id)
            self._publish("browser/changed", {"threadId": thread_id})
            closed = self.work_thread(thread_id)
            if external:
                closed["close_notice"] = "Thread closed in Ribbon Field. Work may continue in Codex."
            return closed

    def reopen_work_thread(self, thread_id: str) -> dict:
        with self._thread_lock(thread_id):
            thread = self.work_thread(thread_id)
            if thread["attention"] != "archived":
                return thread
            if thread["browser_session"] and self._record(thread_id)["archived"]:
                self.update(thread_id, {"archived": False})
            else:
                self.store.threads.update(thread_id, attention="now")
            self._publish("browser/changed", {"threadId": thread_id})
            return self.work_thread(thread_id)

    def checkpoint_work_thread(self, thread_id: str, body: dict) -> dict:
        self.work_thread(thread_id)
        self.store.threads.checkpoint(thread_id, body, author="user")
        self.store.threads.update(thread_id, seen=True)
        self._publish("browser/changed", {"threadId": thread_id})
        return self.work_thread(thread_id)

    def link_work_thread(self, thread_id: str, body: dict) -> dict:
        self.work_thread(thread_id)
        if set(body) == {"remove"}:
            if type(body["remove"]) is not int:
                raise CoordinationError("Choose a link to remove.")
            self.store.threads.remove_link(thread_id, body["remove"])
        else:
            self.store.threads.add_link(thread_id, body)
        self._publish("browser/changed", {"threadId": thread_id})
        return self.work_thread(thread_id)

    def resume_work_thread(self, thread_id: str) -> dict:
        with self._thread_lock(thread_id):
            thread = self.work_thread(thread_id)
            if not thread["can_resume"] or thread["attention"] == "archived":
                raise CoordinationError("Reopen the thread and close its terminal session before continuing in the browser.")
            record = {"thread_id": thread_id, "cwd": thread["cwd"], "name": thread["title"]}
            result = self.rpc.request("thread/resume", {"threadId": thread_id, "excludeTurns": True, **self._options(record)})
            with self.store._connection() as db:
                now = self.store.clock()
                db.execute("INSERT INTO browser_sessions (thread_id, cwd, name, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                           (thread_id, thread["cwd"], thread["title"], now, now))
            self._capture_settings(thread_id, result)
            self.store.register(session_id=thread_id, client="codex", cwd=thread["cwd"], name=thread["title"])
            with self.lock:
                self.loaded.add(thread_id)
                self.loaded_clients[thread_id] = "codex"
                self._remember_thread(result["thread"])
            self._publish("browser/changed", {"threadId": thread_id})
            return self.work_thread(thread_id)

    def fork_work_thread(self, thread_id: str) -> dict:
        with self._thread_lock(thread_id):
            source = self.work_thread(thread_id)
            if source["status"] in {"running", "needs input"}:
                raise BrowserBusyError("Wait for the running turn and pending input to finish before forking.")
            if not source["can_fork"]:
                raise CoordinationError("Fork requires an open, idle Codex or Claude app thread, or a saved Codex terminal thread.")
            # Read without resuming or taking ownership of the source. Also check
            # native state, which may have advanced since our last notification.
            original = self._read_thread(thread_id)
            turns = original.get("turns", [])
            latest = turns[-1] if turns else {}
            if original.get("status", {}).get("type") == "active" or latest.get("status") == "inProgress":
                raise BrowserBusyError("Wait for the running turn to finish before forking.")
            name = "Fork of " + source["title"][:152]
            record = {"cwd": source["cwd"], "name": name, "yolo": False}
            if source["browser_session"]:
                settings = self._record(thread_id)
                record.update({key: settings[key] for key in ("model", "effort", "yolo")})
            params = {"threadId": thread_id, **self._options(record)}
            if latest.get("id"):
                params["lastTurnId"] = latest["id"]
            baseline = None
            if source["client"] == "codex":
                try:
                    # Freeze *raw* native counters before the fork. A parent's
                    # later total is never a valid child baseline, and nested
                    # forks inherit the full raw ancestry, not adjusted totals.
                    # The native turn can already be idle while its last usage
                    # event is queued behind SQLite contention. Freeze only
                    # after prior bookkeeping drains; a stale immutable baseline
                    # would charge inherited tokens to the child. If the bounded
                    # wait expires, leave it unknown for native-prefix recovery.
                    if self.persistence.flush(timeout=1):
                        self.store.usage.backfill(thread_id)
                        with self.lock:
                            # Do not wait holding the event lock: a worker may
                            # need it to publish its durable usage notification.
                            # Newly queued work during backfill also fails closed.
                            if self.persistence.flush(timeout=0):
                                baseline = self.store.usage.raw_snapshot(thread_id, provider="codex", native_id=thread_id)
                except Exception:
                    pass
            result = self._rpc(thread_id).request("thread/fork", params)
            child_id = result["thread"]["id"]
            # thread.sessionId may identify the shared Codex tree root. Only the
            # new thread.id is an independent Agent Coord checkpoint identity.
            self.store.register(session_id=child_id, client=source["client"], cwd=record["cwd"], name=name)
            self.store.threads.record_fork(child_id, source, turn_id=latest.get("id"))
            if source["client"] == "codex":
                try:
                    self.store.usage.set_baseline(child_id, provider="codex", native_id=child_id, totals=baseline)
                except Exception:
                    # Lineage itself makes absent baseline fail closed.
                    pass
            with self.store._connection() as db:
                now = self.store.clock()
                db.execute("""INSERT INTO browser_sessions
                              (thread_id, cwd, name, model, effort, yolo, created_at, updated_at, client)
                              VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                           (child_id, record["cwd"], name, result.get("model") or record.get("model"),
                            result.get("reasoningEffort", record.get("effort")), int(record["yolo"]), now, now, source["client"]))
            with self.lock:
                self._remember_thread(result["thread"])
            self.store.end_session(child_id)
            # Leave the fork saved locally. Opening/sending uses read() to resume
            # with child-specific instructions before any turn. A failed resume
            # can be retried by opening the same child, without forking again.
            self._publish("browser/changed", {"threadId": child_id})
            return self.work_thread(child_id)

    def models(self, client: str = "codex") -> list[dict]:
        models, cursor = [], None
        while True:
            result = self._provider(client).request("model/list", {"limit": 100, **({"cursor": cursor} if cursor else {})})
            models.extend(result["data"])
            cursor = result.get("nextCursor")
            if not cursor:
                return models

    def _options(self, record: dict) -> dict:
        yolo = bool(record.get("yolo"))
        options: dict = {
            "cwd": record["cwd"], "approvalPolicy": "never" if yolo else "on-request", "approvalsReviewer": "user",
            "sandbox": "danger-full-access" if yolo else "workspace-write",
            "config": {"sandbox_workspace_write.writable_roots": [str(self.store.database_path.parent)]},
            "developerInstructions": self.store.threads.instructions(record.get("thread_id"), caller_context=True),
        }
        if record.get("model"):
            options["model"] = record["model"]
        if record.get("effort"):
            options["config"]["model_reasoning_effort"] = record["effort"]
        return options

    def _capture_settings(self, thread_id: str, result: dict) -> None:
        """Save resolved defaults from Codex, including when reopening old sessions."""
        record = self._record(thread_id)
        model = result.get("model") or record["model"]
        effort = result.get("reasoningEffort", record["effort"])
        if model and effort is None and record["client"] == "codex":
            selected = next((m for m in self.models(record["client"]) if m["model"] == model), {})
            effort = selected.get("defaultReasoningEffort")
        with self.store._connection() as db:
            db.execute("UPDATE browser_sessions SET model = ?, effort = ? WHERE thread_id = ?", (model, effort, thread_id))

    def _validate_settings(self, model: str | None, effort: str | None, client: str = "codex") -> dict:
        def model_key(value):
            # Claude's catalog reflects the initializing session's context
            # selection (e.g. fable vs fable[1m]). Other sessions can refresh
            # that shared catalog after the browser has loaded its choices.
            return value.removesuffix("[1m]") if client == "claude" and isinstance(value, str) else value

        selected = next((m for m in self.models(client) if model_key(m["model"]) == model_key(model)), None)
        if not selected:
            raise CoordinationError("Choose an available model. Use /model to list models.")
        self._validate_effort(selected, effort)
        return selected

    @staticmethod
    def _validate_effort(selected: dict, effort: str | None) -> None:
        supported = [e["reasoningEffort"] for e in selected.get("supportedReasoningEfforts", [])]
        if effort is not None and effort not in supported:
            raise CoordinationError("Reasoning effort is not supported by this model. Available: " + ", ".join(supported))

    def _change_settings(self, thread_id: str, body: dict, *, reconciled_session: dict | None = None) -> dict:
        # send() already reconciles provider turn state under this thread's lock.
        record = reconciled_session if reconciled_session is not None else self.read(thread_id)["session"]
        if self.store.threads.get(thread_id)["attention"] == "archived":
            raise CoordinationError("Restore this session before changing its settings.")
        with self.lock:
            if thread_id in self.active:
                raise BrowserBusyError("Wait for the running turn to finish before changing settings.")
        yolo = body.get("yolo", bool(record["yolo"]))
        if not isinstance(yolo, bool):
            raise CoordinationError("YOLO must be true or false.")
        model, effort = record["model"], record["effort"]
        if "model" in body or "effort" in body:
            model = self._text(body.get("model", model), "Model", 200)
            selected = self._validate_settings(model, None, record["client"])
            supported = [e["reasoningEffort"] for e in selected.get("supportedReasoningEfforts", [])]
            effort = body.get("effort", effort)
            if "effort" in body:
                effort = self._text(effort, "Reasoning effort", 40)
            elif effort not in supported:
                effort = selected.get("defaultReasoningEffort")
            self._validate_effort(selected, effort)
        with self.store._connection() as db:
            db.execute("UPDATE browser_sessions SET model = ?, effort = ?, yolo = ?, updated_at = ? WHERE thread_id = ?",
                       (model, effort, int(yolo), time.time(), thread_id))
        self._publish("browser/changed", {"threadId": thread_id})
        return self._record(thread_id)

    def _change_directory(self, thread_id: str, argument: str) -> dict:
        record = self._record(thread_id)
        with self.lock:
            if thread_id in self.active:
                raise BrowserBusyError("Wait for the running turn to finish before changing directories.")
        try:
            # Accept a pasted path with spaces as well as a quoted shell path.
            if argument.startswith(("'", '"')):
                paths = shlex.split(argument)
                if len(paths) != 1:
                    raise ValueError("Choose one directory.")
                argument = paths[0]
            if not argument or "\0" in argument:
                raise ValueError("Empty or invalid directory.")
            path = Path(argument).expanduser()
            directory = str((Path(record["cwd"]) / path).resolve())
            valid = Path(directory).is_dir() and matches_workspace(directory, self.cwd)
        except (OSError, RuntimeError, ValueError) as exc:
            raise CoordinationError("Use /cd <directory>, with matching quotes around quoted paths.") from exc
        if not valid:
            raise CoordinationError("Choose an existing directory within this UI's workspace.")
        if record["client"] in {"kimi", "muse"} and directory != record["cwd"]:
            raise CoordinationError(record["client"].capitalize() + " sessions keep their original workspace. Start a new session in the new directory.")
        with self.store._connection() as db:
            db.execute("UPDATE browser_sessions SET cwd = ?, updated_at = ? WHERE thread_id = ?",
                       (directory, time.time(), thread_id))
            db.execute("UPDATE sessions SET cwd = ? WHERE session_id = ?", (directory, thread_id))
        self._publish("browser/changed", {"threadId": thread_id})
        return self._record(thread_id)

    def _command(self, thread_id: str, message: str, *, reconciled_session: dict | None = None) -> dict | None:
        parts = message.split()
        command = parts[0].lower()
        if command not in {"/cd", "/permissions", "/model", "/effort", "/help"}:
            return None
        client = self._record(thread_id)["client"]
        default_permissions = {"claude": "Claude Code configured permissions with tool approval prompts",
                               "antigravity": "Antigravity native permissions with tool approval prompts",
                               "kimi": "Kimi native permissions with tool approval prompts",
                               "muse": "Muse sandboxing with tool approval prompts"}.get(
                                   client, "workspace access with approval prompts")
        if command == "/help":
            reply = ("Commands: /cd [directory], /model [model-id] [effort], /effort [level], "
                     "/permissions [default|yolo], /help. Changes apply to your next message. "
                     "Use /cd with a relative, absolute, or ~ path. "
                     "Permissions: default uses " + default_permissions + "; "
                     "yolo gives full machine access without approval prompts. "
                     "In the UI, /fork opens a new thread from an idle Codex or Claude conversation; "
                     "/close stops and closes the current thread, keeping its history.")
        elif command == "/cd":
            argument = message[len(parts[0]):].strip()
            record = self._change_directory(thread_id, argument) if argument else self._record(thread_id)
            reply = "Working directory: " + record["cwd"] + ". Use /cd <directory> to change it."
        elif command == "/permissions":
            if len(parts) > 2 or (len(parts) == 2 and parts[1].lower() not in {"default", "yolo"}):
                raise CoordinationError("Use /permissions default or /permissions yolo.")
            record = (self._change_settings(thread_id, {"yolo": parts[1].lower() == "yolo"},
                                           reconciled_session=reconciled_session)
                      if len(parts) == 2 else self._record(thread_id))
            reply = ("Permissions: yolo — full machine access without approval prompts." if record["yolo"] else
                     "Permissions: default — " + default_permissions + ".")
            reply += " Use /permissions default or /permissions yolo to change them."
        elif len(parts) == 1:
            record = self._record(thread_id)
            models = self.models(record["client"])
            if command == "/model":
                reply = "Current model: " + (record["model"] or record["client"].capitalize() + " default") + ". Available: " + ", ".join(m["model"] for m in models) + ". Use /model <model-id> [effort]."
            else:
                selected = next((m for m in models if m["model"] == record["model"]), {})
                reply = "Current effort: " + (record["effort"] or "Model default") + ". Available: " + ", ".join(e["reasoningEffort"] for e in selected.get("supportedReasoningEfforts", [])) + ". Use /effort <level>."
        else:
            if len(parts) > (3 if command == "/model" else 2):
                raise CoordinationError("Use /model <model-id> [effort] or /effort <level>.")
            values = {"model" if command == "/model" else "effort": parts[1]}
            if command == "/model" and len(parts) == 3:
                values["effort"] = parts[2]
            record = self._change_settings(thread_id, values, reconciled_session=reconciled_session)
            reply = f"Next message: {record['model']} · {record['effort'] or 'Model default'} reasoning."
        return {"command": {"name": command, "message": reply}, "session": self._record(thread_id)}

    def _creation_settings(self, body: dict) -> tuple[dict, dict]:
        associations = {key: body[key] for key in ("repository_id", "project_id") if key in body}
        self.store.threads.organization.validate_assignments(**associations)
        cwd = body.get("cwd") or self.cwd or os.getcwd()
        if not isinstance(cwd, str):
            raise CoordinationError("Choose a workspace directory.")
        repository = str(Path(cwd).expanduser().resolve())
        if not Path(repository).is_dir() or not matches_workspace(repository, self.cwd):
            raise CoordinationError("Choose an existing directory within this UI's workspace.")
        name = self._text(body.get("name") or "New session", "Session name", 160)
        yolo = body.get("yolo", False)
        if not isinstance(yolo, bool):
            raise CoordinationError("YOLO must be true or false.")
        client = body.get("client", "codex")
        self._provider(client)
        record = {"client": client, "cwd": repository, "name": name, "model": body.get("model") or None, "effort": body.get("effort") or None, "yolo": yolo}
        if record["model"] or record["effort"]:
            self._validate_settings(record["model"], record["effort"], client)
        return record, associations

    def create(self, body: dict, *, on_created=None) -> dict:
        record, associations = self._creation_settings(body)
        repository, name, client, yolo = (record[key] for key in ("cwd", "name", "client", "yolo"))
        provider = self._provider(client)
        result = provider.request("thread/start", self._options(record))
        thread_id = result["thread"]["id"]
        if on_created is not None:
            on_created(thread_id)
        now = time.time()
        with self.store._connection() as connection:
            connection.execute("INSERT INTO browser_sessions (thread_id, cwd, name, model, effort, yolo, created_at, updated_at, client) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                               (thread_id, repository, name, record["model"], record["effort"], int(yolo), now, now, client))
        self._capture_settings(thread_id, result)
        self.store.register(session_id=thread_id, client=client, cwd=repository, name=name)
        self.store.threads.update(thread_id, **associations)
        with self.lock:
            self._remember_thread(result["thread"])
            self.loaded.add(thread_id)
            self.loaded_clients[thread_id] = client
        self._publish("browser/changed", {"threadId": thread_id})
        return {"session": self._record(thread_id), "thread": result["thread"]}

    @staticmethod
    def _text(value, label: str, limit: int) -> str:
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise CoordinationError(f"{label} must contain 1–{limit} characters.")
        return value.strip()

    def _resume_thread(self, thread_id: str, record: dict) -> None:
        """Restore provider context and settings without changing UI placement."""
        if thread_id not in self.loaded and not self.persistence.flush():
            raise BrowserBusyError("Conversation state is still being saved. Try again shortly.")
        if record["archived"]:
            self._rpc(thread_id).request("thread/unarchive", {"threadId": thread_id})
            with self.store._connection() as db:
                db.execute("UPDATE browser_sessions SET archived = 0 WHERE thread_id = ?", (thread_id,))
        if thread_id not in self.loaded:
            result = self._rpc(thread_id).request("thread/resume", {"threadId": thread_id, "excludeTurns": True, **self._options(record)})
            self._capture_settings(thread_id, result)
            self.store.register(session_id=thread_id, client=record["client"], cwd=record["cwd"], name=record["name"])
            with self.lock:
                self.loaded.add(thread_id)
                self.loaded_clients[thread_id] = record["client"]
            self._remember_thread(result["thread"])

    def read(self, thread_id: str) -> dict:
        with self._thread_lock(thread_id):
            with self.lock:
                revision = self.turn_revisions.get(thread_id, 0)
            record = self._record(thread_id)
            work_thread = self.work_thread(thread_id)
            if record["archived"] or work_thread["attention"] == "archived":
                thread = self._read_thread(thread_id)
            elif thread_id not in self.loaded:
                self._resume_thread(thread_id, record)
                record = self._record(thread_id)
                thread = self._read_thread(thread_id)
            else:
                thread = self._read_thread(thread_id)
            with self.lock:
                # A response predating a turn event cannot replace live state.
                if revision == self.turn_revisions.get(thread_id, 0):
                    turns = thread.get("turns", [])
                    latest = turns[-1] if turns else {}
                    status = thread.get("status", {}).get("type")
                    known = self.active.get(thread_id)
                    missing_live_turn = known and not any(t["id"] == known for t in turns)
                    if status in {"idle", "notLoaded"}:
                        self.active.pop(thread_id, None)
                    elif not missing_live_turn:
                        # Earlier turns can retain stale inProgress statuses;
                        # only the latest turn can still be running. Incomplete
                        # stored history must not replace a streamed turn ID.
                        if latest.get("status") == "inProgress":
                            self.active[thread_id] = latest["id"]
                        elif status != "active":
                            self.active.pop(thread_id, None)
                active_turn = self.active.get(thread_id)
                running = thread_id in self.active
            # Read-side recovery may have changed the live turn. Project the card
            # after reconciliation so it agrees with the conversation status.
            return {"session": record, "thread": thread, "work_thread": self.work_thread(thread_id), "requests": self.pending_requests(thread_id),
                    "activeTurn": active_turn, "running": running,
                    "coordinationMessages": message_timeline(self.store, thread_id, self.cwd),
                    "dispatchRequests": self.dispatch.receipts(thread_id),
                    "dispatchSettings": self.dispatch.settings(thread_id),
                    "queuedMessages": self.queue.list(thread_id)}

    def submit(self, thread_id: str, body: dict, *, queued=False) -> dict:
        """User composer entry point. Internal deliveries bypass routing."""
        message = body.get("message", "")
        if self.dispatch.settings(thread_id)["enabled"] and not (isinstance(message, str) and message.strip().startswith("/")):
            return self.dispatch.submit(thread_id, body)
        return self.queue.enqueue(thread_id, body) if queued else self.send(thread_id, body)

    def send(self, thread_id: str, body: dict, *, start_only: bool = False, automatic: bool = False, preserve_text: bool = False) -> dict:
        # A new user turn must not overtake deferred completion/disconnect
        # bookkeeping and then be overwritten by that older state.
        if not self.persistence.flush():
            raise BrowserBusyError("Conversation state is still being saved. Try again shortly.")
        message, images = message_images(body)
        if preserve_text or body.get("mentions"):
            message = body["message"]  # Selected ranges refer to the untrimmed composer text.
        mentions = thread_mentions(message, body, self)
        source_window = window_id(body.get("windowId"))
        expected_turn = body.get("expectedTurnId")
        if "expectedTurnId" in body:
            expected_turn = self._text(expected_turn, "Expected turn ID", 200)
        with self._thread_lock(thread_id):
            record = self._record(thread_id)
            if automatic:
                # The wake worker has claimed the message durably. Closed is
                # organization, while Stop/failure/queued user input remain gates.
                if not self.inbox_wake.can_submit(thread_id):
                    raise BrowserBusyError("Inbox wake paused or the conversation is no longer idle.")
            elif self.store.threads.get(thread_id)["attention"] == "archived":
                raise CoordinationError("Reopen this thread before sending a message.")
            self._resume_thread(thread_id, record)
            self.read(thread_id)
            record = self._record(thread_id)
            if automatic and not self.inbox_wake.can_submit(thread_id):
                raise BrowserBusyError("Inbox wake paused or the conversation is no longer idle.")
            if automatic and self.store.threads.get(thread_id)["attention"] == "archived":
                # Work queued before a subsequent Close is retained; reopen if
                # it is now eligible to run. A stopped/uncertain wake cannot get here.
                self.store.threads.update(thread_id, attention="now")
            command = self._command(thread_id, message, reconciled_session=record) if message and not images else None
            if command is not None:
                return command
            with self.lock:
                turn_id = self.active.get(thread_id)
                if (start_only or automatic) and thread_id in self.active:
                    raise BrowserBusyError("A turn is already running. The queued message was not sent as steering.")
                if expected_turn is not None and expected_turn != turn_id:
                    raise BrowserBusyError("The active turn changed or finished. Your draft was not sent; send it again to continue.")
                if thread_id in self.active and not turn_id:
                    raise BrowserBusyError("The turn is starting. Wait a moment before steering.")
                if not turn_id:
                    self.active[thread_id] = None
            inputs = ([{"type": "text", "text": provider_message(message, mentions)}] if message else [])
            inputs.extend({"type": "image", "url": image["url"]} for image in images)
            dispatch_context = self.dispatch.prepare_context(thread_id)
            if dispatch_context:
                inputs.insert(0, {"type": "text", "text": dispatch_context["text"]})
            params = {"threadId": thread_id, "input": inputs}
            if turn_id:
                # The server checks this too, covering completion during the RPC.
                with self.lock:
                    self.starting_notification_context[thread_id] = {"origin": "user", "owner_thread_id": thread_id}
                try:
                    with self.navigation.sending(thread_id, source_window):
                        result = self._rpc(thread_id).request("turn/steer", {**params, "expectedTurnId": turn_id})
                finally:
                    with self.lock:
                        self.starting_notification_context.pop(thread_id, None)
                self.persistence.submit(lambda: self.store.threads.user_turn_context(thread_id, turn_id))
                self.dispatch.confirm_context(thread_id, dispatch_context)
                self.store.threads.user_message(thread_id)
                self.inbox_wake.resume(thread_id)
                with self.store._connection() as connection:
                    connection.execute("UPDATE browser_sessions SET updated_at = ? WHERE thread_id = ?", (time.time(), thread_id))
                self._publish("browser/changed", {"threadId": thread_id})
                return result
            if record["model"]:
                params["model"] = record["model"]
            params["cwd"] = record["cwd"]
            if record["effort"]:
                params["effort"] = record["effort"]
            if record["yolo"]:
                params.update(approvalPolicy="never", sandboxPolicy={"type": "dangerFullAccess"})
            else:
                roots = list(dict.fromkeys((record["cwd"], str(self.store.database_path.parent))))
                params.update(approvalPolicy="on-request", sandboxPolicy={
                    "type": "workspaceWrite", "writableRoots": roots, "networkAccess": False,
                })
            try:
                context = (notification_policy.automatic_context(self.store, thread_id) if automatic else
                           {"origin": "user", "owner_thread_id": thread_id})
                with self.lock:
                    self.starting_notification_context[thread_id] = context
                with self.navigation.sending(thread_id, source_window):
                    result = self._rpc(thread_id).request("turn/start", params)
                # Some providers reply before emitting turn/started. Save by
                # the accepted ID too; the callback path covers fast completion.
                self.persistence.submit(lambda: self.store.threads.remember_notification_context(
                    thread_id, result["turn"]["id"], context))
            except Exception:
                with self.lock:
                    self.active.pop(thread_id, None)
                raise
            finally:
                with self.lock:
                    self.starting_notification_context.pop(thread_id, None)
            if result.get("model"):
                self._capture_settings(thread_id, result)
            self.dispatch.confirm_context(thread_id, dispatch_context)
            # Some app-server versions omit input items from turn/started.
            if not automatic:
                self.store.threads.user_message(thread_id)
                self.store.threads.capture_request(thread_id, message or "\n".join("[Image]" for _ in images))
                self.inbox_wake.resume(thread_id)
            with self.lock:
                # A fast completed event may arrive before the request response.
                if thread_id in self.active:
                    self.active[thread_id] = result["turn"]["id"]
            with self.store._connection() as connection:
                connection.execute("UPDATE browser_sessions SET updated_at = ? WHERE thread_id = ?", (time.time(), thread_id))
            self._publish("browser/changed", {"threadId": thread_id})
            return result

    def interrupt(self, thread_id: str) -> dict:
        with self._thread_lock(thread_id):
            self._record(thread_id)
            with self.lock:
                turn_id = self.active.get(thread_id)
            if not turn_id:
                raise CoordinationError("This session has no running turn to stop yet.")
            self.inbox_wake.pause(thread_id, "You stopped the turn. Send a message to continue.", kind="stop")
            self.queue.pause(thread_id, "You stopped the turn. Resume queued messages when ready.")
            return self._rpc(thread_id).request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})

    def update(self, thread_id: str, body: dict) -> dict:
        with self._thread_lock(thread_id):
            record = self._record(thread_id)
            if {"model", "effort", "yolo"}.intersection(body):
                record = self._change_settings(thread_id, body)
            if "name" in body:
                name = self._text(body["name"], "Session name", 160)
                self._rpc(thread_id).request("thread/name/set", {"threadId": thread_id, "name": name})
                with self.store._connection() as connection:
                    connection.execute("UPDATE browser_sessions SET name = ? WHERE thread_id = ?", (name, thread_id))
                self.store.threads.update(thread_id, title=name)
                if thread_id in self.loaded:
                    self.store.register(session_id=thread_id, client=record["client"], cwd=record["cwd"], name=name)
            if "archived" in body:
                archived = body["archived"]
                if not isinstance(archived, bool):
                    raise CoordinationError("Archived must be true or false.")
                with self.lock:
                    if thread_id in self.active:
                        raise BrowserBusyError("Stop the running turn before archiving this session.")
                if archived != bool(record["archived"]):
                    if archived:
                        self.queue.pause(thread_id, "The thread closed. Reopen it to resume queued messages.")
                    self._rpc(thread_id).request("thread/archive" if archived else "thread/unarchive", {"threadId": thread_id})
                    with self.store._connection() as connection:
                        connection.execute("UPDATE browser_sessions SET archived = ? WHERE thread_id = ?", (int(archived), thread_id))
                    if archived:
                        with self.lock:
                            self.loaded.discard(thread_id)
                        self.store.end_session(thread_id)
                self.store.threads.update(thread_id, attention="archived" if archived else "now")
            self._publish("browser/changed", {"threadId": thread_id})
            return self._record(thread_id)

    def answer(self, thread_id: str, key: str, body: dict) -> dict:
        self._record(thread_id)
        with self.lock:
            request = self.requests.get(key)
            if not request or request["params"].get("threadId") != thread_id:
                raise CoordinationError("This request is no longer pending in this session.")
            method, params = request["method"], request["params"]
            decision = body.get("decision")
            if method != "item/tool/requestUserInput" and not isinstance(decision, str):
                raise CoordinationError("Choose an approval decision.")
            if method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
                allowed = params.get("availableDecisions") or ["accept", "acceptForSession", "decline", "cancel"]
                if decision not in allowed or decision not in {"accept", "acceptForSession", "decline", "cancel"}:
                    raise CoordinationError("Choose an available approval decision.")
                result = {"decision": decision}
            elif method == "item/permissions/requestApproval":
                if decision not in {"accept", "decline"}:
                    raise CoordinationError("Choose allow or decline.")
                result = {"permissions": params["permissions"] if decision == "accept" else {}, "scope": "turn"}
            elif method == "item/tool/requestUserInput" and decision == "cancel" and params.get("allowCancel"):
                result = {"decision": "cancel"}
            elif method == "item/tool/requestUserInput":
                answers = body.get("answers")
                questions = params.get("questions", [])
                if not isinstance(answers, dict) or set(answers) != {q["id"] for q in questions} or any(
                    not isinstance(a, dict) or not isinstance(a.get("answers"), list) or
                    not a["answers"] or any(not isinstance(s, str) or not s.strip() for s in a["answers"])
                    for a in answers.values()
                ):
                    raise CoordinationError("Answer each question before submitting.")
                result = {"answers": answers}
            elif method == "mcpServer/elicitation/request":
                if decision not in {"accept", "decline", "cancel"}:
                    raise CoordinationError("Choose an elicitation decision.")
                result = {"action": decision, "content": body.get("content") if decision == "accept" else None}
            else:
                if decision != "cancel":
                    raise CoordinationError("This request type is unsupported; cancel it to continue.")
                self._rpc(thread_id).write({"id": request["id"], "error": {"code": -32601, "message": "Request not supported by Ribbon Field UI."}})
                self.requests.pop(key)
                self._publish("browser/requests", {"threadId": thread_id})
                return {}
            self._rpc(thread_id).write({"id": request["id"], "result": result})
            self.requests.pop(key, None)
            self._publish("browser/requests", {"threadId": thread_id})
            return {}

    def close(self) -> None:
        with self.changed:
            if self.closed:
                return
            self.closed = True
            self.changed.notify_all()
        self.classifier.close()
        self.dispatch.close()
        self.event_subscriptions.close()
        self.inbox_wake.close()
        self.rpc.close()
        self.claude.close()
        self.antigravity.close()
        self.kimi.close()
        self.muse.close()
        self.schedules.close()
        for thread_id in list(self.loaded):
            if self.loaded_clients.get(thread_id) != "codex":
                self._event({"method": "thread/closed", "params": {"threadId": thread_id}})
        self.queue.close()
        with self.history_lock:
            if self.history_rpc is not None:
                self.history_rpc.close()
                self.history_rpc = None
        self.persistence.close()
