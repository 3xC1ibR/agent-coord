from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))

from agent_coord.browser_persistence import BrowserPersistence
from agent_coord.execution_state import RuntimeExecution, read_many, RUNTIME_STALE_SECONDS
from agent_coord.store import CoordinationStore


class ExecutionEvidenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.now = 1000
        self.store = CoordinationStore(Path(temporary.name) / "state.sqlite3", clock=lambda: self.now)
        self.store.register(session_id="s", client="codex", cwd=temporary.name)
        self.store.threads.start_turn("s", turn_id="t")
        self.sessions = SimpleNamespace(store=self.store, lock=threading.RLock(), active={"s": "t"},
                                        requests={}, persistence=BrowserPersistence())
        self.runtime = RuntimeExecution(self.sessions)
        self.addCleanup(self.sessions.persistence.close)
        self.addCleanup(self.runtime.close)

    def read(self):
        self.assertTrue(self.sessions.persistence.flush(timeout=3, key="s"))
        with self.store._connection() as db:
            return read_many(self.store, db, ["s"])["s"]

    def approval(self, key="request"):
        self.sessions.requests[key] = {"method": "item/commandExecution/requestApproval",
            "params": {"threadId": "s", "command": "secret token", "env": {"TOKEN": "private"}},
            "received_at": self.now}
        self.runtime.capture("s")

    def test_request_is_waiting_without_exposing_tool_parameters(self):
        self.runtime.capture("s")
        self.assertEqual(self.read()["state"], "running")
        self.approval()
        state = self.read()
        self.assertEqual((state["state"], state["freshness"], state["source"]),
                         ("waiting_approval", "live", "provider_request"))
        self.assertEqual(state["pending_requests"][0]["request_key"], "request")
        self.assertNotIn("secret", json.dumps(state))
        self.assertNotIn("private", json.dumps(state))
        self.sessions.requests.clear()
        self.runtime.capture("s")
        self.assertEqual(self.read()["state"], "running")

    def test_long_wait_remains_live_when_runtime_heartbeats(self):
        self.approval()
        self.now += 3600
        self.runtime.heartbeat()
        self.assertEqual(self.read()["state"], "waiting_approval")
        self.assertEqual(self.read()["observed_at"], "1970-01-01T00:16:40+00:00")

    def test_stale_runtime_hides_pending_requests_without_claiming_idle(self):
        self.approval()
        self.now += RUNTIME_STALE_SECONDS + 1
        state = self.read()
        self.assertEqual((state["state"], state["freshness"], state["pending_requests"]),
                         ("unknown", "stale", []))

    def test_closed_or_dead_runtime_cannot_supply_a_live_wait(self):
        self.approval()
        with patch("agent_coord.execution_state._alive", return_value=False):
            self.assertEqual(self.read()["state"], "unknown")
        self.runtime.close()
        self.assertEqual(self.read()["freshness"], "stale")
        # A replacement process does not inherit the lost in-memory requests.
        replacement = RuntimeExecution(self.sessions)
        self.addCleanup(replacement.close)
        self.assertEqual(self.read()["state"], "unknown")

    def test_explicit_stop_and_failure_override_old_requests(self):
        self.approval()
        self.now += 1
        self.store.threads.request_stop("s", "t")
        self.store.threads.finish_turn("s", turn_id="t", status="interrupted")
        self.assertEqual(self.read()["state"], "stopped")
        self.assertEqual(self.read()["source"], "user_stop")
        self.store.threads.start_turn("s", turn_id="new")
        self.assertEqual(self.read()["state"], "unknown")
        self.store.threads.finish_turn("s", turn_id="new", status="failed")
        self.assertEqual(self.read()["state"], "failed")

    def test_provider_interruption_does_not_claim_user_stop(self):
        self.store.threads.finish_turn("s", turn_id="t", status="interrupted")
        self.assertEqual(self.read()["state"], "interrupted")

    def test_stop_intent_that_loses_to_success_does_not_relabel_the_result(self):
        self.store.threads.request_stop("s", "t")
        self.store.threads.finish_turn("s", turn_id="t", status="completed")
        self.assertEqual(self.read()["state"], "idle")
        self.assertEqual(self.read()["source"], "turn_completion")

    def test_unknown_native_request_type_does_not_become_a_question(self):
        self.sessions.requests["future"] = {"method": "future/method", "params": {"threadId": "s"}}
        self.runtime.capture("s")
        self.assertEqual(self.read()["state"], "unknown")
        self.assertEqual(self.read()["pending_requests"][0]["kind"], "unsupported")

    def test_old_snapshot_cannot_cross_turn_even_at_same_timestamp(self):
        self.approval()
        self.store.threads.start_turn("s", turn_id="new")
        self.assertEqual(self.read()["state"], "unknown")
        self.assertEqual(self.read()["pending_requests"], [])

    def test_multiple_requests_keep_approval_precedence_until_last_resolution(self):
        self.approval()
        self.sessions.requests["question"] = {"method": "item/tool/requestUserInput",
            "params": {"threadId": "s", "questions": [{"question": "private question"}]},
            "received_at": self.now}
        self.runtime.capture("s")
        self.assertEqual(self.read()["state"], "waiting_approval")
        del self.sessions.requests["request"]
        self.runtime.capture("s")
        self.assertEqual(self.read()["state"], "waiting_answer")
        self.assertNotIn("private question", json.dumps(self.read()))

    def test_active_heartbeat_alone_does_not_establish_progress(self):
        self.store.touch("s", turn_active=True)
        self.assertEqual(self.read()["state"], "unknown")

    def test_durable_ask_is_distinct_from_native_approval(self):
        ask = self.store.asks.create("s", "Private question", blocking=True)
        self.assertEqual(self.read()["state"], "waiting_answer")
        self.assertEqual(self.read()["source"], "blocking_ask")
        self.assertNotIn("Private question", json.dumps(self.read()))
        self.store.asks.withdraw(ask["id"], "s")
        self.assertEqual(self.read()["state"], "unknown")


class BrowserExecutionTests(unittest.TestCase):
    def setUp(self):
        from test_codex_app_server import FakeCodex
        from agent_coord.codex_app_server import BrowserSessions
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = CoordinationStore(Path(temporary.name) / "state.sqlite3")
        self.sessions = BrowserSessions(self.store, temporary.name, rpc_factory=FakeCodex)
        self.addCleanup(self.sessions.close)
        self.thread = self.sessions.create({})["session"]["thread_id"]

    def state(self):
        self.assertTrue(self.sessions.persistence.flush(timeout=3, key=self.thread))
        return self.store.get_session(self.thread)["execution"]

    def start(self):
        self.sessions.send(self.thread, {"message": "Run the fixture"})

    def request(self, method="item/commandExecution/requestApproval", native_id=7, **params):
        self.sessions._event({"id": native_id, "method": method,
            "params": {"threadId": self.thread, "turnId": self.sessions.active[self.thread], **params}})
        return self.state()["pending_requests"][-1]["request_key"]

    def test_all_normal_metadata_read_paths_expose_same_live_wait_without_provider_calls(self):
        self.assertEqual(self.state()["state"], "idle")
        self.start()
        self.request(command="sensitive command")
        with patch.object(self.sessions, "read", side_effect=AssertionError("provider read")), \
             patch.object(self.sessions, "_rpc", side_effect=AssertionError("provider resume")):
            states = [self.state(), self.store.list_sessions()[0]["execution"],
                self.store.threads.get(self.thread)["execution"],
                self.store.threads.list()[0]["execution"]]
        self.assertTrue(all(state == states[0] for state in states))
        self.assertEqual(states[0]["state"], "waiting_approval")
        self.assertTrue(self.store.get_session(self.thread)["turn_active"])
        self.assertNotIn("sensitive", json.dumps(states))

    def test_accept_decline_and_cancel_all_resolve_the_pending_wait(self):
        self.start()
        for decision in ("accept", "decline", "cancel"):
            with self.subTest(decision=decision):
                key = self.request()
                self.assertEqual(self.state()["state"], "waiting_approval")
                self.sessions.answer(self.thread, key, {"decision": decision})
                self.assertEqual(self.state()["state"], "running")
                self.assertEqual(self.state()["pending_requests"], [])

    def test_provider_resolution_removes_the_wait(self):
        self.start()
        self.request()
        self.sessions._event({"method": "serverRequest/resolved", "params": {"requestId": 7, "threadId": self.thread}})
        self.assertEqual(self.state()["state"], "running")

    def test_contended_snapshot_defers_without_losing_or_replaying_the_request(self):
        self.start()
        self.assertTrue(self.sessions.persistence.flush(timeout=3, key=self.thread))
        writer = sqlite3.connect(self.store.database_path)
        try:
            writer.execute("BEGIN IMMEDIATE")
            with self.assertLogs("agent_coord.browser_persistence", "WARNING"):
                self.sessions._event({"id": 7, "method": "item/fileChange/requestApproval", "params": {
                    "threadId": self.thread, "turnId": self.sessions.active[self.thread]}})
                self.assertFalse(self.sessions.persistence.flush(timeout=.2, key=self.thread))
            self.assertEqual(len(self.sessions.pending_requests(self.thread)), 1)
        finally:
            writer.rollback()
            writer.close()
        self.assertTrue(self.sessions.persistence.flush(timeout=3, key=self.thread))
        self.assertEqual(self.state()["state"], "waiting_approval")
        self.assertEqual(len(self.state()["pending_requests"]), 1)
        self.assertEqual(self.sessions.rpc.writes, [])

    def test_cold_resume_does_not_invent_request_map_or_computing_state(self):
        self.start()
        self.request()
        self.sessions.active.clear()
        self.sessions.requests.clear()
        self.sessions.loaded.clear()
        self.sessions.read(self.thread)
        self.assertEqual(self.state()["state"], "unknown")
        self.assertEqual(self.state()["pending_requests"], [])

    def test_native_question_answer_and_stop_are_distinct(self):
        self.start()
        key = self.request("item/tool/requestUserInput", questions=[{"id": "q", "question": "private"}])
        self.assertEqual(self.state()["state"], "waiting_answer")
        self.sessions.answer(self.thread, key, {"answers": {"q": {"answers": ["response"]}}})
        self.assertEqual(self.state()["state"], "running")
        self.request()
        self.sessions.interrupt(self.thread)
        self.assertEqual(self.state()["state"], "stopped")
        self.assertEqual(self.state()["pending_requests"], [])

    def test_failed_and_fast_completed_turns_keep_their_outcome_after_settings_capture(self):
        self.sessions.rpc.fast_turn = True
        self.start()
        self.assertEqual(self.state()["state"], "idle")
        self.sessions.rpc.fast_turn = False
        self.start()
        self.request()
        turn = {"id": self.sessions.active[self.thread], "status": "failed"}
        self.sessions._event({"method": "turn/completed", "params": {"threadId": self.thread, "turn": turn}})
        self.sessions._capture_settings(self.thread, {})
        self.assertEqual(self.state()["state"], "failed")

    def test_permission_settings_distinguish_selected_queued_and_native(self):
        from agent_coord.app_control import AppControl
        control = AppControl(self.store)
        self.start()
        self.request()
        control.request("settings", self.thread, {"yolo": True}, thread_id=self.thread, request_id="pending-yolo")
        settings = control.settings(self.thread)["permissions"]
        self.assertFalse(settings["saved_yolo"])
        self.assertEqual(settings["selected_mode"], "workspace-write")
        self.assertEqual(settings["pending_changes"][0]["request_id"], "pending-yolo")
        self.assertTrue(settings["pending_changes"][0]["yolo"])
        self.assertEqual(settings["last_native_observation"]["effective_mode"], "workspace-write")


class NativeAdapterWaitTests(unittest.TestCase):
    def test_native_permission_frames_round_trip_through_all_non_codex_adapters(self):
        from test_permission_modes import PermissionModeTests
        from test_kimi import KimiTests
        from test_antigravity import AntigravityTests
        from test_muse import MuseTests
        for client in ("claude", "kimi", "antigravity", "muse"):
            with self.subTest(client=client):
                temporary = tempfile.TemporaryDirectory()
                self.addCleanup(temporary.cleanup)
                self.root = Path(temporary.name).resolve()
                self.store = CoordinationStore(self.root / "state.sqlite3")
                self.sessions = PermissionModeTests.make_sessions(self)
                thread = self.sessions.create({"client": client})["session"]["thread_id"]
                self.sessions.send(thread, {"message": "Wait for a fixture approval"})
                if client == "claude":
                    self.sessions.claude.connections[thread].callback({"type": "control_request", "request_id": "approval",
                        "request": {"subtype": "can_use_tool", "tool_name": "Bash", "input": {"command": "pwd"}}})
                elif client == "kimi":
                    KimiTests.permission(self, thread)
                elif client == "antigravity":
                    AntigravityTests.permission(self, thread)
                else:
                    # Muse's helper routes native approval events through event().
                    self.event = lambda target, method, **params: MuseTests.event(self, target, method, **params)
                    MuseTests.permission(self, thread)
                self.assertTrue(self.sessions.persistence.flush(timeout=3, key=thread))
                state = self.store.threads.get(thread)["execution"]
                self.assertEqual((state["state"], state["freshness"]), ("waiting_approval", "live"))
                request = self.sessions.pending_requests(thread)[0]
                self.sessions.answer(thread, request["key"], {"decision": "decline"})
                self.assertTrue(self.sessions.persistence.flush(timeout=3, key=thread))
                self.assertEqual(self.store.get_session(thread)["execution"]["state"], "running")
                self.sessions.interrupt(thread)
                self.assertTrue(self.sessions.persistence.flush(timeout=3, key=thread))
                self.assertEqual(self.store.get_session(thread)["execution"]["state"], "stopped")
                self.sessions.close()


if __name__ == "__main__":
    unittest.main()
