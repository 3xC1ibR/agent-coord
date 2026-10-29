"""Owner-routed commands against a fake native provider, without model turns."""
import copy
import tempfile
from pathlib import Path
import unittest
from test_codex_app_server import BrowserSessions, CoordinationStore, FakeCodex
from agent_coord import supervision
from agent_coord.app_control import AppControl, AppControlWorker
from agent_coord.session_control import SessionControl
from agent_coord.store import CoordinationError


class InteractiveControlTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.store = CoordinationStore(self.root / "state.sqlite3")
        self.store.register(session_id="ramble", client="codex", cwd=str(self.root))
        self.sessions = BrowserSessions(
            self.store, str(self.root), rpc_factory=FakeCodex
        )
        self.addCleanup(self.sessions.close)
        self.worker = AppControlWorker(self.sessions)
        self.addCleanup(self.worker.close)
        self.control = self.worker.control
        self.interactive = SessionControl(self.store)
        create = self.control.request(
            "create",
            "ramble",
            {
                "cwd": str(self.root),
                "client": "codex",
                "name": "Work",
                "prompt": "Investigate",
                "subscribe": False,
            },
        )
        self.worker.process_once()
        self.tid = self.control.status(create["request_id"])["thread_id"]
        self.command("watch", "watch")

    def command(self, action, key, expected=None, message=None):
        payload = {"action": action}
        if action != "watch":
            payload["expected_turn_id"] = expected
        if action == "submit":
            payload["message"] = message or "Continue"
        self.control.request(
            "session", "ramble", payload, thread_id=self.tid, request_id=key
        )
        self.worker.process_once()
        return self.control.status(key)

    def test_start_then_steer_exact_turn_and_replay_same_command(self):
        first = self.command("submit", "start")
        self.assertEqual(first["status"], "completed", first)
        turn = self.sessions.active[self.tid]
        second = self.command("submit", "steer", turn, "Use yesterday")
        self.assertEqual(second["status"], "completed", second)
        calls = len([m for m, p in self.sessions.rpc.calls if m == "turn/steer"])
        self.command("submit", "steer", turn, "Use yesterday")
        self.assertEqual(
            len([m for m, p in self.sessions.rpc.calls if m == "turn/steer"]), calls
        )
        self.assertEqual(len(self.sessions.rpc.threads[self.tid]["turns"]), 1)

    def test_stale_command_never_targets_newer_turn(self):
        self.command("submit", "start")
        turn = self.sessions.active[self.tid]
        result = self.command("submit", "stale", "older")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.sessions.active[self.tid], turn)
        self.assertFalse(any(m == "turn/steer" for m, p in self.sessions.rpc.calls))
        self.assertEqual(
            self.command("interrupt", "bad-stop", "older")["status"], "failed"
        )

    def test_stop_idle_pauses_initial_wake_and_active_stop_targets_exact_turn(self):
        self.assertEqual(self.command("interrupt", "idle-stop")["status"], "completed")
        self.command("submit", "resume")
        turn = self.sessions.active[self.tid]
        self.assertEqual(self.command("interrupt", "stop", turn)["status"], "completed")
        self.assertNotIn(self.tid, self.sessions.active)
        call = [p for m, p in self.sessions.rpc.calls if m == "turn/interrupt"][-1]
        self.assertEqual(call["turnId"], turn)

    def test_only_completed_public_output_is_journaled_and_replayed(self):
        self.command("submit", "start")
        turn = self.sessions.active[self.tid]
        base = {"threadId": self.tid, "turnId": turn}
        for method, item in [
            ("item/started", {"id": "a", "type": "agentMessage", "text": "partial"}),
            (
                "item/completed",
                {"id": "secret", "type": "reasoning", "text": "private"},
            ),
            (
                "item/completed",
                {
                    "id": "tool",
                    "type": "commandExecution",
                    "aggregatedOutput": "secret log",
                },
            ),
            (
                "item/completed",
                {
                    "id": "a",
                    "type": "agentMessage",
                    "phase": "commentary",
                    "text": "[say]A finding[/say]",
                },
            ),
        ]:
            self.sessions._event({"method": method, "params": {**base, "item": item}})
        self.assertTrue(self.sessions.persistence.flush())
        result = self.interactive.snapshot("ramble", self.tid)
        messages = [e for e in result["events"] if e["kind"] == "message"]
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["text"], "[say]A finding[/say]")
        after = result["cursor"]
        self.assertEqual(
            SessionControl(self.store).snapshot("ramble", self.tid, after)["events"], []
        )
        self.assertEqual(len(self.sessions.rpc.threads[self.tid]["turns"]), 1)

    def test_unrelated_caller_cannot_control_or_observe(self):
        self.store.register(session_id="other", client="codex", cwd=str(self.root))
        with self.assertRaises(CoordinationError):
            self.interactive.snapshot("other", self.tid)
        with self.assertRaises(CoordinationError):
            self.control.request(
                "session", "other", {"action": "watch"}, thread_id=self.tid
            )

    def test_recent_failed_refresh_is_disconnected(self):
        with self.store._connection() as conn:
            conn.execute(
                "UPDATE interactive_bindings SET snapshot_json=?,refreshed_at=? WHERE thread_id=?",
                ('{"status":"disconnected"}', self.store.clock(), self.tid),
            )
        self.assertFalse(self.interactive.snapshot("ramble", self.tid)["connected"])

    def test_failed_provider_submission_is_uncertain_and_never_retried(self):
        self.sessions.rpc.fail_turn = True
        result = self.command("submit", "failed")
        self.assertEqual(result["status"], "uncertain")
        count = len([m for m, p in self.sessions.rpc.calls if m == "turn/start"])
        self.command("submit", "failed")
        self.assertEqual(
            len([m for m, p in self.sessions.rpc.calls if m == "turn/start"]), count
        )

    def test_session_queue_is_invisible_to_old_runtime(self):
        request = self.control.request(
            "session",
            "ramble",
            {"action": "submit", "expected_turn_id": None, "message": "hello"},
            thread_id=self.tid,
            request_id="pending",
        )
        self.assertEqual(request["status"], "queued")
        with self.store._connection() as conn:
            self.assertFalse(
                conn.execute(
                    "SELECT 1 FROM app_control_requests WHERE request_id='pending' AND status='queued'"
                ).fetchone()
            )
        self.assertEqual(self.control.cancel("pending")["status"], "cancelled")

    def test_explicit_adoption_is_required_and_cannot_steal_bound_owner(self):
        self.store.register(session_id="other", client="codex", cwd=str(self.root))
        payload = {"action": "watch", "adopt": True}
        self.control.request(
            "session", "other", payload, thread_id=self.tid, request_id="steal"
        )
        self.worker.process_once()
        self.assertEqual(self.control.status("steal")["status"], "failed")
        created = self.sessions.create(
            {"cwd": str(self.root), "client": "codex", "name": "Existing"}
        )
        target = created["session"]["thread_id"]
        self.control.request(
            "session", "ramble", payload, thread_id=target, request_id="adopt"
        )
        self.worker.process_once()
        self.assertEqual(self.control.status("adopt")["status"], "completed")
        self.assertEqual(
            self.interactive.snapshot("ramble", target)["thread_id"], target
        )

    def test_supervision_transfer_moves_interactive_control_to_successor(self):
        self.store.register(session_id="successor", client="codex", cwd=str(self.root))
        with self.store._connection() as db:
            db.execute("UPDATE messages SET delivered_at = 1 WHERE recipient_session_id = ?", (self.tid,))
        self.store.touch(self.tid, turn_active=False)
        with self.assertRaisesRegex(CoordinationError, "Another interactive controller"):
            self.interactive.authorize("successor", self.tid)
        supervision.transfer(self.store, requester="ramble", from_session="ramble", to_session="successor",
                             thread_ids=[self.tid], request_id="adopt-work", reason="Replace lead",
                             workspace=str(self.root))
        with self.assertRaisesRegex(CoordinationError, "current supervisor"):
            self.interactive.authorize("ramble", self.tid)
        self.interactive.bind("successor", self.tid)
        self.assertEqual(self.interactive.snapshot("successor", self.tid)["thread_id"], self.tid)
        with self.assertRaisesRegex(CoordinationError, "Another interactive controller"):
            self.interactive.authorize("ramble", self.tid)

    def test_interactive_creation_binds_before_initial_wake(self):
        receipt = self.control.request(
            "create",
            "ramble",
            {
                "cwd": str(self.root),
                "client": "codex",
                "name": "Interactive",
                "prompt": "Start",
                "subscribe": False,
                "interactive": True,
            },
        )
        self.worker.process_once()
        tid = self.control.status(receipt["request_id"])["thread_id"]
        snapshot = self.interactive.snapshot("ramble", tid)
        self.assertEqual(snapshot["thread_id"], tid)
        self.interactive.observe(
            "item/completed",
            {
                "threadId": tid,
                "turnId": "first",
                "item": {
                    "id": "first",
                    "type": "agentMessage",
                    "text": "[say]Starting[/say]",
                },
            },
        )
        self.assertEqual(len(self.interactive.snapshot("ramble", tid)["events"]), 1)
