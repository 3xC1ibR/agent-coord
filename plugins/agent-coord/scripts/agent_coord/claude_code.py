"""Claude Code stream-json transport, normalized to the browser event contract.

Protocol references: T3 Code's ClaudeAdapterV2 and Anthropic's Python Agent SDK
query/transport. Use the installed CLI so the runtime stays dependency-free.
Each conversation owns its process; Claude owns the resumable native transcript.
"""
from __future__ import annotations

import copy
import json
import os
import subprocess
import threading
import uuid
from collections import deque
from concurrent.futures import Future, TimeoutError

from .context import client_environment
from .store import CoordinationError


class ClaudeConnection:
    def __init__(self, store, callback, options, *, command=None):
        self.callback = callback
        self.lock = threading.Lock()
        self.pending = {}
        self.errors = deque(maxlen=12)
        self.closed = False
        args = list(command or ["claude"])
        args += ["--print", "--input-format", "stream-json", "--output-format", "stream-json",
                 "--verbose", "--include-partial-messages", "--permission-prompt-tool", "stdio"]
        args += ["--resume" if options.get("resume") else "--session-id", options["threadId"]]
        if options.get("probe"):
            # Capability discovery sends no prompts and must not register a
            # phantom work thread through SessionStart/SessionEnd hooks.
            args += ["--settings", json.dumps({"disableAllHooks": True}), "--no-session-persistence"]
        # Do not inherit a parent's bypass mode or disable the user's hooks/settings.
        args += ["--permission-mode", "bypassPermissions" if options.get("yolo") else "default"]
        if options.get("yolo"):
            args += ["--allow-dangerously-skip-permissions"]
        if options.get("model") and options["model"] != "default":
            args += ["--model", options["model"]]
        if options.get("effort"):
            args += ["--effort", options["effort"]]
        if options.get("developerInstructions"):
            args += ["--append-system-prompt", options["developerInstructions"]]
        env = {k: v for k, v in os.environ.items() if not k.startswith("ZELLIJ") and k not in {
            "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CODEX_THREAD_ID", "AGENT_COORD_SESSION_ID",
            "AGENT_COORD_DELEGATION_ID", "AGENT_COORD_ZELLIJ_WAKE",
        }}
        env = client_environment(env, store.database_path, "claude", session_id=options["threadId"])
        try:
            self.process = subprocess.Popen(args, cwd=options["cwd"], env=env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", bufsize=1, start_new_session=True)
        except OSError as exc:
            raise CoordinationError(f"Cannot start Claude Code: {exc}. Install Claude Code and run claude auth login.") from exc
        self.readers = [threading.Thread(target=self._read, daemon=True),
                        threading.Thread(target=self._stderr, daemon=True)]
        for reader in self.readers:
            reader.start()

    def write(self, message):
        with self.lock:
            if self.closed or self.process.poll() is not None:
                raise CoordinationError("Claude Code disconnected. Send again to resume the conversation.")
            try:
                self.process.stdin.write(json.dumps(message) + "\n")
                self.process.stdin.flush()
            except (OSError, ValueError) as exc:
                raise CoordinationError("Connection to Claude Code was lost.") from exc

    def control(self, subtype, *, timeout=30, **values):
        request_id, future = str(uuid.uuid4()), Future()
        with self.lock:
            self.pending[request_id] = future
        try:
            self.write({"type": "control_request", "request_id": request_id,
                        "request": {"subtype": subtype, **values}})
            return future.result(timeout=timeout)
        except TimeoutError as exc:
            raise CoordinationError(f"Claude Code did not respond to {subtype}.") from exc
        finally:
            with self.lock:
                self.pending.pop(request_id, None)

    def _stderr(self):
        for line in self.process.stderr:
            self.errors.append(line.rstrip()[-1000:])

    def _read(self):
        try:
            for line in self.process.stdout:
                try:
                    message = json.loads(line)
                    if not isinstance(message, dict):
                        continue
                    if message.get("type") == "control_response":
                        response = message.get("response", {})
                        with self.lock:
                            future = self.pending.get(response.get("request_id"))
                            if future and not future.done():
                                if response.get("subtype") == "error":
                                    future.set_exception(CoordinationError(str(response.get("error"))))
                                else:
                                    future.set_result(response.get("response") or {})
                    else:
                        self.callback(message)
                except (ValueError, TypeError, KeyError) as exc:
                    self.errors.append(str(exc))
        finally:
            with self.lock:
                for future in self.pending.values():
                    if not future.done():
                        future.set_exception(CoordinationError("Claude Code disconnected. " + "\n".join(self.errors)))
            if not self.closed:
                self.callback({"type": "disconnected", "error": "\n".join(self.errors)})

    def close(self):
        self.closed = True
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


class ClaudeRPC:
    """Translate Claude lifecycle and tool permission frames for BrowserSessions."""

    def __init__(self, store, on_event, *, connection_factory=ClaudeConnection):
        self.store, self.on_event = store, on_event
        self.connection_factory = connection_factory
        self.lock = threading.RLock()
        self.connections, self.options, self.turns, self.requests = {}, {}, {}, {}
        self.blocks = {}
        self.confirmed_blocks = {}
        self.catalog = None
        self.closed = False
        with store._connection() as db:
            db.execute("CREATE TABLE IF NOT EXISTS claude_sessions (thread_id TEXT PRIMARY KEY, started INTEGER NOT NULL DEFAULT 0)")

    def _emit(self, method, thread_id, **params):
        self.on_event({"method": method, "params": {"threadId": thread_id, **params}})

    def _catalog(self, result):
        models = result.get("models")
        if models:
            self.catalog = [{"model": m["value"], "displayName": m.get("displayName", m["value"]),
                             "supportedReasoningEfforts": [{"reasoningEffort": e} for e in m.get("supportedEffortLevels", [])],
                             "defaultReasoningEffort": None} for m in models]

    def _connection(self, thread_id, options):
        current = self.connections.get(thread_id)
        if current and (current.process.poll() is not None or self.options.get(thread_id) != options):
            current.close()
            self.connections.pop(thread_id, None)
            current = None
        if current is None:
            with self.store._connection() as db:
                row = db.execute("SELECT started FROM claude_sessions WHERE thread_id = ?", (thread_id,)).fetchone()
            current = self.connection_factory(self.store, lambda m: self._message(thread_id, m),
                {**options, "threadId": thread_id, "resume": bool(row and row[0])})
            self.connections[thread_id] = current
            self.options[thread_id] = dict(options)
            try:
                self._catalog(current.control("initialize"))
            except Exception:
                current.close()
                self.connections.pop(thread_id, None)
                raise
        return current

    def _history(self, thread_id):
        with self.lock:
            return self._read_history(thread_id)

    def _read_history(self, thread_id):
        with self.store._connection() as db:
            row = db.execute("SELECT history_json FROM browser_history WHERE thread_id = ?", (thread_id,)).fetchone()
        history = json.loads(row[0]) if row else {"id": thread_id, "turns": []}
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
        return {"cwd": params["cwd"], "model": params.get("model") or "default",
                "effort": params.get("effort") or params.get("config", {}).get("model_reasoning_effort"),
                "yolo": params.get("approvalPolicy") == "never",
                "developerInstructions": params.get("developerInstructions", "")}

    def request(self, method, params=None):
        if self.closed:
            raise CoordinationError("Claude connection is closed. Restart the UI.")
        params = params or {}
        thread_id = params.get("threadId")
        if method == "model/list":
            if self.catalog is None:
                probe = self.connection_factory(self.store, lambda m: None,
                    {"threadId": str(uuid.uuid4()), "cwd": str(self.store.database_path.parent), "probe": True})
                try:
                    self._catalog(probe.control("initialize"))
                finally:
                    probe.close()
            if not self.catalog:
                raise CoordinationError("Claude Code did not advertise available models. Update Claude Code and try again.")
            return {"data": self.catalog}
        if method == "thread/start":
            thread_id = str(uuid.uuid4())
            options = self._options(params)
            self._connection(thread_id, options)
            with self.store._connection() as db:
                db.execute("INSERT INTO claude_sessions (thread_id) VALUES (?)", (thread_id,))
            return {"thread": self._history(thread_id), "model": options["model"], "reasoningEffort": options["effort"]}
        if method == "thread/resume":
            # Opening saved history does not need authentication or a running CLI.
            self.options[thread_id] = self._options(params)
            return {"thread": self._history(thread_id), "model": self.options[thread_id]["model"],
                    "reasoningEffort": self.options[thread_id]["effort"]}
        if method == "thread/read":
            return {"thread": self._history(thread_id)}
        if method in {"thread/archive", "thread/unarchive", "thread/name/set"}:
            if method == "thread/archive":
                connection = self.connections.pop(thread_id, None)
                if connection:
                    connection.close()
            return {}
        if method == "turn/steer":
            raise CoordinationError("Claude does not support steering here. Queue the message or stop the turn first.")
        if method == "turn/start":
            if thread_id in self.turns:
                raise CoordinationError("A Claude turn is already running.")
            options = {**self.options.get(thread_id, {}), **self._options(params)}
            options["developerInstructions"] = self.store.threads.instructions(thread_id, caller_context=True)
            connection = self._connection(thread_id, options)
            user = {"id": str(uuid.uuid4()), "type": "userMessage", "content": params["input"]}
            turn = {"id": str(uuid.uuid4()), "items": [user], "status": "inProgress"}
            self.turns[thread_id] = turn
            self.blocks[thread_id] = {}
            self.confirmed_blocks[thread_id] = set()
            self._emit("turn/started", thread_id, turn=copy.deepcopy(turn))
            content = []
            for part in params["input"]:
                if part["type"] == "text":
                    content.append({"type": "text", "text": part["text"]})
                elif part["type"] == "image":
                    header, data = part["url"].split(",", 1)
                    content.append({"type": "image", "source": {"type": "base64", "media_type": header[5:].split(";")[0], "data": data}})
            try:
                connection.write({"type": "user", "session_id": thread_id, "parent_tool_use_id": None,
                                  "uuid": user["id"], "message": {"role": "user", "content": content}})
            except Exception:
                self._finish(thread_id, "failed", "Claude Code disconnected before accepting the message.")
                raise
            return {"turn": copy.deepcopy(turn)}
        if method == "turn/interrupt":
            turn = self.turns.get(thread_id)
            if not turn or turn["id"] != params["turnId"]:
                raise CoordinationError("The active Claude turn changed or finished.")
            # Kill only this conversation's process: no shared server or other chat.
            # Closing the transport bounds Stop even when an approval is outstanding.
            connection = self.connections.pop(thread_id, None)
            if connection:
                connection.close()
            self._finish(thread_id, "interrupted")
            return {}
        raise CoordinationError(f"Claude does not support {method}.")

    def _item(self, thread_id, item, *, completed=False):
        turn = self.turns.get(thread_id)
        if not turn:
            return
        old = next((i for i in turn["items"] if i["id"] == item["id"]), None)
        if old is None:
            turn["items"].append(item)
        else:
            old.update(item)
        self._emit("item/completed" if completed else "item/started", thread_id, turnId=turn["id"], item=copy.deepcopy(item))

    @staticmethod
    def _block(block, item_id):
        kind = block.get("type")
        if kind == "text":
            return {"id": item_id, "type": "agentMessage", "text": block.get("text", "")}
        if kind in {"thinking", "redacted_thinking"}:
            return {"id": item_id, "type": "reasoning", "summary": [block.get("thinking", "")]}
        if kind == "tool_use":
            return {"id": block["id"], "type": "mcpToolCall", "tool": block["name"],
                    "arguments": block.get("input", {}), "status": "inProgress"}
        return None

    def _message(self, thread_id, message):
        with self.lock:
            self._handle_message(thread_id, message)

    def _handle_message(self, thread_id, message):
        kind = message.get("type")
        if kind == "control_request":
            self._permission(thread_id, message)
            return
        if kind == "control_cancel_request":
            request_id = f"claude:{thread_id}:{message['request_id']}"
            self.requests.pop(request_id, None)
            self._emit("serverRequest/resolved", thread_id, requestId=request_id)
            return
        if kind == "disconnected":
            self._finish(thread_id, "failed", message.get("error") or "Claude Code disconnected. Send again to resume.")
            return
        if message.get("parent_tool_use_id"):
            return  # Nested agent blocks must not overwrite root stream indices.
        turn = self.turns.get(thread_id)
        if not turn:
            return
        if kind == "system" and message.get("subtype") == "init":
            with self.store._connection() as db:
                db.execute("UPDATE claude_sessions SET started = 1 WHERE thread_id = ?", (thread_id,))
        elif kind == "stream_event":
            event = message.get("event", {})
            index = event.get("index", 0)
            if event.get("type") == "message_start":
                self.blocks[thread_id] = {}
                self.confirmed_blocks[thread_id] = set()
            elif event.get("type") == "content_block_start":
                item = self._block(event["content_block"], str(uuid.uuid4()))
                if item:
                    self.blocks[thread_id][index] = item
                    self._item(thread_id, item)
            elif event.get("type") == "content_block_delta":
                item = self.blocks.get(thread_id, {}).get(index)
                if item:
                    delta = event.get("delta", {})
                    if delta.get("type") == "text_delta":
                        item["text"] += delta.get("text", "")
                        self._emit("item/agentMessage/delta", thread_id, turnId=turn["id"], itemId=item["id"], delta=delta.get("text", ""))
                    elif delta.get("type") == "thinking_delta":
                        item["summary"][0] += delta.get("thinking", "")
                        self._item(thread_id, item)
            elif event.get("type") == "content_block_stop":
                item = self.blocks.get(thread_id, {}).get(index)
                if item:
                    self._item(thread_id, item, completed=True)
        elif kind == "assistant":
            # Claude can emit one assistant frame per block, so its content
            # index is not the stream's index (e.g. thinking, then text, then a
            # tool). Reconcile by block kind/identity without duplicating text.
            confirmed = self.confirmed_blocks.setdefault(thread_id, set())
            for block in message.get("message", {}).get("content", []):
                item = self._block(block, str(uuid.uuid4()))
                if item:
                    candidates = [i for i in self.blocks.get(thread_id, {}).values() if i["type"] == item["type"]]
                    previous = next((i for i in candidates if i["id"] not in confirmed), None)
                    if previous is None and item["type"] == "agentMessage":
                        previous = next((i for i in candidates if i.get("text") == item["text"]), None)
                    if previous and item["type"] != "mcpToolCall":
                        item["id"] = previous["id"]
                    confirmed.add(item["id"])
                    self._item(thread_id, item, completed=True)
        elif kind == "user":
            content = message.get("message", {}).get("content", [])
            for block in content if isinstance(content, list) else []:
                if block.get("type") == "tool_result":
                    item = next((i for i in turn["items"] if i["id"] == block.get("tool_use_id")), None)
                    if item:
                        self._item(thread_id, {**item, "result": block.get("content"),
                            "status": "failed" if block.get("is_error") else "completed"}, completed=True)
        elif kind == "result":
            failed = message.get("is_error") or message.get("subtype") != "success"
            error = "\n".join(message.get("errors") or []) or message.get("result") if failed else None
            self._finish(thread_id, "failed" if failed else "completed", error)

    def _finish(self, thread_id, status, error=None):
        with self.lock:
            self._complete_turn(thread_id, status, error)

    def _complete_turn(self, thread_id, status, error=None):
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
        self.blocks.pop(thread_id, None)
        self.confirmed_blocks.pop(thread_id, None)
        self._emit("turn/completed", thread_id, turn=copy.deepcopy(turn))

    def _permission(self, thread_id, message):
        request, request_id = message["request"], message["request_id"]
        connection = self.connections.get(thread_id)
        if not connection:
            return
        if request.get("subtype") != "can_use_tool":
            connection.write({"type": "control_response", "response": {"subtype": "error", "request_id": request_id,
                "error": "This Claude control request is not supported by Ribbon Field."}})
            return
        key = f"claude:{thread_id}:{request_id}"
        self.requests[key] = (thread_id, request_id, request)
        params = {"threadId": thread_id, "turnId": self.turns.get(thread_id, {}).get("id")}
        if request.get("tool_name") == "AskUserQuestion":
            method = "item/tool/requestUserInput"
            params["questions"] = [{**q, "id": str(i)} for i, q in enumerate(request.get("input", {}).get("questions", []))]
        else:
            method = "item/commandExecution/requestApproval"
            params.update(reason=request.get("description") or request.get("tool_name"),
                          command=json.dumps(request.get("input", {}), indent=2), availableDecisions=["accept", "decline", "cancel"])
        self.on_event({"id": key, "method": method, "params": params})

    def write(self, message):
        pending = self.requests.get(message.get("id"))
        if not pending:
            raise CoordinationError("This Claude request is no longer pending.")
        thread_id, request_id, request = pending
        result = message.get("result", {})
        if request.get("tool_name") == "AskUserQuestion" and "answers" in result:
            answers = {q["question"]: ", ".join(result["answers"][str(i)]["answers"])
                       for i, q in enumerate(request["input"]["questions"])}
            response = {"behavior": "allow", "updatedInput": {**request["input"], "answers": answers}}
        elif result.get("decision") == "accept":
            response = {"behavior": "allow", "updatedInput": request.get("input", {})}
        else:
            response = {"behavior": "deny", "message": "The user declined this request.", "interrupt": result.get("decision") == "cancel"}
        self.connections[thread_id].write({"type": "control_response", "response": {
            "subtype": "success", "request_id": request_id, "response": response}})
        self.requests.pop(message["id"], None)

    def close(self):
        self.closed = True
        for thread_id, connection in list(self.connections.items()):
            connection.close()
            self._finish(thread_id, "interrupted")
        self.connections.clear()
