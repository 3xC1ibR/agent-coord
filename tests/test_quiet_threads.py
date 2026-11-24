"""Quiet threads: notification suppression, attention exceptions and their sources."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_codex_app_server import BrowserSessions, CoordinationStore, FakeCodex
from agent_coord import notification_policy
from agent_coord.cli import _parser, run
from agent_coord.store import CoordinationError
from agent_coord.stoic_integration import StoicIntegration


def policy_thread(**extra):
    return {"thread_id": "t", "notification_context": None, "checkpoint": None, "checkpoint_stale": False,
            "turn_completion": {"id": 1, "status": "completed"}, **extra}


class QuietPolicyTests(unittest.TestCase):
    def test_quiet_suppresses_results_but_not_failures_or_user_needs(self):
        ordinary = notification_policy.decide(policy_thread())
        self.assertEqual((ordinary["quiet"], ordinary["notify"]), (False, True))
        quiet = notification_policy.decide(policy_thread(quiet=True))
        self.assertEqual((quiet["quiet"], quiet["notify"]), (True, False))
        failed = notification_policy.decide(policy_thread(quiet=True, turn_completion={"id": 1, "status": "failed"}))
        self.assertTrue(failed["notify"])
        asks = notification_policy.decide(policy_thread(quiet=True, checkpoint={
            "id": 1, "phase": "planning", "next_actor": "user", "next_action": "Choose"}))
        self.assertTrue(asks["notify"])
        self.assertTrue(notification_policy.decide(policy_thread(quiet=True), waiting=True)["notify"])
        stalled = notification_policy.decide(policy_thread(quiet=True, stall_incident={"last_user_alert_at": 5.0}))
        self.assertTrue(stalled["notify"])


class QuietThreadTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = CoordinationStore(self.root / "state.sqlite3")
        self.sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(self.sessions.close)
        self.thread = self.sessions.create({"name": "Question writer"})["session"]["thread_id"]

    def turn(self, status="completed", checkpoint=None):
        self.sessions.send(self.thread, {"message": "Write today's questions."})
        self.assertTrue(self.sessions.persistence.flush())
        if checkpoint:
            self.store.threads.checkpoint(self.thread, checkpoint)
        turn = self.sessions.rpc.threads[self.thread]["turns"][-1]
        turn["status"] = status
        self.sessions._event({"method": "turn/completed", "params": {"threadId": self.thread, "turn": turn}})
        self.assertTrue(self.sessions.persistence.flush())
        return self.store.threads.get(self.thread)["turn_completion"]["id"]

    def quiet(self, value=True):
        return self.sessions.update_work_thread(self.thread, {"quiet": value})

    def test_update_persists_and_validates(self):
        self.assertFalse(self.sessions.work_thread(self.thread)["quiet"])
        self.assertTrue(self.quiet()["quiet"])
        self.assertTrue(next(t for t in self.sessions.list_work_threads() if t["thread_id"] == self.thread)["quiet"])
        with self.assertRaises(CoordinationError):
            self.quiet("yes")
        self.assertFalse(self.quiet(False)["quiet"])

    def test_completed_turn_is_silent_and_outside_attention(self):
        self.quiet()
        completion = self.turn(checkpoint={"phase": "finished", "summary": "Questions written.", "next_step": None})
        self.assertFalse(self.sessions.completion_notification_allowed(completion))
        self.assertEqual(self.sessions.completions_after(0)["events"], [])
        self.assertFalse(self.sessions.claim_notification(completion))
        thread = self.sessions.work_thread(self.thread)
        self.assertFalse(thread["needs_attention"])
        # The result itself remains readable; only presentation is quiet.
        self.assertEqual(self.store.threads.completions_after(0)["events"][0]["id"], completion)

    def test_failure_and_user_request_still_notify_and_need_attention(self):
        self.quiet()
        failed = self.turn(status="failed")
        self.assertTrue(self.sessions.completion_notification_allowed(failed))
        self.assertTrue(self.sessions.work_thread(self.thread)["needs_attention"])
        needs_user = self.turn(checkpoint={"phase": "planning", "summary": "Two options.",
                                           "next_step": {"actor": "user", "action": "Choose one"}})
        self.assertTrue(self.sessions.completion_notification_allowed(needs_user))
        self.assertTrue(self.sessions.work_thread(self.thread)["needs_attention"])

    def test_stalled_quiet_thread_stays_in_attention(self):
        self.quiet()
        self.turn()
        self.assertFalse(self.sessions.work_thread(self.thread)["needs_attention"])
        stalled = ({}, False, True, {"state": "stalled", "turn_id": "stalled-turn"})
        with patch.object(self.sessions, "_thread_runtime_state", return_value=stalled):
            thread = self.sessions.work_thread(self.thread)
        self.assertEqual((thread["status"], thread["needs_attention"]), ("stalled", True))

    def test_turning_quiet_off_restores_ordinary_notifications(self):
        self.quiet()
        first = self.turn()
        self.assertFalse(self.sessions.completion_notification_allowed(first))
        self.quiet(False)
        second = self.turn()
        self.assertTrue(self.sessions.completion_notification_allowed(second))
        self.assertTrue(self.sessions.work_thread(self.thread)["needs_attention"])

    def test_cli_update_marks_the_caller_quiet(self):
        def run_cli(*args):
            return run(_parser().parse_args(["--db", str(self.store.database_path), *args]))
        self.assertTrue(run_cli("thread", "update", "--session-id", self.thread, "--quiet")["quiet"])
        self.assertFalse(run_cli("thread", "update", "--session-id", self.thread, "--no-quiet")["quiet"])
        self.assertNotIn("quiet", vars(_parser().parse_args(["thread", "update", "--title", "Kept"])))


class QuietExternalWorkTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = CoordinationStore(self.root / "state.sqlite3")
        self.sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(self.sessions.close)
        self.work = self.sessions.external_work

    def test_quiet_option_marks_the_new_conversation_before_its_turn(self):
        quiet = self.work.submit("test", "quiet", {}, "Compute.", name="Daily", quiet=True)
        self.assertEqual(quiet["state"], "running", quiet)
        self.assertTrue(self.store.threads.get(quiet["thread_id"])["quiet"])
        ordinary = self.work.submit("test", "ordinary", {}, "Compute.")
        self.assertFalse(self.store.threads.get(ordinary["thread_id"])["quiet"])
        self.assertEqual(self.work.submit("test", "quiet", {}, "Compute.", name="Daily", quiet=True), quiet)
        with self.assertRaises(CoordinationError):
            self.work.submit("test", "bad", {}, "Compute.", quiet="failures")


class StoicNotifyMappingTests(unittest.TestCase):
    def test_notify_failures_submits_quiet_work_and_other_values_do_not(self):
        from test_stoic_integration import FakeExternalWork, FakeStoic
        for notify, expected in (("failures", True), ("always", False), (None, False)):
            with self.subTest(notify=notify), tempfile.TemporaryDirectory() as directory:
                store = CoordinationStore(Path(directory) / "state.sqlite3")
                engine, remote = FakeExternalWork(store), FakeStoic()
                if notify is not None:
                    remote.runs["run-1"]["notify"] = notify
                connector = StoicIntegration(engine, client_factory=lambda *_: remote)
                try:
                    connector.configure({"enabled": True, "allowed_roots": ["/workspace"],
                                         "allowed_permission_modes": {"claude": ["bypassPermissions"]}})
                    connector.process_once()
                    connector.process_once()
                finally:
                    connector.close()
                self.assertEqual(next(iter(engine.rows.values()))["quiet"], expected)


if __name__ == "__main__":
    unittest.main()
