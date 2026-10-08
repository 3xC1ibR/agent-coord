from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from test_codex_app_server import FakeCodex, BrowserSessions, CoordinationStore, CoordinationError
from agent_coord.claude_code import ClaudeRPC


class FakeConnection:
    def __init__(self, store, callback, options):
        self.callback, self.options = callback, options
        self.writes = []
        self.closed = False
        self.process = self

    def poll(self):
        return 0 if self.closed else None

    def control(self, subtype):
        return {"models": [{"value": "default", "supportedEffortLevels": ["low", "high"]},
                            {"value": "sonnet", "supportedEffortLevels": ["low", "high"]}]}

    def write(self, message):
        self.writes.append(message)

    def close(self):
        self.closed = True


def claude_factory(store, callback):
    return ClaudeRPC(store, callback, connection_factory=FakeConnection)


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = CoordinationStore(self.root / "state.sqlite3")
        self.sessions = self.make_sessions()

    def make_sessions(self):
        sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex, claude_factory=claude_factory)
        self.addCleanup(sessions.close)
        return sessions

    def create(self, **values):
        return self.sessions.create({"client": "claude", **values})["session"]["thread_id"]

    def emit(self, thread_id, message):
        self.sessions.claude.connections[thread_id].callback(message)

    def start(self, thread_id):
        self.sessions.send(thread_id, {"message": "Hello"})
        self.emit(thread_id, {"type": "system", "subtype": "init"})

    def finish(self, thread_id):
        self.emit(thread_id, {"type": "result", "subtype": "success"})

    def test_provider_selection_and_existing_codex_default(self):
        claude = self.create()
        codex = self.sessions.create({})["session"]["thread_id"]
        self.assertEqual(self.sessions._record(claude)["client"], "claude")
        self.assertEqual(self.store.get_session(claude)["client"], "claude")
        self.assertEqual(self.sessions._record(codex)["client"], "codex")
        self.assertEqual(self.sessions.models("claude")[0]["model"], "default")
        for invalid in ("other", [], None):
            with self.assertRaises(CoordinationError):
                self.sessions.create({"client": invalid})

    def test_streaming_tools_and_durable_resume(self):
        thread = self.create()
        self.start(thread)
        for event in [
            {"type": "message_start"},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hello back"}},
            {"type": "content_block_stop", "index": 0},
        ]:
            self.emit(thread, {"type": "stream_event", "event": event})
        self.emit(thread, {"type": "assistant", "message": {"content": [{"type": "text", "text": "Hello back"}]}})
        self.emit(thread, {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "tool-1", "name": "Read", "input": {"file_path": "x"}}]}})
        self.emit(thread, {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "tool-1", "content": "contents"}]}})
        self.finish(thread)
        read = self.sessions.read(thread)
        items = read["thread"]["turns"][0]["items"]
        self.assertEqual([i["text"] for i in items if i["type"] == "agentMessage"], ["Hello back"])
        self.assertEqual(items[-1]["result"], "contents")
        self.assertFalse(read["running"])
        self.sessions.close()
        resumed = self.make_sessions()
        read = resumed.read(thread)
        self.assertEqual(read["thread"]["turns"][0]["items"], items)
        self.assertFalse(resumed.claude.connections)  # Viewing saved history is offline.
        resumed.send(thread, {"message": "Continue"})
        self.assertTrue(resumed.claude.connections[thread].options["resume"])
        self.assertFalse(resumed.rpc.calls)

    def test_approvals_questions_and_cancellation(self):
        thread = self.create()
        self.start(thread)
        self.emit(thread, {"type": "control_request", "request_id": "approve", "request": {
            "subtype": "can_use_tool", "tool_name": "Bash", "input": {"command": "pwd"}}})
        request = self.sessions.pending_requests(thread)[0]
        self.assertEqual(request["params"]["availableDecisions"], ["accept", "decline", "cancel"])
        other = self.create()
        with self.assertRaises(CoordinationError):
            self.sessions.answer(other, request["key"], {"decision": "accept"})
        self.sessions.answer(thread, request["key"], {"decision": "decline"})
        reply = self.sessions.claude.connections[thread].writes[-1]["response"]
        self.assertEqual(reply["request_id"], "approve")
        self.assertEqual(reply["response"]["behavior"], "deny")
        self.emit(thread, {"type": "control_request", "request_id": "question", "request": {
            "subtype": "can_use_tool", "tool_name": "AskUserQuestion", "input": {
                "questions": [{"question": "Which color?", "options": [{"label": "Blue"}]}]}}})
        request = self.sessions.pending_requests(thread)[0]
        self.sessions.answer(thread, request["key"], {"answers": {"0": {"answers": ["Blue"]}}})
        reply = self.sessions.claude.connections[thread].writes[-1]["response"]["response"]
        self.assertEqual(reply["updatedInput"]["answers"], {"Which color?": "Blue"})
        self.emit(thread, {"type": "control_request", "request_id": "cancelled", "request": {
            "subtype": "can_use_tool", "tool_name": "Write", "input": {}}})
        self.emit(thread, {"type": "control_cancel_request", "request_id": "cancelled"})
        self.assertEqual(self.sessions.pending_requests(thread), [])

    def test_per_block_assistant_frames_do_not_duplicate_streamed_text(self):
        thread = self.create()
        self.start(thread)
        for index, block in enumerate([{"type": "thinking", "thinking": "Consider"},
                                       {"type": "text", "text": "Writing now"},
                                       {"type": "tool_use", "id": "write", "name": "Write", "input": {}}]):
            self.emit(thread, {"type": "stream_event", "event": {
                "type": "content_block_start", "index": index, "content_block": block}})
            self.emit(thread, {"type": "stream_event", "event": {"type": "content_block_stop", "index": index}})
            self.emit(thread, {"type": "assistant", "message": {"content": [block]}})
        self.finish(thread)
        items = self.sessions.read(thread)["thread"]["turns"][-1]["items"]
        self.assertEqual([i["type"] for i in items], ["userMessage", "reasoning", "agentMessage", "mcpToolCall"])
        self.assertEqual(items[2]["text"], "Writing now")

    def test_stop_and_disconnect_are_isolated(self):
        first, second = self.create(), self.create()
        codex = self.sessions.create({})["session"]["thread_id"]
        self.start(first)
        self.start(second)
        self.sessions.send(codex, {"message": "Hi"})
        connection = self.sessions.claude.connections[first]
        self.sessions.interrupt(first)
        self.assertTrue(connection.closed)
        self.assertEqual(self.sessions.read(first)["thread"]["turns"][-1]["status"], "interrupted")
        self.assertTrue(self.sessions.read(second)["running"])
        self.sessions.rpc.close()
        self.assertIn(second, self.sessions.active)
        self.assertIn(second, self.sessions.loaded)
        self.emit(second, {"type": "disconnected", "error": "Lost connection"})
        self.assertNotIn(second, self.sessions.active)
        self.assertEqual(self.sessions.read(second)["thread"]["turns"][-1]["error"]["message"], "Lost connection")

    def test_identical_native_request_ids_remain_scoped_to_their_process(self):
        first, second = self.create(), self.create()
        for thread in (first, second):
            self.start(thread)
            self.emit(thread, {"type": "control_request", "request_id": "same-id", "request": {
                "subtype": "can_use_tool", "tool_name": "Write", "input": {"file_path": thread}}})
        request = self.sessions.pending_requests(first)[0]
        self.sessions.answer(first, request["key"], {"decision": "accept"})
        response = self.sessions.claude.connections[first].writes[-1]["response"]["response"]
        self.assertEqual(response["updatedInput"]["file_path"], first)
        self.assertEqual(len(self.sessions.pending_requests(second)), 1)

    def test_settings_apply_next_turn_without_silent_permission_bypass(self):
        thread = self.create()
        self.start(thread)
        self.assertFalse(self.sessions.claude.connections[thread].options["yolo"])
        self.finish(thread)
        self.sessions.send(thread, {"message": "/model sonnet high"})
        self.sessions.update(thread, {"yolo": True})
        self.sessions.send(thread, {"message": "Continue"})
        options = self.sessions.claude.connections[thread].options
        self.assertEqual((options["model"], options["effort"], options["yolo"]), ("sonnet", "high", True))
        with self.assertRaises(CoordinationError):
            self.sessions.send(thread, {"message": "steer", "expectedTurnId": self.sessions.active[thread]})
        self.assertTrue(self.sessions.read(thread)["running"])

    def test_close_reopen_and_failed_result(self):
        thread = self.create()
        self.start(thread)
        self.emit(thread, {"type": "result", "subtype": "error_during_execution", "is_error": True, "errors": ["Authentication failed"]})
        self.assertEqual(self.sessions.read(thread)["thread"]["turns"][-1]["status"], "failed")
        self.sessions.close_work_thread(thread)
        self.assertNotIn(thread, self.sessions.claude.connections)
        self.sessions.reopen_work_thread(thread)
        self.start(thread)
        self.assertTrue(self.sessions.claude.connections[thread].options["resume"])

    @unittest.skipUnless(shutil.which("node"), "Node required")
    def test_provider_browser_controls(self):
        result = subprocess.run(["node", "--test", str(Path(__file__).with_name("test_web_providers.js"))], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
