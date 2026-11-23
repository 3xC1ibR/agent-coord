"""Atomic event application and command identity across coordinator recovery."""
import json
import copy
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
import zlib
from concurrent.futures import Future
from unittest.mock import patch

from test_codex_app_server import FakeCodex
from agent_coord.codex_app_server import BrowserSessions
from agent_coord.coordinator_projection import CoordinatorProjection, pack, unpack
from agent_coord.provider_transport import ProviderLaunchRejected
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

    def test_codec_preserves_integer_indices_and_sets(self):
        value = {"blocks": {"thread": {0: {"id": "item"}}}, "confirmed": {"item"}, "empty": set()}
        self.assertEqual(unpack(json.loads(json.dumps(pack(value)))), value)
        with self.assertRaises(TypeError):
            pack(object())

    def test_large_active_turn_payloads_survive_restart_rollback_and_legacy_handover(self):
        from agent_coord.coordinator_projection import MIN_PAYLOAD_CHARACTERS
        large = "中" * MIN_PAYLOAD_CHARACTERS + "\udc00"
        turn = {"id": "active", "items": [{"id": "a", "text": large}, {"id": "b", "text": large}]}
        self.sessions.claude.turns[self.identity] = turn
        self.projection.checkpoint()
        with self.store.reading(), self.store._connection() as db:
            original = db.execute("SELECT digest, compressed FROM coordinator_projection_payloads").fetchall()
            size = db.execute("SELECT length(state) FROM coordinator_projection").fetchone()[0]
        self.assertEqual(len(original), 1)
        self.assertLess(size, len(large))
        with patch("agent_coord.coordinator_projection.hashlib.sha256", side_effect=AssertionError("rehashed immutable text")):
            self.projection.checkpoint()
        original_save = self.projection._save
        def fail_after_save(db, encoded, cursor):
            original_save(db, encoded, cursor)
            raise sqlite3.IntegrityError("rollback after replacing the payload")
        self.sessions.claude.turns[self.identity]["items"] = [{"id": "b", "text": large + "changed"}]
        with patch.object(self.projection, "_save", side_effect=fail_after_save), self.assertRaises(sqlite3.IntegrityError):
            self.projection.checkpoint()
        self.projection = CoordinatorProjection(self.sessions, self.client)
        self.projection.load()
        self.assertEqual(self.sessions.claude.turns[self.identity]["items"], [
            {"id": "a", "text": large}, {"id": "b", "text": large}])
        self.sessions.claude.turns[self.identity]["items"].pop()
        self.sessions.claude.turns[self.identity]["items"][0]["text"] = large + "new"
        self.projection.checkpoint()
        with self.store.reading(), self.store._connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM coordinator_projection_payloads").fetchone()[0], 1)
            self.assertIsNone(db.execute("SELECT 1 FROM coordinator_projection_payloads WHERE digest=?", (original[0][0],)).fetchone())
        self.projection.freeze()
        with self.store.reading(), self.store._connection() as db:
            version, encoded = db.execute("SELECT version, state FROM coordinator_projection").fetchone()
            self.assertEqual(db.execute("SELECT count(*) FROM coordinator_projection_payloads").fetchone()[0], 0)
        self.assertEqual(version, 1)
        self.assertEqual(unpack(json.loads(zlib.decompress(encoded)))["claude"]["turns"], self.sessions.claude.turns)

    def test_codec_recovers_existing_tuple_keys_and_set_members(self):
        value = {"requests": {("thread", ("turn", 1)): ["pending"]},
                 "confirmed": {("thread", "item")}}
        self.assertEqual(unpack(json.loads(json.dumps(pack(value)))), value)

    def test_projection_failure_rolls_back_effects_state_and_cursor(self):
        event = self.event(1, "item/started", item={"id": "output", "type": "agentMessage", "text": ""})
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
            self.assertEqual(db.execute("SELECT COUNT(*) FROM coordinator_events").fetchone()[0], 0)
            self.assertNotEqual(db.execute("SELECT name FROM browser_sessions WHERE thread_id=?", (self.identity,)).fetchone()[0], "uncommitted")
        self.projection.project(event)
        self.assertEqual(self.projection.cursor, 1)

    def test_only_committed_matching_completions_leave_recovery_state(self):
        from agent_coord.history_persistence import read_history
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

    def test_legacy_completion_reconciles_from_saved_manifests_after_readiness(self):
        from agent_coord.history_persistence import INLINE_BYTES
        completed = {"id": self.turn_id, "status": "interrupted",
                     "items": [{"id": "large", "text": "x" * INLINE_BYTES}]}
        self.sessions.histories[self.identity] = {"id": self.identity, "turns": [completed]}
        self.sessions._dirty_history(self.identity)
        self.projection.checkpoint()
        # This is the old layout: an already published turn was saved again.
        self.sessions.claude.completed.update({self.identity: completed, "unpublished": completed})
        self.projection.checkpoint()
        self.projection.load()
        self.assertEqual(self.sessions.histories, {})
        while self.projection.restored_completions:
            self.projection.reconcile_restored_completion()
        self.assertEqual(self.sessions.histories, {})
        self.assertEqual(self.sessions.claude.completed, {"unpublished": completed})
        self.projection.checkpoint()
        self.projection.load()
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

    def test_replaying_committed_delta_has_no_second_effect(self):
        self.projection.project(self.event(1, "item/started", item={"id": "output", "type": "agentMessage", "text": ""}))
        delta = self.event(2, "item/agentMessage/delta", itemId="output", delta="once")
        self.projection.project(delta)
        sequence = self.sessions.sequence
        self.projection.project(delta)
        self.assertEqual(self.sessions.sequence, sequence)
        turn = self.sessions.histories[self.identity]["turns"][-1]
        self.assertEqual(next(item["text"] for item in turn["items"] if item["id"] == "output"), "once")
        with self.store.reading(), self.store._connection() as db:
            self.assertEqual(db.execute("SELECT cursor FROM coordinator_projection").fetchone()[0], 2)

    def test_restore_loads_history_on_first_event_and_rolls_back_failed_delta(self):
        from agent_coord.history_persistence import read_history
        self.projection.attach("codex", 123, self.sessions.rpc._message, provider="codex")
        self.projection.project(self.event(1, "item/started", item={"id": "output", "type": "agentMessage", "text": "retained "}))
        saved = read_history(self.store, self.identity)
        with patch("agent_coord.history_persistence.read_history", side_effect=AssertionError("eager startup history read")):
            self.projection.load()
        self.assertEqual(self.sessions.histories, {})
        self.assertEqual(self.sessions.dirty_histories, set())
        self.assertIn(self.identity, self.sessions.loaded)
        self.assertEqual(self.sessions.active[self.identity], self.turn_id)
        self.assertEqual(self.projection.cursor, 1)
        delta = self.event(2, "item/agentMessage/delta", itemId="output", delta="once")
        original = self.projection._save
        def failed(db, state, cursor):
            original(db, state, cursor)
            raise sqlite3.IntegrityError("injected after restored history write")
        with patch.object(self.projection, "_save", side_effect=failed):
            with self.assertRaisesRegex(sqlite3.IntegrityError, "injected"):
                self.projection.project(delta)
        self.assertEqual(self.sessions.histories, {})
        self.assertEqual(self.projection.cursor, 1)
        self.assertEqual(read_history(self.store, self.identity), saved)
        self.projection.project(delta)
        self.projection.project(delta)
        restored = read_history(self.store, self.identity)
        self.assertEqual(restored["turns"][-1]["items"][:-1], saved["turns"][-1]["items"][:-1])
        self.assertEqual(restored["turns"][-1]["items"][-1]["text"], "retained once")
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

        def retained(action, **values):
            calls.append(action)
            return {"outcome": "written", "result": "{}"}

        self.client.call = retained
        self.projection.recover_commands()
        self.assertEqual(calls, ["outcome"])
        with self.store.reading(), self.store._connection() as db:
            self.assertEqual(db.execute("SELECT outcome FROM coordinator_commands").fetchone()[0], "written")

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
        with self.store.reading(), self.store._connection() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM coordinator_channels WHERE channel=?", (channel,)).fetchone())
            self.assertEqual(db.execute("SELECT outcome FROM coordinator_commands WHERE channel=?", (channel,)).fetchone()[0],
                             "rejected")

    def test_malformed_orphaned_launch_receipt_stays_uncertain_without_blocking_restore(self):
        self.projection.checkpoint()
        channel = "claude:damaged-receipt"
        damaged = '{"error": "[Errno 2] No such file or directory: '
        with self.store._connection() as db:
            db.execute("INSERT INTO coordinator_commands VALUES (?, ?, 'open', '{}', 'uncertain', ?)",
                       ("open:" + channel, channel, damaged))
        with patch.object(self.client, "call", return_value={
                "channels": [{"channel": channel, "pid": None, "state": "closed"}]}) as transport:
            projection = CoordinatorProjection(self.sessions, self.client)
            projection.load()
            transport.assert_called_once_with("status")
        self.assertIn("uncertain", projection.delivery_error)
        self.assertIn("codex", projection.channels)
        with self.store.reading(), self.store._connection() as db:
            self.assertEqual(tuple(db.execute("SELECT outcome,result_json FROM coordinator_commands WHERE channel=?",
                                              (channel,)).fetchone()), ("uncertain", damaged))

    def test_restart_recovers_legacy_enoent_open_only_with_closed_unstarted_channel(self):
        self.projection.checkpoint()
        channel = "claude:missing-worktree"
        error = "[Errno 2] No such file or directory: '/removed-worktree'"
        with self.store._connection() as db:
            db.execute("INSERT INTO coordinator_channels VALUES (?, 'claude', ?, 'main', NULL, 0)",
                       (channel, self.identity))
            db.execute("INSERT INTO coordinator_commands VALUES (?, ?, 'open', '{}', 'uncertain', ?)",
                       ("open:" + channel, channel, json.dumps({"error": error})))
        for pid, state in [(987, "closed"), (None, "opening"), (None, "closed")]:
            with self.subTest(pid=pid, state=state), patch.object(self.client, "call", return_value={
                    "channels": [{"channel": channel, "pid": pid, "state": state}]}) as transport:
                projection = CoordinatorProjection(self.sessions, self.client)
                if pid or state != "closed":
                    with self.assertRaisesRegex(RuntimeError, "uncertain"):
                        projection.load()
                else:
                    projection.load()
                    self.assertNotIn(channel, projection.channels)
                    self.assertIsNone(projection.delivery_error)
                self.assertTrue(all(call.args == ("status",) for call in transport.call_args_list))

    def test_restart_recovers_orphaned_launch_receipts_from_matching_retained_channels(self):
        self.projection.checkpoint()
        closed = ["claude:removed-first", "claude:removed-second"]
        unknown = "claude:absent"
        error = "[Errno 2] No such file or directory: '/removed-worktree'"
        with self.store._connection() as db:
            db.executemany("INSERT INTO coordinator_commands VALUES (?, ?, 'open', '{}', 'uncertain', ?)",
                           [("open:" + c, c, json.dumps({"error": error})) for c in [*closed, unknown]])
        with patch.object(self.client, "call", return_value={
                "channels": [{"channel": c, "pid": None, "state": "closed"} for c in closed]}) as transport:
            projection = CoordinatorProjection(self.sessions, self.client)
            projection.load()
            transport.assert_called_once_with("status")
        with self.store.reading(), self.store._connection() as db:
            outcomes = dict(db.execute("SELECT channel, outcome FROM coordinator_commands"))
        self.assertEqual(outcomes, {closed[0]: "rejected", closed[1]: "rejected", unknown: "uncertain"})
        self.assertIn("uncertain", projection.delivery_error)

    def test_uncertain_command_is_not_replayed(self):
        with self.store._connection() as db:
            db.execute("INSERT INTO coordinator_commands VALUES ('retained','codex','write',?, 'pending',NULL)",
                       (json.dumps({"data": "frame\n"}),))
        calls = []

        def uncertain(action, **values):
            calls.append(action)
            return {"outcome": "uncertain", "result": "{}"}

        self.client.call = uncertain
        self.projection.recover_commands()
        self.assertEqual(calls, ["outcome"])
        self.assertIn("uncertain", self.projection.delivery_error)

    def test_lost_acknowledgement_recovers_retained_receipt_without_resend(self):
        calls = []

        def retained(action, **values):
            calls.append(action)
            if action == "command":
                raise ConnectionError("acknowledgement connection closed")
            return {"outcome": "written", "result": {"bytes": 42}}

        self.client.call = retained
        with self.assertRaises(ConnectionError):
            self.projection.write("codex", {"method": "turn/steer", "params": {"text": "Exactly once"}})
        self.assertIn("acknowledgement", self.projection.delivery_error)
        self.projection.recover_commands()
        self.assertEqual(calls, ["command", "outcome"])
        self.assertIsNone(self.projection.delivery_error)
        with self.store.reading(), self.store._connection() as db:
            row = db.execute("SELECT outcome,value_json FROM coordinator_commands").fetchone()
        self.assertEqual(tuple(row), ("written", "{}"))

    def test_provider_delivery_does_not_hold_browser_lock(self):
        from agent_coord.codex_app_server import CodexRPC
        rpc = self.sessions.rpc = CodexRPC(self.store, self.sessions._event)
        rpc.channel = "codex"
        entered, release = threading.Event(), threading.Event()
        outcomes = []

        def blocked_delivery(action, **values):
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

    def test_restored_uncertain_receipt_remains_visible_without_replay(self):
        with self.store._connection() as db:
            db.execute("INSERT INTO coordinator_commands VALUES ('uncertain','codex','write','{}','uncertain',NULL)")
        projection = CoordinatorProjection(self.sessions, self.client)
        projection.recover_commands()
        self.assertIn("uncertain", projection.delivery_error)
        self.assertEqual(self.client.commands, [])

    def test_eof_projection_failure_restores_process_state(self):
        process = self.projection.channels["codex"]["process"]
        with patch.object(self.projection, "_save", side_effect=sqlite3.IntegrityError("rollback")):
            with self.assertRaises(sqlite3.IntegrityError):
                self.projection.project({"sequence": 1, "channel": "codex", "stream": "eof", "data": ""})
        self.assertIsNone(process.poll())
        self.assertEqual(self.projection.cursor, 0)

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

    def test_confirmed_stop_clears_transport_projection_but_retains_history(self):
        from agent_coord.desktop_runtime import clear_stopped_projection
        self.projection.project(self.event(1, "item/started", item={"id": "output", "type": "agentMessage", "text": "retained"}))
        clear_stopped_projection(self.store.database_path)
        with self.store.reading(), self.store._connection() as db:
            for table in ("coordinator_projection", "coordinator_commands", "coordinator_channels", "coordinator_events"):
                self.assertEqual(db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0], 0)
        from agent_coord.history_persistence import read_history
        history = read_history(self.store, self.identity)
        self.assertEqual(history["turns"][-1]["items"][-1]["text"], "retained")

    def test_stop_proof_requires_retained_transport_owner_exit(self):
        import os
        from agent_coord.desktop_runtime import confirm_stopped, lock_file
        directory = Path(self.temporary.name) / "runtime"
        transport = directory / "provider-transport"
        transport.mkdir(parents=True)
        record = transport / "owner.json"
        record.write_text("{}")
        guard = lock_file(transport / "owner.lock")
        try:
            self.assertFalse(confirm_stopped(directory, self.store.database_path, "test-instance"))
            record.unlink()
            self.assertFalse(confirm_stopped(directory, self.store.database_path, "test-instance"))
            self.assertFalse((directory / "stop-proof.json").exists())
        finally:
            os.close(guard)
        self.assertTrue(confirm_stopped(directory, self.store.database_path, "test-instance"))

    def test_lost_owner_maintenance_retains_commands_and_interrupts_work(self):
        from contextlib import closing
        from agent_coord.desktop_runtime import interrupt_stopped_projection
        # Native children can be in the adapter mirror without an app session.
        self.sessions.active["unregistered-native-child"] = "child-turn"
        self.sessions.usage_recovery_attempts.add((self.identity, self.turn_id))
        self.projection.project(self.event(1, "item/started", item={"id": "output", "type": "agentMessage", "text": "retained"}))
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
            self.assertEqual(db.execute("SELECT paused,pause_kind FROM browser_inbox_wake WHERE thread_id=?", (self.identity,)).fetchone()[:], (1, "stop"))

    def test_sse_publication_and_future_wait_for_commit(self):
        observed = []
        original = self.sessions._event

        def callback(message):
            original(message)
            self.projection.after_commit(lambda: observed.append("committed"))

        self.projection.channels["codex"]["callback"] = callback
        event = self.event(1, "item/started", item={"id": "output", "type": "agentMessage", "text": ""})
        sequence = self.sessions.sequence

        def reject(*args):
            self.assertEqual(self.sessions.sequence, sequence)
            self.assertFalse(any(item["method"] == "item/started" and item["params"].get("item", {}).get("id") == "output"
                                 for item in self.sessions.events))
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

        def read(*args, **kwargs):
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

    def test_shared_transaction_preserves_explicit_read_only_scope(self):
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            with self.store.sharing_transaction(db):
                with self.store.reading(), self.store._connection() as reader:
                    self.assertEqual(reader.execute("SELECT 1").fetchone()[0], 1)
                    with self.assertRaises(sqlite3.DatabaseError):
                        reader.execute("UPDATE browser_sessions SET name='forbidden'")
                with self.store._connection() as writer:
                    writer.execute("UPDATE browser_sessions SET name='permitted' WHERE thread_id=?", (self.identity,))


if __name__ == "__main__":
    unittest.main()
