"""Google Antigravity ACP transport and Ribbon Field conversation adapter.

Uses the official ACP runtime, not the Antigravity IDE or Gemini CLI. Protocol
behavior follows T3 Code's Antigravity adapter and the ACP v1 specification.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import shlex
import sqlite3
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from .acp import ACPConnection
from .antigravity_setup import model_catalog, provider_directory, resolve_runtime
from .context import client_environment, cli_path, coordination_bootstrap
from .message_delivery import MessageDelivery
from .store import CoordinationError
from .permission_modes import acp_modes, acp_current_mode, legacy_mode, validate_mode

AUTH_PREFIX = "Open the following link to authenticate the ACP server: "
BROWSER_MARKER = "RIBBON_FIELD_AUTH_URL="
SIGN_IN = "Sign in to Antigravity with agent-coord antigravity login on this machine, then try again."


def ensure_coordination_hooks(profile):
    """Install only our entry in the ACP private profile; preserve user hooks."""
    import fcntl
    from .hook import ANTIGRAVITY_WRITE_TOOLS

    hook = cli_path().with_name("agent-coord-hook")
    if not hook.is_file():
        raise CoordinationError(f"Antigravity coordination hook is missing: {hook}. Reinstall or rebuild Ribbon Field.")
    path = profile / "config/hooks.json"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Separate locks cover concurrent app processes as well as threads.
    with (path.parent / ".agent-coord-hooks.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        original = path.read_text(encoding="utf-8") if path.exists() else ""
        try:
            config = json.loads(original) if original else {}
        except ValueError as exc:
            raise CoordinationError(f"Cannot parse Antigravity hooks at {path}; repair that JSON before starting.") from exc
        if not isinstance(config, dict):
            raise CoordinationError(f"Antigravity hooks must be a JSON object: {path}")
        config["ribbon-field-coordination"] = {"PreToolUse": [{
            "matcher": "|".join(sorted(ANTIGRAVITY_WRITE_TOOLS)),
            "hooks": [{"type": "command", "timeout": 10,
                "command": shlex.join(["env", "AGENT_COORD_CLIENT=antigravity", sys.executable, str(hook)])}],
        }]}
        updated = json.dumps(config, indent=2) + "\n"
        if updated == original:
            return path
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as output:
                temporary = Path(output.name)
                output.write(updated)
            temporary.chmod(0o600)
            if (path.read_text(encoding="utf-8") if path.exists() else "") != original:
                raise CoordinationError("Antigravity hooks changed during setup. Try again.")
            os.replace(temporary, path)
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)
    return path


class AntigravityConnection(ACPConnection):
    label = "Antigravity"
    sign_in = SIGN_IN

    def __init__(self, store, callback, options, *, command=None):
        self.callback, self.options = callback, options
        self.auth_urls = set()
        root = provider_directory(store)
        profile = root / "profile"
        acp = profile / "antigravity-acp"
        acp.mkdir(parents=True, exist_ok=True, mode=0o700)
        profile.chmod(0o700)
        if options.get("threadId"):
            ensure_coordination_hooks(profile)
        acp.chmod(0o700)
        (acp / "settings.json").write_text('{"auth":{"type":"oauth-personal"}}\n', encoding="utf-8")
        if command is None:
            binary, harness = resolve_runtime(store)
            command = [str(binary)] + (["--uid="] if sys.platform.startswith("linux") else [])
        else:
            harness = Path(command[0]).with_name("localharness_external")
        removed = {"GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_CLOUD_PROJECT",
                   "GOOGLE_CLOUD_LOCATION", "GOOGLE_CLOUD_QUOTA_PROJECT", "GOOGLE_GENAI_USE_VERTEXAI",
                   "GCLOUD_PROJECT", "CLOUDSDK_CORE_PROJECT", "AGY_ACP_CCPA_PROJECT", "AGY_ACP_ENABLE_OAUTH",
                   "GEMINI_HOME", "BROWSER", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "AGENT_COORD_DELEGATION_ID",
                   "AGENT_COORD_ZELLIJ_WAKE"}
        env = {k: v for k, v in os.environ.items() if k.upper() not in removed and not k.startswith("ZELLIJ")}
        env = client_environment(env, store.database_path, "antigravity", session_id=options.get("threadId"))
        # Capture browser requests on both stdout and stderr. Model discovery
        # must never open a browser or start an implicit login.
        helper = 'print("' + BROWSER_MARKER + '"+__import__("sys").argv[1],file=__import__("sys").stderr)'
        env.update(GEMINI_HOME=str(profile), AGY_ACP_FORCE_FILE_STORAGE="1", PYTHONUNBUFFERED="1",
                   ANTIGRAVITY_HARNESS_PATH=str(harness), BROWSER=shlex.join([sys.executable, "-c", helper]) + " %s")
        self.temporary = tempfile.TemporaryDirectory(prefix="process-", dir=root)
        env.update({key: self.temporary.name for key in ("TMPDIR", "TMP", "TEMP")})
        try:
            super().__init__(callback, command=command, cwd=options["cwd"], env=env)
        except Exception:
            self.temporary.cleanup()
            raise

    def initialize(self):
        result = self.request("initialize", {"protocolVersion": 1,
            "clientInfo": {"name": "ribbon-field", "version": "1.0"},
            "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False}})
        # Google's 1.x runtime reports version 2 with the legacy ACP shape.
        if result.get("protocolVersion") not in (1, 2) or "agentCapabilities" not in result or result.get("agentInfo", {}).get("name") != "antigravity-acp":
            raise CoordinationError("Unsupported Antigravity ACP protocol. Update the runtime and Ribbon Field.")
        self.capabilities = result["agentCapabilities"]
        return result

    def _authorization(self, line):
        prefix = next((p for p in (AUTH_PREFIX, BROWSER_MARKER) if line.startswith(p)), None)
        if prefix is None:
            return False
        url = line[len(prefix):].strip()
        parsed = urlparse(url)
        if len(url) > 16384 or parsed.scheme != "https" or parsed.netloc != "accounts.google.com" or any(c.isspace() for c in url):
            self._fail(CoordinationError("Antigravity returned an invalid Google sign-in URL."))
        elif not self.options.get("authorize"):
            self._fail(CoordinationError(SIGN_IN))
        elif url not in self.auth_urls:
            self.auth_urls.add(url)
            self.options["authorize"](url)
        return True

    def _consume_line(self, line):
        return self._authorization(line)

    def close(self):
        try:
            super().close()
        finally:
            self.temporary.cleanup()


class AntigravityRPC:
    def __init__(self, store, on_event, *, connection_factory=AntigravityConnection):
        self.store, self.on_event, self.connection_factory = store, on_event, connection_factory
        self.connections, self.options, self.turns, self.requests, self.native = {}, {}, {}, {}, {}
        self.resolved_models = {}
        self.catalog = None
        self.permission_catalogs = {}
        self.effective_permissions = {}
        self.closed = False
        self.lock = threading.RLock()
        self.catalog_lock = threading.Lock()
        self.delivery = MessageDelivery(store)
        self.delivery_turns = {}
        self.last_activity = {}
        self.prompt_locks = {}
        with store._connection() as db:
            db.execute("CREATE TABLE IF NOT EXISTS antigravity_sessions (thread_id TEXT PRIMARY KEY, native_id TEXT NOT NULL)")

    def _emit(self, method, thread_id, **params):
        self.on_event({"method": method, "params": {"threadId": thread_id, **params}})

    def _context_usage(self, thread_id, update):
        if any(type(update.get(key)) is not int or update[key] < 0 for key in ("used", "size")):
            return
        try:
            self.store.usage.record_unavailable(thread_id, provider="antigravity", native_id=self.native[thread_id],
                note="Antigravity ACP reports context occupancy; cumulative token consumption is unsupported")
            self.store.usage.record_context(thread_id, provider="antigravity", native_id=self.native[thread_id],
                used=update.get("used"), limit=update.get("size"))
            self._emit("thread/usage/updated", thread_id,
                usage=self.store.usage.summary(thread_id, provider="antigravity"))
        except Exception:
            logging.getLogger(__name__).warning("Could not capture Antigravity context usage")

    def _history(self, thread_id):
        with self.store._connection() as db:
            row = db.execute("SELECT history_json FROM browser_history WHERE thread_id = ?", (thread_id,)).fetchone()
        history = json.loads(row[0]) if row else {"id": thread_id, "turns": []}
        with self.lock:
            active = self.turns.get(thread_id)
            if active:
                history["turns"] = [t for t in history.get("turns", []) if t["id"] != active["id"]] + [copy.deepcopy(active)]
            else:
                for turn in history.get("turns", []):
                    if turn.get("status") == "inProgress":
                        turn["status"] = "interrupted"
                        turn["termination"] = {"origin": "runtime", "reason": "runtime_state_lost", "execution": "unknown"}
                    self._unconfirmed_tools(turn, "The session ended before Antigravity reported this tool's outcome.")
        history["status"] = {"type": "active" if active else "idle"}
        return history

    @staticmethod
    def _unconfirmed_tools(turn, reason):
        """Turn termination is not evidence that an individual tool stopped."""
        changed = []
        for item in turn.get("items", []):
            if item.get("type") == "mcpToolCall" and item.get("status") == "inProgress":
                item.update(status="unknown", statusReason=reason)
                changed.append(copy.deepcopy(item))
        return changed

    @staticmethod
    def _options(params):
        mode = params.get("permissionMode") or legacy_mode("antigravity", params.get("approvalPolicy") == "never")
        return {"cwd": params["cwd"], "model": params.get("model"),
                "permission_mode": mode, "yolo": mode == "yolo"}

    def permission_modes(self, cwd):
        with self.catalog_lock:
            if cwd in self.permission_catalogs:
                return copy.deepcopy(self.permission_catalogs[cwd])
            probe = self.connection_factory(self.store, lambda message: None, {"cwd": cwd})
            try:
                probe.initialize()
                probe.request("authenticate", {"methodId": "oauth-personal"})
                session = probe.request("session/new", {"cwd": cwd, "mcpServers": []})
                catalog = acp_modes(session)
                if not catalog:
                    raise CoordinationError("Antigravity did not advertise permission modes.")
                self.permission_catalogs[cwd] = catalog
                return copy.deepcopy(catalog)
            finally:
                probe.close()

    def permission_state(self, thread_id):
        return self.effective_permissions.get(thread_id) if thread_id in self.connections else None

    def _connection(self, thread_id, options):
        current = self.connections.get(thread_id)
        if current and (current.closed or current.process.poll() is not None or self.options.get(thread_id) != options):
            self.connections.pop(thread_id, None)
            current.close()
            current = None
        if current:
            return current
        with self.store._connection() as db:
            row = db.execute("SELECT native_id FROM antigravity_sessions WHERE thread_id = ?", (thread_id,)).fetchone()
        connection = None

        def on_message(message):
            # A retired process can still drain buffered output while closing.
            # It must never complete or append to a replacement process's turn.
            if connection is not None and self.connections.get(thread_id) is connection:
                self._message(thread_id, message)

        connection = self.connection_factory(self.store, on_message, {**options, "threadId": thread_id})
        self.connections[thread_id] = connection
        try:
            initialized = connection.initialize()
            connection.request("authenticate", {"methodId": "oauth-personal"})
            params = {"cwd": options["cwd"], "mcpServers": []}
            method = "session/new"
            if row:
                self.native[thread_id] = row[0]
                caps = initialized["agentCapabilities"]
                if "resume" in caps.get("sessionCapabilities", {}):
                    method = "session/resume"
                elif caps.get("loadSession"):
                    method = "session/load"
                else:
                    raise CoordinationError("This Antigravity runtime cannot resume saved sessions. Update it before continuing.")
                params["sessionId"] = row[0]
            session = connection.request(method, params)
            native_id = row[0] if row else session["sessionId"]
            # Save identity before settings requests; a failed selection must
            # never silently create a replacement native conversation.
            with self.store._connection() as db:
                db.execute("INSERT OR REPLACE INTO antigravity_sessions VALUES (?, ?)", (thread_id, native_id))
            self.native[thread_id] = native_id
            if row:
                try:
                    self.store.usage.record_unavailable(thread_id, provider="antigravity", native_id=native_id,
                        note="Antigravity ACP reports context occupancy; cumulative token consumption is unsupported")
                except Exception:
                    logging.getLogger(__name__).warning("Could not record Antigravity usage availability")
            self.catalog = model_catalog(session)
            model = options.get("model")
            current_model = next((o.get("currentValue") for o in session.get("configOptions", []) if o.get("id") == "model"), None)
            self.resolved_models[thread_id] = model or current_model
            if model:
                if not any(m["model"] == model for m in self.catalog):
                    raise CoordinationError(f"Antigravity model '{model}' is unavailable for this account. Choose an available model.")
                connection.request("session/set_config_option", {"sessionId": native_id, "configId": "model", "value": model})
            mode = options["permission_mode"]
            offered_modes = acp_modes(session)
            if offered_modes:
                validate_mode(mode, offered_modes)
            elif mode not in {"default", "yolo"}:
                raise CoordinationError("Antigravity did not advertise this permission mode.")
            mode_option = next((o for o in session.get("configOptions", []) if o.get("category") == "mode" or o.get("id") == "mode"), None)
            if mode_option:
                applied = connection.request("session/set_config_option", {"sessionId": native_id, "configId": mode_option["id"], "value": mode})
                if acp_current_mode(applied) not in (None, mode):
                    raise CoordinationError("Antigravity did not apply the selected permission mode; no turn was started.")
            else:
                connection.request("session/set_mode", {"sessionId": native_id, "modeId": mode})
            self.effective_permissions[thread_id] = mode
            self.options[thread_id] = dict(options)
            return connection
        except Exception:
            self.connections.pop(thread_id, None)
            connection.close()
            raise

    def _prompt_lock(self, thread_id):
        with self.lock:
            return self.prompt_locks.setdefault(thread_id, threading.RLock())

    def request(self, method, params=None):
        if method == "turn/start":
            # Serialize native prompt starts. Stop itself never waits here.
            with self._prompt_lock(params["threadId"]):
                return self._request(method, params)
        return self._request(method, params)

    def _request(self, method, params=None):
        if self.closed:
            raise CoordinationError("Antigravity connection is closed. Restart Ribbon Field.")
        params = params or {}
        thread_id = params.get("threadId")
        if method == "model/list":
            with self.catalog_lock:
                if self.catalog is None:
                    root = provider_directory(self.store)
                    root.mkdir(parents=True, exist_ok=True)
                    probe = self.connection_factory(self.store, lambda m: None, {"cwd": str(root)})
                    try:
                        probe.initialize()
                        probe.request("authenticate", {"methodId": "oauth-personal"})
                        self.catalog = model_catalog(probe.request("session/new", {"cwd": str(root), "mcpServers": []}))
                    finally:
                        probe.close()
                if not self.catalog:
                    raise CoordinationError("Antigravity did not offer any models for this Google account.")
                return {"data": self.catalog}
        if method == "thread/start":
            thread_id = str(uuid.uuid4())
            options = self._options(params)
            self._connection(thread_id, options)
            return {"thread": self._history(thread_id), "model": self.resolved_models.get(thread_id), "reasoningEffort": None}
        if method == "thread/resume":
            self.options[thread_id] = self._options(params)
            return {"thread": self._history(thread_id), "model": params.get("model"), "reasoningEffort": None}
        if method == "thread/read":
            return {"thread": self._history(thread_id)}
        if method in {"thread/archive", "thread/unarchive", "thread/name/set"}:
            if method == "thread/archive":
                connection = self.connections.pop(thread_id, None)
                if connection:
                    connection.close()
            return {}
        if method == "turn/steer":
            raise CoordinationError("Antigravity does not support steering. Queue the message or stop the turn first.")
        if method == "turn/start":
            if thread_id in self.turns:
                raise CoordinationError("An Antigravity turn is already running.")
            connection = self._connection(thread_id, self._options(params))
            prompt = [{"type": "text", "text": coordination_bootstrap(self.store, thread_id)}]
            for part in params["input"]:
                if part["type"] == "text":
                    prompt.append({"type": "text", "text": part["text"]})
                elif part["type"] == "image":
                    header, data = part["url"].split(",", 1)
                    prompt.append({"type": "image", "mimeType": header[5:].split(";")[0], "data": data})
            turn = {"id": str(uuid.uuid4()), "status": "inProgress", "items": [
                {"id": str(uuid.uuid4()), "type": "userMessage", "content": params["input"]}]}
            self.turns[thread_id] = turn
            self.delivery_turns[thread_id] = {"id": turn["id"], "batch": None, "accepted": False}
            self._emit("turn/started", thread_id, turn=copy.deepcopy(turn))
            try:
                self._submit_prompt(thread_id, turn["id"], connection, prompt)
            except Exception as exc:
                self._finish(thread_id, "failed", str(exc),
                             termination={"origin": "runtime", "reason": "submission_error", "execution": "unknown"})
                raise
            return {"turn": copy.deepcopy(turn)}
        if method == "turn/interrupt":
            turn = self.turns.get(thread_id)
            if not turn or turn["id"] != params["turnId"]:
                raise CoordinationError("The active Antigravity turn changed or finished.")
            self._finish(thread_id, "interrupted",
                         termination={"origin": "user", "reason": "turn_interrupt", "execution": "unknown"})
            connection = self.connections.pop(thread_id, None)
            if connection:
                try:
                    connection.write({"method": "session/cancel", "params": {"sessionId": self.native[thread_id]}})
                finally:
                    connection.close()
            return {}
        raise CoordinationError(f"Antigravity does not support {method}.")

    def _prompt_done(self, thread_id, turn_id, future):
        if self.turns.get(thread_id, {}).get("id") != turn_id:
            return
        def finish(status, error=None, *, reason, origin="provider", execution="unknown"):
            self._finish(thread_id, status, error, expected_turn_id=turn_id,
                         termination={"origin": origin, "reason": reason, "execution": execution})
        try:
            result = future.result()
            reason = result.get("stopReason", "end_turn")
            error = self._runtime_failure(thread_id)
            if error:
                finish("failed", error, reason=reason)
                return
            with self.lock:
                turn = self.turns.get(thread_id, {})
                if turn.get("id") != turn_id:
                    return
                items = turn.get("items", [])
                user_cancelled = self.delivery_turns.get(thread_id, {}).get("user_cancel_requested", False)
                execution = "unknown" if any(i.get("type") == "mcpToolCall" and
                    i.get("status") in {"inProgress", "unknown"} for i in items) else "ended"
            if reason in {"cancelled", "canceled"}:
                finish("interrupted", reason=reason, origin="user" if user_cancelled else "provider")
            elif reason in {"max_tokens", "max_turn_requests"}:
                finish("failed", "Antigravity reached " + reason.replace("_", " ") +
                       ". Review the saved work before continuing.", reason=reason, execution=execution)
            elif reason != "end_turn":
                finish("failed", f"Antigravity stopped the prompt ({reason}). Review the response and native session before continuing.",
                       reason=reason, execution=execution)
            elif not any((i.get("type") == "agentMessage" and i.get("text", "").strip())
                         or i.get("type") == "mcpToolCall" for i in items):
                finish("failed", "Antigravity ended the prompt without a response. Check Antigravity status and logs before continuing.",
                       reason=reason)
            else:
                self._confirm_delivery(thread_id)
                finish("completed", reason=reason, execution=execution)
        except Exception as exc:
            finish("failed", str(exc), origin="runtime", reason="prompt_error")

    def _runtime_failure(self, thread_id):
        # ACP 1.3.0 emits these runtime diagnostics as assistant text followed
        # by end_turn, including when its inner agent has already died. Match
        # only its known diagnostic prefixes, after streaming chunks coalesce.
        with self.lock:
            items = self.turns.get(thread_id, {}).get("items", [])
            item = items[-1] if items else {}
            if item.get("type") != "agentMessage":
                return None
            text = item.get("text", "").strip()
        if text.startswith("Agent connection was lost and could not be re-established:"):
            return text
        if text.startswith("Usage Limit Reached\n") and "You have reached your current quota for this period." in text:
            return text
        return None

    def _submit_prompt(self, thread_id, turn_id, connection, prompt):
        batch = self.delivery.prepare(thread_id, turn_id)
        if batch:
            prompt = [*prompt, {"type": "text", "text": batch["text"]}]
        with self.lock:
            state = self.delivery_turns.get(thread_id)
            if self.closed or not state or state["id"] != turn_id:
                return  # Stop won before submission.
            self.delivery.attempted(turn_id, batch)
            state.update(batch=batch, accepted=False)
            future = connection.submit("session/prompt", {"sessionId": self.native[thread_id], "prompt": prompt})
        future.add_done_callback(lambda result: self._prompt_done(thread_id, turn_id, result))

    def _confirm_delivery(self, thread_id):
        state = self.delivery_turns.get(thread_id)
        if state and not state["accepted"]:
            self.delivery.submitted(thread_id, state["id"], state["batch"])
            state["accepted"] = True

    # ACP has no steering operation or safe in-prompt delivery boundary.
    # Peer messages remain durable while native work runs. BrowserInboxWake
    # delivers them on the next idle turn, with its user queue/Stop/Close guards.
    # Never cancel a prompt (or a pending permission) merely to deliver peers.

    def _finish(self, thread_id, status, error=None, *, termination=None, expected_turn_id=None):
        with self.lock:
            if expected_turn_id is not None and self.turns.get(thread_id, {}).get("id") != expected_turn_id:
                return
            turn = self.turns.pop(thread_id, None)
            if not turn:
                return
            state = self.delivery_turns.pop(thread_id, None)
            if state and status != "completed":
                self.delivery.failed(turn["id"], state["batch"], error or "The turn was stopped; receipt is not acknowledgment.")
            turn["status"] = status
            turn["termination"] = termination or {"origin": "runtime", "reason": "unknown", "execution": "unknown"}
            self._unconfirmed_tools(turn,
                "The turn ended before Antigravity reported this tool's outcome.")
            if error:
                turn["error"] = {"message": str(error)}
            self.requests = {k: v for k, v in self.requests.items() if v[0] != thread_id}
            # A live ACP wrapper does not imply a healthy inner agent. Retire
            # failed processes before publishing completion, so the next user
            # send resumes the saved native session in a fresh process. Never
            # replay a failed prompt automatically: its tools may have run.
            connection = self.connections.pop(thread_id, None) if status == "failed" else None
        if connection:
            connection.close()
        self._emit("turn/completed", thread_id, turn=copy.deepcopy(turn))

    def _message(self, thread_id, message):
        method, params = message.get("method"), message.get("params", {})
        if method == "disconnected":
            self._finish(thread_id, "failed", params.get("error"),
                         termination={"origin": "runtime", "reason": "disconnected", "execution": "unknown"})
            return
        connection = self.connections.get(thread_id)
        if not connection:
            return
        if params.get("sessionId") != self.native.get(thread_id):
            if "id" in message:
                connection.write({"id": message["id"], "error": {"code": -32602, "message": "Unknown session"}})
            return
        if "id" in message:
            if method == "session/request_permission" and thread_id in self.turns:
                self._permission(thread_id, message)
            else:
                connection.write({"id": message["id"], "error": {"code": -32601, "message": "Unsupported client request"}})
            return
        if method != "session/update":
            return
        update = params.get("update", {})
        kind = update.get("sessionUpdate")
        if kind == "usage_update":
            self._context_usage(thread_id, update)
            return
        state = self.delivery_turns.get(thread_id)
        if state:
            self._confirm_delivery(thread_id)
            now = time.monotonic()
            if now - self.last_activity.get(thread_id, -30) >= 15:
                # Liveness is best-effort bookkeeping, not part of receiving
                # provider output. Throttle failed attempts too, so contention
                # cannot make every streamed chunk wait on the same DB lock.
                self.last_activity[thread_id] = now
                try:
                    self.store.touch(thread_id)
                except (sqlite3.Error, CoordinationError):
                    logging.getLogger(__name__).warning(
                        "Could not refresh Antigravity activity for %s; continuing provider stream",
                        thread_id, exc_info=True)
        if kind == "config_option_update":
            self.catalog = model_catalog(update)
        with self.lock:
            turn = self.turns.get(thread_id)
            if not turn:
                return  # Native replay on resume must not duplicate display history.
            if kind in {"agent_message_chunk", "agent_thought_chunk"}:
                content = update.get("content", {})
                if content.get("type") != "text":
                    return
                item_type = "agentMessage" if kind == "agent_message_chunk" else "reasoning"
                item = turn["items"][-1] if turn["items"] else {}
                if item.get("type") != item_type:
                    item = {"id": str(uuid.uuid4()), "type": item_type, **({"text": ""} if item_type == "agentMessage" else {"summary": [""]})}
                    turn["items"].append(item)
                if item_type == "agentMessage":
                    item["text"] += content.get("text", "")
                else:
                    item["summary"][0] += content.get("text", "")
            elif kind in {"tool_call", "tool_call_update"}:
                item_id = update.get("toolCallId")
                if not item_id:
                    return
                item = next((i for i in turn["items"] if i["id"] == item_id), None)
                if item is None:
                    item = {"id": item_id, "type": "mcpToolCall", "tool": update.get("title", "Antigravity tool"), "arguments": {}, "status": "inProgress"}
                    turn["items"].append(item)
                if "title" in update:
                    item["tool"] = update["title"][:1000]
                if "rawInput" in update:
                    item["arguments"] = self._bounded(update["rawInput"])
                if "rawOutput" in update or "content" in update:
                    output = update.get("rawOutput")
                    if output is None:
                        output = update.get("content")
                    if output is not None:
                        item["result"] = self._bounded(output)
                if "status" in update:
                    item["providerStatus"] = update["status"]
                provider_status = item.get("providerStatus", "in_progress")
                item["status"] = {"pending": "inProgress", "in_progress": "inProgress"}.get(provider_status, provider_status)
                # History merges item fields, so explicitly clear an earlier
                # uncertainty reason when a late result resolves the outcome.
                item["statusReason"] = None
                # ACP 1.3.0 can report both a successful read and a missing-file
                # error as completed, omitting the result in both cases. Keep
                # the provider status, but do not present that as a known outcome.
                if provider_status == "completed" and (item.get("result") is None or item.get("result") == []):
                    item.update(status="unknown", statusReason="Antigravity reported completion without a tool result.")
            else:
                return
            snapshot, turn_id = copy.deepcopy(item), turn["id"]
        self._emit("item/completed" if snapshot.get("status") in {"completed", "failed", "unknown"} else "item/started", thread_id, turnId=turn_id, item=snapshot)

    @staticmethod
    def _bounded(value):
        encoded = json.dumps(value, ensure_ascii=False)
        return value if len(encoded) <= 64000 else encoded[:64000] + "…"

    def _permission(self, thread_id, message):
        request = message["params"]
        tool = request.get("toolCall", {})
        options = [o for o in request.get("options", []) if isinstance(o.get("optionId"), str) and o["optionId"]]
        question = str(tool.get("toolCallId", "")).startswith("interaction_")
        key = f"antigravity:{thread_id}:{message['id']}"
        self.requests[key] = (thread_id, message["id"], request)
        params = {"threadId": thread_id, "turnId": self.turns[thread_id]["id"]}
        if question and options:
            method = "item/tool/requestUserInput"
            params["questions"] = [{"id": tool["toolCallId"], "question": tool.get("title", "Choose an option"),
                "allowCustomAnswer": False, "options": [{"value": o["optionId"], "label": o.get("name", o["optionId"])} for o in options]}]
        else:
            method = "item/commandExecution/requestApproval"
            decisions = {"allow_once": "accept", "allow_always": "acceptForSession", "reject_once": "decline"}
            params.update(reason=tool.get("title", "Antigravity tool request"),
                          command=json.dumps(self._bounded(tool.get("rawInput", tool.get("content", {}))), indent=2),
                          availableDecisions=([decisions[o["kind"]] for o in options if o.get("kind") in decisions] if not question else []) + ["cancel"])
        self.on_event({"id": key, "method": method, "params": params})

    def write(self, message):
        pending = self.requests.get(message.get("id"))
        if not pending:
            raise CoordinationError("This Antigravity request is no longer pending.")
        thread_id, request_id, request = pending
        result, selected = message.get("result", {}), None
        question_id = request.get("toolCall", {}).get("toolCallId", "")
        if question_id.startswith("interaction_") and "answers" in result:
            answers = result["answers"].get(question_id, {}).get("answers", [])
            if len(answers) == 1:
                selected = next((o for o in request["options"] if o["optionId"] == answers[0]), None)
            if not selected:
                raise CoordinationError("Choose one of the options offered by Antigravity.")
        elif result.get("decision") != "cancel":
            kind = {"accept": "allow_once", "acceptForSession": "allow_always", "decline": "reject_once"}.get(result.get("decision"))
            selected = next((o for o in request.get("options", []) if kind and o.get("kind") == kind), None)
            if not selected:
                raise CoordinationError("This decision is not offered by Antigravity.")
        outcome = {"outcome": "selected", "optionId": selected["optionId"]} if selected else {"outcome": "cancelled"}
        if selected is None:
            with self.lock:
                state = self.delivery_turns.get(thread_id)
                if state:
                    state["user_cancel_requested"] = True
        self.connections[thread_id].write({"id": request_id, "result": {"outcome": outcome}})
        self.requests.pop(message["id"], None)

    def close(self):
        self.closed = True
        for thread_id in list(self.turns):
            self._finish(thread_id, "interrupted",
                         termination={"origin": "runtime", "reason": "runtime_closed", "execution": "unknown"})
        for thread_id, connection in list(self.connections.items()):
            connection.close()
        self.connections.clear()
