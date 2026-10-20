from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_browser_providers import BrowserSessions, CoordinationStore, CoordinationError, FakeCodex, claude_factory


class ClaudeSteeringTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = CoordinationStore(self.root / "state.sqlite3")
        self.sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex, claude_factory=claude_factory)
        self.addCleanup(self.sessions.close)
        self.thread = self.sessions.create({"client": "claude"})["session"]["thread_id"]
        self.sessions.send(self.thread, {"message": "Original task"})
        self.connection = self.sessions.claude.connections[self.thread]
        self.turn_id = self.sessions.active[self.thread]

    def steer(self, message="Use the smaller change"):
        return self.sessions.send(self.thread, {"message": message, "expectedTurnId": self.turn_id})

    def result(self, **values):
        self.connection.callback({"type": "result", "subtype": "success", **values})

    def test_steering_uses_same_process_and_turn_and_survives_reload(self):
        first = self.connection.writes[-1]["uuid"]
        self.assertEqual(self.steer(), {"turnId": self.turn_id})
        last = self.connection.writes[-1]
        self.assertIs(self.sessions.claude.connections[self.thread], self.connection)
        self.assertFalse(self.connection.closed)
        self.assertEqual(last["message"]["content"], [{"type": "text", "text": "Use the smaller change"}])
        self.assertEqual(last["session_id"], self.thread)
        self.assertNotEqual(first, last["uuid"])
        self.assertEqual(self.sessions.read(self.thread)["queuedMessages"], [])
        self.result(user_message_uuids=[first, last["uuid"]])
        self.sessions.close()
        resumed = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex, claude_factory=claude_factory)
        self.addCleanup(resumed.close)
        read = resumed.read(self.thread)
        self.assertFalse(read["running"])
        self.assertEqual(len(read["thread"]["turns"]), 1)
        users = [i for i in read["thread"]["turns"][0]["items"] if i["type"] == "userMessage"]
        self.assertEqual([i["content"][0]["text"] for i in users], ["Original task", "Use the smaller change"])

    def test_earlier_result_cannot_finish_pending_steering_or_dispatch_queue(self):
        first = self.connection.writes[-1]["uuid"]
        self.steer()
        second = self.connection.writes[-1]["uuid"]
        self.steer("Also preserve history")
        latest = self.connection.writes[-1]["uuid"]
        self.sessions.queue.enqueue(self.thread, {"message": "Next task"})
        for prompt_id in (first, second, "background-prompt"):
            self.result(user_message_uuid=prompt_id)
            self.assertEqual(self.sessions.active[self.thread], self.turn_id)
            self.assertEqual(len(self.connection.writes), 3)
        self.result(user_message_uuids=[second, latest])
        self.assertTrue(self.sessions.persistence.flush())
        turns = self.sessions.read(self.thread)["thread"]["turns"]
        self.assertEqual(turns[0]["status"], "completed")

    def test_changed_or_finished_turn_rejects_steering_without_writing(self):
        rpc = self.sessions.claude
        for expected in ("old-turn", None):
            with self.assertRaisesRegex(CoordinationError, "changed or finished"):
                rpc.request("turn/steer", {"threadId": self.thread, "expectedTurnId": expected,
                                          "input": [{"type": "text", "text": "Correction"}]})
        self.sessions.interrupt(self.thread)
        with self.assertRaisesRegex(CoordinationError, "changed or finished"):
            rpc.request("turn/steer", {"threadId": self.thread, "expectedTurnId": self.turn_id, "input": []})
        self.assertEqual(len(self.connection.writes), 1)

    def test_failed_write_does_not_add_a_phantom_message(self):
        with patch.object(self.connection, "write", side_effect=CoordinationError("Disconnected")):
            with self.assertRaisesRegex(CoordinationError, "Disconnected"):
                self.steer()
        self.assertEqual(len(self.sessions.claude.turns[self.thread]["items"]), 1)
        self.result(user_message_uuid=self.connection.writes[0]["uuid"])
        self.assertFalse(self.sessions.read(self.thread)["running"])

    def test_images_use_the_same_encoding_as_initial_messages(self):
        self.sessions.claude.request("turn/steer", {"threadId": self.thread, "expectedTurnId": self.turn_id,
            "input": [{"type": "text", "text": "Use this reference"},
                      {"type": "image", "url": "data:image/png;base64,aGVsbG8="}]})
        self.assertEqual(self.connection.writes[-1]["message"]["content"][-1], {
            "type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "aGVsbG8="}})

    def test_explicit_queue_waits_for_turn_completion(self):
        self.sessions.submit(self.thread, {"message": "Next task"}, queued=True)
        self.assertEqual(len(self.connection.writes), 1)
        self.assertEqual(self.sessions.read(self.thread)["queuedMessages"][0]["message"], "Next task")
