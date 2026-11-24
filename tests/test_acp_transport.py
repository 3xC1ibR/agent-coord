"""Retained ACP correlation, native requests and stream routing."""
import copy
import json
import unittest
from unittest.mock import patch

import test_coordinator_projection as fixtures
from agent_coord.acp import ACPConnection
from agent_coord.coordinator_projection import CoordinatorProjection
from agent_coord import acp_state


class ACPProjectionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.sessions = self.fixture.sessions
        self.projection = self.fixture.projection
        self.store = self.fixture.store
        self.identity = self.fixture.identity
        self.turn_id = self.fixture.turn_id

    def bind(self, provider="kimi"):
        adapter = getattr(self.sessions, provider)
        adapter.native[self.identity] = "native-session"
        adapter.turns[self.identity] = {"id": self.turn_id, "status": "inProgress", "items": []}
        adapter.options[self.identity] = {"cwd": self.fixture.temporary.name}
        self.sessions.loaded_clients[self.identity] = provider
        connection = acp_state.attach(self.projection, provider, self.identity, provider, 981, {})
        return adapter, connection

    def event(self, sequence, message, stream="stdout", channel="kimi"):
        return {"sequence": sequence, "channel": channel, "stream": stream,
                "data": json.dumps(message) if stream == "stdout" else message}

    def test_stderr_and_raw_stdout_reach_handlers_and_replay_once(self):
        adapter, connection = self.bind()
        self.projection.project(self.event(1, "diagnostic", "stderr"))
        self.projection.project(self.event(1, "diagnostic", "stderr"))
        self.assertEqual(list(connection.errors), ["diagnostic"])
        consumed = []
        self.projection.channels["kimi"]["stdout_callback"] = lambda line: consumed.append(line) or True
        self.projection.project(self.event(2, {"method": "ignored"}))
        self.assertEqual(len(consumed), 1)

    def test_native_prompt_correlation_restores_without_future_and_finishes_once(self):
        adapter, connection = self.bind()
        connection.submit("session/prompt", {"sessionId": "native-session", "prompt": []})
        request_id = next(iter(connection.bindings))
        self.projection.checkpoint()
        restored = CoordinatorProjection(self.sessions, self.fixture.client)
        self.store.provider_projection = self.sessions.projection = restored
        restored.load()
        self.assertTrue(restored.owns_provider(self.identity, "kimi"))
        attached = self.sessions.kimi.connections[self.identity]
        self.assertEqual(attached.process.pid, 981)
        self.assertFalse(attached.pending)
        self.assertEqual(attached.bindings[request_id]["turn_id"], self.turn_id)
        update = {"method": "session/update", "params": {"sessionId": "native-session", "update": {
            "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "retained answer"}}}}
        with patch.object(adapter, "_capture_usage"):
            restored.project(self.event(1, update))
            restored.project(self.event(2, {"id": request_id, "result": {"stopReason": "end_turn"}}))
            restored.project(self.event(2, {"id": request_id, "result": {"stopReason": "end_turn"}}))
        self.assertNotIn(self.identity, adapter.turns)
        turns = self.sessions.histories[self.identity]["turns"]
        self.assertEqual(sum(t["id"] == self.turn_id for t in turns), 1)
        self.assertEqual(turns[-1]["status"], "completed")
        self.assertEqual(turns[-1]["items"][-1]["text"], "retained answer")

    def test_prompt_response_rollback_retains_binding_and_future(self):
        adapter, connection = self.bind()
        future = connection.submit("session/prompt", {"sessionId": "native-session", "prompt": []})
        request_id = next(iter(connection.bindings))
        adapter.turns[self.identity]["items"] = [{"id": "answer", "type": "agentMessage", "text": "answer"}]
        with patch.object(adapter, "_capture_usage"), patch.object(self.projection, "_save", side_effect=RuntimeError("commit failure")):
            with self.assertRaisesRegex(RuntimeError, "commit failure"):
                self.projection.project(self.event(1, {"id": request_id, "result": {"stopReason": "end_turn"}}))
        self.assertIn(request_id, connection.bindings)
        self.assertFalse(future.done())
        self.assertIn(self.identity, adapter.turns)
        with patch.object(adapter, "_capture_usage"):
            self.projection.project(self.event(1, {"id": request_id, "result": {"stopReason": "end_turn"}}))
        self.assertTrue(future.done())

    def test_approval_mapping_survives_restart(self):
        adapter, connection = self.bind()
        permission = {"id": 7, "method": "session/request_permission", "params": {"sessionId": "native-session",
            "toolCall": {"toolCallId": "tool-1", "title": "Read file"},
            "options": [{"optionId": "allow", "kind": "allow_once", "name": "Allow"}]}}
        self.projection.project(self.event(1, permission))
        before = copy.deepcopy(adapter.requests)
        restored = CoordinatorProjection(self.sessions, self.fixture.client)
        self.store.provider_projection = self.sessions.projection = restored
        restored.load()
        self.assertEqual(adapter.requests, before)
        key = next(iter(before))
        adapter.write({"id": key, "result": {"decision": "accept"}})
        written = [v for action, v in self.fixture.client.commands if action == "command" and v["operation"] == "write"]
        self.assertEqual(json.loads(written[-1]["value"]["data"])["id"], 7)

    def test_handover_does_not_close_acp_or_clear_active_work(self):
        adapter, connection = self.bind()
        self.sessions.transport_handover = True
        adapter.close()
        self.assertIn(self.identity, adapter.turns)
        self.assertFalse(connection.closed)
        self.assertTrue(self.projection.owns_provider(self.identity, "kimi"))
        self.sessions.transport_handover = False

    def test_unbrokered_process_is_never_classified_as_owned(self):
        adapter, connection = self.bind()
        self.assertTrue(self.projection.owns_provider(self.identity, "kimi"))
        connection.process = copy.copy(connection.process)
        self.assertFalse(self.projection.owns_provider(self.identity, "kimi"))

    def test_native_replay_merges_missing_turns_without_duplicate_live_completion(self):
        adapter, connection = self.bind()
        adapter.turns.clear()
        self.sessions.active.clear()
        self.sessions.histories[self.identity] = {"id": self.identity, "turns": []}
        for _ in range(2):
            acp_state.begin_replay(adapter, self.identity)
            for kind, text in (("user_message_chunk", "terminal question"), ("agent_message_chunk", "terminal answer")):
                acp_state.replay_update(adapter, self.identity, {"sessionUpdate": kind,
                    "content": {"type": "text", "text": text}})
            acp_state.finish_replay(adapter, self.identity)
        turns = self.sessions.histories[self.identity]["turns"]
        self.assertEqual(len(turns), 1)
        self.assertEqual([item["type"] for item in turns[0]["items"]], ["userMessage", "agentMessage"])
        self.assertNotIn(self.identity, self.sessions.active)

    def test_muse_gap_recovers_missing_completed_items_and_background_workflow(self):
        adapter, connection = self.bind("muse")
        adapter.ready.add(self.identity)
        adapter.items[self.identity] = {}
        adapter.seen[self.identity] = set()
        gap = {"method": "view/gap", "params": {"sessionId": "native-session", "viewCursor": "gap-1"}}
        self.projection.project(self.event(1, gap, channel="muse"))
        request_id = next(iter(connection.bindings))
        self.assertEqual(connection.bindings[request_id]["method"], "session/read")
        response = {"session": {"sessionId": "native-session", "turns": [{"turnId": self.turn_id,
            "terminal": "completed", "items": [
                {"itemId": "answer", "turnId": self.turn_id, "kind": "agentMessage", "text": "recovered", "revision": 1, "status": "completed"},
                {"itemId": "workflow", "turnId": self.turn_id, "kind": "workflow", "revision": 1, "status": "inProgress", "workflowRunId": "work-1"}]}]}}
        self.projection.project(self.event(2, {"id": request_id, "result": response}, channel="muse"))
        self.assertNotIn(self.identity, adapter.turns)
        self.assertTrue(adapter.background_active(self.identity))
        self.assertEqual(adapter.completed[self.identity]["items"][0]["text"], "recovered")
        self.projection.project(self.event(2, {"id": request_id, "result": response}, channel="muse"))
        self.assertEqual(len(adapter.completed[self.identity]["items"]), 2)

    def test_recorded_approval_answer_is_not_reissued_after_restore(self):
        adapter, connection = self.bind()
        adapter.requests["kimi:pending"] = {"thread": self.identity, "id": 77}
        self.sessions.requests["browser:pending"] = {"id": "kimi:pending", "params": {"threadId": self.identity}}
        connection.write({"id": 77, "result": {"outcome": {"outcome": "selected", "optionId": "allow"}}})
        restored = CoordinatorProjection(self.sessions, self.fixture.client)
        self.store.provider_projection = self.sessions.projection = restored
        restored.load()
        self.assertNotIn("kimi:pending", adapter.requests)
        self.assertNotIn("browser:pending", self.sessions.requests)
        writes_before = len(self.fixture.client.commands)
        with self.assertRaisesRegex(Exception, "recorded answer"):
            adapter.connections[self.identity].write({"id": 77, "result": {"outcome": {"outcome": "selected", "optionId": "allow"}}})
        self.assertEqual(len(self.fixture.client.commands), writes_before)

    def test_muse_decision_receipt_settles_restored_request_without_waiter(self):
        adapter, connection = self.bind("muse")
        key = "muse:pending"
        adapter.requests[key] = {"thread": self.identity, "native_id": "approval-1"}
        self.sessions.requests[key] = {"id": key, "params": {"threadId": self.identity}}
        connection.submit("approval/decide", {"commandId": "decision-1", "approvalId": "approval-1",
            "sessionId": "native-session", "requirementId": {"sourceIndex": 1}, "choiceId": "allow"})
        request_id = next(iter(connection.bindings))
        restored = CoordinatorProjection(self.sessions, self.fixture.client)
        self.store.provider_projection = self.sessions.projection = restored
        restored.load()
        restored.project(self.event(1, {"id": request_id, "result": {"terminal": True}}, channel="muse"))
        self.assertNotIn(key, adapter.requests)
        self.assertNotIn(key, self.sessions.requests)
        self.assertIn((self.identity, "approval-1"), adapter.settled)

    def test_muse_answer_does_not_wait_under_browser_lock(self):
        adapter, connection = self.bind("muse")
        key = "muse:pending"
        adapter.requests[key] = {"thread": self.identity, "native_id": "approval-1", "question": False,
            "choices": {"accept": "allow"}, "request": {"currentRequirementId": {"sourceIndex": 1}}}
        with self.sessions.lock:
            adapter.write({"id": key, "result": {"decision": "accept"}})
        request_id = next(iter(connection.bindings))
        self.assertEqual(connection.bindings[request_id]["method"], "approval/decide")
        self.projection.project(self.event(1, {"id": request_id, "result": {"terminal": True}}, channel="muse"))
        self.assertNotIn(key, adapter.requests)

    def test_muse_steering_commits_display_before_waking_waiter(self):
        from concurrent.futures import ThreadPoolExecutor
        import time
        adapter, connection = self.bind("muse")
        with ThreadPoolExecutor(max_workers=1) as worker:
            future = worker.submit(adapter.request, "turn/steer", {"threadId": self.identity,
                "expectedTurnId": self.turn_id, "input": [{"type": "text", "text": "steered input"}]})
            deadline = time.monotonic() + 2
            while not connection.bindings and time.monotonic() < deadline:
                time.sleep(.005)
            request_id = next(iter(connection.bindings))
            binding = connection.bindings[request_id]
            self.projection.project(self.event(1, {"id": request_id, "result": {
                "commandId": binding["command_id"], "turnId": self.turn_id, "status": "accepted"}}, channel="muse"))
            self.assertEqual(future.result(timeout=2), {"turnId": self.turn_id})
        self.assertEqual(adapter.turns[self.identity]["items"][-1]["content"][0]["text"], "steered input")

    def test_acp_text_and_reasoning_emit_deltas_without_copying_growing_items(self):
        for provider in ("antigravity", "kimi"):
            with self.subTest(provider=provider):
                adapter, connection = self.bind(provider)
                self.projection.stream_delta_protocol = 1
                events = []
                adapter.on_event = events.append
                for kind, expected_method in (("agent_message_chunk", "item/agentMessage/delta"),
                                              ("agent_thought_chunk", "item/reasoning/summaryTextDelta")):
                    message = {"method": "session/update", "params": {"sessionId": "native-session", "update": {
                        "sessionUpdate": kind, "content": {"type": "text", "text": "x"}}}}
                    connection._message(message)
                    with patch.object(copy, "deepcopy", side_effect=AssertionError("Growing item copied")):
                        for _ in range(100):
                            connection._message(message)
                    deltas = [event for event in events if event["method"] == expected_method]
                    self.assertEqual(len(deltas), 101)
                    self.assertTrue(all(event["params"]["delta"] == "x" for event in deltas))
                self.assertEqual(sum(event["method"] == "item/started" for event in events), 2)
                self.assertEqual(adapter.turns[self.identity]["items"][-1]["summary"], ["x" * 101])

    def test_tool_delta_requires_an_unchanged_text_prefix(self):
        adapter, connection = self.bind()
        self.projection.stream_delta_protocol = 1
        events = []
        adapter.on_event = events.append
        adapter.turns[self.identity]["items"] = [{"id": "tool", "type": "mcpToolCall", "result": "prefix"}]
        turn = adapter.turns[self.identity]
        with patch.object(copy, "deepcopy", side_effect=AssertionError("Growing item copied")):
            self.assertTrue(acp_state.append_tool_delta(adapter, self.identity, turn,
                {"sessionUpdate": "tool_call_update", "toolCallId": "tool", "rawOutput": "prefix suffix"}))
        self.assertEqual(events[-1]["params"]["delta"], " suffix")
        self.assertFalse(acp_state.append_tool_delta(adapter, self.identity, turn,
            {"sessionUpdate": "tool_call_update", "toolCallId": "tool", "rawOutput": "replaced"}))

    def test_muse_text_output_and_reasoning_native_deltas_avoid_item_snapshots(self):
        adapter, connection = self.bind("muse")
        self.projection.stream_delta_protocol = 1
        adapter.items[self.identity] = {}
        events = []
        adapter.on_event = events.append
        cases = [("agentMessage", "text", "item/agentMessage/delta"),
                 ("toolCall", "output", "item/mcpToolCall/outputDelta"),
                 ("reasoning", "summary.0", "item/reasoning/summaryTextDelta")]
        for kind, field, normalized in cases:
            native = {"itemId": kind, "turnId": self.turn_id, "kind": kind, "revision": 1, "status": "inProgress"}
            if kind == "reasoning":
                native["summary"] = [""]
            adapter._update_item(self.identity, "item/started", {"turnId": self.turn_id, "item": native})
            with patch.object(adapter, "_item", side_effect=AssertionError("Growing item rendered")):
                adapter._update_item(self.identity, "item/delta", {"turnId": self.turn_id, "itemId": kind, "field": field, "delta": "x"})
            self.assertEqual(events[-1]["method"], normalized)
            self.assertEqual(events[-1]["params"]["delta"], "x")


class ACPCancellationTests(unittest.TestCase):
    def test_cancel_waits_for_native_terminal_reply_before_close(self):
        import os
        import sys
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            program = '''import json, sys
pending = None
for line in sys.stdin:
    message = json.loads(line)
    if message.get("method") == "session/prompt":
        pending = message["id"]
    elif message.get("method") == "session/cancel":
        print(json.dumps({"id": pending, "result": {"stopReason": "cancelled"}}), flush=True)
'''
            connection = ACPConnection(lambda _: None, command=[sys.executable, "-u", "-c", program], cwd=directory, env=dict(os.environ))
            try:
                future = connection.submit("session/prompt", {"sessionId": "native"})
                connection.cancel("native")
                self.assertEqual(future.result(timeout=.1), {"stopReason": "cancelled"})
                self.assertIsNone(connection.process.poll())
            finally:
                connection.close()
