import copy
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from test_codex_app_server import BrowserSessions, CoordinationStore, FakeCodex
from test_browser_providers import claude_factory
from agent_coord.app_control import AppControlWorker
from agent_coord.turn_watchdog import TurnWatchdog


class TurnWatchdogTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = CoordinationStore(self.root / "state.sqlite3")
        self.sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex,
                                        claude_factory=claude_factory)
        self.addCleanup(self.sessions.close)
        self.now = 0
        self.watchdog = self.sessions.watchdog
        self.watchdog.clock = lambda: self.now
        self.worker = AppControlWorker(self.sessions)
        self.addCleanup(self.worker.close)
        self.parent = self.sessions.create({"name": "Orchestrator"})["session"]["thread_id"]

    def child(self, client="codex"):
        request = self.worker.control.request("create", self.parent, {
            "cwd": str(self.root), "client": client, "prompt": "Implement the change.", "name": "Worker"})
        self.worker.process_once()
        child = self.worker.control.status(request["request_id"])["thread_id"]
        self.sessions.inbox_wake._dispatch(child)
        self.assertTrue(self.sessions.persistence.flush(key=child))
        return child

    def turn(self, child):
        return self.sessions.rpc.threads[child]["turns"][-1]

    def tick(self, now):
        self.assertTrue(self.sessions.persistence.flush())
        self.now = now
        self.watchdog.process_once()
        self.assertTrue(self.sessions.persistence.flush())

    def reports(self):
        return self.store.inbox(self.parent, mark_delivered=False)

    def nudges(self, child):
        return [p for method, p in self.sessions.rpc.calls if method == "turn/steer" and p["threadId"] == child]

    def progress(self, child, *, turn_id=None, item=None):
        self.sessions._event({"method": "item/started", "params": {
            "threadId": child, "turnId": turn_id or self.turn(child)["id"],
            "item": item or {"id": "reasoning", "type": "reasoning", "summary": ["Working"]}}})

    def test_repairs_missed_completion_and_durable_flag_without_replaying_work(self):
        for status in ("completed", "failed", "interrupted"):
            child = self.child()
            self.turn(child)["status"] = status  # Drop the provider completion notification.
            self.tick(self.now + 301)
            self.assertNotIn(child, self.sessions.active)
            self.assertFalse(self.store.get_session(child)["turn_active"])
            self.assertEqual(self.store.threads.get(child)["turn_completion"]["status"], status)
            self.assertEqual(len(self.sessions.rpc.threads[child]["turns"]), 1)
            self.assertEqual(self.nudges(child), [])

    def test_read_repairs_flag_even_when_live_flag_already_cleared(self):
        child = self.child()
        self.turn(child)["status"] = "interrupted"
        self.sessions.active.pop(child)
        self.sessions.read(child)
        self.assertTrue(self.sessions.persistence.flush(key=child))
        self.assertFalse(self.store.get_session(child)["turn_active"])
        self.assertEqual(self.store.threads.get(child)["turn_completion"]["status"], "interrupted")

    def test_late_hook_after_completed_turn_is_repaired_without_resetting_scope(self):
        child = self.child()
        self.store.begin_work(session_id=child, scopes=["owned.py"])
        self.sessions.interrupt(child)
        self.store.touch(child, turn_active=True)
        self.tick(15)
        self.assertFalse(self.store.get_session(child)["turn_active"])
        self.assertEqual(self.store.get_session(child)["write_scope"], ["owned.py"])
        self.assertEqual(self.reports(), [])  # Deliberate Stop is not a failure.

    def test_nudges_once_then_alerts_and_wakes_orchestrator(self):
        child = self.child()
        self.tick(299)
        self.assertEqual(self.nudges(child), [])
        self.tick(301)
        self.assertEqual(len(self.nudges(child)), 1)
        self.assertEqual(self.nudges(child)[0]["expectedTurnId"], self.turn(child)["id"])
        self.assertEqual(self.watchdog.snapshot(child)["state"], "recovering")
        self.tick(400)
        self.assertEqual(len(self.nudges(child)), 1)
        self.tick(601)
        self.tick(700)
        self.assertEqual(len(self.reports()), 1)
        body = json.loads(self.reports()[0]["body"])
        self.assertEqual(body["event"], "app_agent_stalled")
        self.assertEqual(body["turn_id"], self.turn(child)["id"])
        self.assertFalse(self.reports()[0]["reply_required"])
        self.assertEqual(self.sessions.work_thread(child)["status"], "stalled")
        self.assertTrue(self.store.get_session(child)["turn_active"])
        self.assertIsNone(self.store.threads.get(child)["turn_completion"])
        self.sessions.inbox_wake._dispatch(self.parent)
        self.assertIn(self.parent, self.sessions.active)

    def test_recovery_deadline_error_does_not_disable_active_turn_supervision(self):
        child = self.child()
        with patch("agent_coord.turn_watchdog.process_failure_followups", side_effect=RuntimeError("deadline scan failed")):
            with self.assertLogs("agent_coord.turn_watchdog", level="ERROR"):
                self.tick(301)
        self.assertEqual(len(self.nudges(child)), 1)

    def test_streaming_reasoning_counts_as_progress_without_new_tools(self):
        child = self.child()
        self.now = 290
        self.progress(child)
        self.tick(580)
        self.assertEqual(self.nudges(child), [])
        self.assertEqual(self.reports(), [])
        self.tick(591)
        self.assertEqual(len(self.nudges(child)), 1)
        self.now = 600
        self.progress(child)
        self.tick(1000)
        self.assertEqual(len(self.nudges(child)), 1)  # One attempt for this turn, ever.
        self.assertEqual(self.watchdog.snapshot(child)["state"], "healthy")

    def test_approval_and_answer_waits_are_not_stalls(self):
        child = self.child()
        self.sessions._event({"id": "approval", "method": "item/tool/requestUserInput",
            "params": {"threadId": child, "turnId": self.turn(child)["id"], "questions": []}})
        self.tick(10000)
        self.assertEqual(self.nudges(child), [])
        self.assertEqual(self.reports(), [])
        self.sessions._event({"method": "serverRequest/resolved", "params": {"threadId": child, "requestId": "approval"}})
        self.tick(10001)
        self.assertEqual(self.nudges(child), [])

    def test_silent_tool_is_not_cancelled_or_nudged_but_eventually_alerts(self):
        child = self.child()
        self.progress(child, item={"id": "tool", "type": "mcpToolCall", "status": "inProgress"})
        self.tick(601)
        self.assertEqual(self.nudges(child), [])
        self.assertEqual(self.reports(), [])
        self.tick(1201)
        self.assertIn("tool may still be running", self.reports()[0]["body"])
        self.assertNotIn("turn/interrupt", [m for m, _ in self.sessions.rpc.calls])
        self.sessions._event({"method": "item/completed", "params": {"threadId": child,
            "turnId": self.turn(child)["id"], "item": {"id": "tool", "type": "mcpToolCall", "status": "completed"}}})
        self.assertEqual(self.watchdog.snapshot(child)["state"], "healthy")

    def test_busy_orchestrator_receives_durable_alert_without_steering(self):
        child = self.child()
        self.sessions.send(self.parent, {"message": "Coordinate"})
        self.tick(601)
        self.sessions.inbox_wake._dispatch(self.parent)
        self.assertEqual(len(self.sessions.rpc.threads[self.parent]["turns"]), 1)
        self.assertEqual(len(self.reports()), 1)
        self.assertIsNone(self.reports()[0]["delivered_at"])

    def test_stop_and_close_block_recovery(self):
        for gate in ("stop", "close"):
            child = self.child()
            if gate == "stop":
                self.sessions.inbox_wake.pause(child, "User stopped", kind="stop")
            elif gate == "close":
                self.store.threads.update(child, attention="archived")
            self.tick(self.now + 601)
            self.assertEqual(self.nudges(child), [])
        self.assertEqual(self.reports(), [])

    def test_queued_user_input_blocks_nudge_but_not_stall_alert(self):
        child = self.child()
        self.sessions.queue.close()
        self.sessions.queue.enqueue(child, {"message": "User instruction"})
        self.tick(301)
        self.tick(601)
        self.assertEqual(self.nudges(child), [])
        self.assertEqual(len(self.reports()), 1)

    def test_late_browser_hooks_cannot_resurrect_or_finish_runtime_turns(self):
        from agent_coord.hook import handle
        child = self.child()
        payload = {"session_id": child, "cwd": str(self.root), "tool_name": "Read", "tool_input": {}}
        with patch.dict("os.environ", {"AGENT_COORD_CLIENT": "codex", "AGENT_COORD_SESSION_ID": child}):
            handle({**payload, "hook_event_name": "Stop"}, self.store)
            self.assertTrue(self.store.get_session(child)["turn_active"])
            self.sessions.interrupt(child)
            self.assertTrue(self.sessions.persistence.flush(key=child))
            for event in ("UserPromptSubmit", "PreToolUse", "PostToolUse"):
                handle({**payload, "hook_event_name": event, "prompt": "Old prompt"}, self.store)
                self.assertFalse(self.store.get_session(child)["turn_active"], event)

    def test_ordinary_user_conversation_is_alerted_in_ui_without_automatic_input(self):
        child = self.sessions.create({})["session"]["thread_id"]
        self.sessions.send(child, {"message": "Work"})
        self.tick(301)
        self.tick(601)
        self.assertEqual(self.nudges(child), [])
        self.assertEqual(self.sessions.work_thread(child)["status"], "stalled")
        self.assertTrue(self.sessions.work_thread(child)["needs_attention"])

    def test_recovery_timeout_never_retries_uncertain_submission(self):
        child = self.child()
        real = self.sessions.rpc.request
        def request(method, params=None):
            if method == "turn/steer":
                real(method, params)
                raise RuntimeError("Lost response after acceptance")
            return real(method, params)
        with patch.object(self.sessions.rpc, "request", side_effect=request):
            self.tick(301)
            self.tick(401)
            self.tick(601)
        self.assertEqual(len(self.nudges(child)), 1)
        self.assertEqual(len(self.reports()), 1)

    def test_recovery_claim_and_alert_survive_watchdog_recreation(self):
        child = self.child()
        self.tick(301)
        self.watchdog = self.sessions.watchdog = TurnWatchdog(self.sessions, clock=lambda: self.now)
        self.tick(302)  # New runtime gets a grace period, not another recovery budget.
        self.tick(603)
        self.tick(903)
        self.assertEqual(len(self.nudges(child)), 1)
        self.assertEqual(len(self.reports()), 1)
        self.watchdog = self.sessions.watchdog = TurnWatchdog(self.sessions, clock=lambda: self.now)
        self.tick(904)
        self.tick(1505)
        self.assertEqual(len(self.reports()), 1)

    def test_old_completion_and_old_probe_cannot_end_newer_turn(self):
        child = self.child()
        old = copy.deepcopy(self.turn(child))
        self.sessions.interrupt(child)
        self.sessions.send(child, {"message": "New turn"})
        self.assertTrue(self.sessions.persistence.flush(key=child))
        new_id = self.turn(child)["id"]
        old["status"] = "completed"
        self.sessions._event({"method": "turn/completed", "params": {"threadId": child, "turn": old}})
        self.assertEqual(self.sessions.active[child], new_id)
        self.assertTrue(self.store.get_session(child)["turn_active"])
        self.sessions._reconcile_turn(child, {"turns": [old], "status": {"type": "idle"}}, -1)
        self.assertEqual(self.sessions.active[child], new_id)

    def test_progress_during_probe_aborts_recovery(self):
        child = self.child()
        real = self.watchdog._request
        def request(tid, method, params):
            result = real(tid, method, params)
            self.progress(tid)
            return result
        with patch.object(self.watchdog, "_request", side_effect=request):
            self.tick(301)
        self.assertEqual(self.nudges(child), [])

    def test_probe_failure_in_one_child_does_not_hide_other_stalls(self):
        child, other = self.child(), self.child()
        real = self.watchdog._request
        def request(tid, method, params):
            if tid == child:
                raise RuntimeError("Provider unavailable")
            return real(tid, method, params)
        with patch.object(self.watchdog, "_request", side_effect=request):
            self.tick(601)
        self.assertEqual({json.loads(r["body"])["session_id"] for r in self.reports()}, {child, other})

    def test_unsupported_steering_only_probes_and_alerts(self):
        child = self.child()
        with patch.dict(self.sessions.loaded_clients, {child: "antigravity"}):
            self.tick(301)
            self.tick(601)
        self.assertEqual(self.nudges(child), [])
        self.assertEqual(len(self.reports()), 1)

    def test_old_turn_progress_and_metadata_do_not_reset_timer(self):
        child = self.child()
        self.now = 290
        self.progress(child, turn_id="obsolete-turn")
        self.sessions._event({"method": "thread/status/changed", "params": {"threadId": child, "status": {"type": "active"}}})
        self.tick(301)
        self.assertEqual(len(self.nudges(child)), 1)

    def test_disconnect_clears_watchdog_status_without_affecting_other_providers(self):
        child = self.child()
        claude = self.child("claude")
        self.tick(601)
        self.sessions._event({"method": "bridge/disconnected", "params": {}})
        self.assertIsNone(self.watchdog.snapshot(child))
        self.assertIsNotNone(self.watchdog.snapshot(claude))

    def test_confirmation_during_probe_cannot_be_mistaken_for_stall(self):
        child = self.child()
        real = self.watchdog._request
        def request(tid, method, params):
            result = real(tid, method, params)
            self.turn(tid)["status"] = "completed"
            self.sessions._event({"method": "turn/completed", "params": {"threadId": tid, "turn": copy.deepcopy(self.turn(tid))}})
            return result
        with patch.object(self.watchdog, "_request", side_effect=request):
            self.tick(601)
        self.assertEqual(self.reports(), [])
        self.assertFalse(self.store.get_session(child)["turn_active"])

    def test_database_failure_retries_alert_without_recovery_replay(self):
        child = self.child()
        self.tick(301)
        with patch("agent_coord.turn_watchdog.notify_agent_stall", side_effect=sqlite3.OperationalError("database is locked")):
            with self.assertLogs("agent_coord.turn_watchdog", level="ERROR"):
                self.tick(601)
        self.tick(701)
        self.assertEqual(len(self.reports()), 1)
        self.assertEqual(len(self.nudges(child)), 1)

    def test_claude_uses_same_turn_continuation_and_requires_actual_progress(self):
        child = self.child("claude")
        connection = self.sessions.claude.connections[child]
        before = len(connection.writes)
        self.tick(301)
        self.assertEqual(len(connection.writes), before + 1)
        self.tick(601)
        self.assertEqual(len(self.reports()), 1)

    def test_watchdog_does_not_wait_for_thread_owned_by_user_operation(self):
        child = self.child()
        acquired, release = threading.Event(), threading.Event()
        def hold():
            with self.sessions._thread_lock(child):
                acquired.set()
                release.wait(3)
        worker = threading.Thread(target=hold)
        worker.start()
        try:
            self.assertTrue(acquired.wait(2))
            self.tick(601)
            self.assertEqual(self.reports(), [])
        finally:
            release.set()
            worker.join(3)


if __name__ == "__main__":
    unittest.main()
