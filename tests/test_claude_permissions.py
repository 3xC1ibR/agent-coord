"""Explain Claude's retained confirmations without changing permission policy."""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from test_browser_providers import claude_factory
from test_codex_app_server import BrowserSessions, CoordinationStore, FakeCodex


# Wire shape captured from Claude Code 2.1.312 with bypassPermissions enabled.
# The native probe targeted only an absent leaf in its private scratch directory.
RM_REQUEST = {
    "subtype": "can_use_tool", "tool_name": "Bash", "display_name": "Bash",
    "input": {"command": "rm -rf $(printf '%s' '/private/scratch/absent')",
              "description": "Delete the absent scratch leaf path"},
    "description": "Delete the absent scratch leaf path", "permission_suggestions": [],
    "decision_reason": "Dangerous rm operation on statically-unresolvable target: command substitution output",
    "decision_reason_type": "safetyCheck", "classifier_approvable": False,
    "tool_use_id": "tool-rm", "suppress_always_allow_rule": True,
}


class ClaudePermissionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = CoordinationStore(self.root / "state.sqlite3")
        self.sessions = self.make_sessions()

    def make_sessions(self):
        sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex,
                                   claude_factory=claude_factory)
        self.addCleanup(sessions.close)
        return sessions

    def start(self, *, yolo=False):
        thread = self.sessions.create({"client": "claude", "yolo": yolo})["session"]["thread_id"]
        self.sessions.send(thread, {"message": "Start"})
        self.emit(thread, {"type": "system", "subtype": "init"})
        return thread

    def emit(self, thread, frame):
        self.sessions.claude.connections[thread].callback(frame)

    def request(self, thread, **overrides):
        request = {**copy.deepcopy(RM_REQUEST), **overrides}
        self.emit(thread, {"type": "control_request", "request_id": "permission-rm", "request": request})
        return request

    def assert_pending(self, thread, request):
        pending = self.sessions.pending_requests(thread)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["params"]["reason"], request["decision_reason"])
        self.assertEqual(json.loads(pending[0]["params"]["command"]), request["input"])
        self.assertNotEqual(self.sessions.claude.connections[thread].writes[-1].get("type"), "control_response")
        self.assertTrue(self.sessions.read(thread)["running"])
        return pending[0]

    def test_yolo_explains_native_rm_callback_without_automatically_accepting_it(self):
        thread = self.start(yolo=True)
        request = self.request(thread)
        pending = self.assert_pending(thread, request)
        self.sessions.answer(thread, pending["key"], {"decision": "decline"})
        response = self.sessions.claude.connections[thread].writes[-1]["response"]["response"]
        self.assertEqual(response["behavior"], "deny")
        self.assertEqual(self.sessions.pending_requests(thread), [])
        self.assertEqual(len(self.sessions.claude.connections[thread].writes), 2)  # Prompt and reply only.

    def test_default_keeps_native_rm_approval_and_explains_why(self):
        thread = self.start()
        request = self.request(thread)
        pending = self.sessions.pending_requests(thread)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["params"]["reason"], request["decision_reason"])
        self.sessions.answer(thread, pending[0]["key"], {"decision": "decline"})
        self.assertEqual(self.sessions.claude.connections[thread].writes[-1]["response"]["response"]["behavior"], "deny")

    def test_yolo_preserves_explicit_rules_unknown_checks_and_incomplete_metadata(self):
        thread = self.start(yolo=True)
        for overrides in (
            {"decision_reason_type": "rule"},
            {"decision_reason": "Your organization requires approval for this tool"},
            {"decision_reason": "A different safety check"},
            {"decision_reason": None},
            {"decision_reason_type": None},
            {"tool_name": "Write"},
            {"tool_name": "mcp__external__delete"},
        ):
            with self.subTest(overrides=overrides):
                self.request(thread, **overrides)
                self.assertEqual(len(self.sessions.pending_requests(thread)), 1)
                self.emit(thread, {"type": "control_cancel_request", "request_id": "permission-rm"})

    def test_older_providers_fall_back_to_description_then_tool_name(self):
        thread = self.start()
        for description, expected in (("Run the focused tests", "Run the focused tests"), (None, "Bash")):
            with self.subTest(description=description):
                self.request(thread, decision_reason=None, description=description)
                self.assertEqual(self.sessions.pending_requests(thread)[0]["params"]["reason"], expected)
                self.emit(thread, {"type": "control_cancel_request", "request_id": "permission-rm"})

    def test_yolo_still_waits_for_answers_to_product_questions(self):
        thread = self.start(yolo=True)
        self.request(thread, tool_name="AskUserQuestion", input={
            "questions": [{"question": "Which account?", "options": [{"label": "Personal"}]}]})
        pending = self.sessions.pending_requests(thread)
        self.assertEqual(pending[0]["method"], "item/tool/requestUserInput")
        self.sessions.answer(thread, pending[0]["key"], {"answers": {"0": {"answers": ["Personal"]}}})
        response = self.sessions.claude.connections[thread].writes[-1]["response"]["response"]
        self.assertEqual(response["updatedInput"]["answers"], {"Which account?": "Personal"})

    def test_stop_enable_yolo_resume_then_restore_default(self):
        thread = self.start()
        original = self.sessions.claude.connections[thread]
        self.sessions.interrupt(thread)
        self.sessions.update(thread, {"yolo": True})
        self.sessions.send(thread, {"message": "Continue"})
        self.assertTrue(original.closed)
        resumed = self.sessions.claude.connections[thread]
        self.assertTrue(resumed.options["resume"])
        self.assert_pending(thread, self.request(thread))
        self.emit(thread, {"type": "control_cancel_request", "request_id": "permission-rm"})
        self.emit(thread, {"type": "result", "subtype": "success"})
        self.sessions.update(thread, {"yolo": False})
        self.sessions.send(thread, {"message": "Review"})
        self.assertTrue(resumed.closed)
        self.assertFalse(self.sessions.claude.connections[thread].options["yolo"])
        self.assert_pending(thread, self.request(thread))

    def test_runtime_recreation_uses_saved_yolo(self):
        thread = self.start(yolo=True)
        self.emit(thread, {"type": "system", "subtype": "init"})
        self.emit(thread, {"type": "result", "subtype": "success"})
        self.sessions.close()
        self.sessions = self.make_sessions()
        self.sessions.send(thread, {"message": "Continue after restart"})
        self.assertTrue(self.sessions.claude.connections[thread].options["resume"])
        self.assertTrue(self.sessions.claude.connections[thread].options["yolo"])
        self.assert_pending(thread, self.request(thread))

    def test_idle_release_and_prewarm_keep_effective_permissions(self):
        thread = self.start(yolo=True)
        self.emit(thread, {"type": "result", "subtype": "success"})
        self.assertTrue(self.sessions.claude.release(thread))
        self.assertTrue(self.sessions.claude.prewarm(thread))
        warm = self.sessions.claude.connections[thread]
        self.sessions.send(thread, {"message": "Continue after idle release"})
        self.assertIs(self.sessions.claude.connections[thread], warm)
        self.assertTrue(warm.options["yolo"])
        self.assert_pending(thread, self.request(thread))

    def test_permission_choice_is_isolated_per_thread(self):
        yolo = self.start(yolo=True)
        default = self.start()
        pending = self.assert_pending(yolo, self.request(yolo))
        self.assert_pending(default, self.request(default))
        self.sessions.answer(yolo, pending["key"], {"decision": "accept"})
        self.assertEqual(len(self.sessions.pending_requests(default)), 1)
        self.assertFalse(self.sessions.pending_requests(yolo))
