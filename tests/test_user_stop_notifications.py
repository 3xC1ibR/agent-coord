"""Parent notifications retain the intent of an exact, deliberately stopped turn."""
import copy
import json
import os
import unittest
from unittest.mock import patch

import test_app_agent_failures as failures
from test_antigravity import FakeACP
from test_kimi import FakeKimi
from test_muse import FakeMuse
from agent_coord.event_subscriptions import EventSubscriptions
from agent_coord.app_agent_failures import notify_agent_failure
from agent_coord.store import CoordinationStore


class UserStopNotificationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = failures.AppAgentFailureTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.sessions = self.fixture.sessions
        self.store = self.fixture.store

    def start(self, **options):
        child = self.fixture.create(**options)
        self.sessions.inbox_wake._dispatch(child)
        self.store.inbox(child)
        return child, self.sessions.active[child]

    def outcomes(self):
        self.assertTrue(self.sessions.persistence.flush())
        self.sessions.event_subscriptions.process_once()
        return [json.loads(m["body"]) for m in self.fixture.reports()]

    def assert_user_stop(self, body, turn):
        self.assertEqual(body["event"], "subscription_matched")
        outcome = body["outcomes"][0]
        self.assertEqual(outcome["turn_id"], turn)
        self.assertEqual(outcome["state"], "interrupted")
        self.assertEqual(outcome["termination"]["origin"], "user")
        self.assertIn("stopped by the user", body["continuation"])
        self.assertIn("Do not retry", body["continuation"])

    def test_stop_notifies_parent_once_with_explicit_intent(self):
        self.sessions.antigravity.connection_factory = FakeACP
        self.sessions.kimi.connection_factory = FakeKimi
        self.sessions.muse.connection_factory = FakeMuse
        environment = patch.dict(os.environ, {"KIMI_CODE_HOME": str(self.fixture.root / "kimi")})
        environment.start()
        self.addCleanup(environment.stop)
        for index, client in enumerate(("codex", "claude", "antigravity", "kimi", "muse"), 1):
            with self.subTest(client=client):
                child, turn = self.start(client=client)
                self.sessions.interrupt(child)
                bodies = self.outcomes()
                self.assert_user_stop(bodies[-1], turn)
                self.assertEqual(len(bodies), index)
                self.assertEqual(self.outcomes(), bodies)
                with self.store._connection() as db:
                    self.assertFalse(db.execute("SELECT 1 FROM sqlite_master WHERE name = 'app_agent_followups'").fetchone())

    def test_provider_failure_shaped_stop_is_still_a_user_stop(self):
        child, turn = self.start()
        native_request = self.sessions.rpc.request
        def request(method, params=None):
            if method == "turn/interrupt":
                self.fixture.finish(child, status="failed", error="Cancelled")
                return {}
            return native_request(method, params)
        with patch.object(self.sessions.rpc, "request", side_effect=request):
            self.sessions.interrupt(child)
        bodies = self.outcomes()
        self.assertEqual(len(bodies), 1)
        self.assert_user_stop(bodies[0], turn)

    def test_delayed_stop_bookkeeping_after_resume_does_not_become_failure(self):
        child, turn = self.start()
        saved = self.sessions._persist_event
        pending = []
        def persist(method, params, disconnected, disconnected_turns=None):
            if method == "turn/completed":
                pending.append((method, copy.deepcopy(params), disconnected, disconnected_turns))
            else:
                saved(method, params, disconnected, disconnected_turns)
        with patch.object(self.sessions, "_persist_event", side_effect=persist):
            self.sessions.interrupt(child)
        # Direct user input clears the session-wide pause before bookkeeping.
        self.sessions.inbox_wake.resume(child)
        saved(*pending[0])
        bodies = self.outcomes()
        self.assertEqual(len(bodies), 1)
        self.assert_user_stop(bodies[0], turn)

    def test_stop_metadata_survives_restart_and_newer_turn(self):
        child, turn = self.start()
        self.sessions.interrupt(child)
        self.sessions.send(child, {"message": "Continue after inspecting prior results"})
        restarted = EventSubscriptions(CoordinationStore(self.store.database_path))
        self.addCleanup(restarted.close)
        restarted.process_once()
        self.assert_user_stop(json.loads(self.fixture.reports()[0]["body"]), turn)
        self.fixture.finish(child, error="Insufficient credits")
        bodies = self.outcomes()
        self.assertEqual(bodies[-1]["event"], "app_agent_failed")
        self.assertEqual(bodies[-1]["error"], "Insufficient credits")

    def test_native_user_termination_is_durable_for_subscription(self):
        child, turn = self.start()
        native = self.sessions.rpc.threads[child]["turns"][-1]
        native["termination"] = {"origin": "user", "reason": "cancelled", "execution": "unknown"}
        self.fixture.finish(child, status="interrupted")
        self.assert_user_stop(self.outcomes()[0], turn)

    def test_unsolicited_interruption_remains_a_failure(self):
        child, _ = self.start()
        self.fixture.finish(child, status="interrupted")
        bodies = self.outcomes()
        self.assertEqual(len(bodies), 1)
        self.assertEqual(bodies[0]["event"], "app_agent_failed")

    def test_cancelled_provider_statuses_produce_terminal_outcomes(self):
        for status in ("cancelled", "canceled"):
            for user in (False, True):
                with self.subTest(status=status, user=user):
                    child, turn = self.start()
                    if user:
                        self.store.threads.request_stop(child, turn)
                    self.fixture.finish(child, status=status)
                    body = self.outcomes()[-1]
                    if user:
                        self.assert_user_stop(body, turn)
                    else:
                        self.assertEqual(body["event"], "app_agent_failed")
                    subscription = next(s for s in self.sessions.event_subscriptions.list(self.fixture.parent)
                                        if s["items"][0]["session_id"] == child)
                    self.assertEqual(subscription["state"], "fired")

    def test_late_failure_callback_cannot_create_alert_or_recovery_deadline(self):
        child, turn = self.start(subscribe=False)
        self.sessions.interrupt(child)
        self.sessions.inbox_wake.resume(child)
        notify_agent_failure(self.store, child, event_key=["turn", turn], stage="turn",
                             turn_id=turn, error="Late provider failure")
        self.assertEqual(self.outcomes(), [])
        with self.store._connection() as db:
            self.assertFalse(db.execute("SELECT 1 FROM sqlite_master WHERE name = 'app_agent_followups'").fetchone())

    def test_cold_provider_read_preserves_stop_even_after_pause_reset(self):
        child, _ = self.start(subscribe=False)
        self.sessions.interrupt(child)
        self.sessions.inbox_wake.resume(child)
        self.store.send_message(sender_session_id=self.fixture.parent, recipient_session_id=child,
                                body="Pending request", reply_required=False)
        self.sessions.inbox_wake._dispatch(child)
        self.assertEqual(self.outcomes(), [])
        with self.store._connection() as db:
            pause = db.execute("SELECT paused, pause_kind FROM browser_inbox_wake WHERE thread_id = ?", (child,)).fetchone()
            self.assertEqual(tuple(pause), (1, "stop"))

    def test_unrelated_stop_pause_cannot_hide_genuine_turn_failure(self):
        child, _ = self.start()
        self.sessions.inbox_wake.pause(child, "An earlier stop", kind="stop")
        self.fixture.finish(child, error="Provider crashed")
        body = self.outcomes()[0]
        self.assertEqual(body["event"], "app_agent_failed")
        self.assertEqual(body["error"], "Provider crashed")

    def test_explicit_subscription_mixed_outcomes_preserves_other_continuation(self):
        stopped, stop_turn = self.start(subscribe=False)
        failed, fail_turn = self.start(subscribe=False)
        subscription = self.sessions.event_subscriptions.create(
            self.fixture.parent, sessions=[stopped, failed], continuation="Inspect the failed worker")
        self.sessions.interrupt(stopped)
        self.fixture.finish(failed, error="Out of credits")
        bodies = self.outcomes()
        body = next(b for b in bodies if b.get("subscription_id") == subscription["id"])
        by_turn = {o["turn_id"] + o["session_id"]: o for o in body["outcomes"]}
        self.assertEqual(by_turn[stop_turn + stopped]["termination"]["origin"], "user")
        self.assertEqual(by_turn[fail_turn + failed]["state"], "failed")
        self.assertIn("Do not retry", body["continuation"])
        self.assertIn("For the other outcomes only: Inspect the failed worker", body["continuation"])

    def test_each_subscription_carries_stop_intent(self):
        child, turn = self.start(subscribe=False)
        subscription = self.sessions.event_subscriptions.create(self.fixture.parent, sessions=[child], mode="each")
        self.sessions.interrupt(child)
        body = next(b for b in self.outcomes() if b.get("subscription_id") == subscription["id"])
        self.assert_user_stop(body, turn)

    def test_stop_does_not_automatically_restart_queued_or_inbox_work(self):
        child, turn = self.start()
        self.sessions.interrupt(child)
        self.store.send_message(sender_session_id=self.fixture.parent, recipient_session_id=child,
                                body="More work", reply_required=False)
        self.sessions.inbox_wake.process_once()
        self.assertNotIn(child, self.sessions.active)
        self.assert_user_stop(self.outcomes()[0], turn)

    def test_stop_during_provider_disconnect_does_not_report_failure(self):
        child, turn = self.start()
        native_request = self.sessions.rpc.request
        def request(method, params=None):
            if method == "turn/interrupt":
                self.sessions._event({"method": "bridge/disconnected"})
                return {}
            return native_request(method, params)
        with patch.object(self.sessions.rpc, "request", side_effect=request):
            self.sessions.interrupt(child)
        bodies = self.outcomes()
        self.assertEqual(len(bodies), 1)
        self.assert_user_stop(bodies[0], turn)

    def test_stop_during_initial_submission_does_not_become_wake_failure(self):
        child = self.fixture.create()
        native_request = self.sessions.rpc.request
        def request(method, params=None):
            result = native_request(method, params)
            if method == "turn/start":
                self.sessions.interrupt(child)
                raise RuntimeError("Submission was cancelled")
            return result
        with patch.object(self.sessions.rpc, "request", side_effect=request):
            self.sessions.inbox_wake._dispatch(child)
        bodies = self.outcomes()
        self.assertEqual(len(bodies), 1)
        turn = self.store.threads.get(child)["turn_id"]
        self.assert_user_stop(bodies[0], turn)

    def test_success_that_wins_stop_race_stays_successful(self):
        child, _ = self.start()
        native_request = self.sessions.rpc.request
        def request(method, params=None):
            if method == "turn/interrupt":
                self.fixture.finish(child, status="completed")
                return {}
            return native_request(method, params)
        with patch.object(self.sessions.rpc, "request", side_effect=request):
            self.sessions.interrupt(child)
        body = self.outcomes()[0]
        self.assertEqual(body["outcomes"][0]["state"], "completed")
        self.assertNotIn("termination", body["outcomes"][0])
