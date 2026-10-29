"""Native Muse Session Protocol adapter, tested against Muse Code 1.4.8.

Muse owns execution, sandboxing and native history. One host per conversation
isolates cancellation and the host-wide sandbox. No model API or SDK dependency.
"""
from __future__ import annotations

import copy
import json
import logging
import os
from pathlib import Path
import queue
import re
import shlex
import threading
import time
import uuid

from .acp import ACPConnection
from .context import client_environment, coordination_bootstrap
from .muse_setup import SCHEMA_FINGERPRINT, TESTED_VERSION, host_config, resolve_runtime
from .muse_usage import MuseUsage
from .store import CoordinationError
from .permission_modes import legacy_mode


def command_id():
    """MSP commands require UUIDv7, including on Python versions before 3.14."""
    value = (int(time.time() * 1000) << 80) | (uuid.uuid4().int & ((1 << 76) - 1)) | (7 << 76)
    return str(uuid.UUID(int=(value & ~(3 << 62)) | (2 << 62)))


class MuseConnection(ACPConnection):
    label = "Muse Code"

    def __init__(self, store, callback, options, *, command=None):
        if command is None:
            command = [resolve_runtime(store), "serve"]
            if options.get("yolo"):
                command.append("--disable-sandbox")
        removed = {"CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "AGENT_COORD_DELEGATION_ID", "AGENT_COORD_ZELLIJ_WAKE"}
        env = {k: v for k, v in os.environ.items() if k not in removed and not k.startswith("ZELLIJ")}
        env = client_environment(env, store.database_path, "muse", session_id=options.get("threadId"))
        # Starting a conversation should not trigger a shared runtime update.
        env["MUSE_NO_AUTO_UPDATE"] = "1"
        self.configuration = host_config(store, options["threadId"]) if options.get("threadId") else None
        if self.configuration:
            env["XDG_CONFIG_HOME"] = self.configuration.name
        self.notifications = queue.Queue()
        self.dispatcher = None
        try:
            super().__init__(self.notifications.put, command=command, cwd=options["cwd"], env=env)
        except Exception:
            if self.configuration:
                self.configuration.cleanup()
            raise
        def dispatch():
            while True:
                message = self.notifications.get()
                if message is None:
                    return
                try:
                    callback(message)
                except Exception as exc:
                    self.errors.append(str(exc)[-1000:])
                    callback({"method": "disconnected", "params": {"error": f"Invalid Muse event: {exc}"}})
                    return
        # Responses must keep draining while the UI lock is held by answer().
        self.dispatcher = threading.Thread(target=dispatch, daemon=True)
        self.dispatcher.start()

    def initialize(self):
        result = self.request("initialize", {"clientInfo": {"name": "ribbon_field", "version": "0.1.0"},
            "capabilities": {"userInputDialogs": True, "experimentalApi": True}})
        schema = result.get("schema", {})
        if not isinstance(schema, dict) or schema.get("version") != 1:
            raise CoordinationError("Unsupported Muse protocol schema version. Ribbon Field supports schema version 1; "
                                    "update Ribbon Field or select a compatible executable with AGENT_COORD_MUSE_BIN.")
        if result.get("sessionDurability", "durable") != "durable":
            raise CoordinationError("Muse must retain session history to support Ribbon Field conversations.")
        # MSP evolves additively. A fingerprint identifies a schema, not an
        # incompatibility; keep it diagnostic so routine host upgrades work.
        if schema.get("fingerprint") != SCHEMA_FINGERPRINT:
            logging.getLogger(__name__).warning(
                "Muse schema fingerprint differs from the Muse Code %s reference: expected %s, received %s. "
                "Continuing with schema version 1.", TESTED_VERSION, SCHEMA_FINGERPRINT, schema.get("fingerprint"))
        self.write({"method": "initialized"})
        return result

    def close(self):
        if self.closed:
            return
        super().close()
        self.notifications.put(None)
        if self.dispatcher and self.dispatcher is not threading.current_thread():
            self.dispatcher.join(timeout=3)
        if self.configuration:
            self.configuration.cleanup()


class MuseRPC:
    def __init__(self, store, on_event, *, connection_factory=MuseConnection):
        self.store, self.on_event, self.connection_factory = store, on_event, connection_factory
        self.connections, self.options, self.native, self.generations = {}, {}, {}, {}
        self.turns, self.completed, self.items, self.requests, self.settings = {}, {}, {}, {}, {}
        self.seen = {}
        self.request_history, self.answered, self.settled = {}, set(), set()
        self.lock, self.catalog_lock = threading.RLock(), threading.Lock()
        self.permission_catalogs = {}
        self.closed = False
        with store._connection() as db:
            db.execute("CREATE TABLE IF NOT EXISTS muse_sessions (thread_id TEXT PRIMARY KEY, native_id TEXT NOT NULL, cwd TEXT NOT NULL)")
        self.usage = MuseUsage(store, on_event)

    def _emit(self, method, thread_id, **params):
        self.on_event({"method": method, "params": {"threadId": thread_id, **params}})

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
        mode = params.get("permissionMode") or legacy_mode("muse", params.get("approvalPolicy") == "never")
        return {"cwd": params["cwd"], "model": params.get("model"),
                "effort": params.get("effort") or params.get("config", {}).get("model_reasoning_effort"),
                "permission_mode": mode, "yolo": mode == ":unrestricted"}

    def permission_modes(self, cwd):
        # The native catalog carries managed availability and custom profiles.
        # A host cannot negotiate its shell sandbox after startup. The four
        # native built-ins have a known launch posture; unknown profiles cannot
        # be offered faithfully until the protocol also exposes their axes.
        with self.catalog_lock:
            if cwd in self.permission_catalogs:
                return copy.deepcopy(self.permission_catalogs[cwd])
            probe = self.connection_factory(self.store, lambda message: None, {"cwd": cwd})
            try:
                probe.initialize()
                result = probe.request("permissionProfile/list", {})
                catalog = []
                for entry in result.get("profiles", []):
                    native_id = entry.get("profileId")
                    if not isinstance(native_id, str) or not native_id:
                        continue
                    known = native_id in {":read-only", ":ask-me", ":auto-review", ":unrestricted"}
                    available = entry.get("available") is True and known
                    reason = ("This runtime does not expose the custom profile's shell sandbox requirements."
                              if not known else ", ".join(entry.get("unavailableReasons", [])))
                    catalog.append({"id": native_id, "label": entry.get("label") or native_id,
                                    "description": entry.get("description") or "", "available": available,
                                    **({"reason": reason or "The native runtime excludes this profile."} if not available else {})})
                if not catalog:
                    raise CoordinationError("Muse did not advertise permission profiles. Update Muse Code.")
                self.permission_catalogs[cwd] = catalog
                return copy.deepcopy(catalog)
            finally:
                probe.close()

    def permission_state(self, thread_id):
        return self.options.get(thread_id, {}).get("permission_mode") if thread_id in self.connections else None

    @staticmethod
    def _command(connection, method, **params):
        return connection.request(method, {"commandId": command_id(), **params})

    @staticmethod
    def _input(parts):
        inputs = []
        for part in parts:
            if part["type"] == "text":
                inputs.append({"type": "text", "text": part["text"]})
            elif part["type"] == "image":
                header, data = part["url"].split(",", 1)
                inputs.append({"type": "image", "mediaType": header[5:].split(";")[0], "base64Data": data})
        return inputs

    def _connection(self, thread_id, options):
        current = self.connections.get(thread_id)
        if current and (current.process.poll() is not None or self.options.get(thread_id) != options):
            self._shutdown(thread_id)
            current = None
        if current:
            return current
        with self.store._connection() as db:
            row = db.execute("SELECT native_id, cwd FROM muse_sessions WHERE thread_id = ?", (thread_id,)).fetchone()
        if row and row["cwd"] != options["cwd"]:
            raise CoordinationError("Muse sessions keep their original workspace. Start a new session in the new directory.")
        generation = object()
        self.generations[thread_id] = generation
        connection = self.connection_factory(self.store,
            lambda message: self._message(thread_id, generation, message), {**options, "threadId": thread_id})
        self.connections[thread_id] = connection
        self.native[thread_id] = row["native_id"] if row else thread_id
        self.seen[thread_id] = set()
        profile = options["permission_mode"]
        try:
            connection.initialize()
            if row:
                result = self._command(connection, "session/resume", sessionId=row["native_id"],
                                       excludeItems=True, permissionProfile=profile)
                if result["session"].get("activeTurnId"):
                    raise CoordinationError("Muse reports an existing active turn. Stop it in Muse before resuming here.")
            else:
                params = {"sessionId": thread_id, "workspaceRoot": options["cwd"],
                          "workspaceRoots": list(dict.fromkeys([options["cwd"], str(self.store.database_path.parent)])),
                          "permissionProfile": profile}
                if options.get("model"):
                    params["modelId"] = options["model"]
                result = self._command(connection, "session/start", **params)
            session = result["session"]
            native_id = session["sessionId"]
            if native_id != self.native[thread_id]:
                raise CoordinationError("Muse returned a different session identity; refusing to replace the conversation.")
            self.usage.snapshot(thread_id, native_id, result)
            # Preserve the native identity even when a subsequent setting fails.
            with self.store._connection() as db:
                db.execute("INSERT OR REPLACE INTO muse_sessions VALUES (?, ?, ?)", (thread_id, native_id, options["cwd"]))
            # Approval mode alone does not change the filesystem/network policy.
            # Prove the native profile was committed before admitting any turn.
            state = session.get("permissionState") or {}
            if not state.get("profileId"):
                snapshot = self._command(connection, "session/read", sessionId=native_id)["session"]
                state = snapshot.get("permissionState") or {}
            if state.get("profileId") != profile:
                raise CoordinationError(f"Muse did not apply the requested {profile} permission profile; no turn was started.")
            if row:
                if options.get("model") and options["model"] != session.get("modelId"):
                    self._command(connection, "session/setModel", sessionId=native_id, model={"modelId": options["model"]})
            if options.get("effort"):
                self._command(connection, "session/setReasoningEffort", sessionId=native_id, reasoningEffort=options["effort"])
            self.settings[thread_id] = (options.get("model") or session.get("modelId"), options.get("effort"))
            self.options[thread_id] = {**options, "model": self.settings[thread_id][0]}
            return connection
        except Exception:
            self._shutdown(thread_id)
            raise

    def _models(self):
        # Refresh instead of retaining an unauthenticated/old subscription catalog.
        with self.catalog_lock:
            root = self.store.database_path.parent / "providers/muse"
            root.mkdir(parents=True, exist_ok=True)
            probe = self.connection_factory(self.store, lambda message: None, {"cwd": str(root)})
            try:
                probe.initialize()
                entries = probe.request("model/list", {}).get("models", [])
                catalog = []
                for entry in entries:
                    descriptions = {v["tier"]: v.get("description", "") for v in entry.get("reasoningEffortVariants", [])}
                    variants = entry.get("variants")
                    if not isinstance(variants, list):
                        variants = list(descriptions)
                    catalog.append({"id": entry["modelId"], "model": entry["modelId"],
                        "displayName": entry.get("displayLabel", entry["modelId"]),
                        "description": entry.get("description") or "", "isDefault": entry.get("isDefault", False),
                        "defaultReasoningEffort": entry.get("defaultReasoningEffort"),
                        "supportedReasoningEfforts": [{"reasoningEffort": v, "description": descriptions.get(v, "")}
                                                       for v in variants]})
                if not catalog:
                    raise CoordinationError("Muse did not advertise any models. Run muse login, verify your model access, then try again.")
                return catalog
            finally:
                probe.close()

    def request(self, method, params=None):
        if self.closed:
            raise CoordinationError("Muse connection is closed. Restart Ribbon Field.")
        params = params or {}
        thread_id = params.get("threadId")
        if method == "model/list":
            return {"data": self._models()}
        if method == "thread/start":
            thread_id = command_id()
            # Native startup housekeeping reclaims sessions without a first
            # turn. Reserve the app identity now; open Muse only when sending.
            options = self._options(params)
            return {"thread": self._history(thread_id), "model": options["model"],
                    "reasoningEffort": options["effort"]}
        if method in {"thread/resume", "thread/read"}:
            return {"thread": self._history(thread_id)}
        if method in {"thread/archive", "thread/unarchive", "thread/name/set"}:
            if method == "thread/archive":
                self._shutdown(thread_id)
            return {}
        if method == "turn/steer":
            # Keep completion behind the acknowledgement and display update.
            # MuseConnection drains RPC responses independently of its events.
            with self.lock:
                turn = self.turns.get(thread_id)
                if not turn or turn["id"] != params.get("expectedTurnId"):
                    raise CoordinationError("The active Muse turn changed or finished. Your message was not sent.")
                connection = self.connections.get(thread_id)
                if not connection:
                    raise CoordinationError("Muse disconnected. Stop the turn and send again.")
                identity = command_id()
                result = connection.request("turn/steer", {"commandId": identity,
                    "sessionId": self.native[thread_id], "expectedTurnId": turn["id"],
                    "input": self._input(params["input"])})
                if (result.get("commandId") != identity or result.get("turnId") != turn["id"]
                        or result.get("status") != "accepted"):
                    raise CoordinationError("Muse did not confirm steering the expected turn. Review the native session before retrying.")
                user = {"id": str(uuid.uuid4()), "type": "userMessage", "content": params["input"]}
                turn["items"].append(user)
                self._emit("item/completed", thread_id, turnId=turn["id"], item=copy.deepcopy(user))
                return {"turnId": turn["id"]}
        if method == "turn/start":
            if thread_id in self.turns:
                raise CoordinationError("A Muse turn is already running.")
            connection = self._connection(thread_id, self._options(params))
            # Explicit identity also works in native tools that sanitize their environment.
            command = shlex.join(["env", f"AGENT_COORD_SESSION_ID={thread_id}", f"AGENT_COORD_DB={self.store.database_path}", "agent-coord"])
            context = coordination_bootstrap(self.store, thread_id, command=command)
            if not self.options[thread_id]["yolo"] and not self.store.database_path.resolve().is_relative_to(Path(params["cwd"]).resolve()):
                context += ("\nFor Agent Coord CLI calls, set the native bash tool's sandbox_permissions to "
                            "require_escalated so Muse can ask for approval to access the coordination database "
                            "outside this workspace. An ordinary sandboxed-command approval does not grant "
                            "that filesystem access.")
            inputs = [{"type": "text", "text": context}, *self._input(params["input"])]
            turn = {"id": command_id(), "status": "inProgress", "items": [
                {"id": str(uuid.uuid4()), "type": "userMessage", "content": params["input"]}]}
            self.turns[thread_id] = turn
            self.items[thread_id] = {}
            self.request_history = {k: v for k, v in self.request_history.items() if k[0] != thread_id}
            self.answered = {k for k in self.answered if k[0] != thread_id}
            self.settled = {k for k in self.settled if k[0] != thread_id}
            self._emit("turn/started", thread_id, turn=copy.deepcopy(turn))
            try:
                result = connection.request("turn/start", {"commandId": turn["id"], "sessionId": self.native[thread_id],
                    "input": inputs, "displayText": "\n".join(p.get("text", "[Image]") for p in params["input"])})
                if result.get("turnId") != turn["id"] or result.get("disposition") not in {"started", "queued"}:
                    raise CoordinationError("Muse did not start the expected turn. Review the native session before retrying.")
            except Exception as exc:
                self._shutdown(thread_id)
                self._finish(thread_id, "failed", str(exc))
                raise
            model, effort = self.settings[thread_id]
            return {"turn": copy.deepcopy(turn), "model": model, "reasoningEffort": effort}
        if method == "turn/interrupt":
            turn = self.turns.get(thread_id)
            if not turn or turn["id"] != params["turnId"]:
                raise CoordinationError("The active Muse turn changed or finished.")
            try:
                self._command(self.connections[thread_id], "turn/interrupt", sessionId=self.native[thread_id], turnId=turn["id"])
            finally:
                self._shutdown(thread_id)
                self._finish(thread_id, "interrupted")
            return {}
        raise CoordinationError(f"Muse does not support {method}.")

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
            self.completed[thread_id] = copy.deepcopy(turn)
            self.requests = {k: v for k, v in self.requests.items() if v["thread"] != thread_id}
        self._emit("turn/completed", thread_id, turn=copy.deepcopy(turn))

    @staticmethod
    def _item(native):
        item = {"id": native["itemId"], "status": native.get("status", "inProgress")}
        kind = native["kind"]
        if kind == "agentMessage":
            return {**item, "type": kind, "text": native.get("text", ""), "phase": native.get("messagePhase", "finalAnswer")}
        if kind == "reasoning":
            return {**item, "type": kind, "summary": native.get("summary") or [native.get("text", "")]}
        args = native.get("args", "{}")
        try:
            args = json.loads(args)
        except (TypeError, ValueError):
            pass
        return {**item, "type": "mcpToolCall", "tool": native.get("tool") or native.get("label") or kind,
                "arguments": args, "result": native.get("visibleOutput") or native.get("failureReason") or native.get("fallbackText", "")}

    def _message(self, thread_id, generation, message):
        if self.generations.get(thread_id) is not generation:
            return
        method, params = message.get("method"), message.get("params") or {}
        if method == "disconnected":
            self._finish(thread_id, "failed", params.get("error", "Muse disconnected."))
            return
        if "id" in message:
            if method in {"approval/request", "userInput/request"} and params.get("sessionId") == self.native.get(thread_id):
                self.connections[thread_id].write({"id": message["id"], "result": {}})
                method = method + "ed"
            else:
                self.connections[thread_id].write({"id": message["id"], "error": {"code": -32601, "message": "Unsupported client request"}})
                return
        if params.get("sessionId") != self.native.get(thread_id):
            return
        # Retain native generation/identity checks, but do not drop telemetry
        # merely because it belongs to replay or an already-finished UI turn.
        self.usage.capture(thread_id, self.native[thread_id], method, params)
        if method == "session/closed":
            self._shutdown(thread_id)
            self._finish(thread_id, "failed", "Muse unloaded the session. Send again to resume.")
            return
        # A cursor can carry several kinds of event; do not order opaque cursors.
        cursor = params.get("viewCursor")
        key = (method, cursor,
               params.get("approvalId") or params.get("userInputId") or params.get("itemId") or params.get("item", {}).get("itemId"),
               params.get("currentRequirementId", {}).get("sourceIndex"),
               params.get("change", {}).get("kind"),
               params.get("change", {}).get("requirementId", {}).get("sourceIndex"))
        if cursor:
            if key in self.seen[thread_id]:
                return
            self.seen[thread_id].add(key)
        turn = self.turns.get(thread_id)
        if not turn:
            return  # Native history replay is already in our persisted display mirror.
        if params.get("turnId") and params["turnId"] != turn["id"]:
            return
        if method == "view/gap":
            self._shutdown(thread_id)
            self._finish(thread_id, "failed", "Muse lost conversation events. Stop and resume the session before continuing.")
        elif method == "turn/completed":
            status = {"completed": "completed", "failed": "failed", "cancelled": "interrupted"}.get(params.get("terminal"), "failed")
            error = (params.get("error") or {}).get("message") or params.get("reason")
            if status == "completed" and not any(i.get("type") == "agentMessage" and i.get("text", "").strip() or
                                                i.get("type") == "mcpToolCall" for i in turn["items"]):
                status, error = "failed", "Muse completed without a response. Check workspace trust, login, and hook configuration."
            self._finish(thread_id, status, error)
        elif method in {"approval/requested", "approval/updated", "userInput/requested"}:
            self._permission(thread_id, method, params)
        elif method in {"approval/resolved", "userInput/settled"}:
            native_id = params.get("approvalId") or params.get("userInputId")
            self.settled.add((thread_id, native_id))
            self._resolve(thread_id, native_id)
        elif method in {"item/started", "item/updated", "item/completed", "item/delta"}:
            with self.lock:
                items = self.items[thread_id]
                if method == "item/delta":
                    native = items.get(params.get("itemId"))
                    if not native:
                        return
                    field = params.get("field", "text")
                    if field == "output":
                        field = "visibleOutput"
                    if field in {"text", "visibleOutput", "args"}:
                        native[field] = native.get(field, "") + params["delta"]
                    elif re.fullmatch(r"summary\.\d+", field):
                        index = int(field.split(".")[1])
                        if index > 100:
                            return
                        summary = native.setdefault("summary", [])
                        while len(summary) <= index:
                            summary.append("")
                        summary[index] += params["delta"]
                    else:
                        return
                else:
                    native = copy.deepcopy(params["item"])
                    if native.get("turnId") not in {None, turn["id"]} or native["kind"] == "userMessage":
                        return
                    previous = items.get(native["itemId"])
                    if previous and native.get("revision", 0) <= previous.get("revision", 0):
                        return
                    items[native["itemId"]] = native
                item = self._item(native)
                index = next((i for i, old in enumerate(turn["items"]) if old["id"] == item["id"]), None)
                if index is None:
                    turn["items"].append(item)
                else:
                    turn["items"][index] = item
            self._emit("item/completed" if item["status"] != "inProgress" else "item/started", thread_id,
                       turnId=turn["id"], item=copy.deepcopy(item))

    def _resolve(self, thread_id, native_id):
        for key, pending in list(self.requests.items()):
            if pending["thread"] == thread_id and pending["native_id"] == native_id:
                self.requests.pop(key, None)
                self._emit("serverRequest/resolved", thread_id, requestId=key)

    def _permission(self, thread_id, method, params):
        question = method == "userInput/requested"
        native_id = params.get("userInputId") if question else params["approvalId"]
        identity = (thread_id, native_id)
        if identity in self.settled:
            return
        old = self.request_history.get(identity)
        if method == "approval/updated":
            if not old:
                return  # Native review progress can precede the actionable request.
            params = {**old, **params}
        self.request_history[identity] = copy.deepcopy(params)
        requirement = params.get("currentRequirementId", {})
        if not question and (*identity, requirement.get("sourceIndex")) in self.answered:
            return
        if not question and params.get("change", {}).get("kind") == "stageResolved":
            resolved = params["change"].get("requirementId")
            if not resolved or resolved == requirement:
                self._resolve(thread_id, native_id)
                return
            # The snapshot already offers the next stage. Native requests can
            # reuse the approval's opening cursor across all requirements.
        self._resolve(thread_id, native_id)
        key = f"muse:{thread_id}:{native_id}:{uuid.uuid4()}"
        display = {"threadId": thread_id, "turnId": self.turns[thread_id]["id"]}
        choices = {}
        if question:
            display.update(allowCancel=True, questions=[{**q, "allowCustomAnswer": q.get("selection", {}).get("mode") != "multiple",
                "multiSelect": q.get("selection", {}).get("mode") == "multiple"} for q in params["questions"]])
        else:
            decisions = {"approved": "accept", "approvedForSession": "acceptForSession", "denied": "decline", "abort": "cancel"}
            for choice in params.get("availableChoices", []):
                decision = decisions.get(choice.get("decision"))
                if decision and choice.get("scope") in {"once", "session"}:
                    choices.setdefault(decision, choice["choiceId"])
            reason = params.get("toolName", "Muse tool request")
            args = params.get("rawArgs", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            if isinstance(args, dict) and args.get("sandbox_permissions") == "require_escalated":
                reason = "Run outside the workspace sandbox"
                justification = args.get("justification")
                if isinstance(justification, str) and justification.strip():
                    reason += ": " + justification.strip()
            display.update(reason=reason,
                command=params.get("subject", {}).get("command") or params.get("rawArgs", ""),
                availableDecisions=list(choices) + ([] if "cancel" in choices else ["cancel"]))
        self.requests[key] = {"thread": thread_id, "native_id": native_id, "request": copy.deepcopy(params),
                              "question": question, "choices": choices}
        self.on_event({"id": key, "method": "item/tool/requestUserInput" if question else "item/commandExecution/requestApproval", "params": display})

    def write(self, message):
        pending = self.requests.get(message.get("id"))
        if not pending:
            raise CoordinationError("This Muse request changed or is no longer pending.")
        thread_id, request = pending["thread"], pending["request"]
        result = message.get("result", {})
        cancelled = result.get("decision") == "cancel"
        params = {"sessionId": self.native[thread_id]}
        if pending["question"]:
            params["userInputId"] = pending["native_id"]
            method = "userInput/cancel" if cancelled else "userInput/answer"
            if not cancelled:
                answers = result.get("answers", {})
                if set(answers) != {q["id"] for q in request["questions"]}:
                    raise CoordinationError("Answer every Muse question.")
                params["answers"] = []
                for question in request["questions"]:
                    values = answers[question["id"]].get("answers", [])
                    labels = {o["label"] for o in question["options"]}
                    selection = question.get("selection", {})
                    multiple = selection.get("mode") == "multiple"
                    if not values or any(not isinstance(v, str) or not v.strip() for v in values) or len(values) != len(set(values)):
                        raise CoordinationError("Choose a valid Muse answer.")
                    if all(v in labels for v in values):
                        if not (selection.get("minSelections", 1) <= len(values) <= selection.get("maxSelections", len(labels) if multiple else 1)):
                            raise CoordinationError("Choose the number of answers requested by Muse.")
                        answer = {"selectedLabels": values} if multiple else {"selectedLabel": values[0]}
                    elif len(values) == 1 and len(values[0]) <= 500:
                        answer = {"freeText": values[0]}
                    else:
                        raise CoordinationError("Custom Muse answers must be one response of at most 500 characters.")
                    params["answers"].append({"questionId": question["id"], **answer})
        else:
            choice = pending["choices"].get(result.get("decision"))
            if not choice:
                if cancelled:
                    self.request("turn/interrupt", {"threadId": thread_id, "turnId": self.turns[thread_id]["id"]})
                    return
                raise CoordinationError("This decision is not offered by Muse.")
            method = "approval/decide"
            params.update(approvalId=pending["native_id"], requirementId=request["currentRequirementId"], choiceId=choice)
        receipt = self._command(self.connections[thread_id], method, **params)
        identity = (thread_id, pending["native_id"])
        if pending["question"] or receipt.get("terminal"):
            self.settled.add(identity)
        else:
            self.answered.add((*identity, request["currentRequirementId"]["sourceIndex"]))
        self.requests.pop(message["id"], None)

    def _shutdown(self, thread_id):
        self.generations.pop(thread_id, None)
        connection = self.connections.pop(thread_id, None)
        if connection:
            connection.close()

    def close(self):
        self.closed = True
        for thread_id in list(self.connections):
            self._shutdown(thread_id)
            self._finish(thread_id, "interrupted")
