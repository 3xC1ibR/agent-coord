"""Recoverable coordinator state, independent of provider pipe ownership.

Only explicit JSON-compatible adapter state crosses this boundary. Code,
callbacks, futures, locks and open descriptors are reconstructed by the new
payload. Provider commands use durable identities before crossing the pipe.
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
from .provider_transport import Client, ProviderLaunchRejected, TransportError
from .history_persistence import saved_turn_status
from .store import CoordinationError

VERSION = 1
SINGLE_PAYLOAD_FORMAT = 2
PAYLOAD_FORMAT = 3
PAYLOAD_SEGMENT_CHARACTERS = 64 * 1024
MIN_PAYLOAD_CHARACTERS = 16 * 1024
BROWSER_FIELDS = ("sequence", "turn_revisions", "requests", "active", "loaded", "loaded_clients",
    "starting_notification_context", "histories", "dirty_histories", "history_revisions",
    "usage_recovery_attempts", "last_used", "permission_catalogs", "permission_effective")
CLAUDE_FIELDS = ("options", "turns", "requests", "completed", "blocks", "confirmed_blocks", "started",
                 "catalog", "effective_permissions")


def pack(value, encode_text=None):
    """Preserve integer keys and sets without accepting arbitrary object types."""
    if isinstance(value, dict):
        return {"$map": [[pack(k, encode_text), pack(v, encode_text)] for k, v in value.items()]}
    if isinstance(value, set):
        return {"$set": [pack(v, encode_text) for v in value]}
    if isinstance(value, (tuple, list, deque)):
        return [pack(v, encode_text) for v in value]
    if encode_text is not None and isinstance(value, str) and len(value) >= MIN_PAYLOAD_CHARACTERS:
        return {"$text": encode_text(value)}
    if value is None or type(value) in {str, int, float, bool}:
        return value
    raise TypeError("Unsupported coordinator state: " + type(value).__name__)


def _unpack_hashable(value, texts):
    # Arrays in mapping keys and set members were tuples before serialization.
    # Keep decoding the existing representation so retained journals recover.
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


class ProcessHandle:
    """Process identity only; the stable owner retains the actual Popen object."""

    def __init__(self, pid):
        self.pid = pid
        self.returncode = None

    def poll(self):
        return self.returncode


class CoordinatorProjection:
    def __init__(self, sessions, client):
        self.sessions, self.store, self.client = sessions, sessions.store, client
        # Browser mirrors and Claude adapter state form one projection. Sharing
        # their reentrant lock avoids browser->adapter versus adapter->browser
        # inversions during an approval answer or steering write.
        self.lock = sessions.lock
        sessions.claude.lock = sessions.lock
        self.cursor = 0
        self.stopped = threading.Event()
        self.worker = None
        self.channels = {}
        self.error = None
        self.delivery_error = None
        self.prepared_histories = {}
        self.payload_cache = {}
        self.text_references = {}
        self.unloaded_observation = None
        self.restored_completions = deque()
        self.in_event = threading.local()
        with self.store._connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS coordinator_projection (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL,
                transport_instance TEXT NOT NULL, cursor INTEGER NOT NULL, state BLOB NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS coordinator_projection_payloads (
                digest TEXT PRIMARY KEY, compressed BLOB NOT NULL) WITHOUT ROWID""")
            db.execute("""CREATE TABLE IF NOT EXISTS coordinator_commands (
                command_id TEXT PRIMARY KEY, channel TEXT NOT NULL, operation TEXT NOT NULL,
                value_json TEXT NOT NULL, outcome TEXT NOT NULL, result_json TEXT)""")
            db.execute("""CREATE TABLE IF NOT EXISTS coordinator_events (
                sequence INTEGER PRIMARY KEY, event_json TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS coordinator_channels (
                channel TEXT PRIMARY KEY, provider TEXT NOT NULL, thread_id TEXT,
                role TEXT NOT NULL, pid INTEGER, closed INTEGER NOT NULL DEFAULT 0)""")

    def state(self, *, durable=False, affected=None, published_completions=()):
        sessions = self.sessions
        browser = {name: copy.deepcopy(getattr(sessions, name)) for name in BROWSER_FIELDS if name != "histories"}
        if not durable:
            browser["histories"] = {identity: copy.deepcopy(value) if affected is None or identity in affected else value
                                    for identity, value in sessions.histories.items()}
        claude = {name: copy.deepcopy(getattr(sessions.claude, name)) for name in CLAUDE_FIELDS if name != "completed"}
        claude["completed"] = {identity: copy.deepcopy(turn) for identity, turn in sessions.claude.completed.items()
                               if identity not in published_completions}
        return {"version": VERSION,
            "browser": browser,
            "events": [] if durable else list(sessions.events),
            "claude": claude,
            "codex": {"next_id": sessions.rpc.next_id, "ready": sessions.rpc.ready,
                      "initialized_response": getattr(sessions.rpc, "initialized_response", False),
                      "pending_methods": copy.deepcopy(getattr(sessions.rpc, "pending_methods", {}))},
            "history_codex": ({"next_id": sessions.history_rpc.next_id, "ready": sessions.history_rpc.ready,
                               "initialized_response": sessions.history_rpc.initialized_response,
                               "pending_methods": copy.deepcopy(sessions.history_rpc.pending_methods)}
                              if getattr(sessions, "history_rpc", None) is not None else None),
            "channels": {name: {"pid": entry["process"].pid, "provider": entry["provider"],
                                "thread_id": entry.get("thread_id"), "closed": entry.get("closed", False),
                                "role": entry.get("role", "main"),
                                "returncode": entry["process"].returncode} for name, entry in self.channels.items()},
            "permissions": copy.deepcopy(sessions.execution.permissions),
            "watchdog": copy.deepcopy(sessions.watchdog.observed),
            "queue_owner": sessions.queue.owner, "schedule_start": sessions.schedules.started_at}

    def restore(self, state):
        if state["version"] != VERSION:
            raise TransportError("Coordinator projection version is incompatible.")
        sessions = self.sessions
        for name, value in state["browser"].items():
            setattr(sessions, name, copy.deepcopy(value))
        sessions.events = deque(copy.deepcopy(state["events"]), maxlen=1500)
        for name, value in state["claude"].items():
            setattr(sessions.claude, name, copy.deepcopy(value))
        # Stream block entries and active turn items refer to the same objects.
        for identity, blocks in sessions.claude.blocks.items():
            items = {item["id"]: item for item in sessions.claude.turns.get(identity, {}).get("items", [])}
            for index, item in list(blocks.items()):
                blocks[index] = items.get(item["id"], item)
        sessions.rpc.next_id = state["codex"]["next_id"]
        sessions.rpc.ready = state["codex"]["ready"]
        sessions.rpc.initialized_response = state["codex"].get("initialized_response", False)
        sessions.rpc.pending_methods = state["codex"].get("pending_methods", {})
        sessions.execution.permissions = copy.deepcopy(state["permissions"])
        sessions.watchdog.observed = copy.deepcopy(state["watchdog"])
        for channel, entry in state["channels"].items():
            if channel in self.channels:
                self.channels[channel]["closed"] = entry.get("closed", False)
                self.channels[channel]["process"].returncode = entry.get("returncode")

    def load(self):
        with self.store.reading(), self.store._connection() as db:
            if not db.in_transaction:
                db.execute("BEGIN")
            row = db.execute("SELECT * FROM coordinator_projection WHERE singleton=1").fetchone()
            if row is None:
                return
            if row["version"] not in (VERSION, SINGLE_PAYLOAD_FORMAT, PAYLOAD_FORMAT) or row["transport_instance"] != self.client.record["instance"]:
                raise TransportError("The saved coordinator projection does not match the surviving transport.")
            payloads = (db.execute("SELECT digest, compressed FROM coordinator_projection_payloads").fetchall()
                        if row["version"] != VERSION else [])
        texts = {}
        for digest, compressed in payloads:
            data = zlib.decompress(compressed)
            if hashlib.sha256(data).hexdigest() != digest:
                raise TransportError("Saved coordinator payload failed integrity verification.")
            texts[digest] = data.decode("utf-8", "surrogatepass")
            self.payload_cache[digest] = compressed
        state = unpack(json.loads(zlib.decompress(row["state"])), texts)
        self.restore(state)
        # Earlier versions also checkpointed turns already published in history.
        # Verify those copies incrementally after readiness, outside the session lock.
        self.restored_completions = deque((identity, turn["id"], turn["status"])
                                          for identity, turn in self.sessions.claude.completed.items())
        # Histories are durable independently of the provider projection. The
        # normal event and read paths load them on demand; eagerly decoding all
        # loaded conversations can exceed readiness before any work can resume.
        self.sessions.histories = {}
        # The saved projection and these histories committed atomically. Dirty
        # markers were captured before publication and are no longer pending.
        self.sessions.dirty_histories.clear()
        with self.store.reading(), self.store._connection() as db:
            rows = db.execute("SELECT event_json FROM coordinator_events ORDER BY sequence").fetchall()
        if rows:
            self.sessions.events = deque((json.loads(row[0]) for row in rows), maxlen=1500)
            self.sessions.sequence = self.sessions.events[-1]["seq"]
        self.cursor = row["cursor"]
        self.recover_commands(operations={"open"})
        with self.store.reading(), self.store._connection() as db:
            records = db.execute("""SELECT c.*, o.outcome AS launch_outcome FROM coordinator_channels c
                LEFT JOIN coordinator_commands o ON o.command_id = 'open:' || c.channel
                AND o.channel = c.channel AND o.operation = 'open'""").fetchall()
        channels = copy.deepcopy(state["channels"])
        for row in records:
            channels.setdefault(row["channel"], {}).update(dict(row))
        for channel, entry in channels.items():
            if entry.get("pid") is None:
                if entry.get("closed") and entry.get("launch_outcome") == "rejected":
                    continue
                raise TransportError("Provider launch has an uncertain outcome; retained transport requires review.")
            if entry.get("closed"):
                process = self.attach(channel, entry["pid"], lambda _: None,
                                      provider=entry["provider"], thread_id=entry.get("thread_id"))
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
            else:
                from .claude_code import ClaudeConnection
                identity = entry["thread_id"]
                connection = ClaudeConnection.attach(self.store,
                    lambda message, identity=identity: self.sessions.claude._message(identity, message),
                    identity, channel, entry["pid"])
                self.sessions.claude.connections[identity] = connection
        with self.store._connection() as db:
            db.execute("UPDATE browser_message_queue SET owner=? WHERE owner=? AND state='queued'",
                       (self.sessions.queue.owner, state["queue_owner"]))
        self.sessions.schedules.started_at = state["schedule_start"]

    def attach(self, channel, pid, callback, *, provider, thread_id=None):
        process = ProcessHandle(pid)
        role = "history" if provider == "codex" and getattr(callback, "__self__", None) is not self.sessions.rpc else "main"
        self.channels[channel] = {"process": process, "callback": callback, "provider": provider,
                                  "thread_id": thread_id, "role": role, "closed": False}
        return process

    def open(self, channel, args, env, cwd, callback, *, provider, thread_id=None):
        identity = "open:" + channel
        value = {"args": args, "env": env, "cwd": cwd}
        role = "history" if provider == "codex" and getattr(callback, "__self__", None) is not self.sessions.rpc else "main"
        with self.lock:
            prepared, completions, encoded = self.prepare_checkpoint()
            value_json = json.dumps(value)
            with self.store.database_operation("coordinator.open"), self.store._connection() as db:
                self.publish_histories(db, prepared)
                self._save(db, encoded, self.cursor)
                db.execute("INSERT INTO coordinator_channels(channel,provider,thread_id,role) VALUES (?,?,?,?)",
                           (channel, provider, thread_id, role))
                db.execute("INSERT INTO coordinator_commands VALUES (?,?,?,?, 'pending',NULL)",
                           (identity, channel, "open", value_json))
            self.remember_histories(prepared, completions)
        result = self.deliver(identity, channel, "open", value)
        return self.attach(channel, result["pid"], callback, provider=provider, thread_id=thread_id)

    def prepare_checkpoint(self, *, inline=False):
        from .history_persistence import prepare_history, stage_history
        prepared = {identity: prepare_history(self.sessions.histories[identity], previous=self.prepared_histories.get(identity))
                    for identity in self.sessions.dirty_histories if identity in self.sessions.histories}
        for history in prepared.values():
            stage_history(self.store, history)
        # These roots and the projection commit together. Exclude only a matching
        # terminal outcome, and keep the live recovery copy until commit succeeds.
        completions = {}
        for identity in prepared:
            completed = self.sessions.claude.completed.get(identity)
            if completed and completed["status"] in {"completed", "failed", "interrupted"} and any(
                    turn.get("id") == completed["id"] and turn.get("status") == completed["status"]
                    for turn in reversed(self.sessions.histories[identity].get("turns", []))):
                completions[identity] = {completed["id"]: completed["status"]}
        state = self.state(durable=True, published_completions=completions)
        # A growing provider message shares all complete segments with its
        # previous checkpoint. Only new segment contents need compression.
        references, payloads = {}, {}
        previous_by_prefix = {text[:PAYLOAD_SEGMENT_CHARACTERS]: text for text in self.text_references
                              if len(text) >= PAYLOAD_SEGMENT_CHARACTERS}

        def encode_text(text):
            parts = references.get(text) or self.text_references.get(text)
            if parts is None:
                parts = []
                previous = previous_by_prefix.get(text[:PAYLOAD_SEGMENT_CHARACTERS])
                if previous is not None and text.startswith(previous):
                    # Only a complete prior segment has the same boundaries
                    # after appending. Rebuild the previous partial tail.
                    count = len(previous) // PAYLOAD_SEGMENT_CHARACTERS
                    parts.extend(self.text_references[previous][:count])
                    for digest in parts:
                        payloads[digest] = self.payload_cache[digest]
                for offset in range(len(parts) * PAYLOAD_SEGMENT_CHARACTERS, len(text), PAYLOAD_SEGMENT_CHARACTERS):
                    data = text[offset:offset + PAYLOAD_SEGMENT_CHARACTERS].encode("utf-8", "surrogatepass")
                    digest = hashlib.sha256(data).hexdigest()
                    if digest not in payloads:
                        payloads[digest] = self.payload_cache.get(digest) or zlib.compress(data, level=1)
                    parts.append(digest)
            else:
                for digest in parts:
                    if digest not in payloads:
                        payloads[digest] = self.payload_cache[digest]
            references[text] = parts
            return parts[0] if len(parts) == 1 else parts

        root = pack(state, None if inline else encode_text)
        encoded = zlib.compress(json.dumps(root, separators=(",", ":")).encode(), level=1)
        self.text_references, self.payload_cache = references, payloads
        return prepared, completions, (encoded, payloads)

    def publish_histories(self, db, prepared):
        from .history_persistence import publish_history
        for identity, history in prepared.items():
            publish_history(db, identity, history)

    def remember_histories(self, prepared, completions):
        for identity, outcome in completions.items():
            self.sessions.claude.discard_persisted_completion(identity, outcome)
        self.sessions.dirty_histories.difference_update(prepared)
        for identity, history in prepared.items():
            # Keep bounded immutable item values so unchanged archive items can
            # be compared without serializing and hashing them on every delta.
            history.cache_bytes = max(history.cache_bytes, 3 * history.source_bytes)
            self.prepared_histories[identity] = history
        for identity in list(self.prepared_histories):
            if identity not in self.sessions.loaded:
                self.prepared_histories.pop(identity)
        while (len(self.prepared_histories) > 16 or
               sum(value.cache_bytes for value in self.prepared_histories.values()) > 64 * 1024 * 1024):
            self.prepared_histories.pop(next(iter(self.prepared_histories)))

    def publication(self, event):
        value = (event["seq"], json.dumps(event))
        with self.store.database_operation("coordinator.publication"), self.store._connection() as db:
            self._publish_rows(db, [value])

    def buffer_publication(self, event):
        """Reserve a durable sequence without exposing uncommitted output."""
        if not getattr(self.in_event, "active", False):
            return False
        sequence = self.sessions.sequence + len(self.in_event.publications) + 1
        self.in_event.publications.append((sequence, json.dumps({"seq": sequence, **event})))
        return True

    @staticmethod
    def _publish_rows(db, rows):
        if rows:
            db.executemany("INSERT INTO coordinator_events VALUES (?,?)", rows)
            db.execute("DELETE FROM coordinator_events WHERE sequence <= ?", (rows[-1][0] - 1500,))

    def _save(self, db, encoded, cursor):
        root, payloads = encoded
        db.execute("INSERT OR REPLACE INTO coordinator_projection VALUES (1,?,?,?,?)",
                   (PAYLOAD_FORMAT if payloads else VERSION, self.client.record["instance"], cursor, root))
        stored = {row[0] for row in db.execute("SELECT digest FROM coordinator_projection_payloads")}
        db.executemany("INSERT INTO coordinator_projection_payloads VALUES (?,?)",
                       ((digest, payloads[digest]) for digest in payloads.keys() - stored))
        db.executemany("DELETE FROM coordinator_projection_payloads WHERE digest=?",
                       ((digest,) for digest in stored - payloads.keys()))

    def checkpoint(self, *, inline=False):
        with self.lock, self.sessions.claude.lock, self.sessions.lock:
            prepared, completions, encoded = self.prepare_checkpoint(inline=inline)
            with self.store.database_operation("coordinator.checkpoint"), self.store._connection() as db:
                self.publish_histories(db, prepared)
                self._save(db, encoded, self.cursor)
            self.remember_histories(prepared, completions)

    def write(self, channel, message):
        self.write_registered(channel, lambda: message)

    def write_registered(self, channel, register):
        """Register adapter state and intent together, then deliver outside locks."""
        # Store intent and adapter state before provider input. A recovered
        # coordinator queries this identity; it never invents a replacement.
        with self.lock:
            message = register()
            value = {"data": json.dumps(message) + "\n"}
            if getattr(self.in_event, "active", False):
                identity = f"{self.client.record['instance']}:{self.in_event.sequence}:{self.in_event.command_index}"
                self.in_event.command_index += 1
                self.in_event.commands.append((identity, channel, "write", json.dumps(value)))
                return  # Dispatch only after this event and its command commit.
            identity = "initialized:" + channel if message.get("method") == "initialized" else uuid.uuid4().hex
            prepared, completions, encoded = self.prepare_checkpoint()
            value_json = json.dumps(value)
            with self.store.database_operation("coordinator.command"), self.store._connection() as db:
                self.publish_histories(db, prepared)
                self._save(db, encoded, self.cursor)
                db.execute("INSERT OR IGNORE INTO coordinator_commands VALUES (?,?,?,?, 'pending',NULL)",
                           (identity, channel, "write", value_json))
            self.remember_histories(prepared, completions)
        self.deliver(identity, channel, "write", value)

    def deliver(self, identity, channel, operation, value):
        try:
            result = self.client.call("command", command_id=identity, channel=channel, operation=operation, value=value)
        except Exception:
            self.delivery_error = "Provider command acknowledgement was lost; its retained outcome is being reconciled."
            raise
        state, detail = self._record_outcome(identity, channel, operation, result)
        if state == "rejected":
            raise ProviderLaunchRejected(detail["error"])
        if state != "written":
            self.delivery_error = "Provider delivery is uncertain; the command will not be replayed."
            raise TransportError(self.delivery_error)
        return detail

    def _unstarted_channels(self):
        # Legacy owners closed a Popen failure with a NULL PID. A process
        # that ever started keeps its PID even when its channel is closed.
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
                # Keep damaged evidence verbatim and unresolved. A malformed
                # receipt is neither a rejection proof nor provider input.
                retained_result, detail, state = detail, None, "uncertain"
        if (operation == "open" and state == "uncertain" and isinstance(detail, dict)
                and str(detail.get("error", "")).startswith("[Errno 2] No such file or directory: ")):
            if unstarted_channels is None:
                unstarted_channels = self._unstarted_channels()
            if channel in unstarted_channels:
                state = "rejected"
        if state != "written" and not (state == "rejected" and operation == "open"):
            state = "uncertain"
        result_json = retained_result if retained_result is not None else json.dumps(detail)
        with self.store.database_operation("coordinator.command_outcome"), self.store._connection() as db:
            db.execute("""UPDATE coordinator_commands SET outcome=?, result_json=?,
                value_json=CASE WHEN ? IN ('written','rejected') THEN '{}' ELSE value_json END WHERE command_id=?""",
                (state, result_json, state, identity))
            if operation == "open" and state == "written":
                db.execute("UPDATE coordinator_channels SET pid=? WHERE channel=?", (detail["pid"], channel))
            elif state == "rejected":
                # No process was attached. Retain its terminal receipt, not a
                # derived NULL-PID channel that older coordinators cannot load.
                db.execute("DELETE FROM coordinator_channels WHERE channel=? AND pid IS NULL", (channel,))
        return state, detail

    def recover_commands(self, *, operations=None):
        """Reconcile durable intent against the owner's retained result.

        A confirmed absent identity is an unsent outbox entry. Resubmitting the
        exact recorded identity is safe; writing/uncertain are never replayed.
        """
        # A previous coordinator can remove a failed channel row while its
        # launch receipt survives. Inspect these opens during the existing
        # pending-command scan at startup; retain specifications only for
        # pending commands, which may still need the original bytes.
        repair_launches = operations == {"open"}
        with self.store.reading(), self.store._connection() as db:
            pending = db.execute("""SELECT command_id,channel,operation,outcome,result_json,
                CASE WHEN outcome='pending' THEN value_json END AS value_json FROM coordinator_commands o
                WHERE outcome='pending' OR (? AND outcome='uncertain' AND operation='open'
                AND command_id='open:' || channel AND instr(result_json, '[Errno 2] No such file or directory: ')>0
                AND NOT EXISTS (SELECT 1 FROM coordinator_channels c WHERE c.channel=o.channel AND c.pid IS NOT NULL))
                """, (repair_launches,)).fetchall()
        unstarted_channels = None
        for row in pending:
            if operations is not None and row["operation"] not in operations:
                continue
            if row["outcome"] == "uncertain":
                if unstarted_channels is None:
                    unstarted_channels = self._unstarted_channels()
                if row["channel"] in unstarted_channels:
                    self._record_outcome(row["command_id"], row["channel"], "open",
                                         {"outcome": "uncertain", "result": row["result_json"]},
                                         unstarted_channels=unstarted_channels)
                continue
            outcome = self.client.call("outcome", command_id=row["command_id"])
            if outcome["outcome"] == "absent":
                try:
                    self.deliver(row["command_id"], row["channel"], row["operation"], json.loads(row["value_json"]))
                except ProviderLaunchRejected:
                    pass  # The rejected receipt is durable; there is no process to attach.
                continue
            deadline = time.monotonic() + 10
            while outcome["outcome"] == "writing" and time.monotonic() < deadline:
                time.sleep(.02)
                outcome = self.client.call("outcome", command_id=row["command_id"])
            self._record_outcome(row["command_id"], row["channel"], row["operation"], outcome)
        with self.store.reading(), self.store._connection() as db:
            uncertain = db.execute("SELECT 1 FROM coordinator_commands WHERE outcome IN ('uncertain','writing') LIMIT 1").fetchone()
        self.delivery_error = ("A retained provider command has uncertain delivery; review its outcome before sending again."
                               if uncertain else None)

    def close_channel(self, channel):
        if getattr(self.sessions, "transport_handover", False):
            return
        self.client.command(channel, "close", {})
        self._mark_closed(channel)

    def close_exited_channel(self, channel):
        """Retire a channel whose output ended, before its replacement opens.

        The owner may already have reaped an exited provider; that channel has
        nothing left to close. A provider that outlived a failed reader still
        owns its process group and must not run beside the replacement.
        """
        if getattr(self.sessions, "transport_handover", False):
            raise CoordinationError("Codex app-server stopped during a coordinator handover; "
                                    "the replacement coordinator starts it again.")
        retained = self.client.call("status").get("channels", [])
        if any(entry["channel"] == channel and entry["state"] != "closed" for entry in retained):
            self.client.command(channel, "close", {})
        self._mark_closed(channel)

    def _mark_closed(self, channel):
        with self.lock:
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

    def project_batch(self, events):
        decoded = []
        for event in events:
            message = None
            if event["stream"] == "stdout":
                try:
                    message = json.loads(event["data"])
                except ValueError:
                    logging.getLogger(__name__).warning("Ignoring malformed provider frame at sequence %s", event["sequence"])
            decoded.append((event, message))
        with self.lock, self.sessions.claude.lock, self.sessions.lock:
            decoded = [(event, message) for event, message in decoded if event["sequence"] > self.cursor]
            if not decoded:
                return
            affected = {message.get("params", {}).get("threadId") for _, message in decoded
                        if isinstance(message, dict) and isinstance(message.get("params", {}), dict)}
            affected.update(self.channels.get(event["channel"], {}).get("thread_id") for event, _ in decoded)
            if any(event["stream"] == "eof" for event, _ in decoded):
                affected = None
            before = self.state(affected=affected)
            try:
                if affected is not None:
                    # While the next batch is uncommitted, an unrelated unloaded
                    # conversation can be read entirely from its published history.
                    # Never use this shortcut for resident state, input or health.
                    prior = before["browser"]
                    excluded = set(affected).union(prior["loaded"], prior["active"], prior["histories"], before["watchdog"])
                    for request in prior["requests"].values():
                        params = request.get("params")
                        if isinstance(params, dict) and isinstance(params.get("threadId"), str):
                            excluded.add(params["threadId"])
                    self.unloaded_observation = {"excluded": excluded, "revisions": prior["turn_revisions"]}
                with capture_writes() as writes:
                    self.in_event.active = True
                    self.in_event.publications = []
                    self.in_event.commands = []
                    self.in_event.completions = []
                    for event, message in decoded:
                        entry = self.channels.get(event["channel"])
                        if entry is None:
                            raise TransportError("Event belongs to a transport missing from the saved adapter state.")
                        self.in_event.sequence = event["sequence"]
                        self.in_event.command_index = 0
                        if entry.get("closed"):
                            continue
                        if event["stream"] == "stdout" and isinstance(message, dict):
                            entry["callback"](message)
                        elif event["stream"] == "eof":
                            entry["process"].returncode = 1
                            if entry["provider"] == "codex":
                                entry["callback"]({"method": "bridge/disconnected", "params": {}})
                            else:
                                entry["callback"]({"type": "disconnected", "error": "Provider transport closed."})
                    # Histories and the replay cursor share the same snapshot.
                    # Metadata callbacks are finite and borrow this transaction;
                    # nested callbacks append to this list before it is exhausted.
                    prepared, published_completions, encoded = self.prepare_checkpoint()
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
                            self.publish_histories(db, prepared)
                            db.executemany("INSERT OR IGNORE INTO coordinator_commands VALUES (?,?,?,?,'pending',NULL)",
                                           self.in_event.commands)
                            self._publish_rows(db, self.in_event.publications)
                            self._save(db, encoded, event["sequence"])
                self.cursor = event["sequence"]
                for sequence, payload in self.in_event.publications:
                    self.sessions.events.append(json.loads(payload))
                    self.sessions.sequence = sequence
                if self.in_event.publications:
                    self.sessions.changed.notify_all()
                self.remember_histories(prepared, published_completions)
                commands = list(self.in_event.commands)
                completions = list(self.in_event.completions)
            except Exception:
                self.restore(before)
                raise
            finally:
                self.unloaded_observation = None
                self.in_event.active = False
        for callback in completions:
            callback()
        for identity, channel, operation, value in commands:
            self.deliver(identity, channel, operation, json.loads(value))
        # No candidate consumes events before its durable activation commit.
        # Consequently this cursor is also the oldest restorable checkpoint;
        # pre-commit rollback retains the source cursor and cannot lose frames.
        self.client.call("acknowledge_events", cursor=self.cursor, retain_after=self.cursor)

    def start(self):
        self.recover_commands()
        self.stopped.clear()
        self.worker = threading.Thread(target=self._run, name="provider-projection", daemon=True)
        self.worker.start()
        rpc = self.sessions.rpc
        process = getattr(rpc, "process", None)
        # Readiness runs with admission closed. It finishes only the handshake of a
        # retained, running provider, which restores existing work; this entry
        # authorizes that handshake alone. An exited provider waits for its first
        # admitted request.
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
                events = self.client.call("events", after=self.cursor)["events"]
                if self.stopped.is_set():
                    return
                self.project_batch(events)
                self.error = None
                if not events:
                    self.stopped.wait(.02)
            except Exception as error:
                self.error = str(error)
                logging.getLogger(__name__).exception("Provider projection paused at cursor %s", self.cursor)
                self.stopped.wait(.25)

    def freeze(self):
        self.stopped.set()
        if self.worker is not None:
            self.worker.join(timeout=10)
            if self.worker.is_alive():
                raise TransportError("Provider projection did not reach its transaction boundary.")
        if self.error:
            raise TransportError("Provider projection is not healthy: " + self.error)
        # Planned handover also supports a reader that predates payload references.
        self.checkpoint(inline=True)
