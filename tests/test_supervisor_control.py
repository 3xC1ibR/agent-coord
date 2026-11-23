"""Public supervisor controls against real runtimes with inert native transports."""
import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_app_control as app_tests
from test_codex_app_server import PLUGIN_SCRIPTS
from agent_coord.checkpoint_mcp import dispatch
from agent_coord.session_control import SessionControl
from agent_coord.store import CoordinationError


class SupervisorControlTests(unittest.TestCase):
    setUp = app_tests.AppControlTests.setUp
    runtime = app_tests.AppControlTests.runtime
    request = app_tests.AppControlTests.request
    create = app_tests.AppControlTests.create
    finish = app_tests.AppControlTests.finish
    deliver = app_tests.AppControlTests.deliver
    continuation = app_tests.AppControlTests.continuation

    def cli(self, *args, caller="dispatcher", expected=0):
        result = subprocess.run(
            [sys.executable, str(PLUGIN_SCRIPTS / "agent-coord"), "--db", str(self.store.database_path), *args],
            env=dict(os.environ, AGENT_COORD_SESSION_ID=caller), cwd=self.root,
            capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, expected, result.stderr)
        return json.loads(result.stdout if expected == 0 else result.stderr)

    def mcp(self, name, arguments, caller="dispatcher", error=False):
        with patch("agent_coord.checkpoint_mcp.CoordinationStore", return_value=self.store):
            response = dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                "name": name, "arguments": arguments, "_meta": {"threadId": caller}}})
        self.assertNotIn("error", response, response)
        self.assertEqual(bool(response["result"].get("isError")), error, response)
        text = response["result"]["content"][0]["text"]
        return text if error else json.loads(text)

    def active_agent(self, client):
        thread = self.create(client, subscribe=False)
        self.sessions.inbox_wake.process_once()
        self.deliver(thread)
        self.assertTrue(self.sessions.persistence.flush(key=thread))
        return thread, self.sessions.active[thread]

    def interrupt(self, thread, turn, key, entry="cli", **kwargs):
        if entry == "mcp":
            return self.mcp("interrupt_thread", {"session_id": thread, "expected_turn_id": turn,
                "request_id": key, "reason": "Inspect silent execution", "wait_seconds": 0}, **kwargs)
        return self.cli("thread", "interrupt", "--session-id", thread, "--expected-turn-id", turn,
            "--request-id", key, "--reason", "Inspect silent execution", "--wait", "0", **kwargs)

    def resume(self, thread, turn, key, entry="cli"):
        if entry == "mcp":
            return self.mcp("resume_thread", {"session_id": thread, "expected_turn_id": turn,
                "request_id": key, "reason": "Inspected native process and tool outcomes",
                "confirm_stopped": True, "wait_seconds": 0})
        return self.cli("thread", "resume", "--session-id", thread, "--expected-turn-id", turn,
            "--request-id", key, "--reason", "Inspected native process and tool outcomes",
            "--confirm-stopped", "--wait", "0")

    def process(self, key):
        self.worker.process_once()
        self.assertTrue(self.sessions.persistence.flush())
        return self.control.status(key)

    def wake(self, thread):
        with self.store.reading(), self.store._connection() as db:
            return dict(db.execute("SELECT * FROM browser_inbox_wake WHERE thread_id=?", (thread,)).fetchone())

    def test_public_interrupt_and_resume_for_both_providers_and_entry_points(self):
        for client in ("codex", "claude"):
            for entry in ("cli", "mcp"):
                with self.subTest(client=client, entry=entry):
                    thread, turn = self.active_agent(client)
                    key = client + entry
                    self.interrupt(thread, turn, key, entry)
                    result = self.process(key)
                    self.assertEqual(result["status"], "completed", result)
                    self.assertEqual(result["result"]["interrupted_turn_id"], turn)
                    self.assertEqual(self.wake(thread)["pause_kind"], "supervisor")
                    self.assertFalse(self.store.threads.stop_requested(thread, turn))
                    self.assertNotIn(thread, self.sessions.active)
                    self.assertEqual(self.interrupt(thread, turn, key, entry)["status"], "completed")
                    with self.store.reading(), self.store._connection() as db:
                        audit = db.execute("SELECT * FROM supervisor_interrupts WHERE request_id=?", (key,)).fetchone()
                        self.assertEqual((audit["thread_id"], audit["turn_id"], audit["requester"]),
                                         (thread, turn, "dispatcher"))
                        self.assertEqual(audit["reason"], "Inspect silent execution")
                    self.continuation(thread)
                    self.resume(thread, turn, key + "-resume", entry)
                    result = self.process(key + "-resume")
                    self.assertEqual(result["status"], "completed", result)
                    self.sessions.inbox_wake.process_once()
                    self.assertTrue(self.sessions.read(thread)["running"])
                    self.assertNotEqual(self.sessions.active[thread], turn)
                    self.assertIn("Inspect existing results", str(self.deliver(thread)))
                    # A retry after resumption never targets the new turn.
                    newer = self.sessions.active[thread]
                    self.interrupt(thread, turn, key, entry)
                    self.worker.process_once()
                    self.assertEqual(self.sessions.active[thread], newer)

    def test_stale_turn_and_reused_key_cannot_target_new_turn(self):
        for client in ("codex", "claude"):
            thread, turn = self.active_agent(client)
            key = "stale-" + client
            self.interrupt(thread, "obsolete", key)
            result = self.process(key)
            self.assertEqual(result["status"], "failed", result)
            self.assertEqual(self.sessions.active[thread], turn)
            result = self.interrupt(thread, turn, key, expected=2)
            self.assertIn("different app request", result["error"])
            self.assertFalse(self.wake(thread)["paused"])

    def test_only_current_supervisor_can_interrupt_and_mcp_cannot_spoof_caller(self):
        thread, turn = self.active_agent("claude")
        self.store.register(session_id="unrelated", client="codex", cwd=str(self.root))
        SessionControl(self.store).adopt("unrelated", thread)
        self.assertIn("supervisor", self.interrupt(thread, turn, "unauthorized", caller="unrelated", expected=2)["error"])
        self.mcp("interrupt_thread", {"session_id": thread, "expected_turn_id": turn,
            "request_id": "spoof", "reason": "Test", "from_session": "dispatcher"}, caller="unrelated", error=True)
        self.assertEqual(self.sessions.active[thread], turn)
        # Interactive adoption cannot block the actual supervisor's stop control.
        self.interrupt(thread, turn, "authorized")
        self.assertEqual(self.process("authorized")["status"], "completed")

    def test_changed_supervisor_rejects_queued_interrupt(self):
        thread, turn = self.active_agent("codex")
        self.interrupt(thread, turn, "ownership-change")
        self.store.register(session_id="successor", client="codex", cwd=str(self.root))
        with self.store._connection() as db:
            from agent_coord.supervision import ensure_schema
            ensure_schema(db)
            db.execute("INSERT INTO agent_supervisors(thread_id,supervisor_session_id,transfer_id,updated_at) VALUES (?,?,?,?)",
                       (thread, "successor", "test-transfer", self.store.clock()))
        result = self.process("ownership-change")
        self.assertEqual(result["status"], "failed", result)
        self.assertIn("supervisor", result["error"])
        self.assertEqual(self.sessions.active[thread], turn)

    def test_uncertain_interrupt_is_never_retried_even_with_a_new_key(self):
        for client in ("codex", "claude"):
            thread, turn = self.active_agent(client)
            rpc = self.sessions._rpc(thread)
            native = rpc.request
            attempts = []
            def uncertain(method, params, **kwargs):
                if method == "turn/interrupt":
                    attempts.append(params)
                    raise TimeoutError("Native interrupt outcome unknown")
                return native(method, params, **kwargs)
            key = "uncertain-" + client
            with patch.object(rpc, "request", side_effect=uncertain):
                self.interrupt(thread, turn, key)
                self.assertEqual(self.process(key)["status"], "uncertain")
                self.interrupt(thread, turn, key)
                self.assertEqual(self.process(key)["status"], "uncertain")
                self.interrupt(thread, turn, key + "-new")
                self.assertEqual(self.process(key + "-new")["status"], "failed")
            self.assertEqual(len(attempts), 1)
            self.assertEqual(self.wake(thread)["pause_kind"], "supervisor")

    def test_lost_response_after_applied_interrupt_is_recoverable_without_replay(self):
        thread, turn = self.active_agent("claude")
        rpc = self.sessions._rpc(thread)
        native = rpc.request
        def uncertain(method, params, **kwargs):
            result = native(method, params, **kwargs)
            if method == "turn/interrupt":
                raise TimeoutError("Response lost")
            return result
        with patch.object(rpc, "request", side_effect=uncertain):
            self.interrupt(thread, turn, "lost-response")
            self.assertEqual(self.process("lost-response")["status"], "uncertain")
        self.continuation(thread)
        self.resume(thread, turn, "lost-response-recovery", "mcp")
        self.assertEqual(self.process("lost-response-recovery")["status"], "completed")

    def test_resume_requires_post_interrupt_instructions_and_native_confirmation(self):
        thread, turn = self.active_agent("codex")
        self.continuation(thread)  # Old queued instructions cannot authorize recovery.
        self.interrupt(thread, turn, "fresh-only")
        self.assertEqual(self.process("fresh-only")["status"], "completed")
        self.resume(thread, turn, "no-fresh-work")
        self.assertEqual(self.process("no-fresh-work")["status"], "failed")
        self.continuation(thread)
        self.cli("thread", "resume", "--session-id", thread, "--expected-turn-id", turn,
                 "--request-id", "no-evidence", "--wait", "0")
        self.assertIn("confirm-stopped", self.process("no-evidence")["error"])
        self.resume(thread, turn, "with-evidence")
        self.assertEqual(self.process("with-evidence")["status"], "completed")

    def test_genuine_user_stop_before_or_after_supervisor_stop_remains_protected(self):
        for before in (True, False):
            thread, turn = self.active_agent("claude")
            key = "user-stop-" + str(before)
            self.interrupt(thread, turn, key)
            if before:
                self.sessions.interrupt(thread)
            self.process(key)
            if not before:
                self.sessions.inbox_wake.pause(thread, "User stopped", kind="stop")
                self.store.threads.request_stop(thread, turn)
            self.continuation(thread)
            self.resume(thread, turn, key + "-resume")
            self.assertEqual(self.process(key + "-resume")["status"], "failed")
            self.assertEqual(self.wake(thread)["pause_kind"], "stop")

    def test_interrupt_request_is_hidden_from_older_runtime_and_cancellable(self):
        thread, turn = self.active_agent("codex")
        self.interrupt(thread, turn, "cancel")
        with self.store.reading(), self.store._connection() as db:
            self.assertEqual(db.execute("SELECT status FROM app_control_requests WHERE request_id='cancel'").fetchone()[0],
                             "queued_interrupt")
        self.cli("thread", "cancel-request", "--request-id", "cancel")
        self.assertEqual(self.process("cancel")["status"], "cancelled")
        self.assertEqual(self.sessions.active[thread], turn)

    def test_public_interrupt_requires_nonempty_immutable_turn_request_and_reason(self):
        thread, turn = self.active_agent("claude")
        fields = {"session_id": thread, "expected_turn_id": turn, "request_id": "valid", "reason": "Inspect"}
        for name in ("expected_turn_id", "request_id", "reason"):
            for value in (None, "", "   "):
                with self.subTest(name=name, value=value):
                    self.mcp("interrupt_thread", {**fields, name: value}, error=True)

    def test_mcp_catalog_advertises_both_controls(self):
        result = dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        catalog = {tool["name"]: tool for tool in result["result"]["tools"]}
        self.assertIn("interrupt_thread", catalog)
        self.assertIn("resume_thread", catalog)
        self.assertIn("expected_turn_id", catalog["interrupt_thread"]["inputSchema"]["required"])
        self.assertIn("request_id", catalog["interrupt_thread"]["inputSchema"]["required"])

    def test_cli_wait_releases_database_ownership_for_interrupt_worker(self):
        thread, turn = self.active_agent("claude")
        self.worker.start()
        result = self.cli("thread", "interrupt", "--session-id", thread, "--expected-turn-id", turn,
                          "--request-id", "wait-cli", "--reason", "Inspect", "--wait", "3")
        self.assertEqual(result["status"], "completed", result)

    def test_stdio_mcp_wait_releases_ownership_and_binds_host_identity(self):
        thread, turn = self.active_agent("codex")
        self.worker.start()
        message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "interrupt_thread", "_meta": {"threadId": "dispatcher"}, "arguments": {
                "session_id": thread, "expected_turn_id": turn, "request_id": "wait-mcp",
                "reason": "Inspect", "wait_seconds": 3}}}
        result = subprocess.run([sys.executable, str(PLUGIN_SCRIPTS / "agent-coord-mcp")],
            input=json.dumps(message) + "\n", env=dict(os.environ, AGENT_COORD_DB=str(self.store.database_path),
            AGENT_COORD_SESSION_ID="stale-inherited-caller"), cwd=self.root, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        response = json.loads(result.stdout)
        self.assertFalse(response["result"].get("isError"), response)
        receipt = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(receipt["status"], "completed", receipt)

    def test_audit_failure_prevents_native_interrupt_and_pause(self):
        thread, turn = self.active_agent("codex")
        with self.store._connection() as db:
            db.execute("CREATE TRIGGER reject_interrupt BEFORE INSERT ON supervisor_interrupts BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END")
        self.interrupt(thread, turn, "audit-failure")
        self.assertEqual(self.process("audit-failure")["status"], "failed")
        self.assertEqual(self.sessions.active[thread], turn)
        self.assertFalse(self.wake(thread)["paused"])

    def test_user_origin_native_completion_does_not_reclassify_supervisor_as_user(self):
        thread, turn = self.active_agent("codex")
        self.sessions.rpc.threads[thread]["turns"][-1]["termination"] = {"origin": "user"}
        self.interrupt(thread, turn, "provider-user-origin")
        self.assertEqual(self.process("provider-user-origin")["status"], "completed")
        self.assertFalse(self.store.threads.stop_requested(thread, turn))
        self.assertEqual(self.wake(thread)["pause_kind"], "supervisor")

    def test_legacy_adopted_interactive_controller_keeps_existing_stop_semantics(self):
        thread = self.sessions.create({"client": "codex", "cwd": str(self.root)})["session"]["thread_id"]
        SessionControl(self.store).adopt("dispatcher", thread)
        self.sessions.send(thread, {"message": "Investigate"})
        turn = self.sessions.active[thread]
        self.cli("session-control", "interrupt", "--thread", thread, "--expected-turn-id", turn,
                 "--request-id", "legacy", "--wait", "0")
        self.assertEqual(self.process("legacy")["status"], "completed")
        self.assertEqual(self.wake(thread)["pause_kind"], "stop")
        self.assertTrue(self.store.threads.stop_requested(thread, turn))

    def test_native_completion_racing_interrupt_allows_guarded_supervisor_recovery(self):
        for terminal_status in ("completed", "failed"):
            thread, turn = self.active_agent("codex")
            native = self.sessions.rpc.request
            def completion(method, params, **kwargs):
                if method != "turn/interrupt":
                    return native(method, params, **kwargs)
                latest = self.sessions.rpc.threads[thread]["turns"][-1]
                latest["status"] = terminal_status
                self.sessions._event({"method": "turn/completed", "params": {"threadId": thread, "turn": latest}})
                return {}
            key = "race-" + terminal_status
            with patch.object(self.sessions.rpc, "request", side_effect=completion):
                self.interrupt(thread, turn, key)
                self.assertEqual(self.process(key)["status"], "completed")
            self.continuation(thread)
            self.cli("thread", "resume", "--session-id", thread, "--expected-turn-id", turn,
                     "--request-id", key + "-no-evidence", "--wait", "0")
            self.assertEqual(self.process(key + "-no-evidence")["status"], "failed")
            self.resume(thread, turn, key + "-resume")
            result = self.process(key + "-resume")
            self.assertEqual(result["status"], "completed", result)

    def test_other_runtime_does_not_claim_interrupt_for_loaded_owner(self):
        thread, turn = self.active_agent("claude")
        self.interrupt(thread, turn, "runtime-owner")
        other_sessions, other_worker = self.runtime()
        other_worker.process_once()
        self.assertEqual(self.control.status("runtime-owner")["status"], "queued")
        self.assertNotIn(thread, other_sessions.loaded)
        self.assertEqual(self.process("runtime-owner")["status"], "completed")

    def test_restart_preserves_uncertain_receipt_and_never_reissues_interrupt(self):
        from agent_coord.app_control import AppControlWorker
        thread, turn = self.active_agent("codex")
        native = self.sessions.rpc.request
        def timeout(method, params, **kwargs):
            if method == "turn/interrupt":
                raise TimeoutError("No receipt")
            return native(method, params, **kwargs)
        with patch.object(self.sessions.rpc, "request", side_effect=timeout):
            self.interrupt(thread, turn, "restart")
            self.assertEqual(self.process("restart")["status"], "uncertain")
        self.worker.close()
        self.worker = AppControlWorker(self.sessions)
        self.addCleanup(self.worker.close)
        self.interrupt(thread, turn, "restart")
        self.assertEqual(self.process("restart")["status"], "uncertain")
        self.interrupt(thread, turn, "restart-new")
        self.assertEqual(self.process("restart-new")["status"], "failed")
        self.assertEqual(self.sessions.active[thread], turn)

    def test_pre_upgrade_interactive_interrupt_receipts_remain_idempotent(self):
        for status in ("queued_session", "completed", "uncertain"):
            thread, turn = self.active_agent("codex")
            key = "pre-upgrade-" + status
            payload = {"action": "interrupt", "expected_turn_id": turn}
            with self.store._connection() as db:
                db.execute("""INSERT INTO app_control_requests
                    (request_id,operation,sender_session_id,cwd,thread_id,payload_json,status,created_at,updated_at)
                    VALUES (?,'session','dispatcher',?,?,?,?,0,0)""",
                    (key, str(self.root), thread, json.dumps(payload), status))
            result = self.cli("session-control", "interrupt", "--thread", thread,
                             "--expected-turn-id", turn, "--request-id", key, "--wait", "0")
            if status == "queued_session":
                self.assertEqual(self.process(key)["status"], "completed")
                self.assertEqual(self.wake(thread)["pause_kind"], "supervisor")
            else:
                self.assertEqual(result["status"], status)
                self.worker.process_once()
                self.assertEqual(self.sessions.active[thread], turn)

    def test_original_session_control_interrupt_routes_current_supervisor_for_both_providers(self):
        for client in ("claude", "codex"):
            thread, turn = self.active_agent(client)
            key = "original-" + client
            self.cli("session-control", "interrupt", "--thread", thread, "--expected-turn-id", turn,
                     "--request-id", key, "--wait", "0")
            result = self.process(key)
            self.assertEqual(result["status"], "completed", result)
            self.assertEqual(self.wake(thread)["pause_kind"], "supervisor")
            self.continuation(thread)
            self.resume(thread, turn, key + "-resume")
            self.assertEqual(self.process(key + "-resume")["status"], "completed")
