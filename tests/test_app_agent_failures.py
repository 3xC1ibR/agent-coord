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
from agent_coord import supervision
from agent_coord.app_agent_failures import notify_agent_failure, process_failure_followups, turn_failure_message


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
        self.persisted(self.parent)

    def persisted(self, identity):
        self.assertTrue(self.sessions.persistence.flush(timeout=3, key=identity))

    def send(self, identity, body):
        result = self.sessions.send(identity, body)
        self.persisted(identity)
        return result

    def dispatch(self, identity):
        self.sessions.inbox_wake._dispatch(identity)
        self.persisted(identity)

    def request(self, **values):
        return self.control.request("create", self.parent, {
            "cwd": str(self.root), "client": "codex", "prompt": "Implement the requested change.",
            "name": "Worker", **values})

    def create(self, **values):
        request = self.request(**values)
        self.worker.process_once()
        receipt = self.control.status(request["request_id"])
        self.assertEqual(receipt["status"], "completed")
        self.persisted(receipt["thread_id"])
        return receipt["thread_id"]

    def reports(self):
        return self.store.inbox(self.parent, mark_delivered=False)

    def finish(self, child, status="failed", error=None, *, persist=True):
        turn = self.sessions.rpc.threads[child]["turns"][-1]
        turn.update(status=status, error={"message": error} if error else None)
        event = {"method": "turn/completed", "params": {"threadId": child, "turn": copy.deepcopy(turn)}}
        self.sessions._event(event)
        if persist:
            self.persisted(child)
        return event

    def test_limits_and_other_errors_notify_creator_without_reply_contract(self):
        for error in ("5 hour usage limit reached; resets at 6pm", "Insufficient credits", "Provider crashed"):
            child = self.create()
            self.dispatch(child)
            self.store.inbox(child)  # Normal hook delivery.
            event = self.finish(child, error=error)
            self.sessions._event(event)
            self.persisted(child)
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
        self.dispatch(self.parent)
        self.assertIn(self.parent, self.sessions.active)

    def test_claude_limit_result_uses_same_notification_path(self):
        child = self.create(client="claude", mode="orchestration", reply_required=True)
        self.dispatch(child)
        self.sessions.claude.connections[child].callback({
            "type": "result", "subtype": "error_during_execution", "is_error": True,
            "errors": ["You have hit your limit; resets at 7pm"]})
        self.persisted(child)
        self.assertEqual(json.loads(self.reports()[0]["body"])["error"], "You have hit your limit; resets at 7pm")

    def test_success_stop_and_unrelated_conversations_do_not_notify(self):
        for status in ("completed", "interrupted"):
            child = self.create()
            self.send(child, {"message": "Start"})
            if status == "interrupted":
                self.sessions.interrupt(child)
                self.persisted(child)
            else:
                self.finish(child, status=status)
        ordinary = self.sessions.create({})["session"]["thread_id"]
        self.send(ordinary, {"message": "Start"})
        self.finish(ordinary)
        self.assertEqual(self.reports(), [])

    def test_unsolicited_interruption_after_initial_result_alerts_and_does_not_resume(self):
        child = self.create()
        self.dispatch(child)
        self.finish(child, status="completed")
        self.sessions.event_subscriptions.process_once()
        before = len(self.reports())
        self.send(child, {"message": "Continue the assignment"})
        event = self.finish(child, status="interrupted")
        self.sessions._event(event)
        self.persisted(child)
        self.assertEqual(len(self.reports()), before + 1)
        self.assertEqual(json.loads(self.reports()[-1]["body"])["stage"], "turn")
        with self.store._connection() as db:
            self.assertEqual(db.execute("SELECT pause_kind FROM browser_inbox_wake WHERE thread_id=?", (child,)).fetchone()[0], "failure")
        self.sessions.inbox_wake.process_once()
        self.assertNotIn(child, self.sessions.active)

    def test_native_user_cancellation_remains_stop_without_failure_alert(self):
        child = self.create()
        self.send(child, {"message": "Start"})
        turn = self.sessions.rpc.threads[child]["turns"][-1]
        turn["termination"] = {"origin": "user", "reason": "cancelled", "execution": "unknown"}
        self.finish(child, status="interrupted")
        self.assertEqual(self.reports(), [])
        with self.store._connection() as db:
            self.assertEqual(db.execute("SELECT pause_kind FROM browser_inbox_wake WHERE thread_id=?", (child,)).fetchone()[0], "stop")

    def overdue_child(self):
        self.wall = 2000000000.0
        self.store.clock = lambda: self.wall
        child = self.create()
        self.dispatch(child)
        self.store.inbox(child)
        self.finish(child, status="interrupted")
        return child

    def test_unresolved_failure_escalates_once_after_ten_minutes_even_when_acknowledged(self):
        child = self.overdue_child()
        self.store.acknowledge(self.parent, self.reports()[0]["id"])
        self.wall += 599
        process_failure_followups(self.store)
        self.assertEqual(len(self.reports()), 0)  # Acknowledged reports leave the unread inbox.
        self.wall += 1
        self.sessions.watchdog.process_once()
        self.assertEqual(len(self.reports()), 1)
        self.assertEqual(json.loads(self.reports()[-1]["body"])["event"], "app_agent_recovery_overdue")
        process_failure_followups(CoordinationStore(self.store.database_path, clock=lambda: self.wall + 1000))
        self.assertEqual(len(self.reports()), 1)
        self.assertEqual(len(self.sessions.rpc.threads[child]["turns"]), 1)
        self.assertNotIn(child, self.sessions.active)

    def test_resumed_stopped_or_closed_work_cancels_deadline(self):
        for resolution in ("new_turn", "user_stop", "closed"):
            child = self.overdue_child()
            before = len(self.reports())
            if resolution == "new_turn":
                self.send(child, {"message": "Checked prior native results; continue"})
            elif resolution == "user_stop":
                self.sessions.inbox_wake.pause(child, "User stopped", kind="stop")
            else:
                self.store.threads.update(child, attention="archived")
            self.wall += 600
            process_failure_followups(self.store)
            self.assertEqual(len(self.reports()), before)
            with self.store._connection() as db:
                self.assertEqual(db.execute("SELECT resolution FROM app_agent_followups WHERE thread_id=?", (child,)).fetchone()[0], resolution)

    def test_deadline_insert_and_marker_roll_back_together(self):
        self.overdue_child()
        self.wall += 600
        with patch.object(self.store, "_insert_message", side_effect=sqlite3.OperationalError("locked")):
            with self.assertRaises(sqlite3.OperationalError):
                process_failure_followups(self.store)
        with self.store._connection() as db:
            self.assertIsNone(db.execute("SELECT escalated_at FROM app_agent_followups").fetchone()[0])
        process_failure_followups(self.store)
        self.assertEqual(len(self.reports()), 2)

    def test_recorded_scope_handoff_cancels_escalation(self):
        child = self.overdue_child()
        self.store.begin_work(session_id=child, scopes=["owned.py"])
        replacement = self.create()
        self.store.handoff_work(sender_session_id=child, recipient_session_id=replacement,
            patch_label="Saved work", validation_boundary="Native agent and job results inspected",
            validation_responsibility="Replacement owns remaining checks", mode="write")
        before = len(self.reports())
        self.wall += 600
        process_failure_followups(self.store)
        self.assertEqual(len(self.reports()), before)
        with self.store._connection() as db:
            self.assertEqual(db.execute("SELECT resolution FROM app_agent_followups WHERE thread_id=?", (child,)).fetchone()[0], "handoff")

    def test_escalation_keeps_paused_owner_paused_and_persists_message(self):
        self.overdue_child()
        self.sessions.inbox_wake.pause(self.parent, "User stopped", kind="stop")
        self.wall += 600
        process_failure_followups(self.store)
        self.dispatch(self.parent)
        self.assertNotIn(self.parent, self.sessions.active)
        self.assertEqual(len(self.reports()), 2)
        self.assertIsNone(self.reports()[-1]["delivered_at"])

    def test_initial_interruption_subscription_and_failure_share_notice_and_deadline(self):
        child = self.overdue_child()
        self.sessions.event_subscriptions.process_once()
        self.assertEqual(len(self.reports()), 1)
        self.wall += 600
        process_failure_followups(self.store)
        self.assertEqual(len(self.reports()), 2)

    def test_later_failure_reports_again_and_busy_parent_keeps_message(self):
        child = self.create()
        self.send(self.parent, {"message": "Continue orchestration"})
        for _ in range(2):
            self.send(child, {"message": "Try again"})
            self.finish(child, error="No credits")
        self.dispatch(self.parent)
        self.assertEqual(len(self.sessions.rpc.threads[self.parent]["turns"]), 1)
        self.assertEqual(len(self.reports()), 2)
        self.assertTrue(all(report["delivered_at"] is None for report in self.reports()))

    def test_disconnect_reports_active_child_once_but_not_idle_children(self):
        child = self.create()
        self.create()
        self.send(child, {"message": "Start"})
        self.sessions._event({"method": "bridge/disconnected"})
        self.sessions._event({"method": "bridge/disconnected"})
        self.persisted(child)
        self.assertEqual(len(self.reports()), 1)
        self.assertEqual(json.loads(self.reports()[0]["body"])["stage"], "disconnect")
        self.assertEqual(self.store.threads.get(child)["turn_completion"]["status"], "failed")

    def test_normal_runtime_shutdown_does_not_report_disconnect_failure(self):
        for client in ("codex", "claude"):
            child = self.create(client=client)
            self.send(child, {"message": "Start"})
        self.sessions.close()
        self.assertEqual(self.reports(), [])
        with self.store._connection() as db:
            self.assertEqual(db.execute("SELECT pause_kind FROM browser_inbox_wake WHERE thread_id=?", (child,)).fetchone()[0], "stop")

    def test_wake_start_and_resume_failures_report_and_do_not_retry(self):
        child = self.create()
        self.sessions.rpc.fail_turn = True
        self.dispatch(child)
        self.dispatch(child)
        self.assertEqual(len(self.reports()), 1)
        self.assertIn("Provider unavailable", self.reports()[0]["body"])
        other = self.create()
        with patch.object(self.sessions, "read", side_effect=RuntimeError("Login expired")):
            self.dispatch(other)
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
        self.send(child, {"message": "Start"})
        from agent_coord import codex_app_server
        real = codex_app_server.notify_agent_failure
        locked = threading.Event()
        attempted = threading.Event()
        def notify(*args, **kwargs):
            attempted.set()
            if not locked.is_set():
                raise sqlite3.OperationalError("database is locked")
            return real(*args, **kwargs)
        with patch.object(codex_app_server, "notify_agent_failure", side_effect=notify):
            self.finish(child, error="Credit limit", persist=False)
            self.assertTrue(attempted.wait(1), "The persistence worker must encounter the held report write")
            self.assertEqual(self.reports(), [])
            locked.set()
            self.persisted(child)
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

    def test_failure_and_subscription_matching_emit_one_notice_in_either_order(self):
        for failure_first in (True, False):
            with self.subTest(failure_first=failure_first):
                child = self.create(mode="orchestration")
                self.dispatch(child)
                self.store.inbox(child)
                turn = self.sessions.rpc.threads[child]["turns"][-1]["id"]
                if failure_first:
                    self.finish(child, error="No credits")
                else:
                    self.store.threads.finish_turn(child, turn_id=turn, status="failed")
                self.sessions.event_subscriptions.process_once()
                before = len(self.reports())
                self.assertEqual(before, 1 if failure_first else 2)
                notify_agent_failure(self.store, child, event_key=["turn", turn],
                                     stage="turn", error="No credits", turn_id=turn)
                self.sessions.event_subscriptions.process_once()
                self.assertEqual(len(self.reports()), before)
                body = json.loads(self.reports()[-1]["body"])
                snapshot = body if failure_first else body["outcomes"][0]
                self.assertIsNone(snapshot["context_lifecycle"])
                subscription = next(sub for sub in self.sessions.event_subscriptions.list(self.parent)
                                    if sub["items"][0]["session_id"] == child)
                self.assertEqual(subscription["state"], "fired")
                self.assertEqual(subscription["message_id"], self.reports()[-1]["id"])
                if failure_first:
                    self.assertEqual(body["error"], "No credits")
                    # Cancelling a subscription must not suppress the independent failure report.
                    self.sessions.event_subscriptions.cancel(self.parent, subscription["id"])
                    self.assertEqual(len(self.reports()), before)

    def test_failures_after_supervision_transfer_report_to_successor_once(self):
        child = self.create(subscribe=False)
        successor = self.sessions.create({"name": "Successor"})["session"]["thread_id"]
        self.store.inbox(child)
        self.store.touch(child, turn_active=False)
        supervision.transfer(self.store, requester=self.parent, from_session=self.parent, to_session=successor,
                             thread_ids=[child], request_id="adopt", reason="Replace lead", workspace=str(self.root))
        before = len(self.reports())
        for _ in range(2):
            notify_agent_failure(self.store, child, event_key=["turn", "t1"], stage="turn",
                                 error="Provider crashed", turn_id="t1")
        self.assertEqual(len(self.reports()), before)
        reports = [r for r in self.store.inbox(successor, mark_delivered=False) if r["classification"] == "action_required"]
        self.assertEqual([json.loads(r["body"])["session_id"] for r in reports], [child])
        with self.store._connection() as db:
            self.assertEqual(turn_failure_message(db, successor, child, "t1"), reports[0]["id"])
            self.assertIsNone(turn_failure_message(db, self.parent, child, "t1"))

    def test_opt_out_keeps_failure_notification(self):
        child = self.create(subscribe=False)
        self.dispatch(child)
        self.store.inbox(child)
        self.finish(child, error="Usage limit")
        self.sessions.event_subscriptions.process_once()
        self.assertEqual(len(self.reports()), 1)
        self.assertEqual(json.loads(self.reports()[0]["body"])["error"], "Usage limit")
        self.assertEqual(self.sessions.event_subscriptions.list(self.parent), [])

    def test_failure_snapshot_contains_only_this_turns_assignment_report(self):
        child = self.create(mode="orchestration")
        self.dispatch(child)
        message = self.store.inbox(child)[0]
        self.store.threads.checkpoint(child, {
            "phase": "implementation", "summary": "Provider failed during the requested work.",
            "context_lifecycle": {
                "assignment_message_id": message["id"], "recommendation": "fresh", "reason": "Useful facts are saved.",
                "reuse_for": [], "fresh_for": ["Retry the task"], "handover": {
                    "confirmed_facts": [], "constraints": [], "tried_and_failed": ["The provider has no credits."],
                    "open_questions": []}}})
        self.finish(child, error="No credits")
        self.sessions.event_subscriptions.process_once()
        body = json.loads(self.reports()[0]["body"])
        self.assertEqual(body["context_lifecycle"]["recommendation"], "fresh")
        self.assertEqual(body["assignment_message_id"], message["id"])
        self.send(child, {"message": "Try again"})
        self.finish(child, error="Still no credits")
        later = json.loads(self.reports()[1]["body"])
        self.assertIsNone(later["context_lifecycle"])
