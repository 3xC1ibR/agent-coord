"""Google Antigravity ACP transport and Ribbon Field conversation adapter.

Uses the official ACP runtime, not the Antigravity IDE or Gemini CLI. Protocol
behavior follows T3 Code's Antigravity adapter and the ACP v1 specification.
"""
from __future__ import annotations

import copy
import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import uuid
from collections import deque
from concurrent.futures import Future, TimeoutError
from pathlib import Path
from urllib.parse import urlparse

from .antigravity_setup import model_catalog, provider_directory, resolve_runtime
from .context import client_environment
from .store import CoordinationError

AUTH_PREFIX = "Open the following link to authenticate the ACP server: "
BROWSER_MARKER = "RIBBON_FIELD_AUTH_URL="
SIGN_IN = "Sign in to Antigravity with agent-coord antigravity login on this machine, then try again."


class AntigravityConnection:
    def __init__(self, store, callback, options, *, command=None):
        self.callback, self.options = callback, options
        self.lock = threading.RLock()
        self.pending = {}
        self.errors = deque(maxlen=12)
        self.closed = False
        self.auth_urls = set()
        root = provider_directory(store)
        profile = root / "profile"
        acp = profile / "antigravity-acp"
        acp.mkdir(parents=True, exist_ok=True, mode=0o700)
        profile.chmod(0o700)
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
            self.process = subprocess.Popen(command, cwd=options["cwd"], env=env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", bufsize=1, start_new_session=True)
        except OSError as exc:
            self.temporary.cleanup()
            raise CoordinationError(f"Cannot start Antigravity: {exc}") from exc
        self.readers = [threading.Thread(target=self._read, daemon=True), threading.Thread(target=self._stderr, daemon=True)]
        for reader in self.readers:
            reader.start()

    def write(self, message):
        with self.lock:
            if self.closed or self.process.poll() is not None:
                raise CoordinationError("Antigravity disconnected. Send again to resume.")
            try:
                self.process.stdin.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
                self.process.stdin.flush()
            except (OSError, ValueError) as exc:
                raise CoordinationError("Connection to Antigravity was lost.") from exc

    def submit(self, method, params):
        request_id, future = str(uuid.uuid4()), Future()
        with self.lock:
            self.pending[request_id] = future
        try:
            self.write({"id": request_id, "method": method, "params": params})
        except Exception:
            with self.lock:
                self.pending.pop(request_id, None)
            raise
        return future

    def request(self, method, params, *, timeout=60):
        future = self.submit(method, params)
        try:
            return future.result(timeout=timeout)
        except TimeoutError as exc:
            raise CoordinationError(f"Antigravity did not respond to {method}.") from exc
        finally:
            with self.lock:
                self.pending = {k: v for k, v in self.pending.items() if v is not future}

    def initialize(self):
        result = self.request("initialize", {"protocolVersion": 1,
            "clientInfo": {"name": "ribbon-field", "version": "1.0"},
            "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False}})
        # Google's 1.x runtime reports version 2 with the legacy ACP shape.
        if result.get("protocolVersion") not in (1, 2) or "agentCapabilities" not in result or result.get("agentInfo", {}).get("name") != "antigravity-acp":
            raise CoordinationError("Unsupported Antigravity ACP protocol. Update the runtime and Ribbon Field.")
        self.capabilities = result["agentCapabilities"]
        return result

    def _fail(self, error):
        with self.lock:
            pending, self.pending = list(self.pending.values()), {}
        for future in pending:
            if not future.done():
                future.set_exception(error)

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

    def _stderr(self):
        for line in self.process.stderr:
            if not self._authorization(line):
                self.errors.append(line.rstrip()[-1000:])

    def _read(self):
        try:
            for line in self.process.stdout:
                if self._authorization(line):
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    continue  # The official runtime also emits startup diagnostics.
                if not isinstance(message, dict):
                    continue
                if "method" in message:
                    self.callback(message)
                elif "id" in message:
                    with self.lock:
                        future = self.pending.pop(message["id"], None)
                    if future and not future.done():
                        if "error" in message:
                            error = message["error"]
                            detail = error.get("message", "Request failed")
                            if error.get("code") == -32000:
                                detail += ". " + SIGN_IN
                            future.set_exception(CoordinationError("Antigravity: " + detail))
                        else:
                            future.set_result(message.get("result") or {})
        except (OSError, ValueError, TypeError, KeyError) as exc:
            self.errors.append(str(exc))
        finally:
            error = CoordinationError("Antigravity disconnected. " + "\n".join(self.errors))
            self._fail(error)
            if not self.closed:
                self.callback({"method": "disconnected", "params": {"error": str(error)}})

    def close(self):
        if self.closed:
            return
        self.closed = True
        self._fail(CoordinationError("Antigravity connection closed."))
        # The ACP process owns a harness subprocess; Stop must stop both.
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                if os.name == "posix":
                    os.killpg(self.process.pid, sig)
                elif self.process.poll() is None:
                    self.process.terminate() if sig == signal.SIGTERM else self.process.kill()
            except (ProcessLookupError, PermissionError):
                # A just-exited process group may no longer be signalable on
                # macOS. Reap the owned process; never target a different PID.
                if self.process.poll() is None:
                    self.process.terminate() if sig == signal.SIGTERM else self.process.kill()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                continue
        for reader in self.readers:
            if reader is not threading.current_thread():
                reader.join(timeout=3)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            stream.close()
        self.temporary.cleanup()


class AntigravityRPC:
    def __init__(self, store, on_event, *, connection_factory=AntigravityConnection):
        self.store, self.on_event, self.connection_factory = store, on_event, connection_factory
        self.connections, self.options, self.turns, self.requests, self.native = {}, {}, {}, {}, {}
        self.resolved_models = {}
        self.catalog = None
        self.closed = False
        self.lock = threading.RLock()
        self.catalog_lock = threading.Lock()
        with store._connection() as db:
            db.execute("CREATE TABLE IF NOT EXISTS antigravity_sessions (thread_id TEXT PRIMARY KEY, native_id TEXT NOT NULL)")

    def _emit(self, method, thread_id, **params):
        self.on_event({"method": method, "params": {"threadId": thread_id, **params}})

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
        history["status"] = {"type": "active" if active else "idle"}
        return history

    @staticmethod
    def _options(params):
        return {"cwd": params["cwd"], "model": params.get("model"), "yolo": params.get("approvalPolicy") == "never"}

    def _connection(self, thread_id, options):
        current = self.connections.get(thread_id)
        if current and (current.process.poll() is not None or self.options.get(thread_id) != options):
            self.connections.pop(thread_id, None)
            current.close()
            current = None
        if current:
            return current
        with self.store._connection() as db:
            row = db.execute("SELECT native_id FROM antigravity_sessions WHERE thread_id = ?", (thread_id,)).fetchone()
        connection = self.connection_factory(self.store, lambda m: self._message(thread_id, m), {**options, "threadId": thread_id})
        self.connections[thread_id] = connection
        try:
            initialized = connection.initialize()
            connection.request("authenticate", {"methodId": "oauth-personal"})
            params = {"cwd": options["cwd"], "mcpServers": []}
            method = "session/new"
            if row:
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
            self.catalog = model_catalog(session)
            model = options.get("model")
            current_model = next((o.get("currentValue") for o in session.get("configOptions", []) if o.get("id") == "model"), None)
            self.resolved_models[thread_id] = model or current_model
            if model:
                if not any(m["model"] == model for m in self.catalog):
                    raise CoordinationError(f"Antigravity model '{model}' is unavailable for this account. Choose an available model.")
                connection.request("session/set_config_option", {"sessionId": native_id, "configId": "model", "value": model})
            mode = "yolo" if options.get("yolo") else "default"
            mode_option = next((o for o in session.get("configOptions", []) if o.get("category") == "mode" or o.get("id") == "mode"), None)
            if mode_option:
                connection.request("session/set_config_option", {"sessionId": native_id, "configId": mode_option["id"], "value": mode})
            else:
                connection.request("session/set_mode", {"sessionId": native_id, "modeId": mode})
            self.options[thread_id] = dict(options)
            return connection
        except Exception:
            self.connections.pop(thread_id, None)
            connection.close()
            raise

    def request(self, method, params=None):
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
            prompt = [{"type": "text", "text": f"Your Ribbon Field session ID is {thread_id}. The agent-coord CLI inherits this identity.\n"
                       + self.store.threads.instructions(thread_id, caller_context=True)
                       + "\nRead workspace AGENTS.md before editing. Use agent-coord for checkpoints and concurrent write scopes."}]
            for part in params["input"]:
                if part["type"] == "text":
                    prompt.append({"type": "text", "text": part["text"]})
                elif part["type"] == "image":
                    header, data = part["url"].split(",", 1)
                    prompt.append({"type": "image", "mimeType": header[5:].split(";")[0], "data": data})
            turn = {"id": str(uuid.uuid4()), "status": "inProgress", "items": [
                {"id": str(uuid.uuid4()), "type": "userMessage", "content": params["input"]}]}
            self.turns[thread_id] = turn
            self._emit("turn/started", thread_id, turn=copy.deepcopy(turn))
            try:
                future = connection.submit("session/prompt", {"sessionId": self.native[thread_id], "prompt": prompt})
                future.add_done_callback(lambda result: self._prompt_done(thread_id, turn["id"], result))
            except Exception as exc:
                self._finish(thread_id, "failed", str(exc))
                raise
            return {"turn": copy.deepcopy(turn)}
        if method == "turn/interrupt":
            turn = self.turns.get(thread_id)
            if not turn or turn["id"] != params["turnId"]:
                raise CoordinationError("The active Antigravity turn changed or finished.")
            self._finish(thread_id, "interrupted")
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
        try:
            result = future.result()
            reason = result.get("stopReason", "end_turn")
            self._finish(thread_id, "interrupted" if reason in {"cancelled", "canceled"} else "completed")
        except Exception as exc:
            self._finish(thread_id, "failed", str(exc))

    def _finish(self, thread_id, status, error=None):
        with self.lock:
            turn = self.turns.pop(thread_id, None)
            if not turn:
                return
            turn["status"] = status
            for item in turn["items"]:
                if item.get("status") == "inProgress":
                    item["status"] = "completed" if status == "completed" else status
            if error:
                turn["error"] = {"message": str(error)}
            self.requests = {k: v for k, v in self.requests.items() if v[0] != thread_id}
        self._emit("turn/completed", thread_id, turn=copy.deepcopy(turn))

    def _message(self, thread_id, message):
        method, params = message.get("method"), message.get("params", {})
        if method == "disconnected":
            self._finish(thread_id, "failed", params.get("error"))
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
                    item["result"] = self._bounded(update.get("rawOutput", update.get("content")))
                if "status" in update:
                    item["status"] = {"pending": "inProgress", "in_progress": "inProgress"}.get(update["status"], update["status"])
            else:
                return
            snapshot, turn_id = copy.deepcopy(item), turn["id"]
        self._emit("item/completed" if snapshot.get("status") in {"completed", "failed"} else "item/started", thread_id, turnId=turn_id, item=snapshot)

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
        self.connections[thread_id].write({"id": request_id, "result": {"outcome": outcome}})
        self.requests.pop(message["id"], None)

    def close(self):
        self.closed = True
        for thread_id, connection in list(self.connections.items()):
            self._finish(thread_id, "interrupted")
            connection.close()
        self.connections.clear()
