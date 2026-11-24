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
