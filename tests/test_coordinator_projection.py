"""CoordinatorProjection Stage 2/3 contracts: 5-provider attach/EOF/stderr, lock separation, and zero delta writes."""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import closing
import copy
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.request
import zlib

from test_codex_app_server import FakeCodex
from agent_coord.codex_app_server import BrowserSessions
from agent_coord.coordinator_projection import CoordinatorProjection, pack, unpack
from agent_coord.history_persistence import read_history
from agent_coord.provider_transport import ProviderCommandRejected, ProviderLaunchRejected
from agent_coord.store import CoordinationError, CoordinationStore


class ProjectionCodex(FakeCodex):
    def __init__(self, *args):
        super().__init__(*args)
        self.next_id, self.ready = 0, True

    def _message(self, message):
        self.callback(message)


class TransportClient:
    record = {"instance": "test-transport", "pid": 123}
    epoch = 1

    def __init__(self):
        self.commands = []

    def call(self, action, **values):
        self.commands.append((action, values))
        return {"outcome": "written", "result": {}}

    def command(self, *args, **kwargs):
        return {"closed": True}


class ProviderOwner(TransportClient):
    """Owner double that launches channels and answers Codex initialization."""

    pid = 456

    def __init__(self, projection, channels):
        super().__init__()
        self.projection, self.channels = projection, dict(channels)
        self.opened, self.closed = [], []

    def call(self, action, **values):
        if action == "status":
            return {"channels": [{"channel": name, "pid": 123, "state": state}
                                 for name, state in self.channels.items()]}
        if action == "events":
            return {"events": []}
        if action != "command":
            return super().call(action, **values)
        channel, operation, value = values["channel"], values["operation"], values["value"]
        if operation == "open":
            self.opened.append(channel)
            self.channels[channel] = "running"
            return {"outcome": "written", "result": {"pid": self.pid, "attached": False}}
        if operation == "close":
            self.closed.append(channel)
            self.channels[channel] = "closed"
            return {"outcome": "written", "result": {"closed": True}}
        message = json.loads(value["data"])
        if message.get("method") == "initialize":
            response = {"sequence": self.projection.cursor + 1, "channel": channel, "stream": "stdout",
                        "data": json.dumps({"id": message["id"], "result": {}})}
            threading.Thread(target=self.projection.project, args=(response,), daemon=True).start()
        return {"outcome": "written", "result": {"bytes": len(value["data"])}}

    def command(self, channel, operation, value, **_):
        return self.call("command", channel=channel, operation=operation, value=value)["result"]


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = CoordinationStore(Path(self.temporary.name) / "state.sqlite3")
        self.sessions = BrowserSessions(self.store, self.temporary.name, rpc_factory=ProjectionCodex)
        self.addCleanup(self.sessions.close)
        self.identity = self.sessions.create({})["session"]["thread_id"]
        self.sessions.send(self.identity, {"message": "Initial work"})
        self.assertTrue(self.sessions.persistence.flush())
        self.client = TransportClient()
        self.projection = CoordinatorProjection(self.sessions, self.client)
        self.sessions.projection = self.projection
        self.store.provider_projection = self.projection
        self.projection.attach("codex", 123, self.sessions._event, provider="codex")
        self.turn_id = self.sessions.active[self.identity]

    def event(self, sequence, method, **params):
        return {"sequence": sequence, "channel": "codex", "stream": "stdout",
                "data": json.dumps({"method": method, "params": {
                    "threadId": self.identity, "turnId": self.turn_id, **params}})}

    def saved_conversation(self):
        identity = "saved-conversation"
        self.store.register(session_id=identity, client="codex", cwd=self.temporary.name, name="Saved")
        history = {"id": identity, "turns": [{"id": "old", "status": "completed",
                    "items": [{"type": "agentMessage", "id": "message", "text": "Saved answer"}]}]}
        with self.store._connection() as db:
            db.execute("""INSERT INTO browser_sessions(thread_id,cwd,name,created_at,updated_at)
                VALUES(?,?,?,1,1)""", (identity, self.temporary.name, "Saved"))
        self.store.transcripts.save_turns(identity, history)
        return identity

    def test_codec_preserves_integer_indices_tuples_and_sets(self):
        value = {
            "blocks": {"thread": {0: {"id": "item"}}},
            "requests": {("thread", ("turn", 1)): ["pending"]},
            "confirmed": {("thread", "item")},
            "empty": set(),
        }
        self.assertEqual(unpack(json.loads(json.dumps(pack(value)))), value)
        with self.assertRaises(TypeError):
            pack(object())

    def test_projection_lock_is_distinct_from_sessions_lock_and_encodes_outside_sessions_lock(self):
        self.assertIsNot(self.projection.projection_lock, self.sessions.lock)
        observed_locked = []
        real_compress = zlib.compress

        def check_lock_during_compress(data, *args, **kwargs):
            # Another thread must be able to acquire sessions.lock while projection compresses state.
            with ThreadPoolExecutor(max_workers=1) as pool:
                can_acquire = pool.submit(lambda: self.sessions.lock.acquire(timeout=0.5)).result(timeout=1.0)
                if can_acquire:
                    pool.submit(self.sessions.lock.release)
            observed_locked.append(can_acquire)
            return real_compress(data, *args, **kwargs)

        with patch("agent_coord.coordinator_projection.zlib.compress", side_effect=check_lock_during_compress):
            self.projection.checkpoint()
        self.assertEqual(observed_locked, [True])

    def test_streaming_deltas_perform_zero_sqlite_writes_and_append_to_live_jsonl(self):
        self.projection.checkpoint()
        before_scopes = self.store.database_metrics()["completed_scopes"]
        batch = [
            self.event(1, "item/started", item={"id": "output", "type": "agentMessage", "text": ""}),
            self.event(2, "item/agentMessage/delta", itemId="output", delta="Hello "),
            self.event(3, "item/agentMessage/delta", itemId="output", delta="world"),
            self.event(4, "item/completed", item={"id": "output", "type": "agentMessage", "text": "Hello world"}),
        ]
        self.projection.project_batch(batch)
        after_scopes = self.store.database_metrics()["completed_scopes"]
        self.assertEqual(after_scopes - before_scopes, 0, "Streaming frames must perform zero SQLite writes")
        self.assertEqual(self.projection.cursor, 4)

        live_events = self.store.transcripts.read_live_events(self.identity)
        self.assertEqual(len(live_events), 4)

        # Boundary turn/completed commits finalized turn to TranscriptStore and truncates live.jsonl.
        completed = {"id": self.turn_id, "status": "completed", "items": [
            {"id": "output", "type": "agentMessage", "text": "Hello world"}]}
        self.projection.project(self.event(5, "turn/completed", turn=completed))
        self.assertGreater(self.store.database_metrics()["completed_scopes"], after_scopes)
        self.assertEqual(self.store.transcripts.read_live_events(self.identity), [])
        self.assertEqual(read_history(self.store, self.identity)["turns"][-1]["items"][-1]["text"], "Hello world")

    def test_cross_track_contract_stderr_eof_owns_provider_and_all_five_provider_reattach(self):
        stderr_chunks = []
        acp_events = []
        claude_events = []

        self.client.call = lambda action, **values: (
            {"outcome": "written", "result": {"pid": 789}} if action == "command" else {"outcome": "written", "result": {}}
        )
        self.projection.open(
            "antigravity:t-ag",
            ["agy"],
            {},
            self.temporary.name,
            acp_events.append,
            stderr_callback=stderr_chunks.append,
            provider="antigravity",
            thread_id="t-ag",
        )
        self.projection.attach(
            "kimi:t-km",
            790,
            acp_events.append,
            stderr_callback=stderr_chunks.append,
            provider="kimi",
            thread_id="t-km",
        )
        self.projection.attach(
            "muse:t-mu",
            791,
            acp_events.append,
            provider="muse",
            thread_id="t-mu",
        )
        self.projection.attach(
            "claude:t-cl",
            792,
            claude_events.append,
            provider="claude",
            thread_id="t-cl",
        )

        # Verify owns_provider across all 5 providers.
        self.assertTrue(self.projection.owns_provider("codex"))
        self.assertTrue(self.projection.owns_provider("antigravity", "t-ag"))
        self.assertFalse(self.projection.owns_provider("antigravity", "other-thread"))
        self.assertTrue(self.projection.owns_provider("kimi", "t-km"))
        self.assertTrue(self.projection.owns_provider("muse", "t-mu"))
        self.assertTrue(self.projection.owns_provider("claude", "t-cl"))

        # Verify stderr forwarding on both open() and attach() channels.
        self.projection.project_batch([
            {"sequence": 1, "channel": "antigravity:t-ag", "stream": "stderr", "data": "OAuth URL: https://example.test\n"},
            {"sequence": 2, "channel": "kimi:t-km", "stream": "stderr", "data": "kimi diagnostic\n"},
        ])
        self.assertEqual(stderr_chunks, ["OAuth URL: https://example.test\n", "kimi diagnostic\n"])
        self.assertEqual(acp_events, [])

        # Verify load() reattaches antigravity, kimi, and muse via adapter.attach_channel.
        with self.store._connection() as db:
            db.execute("INSERT OR REPLACE INTO coordinator_channels VALUES ('kimi:t-km','kimi','t-km','main',790,0)")
            db.execute("INSERT OR REPLACE INTO coordinator_channels VALUES ('muse:t-mu','muse','t-mu','main',791,0)")
        self.projection.checkpoint()

        reattached = []
        for provider_name in ("antigravity", "kimi", "muse"):
            setattr(
                self.sessions,
                provider_name,
                SimpleNamespace(
                    attach_channel=lambda tid, ch, pid, returncode=None, p=provider_name: reattached.append(
                        (p, tid, ch, pid, returncode)
                    ),
                    close=lambda: None,
                    discard_persisted_completion=lambda *a: None,
                    _read_history=lambda tid: read_history(self.store, tid),
                ),
            )
        restored = CoordinatorProjection(self.sessions, self.client)
        self.store.provider_projection = restored
        try:
            restored.load()
        finally:
            self.store.provider_projection = self.projection
        self.assertEqual(
            set(reattached),
            {
                ("antigravity", "t-ag", "antigravity:t-ag", 789, None),
                ("kimi", "t-km", "kimi:t-km", 790, None),
                ("muse", "t-mu", "muse:t-mu", 791, None),
            },
        )

        # Verify EOF framing per provider family.
        self.projection.project({"sequence": 3, "channel": "antigravity:t-ag", "stream": "eof", "data": ""})
        self.projection.project({"sequence": 4, "channel": "kimi:t-km", "stream": "eof", "data": ""})
        self.projection.project({"sequence": 5, "channel": "muse:t-mu", "stream": "eof", "data": ""})
        self.projection.project({"sequence": 6, "channel": "claude:t-cl", "stream": "eof", "data": ""})
        self.assertEqual(
            acp_events,
            [{"method": "disconnected", "params": {"error": "Provider transport closed."}}] * 3,
        )
        self.assertEqual(claude_events, [{"type": "disconnected", "error": "Provider transport closed."}])

    def test_per_channel_callback_fault_isolation_prevents_global_cursor_halt(self):
        def broken_callback(_message):
            raise ValueError("unexpected provider schema")

        self.projection.attach("kimi:broken", 901, broken_callback, provider="kimi", thread_id="broken-thread")
        with self.assertLogs("agent_coord.coordinator_projection", "ERROR"):
            self.projection.project_batch([
                {"sequence": 1, "channel": "kimi:broken", "stream": "stdout",
                 "data": json.dumps({"method": "session/update", "params": {}})},
                self.event(2, "item/started", item={"id": "healthy", "type": "agentMessage", "text": "healthy output"}),
            ])
        self.assertEqual(self.projection.cursor, 2)
        self.assertIn("unexpected provider schema", self.projection.channels["kimi:broken"]["isolated_error"])
        self.assertEqual(self.sessions.histories[self.identity]["turns"][-1]["items"][-1]["text"], "healthy output")

    def test_sampled_claude_watchdog_does_not_block_codex_command_or_restore(self):
        from agent_coord.claude_progress import TranscriptProgress
        native = "00000000-0000-0000-0000-000000000001"
        config = Path(self.temporary.name) / "claude"
        transcript = config / "projects" / "project" / (native + ".jsonl")
        transcript.parent.mkdir(parents=True)
        transcript.write_text("{}\n")
        watchdog = self.sessions.watchdog
        observed = watchdog._new("claude-turn")
        observed.update(tools={"active-tool"}, classification="long_running_tool",
                        alert_pending=True, next_alert_at=self.store.clock() + 60)
        watchdog.observed["claude-thread"] = observed
        retained = copy.deepcopy(observed)
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(config)}), \
                patch("agent_coord.turn_watchdog.native_id", return_value=native):
            latest, _ = watchdog._sample_claude("claude-thread", observed, lambda: {})
            self.assertEqual(latest, transcript.stat().st_mtime)
            sampler = observed["transcripts"]
            self.assertIsInstance(sampler, TranscriptProgress)
            self.projection.checkpoint()
            self.assertIs(observed["transcripts"], sampler)
            self.assertIsInstance(self.projection.state()["watchdog"]["claude-thread"]["transcripts"],
                                  TranscriptProgress)
            self.projection.write("codex", {"id": 9, "method": "turn/start", "params": {}})
            self.assertEqual(len(self.client.commands), 1)
            self.projection.load()
            restored = watchdog.observed["claude-thread"]
            self.assertEqual(restored, retained)
            self.assertEqual(watchdog._sample_claude("claude-thread", restored, lambda: {})[0], latest)
            self.assertIsInstance(restored["transcripts"], TranscriptProgress)
            self.assertIsNot(restored["transcripts"], sampler)

    def test_existing_legacy_payload_database_loads_cleanly_and_cleans_payload_table_on_checkpoint(self):
        text = "v3-segment-" * 4000
        raw_bytes = text.encode("utf-8")
        digest = __import__("hashlib").sha256(raw_bytes).hexdigest()
        state = self.projection.state(durable=True)
        state["claude"]["turns"] = {self.identity: {"id": "old", "items": [{"id": "i1", "text": text}]}}
        packed = pack(state, encode_text=lambda _s: [digest])
        with self.store._connection() as db:
            db.execute("CREATE TABLE coordinator_projection_payloads (digest TEXT PRIMARY KEY, compressed BLOB NOT NULL)")
            db.execute("INSERT INTO coordinator_projection_payloads VALUES (?, ?)", (digest, zlib.compress(raw_bytes)))
            db.execute(
                "INSERT OR REPLACE INTO coordinator_projection VALUES (1, 3, ?, 0, ?)",
                (self.client.record["instance"], zlib.compress(json.dumps(packed).encode("utf-8"))),
            )
        self.sessions.claude.turns.clear()
        reloaded = CoordinatorProjection(self.sessions, self.client)
        reloaded.load()
        self.assertEqual(self.sessions.claude.turns[self.identity]["items"][0]["text"], text)
        reloaded.checkpoint()
        with self.store.reading(), self.store._connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM coordinator_projection_payloads").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT version FROM coordinator_projection").fetchone()[0], 1)

    def test_unloaded_conversation_opens_while_another_provider_checkpoint_is_pending(self):
        identity = self.saved_conversation()
        entered, release = threading.Event(), threading.Event()
        encode = self.projection._encode_checkpoint

        def held_encode(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError("Checkpoint barrier was not released")
            return encode(*args, **kwargs)

        def open_saved():
            with self.store.reading():
                return self.sessions.read(identity, recent=True, snapshot=True)

        completed = {"id": self.turn_id, "status": "completed", "items": []}
        with patch.object(self.projection, "_encode_checkpoint", side_effect=held_encode), ThreadPoolExecutor(2) as pool:
            event = self.event(1, "turn/completed", turn=completed)
            projection = pool.submit(self.projection.project_batch, [event])
            try:
                self.assertTrue(entered.wait(2))
                response = pool.submit(open_saved).result(timeout=1)
                self.assertEqual(response["thread"]["turns"][0]["items"][0]["text"], "Saved answer")
                self.assertEqual(response["requests"], [])
                self.assertFalse(response["running"])
            finally:
                release.set()
            projection.result(timeout=2)

    def test_saved_open_rechecks_input_when_a_projection_overtakes_the_read(self):
        from agent_coord import codex_app_server
        identity = self.saved_conversation()
        orig_read = codex_app_server.read_history
        self.projection.unloaded_observation = {"excluded": {self.identity}, "revisions": {}}

        def crossed_commit(store, thread_id, **options):
            history = orig_read(store, thread_id, **options)
            if self.projection.unloaded_observation is not None:
                with self.sessions.lock:
                    self.sessions.requests["waiting"] = {
                        "id": 33, "key": "waiting", "method": "item/tool/requestUserInput",
                        "received_at": store.clock(), "params": {"threadId": identity}}
                    self.projection.unloaded_observation = None
            return history

        with patch.object(codex_app_server, "read_history", side_effect=crossed_commit), self.store.reading():
            response = self.sessions.read(identity, recent=True, snapshot=True)
        self.assertEqual([request["key"] for request in response["requests"]], ["waiting"])
        self.assertEqual(response["work_thread"]["status"], "needs input")

    def test_boundary_projection_failure_rolls_back_effects_state_and_cursor(self):
        completed = {"id": self.turn_id, "status": "completed", "items": []}
        event = self.event(1, "turn/completed", turn=completed)
        original = self.projection._save

        def failed(db, state, cursor):
            db.execute("UPDATE browser_sessions SET name='uncommitted' WHERE thread_id=?", (self.identity,))
            original(db, state, cursor)
            raise sqlite3.IntegrityError("injected after projection writes")

        before = self.projection.state()
        with patch.object(self.projection, "_save", side_effect=failed):
            with self.assertRaisesRegex(sqlite3.IntegrityError, "injected"):
                self.projection.project(event)
        self.assertEqual(self.projection.state(), before)
        self.assertEqual(self.projection.cursor, 0)
        with self.store.reading(), self.store._connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM coordinator_projection").fetchone()[0], 0)
            self.assertNotEqual(db.execute("SELECT name FROM browser_sessions WHERE thread_id=?", (self.identity,)).fetchone()[0], "uncommitted")
        self.projection.project(event)
        self.assertEqual(self.projection.cursor, 1)

    def test_only_committed_matching_completions_leave_recovery_state(self):
        completed = {"id": self.turn_id, "status": "completed", "items": []}
        self.sessions.claude.completed[self.identity] = copy.deepcopy(completed)
        self.sessions.histories[self.identity] = {"id": self.identity, "turns": [{**completed, "status": "failed"}]}
        self.sessions._dirty_history(self.identity)
        self.projection.checkpoint()
        self.assertIn(self.identity, self.sessions.claude.completed)
        self.sessions.histories[self.identity]["turns"] = [completed]
        self.sessions._dirty_history(self.identity)
        with patch.object(self.projection, "_save", side_effect=sqlite3.IntegrityError("rollback")):
            with self.assertRaises(sqlite3.IntegrityError):
                self.projection.checkpoint()
        self.assertIn(self.identity, self.sessions.claude.completed)
        self.assertEqual(read_history(self.store, self.identity)["turns"][0]["status"], "failed")
        self.projection.checkpoint()
        self.assertNotIn(self.identity, self.sessions.claude.completed)
        with self.store.reading(), self.store._connection() as db:
            encoded = db.execute("SELECT state FROM coordinator_projection").fetchone()[0]
        self.assertNotIn(self.identity, unpack(json.loads(zlib.decompress(encoded)))["claude"]["completed"])
        self.assertEqual(read_history(self.store, self.identity)["turns"], [completed])

    def test_legacy_completion_reconciles_from_saved_transcripts_after_readiness(self):
        completed = {"id": self.turn_id, "status": "interrupted", "items": [{"id": "large", "text": "x" * 300000}]}
        self.sessions.histories[self.identity] = {"id": self.identity, "turns": [completed]}
        self.sessions._dirty_history(self.identity)
        self.projection.checkpoint()
        self.sessions.claude.completed.update({self.identity: completed, "unpublished": completed})
        with self.store._connection() as db:
            state = self.projection.state(durable=True)
            db.execute("UPDATE coordinator_projection SET state=? WHERE singleton=1",
                       (zlib.compress(json.dumps(pack(state)).encode("utf-8")),))
        self.projection.load()
        self.assertEqual(self.sessions.histories, {})
        while self.projection.restored_completions:
            self.projection.reconcile_restored_completion()
        self.assertEqual(self.sessions.histories, {})
        self.assertEqual(self.sessions.claude.completed, {"unpublished": completed})

    def test_external_http_history_read_journals_transport_without_claiming_session(self):
        from agent_coord.codex_app_server import CodexRPC
        from agent_coord.ui import make_ui_server

        self.store.register(session_id="terminal", client="codex", cwd=self.temporary.name)
        before = self.store.get_session("terminal")
        rpc = self.sessions.history_rpc = CodexRPC(self.store, lambda _: None)
        rpc.channel, rpc.ready = "history", True
        self.projection.attach(rpc.channel, 123, rpc._message, provider="codex")
        native = {"id": "terminal", "turns": [{"id": "saved", "status": "completed", "items": []}]}

        def deliver(action, **values):
            if action == "command":
                message = json.loads(values["value"]["data"])
                self.assertEqual(message["method"], "thread/read")
                rpc._message({"id": message["id"], "result": {"thread": native}})
            return {"outcome": "written", "result": {}}

        self.client.call = deliver
        server = make_ui_server(self.store, port=0, cwd=self.temporary.name, browser_sessions=self.sessions)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            for view in ("recent", "complete"):
                request = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/api/browser/sessions/terminal",
                                                 headers={"X-Agent-Coord-History-View": view})
                with urllib.request.urlopen(request, timeout=5) as response:
                    result = json.load(response)
                self.assertFalse(result["thread"]["historyUnavailable"])
                self.assertEqual(result["thread"]["turns"][0]["id"], "saved")
        finally:
            server.shutdown()
            server.server_close()
            worker.join(2)
        self.assertEqual(self.store.get_session("terminal"), before)
        with self.store.reading(), self.store._connection() as db:
            count = db.execute("SELECT COUNT(*) FROM coordinator_commands WHERE channel='history' AND outcome='written'").fetchone()[0]
        self.assertEqual(count, 2)

    def test_replaying_committed_delta_has_no_second_effect_and_watermark_persists_cursor(self):
        self.projection.project(self.event(1, "item/started", item={"id": "output", "type": "agentMessage", "text": ""}))
        delta = self.event(2, "item/agentMessage/delta", itemId="output", delta="once")
        self.projection.project(delta)
        sequence = self.sessions.sequence
        self.projection.project(delta)
        self.assertEqual(self.sessions.sequence, sequence)
        turn = self.sessions.histories[self.identity]["turns"][-1]
        self.assertEqual(next(item["text"] for item in turn["items"] if item["id"] == "output"), "once")
        self.projection._persist_watermark()
        with self.store.reading(), self.store._connection() as db:
            self.assertEqual(db.execute("SELECT cursor FROM coordinator_projection").fetchone()[0], 2)

    def test_signed_legacy_watchdog_restores_approval_and_processes_next_event(self):
        self.projection.attach("codex", 123, self.sessions.rpc._message, provider="codex")
        self.projection.project(self.event(1, "item/started", item={"id": "output", "type": "agentMessage", "text": ""}))
        self.projection.checkpoint()
        state = self.projection.state(durable=True)
        progress = self.sessions.watchdog.clock() - 45
        state["watchdog"] = {self.identity: {"turn_id": self.turn_id, "progress": progress, "probe": None,
                                            "state": "waiting", "detail": None, "tools": {"original-tool"}}}
        request = {"id": 7, "key": "approval-7", "method": "item/commandExecution/requestApproval",
                   "params": {"threadId": self.identity, "turnId": self.turn_id}}
        state["browser"]["requests"] = {"approval-7": request}
        with self.store._connection() as db:
            db.execute("UPDATE coordinator_projection SET version=1,state=? WHERE singleton=1",
                       (zlib.compress(json.dumps(pack(state)).encode()),))
        self.projection.load()
        watchdog = self.sessions.watchdog
        self.assertEqual(watchdog.observed[self.identity]["progress"], progress)
        self.assertEqual(watchdog.observed[self.identity]["tools"], {"original-tool"})
        self.assertGreaterEqual(watchdog.snapshot(self.identity)["quiet_seconds"], 45)
        event = self.event(2, "item/agentMessage/delta", itemId="output", delta="continued")
        self.projection.project(event)
        self.projection.project(event)
        self.assertEqual(self.projection.cursor, 2)
        self.assertEqual(self.sessions.histories[self.identity]["turns"][-1]["items"][-1]["text"], "continued")
        self.assertEqual(self.sessions.requests, {"approval-7": request})

    def test_restore_keeps_histories_lazy_and_replays_live_jsonl_on_first_event(self):
        self.projection.attach("codex", 123, self.sessions.rpc._message, provider="codex")
        self.projection.checkpoint()
        self.projection.project(self.event(1, "item/started", item={"id": "output", "type": "agentMessage", "text": "retained "}))
        self.projection._persist_watermark()
        with patch("agent_coord.history_persistence.read_history", side_effect=AssertionError("eager startup history read")):
            self.projection.load()
        self.assertEqual(self.sessions.histories, {})
        self.assertEqual(self.sessions.dirty_histories, set())
        self.assertIn(self.identity, self.sessions.loaded)
        self.assertEqual(self.sessions.active[self.identity], self.turn_id)
        self.assertEqual(self.projection.cursor, 1)
        delta = self.event(2, "item/agentMessage/delta", itemId="output", delta="once")
        self.projection.project(delta)
        self.projection.project(delta)
        self.assertEqual(self.sessions.histories[self.identity]["turns"][-1]["items"][-1]["text"], "retained once")
        self.assertEqual(self.projection.cursor, 2)

    def test_command_identity_and_state_are_durable_before_delivery(self):
        def deliver(action, **values):
            with self.store.reading(), self.store._connection() as db:
                row = db.execute("SELECT * FROM coordinator_commands WHERE command_id=?", (values["command_id"],)).fetchone()
                self.assertEqual(row["outcome"], "pending")
                self.assertEqual(db.execute("SELECT cursor FROM coordinator_projection").fetchone()[0], 0)
            self.client.commands.append(values["command_id"])
            return {"outcome": "written", "result": {"bytes": 1}}

        self.client.call = deliver
        self.projection.write("codex", {"id": 9, "method": "turn/start", "params": {}})
        with self.store.reading(), self.store._connection() as db:
            self.assertEqual(db.execute("SELECT outcome FROM coordinator_commands").fetchone()[0], "written")

    def test_borrowed_transaction_cannot_commit_and_rolls_back_as_a_unit(self):
        with self.assertRaisesRegex(ValueError, "rollback"):
            with self.store._connection() as db:
                db.execute("BEGIN IMMEDIATE")
                with self.store.sharing_transaction(db):
                    with self.store._connection() as nested:
                        nested.execute("BEGIN IMMEDIATE")
                        nested.execute("UPDATE browser_sessions SET name='uncommitted' WHERE thread_id=?", (self.identity,))
                        with self.assertRaises(CoordinationError):
                            nested.commit()
                raise ValueError("rollback")
        with self.store.reading(), self.store._connection() as db:
            self.assertNotEqual(db.execute("SELECT name FROM browser_sessions WHERE thread_id=?", (self.identity,)).fetchone()[0], "uncommitted")

    def test_malformed_frames_advance_without_losing_later_output(self):
        invalid = {"sequence": 1, "channel": "codex", "stream": "stdout", "data": "warning: diagnostic\n"}
        valid = self.event(2, "item/started", item={"id": "output", "type": "agentMessage", "text": "retained"})
        with self.assertLogs("agent_coord.coordinator_projection", "WARNING"):
            self.projection.project_batch([invalid, valid])
        self.assertEqual(self.projection.cursor, 2)
        self.assertEqual(self.sessions.histories[self.identity]["turns"][-1]["items"][-1]["text"], "retained")

    def test_pending_written_command_is_reconciled_without_resend(self):
        with self.store._connection() as db:
            db.execute("INSERT INTO coordinator_commands VALUES ('retained','codex','write',?, 'pending',NULL)",
                       (json.dumps({"data": "frame\n"}),))
        calls = []

        def retained(action, **_values):
            calls.append(action)
            return {"outcome": "written", "result": "{}"}

        self.client.call = retained
        self.projection.recover_commands()
        self.assertEqual(calls, ["outcome"])
        with self.store.reading(), self.store._connection() as db:
            self.assertEqual(db.execute("SELECT outcome FROM coordinator_commands").fetchone()[0], "written")

    def test_transport_refusal_is_terminal_and_is_not_replayed_after_recovery(self):
        with patch.object(self.client, "call", side_effect=ProviderCommandRejected("Receipt capacity reached")):
            with self.assertRaises(ProviderCommandRejected):
                self.projection.write("codex", {"id": 9, "method": "turn/start", "params": {}})
        with self.store.reading(), self.store._connection() as db:
            row = db.execute("SELECT outcome,value_json FROM coordinator_commands").fetchone()
        self.assertEqual(row["outcome"], "rejected")
        self.assertEqual(row["value_json"], "{}")
        with patch.object(self.client, "call") as call:
            self.projection.recover_commands()
            call.assert_not_called()

    def test_failed_open_from_retained_transport_has_no_provider_input(self):
        error = "[Errno 2] No such file or directory: '/removed-worktree'"
        receipt = {"outcome": "uncertain", "result": {"error": error}}
        channel = "claude:missing-worktree"
        status = {"channels": [{"channel": channel, "pid": None, "state": "closed"}]}
        with patch.object(self.client, "call", side_effect=[receipt, status]):
            with self.assertRaisesRegex(ProviderLaunchRejected, "removed-worktree"):
                self.projection.open(channel, ["claude"], {}, "/removed-worktree", lambda _: None,
                                     provider="claude", thread_id=self.identity)
        with self.store.reading(), self.store._connection() as db:
            command = db.execute("SELECT outcome,value_json FROM coordinator_commands WHERE command_id=?",
                                 ("open:" + channel,)).fetchone()
            self.assertEqual(tuple(command), ("rejected", "{}"))
            self.assertIsNone(db.execute("SELECT 1 FROM coordinator_channels WHERE channel=?", (channel,)).fetchone())

    def test_restart_recovers_rejected_open_without_replay_or_inert_channel(self):
        self.projection.checkpoint()
        channel = "claude:rejected-launch"
        with self.store._connection() as db:
            db.execute("INSERT INTO coordinator_channels VALUES (?, 'claude', ?, 'main', NULL, 0)",
                       (channel, self.identity))
            db.execute("INSERT INTO coordinator_commands VALUES (?, ?, 'open', '{}', 'pending', NULL)",
                       ("open:" + channel, channel))
        receipt = {"outcome": "rejected", "result": {"error": "Launch directory was removed"}}
        with patch.object(self.client, "call", return_value=receipt) as transport:
            projection = CoordinatorProjection(self.sessions, self.client)
            projection.load()
            projection.checkpoint()
            restored = CoordinatorProjection(self.sessions, self.client)
            restored.load()
            transport.assert_called_once_with("outcome", command_id="open:" + channel)
        self.assertNotIn(channel, restored.channels)
        self.assertIsNone(restored.delivery_error)

    def test_provider_delivery_does_not_hold_browser_lock(self):
        from agent_coord.codex_app_server import CodexRPC
        rpc = self.sessions.rpc = CodexRPC(self.store, self.sessions._event)
        rpc.channel = "codex"
        entered, release = threading.Event(), threading.Event()
        outcomes = []

        def blocked_delivery(_action, **_values):
            entered.set()
            if not release.wait(timeout=3):
                raise TimeoutError("Test did not release delivery")
            return {"outcome": "written", "result": {"bytes": 1}}

        def request():
            try:
                outcomes.append(rpc.request("thread/read", start=False, timeout=3))
            except Exception as error:
                outcomes.append(error)

        self.client.call = blocked_delivery
        worker = threading.Thread(target=request)
        worker.start()
        try:
            self.assertTrue(entered.wait(timeout=2))
            acquired = self.sessions.lock.acquire(timeout=.25)
            try:
                self.assertTrue(acquired, "Provider input backpressure blocked browser state")
                with self.store.reading(), self.store._connection() as db:
                    row = db.execute("SELECT state FROM coordinator_projection").fetchone()
                    state = unpack(json.loads(zlib.decompress(row[0])))
                    command = db.execute("SELECT value_json FROM coordinator_commands").fetchone()
                message = json.loads(json.loads(command[0])["data"])
                self.assertEqual(state["codex"]["pending_methods"][message["id"]], "thread/read")
            finally:
                if acquired:
                    self.sessions.lock.release()
        finally:
            release.set()
            rpc._message({"id": 1, "result": {"thread": "retained"}})
            worker.join(timeout=4)
        self.assertEqual(outcomes, [{"thread": "retained"}])

    def test_provider_eof_rejects_pending_rpc_only_after_commit(self):
        from agent_coord.codex_app_server import CodexRPC
        rpc = CodexRPC(self.store, self.sessions._event)
        self.sessions.rpc = rpc
        rpc.ready = True
        rpc.channel = "codex"
        rpc.process = self.projection.attach("codex", 123, rpc._message, provider="codex")
        future = rpc.pending[1] = Future()
        rpc.pending_methods[1] = "thread/read"
        event = {"sequence": 1, "channel": "codex", "stream": "eof", "data": ""}
        with patch.object(self.projection, "_save", side_effect=sqlite3.IntegrityError("rollback")):
            with self.assertRaises(sqlite3.IntegrityError):
                self.projection.project(event)
        self.assertTrue(rpc.ready)
        self.assertFalse(future.done())
        self.projection.project(event)
        self.assertFalse(rpc.ready)
        with self.assertRaisesRegex(CoordinationError, "disconnected"):
            future.result(timeout=1)

    def test_next_request_replaces_codex_provider_whose_output_ended(self):
        from agent_coord.codex_app_server import CodexRPC
        for retained in ("running", "closed"):
            with self.subTest(retained=retained):
                transport = ProviderOwner(self.projection, {"codex": retained})
                self.projection.client = transport
                rpc = CodexRPC(self.store, self.sessions._event)
                self.sessions.rpc = rpc
                rpc.ready = rpc.initialized_response = True
                rpc.channel = "codex"
                rpc.process = self.projection.attach("codex", 123, rpc._message, provider="codex")
                self.projection.project({"sequence": self.projection.cursor + 1, "channel": "codex",
                                         "stream": "eof", "data": ""})
                self.assertFalse(rpc.ready)
                rpc.start()
                self.assertEqual(transport.closed, ["codex"] if retained == "running" else [])
                self.assertNotEqual(rpc.channel, "codex")
                self.assertEqual(transport.opened, [rpc.channel])
                self.assertTrue(rpc.ready)

    def test_confirmed_stop_clears_transport_projection_but_retains_history(self):
        from agent_coord.desktop_runtime import clear_stopped_projection
        self.projection.project(self.event(1, "item/started", item={"id": "output", "type": "agentMessage", "text": "retained"}))
        clear_stopped_projection(self.store.database_path)
        with self.store.reading(), self.store._connection() as db:
            for table in ("coordinator_projection", "coordinator_commands", "coordinator_channels", "coordinator_events"):
                self.assertEqual(db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0], 0)
        history = read_history(self.store, self.identity)
        self.assertEqual(history["turns"][-1]["items"][-1]["text"], "retained")

    def test_lost_owner_maintenance_retains_commands_and_interrupts_work(self):
        from agent_coord.desktop_runtime import interrupt_stopped_projection
        self.sessions.active["unregistered-native-child"] = "child-turn"
        self.sessions.usage_recovery_attempts.add((self.identity, self.turn_id))
        self.projection.project(self.event(1, "item/started", item={"id": "output", "type": "agentMessage", "text": "retained"}))
        self.projection._persist_watermark()
        self.sessions.queue.enqueue(self.identity, {"message": "Review before sending"})
        with self.store._connection() as db:
            db.execute("INSERT INTO coordinator_commands VALUES ('uncertain-command','codex','write','{}','uncertain',NULL)")
        archive = Path(self.temporary.name) / "retained-evidence"
        archive.mkdir()
        for _ in range(2):
            interrupt_stopped_projection(self.store.database_path, archive, "Provider ownership ended in a previous boot")
        with closing(sqlite3.connect(archive / "coordinator.sqlite3")) as evidence:
            self.assertEqual(evidence.execute("SELECT outcome FROM coordinator_commands").fetchone()[0], "uncertain")
            self.assertEqual(evidence.execute("SELECT cursor FROM coordinator_projection").fetchone()[0], 1)
        with self.store.reading(), self.store._connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM coordinator_projection").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT status FROM turn_completions WHERE thread_id=?", (self.identity,)).fetchone()[0], "interrupted")
            self.assertEqual(db.execute("SELECT state FROM browser_message_queue WHERE thread_id=?", (self.identity,)).fetchone()[0], "paused")

    def test_sse_publication_and_future_wait_for_commit(self):
        observed = []
        original = self.sessions._event

        def callback(message):
            original(message)
            self.projection.after_commit(lambda: observed.append("committed"))

        self.projection.channels["codex"]["callback"] = callback
        event = self.event(1, "item/started", item={"id": "output", "type": "agentMessage", "text": ""})
        sequence = self.sessions.sequence

        def reject(*_args):
            self.assertEqual(self.sessions.sequence, sequence)
            raise sqlite3.IntegrityError("failure")

        with patch.object(self.projection, "_save", side_effect=reject):
            with self.assertRaises(sqlite3.IntegrityError):
                self.projection.project(event)
        self.assertEqual(observed, [])
        self.projection.project(event)
        self.assertEqual(observed, ["committed"])
        with self.store.reading(), self.store._connection() as db:
            row = db.execute("SELECT event_json FROM coordinator_events ORDER BY sequence DESC LIMIT 1").fetchone()
            self.assertEqual(json.loads(row[0])["seq"], self.sessions.sequence)

    def test_delayed_native_read_cannot_replace_newer_completed_output(self):
        stale = copy.deepcopy(self.sessions.histories[self.identity])
        completed = {"id": self.turn_id, "status": "completed", "items": [
            {"id": "output", "type": "agentMessage", "text": "Complete output"}]}

        def read(*_args, **_kwargs):
            self.projection.project(self.event(1, "turn/completed", turn=completed))
            return stale

        with patch.object(self.sessions, "_query_thread", side_effect=read):
            result = self.sessions._read_thread(self.identity)
        self.assertEqual(result["turns"][-1]["status"], "completed")
        self.assertIn("Complete output", [item.get("text") for item in result["turns"][-1]["items"]])

    def test_partial_native_history_retains_committed_stream_items(self):
        stale = copy.deepcopy(self.sessions.histories[self.identity])
        self.projection.project(self.event(1, "item/started", item={"id": "output", "type": "agentMessage", "text": "Live output"}))
        with patch.object(self.sessions, "_query_thread", return_value=stale):
            result = self.sessions._read_thread(self.identity)
        self.assertIn("Live output", [item.get("text") for item in result["turns"][-1]["items"]])

    def test_concurrent_projection_and_direct_publish_produce_strictly_monotonic_sequences(self):
        start_seq = self.sessions.sequence
        barrier = threading.Barrier(3)
        stop = threading.Event()
        observed_seqs: list[int] = []

        def sse_reader():
            cursor = start_seq
            barrier.wait()
            while not stop.is_set() or cursor < self.sessions.sequence:
                batch = self.sessions.events_after(cursor, timeout=0.05)
                for ev in batch["events"]:
                    observed_seqs.append(ev["seq"])
                cursor = batch["seq"]
                if stop.is_set() and not self.sessions._inflight_sequences and cursor >= self.sessions.sequence:
                    break

        def direct_publisher():
            barrier.wait()
            for idx in range(60):
                self.sessions._publish("browser/changed", {"threadId": self.identity, "step": idx})

        reader_thread = threading.Thread(target=sse_reader, daemon=True)
        pub_thread = threading.Thread(target=direct_publisher, daemon=True)
        reader_thread.start()
        pub_thread.start()
        barrier.wait()
        self.projection.project(self.event(1, "item/started", item={"id": "msg-1", "type": "agentMessage", "text": ""}))
        for seq_num in range(2, 62):
            self.projection.project(
                self.event(seq_num, "item/agentMessage/delta", itemId="msg-1", delta=f"t{seq_num}")
            )
        pub_thread.join(timeout=5)
        stop.set()
        with self.sessions.events_changed:
            self.sessions.events_changed.notify_all()
        reader_thread.join(timeout=5)

        expected = list(range(start_seq + 1, self.sessions.sequence + 1))
        self.assertEqual(observed_seqs, expected)
        self.assertEqual(len(observed_seqs), len(set(observed_seqs)))

    def test_maintain_receipts_bounds_transport_and_local_commands_while_preserving_pending(self):
        from agent_coord.provider_transport import Owner

        transport_dir = tempfile.TemporaryDirectory()
        self.addCleanup(transport_dir.cleanup)
        owner = Owner(transport_dir.name)
        claim = owner.dispatch({"action": "claim", "expected_epoch": 0, "coordinator": "coord-1"})

        def cleanup_owner():
            for process in owner.processes.values():
                process.kill()
                process.wait(timeout=5)
                for stream in (process.stdin, process.stdout, process.stderr):
                    stream.close()
            owner.journal.close()

        self.addCleanup(cleanup_owner)

        class DirectOwnerClient:
            record = {"instance": owner.instance, "pid": os.getpid()}

            def call(self, action, **values):
                payload = {"action": action, **values}
                if action != "status":
                    payload.update(claim)
                return owner.dispatch(payload)

            def command(self, channel, operation, value, **_):
                return self.call(
                    "command",
                    command_id=f"{operation}:{channel}",
                    channel=channel,
                    operation=operation,
                    value=value,
                )["result"]

        import sys
        projection = CoordinatorProjection(self.sessions, DirectOwnerClient())
        projection.open(
            "codex",
            [sys.executable, "-u", "-c", "import sys\nfor line in sys.stdin: pass"],
            dict(os.environ),
            self.temporary.name,
            lambda _: None,
            provider="codex",
        )
        projection.write("codex", {"method": "initialized"})

        # Seed a pending local command whose transport write already completed (simulating lost ack).
        pending_id = "pending-awaiting-reconcile"
        with self.store._connection() as db:
            db.execute(
                "INSERT INTO coordinator_commands VALUES (?,'codex','write',?,'pending',NULL)",
                (pending_id, json.dumps({"data": "{\"id\":0}\n"})),
            )
        owner.dispatch({
            "action": "command",
            "command_id": pending_id,
            "channel": "codex",
            "operation": "write",
            "value": {"data": "{\"id\":0}\n"},
            **claim,
        })

        with patch("agent_coord.provider_transport.MAX_RECEIPTS", 15):
            for idx in range(30):
                projection.write("codex", {"id": idx + 1, "method": "ping", "params": {}})
                if idx % 4 == 3:
                    projection.maintain_receipts(force=True, retain_count=4, max_batches=4)

        projection.maintain_receipts(force=True, retain_count=4, max_batches=8)

        remote_ids = {
            row[0] for row in owner.journal.db.execute("SELECT command_id FROM commands").fetchall()
        }
        self.assertLessEqual(len(remote_ids), 10)
        self.assertIn("open:codex", remote_ids)
        self.assertIn("initialized:codex", remote_ids)
        self.assertIn(pending_id, remote_ids)

        with self.store.reading(), self.store._connection() as db:
            local_ids = {row[0] for row in db.execute("SELECT command_id FROM coordinator_commands").fetchall()}
        self.assertLessEqual(len(local_ids), 10)
        self.assertIn("open:codex", local_ids)
        self.assertIn("initialized:codex", local_ids)
        self.assertIn(pending_id, local_ids)

    def test_maintain_receipts_degrades_gracefully_on_legacy_transport_and_prunes_local_commands(self):
        legacy_client = ProviderOwner(self.projection, {"codex": "running"})
        projection = CoordinatorProjection(self.sessions, legacy_client)
        with self.store._connection() as db:
            db.execute("INSERT INTO coordinator_channels VALUES ('codex','codex',NULL,'main',123,0)")
            db.execute("INSERT INTO coordinator_commands VALUES ('open:codex','codex','open','{}','written','{}')")
            db.execute("INSERT INTO coordinator_commands VALUES ('initialized:codex','codex','write','{}','written','{}')")
            db.execute("INSERT INTO coordinator_commands VALUES ('uncertain-1','codex','write','{}','uncertain','{}')")
            for idx in range(25):
                db.execute(
                    "INSERT INTO coordinator_commands VALUES (?,'codex','write','{}','written','{}')",
                    (f"w-{idx}",),
                )
        result = projection.maintain_receipts(force=True, retain_count=5)
        self.assertFalse(projection._transport_supports_receipt_retirement)
        self.assertEqual(result["remote_retired"], 0)
        self.assertGreater(result["local_retired"], 0)
        with self.store.reading(), self.store._connection() as db:
            remaining = {row[0] for row in db.execute("SELECT command_id FROM coordinator_commands").fetchall()}
        self.assertTrue({"open:codex", "initialized:codex", "uncertain-1"} <= remaining)
        self.assertLessEqual(len(remaining), 9)

        # Capacity rejection schedules asynchronous maintenance without making extra synchronous RPC calls.
        with patch.object(self.client, "call", side_effect=ProviderCommandRejected("Transport receipt capacity reached")):
            with self.assertRaises(ProviderCommandRejected):
                self.projection.write("codex", {"id": 99, "method": "turn/start", "params": {}})
        self.assertTrue(self.projection._receipt_maintenance_requested)


if __name__ == "__main__":
    unittest.main()
