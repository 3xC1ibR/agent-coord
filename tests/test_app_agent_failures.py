import copy
import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from test_codex_app_server import BrowserSessions, CoordinationStore, FakeCodex
from test_browser_providers import claude_factory
from agent_coord.app_control import AppControl, AppControlWorker
from agent_coord.app_agent_failures import notify_agent_failure


class AppAgentFailureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = CoordinationStore(self.root / "state.sqlite3")
        self.sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex,
                                        claude_factory=claude_factory)
        self.addCleanup(self.sessions.close)
        self.worker = AppControlWorker(self.sessions)
        self.addCleanup(self.worker.close)
        self.control = self.worker.control
        self.parent = self.sessions.create({"name": "Orchestrator"})["session"]["thread_id"]

    def request(self, **values):
        return self.control.request("create", self.parent, {
            "cwd": str(self.root), "client": "codex", "prompt": "Implement the requested change.",
            "name": "Worker", **values})

    def create(self, **values):
        request = self.request(**values)
        self.worker.process_once()
        receipt = self.control.status(request["request_id"])
        self.assertEqual(receipt["status"], "completed")
        return receipt["thread_id"]

    def reports(self):
        return self.store.inbox(self.parent, mark_delivered=False)

    def finish(self, child, status="failed", error=None):
        turn = self.sessions.rpc.threads[child]["turns"][-1]
        turn.update(status=status, error={"message": error} if error else None)
        event = {"method": "turn/completed", "params": {"threadId": child, "turn": copy.deepcopy(turn)}}
        self.sessions._event(event)
        return event

    def test_limits_and_other_errors_notify_creator_without_reply_contract(self):
        for error in ("5 hour usage limit reached; resets at 6pm", "Insufficient credits", "Provider crashed"):
            child = self.create()
            self.sessions.inbox_wake._dispatch(child)
            self.store.inbox(child)  # Normal hook delivery.
            event = self.finish(child, error=error)
            self.sessions._event(event)
            report = self.reports()[-1]
            body = json.loads(report["body"])
            self.assertEqual(body["session_id"], child)
            self.assertEqual(body["error"], error)
            self.assertEqual(body["turn_id"], event["params"]["turn"]["id"])
            self.assertEqual(report["classification"], "action_required")
            self.assertFalse(report["reply_required"])
            with self.store._connection() as db:
                self.assertEqual(db.execute("SELECT paused FROM browser_inbox_wake WHERE thread_id = ?", (child,)).fetchone()[0], 1)
        self.assertEqual(len(self.reports()), 3)
        self.sessions.inbox_wake._dispatch(self.parent)
        self.assertIn(self.parent, self.sessions.active)

    def test_claude_limit_result_uses_same_notification_path(self):
        child = self.create(client="claude", mode="orchestration", reply_required=True)
        self.sessions.inbox_wake._dispatch(child)
        self.sessions.claude.connections[child].callback({
            "type": "result", "subtype": "error_during_execution", "is_error": True,
            "errors": ["You have hit your limit; resets at 7pm"]})
        self.assertEqual(json.loads(self.reports()[0]["body"])["error"], "You have hit your limit; resets at 7pm")

    def test_success_stop_and_unrelated_conversations_do_not_notify(self):
        for status in ("completed", "interrupted"):
            child = self.create()
            self.sessions.send(child, {"message": "Start"})
            self.finish(child, status=status)
        ordinary = self.sessions.create({})["session"]["thread_id"]
        self.sessions.send(ordinary, {"message": "Start"})
        self.finish(ordinary)
        self.assertEqual(self.reports(), [])

    def test_later_failure_reports_again_and_busy_parent_keeps_message(self):
        child = self.create()
        self.sessions.send(self.parent, {"message": "Continue orchestration"})
        for _ in range(2):
            self.sessions.send(child, {"message": "Try again"})
            self.finish(child, error="No credits")
        self.sessions.inbox_wake._dispatch(self.parent)
        self.assertEqual(len(self.sessions.rpc.threads[self.parent]["turns"]), 1)
        self.assertEqual(len(self.reports()), 2)
        self.assertTrue(all(report["delivered_at"] is None for report in self.reports()))

    def test_disconnect_reports_active_child_once_but_not_idle_children(self):
        child = self.create()
        self.create()
        self.sessions.send(child, {"message": "Start"})
        self.sessions._event({"method": "bridge/disconnected"})
        self.sessions._event({"method": "bridge/disconnected"})
        self.assertEqual(len(self.reports()), 1)
        self.assertEqual(json.loads(self.reports()[0]["body"])["stage"], "disconnect")
        self.assertEqual(self.store.threads.get(child)["turn_completion"]["status"], "failed")

    def test_normal_runtime_shutdown_does_not_report_disconnect_failure(self):
        child = self.create()
        self.sessions.send(child, {"message": "Start"})
        self.sessions.close()
        self.assertEqual(self.reports(), [])

    def test_wake_start_and_resume_failures_report_and_do_not_retry(self):
        child = self.create()
        self.sessions.rpc.fail_turn = True
        self.sessions.inbox_wake._dispatch(child)
        self.sessions.inbox_wake._dispatch(child)
        self.assertEqual(len(self.reports()), 1)
        self.assertIn("Provider unavailable", self.reports()[0]["body"])
        other = self.create()
        with patch.object(self.sessions, "read", side_effect=RuntimeError("Login expired")):
            self.sessions.inbox_wake._dispatch(other)
        self.assertEqual(len(self.reports()), 2)
        self.assertIn("Login expired", self.reports()[1]["body"])

    def test_creation_errors_and_abandoned_creation_notify_once(self):
        for uncertain in (False, True):
            request = self.request()
            self.control.claim(request["request_id"], "owner")
            self.control.complete(request["request_id"], error="Cannot launch provider", uncertain=uncertain)
            self.control.complete(request["request_id"], error="Cannot launch provider", uncertain=uncertain)
        self.assertEqual(len(self.reports()), 2)
        request = self.request()
        self.control.claim(request["request_id"], "gone")
        with self.store._connection() as db:
            db.execute("UPDATE app_control_requests SET updated_at = 0 WHERE request_id = ?", (request["request_id"],))
        self.assertEqual(self.control.status(request["request_id"])["status"], "uncertain")
        AppControl(self.store).status(request["request_id"])
        self.assertEqual(len(self.reports()), 3)

    def test_durable_receipt_deduplicates_concurrent_delivery_and_restart(self):
        child = self.create()
        def notify(_):
            notify_agent_failure(CoordinationStore(self.store.database_path), child,
                                 event_key=["turn", "same-turn"], stage="turn", error="Failed")
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(notify, range(8)))
        self.assertEqual(len(self.reports()), 1)

    def test_database_contention_retries_report_without_replaying_turn(self):
        child = self.create()
        self.sessions.send(child, {"message": "Start"})
        from agent_coord import codex_app_server
        real = codex_app_server.notify_agent_failure
        locked = threading.Event()
        def notify(*args, **kwargs):
            if not locked.is_set():
                raise sqlite3.OperationalError("database is locked")
            return real(*args, **kwargs)
        with patch.object(codex_app_server, "notify_agent_failure", side_effect=notify):
            self.finish(child, error="Credit limit")
            self.assertEqual(self.reports(), [])
            locked.set()
            self.assertTrue(self.sessions.persistence.flush(timeout=3))
        self.assertEqual(len(self.reports()), 1)
        self.assertEqual(len(self.sessions.rpc.threads[child]["turns"]), 1)

    def test_notification_insert_failure_rolls_back_creation_outcome(self):
        request = self.request()
        self.control.claim(request["request_id"], "owner")
        with patch.object(self.store, "_insert_message", side_effect=sqlite3.OperationalError("database is locked")):
            with self.assertRaises(sqlite3.OperationalError):
                self.control.complete(request["request_id"], error="Provider failed")
        self.assertEqual(self.control.status(request["request_id"])["status"], "running")
        self.control.complete(request["request_id"], error="Provider failed")
        self.assertEqual(len(self.reports()), 1)
