from __future__ import annotations

import copy
import unittest
from unittest.mock import patch

import test_muse
from agent_coord.store import CoordinationError


class MuseSteeringTests(unittest.TestCase):
    def setUp(self):
        self.h = test_muse.MuseTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.thread = self.h.create()
        self.connection = self.h.start(self.thread)
        self.turn_id = self.h.sessions.active[self.thread]
        original = self.connection.request

        def request(method, params, **kwargs):
            if method != "turn/steer":
                return original(method, params, **kwargs)
            self.connection.calls.append((method, copy.deepcopy(params)))
            return {"commandId": params["commandId"], "turnId": params["expectedTurnId"], "status": "accepted"}

        self.connection.request = request

    def send(self, text="Use the smaller change", **body):
        return self.h.sessions.send(self.thread, {"message": text, "expectedTurnId": self.turn_id, **body})

    def users(self):
        return [item for item in self.h.sessions.read(self.thread)["thread"]["turns"][-1]["items"]
                if item["type"] == "userMessage"]

    def test_steer_keeps_native_process_and_turn_and_persists_both_prompts(self):
        self.assertEqual(self.send(), {"turnId": self.turn_id})
        method, params = self.connection.calls[-1]
        self.assertEqual(method, "turn/steer")
        self.assertEqual(params["sessionId"], self.connection.native_id)
        self.assertEqual(params["expectedTurnId"], self.turn_id)
        self.assertEqual(test_muse.uuid.UUID(params["commandId"]).version, 7)
        self.assertEqual(params["input"], [{"type": "text", "text": "Use the smaller change"}])
        self.assertEqual(self.h.sessions.active[self.thread], self.turn_id)
        self.assertFalse(self.connection.closed)
        self.assertEqual(len(self.h.connections), 1)
        self.assertEqual(len(self.users()), 2)
        self.h.complete(self.thread)
        self.assertNotIn(self.thread, self.h.sessions.active)
        self.assertTrue(self.h.sessions.persistence.flush(key=self.thread))
        reopened = self.h.make_sessions()
        self.assertEqual(reopened.read(self.thread)["thread"]["turns"][-1]["items"][1]["content"],
                         [{"type": "text", "text": "Use the smaller change"}])

    def test_steer_preserves_images_with_text(self):
        from test_browser_images import image
        reference = image()
        self.send(images=[reference])
        native = self.connection.calls[-1][1]["input"]
        self.assertEqual(native[1], {"type": "image", "mediaType": "image/png", "base64Data": reference["url"].split(",", 1)[1]})
        self.assertEqual(self.users()[-1]["content"][1], {"type": "image", "url": reference["url"]})

    def test_stale_or_missing_expected_turn_never_reaches_native_process(self):
        before = len(self.connection.calls)
        for expected in ("old-turn", None):
            with self.subTest(expected=expected), self.assertRaisesRegex(CoordinationError, "changed or finished"):
                self.h.sessions.muse.request("turn/steer", {"threadId": self.thread,
                    "expectedTurnId": expected, "input": [{"type": "text", "text": "late"}]})
        self.assertEqual(len(self.connection.calls), before)
        self.assertEqual(len(self.users()), 1)

    def test_native_rejection_does_not_record_delivery_or_stop_running_turn(self):
        with patch.object(self.connection, "request", side_effect=CoordinationError("No active turn")):
            with self.assertRaisesRegex(CoordinationError, "No active turn"):
                self.send()
        self.assertEqual(len(self.users()), 1)
        self.assertEqual(self.h.sessions.active[self.thread], self.turn_id)
        self.assertFalse(self.connection.closed)

    def test_unconfirmed_ack_does_not_record_delivery(self):
        for reply in ({}, {"commandId": "wrong", "turnId": self.turn_id, "status": "accepted"}):
            with self.subTest(reply=reply), patch.object(self.connection, "request", return_value=reply):
                with self.assertRaisesRegex(CoordinationError, "did not confirm"):
                    self.send()
        self.assertEqual(len(self.users()), 1)

    def test_explicit_queue_waits_for_completion(self):
        self.h.sessions.submit(self.thread, {"message": "Do this afterward"}, queued=True)
        self.assertFalse(any(method == "turn/steer" for method, _ in self.connection.calls))
        self.assertEqual(len(self.users()), 1)
        self.assertEqual(len(self.h.sessions.queue.list(self.thread)), 1)

    def test_native_user_message_echo_is_not_duplicated(self):
        self.send()
        self.h.event(self.thread, "item/completed", item={"itemId": "native-user", "kind": "userMessage",
            "turnId": self.turn_id, "status": "completed", "revision": 1, "text": "Use the smaller change"})
        self.assertEqual(len(self.users()), 2)


if __name__ == "__main__":
    unittest.main()
