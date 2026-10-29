"""Kimi Code CLI ACP adapter for Ribbon Field conversations.

Protocol: MoonshotAI/kimi-code packages/acp-server (2026-10-12).
Kimi owns execution, authentication and native history. Ribbon Field retains
its own identity, display history and pending user decisions.
"""
from __future__ import annotations

import copy
import json
import logging
import os
from pathlib import Path
import threading
import uuid

from .acp import ACPConnection
from .context import client_environment, coordination_bootstrap
from .kimi_setup import ensure_hooks, resolve_runtime
from .kimi_usage import backfill_usage
from .kimi_status import PromptJournal
from .store import CoordinationError
from .permission_modes import acp_modes, acp_current_mode, legacy_mode, validate_mode


class KimiConnection(ACPConnection):
    label = "Kimi Code CLI"
    sign_in = "Run kimi login on this machine, then try again."

    def __init__(self, store, callback, options, *, command=None):
        if command is None:
            command = [resolve_runtime(), "acp"]
        if options.get("threadId"):
            ensure_hooks()
        removed = {"CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "AGENT_COORD_DELEGATION_ID", "AGENT_COORD_ZELLIJ_WAKE"}
        env = {k: v for k, v in os.environ.items() if k not in removed and not k.startswith("ZELLIJ")}
        env = client_environment(env, store.database_path, "kimi", session_id=options.get("threadId"))
        super().__init__(callback, command=command, cwd=options["cwd"], env=env)

    def initialize(self):
        result = self.request("initialize", {"protocolVersion": 1,
            "clientInfo": {"name": "ribbon-field", "version": "1.0"},
            "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False},
                                   "terminal": False, "elicitation": {"form": {}}}})
        if (result.get("protocolVersion") not in (1, 2)
                or result.get("agentInfo", {}).get("name") != "Kimi Code CLI"
                or not result.get("agentCapabilities", {}).get("loadSession")):
            raise CoordinationError("Unsupported Kimi ACP runtime. Install the current Kimi Code CLI (@moonshot-ai/kimi-code), then try again.")
        self.capabilities = result["agentCapabilities"]
        return result


def select_options(option):
    """ACP select options may be grouped; keep native values opaque."""
    for entry in option.get("options", []):
        if "options" in entry:
            yield from select_options(entry)
        elif isinstance(entry.get("value"), str):
            yield entry


def config_option(session, category):
    return next((option for option in session.get("configOptions", [])
                 if option.get("category") == category or option.get("id") == category), {})


def current_settings(session):
    model = config_option(session, "model").get("currentValue")
    thought = config_option(session, "thought_level") or config_option(session, "thinking")
    return model, thought.get("currentValue")


class KimiRPC:
    def __init__(self, store, on_event, *, connection_factory=KimiConnection):
        self.store, self.on_event, self.connection_factory = store, on_event, connection_factory
        self.connections, self.options, self.native, self.generations = {}, {}, {}, {}
        self.turns, self.completed, self.requests, self.settings, self.capabilities = {}, {}, {}, {}, {}
        self.journals = {}
        self.catalog = None
        self.permission_catalogs = {}
        self.effective_permissions = {}
        self.lock, self.catalog_lock = threading.RLock(), threading.Lock()
        self.closed = False
        with store._connection() as db:
            db.execute("CREATE TABLE IF NOT EXISTS kimi_sessions (thread_id TEXT PRIMARY KEY, native_id TEXT NOT NULL, cwd TEXT NOT NULL)")

    def _emit(self, method, thread_id, **params):
        self.on_event({"method": method, "params": {"threadId": thread_id, **params}})

    def _capture_usage(self, thread_id):
        try:
            usage = backfill_usage(self.store, thread_id)
            self._emit("thread/usage/updated", thread_id, usage=usage)
        except Exception:
            # Telemetry must never fail a provider turn or close its transport.
            logging.getLogger(__name__).warning("Could not capture Kimi usage")

    def _context_usage(self, thread_id, update):
        if any(type(update.get(key)) is not int or update[key] < 0 for key in ("used", "size")):
            return
        try:
            self.store.usage.record_context(thread_id, provider="kimi", native_id=self.native[thread_id],
                used=update.get("used"), limit=update.get("size"))
            self._emit("thread/usage/updated", thread_id,
                usage=self.store.usage.summary(thread_id, provider="kimi"))
        except Exception:
            logging.getLogger(__name__).warning("Could not capture Kimi context usage")

    def _history(self, thread_id):
        with self.store._connection() as db:
            row = db.execute("SELECT history_json FROM browser_history WHERE thread_id = ?", (thread_id,)).fetchone()
        history = json.loads(row[0]) if row else {"id": thread_id, "turns": []}
        with self.lock:
            active = self.turns.get(thread_id)
            latest = active or self.completed.get(thread_id)
            if latest:
                history["turns"] = [t for t in history.get("turns", []) if t["id"] != latest["id"]] + [copy.deepcopy(latest)]
            if not active:
                for turn in history.get("turns", []):
                    if turn.get("status") == "inProgress":
                        turn["status"] = "interrupted"
        history["status"] = {"type": "active" if active else "idle"}
        return history

    @staticmethod
    def _options(params):
        mode = params.get("permissionMode") or legacy_mode("kimi", params.get("approvalPolicy") == "never")
        return {"cwd": params["cwd"], "model": params.get("model"),
                "effort": params.get("effort") or params.get("config", {}).get("model_reasoning_effort"),
                "permission_mode": mode, "yolo": mode == "yolo"}

    def permission_modes(self, cwd):
        with self.catalog_lock:
            if cwd in self.permission_catalogs:
                return copy.deepcopy(self.permission_catalogs[cwd])
            probe = self.connection_factory(self.store, lambda message: None, {"cwd": cwd})
            native_id, caps = None, {}
            try:
                caps = probe.initialize()["agentCapabilities"].get("sessionCapabilities", {})
                session = probe.request("session/new", {"cwd": cwd, "mcpServers": []})
                native_id = session["sessionId"]
                catalog = acp_modes(session)
                if not catalog:
                    raise CoordinationError("Kimi did not advertise permission modes.")
                self.permission_catalogs[cwd] = catalog
                return copy.deepcopy(catalog)
            finally:
                method = "session/delete" if "delete" in caps else "session/close" if "close" in caps else None
                if native_id and method:
                    try:
                        probe.request(method, {"sessionId": native_id}, timeout=3)
                    except CoordinationError:
                        pass
                probe.close()

    def permission_state(self, thread_id):
        return self.effective_permissions.get(thread_id) if thread_id in self.connections else None

    @staticmethod
    def _select(connection, native_id, session, category, value):
        option = config_option(session, category)
        if value not in [entry["value"] for entry in select_options(option)]:
            raise CoordinationError(f"Kimi does not offer {category} '{value}' for this session.")
        result = connection.request("session/set_config_option", {
            "sessionId": native_id, "configId": option["id"], "value": value})
        if not isinstance(result.get("configOptions"), list):
            raise CoordinationError("Kimi did not return updated session settings. Update Kimi Code CLI.")
        return result

    def _connection(self, thread_id, options):
        current = self.connections.get(thread_id)
        if current and (current.process.poll() is not None or self.options.get(thread_id) != options):
            self._shutdown(thread_id)
            current = None
        if current:
            return current
        with self.store._connection() as db:
            row = db.execute("SELECT native_id, cwd FROM kimi_sessions WHERE thread_id = ?", (thread_id,)).fetchone()
        if row and row["cwd"] != options["cwd"]:
            raise CoordinationError("Kimi sessions keep their original workspace. Start a new session in the new directory.")
        generation = object()
        self.generations[thread_id] = generation
        connection = self.connection_factory(self.store,
            lambda message: self._message(thread_id, generation, message), {**options, "threadId": thread_id})
        self.connections[thread_id] = connection
        try:
            initialized = connection.initialize()
            self.capabilities[thread_id] = initialized["agentCapabilities"]
            params = {"cwd": options["cwd"], "mcpServers": []}
            method = "session/new"
            if row:
                self.native[thread_id] = row["native_id"]
                caps = self.capabilities[thread_id]
                if "resume" in caps.get("sessionCapabilities", {}):
                    method = "session/resume"
                elif caps.get("loadSession"):
                    method = "session/load"
                else:
                    raise CoordinationError("This Kimi runtime cannot resume saved sessions. Update Kimi Code CLI.")
                params["sessionId"] = row["native_id"]
            session = connection.request(method, params)
            native_id = row["native_id"] if row else session.get("sessionId")
            if not isinstance(native_id, str) or not native_id:
                raise CoordinationError("Kimi did not return a native session identity.")
            self.native[thread_id] = native_id
            # Persist before setting changes so retries never replace a native conversation.
            with self.store._connection() as db:
                db.execute("INSERT OR REPLACE INTO kimi_sessions VALUES (?, ?, ?)", (thread_id, native_id, options["cwd"]))
            if options.get("model"):
                session = self._select(connection, native_id, session, "model", options["model"])
            if options.get("effort"):
                thought = config_option(session, "thought_level") or config_option(session, "thinking")
                session = self._select(connection, native_id, session, thought.get("id", "thinking"), options["effort"])
            mode = options["permission_mode"]
            validate_mode(mode, acp_modes(session))
            session = self._select(connection, native_id, session, "mode", mode)
            if acp_current_mode(session) != mode:
                raise CoordinationError("Kimi did not apply the selected permission mode; no turn was started.")
            self.effective_permissions[thread_id] = mode
            self.settings[thread_id] = current_settings(session)
            self.options[thread_id] = dict(options)
            if row:
                self._capture_usage(thread_id)
            return connection
        except Exception:
            self._shutdown(thread_id)
            raise

    def _models(self):
        with self.catalog_lock:
            if self.catalog is not None:
                return self.catalog
            root = self.store.database_path.parent / "providers" / "kimi"
            root.mkdir(parents=True, exist_ok=True)
            probe = self.connection_factory(self.store, lambda message: None, {"cwd": str(root)})
            native_id, caps = None, {}
            try:
                caps = probe.initialize()["agentCapabilities"].get("sessionCapabilities", {})
                session = probe.request("session/new", {"cwd": str(root), "mcpServers": []})
                native_id = session["sessionId"]
                models = list(select_options(config_option(session, "model")))
                default_model = current_settings(session)[0]
                catalog = []
                for entry in models:
                    # Thinking choices describe only the selected model. Probe
                    # each on a disposable session, never a user's conversation.
                    session = self._select(probe, native_id, session, "model", entry["value"])
                    thought = config_option(session, "thought_level") or config_option(session, "thinking")
                    catalog.append({"id": entry["value"], "model": entry["value"],
                        "displayName": entry.get("name", entry["value"]), "description": entry.get("description", ""),
                        "isDefault": entry["value"] == default_model,
                        "defaultReasoningEffort": thought.get("currentValue"),
                        "supportedReasoningEfforts": [{"reasoningEffort": e["value"], "description": e.get("name", e["value"])}
                                                       for e in select_options(thought)]})
                if not catalog:
                    raise CoordinationError("Kimi did not advertise any models. Configure a model in Kimi Code CLI.")
                self.catalog = catalog
                return catalog
            finally:
                if native_id:
                    method = "session/delete" if "delete" in caps else "session/close" if "close" in caps else None
                    if method:
                        try:
                            probe.request(method, {"sessionId": native_id}, timeout=3)
                        except CoordinationError:
                            pass
                probe.close()

    def request(self, method, params=None):
        if self.closed:
            raise CoordinationError("Kimi connection is closed. Restart Ribbon Field.")
        params = params or {}
        thread_id = params.get("threadId")
        if method == "model/list":
            return {"data": self._models()}
        if method == "thread/start":
            thread_id = str(uuid.uuid4())
            self._connection(thread_id, self._options(params))
            model, effort = self.settings[thread_id]
            return {"thread": self._history(thread_id), "model": model, "reasoningEffort": effort}
        if method in {"thread/resume", "thread/read"}:
            return {"thread": self._history(thread_id)}
        if method in {"thread/archive", "thread/unarchive", "thread/name/set"}:
            if method == "thread/archive":
                self._shutdown(thread_id)
            return {}
        if method == "turn/steer":
            raise CoordinationError("Kimi does not support steering here. Queue the message or stop the turn first.")
        if method == "turn/start":
            if thread_id in self.turns:
                raise CoordinationError("A Kimi turn is already running.")
            connection = self._connection(thread_id, self._options(params))
            prompt = [{"type": "text", "text": coordination_bootstrap(self.store, thread_id)}]
            for part in params["input"]:
                if part["type"] == "text":
                    prompt.append({"type": "text", "text": part["text"]})
                elif part["type"] == "image":
                    if not self.capabilities[thread_id].get("promptCapabilities", {}).get("image"):
                        raise CoordinationError("This Kimi runtime does not support image input.")
                    header, data = part["url"].split(",", 1)
                    prompt.append({"type": "image", "mimeType": header[5:].split(";")[0], "data": data})
            turn = {"id": str(uuid.uuid4()), "status": "inProgress", "items": [
                {"id": str(uuid.uuid4()), "type": "userMessage", "content": params["input"]}]}
            self.turns[thread_id] = turn
            self._emit("turn/started", thread_id, turn=copy.deepcopy(turn))
            try:
                self.journals[thread_id] = PromptJournal(self.native[thread_id], self.options[thread_id]["cwd"])
                future = connection.submit("session/prompt", {"sessionId": self.native[thread_id], "prompt": prompt})
                future.add_done_callback(lambda result: self._prompt_done(thread_id, turn["id"], result))
            except Exception as exc:
                self._finish(thread_id, "failed", str(exc))
                raise
            return {"turn": copy.deepcopy(turn)}
        if method == "turn/interrupt":
            turn = self.turns.get(thread_id)
            if not turn or turn["id"] != params["turnId"]:
                raise CoordinationError("The active Kimi turn changed or finished.")
            self._finish(thread_id, "interrupted")
            connection = self.connections.get(thread_id)
            try:
                if connection:
                    connection.write({"method": "session/cancel", "params": {"sessionId": self.native[thread_id]}})
            finally:
                self._shutdown(thread_id)
            return {}
        raise CoordinationError(f"Kimi does not support {method}.")

    def _prompt_done(self, thread_id, turn_id, future):
        with self.lock:
            if self.turns.get(thread_id, {}).get("id") != turn_id:
                return
            journal = self.journals.get(thread_id)

        def finish(status, error=None):
            self._finish(thread_id, status, error, expected_turn_id=turn_id)

        try:
            reason = future.result().get("stopReason", "end_turn")
            error = journal.failure() if journal else None
            if error:
                finish("failed", error)
            elif reason in {"max_tokens", "max_turn_requests"}:
                finish("failed", "Kimi reached " + reason.replace("_", " ") + ". Send a message to continue.")
            elif reason in {"cancelled", "canceled"}:
                finish("interrupted")
            elif reason != "end_turn":
                finish("failed", f"Kimi stopped the prompt ({reason}). Check the response and Kimi Code CLI logs before retrying.")
            elif not any((item.get("type") == "agentMessage" and item.get("text", "").strip())
                         or item.get("type") == "mcpToolCall"
                         for item in self.turns.get(thread_id, {}).get("items", [])):
                finish("failed", "Kimi ended the prompt without a response or a terminal error from ACP. "
                       "Check Kimi Code CLI status and logs before continuing.")
            else:
                finish("completed")
        except Exception as exc:
            finish("failed", (journal.failure() if journal else None) or str(exc))

    def _finish(self, thread_id, status, error=None, *, expected_turn_id=None):
        with self.lock:
            if expected_turn_id is not None and self.turns.get(thread_id, {}).get("id") != expected_turn_id:
                return
            turn = self.turns.pop(thread_id, None)
            if not turn:
                return
            self.journals.pop(thread_id, None)
            turn["status"] = status
            for item in turn["items"]:
                if item.get("status") == "inProgress":
                    item["status"] = "completed" if status == "completed" else status
            if error:
                turn["error"] = copy.deepcopy(error) if isinstance(error, dict) else {"message": str(error)}
            # A concurrent read can arrive before the browser event handler
            # persists completion. Keep the last snapshot so disk lag never
            # changes a successful turn into an interrupted, incomplete one.
            self.completed[thread_id] = copy.deepcopy(turn)
            self.requests = {key: value for key, value in self.requests.items() if value["thread"] != thread_id}
        self._capture_usage(thread_id)
        self._emit("turn/completed", thread_id, turn=copy.deepcopy(turn))

    def _message(self, thread_id, generation, message):
        if self.generations.get(thread_id) is not generation:
            return
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
            if thread_id in self.turns and method in {"session/request_permission", "elicitation/create"}:
                try:
                    self._permission(thread_id, message)
                except (CoordinationError, KeyError, TypeError, ValueError) as exc:
                    connection.write({"id": message["id"], "error": {"code": -32602, "message": str(exc)}})
            else:
                connection.write({"id": message["id"], "error": {"code": -32601, "message": "Unsupported client request"}})
            return
        if method != "session/update":
            return
        update = params.get("update", {})
        kind = update.get("sessionUpdate")
        if kind == "usage_update":
            # Kimi sends this after settling session/prompt, outside display
            # turns. The generation and native-session checks still apply.
            self._context_usage(thread_id, update)
            return
        if kind == "config_option_update":
            self.settings[thread_id] = current_settings(update)
        with self.lock:
            turn = self.turns.get(thread_id)
            if not turn:
                return  # Resume history must not duplicate Ribbon Field's display mirror.
            if kind in {"agent_message_chunk", "agent_thought_chunk"}:
                content = update.get("content", {})
                if content.get("type") != "text":
                    return
                item_type = "agentMessage" if kind == "agent_message_chunk" else "reasoning"
                item = turn["items"][-1] if turn["items"] else {}
                if item.get("type") != item_type:
                    item = {"id": str(uuid.uuid4()), "type": item_type,
                            **({"text": ""} if item_type == "agentMessage" else {"summary": [""]})}
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
                    item = {"id": item_id, "type": "mcpToolCall", "tool": update.get("title", "Kimi tool"),
                            "arguments": {}, "status": "inProgress"}
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
        self._emit("item/completed" if snapshot.get("status") in {"completed", "failed"} else "item/started",
                   thread_id, turnId=turn_id, item=snapshot)

    @staticmethod
    def _bounded(value):
        encoded = json.dumps(value, ensure_ascii=False)
        return value if len(encoded) <= 64000 else encoded[:64000] + "…"

    def _permission(self, thread_id, message):
        request = message["params"]
        questions = []
        params = {"threadId": thread_id, "turnId": self.turns[thread_id]["id"]}
        elicitation = message["method"] == "elicitation/create"
        if elicitation:
            if request.get("mode") != "form":
                raise CoordinationError("Only Kimi question forms are supported.")
            for key, schema in request["requestedSchema"]["properties"].items():
                multiple = schema.get("type") == "array"
                entries = schema.get("items", {}).get("anyOf", []) if multiple else schema.get("oneOf", [])
                if not entries or any(not isinstance(entry.get("const"), str) for entry in entries):
                    raise CoordinationError("Unsupported Kimi question form.")
                questions.append({"id": key, "question": schema.get("description") or schema.get("title") or key,
                    "allowCustomAnswer": False, "multiSelect": multiple,
                    "options": [{"value": e["const"], "label": e.get("title", e["const"])} for e in entries]})
        else:
            tool = request.get("toolCall", {})
            options = [o for o in request.get("options", []) if isinstance(o.get("optionId"), str) and o["optionId"]]
            if tool.get("title") == "AskUserQuestion":
                text = "\n".join(c["content"].get("text", "") for c in tool.get("content", [])
                                  if c.get("type") == "content" and c.get("content", {}).get("type") == "text")
                questions = [{"id": tool["toolCallId"], "question": text or "Choose an option", "allowCustomAnswer": False,
                              "options": [{"value": o["optionId"], "label": o.get("name", o["optionId"])} for o in options]}]
            else:
                decisions = {"allow_once": "accept", "allow_always": "acceptForSession", "reject_once": "decline"}
                params.update(reason=tool.get("title", "Kimi tool request"),
                    command=json.dumps(self._bounded(tool.get("rawInput", tool.get("content", {}))), indent=2),
                    availableDecisions=list(dict.fromkeys(decisions[o["kind"]] for o in options if o.get("kind") in decisions)) + ["cancel"])
        if elicitation and not questions:
            raise CoordinationError("Kimi sent an empty question form.")
        if questions:
            params.update(questions=questions, allowCancel=True)
        key = f"kimi:{thread_id}:{message['id']}"
        self.requests[key] = {"thread": thread_id, "id": message["id"], "request": request,
                              "elicitation": elicitation, "questions": questions}
        self.on_event({"id": key, "method": "item/tool/requestUserInput" if questions else "item/commandExecution/requestApproval", "params": params})

    def write(self, message):
        pending = self.requests.get(message.get("id"))
        if not pending:
            raise CoordinationError("This Kimi request is no longer pending.")
        result = message.get("result", {})
        request, questions = pending["request"], pending["questions"]
        cancelled = result.get("decision") == "cancel"
        content, selected = {}, None
        if questions and not cancelled:
            answers = result.get("answers", {})
            if set(answers) != {q["id"] for q in questions}:
                raise CoordinationError("Answer each Kimi question before submitting.")
            for question in questions:
                values = answers[question["id"]].get("answers", [])
                available = {o["value"] for o in question["options"]}
                if (not values or any(value not in available for value in values)
                        or len(values) != len(set(values)) or not question.get("multiSelect") and len(values) != 1):
                    raise CoordinationError("Choose the options offered by Kimi.")
                content[question["id"]] = values if question.get("multiSelect") else values[0]
            if not pending["elicitation"]:
                selected = content[questions[0]["id"]]
        elif not questions and not cancelled:
            kind = {"accept": "allow_once", "acceptForSession": "allow_always", "decline": "reject_once"}.get(result.get("decision"))
            selected = next((o["optionId"] for o in request.get("options", []) if kind and o.get("kind") == kind), None)
            if not selected:
                raise CoordinationError("This decision is not offered by Kimi.")
        if pending["elicitation"]:
            reply = {"action": "cancel"} if cancelled else {"action": "accept", "content": content}
        else:
            reply = {"outcome": {"outcome": "selected", "optionId": selected} if selected else {"outcome": "cancelled"}}
        self.connections[pending["thread"]].write({"id": pending["id"], "result": reply})
        self.requests.pop(message["id"], None)

    def _shutdown(self, thread_id):
        self.generations.pop(thread_id, None)
        connection = self.connections.pop(thread_id, None)
        if connection:
            if "close" in self.capabilities.get(thread_id, {}).get("sessionCapabilities", {}) and thread_id in self.native:
                try:
                    connection.request("session/close", {"sessionId": self.native[thread_id]}, timeout=3)
                except CoordinationError:
                    pass
            connection.close()
            self._capture_usage(thread_id)

    def close(self):
        self.closed = True
        for thread_id in list(self.connections):
            self._finish(thread_id, "interrupted")
            self._shutdown(thread_id)
