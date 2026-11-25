"""Google Antigravity ACP transport and Ribbon Field conversation adapter.

Uses the official ACP runtime, not the Antigravity IDE or Gemini CLI. Protocol
behavior follows T3 Code's Antigravity adapter and the ACP v1 specification.
"""
from __future__ import annotations

from .history_persistence import read_history

import copy
import json
import logging
import os
import shlex
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from .acp import (
    ACPConnection,
    ACPReplayCollector,
    accepts_keyword,
    reconcile_replayed_history,
    run_or_capture_write,
    transport_handover_active,
)
from .antigravity_setup import model_catalog, provider_directory, resolve_runtime
from .antigravity_skills import prepare_skills
from .native_prompt import NativePromptState
from .context import client_environment, cli_path, coordination_bootstrap
from .conversation_sync import ConversationSync
from .message_delivery import MessageDelivery
from .store import CoordinationError
from .permission_modes import acp_modes, acp_current_mode, legacy_mode, validate_mode

AUTH_PREFIX = "Open the following link to authenticate the ACP server: "
BROWSER_MARKER = "RIBBON_FIELD_AUTH_URL="
SIGN_IN = "Sign in to Antigravity with agent-coord antigravity login on this machine, then try again."


class _ProcessScratchDirectory:
    """Scratch directory for an ACP process that survives coordinator handover."""

    def __init__(self, path: Path, store):
        self.path = Path(path)
        self.name = str(self.path)
        self.store = store

    def cleanup(self) -> None:
        if not transport_handover_active(self.store):
            shutil.rmtree(self.path, ignore_errors=True)


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
        self.on_auth_failure = options.get("on_auth_failure")
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
        scratch_path = Path(tempfile.mkdtemp(prefix="process-", dir=root))
        self.temporary = _ProcessScratchDirectory(scratch_path, store)
        env.update({key: self.temporary.name for key in ("TMPDIR", "TMP", "TEMP")})
        try:
            super().__init__(
                callback,
                command=command,
                cwd=options["cwd"],
                env=env,
                store=store,
                provider="antigravity",
                thread_id=options.get("threadId"),
            )
        except Exception:
            shutil.rmtree(scratch_path, ignore_errors=True)
            raise

    def _init_attached(self, *, options=None, on_auth_failure=None, scratch_dir=None, **kwargs):
        self.options = dict(options or {})
        self.auth_urls = set()
        self.on_auth_failure = on_auth_failure
        self.temporary = (
            _ProcessScratchDirectory(Path(scratch_dir), self.store)
            if scratch_dir
            else None
        )

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
            error = CoordinationError("Antigravity returned an invalid Google sign-in URL.")
            self._fail(error)
            if self.on_auth_failure is not None:
                self.on_auth_failure(error)
        elif not self.options.get("authorize"):
            error = CoordinationError(SIGN_IN)
            self._fail(error)
            if self.on_auth_failure is not None:
                self.on_auth_failure(error)
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
            if self.temporary is not None:
                self.temporary.cleanup()


class AntigravityRPC:
    def __init__(self, store, on_event, *, connection_factory=AntigravityConnection):
        self.store, self.on_event, self.connection_factory = store, on_event, connection_factory
        self.connections, self.options, self.turns, self.requests, self.native = {}, {}, {}, {}, {}
        self.completed = {}
        self.resolved_models = {}
        self.catalog = None
        self.permission_catalogs = {}
        self.effective_permissions = {}
        self.closed = False
        self.lock = threading.RLock()
        self.catalog_lock = threading.Lock()
        self.delivery = MessageDelivery(store)
        self.conversation_sync = ConversationSync()
        self.delivery_turns = {}
        self.last_activity = {}
        self.prompt_locks = {}
        self.skill_contexts = {}
        self.active_request_ids = {}
        self.prompt_futures = {}
        self.turn_events = {}
        self._replay_collectors = {}
        # Turns a backend update is stopping; their late requests are refused.
        self.update_stopping = set()
        with store._connection() as db:
            self.initial_prompt = NativePromptState(store, "antigravity", db)
            db.execute("CREATE TABLE IF NOT EXISTS antigravity_sessions (thread_id TEXT PRIMARY KEY, native_id TEXT NOT NULL)")
            columns = {row[1] for row in db.execute("PRAGMA table_info(antigravity_sessions)")}
            for column in ("channel", "native_session_id", "turn_id", "request_id", "options_json", "pending_approvals_json"):
                if column not in columns:
                    db.execute(f"ALTER TABLE antigravity_sessions ADD COLUMN {column} TEXT")

    def _emit(self, method, thread_id, **params):
        self.on_event({"method": method, "params": {"threadId": thread_id, **params}})

    def _auth_failed(self, thread_id, error):
        if not thread_id:
            return
        self._finish(
            thread_id,
            "failed",
            str(error),
            termination={"origin": "runtime", "reason": "authorization_required", "execution": "unknown"},
        )

    def _persist_session_state(self, thread_id: str) -> None:
        with self.lock:
            native_id = self.native.get(thread_id)
            if not native_id:
                return
            connection = self.connections.get(thread_id)
            channel = getattr(connection, "channel", None)
            scratch_dir = getattr(getattr(connection, "temporary", None), "name", None)
            turn = self.turns.get(thread_id)
            turn_id = turn["id"] if turn else None
            request_id = self.active_request_ids.get(thread_id) if turn_id else None
            options_data = {
                **(self.options.get(thread_id) or {}),
                "_resolved_model": self.resolved_models.get(thread_id),
                "_effective_permission": self.effective_permissions.get(thread_id),
                "_scratch_dir": scratch_dir,
            }
            approvals_data = {
                key: {"request_id": req_id, "request": req}
                for key, (tid, req_id, req) in self.requests.items()
                if tid == thread_id
            }
        options_json = json.dumps(options_data, sort_keys=True)
        approvals_json = json.dumps(approvals_data, sort_keys=True)

        def write_row():
            with self.store.database_operation("antigravity.session_state"), self.store._connection() as db:
                db.execute(
                    """INSERT INTO antigravity_sessions (
                        thread_id, native_id, channel, native_session_id,
                        turn_id, request_id, options_json, pending_approvals_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(thread_id) DO UPDATE SET
                        native_id = excluded.native_id,
                        channel = excluded.channel,
                        native_session_id = excluded.native_session_id,
                        turn_id = excluded.turn_id,
                        request_id = excluded.request_id,
                        options_json = excluded.options_json,
                        pending_approvals_json = excluded.pending_approvals_json""",
                    (thread_id, native_id, channel, native_id, turn_id, request_id, options_json, approvals_json),
                )

        run_or_capture_write(write_row, required=True)

    @classmethod
    def _approval_display(cls, thread_id: str, turn_id: str | None, request: dict) -> tuple[str, dict]:
        tool = request.get("toolCall", {})
        options = [o for o in request.get("options", []) if isinstance(o.get("optionId"), str) and o["optionId"]]
        question = str(tool.get("toolCallId", "")).startswith("interaction_")
        params: dict = {"threadId": thread_id, "turnId": turn_id}
        if question and options:
            method = "item/tool/requestUserInput"
            params["questions"] = [{
                "id": tool["toolCallId"],
                "question": tool.get("title", "Choose an option"),
                "allowCustomAnswer": False,
                "options": [{"value": o["optionId"], "label": o.get("name", o["optionId"])} for o in options],
            }]
        else:
            method = "item/commandExecution/requestApproval"
            decisions = {"allow_once": "accept", "allow_always": "acceptForSession", "reject_once": "decline"}
            params.update(
                reason=tool.get("title", "Antigravity tool request"),
                command=json.dumps(cls._bounded(tool.get("rawInput", tool.get("content", {}))), indent=2),
                availableDecisions=(
                    [decisions[o["kind"]] for o in options if o.get("kind") in decisions]
                    if not question
                    else []
                ) + ["cancel"],
            )
        return method, params

    def attach_channel(self, thread_id, channel, pid, *, returncode=None):
        """Rebind a surviving Antigravity ACP channel during CoordinatorProjection.load()."""
        projection = getattr(self.store, "provider_projection", None)
        if not thread_id:
            attached = self.connection_factory.attach(
                self.store, lambda _: None, None, channel, pid, provider="antigravity", returncode=returncode
            )
            if returncode is None and projection is not None:
                projection.close_channel(channel)
            return attached

        with self.store.reading(), self.store._connection() as db:
            row = db.execute("SELECT * FROM antigravity_sessions WHERE thread_id = ?", (thread_id,)).fetchone()
            prompt_row = None
            if row is not None:
                native_id = row["native_session_id"] or row["native_id"]
                prompt_row = db.execute(
                    "SELECT started FROM native_prompt_initialization WHERE provider='antigravity' AND thread_id=? AND native_id=?",
                    (thread_id, native_id),
                ).fetchone()
                browser_row = (
                    db.execute("SELECT cwd, model, permission_mode, yolo FROM browser_sessions WHERE thread_id=?", (thread_id,)).fetchone()
                    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='browser_sessions'").fetchone()
                    else None
                )
        if row is None:
            attached = self.connection_factory.attach(
                self.store, lambda _: None, thread_id, channel, pid, provider="antigravity", returncode=returncode
            )
            if returncode is None and projection is not None:
                projection.close_channel(channel)
            return attached

        native_id = row["native_session_id"] or row["native_id"]
        saved_options = json.loads(row["options_json"]) if row["options_json"] else {}
        resolved_model = saved_options.pop("_resolved_model", None)
        effective_permission = saved_options.pop("_effective_permission", None)
        scratch_dir = saved_options.pop("_scratch_dir", None)
        if not saved_options and browser_row is not None:
            mode = browser_row["permission_mode"] or legacy_mode("antigravity", bool(browser_row["yolo"]))
            saved_options = {
                "cwd": browser_row["cwd"],
                "model": browser_row["model"],
                "permission_mode": mode,
                "yolo": mode == "yolo",
            }
        active_turn_id = row["turn_id"]
        active_request_id = row["request_id"]
        approvals = json.loads(row["pending_approvals_json"]) if row["pending_approvals_json"] else {}
        saved_history = read_history(self.store, thread_id) if active_turn_id else None

        connection = None

        def on_message(message):
            if connection is not None and self.connections.get(thread_id) is connection:
                self._message(thread_id, message)

        connection = self.connection_factory.attach(
            self.store,
            on_message,
            thread_id,
            channel,
            pid,
            provider="antigravity",
            returncode=returncode,
            options=saved_options,
            on_auth_failure=lambda error: self._auth_failed(thread_id, error),
            scratch_dir=scratch_dir,
        )

        with self.lock:
            self.connections[thread_id] = connection
            self.native[thread_id] = native_id
            if saved_options:
                self.options[thread_id] = dict(saved_options)
            if resolved_model is not None:
                self.resolved_models[thread_id] = resolved_model
            if effective_permission is not None:
                self.effective_permissions[thread_id] = effective_permission
            elif saved_options.get("permission_mode"):
                self.effective_permissions[thread_id] = saved_options["permission_mode"]
            if prompt_row is not None and not prompt_row[0]:
                self.initial_prompt.pending[thread_id] = native_id
            else:
                self.initial_prompt.pending.pop(thread_id, None)

            if active_turn_id:
                existing_turn = next(
                    (
                        copy.deepcopy(t)
                        for t in (saved_history or {}).get("turns", [])
                        if t.get("id") == active_turn_id
                    ),
                    None,
                )
                if existing_turn is None:
                    existing_turn = {"id": active_turn_id, "status": "inProgress", "items": []}
                else:
                    existing_turn["status"] = "inProgress"
                    existing_turn.pop("termination", None)
                self.turns[thread_id] = existing_turn
                self.turn_events[active_turn_id] = threading.Event()
                self.delivery_turns[thread_id] = {"id": active_turn_id, "batch": None, "accepted": True}
                if active_request_id:
                    self.active_request_ids[thread_id] = active_request_id
                    future = connection.bind_pending_request(
                        active_request_id,
                        on_response=lambda outcome, tid=thread_id, t_id=active_turn_id: self._prompt_done(tid, t_id, outcome),
                    )
                    self.prompt_futures[thread_id] = future

            for key, entry in approvals.items():
                self.requests[key] = (thread_id, entry["request_id"], entry["request"])

        sessions = getattr(projection, "sessions", None) if projection is not None else None
        if sessions is not None:
            with sessions.lock:
                sessions.loaded.add(thread_id)
                sessions.loaded_clients[thread_id] = "antigravity"
                if active_turn_id:
                    sessions.active[thread_id] = active_turn_id
                existing_ids = {r.get("id") for r in sessions.requests.values()}
                for key, entry in approvals.items():
                    if key not in existing_ids:
                        method, params = self._approval_display(thread_id, active_turn_id, entry["request"])
                        ui_key = str(uuid.uuid4())
                        sessions.requests[ui_key] = {
                            "id": key,
                            "method": method,
                            "params": params,
                            "key": ui_key,
                            "received_at": self.store.clock(),
                        }
        return connection

    def _context_usage(self, thread_id, update):
        if any(type(update.get(key)) is not int or update[key] < 0 for key in ("used", "size")):
            return
        native_id = self.native.get(thread_id)
        if not native_id:
            return

        def record_usage():
            try:
                self.store.usage.record_unavailable(
                    thread_id,
                    provider="antigravity",
                    native_id=native_id,
                    note="Antigravity ACP reports context occupancy; cumulative token consumption is unsupported",
                )
                self.store.usage.record_context(
                    thread_id,
                    provider="antigravity",
                    native_id=native_id,
                    used=update.get("used"),
                    limit=update.get("size"),
                )
                self._emit(
                    "thread/usage/updated",
                    thread_id,
                    usage=self.store.usage.summary(thread_id, provider="antigravity"),
                )
            except Exception:
                logging.getLogger(__name__).warning("Could not capture Antigravity context usage")

        run_or_capture_write(record_usage, required=False)

    def _history(self, thread_id, *, include_turns=True):
        if not include_turns:
            with self.lock:
                return {"id": thread_id, "turns": [],
                        "status": {"type": "active" if thread_id in self.turns else "idle"}}
        history = read_history(self.store, thread_id)
        if history is None:
            history = {"id": thread_id, "turns": []}
        return self.reconcile_history(thread_id, history)

    def reconcile_history(self, thread_id, history):
        """Reconcile a caller-owned mirror without reading persisted history."""
        with self.lock:
            active = self.turns.get(thread_id)
            latest = active or self.completed.get(thread_id)
            if latest:
                turns = history.setdefault("turns", [])
                index = next((index for index, turn in enumerate(turns) if turn["id"] == latest["id"]), None)
                if index is None:
                    turns.append(copy.deepcopy(latest))
                elif active or turns[index].get("status") not in {"completed", "failed", "interrupted"}:
                    # A terminal mirror can include later events than the
                    # retained completion. Use that cache only for recovery.
                    turns[index] = copy.deepcopy(latest)
            if not active:
                for turn in history.get("turns", []):
                    if turn.get("status") == "inProgress":
                        turn["status"] = "interrupted"
                        turn["termination"] = {"origin": "runtime", "reason": "runtime_state_lost", "execution": "unknown"}
                    self._unconfirmed_tools(turn, "The session ended before Antigravity reported this tool's outcome.")
            history["status"] = {"type": "active" if active else "idle"}
            return history

    def discard_persisted_completion(self, thread_id, terminal_turns):
        """Release only the completion represented by a committed snapshot."""
        with self.lock:
            completed = self.completed.get(thread_id)
            if completed is not None and terminal_turns.get(completed["id"]) == completed["status"]:
                self.completed.pop(thread_id)

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

    def _options_match(self, thread_id, options) -> bool:
        saved = self.options.get(thread_id)
        if saved is None:
            return False
        resolved = self.resolved_models.get(thread_id)
        return {**saved, "model": saved.get("model") or resolved} == {
            **options,
            "model": options.get("model") or resolved,
        }

    def _connection(self, thread_id, options):
        current = self.connections.get(thread_id)
        if current and (current.closed or current.process.poll() is not None or not self._options_match(thread_id, options)):
            self.connections.pop(thread_id, None)
            current.close()
            current = None
        if current:
            return current
        with self.store._connection() as db:
            row = db.execute("SELECT native_id, native_session_id FROM antigravity_sessions WHERE thread_id = ?", (thread_id,)).fetchone()
        connection = None

        def on_message(message):
            # A retired process can still drain buffered output while closing.
            # It must never complete or append to a replacement process's turn.
            if connection is not None and self.connections.get(thread_id) is connection:
                self._message(thread_id, message)

        connection = self.connection_factory(
            self.store,
            on_message,
            {**options, "threadId": thread_id, "on_auth_failure": lambda error: self._auth_failed(thread_id, error)},
        )
        self.connections[thread_id] = connection
        try:
            initialized = connection.initialize()
            connection.request("authenticate", {"methodId": "oauth-personal"})
            params = {"cwd": options["cwd"], "mcpServers": []}
            method = "session/new"
            saved_native = (row["native_session_id"] or row["native_id"]) if row else None
            collector = None
            if saved_native:
                self.native[thread_id] = saved_native
                caps = initialized["agentCapabilities"]
                if "resume" in caps.get("sessionCapabilities", {}):
                    method = "session/resume"
                elif caps.get("loadSession"):
                    method = "session/load"
                else:
                    raise CoordinationError("This Antigravity runtime cannot resume saved sessions. Update it before continuing.")
                params["sessionId"] = saved_native
                collector = ACPReplayCollector(thread_id, provider="antigravity", unknown_empty_tool_result=True)
                self._replay_collectors[thread_id] = collector
            try:
                session = connection.request(method, params)
            finally:
                self._replay_collectors.pop(thread_id, None)
            native_id = saved_native if saved_native else session["sessionId"]
            # Save identity before settings requests; a failed selection must
            # never silently create a replacement native conversation.
            with self.store._connection() as db:
                db.execute(
                    """INSERT INTO antigravity_sessions (thread_id, native_id, channel, native_session_id)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(thread_id) DO UPDATE SET
                        native_id = excluded.native_id,
                        channel = excluded.channel,
                        native_session_id = excluded.native_session_id""",
                    (thread_id, native_id, getattr(connection, "channel", None), native_id),
                )
                self.initial_prompt.bind(db, thread_id, native_id, resumed=saved_native is not None)
            self.native[thread_id] = native_id
            if collector is not None:
                replayed_turns = collector.finalize()
                if reconcile_replayed_history(self.store, self.on_event, thread_id, replayed_turns):
                    self.initial_prompt.confirm(thread_id)
            if saved_native:
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
            self._persist_session_state(thread_id)
            if thread_id in self.initial_prompt.pending:
                self.skill_contexts[thread_id] = prepare_skills(self.store, options["cwd"])
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
            return {"thread": self._history(thread_id, include_turns=not params.get("excludeTurns", False)), "model": params.get("model"), "reasoningEffort": None}
        if method == "thread/read":
            return {"thread": self._history(thread_id, include_turns=params.get("includeTurns", True))}
        if method in {"thread/archive", "thread/unarchive", "thread/name/set"}:
            if method == "thread/archive":
                connection = self.connections.pop(thread_id, None)
                if connection:
                    connection.close()
                self._persist_session_state(thread_id)
            return {}
        if method == "turn/steer":
            raise CoordinationError("Antigravity does not support steering. Queue the message or stop the turn first.")
        if method == "turn/start":
            if thread_id in self.turns:
                raise CoordinationError("An Antigravity turn is already running.")
            self.update_stopping.discard(thread_id)
            options = self._options(params)
            connection = self._connection(thread_id, options)
            prompt = []
            initial = thread_id in self.initial_prompt.pending
            changes = self.store.threads.observe_context(thread_id, initial=initial)
            if initial:
                skills = self.skill_contexts.get(thread_id)
                if skills is None:
                    skills = self.skill_contexts[thread_id] = prepare_skills(self.store, options["cwd"])
                prompt.append({"type": "text", "text": coordination_bootstrap(
                    self.store, thread_id, skill_path=skills.coordination_skill) + "\n\n" + skills.instructions})
            elif changes:
                prompt.append({"type": "text", "text": "Updated coordination context: " + json.dumps(changes, sort_keys=True)})
            for part in params["input"]:
                if part["type"] == "text":
                    prompt.append({"type": "text", "text": part["text"]})
                elif part["type"] == "image":
                    header, data = part["url"].split(",", 1)
                    prompt.append({"type": "image", "mimeType": header[5:].split(";")[0], "data": data})
            turn = {"id": str(uuid.uuid4()), "status": "inProgress", "items": [
                {"id": str(uuid.uuid4()), "type": "userMessage", "content": params["input"]}]}
            notice = self.conversation_sync.begin(thread_id, turn["id"],
                "\n".join(part["text"] for part in params["input"] if part["type"] == "text"),
                allow_consent=not params.get("_coordination_delivery", False))
            if notice:
                prompt.insert(0, {"type": "text", "text": notice})
            with self.lock:
                self.turns[thread_id] = turn
                self.turn_events[turn["id"]] = threading.Event()
                self.delivery_turns[thread_id] = {"id": turn["id"], "batch": None, "accepted": False}
            self._emit("turn/started", thread_id, turn=copy.deepcopy(turn))
            try:
                self._submit_prompt(thread_id, turn["id"], connection, prompt, supplied=params.get('_coordination_delivery', False))
            except Exception as exc:
                self._finish(thread_id, "failed", str(exc),
                             termination={"origin": "runtime", "reason": "submission_error", "execution": "unknown"})
                raise
            return {"turn": copy.deepcopy(turn)}
        if method == "turn/interrupt":
            origin, reason = ("runtime", "backend_update") if params.get("runtimeUpdate") else ("user", "turn_interrupt")
            with self.lock:
                turn = self.turns.get(thread_id)
                if not turn or turn["id"] != params["turnId"]:
                    raise CoordinationError("The active Antigravity turn changed or finished.")
                pending_approval = any(value[0] == thread_id for value in self.requests.values())
                if params.get("runtimeUpdate"):
                    if pending_approval:
                        raise CoordinationError("Antigravity is waiting for an approval or answer; the update did not stop this turn.")
                    self.update_stopping.add(thread_id)
                turn_id = turn["id"]
                turn_event = self.turn_events.setdefault(turn_id, threading.Event())
                state = self.delivery_turns.get(thread_id)
                if state is not None:
                    state["interrupt_termination"] = {"origin": origin, "reason": reason}
                    if not params.get("runtimeUpdate"):
                        state["user_cancel_requested"] = True
                connection = self.connections.get(thread_id)
                native_id = self.native.get(thread_id)
            settled = False
            if connection is not None and native_id is not None:
                try:
                    connection.write({"method": "session/cancel", "params": {"sessionId": native_id}})
                except CoordinationError:
                    pass
                if isinstance(connection, ACPConnection) and not pending_approval:
                    settled = turn_event.wait(timeout=2.0)
            if not settled:
                self._finish(thread_id, "interrupted", expected_turn_id=turn_id,
                             termination={"origin": origin, "reason": reason, "execution": "unknown"})
                with self.lock:
                    if self.connections.get(thread_id) is connection:
                        self.connections.pop(thread_id, None)
                if connection is not None:
                    connection.close()
                self._persist_session_state(thread_id)
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
            run_or_capture_write(lambda: self.initial_prompt.confirm(thread_id), required=True)
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
                last_tool = max((index for index, item in enumerate(items)
                                 if item.get("type") == "mcpToolCall"), default=-1)
                answered = any(item.get("type") == "agentMessage" and item.get("text", "").strip()
                               for item in items[last_tool + 1:])
                delivery_state = self.delivery_turns.get(thread_id) or {}
                interrupt_termination = delivery_state.get("interrupt_termination")
                user_cancelled = delivery_state.get("user_cancel_requested", False)
                execution = "unknown" if any(i.get("type") == "mcpToolCall" and
                    i.get("status") in {"inProgress", "unknown"} for i in items) else "ended"
            if interrupt_termination is not None:
                finish("interrupted", reason=interrupt_termination["reason"],
                       origin=interrupt_termination["origin"], execution="unknown")
            elif reason in {"cancelled", "canceled"}:
                finish("interrupted", reason=reason, origin="user" if user_cancelled else "provider")
            elif reason in {"max_tokens", "max_turn_requests"}:
                finish("failed", "Antigravity reached " + reason.replace("_", " ") +
                       ". Review the saved work before continuing.", reason=reason, execution=execution)
            elif reason != "end_turn":
                finish("failed", f"Antigravity stopped the prompt ({reason}). Review the response and native session before continuing.",
                       reason=reason, execution=execution)
            elif not answered:
                finish("failed", "Antigravity ended the prompt without a response after its tools. "
                       "The connection has been reset. Send a message to continue in the same conversation; "
                       "review saved tool outcomes before repeating any actions.", reason=reason,
                       execution=execution if last_tool >= 0 else "unknown")
            else:
                self._confirm_delivery(thread_id)
                finish("completed", reason=reason, execution=execution)
        except Exception as exc:
            with self.lock:
                interrupt_termination = (self.delivery_turns.get(thread_id) or {}).get("interrupt_termination")
            if interrupt_termination is not None:
                finish("interrupted", reason=interrupt_termination["reason"],
                       origin=interrupt_termination["origin"], execution="unknown")
            else:
                finish("failed", str(exc), origin="runtime", reason="prompt_error")

    def _runtime_failure(self, thread_id):
        # ACP 1.3.0 emits these runtime diagnostics as assistant text followed
        # by end_turn, including when its inner agent has already died. Match
        # only its known diagnostic prefixes, after streaming chunks coalesce.
        with self.lock:
            items = self.turns.get(thread_id, {}).get("items", [])
            item = next((item for item in reversed(items) if item.get("type") == "agentMessage"), {})
            text = item.get("text", "").strip()
        if text.startswith("Agent connection was lost and could not be re-established:"):
            return text
        if text.startswith("Usage Limit Reached\n") and "You have reached your current quota for this period." in text:
            return text
        return None

    def _submit_prompt(self, thread_id, turn_id, connection, prompt, *, supplied=False):
        batch = None if supplied else self.delivery.prepare(thread_id, turn_id)
        if batch:
            prompt = [{"type": "text", "text": batch["text"]}, *prompt]
        request_id = str(uuid.uuid4())
        with self.lock:
            state = self.delivery_turns.get(thread_id)
            if self.closed or not state or state["id"] != turn_id:
                return  # Stop won before submission.
            self.delivery.attempted(turn_id, batch, provider_turn_id=turn_id)
            state.update(batch=batch, accepted=False)
            self.active_request_ids[thread_id] = request_id
        self._persist_session_state(thread_id)
        submit_kwargs = {"request_id": request_id} if accepts_keyword(connection.submit, "request_id") else {}
        use_on_response = accepts_keyword(connection.submit, "on_response")
        if use_on_response:
            submit_kwargs["on_response"] = lambda outcome, tid=thread_id, t_id=turn_id: self._prompt_done(tid, t_id, outcome)
        future = connection.submit("session/prompt", {"sessionId": self.native[thread_id], "prompt": prompt}, **submit_kwargs)
        with self.lock:
            self.prompt_futures[thread_id] = future
        if not use_on_response:
            future.add_done_callback(lambda result: self._prompt_done(thread_id, turn_id, result))

    def _confirm_delivery(self, thread_id):
        with self.lock:
            state = self.delivery_turns.get(thread_id)
            if not state or state["accepted"]:
                return
            turn_id, batch = state["id"], state["batch"]
            state["accepted"] = True
        if batch:
            run_or_capture_write(
                lambda: self.delivery.submitted(thread_id, turn_id, batch),
                required=True,
            )

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
            turn_event = self.turn_events.pop(turn["id"], None)
            if turn_event is not None:
                turn_event.set()
            self.active_request_ids.pop(thread_id, None)
            self.prompt_futures.pop(thread_id, None)
            state = self.delivery_turns.pop(thread_id, None)
            turn["status"] = status
            turn["termination"] = termination or {"origin": "runtime", "reason": "unknown", "execution": "unknown"}
            self._unconfirmed_tools(turn,
                "The turn ended before Antigravity reported this tool's outcome.")
            if error:
                turn["error"] = {"message": str(error)}
            # Closing a failed connection can delay the browser callback. Reads
            # must still see the actual outcome, not infer an interruption.
            self.completed[thread_id] = copy.deepcopy(turn)
            self.requests = {k: v for k, v in self.requests.items() if v[0] != thread_id}
            # A live ACP wrapper does not imply a healthy inner agent. Retire
            # failed processes before publishing completion, so the next user
            # send resumes the saved native session in a fresh process. Never
            # replay a failed prompt automatically: its tools may have run.
            connection = self.connections.pop(thread_id, None) if status == "failed" else None
        if state and status != "completed" and state.get("batch"):
            failure_reason = error or "The turn stopped before delivery was confirmed."
            run_or_capture_write(
                lambda: self.delivery.failed(turn["id"], state["batch"], failure_reason),
                required=True,
            )
        if len(turn["items"]) > 1:
            run_or_capture_write(lambda: self.initial_prompt.confirm(thread_id), required=True)
        if connection:
            connection.close()
        self._persist_session_state(thread_id)
        self.conversation_sync.finish(thread_id, turn)
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
        collector = self._replay_collectors.get(thread_id)
        if collector is not None and thread_id not in self.turns:
            collector.observe(update)
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

                def touch_activity():
                    try:
                        self.store.touch(thread_id)
                    except (sqlite3.Error, CoordinationError):
                        logging.getLogger(__name__).warning(
                            "Could not refresh Antigravity activity for %s; continuing provider stream",
                            thread_id, exc_info=True)

                run_or_capture_write(touch_activity, required=False)
        if kind == "config_option_update":
            self.catalog = model_catalog(update)
        with self.lock:
            turn = self.turns.get(thread_id)
            if not turn:
                return  # Native replay outside session/load must not duplicate display history.
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
        key = f"antigravity:{thread_id}:{message['id']}"
        with self.lock:
            stopping = thread_id in self.update_stopping
            turn_id = self.turns[thread_id]["id"]
            if not stopping:
                self.requests[key] = (thread_id, message["id"], request)
        if stopping:
            connection = self.connections.get(thread_id)
            try:
                if connection:
                    connection.write({"id": message["id"], "error": {
                        "code": -32603, "message": "This turn is stopping for a backend update."}})
            except CoordinationError:
                pass  # The provider is already stopping with its turn.
            return
        self._persist_session_state(thread_id)
        method, params = self._approval_display(thread_id, turn_id, request)
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
        with self.lock:
            self.requests.pop(message["id"], None)
        self._persist_session_state(thread_id)

    def close(self):
        self.closed = True
        handover = transport_handover_active(self.store)
        if not handover:
            for thread_id in list(self.turns):
                self._finish(thread_id, "interrupted",
                             termination={"origin": "runtime", "reason": "runtime_closed", "execution": "unknown"})
        for thread_id, connection in list(self.connections.items()):
            connection.close()
        self.connections.clear()
