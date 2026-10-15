from __future__ import annotations

import copy
from pathlib import Path
import tempfile
import unittest

from test_codex_app_server import BrowserSessions, CoordinationStore, FakeCodex
from test_browser_providers import claude_factory
from agent_coord.hook import _actionable_context


class EventRuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = CoordinationStore(self.root / "state.sqlite3")
        self.sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex,
                                        claude_factory=claude_factory)
        self.addCleanup(self.sessions.close)

    def create(self, client="codex"):
        return self.sessions.create({"client": client})["session"]["thread_id"]

    def finish(self, thread):
        if self.sessions._record(thread)["client"] == "claude":
            self.sessions.claude.connections[thread].callback({"type": "result", "subtype": "success"})
        else:
            turn = self.sessions.rpc.threads[thread]["turns"][-1]
            turn["status"] = "completed"
            self.sessions._event({"method": "turn/completed", "params": {
                "threadId": thread, "turn": copy.deepcopy(turn)}})
        self.assertTrue(self.sessions.persistence.flush())

    def turns(self, thread):
        return self.sessions.read(thread)["thread"]["turns"]

    def test_completion_waits_for_subscriber_turn_then_wakes_same_conversation_once(self):
        for client in ("codex", "claude"):
            with self.subTest(client=client):
                subscriber, worker = self.create(client), self.create()
                self.sessions.send(subscriber, {"message": "Coordinate the worker"})
                self.sessions.send(worker, {"message": "Do the work"})
                self.assertTrue(self.sessions.persistence.flush())
                self.sessions.event_subscriptions.create(subscriber, sessions=[worker], continuation="Review the worker result")
                self.finish(worker)
                self.sessions.event_subscriptions.process_once()
                self.sessions.inbox_wake.process_once()
                self.assertEqual(len(self.turns(subscriber)), 1)
                self.finish(subscriber)
                self.sessions.inbox_wake.process_once()
                self.assertEqual(len(self.turns(subscriber)), 2)
                context = _actionable_context(self.store, subscriber)
                self.assertIn("Review the worker result", context)
                self.assertIn("reply_required=false", context)
                self.finish(subscriber)
                self.sessions.event_subscriptions.process_once()
                self.sessions.inbox_wake.process_once()
                self.assertEqual(len(self.turns(subscriber)), 2)

    def test_event_queued_before_close_stays_asleep_until_explicit_reopen(self):
        subscriber, worker = self.create(), self.create()
        self.sessions.send(worker, {"message": "Do the work"})
        self.assertTrue(self.sessions.persistence.flush())
        self.sessions.event_subscriptions.create(subscriber, sessions=[worker])
        self.finish(worker)
        self.sessions.event_subscriptions.process_once()
        self.sessions.close_work_thread(subscriber)
        calls_before = len(self.sessions.rpc.calls)
        self.sessions.inbox_wake.process_once()
        self.assertEqual(len(self.sessions.rpc.calls), calls_before)
        self.assertEqual(self.store.threads.get(subscriber)["attention"], "archived")
        self.assertEqual(self.turns(subscriber), [])
        self.sessions.reopen_work_thread(subscriber)
        self.sessions.inbox_wake.process_once()
        self.assertEqual(len(self.turns(subscriber)), 1)

    def test_stop_retains_event_without_automatically_resuming(self):
        subscriber, worker = self.create(), self.create()
        self.sessions.send(subscriber, {"message": "Wait for work"})
        self.sessions.send(worker, {"message": "Do the work"})
        self.assertTrue(self.sessions.persistence.flush())
        self.sessions.event_subscriptions.create(subscriber, sessions=[worker])
        self.sessions.interrupt(subscriber)
        self.finish(worker)
        self.sessions.event_subscriptions.process_once()
        self.sessions.inbox_wake.process_once()
        self.assertEqual(len(self.turns(subscriber)), 1)
        self.assertEqual(len(self.store.inbox(subscriber, mark_delivered=False)), 1)


if __name__ == "__main__":
    unittest.main()
