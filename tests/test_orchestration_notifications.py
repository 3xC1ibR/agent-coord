from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from test_codex_app_server import BrowserSessions, CoordinationStore, FakeCodex
from agent_coord.app_control import AppControlWorker


class OrchestrationNotificationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = CoordinationStore(self.root / "state.sqlite3")
        self.sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(self.sessions.close)
        self.worker = AppControlWorker(self.sessions)
        self.addCleanup(self.worker.close)
        self.parent = self.sessions.create({"name": "Orchestrator"})["session"]["thread_id"]

    def child(self, *, parent=None, mode="orchestration", reply=True):
        body = {"cwd": str(self.root), "name": "Specialist", "prompt": "Do the work.",
                "reply_required": reply}
        if mode:
            body["mode"] = mode
        request = self.worker.control.request("create", parent or self.parent, body)
        self.worker.process_once()
        result = self.worker.control.status(request["request_id"])
        self.assertEqual(result["status"], "completed", result)
        return result["thread_id"]

    def wake(self, thread):
        self.sessions.inbox_wake.process_once()
        self.assertTrue(self.sessions.persistence.flush())
        self.store.inbox(thread, mark_delivered=True)
        return self.store.threads.get(thread)["turn_id"]

    def send(self, thread):
        self.sessions.send(thread, {"message": "Answer me directly."})
        self.assertTrue(self.sessions.persistence.flush())

    def checkpoint(self, thread, *, phase="implementation", actor="agent"):
        return self.store.threads.checkpoint(thread, {"phase": phase, "summary": "Current result",
            "next_step": None if actor is None else {"actor": actor, "action": "Choose the deployment target"}})

    def finish(self, thread, status="completed"):
        turn = self.sessions.rpc.threads[thread]["turns"][-1]
        turn["status"] = status
        self.sessions._event({"method": "turn/completed", "params": {"threadId": thread, "turn": turn}})
        self.assertTrue(self.sessions.persistence.flush())
        return self.store.threads.get(thread)["turn_completion"]["id"]

    def test_child_success_is_quiet_but_raw_completion_and_inbox_remain(self):
        child = self.child()
        turn = self.wake(child)
        self.checkpoint(child, phase="finished", actor=None)
        completion = self.finish(child)
        self.assertFalse(self.sessions.work_thread(child)["needs_attention"])
        self.assertFalse(self.sessions.claim_notification(completion))
        self.assertEqual(self.sessions.completions_after(0)["events"], [])
        raw = self.store.threads.completions_after(0)["events"]
        self.assertEqual([e["id"] for e in raw], [completion])
        self.assertTrue(self.store.inbox(child, include_delivered=True, mark_delivered=False))
        with self.store._connection() as db:
            context = db.execute("SELECT * FROM turn_notification_context WHERE thread_id=? AND turn_key=?", (child, turn)).fetchone()
        self.assertEqual((context["origin"], context["owner_thread_id"]), ("coordination", self.parent))

    def test_fast_child_turn_is_quiet_before_rpc_returns(self):
        child = self.child()
        self.sessions.rpc.fast_turn = True
        self.wake(child)
        self.assertEqual(self.sessions.completions_after(0)["events"], [])
        self.assertFalse(self.sessions.work_thread(child)["needs_attention"])

    def test_provider_response_before_start_event_retains_automatic_origin(self):
        child = self.child()
        captured = []
        callback = self.sessions.rpc.callback
        self.sessions.rpc.callback = captured.append
        self.sessions.inbox_wake.process_once()
        self.assertTrue(self.sessions.persistence.flush())
        self.sessions.rpc.callback = callback
        for event in captured:
            callback(event)
        self.assertTrue(self.sessions.persistence.flush())
        completion = self.finish(child)
        self.assertFalse(self.sessions.claim_notification(completion))
        self.assertEqual(self.sessions.work_thread(child)["notification_context"]["origin"], "coordination")

    def test_direct_child_followup_and_steering_are_user_facing(self):
        child = self.child()
        self.wake(child)
        self.send(child)  # Direct steering of the automatic turn.
        first = self.finish(child)
        self.assertTrue(self.sessions.claim_notification(first))
        self.assertTrue(self.sessions.work_thread(child)["needs_attention"])
        self.send(child)
        second = self.finish(child)
        self.assertTrue(self.sessions.claim_notification(second))
        self.assertEqual(self.sessions.work_thread(child)["attention_owner_thread_id"], child)

    def test_new_user_turn_cannot_unmute_an_old_internal_completion(self):
        child = self.child()
        self.wake(child)
        quiet = self.finish(child)
        self.send(child)
        self.checkpoint(child, actor="user")
        self.assertFalse(self.sessions.completion_notification_allowed(quiet))

    def test_answering_child_before_delivery_cancels_its_old_escalation(self):
        child = self.child()
        self.wake(child)
        self.checkpoint(child, actor="user")
        escalation = self.finish(child)
        self.assertTrue(self.sessions.completion_notification_allowed(escalation))
        self.send(child)
        self.assertFalse(self.sessions.completion_notification_allowed(escalation))

    def test_dispatched_specialist_still_notifies(self):
        child = self.child(mode=None, reply=False)
        self.wake(child)
        completion = self.finish(child)
        self.assertTrue(self.sessions.claim_notification(completion))
        self.assertTrue(self.sessions.work_thread(child)["needs_attention"])

    def test_orchestrator_wait_is_quiet_and_automatic_delivery_notifies(self):
        child = self.child()
        self.send(self.parent)
        self.checkpoint(self.parent, phase="orchestrating")
        wait = self.finish(self.parent)
        self.assertFalse(self.sessions.claim_notification(wait))
        self.assertFalse(self.sessions.work_thread(self.parent)["needs_attention"])
        self.store.send_message(sender_session_id=child, recipient_session_id=self.parent,
                                body="Result ready", reply_required=False)
        self.wake(self.parent)
        self.checkpoint(self.parent, phase="finished", actor=None)
        delivered = self.finish(self.parent)
        self.assertTrue(self.sessions.claim_notification(delivered))
        self.assertTrue(self.sessions.work_thread(self.parent)["needs_attention"])

    def test_explicit_child_user_need_survives_classifier_unavailability(self):
        child = self.child()
        self.wake(child)
        self.checkpoint(child, actor="user")
        completion = self.finish(child)
        self.assertTrue(self.sessions.claim_notification(completion))
        self.assertTrue(self.sessions.work_thread(child)["needs_attention"])

    def test_approvals_and_questions_stay_live_and_group_under_owner(self):
        children = [self.child(), self.child()]
        for i, child in enumerate(children):
            self.wake(child)
            self.sessions._event({"id": i, "method": ("item/commandExecution/requestApproval" if i == 0 else "item/tool/requestUserInput"),
                                  "params": {"threadId": child}})
        events = self.sessions.approval_notifications()
        self.assertEqual({e["status"] for e in events}, {"approval", "question"})
        threads = {t["thread_id"]: t for t in self.sessions.list_work_threads()}
        self.assertEqual([t["thread_id"] for t in threads.values() if t["needs_attention"] and not t.get("attention_grouped_under")], [self.parent])
        self.assertEqual({i["thread_id"] for i in threads[self.parent]["attention_items"]}, set(children))
        self.assertFalse(threads[self.parent]["can_handle_response"])
        for event in events:
            self.assertTrue(self.sessions.claim_approval_notification(event["request_key"]))
            self.assertFalse(self.sessions.claim_approval_notification(event["request_key"]))
        self.assertEqual(len(self.sessions.approval_notifications(include_claimed=True)), 2)

    def test_closed_or_snoozed_parent_does_not_hide_child_need(self):
        child = self.child()
        self.wake(child)
        self.checkpoint(child, actor="user")
        self.finish(child)
        self.store.threads.update(self.parent, attention="archived")
        attention = [t for t in self.sessions.list_work_threads() if t["needs_attention"]]
        self.assertEqual([t["thread_id"] for t in attention], [child])

    def test_recoverable_failure_goes_to_parent_and_stranded_failure_surfaces(self):
        child = self.child()
        self.wake(child)
        completion = self.finish(child, "failed")
        self.assertFalse(self.sessions.claim_notification(completion))
        reports = self.store.inbox(self.parent, mark_delivered=False)
        self.assertTrue(any("app_agent_failed" in m["body"] for m in reports))
        self.sessions.inbox_wake.pause(self.parent, "The owner cannot continue")
        self.assertTrue(self.sessions.work_thread(child)["needs_attention"])
        self.assertTrue(self.sessions.completion_notification_allowed(completion))

    def test_stale_terminal_owner_cannot_hide_a_failed_child(self):
        child = self.child()
        self.wake(child)
        completion = self.finish(child, "failed")
        with self.store._connection() as db:
            db.execute("DELETE FROM browser_sessions WHERE thread_id=?", (self.parent,))
            db.execute("UPDATE sessions SET last_seen_at=? WHERE session_id=?",
                       (self.store.clock() - self.store.stale_after_seconds - 1, self.parent))
        self.assertTrue(self.sessions.work_thread(child)["needs_attention"])
        self.assertTrue(self.sessions.completion_notification_allowed(completion))

    def test_nested_orchestration_uses_root_owner(self):
        child = self.child()
        grandchild = self.child(parent=child)
        self.wake(grandchild)
        self.checkpoint(grandchild, actor="user")
        self.finish(grandchild)
        threads = {t["thread_id"]: t for t in self.sessions.list_work_threads()}
        self.assertEqual(threads[grandchild]["attention_grouped_under"], self.parent)
        self.assertEqual(threads[self.parent]["attention_target_thread_id"], grandchild)

    def test_reused_specialist_tracks_current_orchestrator(self):
        self.checkpoint(self.parent, phase="orchestrating")
        specialist = self.sessions.create({"name": "Existing specialist"})["session"]["thread_id"]
        self.store.send_message(sender_session_id=self.parent, recipient_session_id=specialist,
                                body="Contribute to this task", reply_required=True)
        self.wake(specialist)
        self.checkpoint(specialist, actor="user")
        self.finish(specialist)
        self.assertEqual(self.sessions.work_thread(specialist)["attention_owner_thread_id"], self.parent)

    def test_origin_survives_restart(self):
        child = self.child()
        self.wake(child)
        completion = self.finish(child)
        reopened = BrowserSessions(CoordinationStore(self.store.database_path), str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(reopened.close)
        self.assertFalse(reopened.completion_notification_allowed(completion))
        self.assertFalse(reopened.work_thread(child)["needs_attention"])

    def test_upgrade_suppresses_known_wakes_but_preserves_unknown_user_turns(self):
        child = self.child()
        self.wake(child)
        automatic = self.finish(child)
        with self.store._connection() as db:
            db.execute("DELETE FROM turn_notification_context WHERE thread_id=?", (child,))
        self.assertFalse(self.sessions.claim_notification(automatic))
        self.assertFalse(self.sessions.work_thread(child)["needs_attention"])
        self.send(child)
        direct = self.finish(child)
        with self.store._connection() as db:
            db.execute("DELETE FROM turn_notification_context WHERE thread_id=?", (child,))
        self.assertTrue(self.sessions.claim_notification(direct))
        self.assertTrue(self.sessions.work_thread(child)["needs_attention"])

    def test_parent_restatement_does_not_duplicate_child_escalation_alert(self):
        child = self.child()
        self.wake(child)
        self.checkpoint(child, actor="user")
        child_completion = self.finish(child)
        self.assertTrue(self.sessions.claim_notification(child_completion))
        self.store.send_message(sender_session_id=child, recipient_session_id=self.parent,
                                body="User decision needed", reply_required=False)
        self.wake(self.parent)
        self.checkpoint(self.parent, phase="orchestrating", actor="user")
        parent_completion = self.finish(self.parent)
        self.assertFalse(self.sessions.claim_notification(parent_completion))


if __name__ == "__main__":
    unittest.main()
