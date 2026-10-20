from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import zipfile
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import patch

from test_codex_app_server import FakeCodex, BrowserSessions, CoordinationStore, CoordinationError
from agent_coord.antigravity import AntigravityConnection, AntigravityRPC, AUTH_PREFIX
from agent_coord.antigravity_setup import install_runtime, provider_directory, resolve_runtime
from agent_coord.acp import ACPError


CONFIG = [{"id": "model", "category": "model", "type": "select", "currentValue": "gemini-test",
           "options": [{"value": "gemini-test", "name": "Gemini Test"}, {"name": "Other", "options": [{"value": "claude-test", "name": "Claude Test"}]}]},
          {"id": "mode", "category": "mode", "type": "select", "currentValue": "default",
           "options": [{"value": "default"}, {"value": "yolo"}]}]


class FakeACP:
    def __init__(self, store, callback, options):
        self.callback, self.options = callback, options
        self.process = self
        self.closed = False
        self.calls, self.writes = [], []
        self.future = None

    def poll(self):
        return 0 if self.closed else None

    def initialize(self):
        return {"agentCapabilities": {"loadSession": True, "sessionCapabilities": {"resume": {}}}}

    def request(self, method, params, **kwargs):
        self.calls.append((method, params))
        if method in {"session/new", "session/resume", "session/load"}:
            return {"sessionId": params.get("sessionId", "native-" + str(len(self.calls))), "configOptions": CONFIG}
        return {}

    def submit(self, method, params):
        self.calls.append((method, params))
        self.future = Future()
        return self.future

    def write(self, message):
        self.writes.append(message)

    def close(self):
        self.closed = True
        if self.future and not self.future.done():
            self.future.set_exception(CoordinationError("Closed"))


class AntigravityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = CoordinationStore(self.root / "state.sqlite3")
        self.sessions = self.make_sessions()

    def make_sessions(self):
        sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex,
            antigravity_factory=lambda s, event: AntigravityRPC(s, event, connection_factory=FakeACP))
        self.addCleanup(sessions.close)
        return sessions

    def create(self, **settings):
        return self.sessions.create({"client": "antigravity", **settings})["session"]["thread_id"]

    def start(self, thread):
        self.sessions.send(thread, {"message": "Hello"})
        return self.sessions.antigravity.connections[thread]

    def update(self, thread, **update):
        self.sessions.antigravity.connections[thread].callback({"method": "session/update", "params": {
            "sessionId": self.sessions.antigravity.native[thread], "update": update}})

    def permission(self, thread, *, question=False, request_id="same-id"):
        connection = self.sessions.antigravity.connections[thread]
        connection.callback({"id": request_id, "method": "session/request_permission", "params": {
            "sessionId": self.sessions.antigravity.native[thread],
            "toolCall": {"toolCallId": "interaction_1" if question else "tool-1", "title": "Which option?" if question else "Run command?", "rawInput": {"command": "pwd"}},
            "options": [{"optionId": "first", "kind": "allow_once", "name": "First"},
                        {"optionId": "second", "kind": "reject_once", "name": "Second"}]}})
        return self.sessions.pending_requests(thread)[0]

    def test_models_and_provider_registration(self):
        thread = self.create(model="gemini-test")
        self.assertEqual(self.store.get_session(thread)["client"], "antigravity")
        self.assertEqual([m["model"] for m in self.sessions.models("antigravity")], ["gemini-test", "claude-test"])
        self.assertEqual(self.sessions.models("antigravity")[0]["supportedReasoningEfforts"], [])
        self.assertFalse(self.sessions.rpc.calls)
        with self.assertRaises(CoordinationError):
            self.create(model="gemini-test", effort="high")

    def test_app_cli_supports_antigravity_without_advertising_terminal_delegation(self):
        from agent_coord.cli import _parser
        parser = _parser()
        self.assertEqual(parser.parse_args(["thread", "models", "--client", "antigravity"]).client, "antigravity")
        self.assertEqual(parser.parse_args(["antigravity", "status"]).antigravity_command, "status")

    def test_images_and_coordination_context_reach_the_native_prompt(self):
        thread = self.create()
        provider = self.sessions.antigravity
        provider.request("turn/start", {"threadId": thread, "cwd": str(self.root), "input": [
            {"type": "text", "text": "Describe this"}, {"type": "image", "url": "data:image/png;base64,YQ=="}]})
        prompt = provider.connections[thread].calls[-1][1]["prompt"]
        self.assertIn(thread, prompt[0]["text"])
        self.assertIn("AGENTS.md", prompt[0]["text"])
        self.assertIn("skills/agent-coordination/SKILL.md", prompt[0]["text"])
        self.assertIn("never comma-join", prompt[0]["text"])
        self.assertEqual(prompt[-1], {"type": "image", "mimeType": "image/png", "data": "YQ=="})

    def test_streaming_tools_restart_and_native_resume(self):
        thread = self.create(model="gemini-test")
        connection = self.start(thread)
        self.update(thread, sessionUpdate="agent_thought_chunk", content={"type": "text", "text": "Thinking"})
        for text in ["Hello", " back"]:
            self.update(thread, sessionUpdate="agent_message_chunk", content={"type": "text", "text": text})
        self.update(thread, sessionUpdate="tool_call", toolCallId="read", title="Read file", rawInput={"path": "x"}, status="in_progress")
        self.update(thread, sessionUpdate="tool_call_update", toolCallId="read", rawOutput="contents", status="completed")
        connection.future.set_result({"stopReason": "end_turn"})
        before = self.sessions.read(thread)["thread"]["turns"]
        self.assertEqual([i["type"] for i in before[0]["items"]], ["userMessage", "reasoning", "agentMessage", "mcpToolCall"])
        self.assertEqual(before[0]["items"][2]["text"], "Hello back")
        self.assertEqual(before[0]["items"][3]["result"], "contents")
        native = self.sessions.antigravity.native[thread]
        self.sessions.close()
        self.sessions = self.make_sessions()
        self.assertEqual(self.sessions.read(thread)["thread"]["turns"], before)
        self.assertFalse(self.sessions.antigravity.connections)
        connection = self.start(thread)
        resume = next(p for m, p in connection.calls if m == "session/resume")
        self.assertEqual(resume["sessionId"], native)
        self.assertIn("skills/agent-coordination/SKILL.md", connection.calls[-1][1]["prompt"][0]["text"])
        selections = [p for m, p in connection.calls if m == "session/set_config_option"]
        self.assertIn({"sessionId": native, "configId": "model", "value": "gemini-test"}, selections)

    def test_approvals_are_scoped_and_only_offer_native_decisions(self):
        first, second = self.create(), self.create()
        one, two = self.start(first), self.start(second)
        approval = self.permission(first)
        self.permission(second)
        self.assertEqual(approval["params"]["availableDecisions"], ["accept", "decline", "cancel"])
        with self.assertRaises(CoordinationError):
            self.sessions.answer(second, approval["key"], {"decision": "accept"})
        with self.assertRaises(CoordinationError):
            self.sessions.answer(first, approval["key"], {"decision": "acceptForSession"})
        self.sessions.answer(first, approval["key"], {"decision": "decline"})
        self.assertEqual(one.writes[-1]["result"], {"outcome": {"outcome": "selected", "optionId": "second"}})
        self.assertFalse(two.writes)
        self.assertEqual(len(self.sessions.pending_requests(second)), 1)

    def test_context_outside_display_turn_keeps_consumption_unsupported(self):
        thread = self.create()
        connection = self.start(thread)
        connection.future.set_result({"stopReason": "end_turn"})
        self.update(thread, sessionUpdate="usage_update", used=13759, size=1000000)
        summary = self.store.usage.summary(thread)
        self.assertEqual(summary["context"], {"used_tokens": 13759, "limit_tokens": 1000000})
        self.assertEqual(summary["totals"], {})
        self.assertIn("unsupported", summary["note"])
        self.update(thread, sessionUpdate="usage_update", used=123, size=200000)
        self.assertEqual(self.store.usage.summary(thread)["context"], {"used_tokens": 123, "limit_tokens": 200000})

    def test_reconnect_display_guard_does_not_discard_context(self):
        thread = self.create()
        self.start(thread)
        self.sessions.antigravity.delivery_turns[thread]["reconnecting"] = True
        self.update(thread, sessionUpdate="usage_update", used=123, size=200000)
        self.assertEqual(self.store.usage.summary(thread)["context"]["used_tokens"], 123)

    def test_retired_process_context_and_telemetry_failure_are_isolated(self):
        thread = self.create()
        old = self.start(thread)
        self.update(thread, sessionUpdate="usage_update", used=50, size=100)
        self.sessions.interrupt(thread)
        self.start(thread)
        old.callback({"method": "session/update", "params": {
            "sessionId": self.sessions.antigravity.native[thread],
            "update": {"sessionUpdate": "usage_update", "used": 99, "size": 100}}})
        self.assertEqual(self.store.usage.summary(thread)["context"]["used_tokens"], 50)
        with patch.object(self.store.usage, "record_context", side_effect=RuntimeError("database failure")), \
                self.assertLogs("agent_coord.antigravity", level="WARNING"):
            self.update(thread, sessionUpdate="usage_update", used=90, size=100)
        self.assertIn(thread, self.sessions.antigravity.turns)

    def test_fixed_choice_questions_require_an_offered_option_even_in_yolo(self):
        thread = self.create(yolo=True)
        connection = self.start(thread)
        question = self.permission(thread, question=True)
        self.assertFalse(question["params"]["questions"][0]["allowCustomAnswer"])
        with self.assertRaises(CoordinationError):
            self.sessions.answer(thread, question["key"], {"answers": {"interaction_1": {"answers": ["custom"]}}})
        self.assertEqual(len(self.sessions.pending_requests(thread)), 1)
        self.sessions.answer(thread, question["key"], {"answers": {"interaction_1": {"answers": ["first"]}}})
        self.assertEqual(connection.writes[-1]["result"]["outcome"]["optionId"], "first")

    def test_stop_and_codex_disconnect_leave_other_providers_running(self):
        first, second = self.create(), self.create()
        one, two = self.start(first), self.start(second)
        self.permission(first)
        self.sessions.interrupt(first)
        self.assertTrue(one.closed)
        self.assertEqual(one.writes[-1]["method"], "session/cancel")
        self.assertEqual(self.sessions.read(first)["thread"]["turns"][-1]["status"], "interrupted")
        self.assertFalse(self.sessions.pending_requests(first))
        self.assertTrue(self.sessions.read(second)["running"])
        self.sessions.rpc.close()
        self.assertTrue(self.sessions.read(second)["running"])
        self.assertFalse(two.closed)
        self.assertFalse(self.start(first).closed)

    def test_permission_model_and_directory_changes_apply_on_next_turn(self):
        thread = self.create()
        connection = self.start(thread)
        self.assertIn("default", [p["value"] for m, p in connection.calls if m == "session/set_config_option"])
        connection.future.set_result({"stopReason": "end_turn"})
        self.sessions.update(thread, {"yolo": True, "model": "claude-test"})
        directory = self.root / "other"
        directory.mkdir()
        self.sessions.send(thread, {"message": "/cd " + str(directory)})
        next_connection = self.start(thread)
        self.assertTrue(connection.closed)
        self.assertEqual(next_connection.options["cwd"], str(directory))
        self.assertEqual(next_connection.options["model"], "claude-test")
        self.assertTrue(next_connection.options["yolo"])
        self.assertIn("yolo", [p["value"] for m, p in next_connection.calls if m == "session/set_config_option"])

    def test_failed_prompt_and_close_reopen_preserve_identity(self):
        thread = self.create()
        connection = self.start(thread)
        native = self.sessions.antigravity.native[thread]
        connection.future.set_exception(CoordinationError("Account quota exhausted"))
        read = self.sessions.read(thread)
        self.assertFalse(read["running"])
        self.assertIn("quota", read["thread"]["turns"][-1]["error"]["message"])
        self.sessions.close_work_thread(thread)
        self.sessions.reopen_work_thread(thread)
        connection = self.start(thread)
        self.assertEqual(self.sessions.antigravity.native[thread], native)
        with self.assertRaisesRegex(CoordinationError, "steering"):
            self.sessions.send(thread, {"message": "steer", "expectedTurnId": self.sessions.active[thread]})

    def test_internal_error_retires_live_process_and_next_send_resumes(self):
        thread = self.create(model="gemini-test", yolo=True)
        connection = self.start(thread)
        native = self.sessions.antigravity.native[thread]
        self.assertIsNone(connection.poll())
        connection.future.set_exception(ACPError("Antigravity: Internal error", -32603))
        self.assertTrue(connection.closed)
        self.assertNotIn(thread, self.sessions.antigravity.connections)
        self.assertEqual(self.sessions.read(thread)["thread"]["turns"][-1]["status"], "failed")

        resumed = self.start(thread)
        self.assertIsNot(resumed, connection)
        self.assertIn(("session/resume", {"cwd": str(self.root), "mcpServers": [], "sessionId": native}), resumed.calls)
        self.assertNotIn("session/new", [m for m, _ in resumed.calls])
        selections = [p for m, p in resumed.calls if m == "session/set_config_option"]
        self.assertIn({"sessionId": native, "configId": "model", "value": "gemini-test"}, selections)
        self.assertIn({"sessionId": native, "configId": "mode", "value": "yolo"}, selections)
        resumed.future.set_result({"stopReason": "end_turn"})
        self.assertIs(self.start(thread), resumed)  # Healthy processes remain reusable.

    def test_runtime_failure_text_is_failed_even_with_end_turn(self):
        diagnostics = [
            "Usage Limit Reached\n\nYou have reached your current quota for this period. Your limit will reset in 1 hour, 10 minutes.",
            "Agent connection was lost and could not be re-established: Failed to rebuild agent: received 1000 (OK); then sent 1000 (OK)",
        ]
        for diagnostic in diagnostics:
            with self.subTest(diagnostic=diagnostic):
                thread = self.create()
                connection = self.start(thread)
                native = self.sessions.antigravity.native[thread]
                for chunk in [diagnostic[:12], diagnostic[12:]]:
                    self.update(thread, sessionUpdate="agent_message_chunk", content={"type": "text", "text": chunk})
                connection.future.set_result({"stopReason": "end_turn"})
                turn = self.sessions.read(thread)["thread"]["turns"][-1]
                self.assertEqual(turn["status"], "failed")
                self.assertEqual(turn["error"]["message"], diagnostic)
                self.assertTrue(connection.closed)
                self.assertNotIn(thread, self.sessions.antigravity.connections)
                self.assertFalse(self.sessions.read(thread)["running"])
                resumed = self.start(thread)
                self.assertEqual(self.sessions.antigravity.native[thread], native)
                self.assertIn("session/resume", [m for m, _ in resumed.calls])

    def test_discussing_runtime_errors_is_not_a_failed_turn(self):
        thread = self.create()
        connection = self.start(thread)
        self.update(thread, sessionUpdate="agent_message_chunk", content={"type": "text", "text":
            'The message "Usage Limit Reached" means the provider quota is exhausted. Internal error can require a reconnect.'})
        connection.future.set_result({"stopReason": "end_turn"})
        self.assertEqual(self.sessions.read(thread)["thread"]["turns"][-1]["status"], "completed")
        self.assertFalse(connection.closed)

    def test_disconnect_retires_process_and_late_events_cannot_fail_retry(self):
        thread = self.create()
        old = self.start(thread)
        old.callback({"method": "disconnected", "params": {"error": "Lost transport"}})
        self.assertTrue(old.closed)
        resumed = self.start(thread)
        old.callback({"method": "disconnected", "params": {"error": "Late disconnect"}})
        old.callback({"method": "session/update", "params": {
            "sessionId": self.sessions.antigravity.native[thread], "update": {
                "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Stale output"}}}})
        self.assertTrue(self.sessions.read(thread)["running"])
        self.assertEqual(len(self.sessions.antigravity.turns[thread]["items"]), 1)
        resumed.future.set_result({"stopReason": "end_turn"})

    def test_prompt_failure_can_close_real_transport_from_reader_callback(self):
        script = self.root / "failing_acp.py"
        script.write_text('''import sys,json
for line in sys.stdin:
 m=json.loads(line)
 if m["method"]=="session/prompt":
  reply={"error":{"code":-32603,"message":"Internal error"}}
 else:
  result={"protocolVersion":2,"agentInfo":{"name":"antigravity-acp"},"agentCapabilities":{"loadSession":True}}
  if m["method"].startswith("session/"): result={"sessionId":"saved-native","configOptions":[]}
  reply={"result":result}
 print(json.dumps({"id":m["id"],**reply}),flush=True)
''')
        provider = self.sessions.antigravity
        provider.connection_factory = lambda store, callback, options: AntigravityConnection(
            store, callback, options, command=[sys.executable, "-u", str(script)])
        finished = threading.Event()
        original = provider.on_event

        def on_event(event):
            original(event)
            if event["method"] == "turn/completed":
                finished.set()

        provider.on_event = on_event
        thread = self.create()
        connection = provider.connections[thread]
        self.sessions.send(thread, {"message": "Hello"})
        self.assertTrue(finished.wait(5), "Transport cleanup blocked turn completion")
        self.assertTrue(connection.closed)
        self.assertIsNotNone(connection.process.poll())
        self.assertFalse(self.sessions.read(thread)["running"])
        self.assertEqual(self.sessions.read(thread)["thread"]["turns"][-1]["status"], "failed")

    def test_unknown_client_rpc_is_rejected_and_replay_is_ignored(self):
        thread = self.create()
        connection = self.sessions.antigravity.connections[thread]
        self.update(thread, sessionUpdate="agent_message_chunk", content={"type": "text", "text": "old message"})
        self.assertFalse(self.sessions.read(thread)["thread"]["turns"])
        connection.callback({"id": 15, "method": "fs/write_text_file", "params": {
            "sessionId": self.sessions.antigravity.native[thread], "path": str(self.root / "unexpected"), "content": "no"}})
        self.assertEqual(connection.writes[-1]["error"]["code"], -32601)
        self.assertFalse((self.root / "unexpected").exists())


class AntigravityDeliveryTests(unittest.TestCase):
    make_sessions = AntigravityTests.make_sessions
    create = AntigravityTests.create
    start = AntigravityTests.start
    update = AntigravityTests.update
    permission = AntigravityTests.permission

    def setUp(self):
        AntigravityTests.setUp(self)
        self.store.register(session_id="sender", client="codex", cwd=str(self.root))
        # Exercise the worker synchronously; lifecycle races are controlled below.
        self.worker_patch = patch.object(AntigravityRPC, "_start_delivery_worker")
        self.worker_patch.start()
        self.addCleanup(self.worker_patch.stop)

    def send_correction(self, thread, body="Use the revised interface"):
        return self.store.send_message(sender_session_id="sender", recipient_session_id=thread, body=body)["id"]

    def deliver(self, thread):
        state = self.sessions.antigravity.delivery_turns.get(thread)
        if state:
            state["next_check"] = 0
        self.sessions.antigravity.deliver_pending()

    def test_initial_prompt_delivers_without_manual_inbox(self):
        thread = self.create()
        ident = self.send_correction(thread)
        connection = self.start(thread)
        self.assertIn('Use the revised interface', connection.calls[-1][1]['prompt'][-1]['text'])
        self.assertIsNone(self.store.inbox(thread, mark_delivered=False)[0]['delivered_at'])
        self.update(thread, sessionUpdate="agent_message_chunk", content={"type": "text", "text": "Received"})
        receipt = self.sessions.antigravity.delivery.read('sender', ident)
        self.assertEqual(receipt['delivery']['state'], 'submitted')
        self.assertIsNone(receipt['acknowledged_at'])
        self.deliver(thread)
        self.assertFalse(connection.writes)

    def test_cancel_waits_then_reconnects_same_session_with_correction(self):
        thread = self.create()
        connection = self.start(thread)
        native = self.sessions.antigravity.native[thread]
        app_turn = self.sessions.active[thread]
        self.update(thread, sessionUpdate='tool_call', toolCallId='tool', title='command', status='in_progress')
        ident = self.send_correction(thread)
        self.deliver(thread)
        self.assertEqual(connection.writes[-1]['method'], 'session/cancel')
        self.assertFalse(connection.closed)
        self.deliver(thread)
        self.assertEqual(len(connection.writes), 1)
        connection.future.set_result({'stopReason': 'cancelled'})
        self.assertTrue(self.sessions.read(thread)['running'])
        self.deliver(thread)
        resumed = self.sessions.antigravity.connections[thread]
        self.assertIsNot(resumed, connection)
        self.assertTrue(connection.closed)
        self.assertEqual(self.sessions.active[thread], app_turn)
        self.assertIn(('session/resume', {'cwd': str(self.root), 'mcpServers': [], 'sessionId': native}), resumed.calls)
        self.assertIn(f'"id": {ident}', resumed.calls[-1][1]['prompt'][-1]['text'])
        tool = next(i for i in self.sessions.read(thread)['thread']['turns'][-1]['items'] if i['id'] == 'tool')
        self.assertEqual(tool['status'], 'unknown')
        self.assertIn('reconnected', tool['statusReason'])
        self.deliver(thread)
        self.assertFalse(resumed.writes)
        resumed.future.set_result({'stopReason': 'end_turn'})
        self.assertFalse(self.sessions.read(thread)['running'])

    def test_user_stop_during_delivery_never_resumes(self):
        thread = self.create()
        connection = self.start(thread)
        ident = self.send_correction(thread)
        self.deliver(thread)
        self.sessions.interrupt(thread)
        self.deliver(thread)
        self.sessions.inbox_wake.process_once()
        self.assertTrue(connection.closed)
        self.assertNotIn(thread, self.sessions.antigravity.connections)
        self.assertFalse(self.sessions.read(thread)['running'])
        receipt = self.sessions.antigravity.delivery.read('sender', ident)
        self.assertIsNone(receipt['delivered_at'])
        self.assertIn('stopped', receipt['delivery']['blocked_reason'].lower())
        self.assertIn('Use the revised interface', self.start(thread).calls[-1][1]['prompt'][-1]['text'])

    def test_stop_during_reconnect_does_not_submit_a_prompt(self):
        thread = self.create()
        original = self.start(thread)
        self.send_correction(thread)
        self.deliver(thread)
        original.future.set_result({'stopReason': 'cancelled'})
        provider = self.sessions.antigravity
        factory = provider.connection_factory
        def stopping_factory(*args):
            connection = factory(*args)
            request = connection.request
            def stopping_request(method, params, **kwargs):
                result = request(method, params, **kwargs)
                if method == 'session/resume':
                    self.sessions.interrupt(thread)
                return result
            connection.request = stopping_request
            return connection
        provider.connection_factory = stopping_factory
        self.deliver(thread)
        self.assertNotIn(thread, provider.connections)
        self.assertFalse(self.sessions.read(thread)['running'])

    def test_cancellation_timeout_is_visible_and_does_not_restart(self):
        thread = self.create()
        connection = self.start(thread)
        ident = self.send_correction(thread)
        self.deliver(thread)
        self.sessions.antigravity.delivery_turns[thread]['cancel_at'] -= 31
        self.deliver(thread)
        self.assertTrue(connection.closed)
        receipt = self.sessions.antigravity.delivery.read('sender', ident)
        self.assertEqual(receipt['delivery']['state'], 'uncertain')
        self.assertIn('timed out', receipt['delivery']['error'])
        self.assertIsNone(receipt['delivered_at'])
        self.assertFalse(self.sessions.read(thread)['running'])

    def test_old_continuation_cannot_close_a_new_user_turn(self):
        thread = self.create()
        original = self.start(thread)
        self.send_correction(thread)
        self.deliver(thread)
        original.future.set_result({'stopReason': 'cancelled'})
        provider = self.sessions.antigravity
        stale = provider.delivery_turns[thread]
        self.sessions.interrupt(thread)
        current = self.start(thread)
        provider._resume_delivery(thread, stale)
        self.assertIs(provider.connections[thread], current)
        self.assertFalse(current.closed)
        self.assertTrue(self.sessions.read(thread)['running'])

    def test_pending_permissions_are_cancelled_before_continuation(self):
        thread = self.create()
        connection = self.start(thread)
        self.permission(thread)
        self.send_correction(thread)
        self.deliver(thread)
        self.assertEqual(connection.writes[-2]['result']['outcome']['outcome'], 'cancelled')
        self.assertFalse(self.sessions.pending_requests(thread))

    def test_user_queue_takes_priority_over_inbox_interrupt(self):
        thread = self.create()
        connection = self.start(thread)
        self.send_correction(thread)
        with self.store._connection() as db:
            db.execute("INSERT INTO browser_message_queue (id, thread_id, message, state, owner, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                       ('queue-item', thread, 'user followup', 'queued', 'test', self.store.clock()))
        self.deliver(thread)
        self.assertFalse(connection.writes)

    def test_restart_retries_unacknowledged_delivery_without_new_identity(self):
        thread = self.create()
        ident = self.send_correction(thread)
        connection = self.start(thread)
        native = self.sessions.antigravity.native[thread]
        self.update(thread, sessionUpdate="agent_message_chunk", content={"type": "text", "text": "Received"})
        self.sessions.close()
        self.sessions = self.make_sessions()
        self.sessions.inbox_wake.process_once()
        self.assertNotIn(thread, self.sessions.antigravity.connections)
        connection = self.start(thread)
        self.assertEqual(self.sessions.antigravity.native[thread], native)
        self.assertIn(f'"id": {ident}', connection.calls[-1][1]['prompt'][-1]['text'])
        self.store.acknowledge(thread, ident)
        connection.future.set_result({'stopReason': 'end_turn'})
        self.assertNotIn('Coordination messages from other agents', str(self.start(thread).calls[-1]))

    def test_idle_wake_includes_message_body(self):
        thread = self.create()
        self.send_correction(thread)
        self.sessions.inbox_wake.process_once()
        connection = self.sessions.antigravity.connections[thread]
        self.assertIn('Use the revised interface', connection.calls[-1][1]['prompt'][-1]['text'])

    def test_natural_completion_can_win_cancel_race(self):
        thread = self.create()
        connection = self.start(thread)
        self.send_correction(thread)
        self.deliver(thread)
        connection.future.set_result({'stopReason': 'end_turn'})
        self.deliver(thread)
        self.assertIsNot(self.sessions.antigravity.connections[thread], connection)
        self.assertTrue(self.sessions.read(thread)['running'])

    def test_provider_activity_refreshes_liveness(self):
        thread = self.create()
        self.start(thread)
        with self.store._connection() as db:
            db.execute('UPDATE sessions SET last_seen_at = last_seen_at - 7200 WHERE session_id = ?', (thread,))
        self.assertEqual(self.store.get_session(thread)['presence'], 'stale')
        self.update(thread, sessionUpdate='tool_call', toolCallId='tool', status='in_progress')
        self.assertEqual(self.store.get_session(thread)['presence'], 'online')


class AntigravityTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = CoordinationStore(self.root / "state.sqlite3")

    def connection(self, source, **options):
        script = self.root / "fake_acp.py"
        script.write_text(source)
        connection = AntigravityConnection(self.store, lambda message: None, {"cwd": str(self.root), **options}, command=[sys.executable, "-u", str(script)])
        self.addCleanup(connection.close)
        return connection

    def test_transport_requests_and_credential_environment_isolation(self):
        source = '''import os,sys,json
for line in sys.stdin:
 m=json.loads(line)
 result={"protocolVersion":2,"agentInfo":{"name":"antigravity-acp"},"agentCapabilities":{}} if m["method"]=="initialize" else {k:os.environ.get(k) for k in ["GEMINI_API_KEY","GOOGLE_API_KEY","AGENT_COORD_SESSION_ID","GEMINI_HOME","BROWSER"]}
 print(json.dumps({"id":m["id"],"result":result}),flush=True)
'''
        with patch.dict(os.environ, {"GEMINI_API_KEY": "do-not-inherit", "GOOGLE_API_KEY": "do-not-inherit", "AGENT_COORD_SESSION_ID": "parent"}):
            connection = self.connection(source, threadId="child")
        self.assertEqual(connection.initialize()["protocolVersion"], 2)
        result = connection.request("environment", {})
        self.assertIsNone(result["GEMINI_API_KEY"])
        self.assertIsNone(result["GOOGLE_API_KEY"])
        self.assertEqual(result["AGENT_COORD_SESSION_ID"], "child")
        self.assertTrue(result["GEMINI_HOME"].endswith("providers/antigravity/profile"))
        self.assertIn("RIBBON_FIELD_AUTH_URL", result["BROWSER"])
        temporary = Path(connection.temporary.name)
        connection.close()
        self.assertFalse(temporary.exists())

    def test_private_hook_setup_preserves_other_hooks_and_rejects_corrupt_config(self):
        from agent_coord.antigravity import ensure_coordination_hooks
        profile = self.root / "profile"
        path = profile / "config/hooks.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"user-hook": {"enabled": False}}))
        ensure_coordination_hooks(profile)
        config = json.loads(path.read_text())
        self.assertEqual(config["user-hook"], {"enabled": False})
        rule = config["ribbon-field-coordination"]["PreToolUse"][0]
        self.assertIn("create_file", rule["matcher"])
        self.assertIn("multi_edit_file", rule["matcher"])
        self.assertIn("agent-coord-hook", rule["hooks"][0]["command"])
        first = path.read_bytes()
        ensure_coordination_hooks(profile)
        self.assertEqual(path.read_bytes(), first)
        path.write_text("not-json")
        with self.assertRaisesRegex(CoordinationError, "Cannot parse"):
            ensure_coordination_hooks(profile)
        self.assertEqual(path.read_text(), "not-json")

    def test_probe_login_request_fails_without_opening_browser(self):
        source = 'import sys,time\nsys.stdin.readline()\nprint(' + repr(AUTH_PREFIX + "https://accounts.google.com/o/oauth2/auth?state=abc") + ',flush=True)\ntime.sleep(5)\n'
        connection = self.connection(source)
        with self.assertRaisesRegex(CoordinationError, "antigravity login"):
            connection.request("authenticate", {}, timeout=2)

    def test_explicit_login_reports_only_valid_google_url_once(self):
        urls = []
        connection = self.connection("import sys\nfor line in sys.stdin: pass\n", authorize=urls.append)
        url = "https://accounts.google.com/o/oauth2/auth?state=abc"
        connection._authorization(AUTH_PREFIX + url)
        connection._authorization(AUTH_PREFIX + url)
        connection._authorization(AUTH_PREFIX + "https://accounts.google.com.evil.invalid/login")
        self.assertEqual(urls, [url])

    def test_disconnect_resolves_pending_request(self):
        connection = self.connection("import sys\nsys.stdin.readline()\n")
        with self.assertRaisesRegex(CoordinationError, "disconnected"):
            connection.request("initialize", {}, timeout=2)

    def test_installer_checks_hash_and_extracts_only_expected_pair(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("agy_acp_server.par", b"agent")
            output.writestr("localharness_external", b"helper")
        contents = archive.getvalue()
        release = ("macos", "darwin-arm64", hashlib.sha256(contents).hexdigest(), len(contents), 5, 6)
        with patch("agent_coord.antigravity_setup.platform.system", return_value="Darwin"), patch("agent_coord.antigravity_setup.platform.machine", return_value="arm64"), patch.dict("agent_coord.antigravity_setup.RELEASES", {("Darwin", "arm64"): release}), patch("urllib.request.urlopen", return_value=io.BytesIO(contents)):
            result = install_runtime(self.store)
        self.assertTrue(Path(result["path"]).is_file())
        with patch.dict(os.environ, {"AGENT_COORD_ANTIGRAVITY_BIN": result["path"]}):
            binary, helper = resolve_runtime(self.store)
        self.assertEqual(binary.read_bytes(), b"agent")
        self.assertEqual(helper.read_bytes(), b"helper")

    def test_invalid_download_never_publishes_runtime(self):
        release = ("macos", "darwin-arm64", "0" * 64, 3, 5, 6)
        with patch("agent_coord.antigravity_setup.platform.system", return_value="Darwin"), patch("agent_coord.antigravity_setup.platform.machine", return_value="arm64"), patch.dict("agent_coord.antigravity_setup.RELEASES", {("Darwin", "arm64"): release}), patch("urllib.request.urlopen", return_value=io.BytesIO(b"bad")):
            with self.assertRaisesRegex(CoordinationError, "SHA-256"):
                install_runtime(self.store)
        self.assertFalse((provider_directory(self.store) / "runtime/1.3.0").exists())


if __name__ == "__main__":
    unittest.main()
