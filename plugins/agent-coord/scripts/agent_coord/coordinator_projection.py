"""Recoverable coordinator state with strict lock separation and zero delta writes.

``self.projection_lock`` serializes transport channel/cursor transitions and is
strictly separate from ``BrowserSessions.lock``. Streaming frames update
in-memory active-turn mirrors under a brief ``sessions.lock`` hold (< 0.1 ms)
and append to ``TranscriptStore.append_live(thread_id, event)`` without touching
SQLite, packing state, or running zlib compression. Durable SQLite checkpoints
occur only on lifecycle boundaries (channel open/close, turn start/completion,
approval request/resolution, outbound RPC registration, or a 5-second idle
cursor watermark), always serializing outside ``sessions.lock``.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Iterator
import copy
import hashlib
import json
import logging
import sqlite3
import threading
import time
import uuid
import zlib

from .browser_persistence import capture_writes
from .history_persistence import saved_turn_status
from .provider_transport import ProviderLaunchRejected, TransportError
from .store import CoordinationError

VERSION = 1
SINGLE_PAYLOAD_FORMAT = 2
PAYLOAD_FORMAT = 3
WATERMARK_INTERVAL_SECONDS = 5.0

BROWSER_FIELDS = (
    "sequence",
    "turn_revisions",
    "requests",
    "active",
    "loaded",
    "loaded_clients",
    "starting_notification_context",
    "dirty_histories",
    "history_revisions",
    "usage_recovery_attempts",
    "last_used",
    "permission_catalogs",
    "permission_effective",
)
CLAUDE_FIELDS = (
    "options",
    "turns",
    "requests",
    "completed",
    "blocks",
    "confirmed_blocks",
    "started",
    "catalog",
    "effective_permissions",
)

CODEX_STREAMING_METHODS = frozenset(
    {
        "item/started",
        "item/completed",
        "item/agentMessage/delta",
        "item/commandExecution/outputDelta",
        "turn/diff/updated",
        "turn/plan/updated",
        "thread/tokenUsage/updated",
        "thread/usage/updated",
        "account/rateLimits/updated",
        "deprecationNotice",
        "mcpServer/startupStatus/updated",
        "warning",
    }
)
ACP_STREAMING_METHODS = frozenset(
    {
        "session/update",
        "item/started",
        "item/updated",
        "item/delta",
        "item/completed",
        "session/tokenUsage",
        "session/contextUsage",
    }
)
CLAUDE_STREAMING_TYPES = frozenset(
    {
        "stream_event",
        "assistant",
        "user",
        "rate_limit_event",
    }
)


def pack(value, encode_text=None):
    """Preserve integer keys and sets without accepting arbitrary object types."""
    if isinstance(value, dict):
        return {"$map": [[pack(k, encode_text), pack(v, encode_text)] for k, v in value.items()]}
    if isinstance(value, set):
        return {"$set": [pack(v, encode_text) for v in value]}
    if isinstance(value, (tuple, list, deque)):
        return [pack(v, encode_text) for v in value]
    if encode_text is not None and isinstance(value, str) and len(value) >= 16384:
        return {"$text": encode_text(value)}
    if value is None or type(value) in {str, int, float, bool}:
        return value
    raise TypeError("Unsupported coordinator state: " + type(value).__name__)


def _unpack_hashable(value, texts):
    if isinstance(value, list):
        return tuple(_unpack_hashable(v, texts) for v in value)
    return unpack(value, texts)


def unpack(value, texts=None):
    if isinstance(value, list):
        return [unpack(v, texts) for v in value]
    if isinstance(value, dict):
        if "$map" in value:
            return {_unpack_hashable(k, texts): unpack(v, texts) for k, v in value["$map"]}
        if "$set" in value:
            return {_unpack_hashable(v, texts) for v in value["$set"]}
        if "$text" in value and texts is not None:
            reference = value["$text"]
            if isinstance(reference, list):
                return "".join(texts[digest] for digest in reference)
            return texts[reference]
        raise ValueError("Unknown coordinator state representation")
    return value


def is_streaming_frame(provider: str, stream: str, message: dict | None) -> bool:
    """Return True when a transport frame is a pure streaming delta/item update."""
    if stream == "stderr":
        return True
    if stream == "eof":
        return False
    if not isinstance(message, dict):
        return True
    if "id" in message:
        return False
    if provider == "codex":
        method = str(message.get("method") or "")
        return method.endswith(("/delta", "/outputDelta")) or method in CODEX_STREAMING_METHODS
    if provider == "claude":
        kind = str(message.get("type") or "")
        if kind == "system":
            return message.get("subtype") != "init" and not message.get("permissionMode")
        return kind in CLAUDE_STREAMING_TYPES
    if provider in ("antigravity", "kimi", "muse"):
        method = str(message.get("method") or "")
        return method.endswith(("/delta", "/outputDelta")) or method in ACP_STREAMING_METHODS
    return False


class ProcessHandle:
    """Process identity only; the transport owner retains the actual Popen object."""

    def __init__(self, pid):
        self.pid = pid
        self.returncode = None

    def poll(self):
        return self.returncode


class CoordinatorProjection:
    def __init__(self, sessions, client):
        self.sessions, self.store, self.client = sessions, sessions.store, client
        self.projection_lock = threading.RLock()
        self.lock = self.projection_lock
        self.cursor = 0
        self.durable_cursor = 0
        self._dirty_cursor = False
        self._last_watermark = time.monotonic()
        self.stopped = threading.Event()
        self.command_delivered = threading.Event()
        self.worker = None
        self.channels = {}
        self.error = None
        self.delivery_error = None
        self.unloaded_observation = None
        self.restored_completions = deque()
        self.in_event = threading.local()
        self._publication_lock = threading.Lock()
        self._pending_publications: list[tuple[int, str]] = []
        self._last_publication_flush = time.monotonic()
        with self.store._connection() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS coordinator_projection (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL,
                transport_instance TEXT NOT NULL, cursor INTEGER NOT NULL, state BLOB NOT NULL)"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS coordinator_commands (
                command_id TEXT PRIMARY KEY, channel TEXT NOT NULL, operation TEXT NOT NULL,
                value_json TEXT NOT NULL, outcome TEXT NOT NULL, result_json TEXT)"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS coordinator_events (
                sequence INTEGER PRIMARY KEY, event_json TEXT NOT NULL)"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS coordinator_channels (
                channel TEXT PRIMARY KEY, provider TEXT NOT NULL, thread_id TEXT,
                role TEXT NOT NULL, pid INTEGER, closed INTEGER NOT NULL DEFAULT 0)"""
            )

    def owns_provider(self, provider: str, thread_id: str | None = None) -> bool:
        """Return True if a live transport channel is attached for provider (/ thread_id)."""
        with self.projection_lock:
            for entry in self.channels.values():
                if entry.get("closed"):
                    continue
                if entry.get("provider") != provider:
                    continue
                if provider != "codex" and entry.get("thread_id") != thread_id:
                    continue
                return True
            return False

    def state(self, *, durable=False, published_completions=()):
        """Snapshot adapter & browser metadata under brief in-memory locks."""
        sessions = self.sessions
        with self.projection_lock:
            channels_snapshot = {
                name: {
                    "pid": entry["process"].pid,
                    "provider": entry["provider"],
                    "thread_id": entry.get("thread_id"),
                    "closed": entry.get("closed", False),
                    "role": entry.get("role", "main"),
                    "returncode": entry["process"].returncode,
                }
                for name, entry in self.channels.items()
            }
        with sessions.claude.lock:
            claude = {
                name: copy.deepcopy(getattr(sessions.claude, name))
                for name in CLAUDE_FIELDS
                if name != "completed"
            }
            claude["completed"] = {
                identity: copy.deepcopy(turn)
                for identity, turn in sessions.claude.completed.items()
                if identity not in published_completions
            }
        with sessions.lock:
            browser = {name: copy.deepcopy(getattr(sessions, name)) for name in BROWSER_FIELDS}
            if not durable:
                browser["histories"] = copy.deepcopy(sessions.histories)
            events = [] if durable else list(sessions.events)
            watchdog = {
                identity: {
                    name: None if durable and name == "transcripts" else copy.deepcopy(value)
                    for name, value in observed.items()
                }
                for identity, observed in sessions.watchdog.observed.items()
            }
            permissions = copy.deepcopy(sessions.execution.permissions)
            queue_owner = sessions.queue.owner
            schedule_start = sessions.schedules.started_at
            codex_state = {
                "next_id": sessions.rpc.next_id,
                "ready": sessions.rpc.ready,
                "initialized_response": getattr(sessions.rpc, "initialized_response", False),
                "pending_methods": copy.deepcopy(getattr(sessions.rpc, "pending_methods", {})),
            }
            history_codex = (
                {
                    "next_id": sessions.history_rpc.next_id,
                    "ready": sessions.history_rpc.ready,
                    "initialized_response": sessions.history_rpc.initialized_response,
                    "pending_methods": copy.deepcopy(sessions.history_rpc.pending_methods),
                }
                if getattr(sessions, "history_rpc", None) is not None
                else None
            )
        return {
            "version": VERSION,
            "browser": browser,
            "events": events,
            "claude": claude,
            "codex": codex_state,
            "history_codex": history_codex,
            "channels": channels_snapshot,
            "permissions": permissions,
            "watchdog": watchdog,
            "queue_owner": queue_owner,
            "schedule_start": schedule_start,
        }

    def restore(self, state):
        if state["version"] != VERSION:
            raise TransportError("Coordinator projection version is incompatible.")
        sessions = self.sessions
        with sessions.claude.lock:
            for name, value in state["claude"].items():
                setattr(sessions.claude, name, copy.deepcopy(value))
            for identity, blocks in sessions.claude.blocks.items():
                items = {
                    item["id"]: item
                    for item in sessions.claude.turns.get(identity, {}).get("items", [])
                }
                for index, item in list(blocks.items()):
                    blocks[index] = items.get(item["id"], item)
        with sessions.lock:
            for name, value in state["browser"].items():
                if name not in {"sequence", "histories"}:
                    setattr(sessions, name, copy.deepcopy(value))
            if "histories" in state["browser"]:
                sessions.histories = copy.deepcopy(state["browser"]["histories"])
            events = deque(copy.deepcopy(state["events"]), maxlen=1500)
            with sessions.events_changed:
                sessions.events = events
                sessions.sequence = state["browser"]["sequence"]
            sessions.rpc.next_id = state["codex"]["next_id"]
            sessions.rpc.ready = state["codex"]["ready"]
            sessions.rpc.initialized_response = state["codex"].get("initialized_response", False)
            sessions.rpc.pending_methods = state["codex"].get("pending_methods", {})
            sessions.execution.permissions = copy.deepcopy(state["permissions"])
            sessions.watchdog.restore_observations(copy.deepcopy(state["watchdog"]))
        with self.projection_lock:
            for channel, entry in state["channels"].items():
                if channel in self.channels:
                    self.channels[channel]["closed"] = entry.get("closed", False)
                    self.channels[channel]["process"].returncode = entry.get("returncode")

    def _encode_checkpoint(self, *, inline: bool = True, pending_plans=()):
        """Build serialized projection bytes completely outside sessions.lock."""
        del inline
        pending_status: dict[tuple[str, str], str] = {}
        for _, _, plan in pending_plans:
            for row in plan.get("upserts", ()):
                pending_status[(row[0], row[2])] = row[3]
        completions = {}
        with self.sessions.claude.lock:
            claude_completed = dict(self.sessions.claude.completed)
        for identity, completed in claude_completed.items():
            if completed and completed.get("status") in {"completed", "failed", "interrupted"}:
                turn_id = completed.get("id")
                saved = (
                    pending_status.get((identity, turn_id))
                    if turn_id is not None and (identity, turn_id) in pending_status
                    else (saved_turn_status(self.store, identity, turn_id) if turn_id is not None else None)
                )
                if saved == completed["status"]:
                    completions[identity] = {turn_id: completed["status"]}
        snapshot = self.state(durable=True, published_completions=completions)
        packed = pack(snapshot)
        encoded = zlib.compress(json.dumps(packed, separators=(",", ":")).encode("utf-8"), level=1)
        return completions, encoded

    def load(self):
        with self.store.reading(), self.store._connection() as db:
            if not db.in_transaction:
                db.execute("BEGIN")
            row = db.execute("SELECT * FROM coordinator_projection WHERE singleton=1").fetchone()
            if row is None:
                return
            if (
                row["version"] not in (VERSION, SINGLE_PAYLOAD_FORMAT, PAYLOAD_FORMAT)
                or row["transport_instance"] != self.client.record["instance"]
            ):
                raise TransportError("The saved coordinator projection does not match the surviving transport.")
            has_payloads = (
                row["version"] != VERSION
                and db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='coordinator_projection_payloads'"
                ).fetchone()
            )
            payloads = (
                db.execute("SELECT digest, compressed FROM coordinator_projection_payloads").fetchall()
                if has_payloads
                else []
            )
        texts = {}
        for digest, compressed in payloads:
            data = zlib.decompress(compressed)
            if hashlib.sha256(data).hexdigest() != digest:
                raise TransportError("Saved coordinator payload failed integrity verification.")
            texts[digest] = data.decode("utf-8", "surrogatepass")
        state = unpack(json.loads(zlib.decompress(row["state"])), texts)
        self.restore(state)
        self.restored_completions = deque(
            (identity, turn["id"], turn["status"])
            for identity, turn in self.sessions.claude.completed.items()
        )
        self.sessions.histories = {}
        self.sessions.dirty_histories.clear()

        # Replay any non-empty live.jsonl delta tails onto active Claude turn mirrors
        # while keeping BrowserSessions.histories lazy until first access/event.
        for tid in list(self.sessions.claude.turns):
            if not tid:
                continue
            reconstructed = self.store.transcripts.reconstruct_live_turn(
                tid, base_turn=self.sessions.claude.turns.get(tid)
            )
            if reconstructed is not None:
                self.sessions.claude.turns[tid] = reconstructed

        with self.store.reading(), self.store._connection() as db:
            rows = db.execute("SELECT event_json FROM coordinator_events ORDER BY sequence").fetchall()
        if rows:
            events = deque((json.loads(item[0]) for item in rows), maxlen=1500)
            with self.sessions.events_changed:
                self.sessions.events = events
                self.sessions.sequence = max(self.sessions.sequence, events[-1]["seq"])
        self.cursor = self.durable_cursor = row["cursor"]
        self._dirty_cursor = False
        self.recover_commands(operations={"open"})
        with self.store.reading(), self.store._connection() as db:
            records = db.execute(
                """SELECT c.*, o.outcome AS launch_outcome FROM coordinator_channels c
                LEFT JOIN coordinator_commands o ON o.command_id = 'open:' || c.channel
                AND o.channel = c.channel AND o.operation = 'open'"""
            ).fetchall()
        channels = copy.deepcopy(state["channels"])
        for record in records:
            channels.setdefault(record["channel"], {}).update(dict(record))
        for channel, entry in channels.items():
            if entry.get("pid") is None:
                if entry.get("closed") and entry.get("launch_outcome") == "rejected":
                    continue
                raise TransportError(
                    "Provider launch has an uncertain outcome; retained transport requires review."
                )
            if entry.get("closed"):
                process = self.attach(
                    channel,
                    entry["pid"],
                    lambda _: None,
                    provider=entry["provider"],
                    thread_id=entry.get("thread_id"),
                )
                process.returncode = 0
                self.channels[channel]["closed"] = True
                continue
            if entry["provider"] == "codex":
                if entry.get("role", "main") == "history":
                    from .codex_app_server import CodexRPC

                    adapter = self.sessions.history_rpc = CodexRPC(self.store, lambda _: None)
                    for name, value in (state.get("history_codex") or {}).items():
                        setattr(adapter, name, value)
                else:
                    adapter = self.sessions.rpc
                adapter.channel = channel
                adapter.process = self.attach(channel, entry["pid"], adapter._message, provider="codex")
                adapter.process.returncode = entry.get("returncode")
            elif entry["provider"] == "claude":
                from .claude_code import ClaudeConnection

                identity = entry["thread_id"]
                connection = ClaudeConnection.attach(
                    self.store,
                    lambda message, identity=identity: self.sessions.claude._message(identity, message),
                    identity,
                    channel,
                    entry["pid"],
                )
                self.sessions.claude.connections[identity] = connection
            elif entry["provider"] in ("antigravity", "kimi", "muse"):
                adapter = getattr(self.sessions, entry["provider"], None)
                if adapter is not None and hasattr(adapter, "attach_channel"):
                    adapter.attach_channel(
                        entry["thread_id"],
                        channel,
                        entry["pid"],
                        returncode=entry.get("returncode"),
                    )
        with self.store._connection() as db:
            db.execute(
                "UPDATE browser_message_queue SET owner=? WHERE owner=? AND state='queued'",
                (self.sessions.queue.owner, state["queue_owner"]),
            )
        self.sessions.schedules.started_at = state["schedule_start"]

    def attach(self, channel, pid, callback, *, stderr_callback=None, provider, thread_id=None):
        process = ProcessHandle(pid)
        role = (
            "history"
            if provider == "codex" and getattr(callback, "__self__", None) is not self.sessions.rpc
            else "main"
        )
        with self.projection_lock:
            self.channels[channel] = {
                "process": process,
                "callback": callback,
                "stderr_callback": stderr_callback,
                "provider": provider,
                "thread_id": thread_id,
                "role": role,
                "closed": False,
            }
        return process

    def open(self, channel, args, env, cwd, callback, *, stderr_callback=None, provider, thread_id=None):
        identity = "open:" + channel
        value = {"args": args, "env": env, "cwd": cwd}
        role = (
            "history"
            if provider == "codex" and getattr(callback, "__self__", None) is not self.sessions.rpc
            else "main"
        )
        with self.projection_lock:
            completions, encoded = self._encode_checkpoint()
            value_json = json.dumps(value)
            with self.store.database_operation("coordinator.open"), self.store._connection() as db:
                self._save(db, encoded, self.cursor)
                db.execute(
                    "INSERT INTO coordinator_channels(channel,provider,thread_id,role) VALUES (?,?,?,?)",
                    (channel, provider, thread_id, role),
                )
                db.execute(
                    "INSERT INTO coordinator_commands VALUES (?,?,?,?, 'pending',NULL)",
                    (identity, channel, "open", value_json),
                )
            self._finish_checkpoint(completions, self.cursor)
        result = self.deliver(identity, channel, "open", value)
        return self.attach(
            channel,
            result["pid"],
            callback,
            stderr_callback=stderr_callback,
            provider=provider,
            thread_id=thread_id,
        )

    def _finish_checkpoint(self, completions: dict, cursor: int) -> None:
        for identity, outcome in completions.items():
            self.sessions.claude.discard_persisted_completion(identity, outcome)
        self.durable_cursor = cursor
        self._dirty_cursor = False
        self._last_watermark = time.monotonic()

    def publication(self, event):
        value = (event["seq"], json.dumps(event))
        now = time.monotonic()
        with self._publication_lock:
            self._pending_publications.append(value)
            pending_count = len(self._pending_publications)
            due = pending_count >= 24 or (now - self._last_publication_flush) >= 0.12
        if not due:
            return
        persistence = getattr(self.sessions, "persistence", None)
        if persistence is not None and not persistence.stopped:
            try:
                persistence.submit(
                    self.flush_publications,
                    key="__coordinator_publications__",
                    coalesce="publications",
                    required=False,
                    blocks_input=False,
                )
                return
            except RuntimeError:
                pass
        self.flush_publications()

    def _drain_pending_publications(self) -> list[tuple[int, str]]:
        with self._publication_lock:
            if not self._pending_publications:
                return []
            rows = sorted(self._pending_publications, key=lambda item: item[0])
            self._pending_publications.clear()
            self._last_publication_flush = time.monotonic()
            return rows

    def flush_publications(self) -> None:
        rows = self._drain_pending_publications()
        if not rows:
            return
        with self.store.database_operation("coordinator.publication", priority="background"), self.store._connection() as db:
            self._publish_rows(db, rows)

    def buffer_publication(self, event):
        """Reserve a sequence during a projected batch without immediate SQLite writes."""
        if not getattr(self.in_event, "active", False):
            return False
        batch_offset = getattr(self.in_event, "batch_offset", 0)
        sequence = self.sessions.sequence + batch_offset + len(self.in_event.publications) + 1
        full_event = {"seq": sequence, **event}
        self.in_event.publications.append((sequence, full_event))
        return True

    @staticmethod
    def _publish_rows(db, rows):
        if rows:
            db.executemany("INSERT OR REPLACE INTO coordinator_events VALUES (?,?)", rows)
            db.execute("DELETE FROM coordinator_events WHERE sequence <= ?", (rows[-1][0] - 1500,))

    def _save(self, db, encoded: bytes, cursor: int):
        pending_pubs = self._drain_pending_publications()
        if pending_pubs:
            self._publish_rows(db, pending_pubs)
        db.execute(
            "INSERT OR REPLACE INTO coordinator_projection VALUES (1,?,?,?,?)",
            (VERSION, self.client.record["instance"], cursor, encoded),
        )
        if db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='coordinator_projection_payloads'"
        ).fetchone():
            db.execute("DELETE FROM coordinator_projection_payloads")

    def _prepare_dirty_history_plans(self) -> list[tuple[str, int, dict]]:
        with self.sessions.lock:
            dirty_ids = [tid for tid in self.sessions.dirty_histories if tid in self.sessions.histories]
            snapshots = {tid: self.sessions._snapshot_for_save_locked(tid) for tid in dirty_ids}
        return [
            (tid, rev, self.store.transcripts.prepare_turns_write(tid, snap))
            for tid, (snap, rev) in snapshots.items()
        ]

    def _finalize_dirty_history_plans(self, prepared_plans: list[tuple[str, int, dict]]) -> None:
        for tid, rev, plan in prepared_plans:
            self.store.transcripts.finalize_turns_commit(plan)
            with self.sessions.lock:
                if self.sessions.history_revisions.get(tid) == rev:
                    self.sessions.dirty_histories.discard(tid)
            if plan.get("finalized_turns"):
                self.store.search_store.enqueue_turns(tid, plan["finalized_turns"])

    def checkpoint(self, *, inline=False):
        with self.projection_lock:
            prepared_plans = self._prepare_dirty_history_plans()
            completions, encoded = self._encode_checkpoint(inline=inline, pending_plans=prepared_plans)
            with self.store.database_operation("coordinator.checkpoint"), self.store._connection() as db:
                db.execute("BEGIN IMMEDIATE")
                for _, _, plan in prepared_plans:
                    self.store.transcripts.commit_turns_index(db, plan)
                self._save(db, encoded, self.cursor)
            self._finalize_dirty_history_plans(prepared_plans)
            self._finish_checkpoint(completions, self.cursor)

    def _persist_watermark(self) -> None:
        with self.projection_lock:
            if not self._dirty_cursor:
                return
            target_cursor = self.cursor
            completions, encoded = self._encode_checkpoint()
            with self.store.database_operation("coordinator.watermark", priority="background"), self.store._connection() as db:
                updated = db.execute(
                    "UPDATE coordinator_projection SET cursor = ? WHERE singleton = 1 AND transport_instance = ?",
                    (target_cursor, self.client.record["instance"]),
                ).rowcount
                if not updated:
                    self._save(db, encoded, target_cursor)
            self._finish_checkpoint(completions, target_cursor)
        with self.store._writer.measure_request("provider.acknowledge_events"):
            self.client.call("acknowledge_events", cursor=self.durable_cursor, retain_after=self.durable_cursor)

    def write(self, channel, message):
        self.write_registered(channel, lambda: message)

    def write_registered(self, channel, register):
        """Register adapter state and command intent, then deliver outside locks."""
        with self.store._writer.measure_request("provider.register_command"), self.projection_lock:
            with self.sessions.lock:
                message = register()
            value = {"data": json.dumps(message) + "\n"}
            if getattr(self.in_event, "active", False):
                identity = f"{self.client.record['instance']}:{self.in_event.sequence}:{self.in_event.command_index}"
                self.in_event.command_index += 1
                self.in_event.commands.append((identity, channel, "write", json.dumps(value)))
                return
            identity = "initialized:" + channel if message.get("method") == "initialized" else uuid.uuid4().hex
            with self.store._writer.measure_request("provider.prepare_command_checkpoint"):
                completions, encoded = self._encode_checkpoint()
            value_json = json.dumps(value)
            with self.store.database_operation("coordinator.command"), self.store._connection() as db:
                self._save(db, encoded, self.cursor)
                db.execute(
                    "INSERT OR IGNORE INTO coordinator_commands VALUES (?,?,?,?, 'pending',NULL)",
                    (identity, channel, "write", value_json),
                )
            self._finish_checkpoint(completions, self.cursor)
        with self.store._writer.measure_request("provider.deliver_command"):
            self.deliver(identity, channel, "write", value)

    def deliver(self, identity, channel, operation, value):
        try:
            result = self.client.call(
                "command", command_id=identity, channel=channel, operation=operation, value=value
            )
            self.command_delivered.set()
        except Exception:
            self.delivery_error = (
                "Provider command acknowledgement was lost; its retained outcome is being reconciled."
            )
            raise
        state, detail = self._record_outcome(identity, channel, operation, result)
        if state == "rejected":
            raise ProviderLaunchRejected(detail["error"])
        if state != "written":
            self.delivery_error = "Provider delivery is uncertain; the command will not be replayed."
            raise TransportError(self.delivery_error)
        return detail

    def _unstarted_channels(self):
        try:
            channels = self.client.call("status")["channels"]
        except TransportError:
            return set()
        return {c["channel"] for c in channels if c["state"] == "closed" and c["pid"] is None}

    def _record_outcome(self, identity, channel, operation, receipt, *, unstarted_channels=None):
        state = receipt["outcome"]
        detail = receipt.get("result")
        retained_result = None
        if isinstance(detail, str):
            try:
                detail = json.loads(detail)
            except json.JSONDecodeError:
                retained_result, detail, state = detail, None, "uncertain"
        if (
            operation == "open"
            and state == "uncertain"
            and isinstance(detail, dict)
            and str(detail.get("error", "")).startswith("[Errno 2] No such file or directory: ")
        ):
            if unstarted_channels is None:
                unstarted_channels = self._unstarted_channels()
            if channel in unstarted_channels:
                state = "rejected"
        if state != "written" and not (state == "rejected" and operation == "open"):
            state = "uncertain"
        result_json = retained_result if retained_result is not None else json.dumps(detail)
        with self.store.database_operation("coordinator.command_outcome"), self.store._connection() as db:
            db.execute(
                """UPDATE coordinator_commands SET outcome=?, result_json=?,
                value_json=CASE WHEN ? IN ('written','rejected') THEN '{}' ELSE value_json END WHERE command_id=?""",
                (state, result_json, state, identity),
            )
            if operation == "open" and state == "written":
                db.execute("UPDATE coordinator_channels SET pid=? WHERE channel=?", (detail["pid"], channel))
            elif state == "rejected":
                db.execute("DELETE FROM coordinator_channels WHERE channel=? AND pid IS NULL", (channel,))
        return state, detail

    def recover_commands(self, *, operations=None):
        repair_launches = operations == {"open"}
        with self.store.reading(), self.store._connection() as db:
            pending = db.execute(
                """SELECT command_id,channel,operation,outcome,result_json,
                CASE WHEN outcome='pending' THEN value_json END AS value_json FROM coordinator_commands o
                WHERE outcome='pending' OR (? AND outcome='uncertain' AND operation='open'
                AND command_id='open:' || channel AND instr(result_json, '[Errno 2] No such file or directory: ')>0
                AND NOT EXISTS (SELECT 1 FROM coordinator_channels c WHERE c.channel=o.channel AND c.pid IS NOT NULL))
                """,
                (repair_launches,),
            ).fetchall()
        unstarted_channels = None
        for row in pending:
            if operations is not None and row["operation"] not in operations:
                continue
            if row["outcome"] == "uncertain":
                if unstarted_channels is None:
                    unstarted_channels = self._unstarted_channels()
                if row["channel"] in unstarted_channels:
                    self._record_outcome(
                        row["command_id"],
                        row["channel"],
                        "open",
                        {"outcome": "uncertain", "result": row["result_json"]},
                        unstarted_channels=unstarted_channels,
                    )
                continue
            outcome = self.client.call("outcome", command_id=row["command_id"])
            if outcome["outcome"] == "absent":
                try:
                    self.deliver(
                        row["command_id"], row["channel"], row["operation"], json.loads(row["value_json"])
                    )
                except ProviderLaunchRejected:
                    pass
                continue
            deadline = time.monotonic() + 10
            while outcome["outcome"] == "writing" and time.monotonic() < deadline:
                time.sleep(0.02)
                outcome = self.client.call("outcome", command_id=row["command_id"])
            self._record_outcome(row["command_id"], row["channel"], row["operation"], outcome)
        with self.store.reading(), self.store._connection() as db:
            uncertain = db.execute(
                "SELECT 1 FROM coordinator_commands WHERE outcome IN ('uncertain','writing') LIMIT 1"
            ).fetchone()
        self.delivery_error = (
            "A retained provider command has uncertain delivery; review its outcome before sending again."
            if uncertain
            else None
        )

    def close_channel(self, channel):
        if getattr(self.sessions, "transport_handover", False):
            return
        self.client.command(channel, "close", {})
        self._mark_closed(channel)

    def close_exited_channel(self, channel):
        if getattr(self.sessions, "transport_handover", False):
            raise CoordinationError(
                "Codex app-server stopped during a coordinator handover; "
                "the replacement coordinator starts it again."
            )
        retained = self.client.call("status").get("channels", [])
        if any(entry["channel"] == channel and entry["state"] != "closed" for entry in retained):
            self.client.command(channel, "close", {})
        self._mark_closed(channel)

    def _mark_closed(self, channel):
        with self.projection_lock:
            if channel in self.channels:
                self.channels[channel]["closed"] = True
                self.channels[channel]["process"].returncode = 0
            with self.store._connection() as db:
                db.execute("UPDATE coordinator_channels SET closed=1 WHERE channel=?", (channel,))

    def after_commit(self, callback):
        if getattr(self.in_event, "active", False):
            self.in_event.completions.append(callback)
        else:
            callback()

    def project(self, event):
        self.project_batch([event])

    def _resolve_event_thread_id(self, entry: dict, message: dict | None) -> str | None:
        tid = entry.get("thread_id")
        if tid:
            return tid
        if isinstance(message, dict):
            params = message.get("params")
            if isinstance(params, dict) and isinstance(params.get("threadId"), str):
                return params["threadId"]
        return None

    def project_batch(self, events):
        if not events:
            return
        decoded = []
        for event in events:
            message = None
            if event["stream"] == "stdout":
                try:
                    message = json.loads(event["data"])
                except ValueError:
                    logging.getLogger(__name__).warning(
                        "Ignoring malformed provider frame at sequence %s", event["sequence"]
                    )
            decoded.append((event, message))
        while decoded:
            decoded = self._process_decoded_slice(decoded)
            if decoded is None:
                break
        if self.durable_cursor > 0:
            with self.store._writer.measure_request("provider.acknowledge_events"):
                self.client.call(
                    "acknowledge_events",
                    cursor=self.durable_cursor,
                    retain_after=self.durable_cursor,
                )

    def _process_decoded_slice(self, decoded):
        with self.projection_lock:
            decoded = [(event, message) for event, message in decoded if event["sequence"] > self.cursor]
            if not decoded:
                return None

            # Fast path for leading streaming frames: update in-memory state under brief
            # sessions.lock (< 0.1 ms), append to live.jsonl, zero SQLite writes!
            first_event, first_message = decoded[0]
            first_entry = self.channels.get(first_event["channel"])
            if first_entry is None:
                raise TransportError("Event belongs to a transport missing from the saved adapter state.")
            if not first_entry.get("closed") and is_streaming_frame(
                first_entry["provider"], first_event["stream"], first_message
            ):
                streaming_count = 0
                live_appends: list[tuple[str, dict]] = []
                in_memory_events: list[dict] = []
                for event, message in decoded:
                    entry = self.channels.get(event["channel"])
                    if entry is None:
                        raise TransportError("Event belongs to a transport missing from the saved adapter state.")
                    if not entry.get("closed") and not is_streaming_frame(
                        entry["provider"], event["stream"], message
                    ):
                        break
                    streaming_count += 1
                    if entry.get("closed"):
                        self.cursor = event["sequence"]
                        self._dirty_cursor = True
                        continue
                    tid = self._resolve_event_thread_id(entry, message)
                    if tid and tid in self.sessions.loaded and tid not in self.sessions.histories:
                        self.sessions._load_history_unlocked(tid)

                    self.in_event.active = True
                    self.in_event.batch_offset = len(in_memory_events)
                    self.in_event.publications = []
                    self.in_event.commands = []
                    self.in_event.completions = []
                    self.in_event.sequence = event["sequence"]
                    self.in_event.command_index = 0
                    try:
                        if event["stream"] == "stderr":
                            stderr_cb = entry.get("stderr_callback")
                            if stderr_cb is not None:
                                stderr_cb(event["data"])
                        elif event["stream"] == "stdout" and isinstance(message, dict):
                            entry["callback"](message)
                    except Exception as exc:
                        entry["isolated_error"] = str(exc)
                        logging.getLogger(__name__).exception(
                            "Isolated callback failure on channel %s at sequence %s",
                            event["channel"],
                            event["sequence"],
                        )
                    finally:
                        self.in_event.active = False

                    if self.in_event.commands or self.in_event.completions:
                        # An outbound command or commit waiter during a stream frame commits durably.
                        if in_memory_events:
                            self.sessions.append_committed_events(in_memory_events)
                        for prior_tid, raw_event in live_appends:
                            self.store.transcripts.append_live(prior_tid, raw_event)
                        prepared_plans = self._prepare_dirty_history_plans()
                        completions, encoded = self._encode_checkpoint(pending_plans=prepared_plans)
                        durable_pub_rows = [(seq, json.dumps(pub)) for seq, pub in self.in_event.publications]
                        with self.store.database_operation("coordinator.project"), self.store._connection() as db:
                            db.execute("BEGIN IMMEDIATE")
                            for _, _, plan in prepared_plans:
                                self.store.transcripts.commit_turns_index(db, plan)
                            db.executemany(
                                "INSERT OR IGNORE INTO coordinator_commands VALUES (?,?,?,?,'pending',NULL)",
                                self.in_event.commands,
                            )
                            self._publish_rows(db, durable_pub_rows)
                            self._save(db, encoded, event["sequence"])
                        self._finalize_dirty_history_plans(prepared_plans)
                        if tid and event["stream"] == "stdout" and isinstance(message, dict):
                            self.store.transcripts.append_live(tid, event)
                        self.cursor = event["sequence"]
                        if self.in_event.publications:
                            self.sessions.append_committed_events([pub for _, pub in self.in_event.publications])
                        self._finish_checkpoint(completions, event["sequence"])
                        pending_cmds = list(self.in_event.commands)
                        pending_cbs = list(self.in_event.completions)
                        for cb in pending_cbs:
                            cb()
                        for cmd_id, ch, op, val in pending_cmds:
                            self.deliver(cmd_id, ch, op, json.loads(val))
                        return decoded[streaming_count:] or None

                    for _, pub in self.in_event.publications:
                        in_memory_events.append(pub)
                    if tid and event["stream"] == "stdout" and isinstance(message, dict):
                        live_appends.append((tid, event))
                    self.cursor = event["sequence"]
                    self._dirty_cursor = True

                if in_memory_events:
                    self.sessions.append_committed_events(in_memory_events)
                for tid, raw_event in live_appends:
                    self.store.transcripts.append_live(tid, raw_event)
                return decoded[streaming_count:] or None

            # Boundary event path (turn/started, turn/completed, approvals, eof, RPC reply).
            return self._commit_boundary_event(decoded)

    def _commit_boundary_event(self, decoded):
        event, message = decoded[0]
        entry = self.channels.get(event["channel"])
        if entry is None:
            raise TransportError("Event belongs to a transport missing from the saved adapter state.")
        tid = self._resolve_event_thread_id(entry, message)
        if tid and tid in self.sessions.loaded and tid not in self.sessions.histories:
            self.sessions._load_history_unlocked(tid)

        before = self.state()
        try:
            with capture_writes() as writes:
                self.in_event.active = True
                self.in_event.publications = []
                self.in_event.commands = []
                self.in_event.completions = []
                self.in_event.sequence = event["sequence"]
                self.in_event.command_index = 0
                if not entry.get("closed"):
                    try:
                        if event["stream"] == "stderr":
                            stderr_cb = entry.get("stderr_callback")
                            if stderr_cb is not None:
                                stderr_cb(event["data"])
                        elif event["stream"] == "stdout" and isinstance(message, dict):
                            entry["callback"](message)
                        elif event["stream"] == "eof":
                            entry["process"].returncode = 1
                            if entry["provider"] == "codex":
                                entry["callback"]({"method": "bridge/disconnected", "params": {}})
                            elif entry["provider"] in ("antigravity", "kimi", "muse"):
                                entry["callback"](
                                    {"method": "disconnected", "params": {"error": "Provider transport closed."}}
                                )
                            else:
                                entry["callback"](
                                    {"type": "disconnected", "error": "Provider transport closed."}
                                )
                    except sqlite3.Error:
                        raise
                    except Exception as exc:
                        entry["isolated_error"] = str(exc)
                        logging.getLogger(__name__).exception(
                            "Isolated boundary callback failure on channel %s at sequence %s",
                            event["channel"],
                            event["sequence"],
                        )

            # Flush any finalized/dirty turns to TranscriptStore outside sessions.lock before SQLite commit.
            with self.sessions.lock:
                dirty_ids = [d_id for d_id in self.sessions.dirty_histories if d_id in self.sessions.histories]
                turn_snapshots = {d_id: self.sessions._snapshot_for_save_locked(d_id) for d_id in dirty_ids}
            prepared_plans = [
                (d_id, rev, self.store.transcripts.prepare_turns_write(d_id, snap))
                for d_id, (snap, rev) in turn_snapshots.items()
            ]
            if tid and event["stream"] == "stdout" and isinstance(message, dict):
                method = message.get("method")
                if method == "turn/started":
                    self.store.transcripts.append_live(tid, event)

            with self.store._writer.measure_request("provider.prepare_event_checkpoint"):
                completions, encoded = self._encode_checkpoint(pending_plans=prepared_plans)

            durable_pub_rows = [
                (seq, json.dumps(pub))
                for seq, pub in self.in_event.publications
            ]
            with self.store.database_operation("coordinator.project"), self.store._connection() as db:
                db.execute("BEGIN IMMEDIATE")
                with self.store.sharing_transaction(db):
                    for callback, required in writes:
                        if not required:
                            db.execute("SAVEPOINT optional_projection")
                        try:
                            result = callback()
                            if isinstance(result, Iterator):
                                for _ in result:
                                    pass
                        except Exception:
                            if required:
                                raise
                            db.execute("ROLLBACK TO SAVEPOINT optional_projection")
                            logging.getLogger(__name__).exception("Optional projected bookkeeping failed")
                        finally:
                            if not required:
                                db.execute("RELEASE SAVEPOINT optional_projection")
                    for _, _, plan in prepared_plans:
                        self.store.transcripts.commit_turns_index(db, plan)
                    db.executemany(
                        "INSERT OR IGNORE INTO coordinator_commands VALUES (?,?,?,?,'pending',NULL)",
                        self.in_event.commands,
                    )
                    self._publish_rows(db, durable_pub_rows)
                    self._save(db, encoded, event["sequence"])

            self._finalize_dirty_history_plans(prepared_plans)

            self.cursor = event["sequence"]
            if self.in_event.publications:
                self.sessions.append_committed_events([pub for _, pub in self.in_event.publications])
            self._finish_checkpoint(completions, event["sequence"])
            commands = list(self.in_event.commands)
            completion_callbacks = list(self.in_event.completions)
        except Exception:
            self.restore(before)
            raise
        finally:
            self.unloaded_observation = None
            self.in_event.active = False

        for callback in completion_callbacks:
            callback()
        for identity, channel, operation, value in commands:
            self.deliver(identity, channel, operation, json.loads(value))
        return decoded[1:] or None

    def start(self):
        self.recover_commands()
        self.stopped.clear()
        self.worker = threading.Thread(target=self._run, name="provider-projection", daemon=True)
        self.worker.start()
        rpc = self.sessions.rpc
        process = getattr(rpc, "process", None)
        if process is not None and process.poll() is None and not rpc.ready:
            with self.sessions.admission.enter(maintenance=True):
                rpc.start()

    def reconcile_restored_completion(self):
        identity, turn_id, status = self.restored_completions.popleft()
        if status not in {"completed", "failed", "interrupted"}:
            return
        try:
            saved = saved_turn_status(self.store, identity, turn_id)
        except (ValueError, sqlite3.Error):
            logging.getLogger(__name__).warning("Retaining an unverified recovery turn", exc_info=True)
            return
        if saved == status:
            self.sessions.claude.discard_persisted_completion(identity, {turn_id: status})

    def _run(self):
        while not self.stopped.is_set():
            try:
                if self.restored_completions:
                    self.reconcile_restored_completion()
                if self.delivery_error and time.monotonic() >= getattr(self, "next_reconciliation", 0):
                    self.next_reconciliation = time.monotonic() + 1
                    self.recover_commands()
                self.command_delivered.clear()
                response = self.client.call("events", after=self.cursor, wait=True)
                events = response["events"]
                if self.stopped.is_set():
                    return
                if events:
                    with self.store._writer.measure_request("provider.apply_events"):
                        self.project_batch(events)
                elif (
                    self._dirty_cursor
                    and time.monotonic() - self._last_watermark >= WATERMARK_INTERVAL_SECONDS
                ):
                    self._persist_watermark()
                self.error = None
                if not events and not response.get("event_wait"):
                    self.command_delivered.wait(0.02)
            except Exception as error:
                self.error = str(error)
                logging.getLogger(__name__).exception("Provider projection paused at cursor %s", self.cursor)
                self.stopped.wait(0.25)

    def freeze(self):
        self.stopped.set()
        self.command_delivered.set()
        if self.worker is not None:
            self.worker.join(timeout=10)
            if self.worker.is_alive():
                raise TransportError("Provider projection did not reach its transaction boundary.")
        if self.error:
            raise TransportError("Provider projection is not healthy: " + self.error)
        with self.sessions.events_changed:
            tail_events = [
                (event["seq"], json.dumps(event))
                for event in self.sessions.events
                if isinstance(event, dict) and isinstance(event.get("seq"), int)
            ]
        if tail_events:
            with self._publication_lock:
                self._pending_publications.extend(tail_events)
        self.checkpoint(inline=True)
