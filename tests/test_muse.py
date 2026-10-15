from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch

from test_codex_app_server import FakeCodex, BrowserSessions, CoordinationStore, CoordinationError
from agent_coord.muse_code import MuseConnection, MuseRPC, command_id
from agent_coord.muse_setup import SCHEMA_FINGERPRINT, hook_config, host_config, resolve_runtime
from agent_coord.hook import handle

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"
FIXTURES = Path(__file__).with_name("fixtures") / "muse"


class FakeMuse:
    def __init__(self, store, callback, options):
        self.callback, self.options = callback, options
        self.process = self
        self.closed = False
        self.calls, self.writes = [], []
        self.fail = None

    def poll(self):
        return 0 if self.closed else None

    def initialize(self):
        return {"schema": {"version": 1, "fingerprint": SCHEMA_FINGERPRINT}}

    def request(self, method, params, **kwargs):
        self.calls.append((method, copy.deepcopy(params)))
        if self.fail == method:
            raise CoordinationError("Muse rejected the setting")
        if "commandId" in params:
            assert uuid.UUID(params["commandId"]).version == 7
        if method == "model/list":
            return {"models": [{"modelId": "muse-wonder-1.0", "displayLabel": "Muse Wonder", "isDefault": True,
                "defaultReasoningEffort": "high", "variants": ["low", "high", "max"],
                "reasoningEffortVariants": [{"tier": "high"}, {"tier": "max", "description": "More reasoning"}]}]}
        if method in {"session/start", "session/resume"}:
            self.native_id = params["sessionId"]
            self.profile = params["permissionProfile"]
            return {"session": {"sessionId": self.native_id, "modelId": params.get("modelId", "muse-wonder-1.0"),
                                "permissionState": {"profileId": self.profile}, "activeTurnId": None}, "viewCursor": ""}
        if method == "session/read":
            return {"session": {"sessionId": self.native_id, "permissionState": {"profileId": self.profile}}}
        if method == "turn/start":
            self.turn = params["commandId"]
            return {"turnId": self.turn, "status": "accepted", "disposition": "started"}
        return {"status": "accepted"}

    def write(self, message):
        self.writes.append(copy.deepcopy(message))

    def close(self):
        self.closed = True


class MuseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = CoordinationStore(self.root / "state.sqlite3")
        self.connections = []
        self.sessions = self.make_sessions()

    def factory(self, store, callback, options):
        connection = FakeMuse(store, callback, options)
        self.connections.append(connection)
        return connection

    def make_sessions(self):
        sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex,
            muse_factory=lambda store, event: MuseRPC(store, event, connection_factory=self.factory))
        self.addCleanup(sessions.close)
        return sessions

    def create(self, **settings):
        return self.sessions.create({"client": "muse", **settings})["session"]["thread_id"]

    def start(self, thread, **body):
        self.sessions.send(thread, {"message": "Hello", **body})
        return self.sessions.muse.connections[thread]

    def event(self, thread, method, **params):
        connection = self.sessions.muse.connections[thread]
        connection.callback({"method": method, "params": {"sessionId": connection.native_id, **params}})

    def complete(self, thread, status="completed", **params):
        connection = self.sessions.muse.connections[thread]
        self.event(thread, "item/completed", item={"itemId": "answer", "turnId": connection.turn,
            "kind": "agentMessage", "revision": 1, "status": "completed", "text": "Done"})
        self.event(thread, "turn/completed", turnId=connection.turn, terminal=status, **params)

    def replay(self, thread, name, *, before=None):
        connection = self.sessions.muse.connections[thread]
        for line in (FIXTURES / (name + ".ndjson")).read_text().splitlines():
            row = json.loads(line)
            message = json.loads(row["raw"])
            if row["dir"] != "server" or "method" not in message:
                continue
            params = message.get("params", {})
            if "sessionId" not in params:
                continue
            # Adapt only identities; all event shapes and content stay upstream.
            raw = json.dumps(message).replace(params["sessionId"], connection.native_id)
            for old in ("018f6a1e-9b3c-7c21-a54a-2f30bd3c9f10",):
                raw = raw.replace(old, connection.turn)
            message = json.loads(raw)
            if before:
                before(message)
            connection.callback(message)

    def permission(self, thread, stage=0, method="approval/requested"):
        self.event(thread, method, approvalId="approval", currentRequirementId={"approvalId": "approval", "sourceIndex": stage},
            toolName="bash", subject={"command": "touch file"}, availableChoices=[
                {"choiceId": "allow", "decision": "approved", "scope": "once"},
                {"choiceId": "deny", "decision": "denied", "scope": "once"}], viewCursor=f"approval:{stage}")
        return self.sessions.pending_requests(thread)[0]

    def test_catalog_registration_and_uuid7_commands(self):
        models = self.sessions.models("muse")
        self.assertEqual(models[0]["model"], "muse-wonder-1.0")
        self.assertEqual(models[0]["supportedReasoningEfforts"], [
            {"reasoningEffort": "low", "description": ""}, {"reasoningEffort": "high", "description": ""},
            {"reasoningEffort": "max", "description": "More reasoning"}])
        self.assertNotIn("threadId", self.connections[0].options)
        self.assertTrue(self.connections[0].closed)
        thread = self.create(model="muse-wonder-1.0", effort="max")
        self.assertEqual(uuid.UUID(thread).version, 7)
        self.assertEqual(self.store.get_session(thread)["client"], "muse")
        self.assertEqual(self.sessions.read(thread)["session"]["effort"], "max")
        self.assertFalse(self.sessions.rpc.calls)
        from agent_coord.cli import _parser
        from agent_coord.app_control import AppControl
        self.assertEqual(_parser().parse_args(["thread", "models", "--client", "muse"]).client, "muse")
        self.assertEqual(AppControl(self.store).request("models", thread, {"client": "muse"})["status"], "queued")

    def test_upstream_stream_fixture_final_text_is_not_duplicated(self):
        thread = self.create()
        self.start(thread)
        self.replay(thread, "text-run-single-turn")
        turn = self.sessions.read(thread)["thread"]["turns"][-1]
        self.assertEqual(turn["status"], "completed")
        self.assertEqual([i["type"] for i in turn["items"]], ["userMessage", "agentMessage"])
        self.assertEqual(turn["items"][-1]["text"], "All 214 tests pass except two in tbh-agent...")
        self.sessions.close()
        self.sessions = self.make_sessions()
        self.assertEqual(self.sessions.read(thread)["thread"]["turns"][-1], turn)
        connection = self.start(thread)
        self.assertIn("session/resume", [m for m, _ in connection.calls])
        self.assertEqual(connection.native_id, thread)

    def test_images_and_coordination_identity_are_native_input(self):
        thread = self.create()
        connection = self.start(thread, images=[{"name": "x.png", "url": "data:image/png;base64,iVBORw0KGgo="}])
        prompt = connection.calls[-1][1]
        self.assertIn(thread, prompt["input"][0]["text"])
        self.assertIn("AGENTS.md", prompt["input"][0]["text"])
        self.assertEqual(prompt["input"][-1], {"type": "image", "mediaType": "image/png", "base64Data": "iVBORw0KGgo="})
        self.assertNotIn("agent-coord", prompt["displayText"])

    def test_approval_stage_refresh_rejects_stale_click_and_cross_session_answer(self):
        one, two = self.create(), self.create()
        connection = self.start(one)
        self.start(two)
        old = self.permission(one)
        with self.assertRaises(CoordinationError):
            self.sessions.answer(two, old["key"], {"decision": "accept"})
        fresh = self.permission(one, 1, "approval/updated")
        with self.assertRaises(CoordinationError):
            self.sessions.answer(one, old["key"], {"decision": "accept"})
        with self.assertRaises(CoordinationError):
            self.sessions.answer(one, fresh["key"], {"decision": "acceptForSession"})
        self.sessions.answer(one, fresh["key"], {"decision": "decline"})
        method, params = connection.calls[-1]
        self.assertEqual(method, "approval/decide")
        self.assertEqual(params["requirementId"]["sourceIndex"], 1)
        self.assertEqual(params["choiceId"], "deny")
        # A new stage may arrive after the decision ack and previous UI removal.
        latest = self.permission(one, 2, "approval/updated")
        self.assertNotEqual(latest["key"], fresh["key"])

    def test_native_fixture_approval_and_tool_result(self):
        thread = self.create()
        connection = self.start(thread)
        def answer_before_resolved(message):
            if message["method"] == "approval/resolved":
                pending = self.sessions.pending_requests(thread)
                self.assertEqual(len(pending), 1)
                self.sessions.answer(thread, pending[0]["key"], {"decision": "acceptForSession"})
        self.replay(thread, "approval-round-trip", before=answer_before_resolved)
        decision = next(p for m, p in connection.calls if m == "approval/decide")
        self.assertEqual(decision["choiceId"], "allow_session")
        self.assertEqual(connection.writes[0]["result"], {})
        turn = self.sessions.read(thread)["thread"]["turns"][-1]
        self.assertEqual(turn["items"][-1]["result"], "Cargo.toml updated\n")
        self.assertFalse(self.sessions.pending_requests(thread))

    def test_review_progress_and_late_requests_do_not_reopen_resolved_approval(self):
        thread = self.create()
        connection = self.start(thread)
        params = {"approvalId": "approval", "currentRequirementId": {"approvalId": "approval", "sourceIndex": 0},
                  "toolName": "bash", "availableChoices": [{"choiceId": "allow", "decision": "approved", "scope": "once"}]}
        self.event(thread, "approval/updated", **params, change={"kind": "reviewStarted"})
        self.assertFalse(self.sessions.pending_requests(thread))
        self.event(thread, "approval/requested", **params)
        pending = self.sessions.pending_requests(thread)[0]
        with patch.object(connection, "request", return_value={"terminal": True}):
            self.sessions.answer(thread, pending["key"], {"decision": "accept"})
        self.event(thread, "approval/updated", **params, change={"kind": "stageResolved"})
        self.event(thread, "approval/requested", **params)
        self.assertFalse(self.sessions.pending_requests(thread))
        self.event(thread, "approval/resolved", approvalId="approval")
        self.event(thread, "approval/requested", **params)
        self.assertFalse(self.sessions.pending_requests(thread))

    def test_questions_allow_free_text_and_multiple_choices(self):
        thread = self.create(yolo=True)
        connection = self.start(thread)
        self.event(thread, "userInput/requested", userInputId="question", questions=[
            {"id": "a", "header": "Name", "question": "Name?", "options": [], "selection": {"mode": "single"}},
            {"id": "b", "header": "OS", "question": "Which?", "options": [{"label": "Mac"}, {"label": "Linux"}],
             "selection": {"mode": "multiple", "minSelections": 1, "maxSelections": 2}}])
        pending = self.sessions.pending_requests(thread)[0]
        self.assertTrue(pending["params"]["questions"][1]["multiSelect"])
        self.assertFalse(pending["params"]["questions"][1]["allowCustomAnswer"])
        self.sessions.answer(thread, pending["key"], {"answers": {"a": {"answers": ["Example"]}, "b": {"answers": ["Mac", "Linux"]}}})
        self.assertEqual(connection.calls[-1][0], "userInput/answer")
        self.assertEqual(connection.calls[-1][1]["answers"], [{"questionId": "a", "freeText": "Example"},
                                                           {"questionId": "b", "selectedLabels": ["Mac", "Linux"]}])

    def test_multistage_native_approval_reuses_opening_cursor(self):
        thread = self.create()
        connection = self.start(thread)
        params = {"sessionId": thread, "approvalId": "multi", "viewCursor": "opening:10", "toolName": "bash",
                  "currentRequirementId": {"approvalId": "multi", "sourceIndex": 0},
                  "availableChoices": [{"choiceId": "allow_once", "decision": "approved", "scope": "once"}]}
        connection.callback({"id": 1, "method": "approval/request", "params": params})
        first = self.sessions.pending_requests(thread)[0]
        self.sessions.answer(thread, first["key"], {"decision": "accept"})
        next_stage = {**params, "currentRequirementId": {"approvalId": "multi", "sourceIndex": 1}}
        self.event(thread, "approval/updated", **{**next_stage, "viewCursor": "update:11"},
                   change={"kind": "stageResolved", "requirementId": params["currentRequirementId"]})
        self.assertEqual(len(self.sessions.pending_requests(thread)), 1)
        connection.callback({"id": 2, "method": "approval/request", "params": next_stage})
        second = self.sessions.pending_requests(thread)[0]
        self.sessions.answer(thread, second["key"], {"decision": "accept"})
        # A later stage can arrive solely as a reissued request with that same cursor.
        final_stage = {**params, "currentRequirementId": {"approvalId": "multi", "sourceIndex": 2}}
        connection.callback({"id": 3, "method": "approval/request", "params": final_stage})
        third = self.sessions.pending_requests(thread)[0]
        with patch.object(connection, "request", return_value={"terminal": True}):
            self.sessions.answer(thread, third["key"], {"decision": "accept"})
        self.assertFalse(self.sessions.pending_requests(thread))
        self.assertEqual([p["requirementId"]["sourceIndex"] for m, p in connection.calls if m == "approval/decide"], [0, 1])
        self.assertEqual([m["id"] for m in connection.writes], [1, 2, 3])

    def test_escalation_approval_discloses_sandbox_scope_and_justification(self):
        thread = self.create()
        connection = self.start(thread)
        self.event(thread, "approval/requested", approvalId="elevated",
            currentRequirementId={"approvalId": "elevated", "sourceIndex": 0}, toolName="bash",
            rawArgs=json.dumps({"command": "agent-coord inbox", "sandbox_permissions": "require_escalated",
                               "justification": "Read and update coordination state outside the workspace."}),
            subject={"command": "agent-coord inbox"},
            availableChoices=[{"choiceId": "allow_once", "decision": "approved", "scope": "once"}])
        pending = self.sessions.pending_requests(thread)[0]
        self.assertEqual(pending["params"]["reason"],
                         "Run outside the workspace sandbox: Read and update coordination state outside the workspace.")
        self.assertEqual(pending["params"]["command"], "agent-coord inbox")
        self.sessions.answer(thread, pending["key"], {"decision": "accept"})
        self.assertEqual(connection.calls[-1][1]["choiceId"], "allow_once")

    def test_stop_and_stale_events_are_isolated_and_resume_keeps_identity(self):
        one, two = self.create(), self.create()
        old = self.start(one)
        self.start(two)
        self.sessions.interrupt(one)
        self.assertTrue(old.closed)
        self.assertIn("turn/interrupt", [m for m, _ in old.calls])
        self.assertTrue(self.sessions.read(two)["running"])
        new = self.start(one)
        self.assertEqual(old.native_id, new.native_id)
        old.callback({"method": "disconnected", "params": {"error": "old host"}})
        self.assertTrue(self.sessions.read(one)["running"])
        new.callback({"method": "disconnected", "params": {"error": "Muse crashed"}})
        self.assertEqual(self.sessions.read(one)["thread"]["turns"][-1]["status"], "failed")
        self.assertTrue(self.sessions.read(two)["running"])

    def test_cancel_approval_stops_turn_without_double_removing_request(self):
        thread = self.create()
        self.start(thread)
        pending = self.permission(thread)
        self.sessions.answer(thread, pending["key"], {"decision": "cancel"})
        self.assertFalse(self.sessions.read(thread)["running"])
        self.assertFalse(self.sessions.pending_requests(thread))

    def test_gap_closes_old_host_before_publishing_completion(self):
        thread = self.create()
        old = self.start(thread)
        emit = self.sessions.muse._emit
        def observe(method, *args, **kwargs):
            if method == "turn/completed":
                self.assertTrue(old.closed)
            return emit(method, *args, **kwargs)
        with patch.object(self.sessions.muse, "_emit", side_effect=observe):
            self.event(thread, "view/gap")
        self.assertEqual(self.sessions.read(thread)["thread"]["turns"][-1]["status"], "failed")
        new = self.start(thread)
        self.assertEqual(new.native_id, old.native_id)
        self.assertFalse(new.closed)

    def test_native_queued_admission_and_streaming_tool_output(self):
        thread = self.create()
        connection = self.start(thread)
        self.complete(thread)
        request = connection.request
        def queued(method, params, **kwargs):
            result = request(method, params, **kwargs)
            if method == "turn/start":
                result["disposition"] = "queued"
            return result
        with patch.object(connection, "request", side_effect=queued):
            self.start(thread)
        self.assertTrue(self.sessions.read(thread)["running"])
        self.event(thread, "item/started", item={"itemId": "tool", "kind": "toolCall", "tool": "bash",
            "turnId": connection.turn, "revision": 1, "status": "inProgress", "visibleOutput": "one"})
        self.event(thread, "item/delta", itemId="tool", field="output", delta=" two")
        self.assertEqual(self.sessions.read(thread)["thread"]["turns"][-1]["items"][-1]["result"], "one two")

    def test_permission_changes_restart_host_and_preserve_native_id(self):
        thread = self.create()
        self.assertNotIn(thread, self.sessions.muse.connections)
        old = self.start(thread)
        self.assertEqual(self.sessions.read(thread)["session"]["model"], "muse-wonder-1.0")
        self.complete(thread)
        self.assertIs(self.start(thread), old)
        self.complete(thread)
        self.sessions.update(thread, {"yolo": True, "effort": "max"})
        new = self.start(thread)
        self.assertTrue(old.closed)
        self.assertTrue(new.options["yolo"])
        self.assertEqual(new.native_id, old.native_id)
        self.assertIn(("session/resume", ":unrestricted"), [(m, p.get("permissionProfile")) for m, p in new.calls])
        self.complete(thread)
        self.sessions.update(thread, {"yolo": False})
        downgraded = self.start(thread)
        self.assertTrue(new.closed)
        self.assertEqual(downgraded.native_id, old.native_id)
        self.assertIn(("session/resume", ":ask-me"), [(m, p.get("permissionProfile")) for m, p in downgraded.calls])
        self.complete(thread)
        self.sessions.close_work_thread(thread)
        self.sessions.reopen_work_thread(thread)
        self.assertEqual(self.start(thread).native_id, old.native_id)

    def test_permission_profile_readback_and_fail_closed(self):
        native_request = FakeMuse.request
        def missing_state(connection, method, params, **kwargs):
            result = native_request(connection, method, params, **kwargs)
            if method in {"session/start", "session/resume"}:
                result["session"].pop("permissionState")
            return result
        with patch.object(FakeMuse, "request", missing_state):
            thread = self.create()
            self.start(thread)
        self.assertIn("session/read", [m for m, _ in self.connections[-1].calls])
        self.complete(thread)
        self.sessions.update(thread, {"yolo": True})
        def wrong_state(connection, method, params, **kwargs):
            result = missing_state(connection, method, params, **kwargs)
            if method == "session/read":
                result["session"]["permissionState"] = {"profileId": ":ask-me"}
            return result
        with patch.object(FakeMuse, "request", wrong_state):
            with self.assertRaisesRegex(CoordinationError, "did not apply"):
                self.start(thread)
        refused = self.connections[-1]
        self.assertTrue(refused.closed)
        self.assertNotIn("turn/start", [m for m, _ in refused.calls])
        self.assertEqual(self.start(thread).native_id, thread)

    def test_unsent_thread_survives_restart_and_idle_unload_resumes(self):
        thread = self.create()
        self.assertFalse(self.connections)
        self.sessions.close()
        self.sessions = self.make_sessions()
        connection = self.start(thread)
        self.assertIn("session/start", [m for m, _ in connection.calls])
        self.complete(thread)
        self.event(thread, "session/closed", reason="idle")
        self.assertTrue(connection.closed)
        self.assertNotIn(thread, self.sessions.muse.connections)
        resumed = self.start(thread)
        self.assertEqual(resumed.native_id, thread)
        self.assertIn("session/resume", [m for m, _ in resumed.calls])

    def test_error_empty_completion_and_unknown_items_are_visible(self):
        thread = self.create()
        connection = self.start(thread)
        self.event(thread, "turn/completed", turnId=connection.turn, terminal="completed")
        self.assertEqual(self.sessions.read(thread)["thread"]["turns"][-1]["status"], "failed")
        connection = self.start(thread)
        self.event(thread, "item/completed", item={"itemId": "future", "kind": "futureKind", "revision": 1,
                                                  "status": "completed", "fallbackText": "Native activity"})
        self.event(thread, "turn/completed", turnId=connection.turn, terminal="failed", error={"message": "Quota exhausted"})
        turn = self.sessions.read(thread)["thread"]["turns"][-1]
        self.assertEqual(turn["items"][-1]["tool"], "futureKind")
        self.assertEqual(turn["error"]["message"], "Quota exhausted")

    def test_newer_schema_supports_catalog_turns_approvals_and_resume(self):
        native_request = FakeMuse.request
        def newer_host(connection, method, params, **kwargs):
            if method == "initialize":
                return {"schema": {"version": 1, "fingerprint": "sha256:future"},
                        "sessionDurability": "durable", "futureField": {"enabled": True}}
            result = native_request(connection, method, params, **kwargs)
            result["futureField"] = True
            if "session" in result:
                result["session"]["futureField"] = True
            return result
        with patch.object(FakeMuse, "initialize", MuseConnection.initialize), \
                patch.object(FakeMuse, "request", newer_host), \
                self.assertLogs("agent_coord.muse_code", level="WARNING"):
            self.assertTrue(self.sessions.models("muse"))
            thread = self.create()
            connection = self.start(thread)
            self.assertIn({"method": "initialized"}, connection.writes)
            self.event(thread, "future/notification", futureField={"enabled": True})
            pending = self.permission(thread)
            self.sessions.answer(thread, pending["key"], {"decision": "accept"})
            self.assertEqual(connection.calls[-1][0], "approval/decide")
            self.replay(thread, "text-run-single-turn")
            self.assertEqual(self.sessions.read(thread)["thread"]["turns"][-1]["status"], "completed")
            self.sessions.muse._shutdown(thread)
            resumed = self.start(thread)
            self.assertEqual(resumed.native_id, thread)
            self.assertIn("session/resume", [m for m, _ in resumed.calls])
            self.complete(thread)

    def test_wrong_session_identity_prevents_turn_start(self):
        native_request = FakeMuse.request
        def wrong_identity(connection, method, params, **kwargs):
            result = native_request(connection, method, params, **kwargs)
            if method in {"session/start", "session/resume"}:
                result["session"]["sessionId"] = "another-session"
            return result
        thread = self.create()
        with patch.object(FakeMuse, "request", wrong_identity):
            with self.assertRaisesRegex(CoordinationError, "different session identity"):
                self.start(thread)
        self.assertTrue(self.connections[-1].closed)
        self.assertNotIn("turn/start", [m for m, _ in self.connections[-1].calls])


class MuseSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = CoordinationStore(self.root / "state.sqlite3")

    def test_hook_override_preserves_user_settings_and_cleared_environment(self):
        settings = self.root / "muse/settings.json"
        settings.parent.mkdir()
        original = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "true"}]}]}, "model": "mine"}
        settings.write_text(json.dumps(original))
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root)}):
            override = hook_config(self.store, "ribbon")
        self.assertEqual(json.loads(settings.read_text()), original)
        hooks = json.loads(override.removeprefix("hooks="))
        self.assertEqual(hooks["Stop"], original["hooks"]["Stop"])
        command = hooks["UserPromptSubmit"][-1]["hooks"][0]["command"]
        result = subprocess.run(command, shell=True, env={"PATH": os.defpath}, text=True, capture_output=True, check=True,
            input=json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": "ribbon", "cwd": str(self.root), "prompt": "Hello"}))
        self.assertIn("checkpoint", result.stdout)
        self.assertEqual(self.store.get_session("ribbon")["client"], "muse")
        with self.assertRaises(CoordinationError):
            self.store.get_session("native")

    def test_native_write_hooks_guard_paths_and_patch_moves(self):
        self.store.register(session_id="muse", client="muse", cwd=str(self.root))
        self.store.register(session_id="peer", client="codex", cwd=str(self.root))
        self.store.begin_work(session_id="peer", scopes=["other/**"])
        self.store.begin_work(session_id="muse", scopes=["src/**"])
        env = {**os.environ, "AGENT_COORD_CLIENT": "muse", "AGENT_COORD_SESSION_ID": "muse", "AGENT_COORD_DB": str(self.store.database_path)}
        for name, args, denied in [("write_file", {"path": "other/x"}, True), ("edit_file", {"path": "src/x"}, False),
            ("apply_patch", {"patch": "*** Begin Patch\n*** Update File: src/x\n*** Move to: other/x\n*** End Patch"}, True)]:
            with self.subTest(name=name):
                result = subprocess.run([sys.executable, str(SCRIPTS / "agent-coord-hook")], env=env, text=True, capture_output=True, check=True,
                    input=json.dumps({"hook_event_name": "PreToolUse", "session_id": "native", "cwd": str(self.root), "tool_name": name, "tool_input": args}))
                output = json.loads(result.stdout).get("hookSpecificOutput", {})
                self.assertEqual(output.get("permissionDecision") == "deny", denied)

    def test_private_hook_overlay_preserves_native_configuration(self):
        source = self.root / "config"
        native = source / "muse"
        native.mkdir(parents=True)
        original = {"schema_version": 1, "model": "mine", "managed_hooks_path": "managed.json"}
        (native / "settings.json").write_text(json.dumps(original))
        (native / "auth.json").write_text('{"dummy":"test-only"}')
        (source / "other-app").mkdir()
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(source)}):
            with host_config(self.store, "ribbon") as directory:
                overlay = Path(directory)
                settings = json.loads((overlay / "muse/settings.json").read_text())
                self.assertEqual(settings["model"], "mine")
                self.assertEqual(settings["managed_hooks_path"], str(native / "managed.json"))
                self.assertIn("PreToolUse", settings["hooks"])
                self.assertTrue((overlay / "muse/auth.json").samefile(native / "auth.json"))
                self.assertTrue((overlay / "other-app").samefile(source / "other-app"))
        self.assertFalse(overlay.exists())
        self.assertEqual(json.loads((native / "settings.json").read_text()), original)

    def test_native_children_preserve_parent_inbox_and_lifecycle(self):
        self.store.register(session_id="parent", client="muse", cwd=str(self.root))
        self.store.register(session_id="peer", client="codex", cwd=str(self.root))
        self.store.begin_work(session_id="parent", scopes=["src/**"])
        self.store.send_message(sender_session_id="peer", recipient_session_id="parent", body="ROOT_ONLY_TASK")
        before = self.store.get_session("parent")
        payload = {"session_id": "native-reminder-child", "cwd": str(self.root / "child"),
                   "tool_name": "submit_reminder_decision", "tool_input": {}}
        env = {"AGENT_COORD_CLIENT": "muse", "AGENT_COORD_SESSION_ID": "parent"}
        with patch.dict(os.environ, env):
            for event in ("UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop", "SessionEnd"):
                self.assertEqual(handle({**payload, "hook_event_name": event}, self.store), {})
            self.assertEqual(self.store.get_session("parent"), before)
            denied = handle({**payload, "hook_event_name": "PreToolUse", "tool_name": "write_file",
                             "tool_input": {"path": "other/file"}}, self.store)
            self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
            allowed = handle({**payload, "hook_event_name": "PreToolUse", "tool_name": "edit_file",
                              "tool_input": {"path": "src/file"}}, self.store)
            self.assertEqual(allowed, {})
            delivered = handle({**payload, "session_id": "parent", "cwd": str(self.root),
                                "hook_event_name": "PostToolUse"}, self.store)
            self.assertIn("ROOT_ONLY_TASK", delivered["hookSpecificOutput"]["additionalContext"])
        self.assertFalse(self.store.inbox("parent"))

    def test_transport_fingerprint_warning_and_nonblocking_notifications(self):
        script = self.root / "host.py"
        script.write_text('''import json,sys
for line in sys.stdin:
 m=json.loads(line)
 if 'id' not in m: continue
 if m['method']=='initialize': r={'schema':{'version':1,'fingerprint':sys.argv[1]},'sessionDurability':'durable'}
 else:
  print(json.dumps({'method':'notice','params':{}}),flush=True)
  r={'ok':True}
 print(json.dumps({'id':m['id'],'result':r}),flush=True)
''')
        entered, release = threading.Event(), threading.Event()
        def callback(message):
            entered.set()
            release.wait(3)
        connection = MuseConnection(self.store, callback, {"cwd": str(self.root)},
            command=[sys.executable, "-u", str(script), SCHEMA_FINGERPRINT])
        self.addCleanup(connection.close)
        with self.assertNoLogs("agent_coord.muse_code", level="WARNING"):
            connection.initialize()
        self.assertTrue(connection.request("test", {}, timeout=1)["ok"])
        self.assertTrue(entered.wait(1))
        release.set()
        connection.close()
        newer = MuseConnection(self.store, lambda _: None, {"cwd": str(self.root)}, command=[sys.executable, "-u", str(script), "unknown"])
        self.addCleanup(newer.close)
        with self.assertLogs("agent_coord.muse_code", level="WARNING") as logs:
            newer.initialize()
        self.assertIn("received unknown", logs.output[0])
        self.assertTrue(newer.request("test", {}, timeout=1)["ok"])

    def test_handshake_rejects_unsupported_schema_and_nondurable_sessions(self):
        script = self.root / "host.py"
        script.write_text('''import json,sys
for line in sys.stdin:
 m=json.loads(line)
 if 'id' in m: print(json.dumps({'id':m['id'],'result':json.loads(sys.argv[1])}),flush=True)
''')
        cases = [({"schema": schema}, "schema version") for schema in
                 ({"version": 2, "fingerprint": SCHEMA_FINGERPRINT}, {}, None)]
        cases.append(({"schema": {"version": 1, "fingerprint": "future"},
                       "sessionDurability": "ephemeral"}, "retain session history"))
        for result, error in cases:
            with self.subTest(result=result):
                connection = MuseConnection(self.store, lambda _: None, {"cwd": str(self.root)},
                    command=[sys.executable, "-u", str(script), json.dumps(result)])
                try:
                    with patch.object(connection, "write", wraps=connection.write) as write:
                        with self.assertRaisesRegex(CoordinationError, error):
                            connection.initialize()
                        self.assertFalse(any(c.args[0].get("method") == "initialized" for c in write.call_args_list))
                finally:
                    connection.close()

    def test_missing_binary_reports_actionable_setup(self):
        with patch.dict(os.environ, {"AGENT_COORD_MUSE_BIN": str(self.root / "missing")}):
            with self.assertRaisesRegex(CoordinationError, "muse login"):
                resolve_runtime(self.store)


if __name__ == "__main__":
    unittest.main()
