from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))

from agent_coord.codex_app_server import BrowserSessions
from agent_coord.store import CoordinationError, CoordinationStore
from test_codex_app_server import FakeCodex


class BrowserQueueTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = CoordinationStore(self.root / "coord.sqlite3")
        self.sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(self.sessions.close)
        self.thread_id = self.sessions.create({"name": "Queued work"})["session"]["thread_id"]
        self.sessions.send(self.thread_id, {"message": "Initial work"})

    def finish(self, status="completed", thread_id=None):
        thread_id = thread_id or self.thread_id
        turn = self.sessions.rpc.threads[thread_id]["turns"][-1]
        turn["status"] = status
        self.sessions._event({"method": "turn/completed", "params": {"threadId": thread_id, "turn": copy.deepcopy(turn)}})

    def wait_for(self, predicate):
        with self.sessions.changed:
            self.assertTrue(self.sessions.changed.wait_for(predicate, timeout=5), "Queue did not reach expected state")

    def pending(self):
        return self.sessions.queue.list(self.thread_id)

    def test_enter_steers_but_queued_message_waits_past_tool_completion(self):
        turn_id = self.sessions.active[self.thread_id]
        self.sessions.queue.enqueue(self.thread_id, {"message": "Run the next task"})
        self.sessions.send(self.thread_id, {"message": "Adjust current work", "expectedTurnId": turn_id})
        self.sessions._event({"method": "item/completed", "params": {
            "threadId": self.thread_id, "turnId": turn_id,
            "item": {"id": "tool", "type": "commandExecution", "status": "completed"},
        }})
        self.assertEqual(len(self.sessions.rpc.threads[self.thread_id]["turns"]), 1)
        self.assertEqual(self.pending()[0]["message"], "Run the next task")
        self.assertEqual([m for m, _ in self.sessions.rpc.calls if m.startswith("turn/")], ["turn/start", "turn/steer"])
        self.finish()
        self.wait_for(lambda: not self.pending())
        turns = self.sessions.rpc.threads[self.thread_id]["turns"]
        self.assertEqual(len(turns), 2)
        self.assertEqual(turns[1]["items"][0]["content"][0]["text"], "Run the next task")
        self.assertEqual(sum(m == "turn/steer" for m, _ in self.sessions.rpc.calls), 1)

    def test_multiple_queued_messages_start_one_turn_each_in_order(self):
        for message in ("First follow-up", "Second follow-up"):
            self.sessions.queue.enqueue(self.thread_id, {"message": message})
        self.finish()
        self.wait_for(lambda: len(self.pending()) == 1)
        self.assertEqual(len(self.sessions.rpc.threads[self.thread_id]["turns"]), 2)
        self.assertEqual(self.pending()[0]["message"], "Second follow-up")
        self.finish()
        self.wait_for(lambda: not self.pending())
        turns = self.sessions.rpc.threads[self.thread_id]["turns"]
        self.assertEqual([t["items"][0]["content"][0]["text"] for t in turns], ["Initial work", "First follow-up", "Second follow-up"])

    def test_stop_pauses_until_explicit_resume(self):
        self.sessions.queue.enqueue(self.thread_id, {"message": "Later"})
        self.sessions.interrupt(self.thread_id)
        self.assertEqual(self.pending()[0]["state"], "paused")
        self.assertEqual(len(self.sessions.rpc.threads[self.thread_id]["turns"]), 1)
        self.sessions.queue.change(self.thread_id, {"action": "resume"})
        self.wait_for(lambda: not self.pending())
        self.assertEqual(len(self.sessions.rpc.threads[self.thread_id]["turns"]), 2)

    def test_failed_turn_pauses_queue(self):
        self.sessions.queue.enqueue(self.thread_id, {"message": "Later"})
        self.finish("failed")
        self.assertEqual(self.pending()[0]["state"], "paused")
        self.assertEqual(len(self.sessions.rpc.threads[self.thread_id]["turns"]), 1)

    def test_failed_delivery_preserves_message_and_blocks_later_items(self):
        self.sessions.queue.enqueue(self.thread_id, {"message": "First"})
        self.sessions.queue.enqueue(self.thread_id, {"message": "Second"})
        self.sessions.rpc.fail_turn = True
        self.finish()
        self.wait_for(lambda: all(item["state"] == "paused" for item in self.pending()))
        self.assertIn("Provider unavailable", self.pending()[0]["error"])
        self.assertEqual(len(self.sessions.rpc.threads[self.thread_id]["turns"]), 1)
        self.sessions.rpc.fail_turn = False
        self.sessions.queue.change(self.thread_id, {"action": "resume"})
        self.wait_for(lambda: len(self.pending()) == 1)
        self.assertEqual(self.pending()[0]["message"], "Second")

    def test_cancel_and_thread_isolation(self):
        first = self.sessions.queue.enqueue(self.thread_id, {"message": "Remove me"})
        other = self.sessions.create({"name": "Another thread"})["session"]["thread_id"]
        self.sessions.queue.change(other, {"action": "cancel", "id": first["id"]})
        self.assertEqual(len(self.pending()), 1)
        self.sessions.queue.change(self.thread_id, {"action": "cancel", "id": first["id"]})
        self.assertEqual(self.pending(), [])
        self.finish()
        self.assertEqual(len(self.sessions.rpc.threads[self.thread_id]["turns"]), 1)

    def test_browser_read_preserves_queue_and_restart_requires_review(self):
        self.sessions.queue.enqueue(self.thread_id, {"message": "Remember this follow-up"})
        self.assertEqual(self.sessions.read(self.thread_id)["queuedMessages"][0]["message"], "Remember this follow-up")
        self.sessions.close()
        reopened = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(reopened.close)
        reopened.rpc.threads = self.sessions.rpc.threads
        reopened.rpc.threads[self.thread_id]["turns"][-1]["status"] = "interrupted"
        saved = reopened.read(self.thread_id)["queuedMessages"][0]
        self.assertEqual(saved["state"], "paused")
        self.assertIn("restarted", saved["error"])
        self.assertFalse(any(m == "turn/start" for m, _ in reopened.rpc.calls))
        self.sessions = reopened
        reopened.queue.change(self.thread_id, {"action": "resume"})
        self.wait_for(lambda: not self.pending())

    def test_queue_after_completion_race_starts_a_new_turn(self):
        self.finish()
        self.sessions.queue.enqueue(self.thread_id, {"message": "Just missed completion"})
        self.wait_for(lambda: not self.pending())
        self.assertEqual(len(self.sessions.rpc.threads[self.thread_id]["turns"]), 2)

    def test_fast_queued_turns_drain_without_a_lost_wakeup(self):
        self.sessions.queue.enqueue(self.thread_id, {"message": "First"})
        self.sessions.queue.enqueue(self.thread_id, {"message": "Second"})
        self.sessions.rpc.fast_turn = True
        self.finish()
        self.wait_for(lambda: not self.pending())
        self.assertEqual(len(self.sessions.rpc.threads[self.thread_id]["turns"]), 3)

    def test_closed_threads_keep_queue_paused_and_reject_new_messages(self):
        self.sessions.queue.enqueue(self.thread_id, {"message": "Retained"})
        self.sessions.close_work_thread(self.thread_id)
        self.assertEqual(self.pending()[0]["state"], "paused")
        with self.assertRaisesRegex(CoordinationError, "Reopen"):
            self.sessions.queue.enqueue(self.thread_id, {"message": "Cannot send"})
        with self.assertRaisesRegex(CoordinationError, "Reopen"):
            self.sessions.queue.change(self.thread_id, {"action": "resume"})

    def test_pause_during_dispatch_read_prevents_start(self):
        self.sessions.queue.enqueue(self.thread_id, {"message": "Do not start after Stop"})
        read = self.sessions.read

        def pause_when_idle(thread_id):
            result = read(thread_id)
            if not result["running"]:
                self.sessions.queue.pause(thread_id, "Stop raced with dispatch")
            return result

        self.sessions.read = pause_when_idle
        self.finish()
        # Calling the dispatcher also synchronizes with the background attempt.
        self.sessions.queue._dispatch(self.thread_id)
        self.assertEqual(self.pending()[0]["state"], "paused")
        self.assertEqual(len(self.sessions.rpc.threads[self.thread_id]["turns"]), 1)

    def test_invalid_and_out_of_workspace_queues_are_rejected(self):
        with self.assertRaises(CoordinationError):
            self.sessions.queue.enqueue(self.thread_id, {"message": "  "})
        with self.assertRaises(CoordinationError):
            self.sessions.queue.change(self.thread_id, {"action": "unknown"})
        outside = self.root / "outside"
        outside.mkdir()
        isolated = BrowserSessions(self.store, str(outside), rpc_factory=FakeCodex)
        self.addCleanup(isolated.close)
        for operation in (lambda: isolated.queue.list(self.thread_id),
                          lambda: isolated.queue.enqueue(self.thread_id, {"message": "No"}),
                          lambda: isolated.queue.change(self.thread_id, {"action": "resume"})):
            with self.assertRaises(CoordinationError):
                operation()
