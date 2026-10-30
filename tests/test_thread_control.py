from __future__ import annotations

import json
import subprocess
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import test_codex_app_server
from agent_coord.codex_app_server import BrowserSessions
from agent_coord.hook import handle
from agent_coord.store import CoordinationError, CoordinationStore
from agent_coord.thread_control import ThreadControl, ThreadControlWorker
from agent_coord.ui import make_ui_server


class ThreadControlTests(unittest.TestCase):
    create = test_codex_app_server.BrowserSessionTests.create

    def setUp(self):
        test_codex_app_server.BrowserSessionTests.setUp(self)
        self.worker = ThreadControlWorker(self.sessions)
        self.addCleanup(self.worker.close)
        self.control = self.worker.control

    def cli(self, command, thread_id, *arguments):
        result = subprocess.run([
            str(test_codex_app_server.PLUGIN_SCRIPTS / "agent-coord"),
            "--db", str(self.root / "coord.sqlite3"), "thread", command,
            "--session-id", thread_id, *arguments,
        ], capture_output=True, text=True, timeout=10, cwd=self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def send(self, thread_id, body):
        result = self.sessions.send(thread_id, body)
        # The fake provider acknowledges before its asynchronous lifecycle
        # bookkeeping commits. These tests then exercise durable close state.
        self.assertTrue(self.sessions.persistence.flush(key=thread_id))
        return result

    def complete_turn(self, thread_id, status="completed"):
        turn = self.sessions.rpc.threads[thread_id]["turns"][-1]
        turn["status"] = status
        self.sessions._event({"method": "turn/completed", "params": {"threadId": thread_id, "turn": turn}})
        self.assertTrue(self.sessions.persistence.flush(key=thread_id))

    def arm_success_close(self):
        thread_id = self.create()
        self.send(thread_id, {"message": "Implement the fix."})
        self.control.request_close(thread_id, after_turn=True, success_only=True)
        return thread_id

    def checkpoint_done(self, thread_id, **fields):
        self.store.threads.checkpoint(thread_id, {"phase": "finished", "summary": "Implemented and tested.",
                                                 "next_step": None, **fields})

    def classify_reply(self, thread_id, choice):
        thread = self.store.threads.get(thread_id)
        token = self.store.threads.attention.claim(thread)
        self.store.threads.attention.save(thread, token, result={"choice": choice, "confidence": .99,
                                                              "probabilities": {choice: .99}})

    def test_close_when_done_waits_for_success_and_preserves_history(self):
        thread_id = self.arm_success_close()
        receipt = self.control.status(thread_id)
        self.assertTrue(receipt["success_only"])
        self.assertEqual(ThreadControl(CoordinationStore(self.root / "coord.sqlite3")).status(thread_id), receipt)
        self.checkpoint_done(thread_id)
        self.worker.process_once()
        self.assertEqual(self.control.status(thread_id)["status"], "queued")
        self.complete_turn(thread_id)
        self.worker.process_once()
        self.assertEqual(self.control.status(thread_id)["status"], "closed")
        self.assertEqual(self.store.threads.get(thread_id)["original_request"], "Implement the fix.")
        self.assertFalse(any(method == "turn/interrupt" for method, _ in self.sessions.rpc.calls))

    def test_close_when_done_keeps_failed_and_interrupted_turns_open(self):
        for status in ("failed", "interrupted"):
            with self.subTest(status=status):
                thread_id = self.arm_success_close()
                self.checkpoint_done(thread_id)
                self.complete_turn(thread_id, status)
                self.worker.process_once()
                self.assertEqual(self.control.status(thread_id)["status"], "cancelled")
                self.assertEqual(self.store.threads.get(thread_id)["attention"], "now")

    def test_close_when_done_keeps_required_actions_and_unconfirmed_results_open(self):
        for fields in ({}, {"phase": "implementation"},
                       *({"next_step": {"actor": actor, "action": "Required action."}}
                         for actor in ("user", "external", "agent"))):
            with self.subTest(fields=fields):
                thread_id = self.arm_success_close()
                if fields:
                    self.checkpoint_done(thread_id, **fields)
                self.complete_turn(thread_id)
                self.worker.process_once()
                self.assertEqual(self.control.status(thread_id)["status"], "cancelled")
                self.assertEqual(self.store.threads.get(thread_id)["attention"], "now")

    def test_close_when_done_does_not_trust_a_previous_turns_finished_checkpoint(self):
        thread_id = self.create()
        self.checkpoint_done(thread_id)
        self.send(thread_id, {"message": "Implement another fix."})
        self.control.request_close(thread_id, after_turn=True, success_only=True)
        self.complete_turn(thread_id)
        self.worker.process_once()
        self.assertEqual(self.control.status(thread_id)["status"], "cancelled")

    def test_close_when_done_waits_for_pending_approval_even_after_completion(self):
        thread_id = self.arm_success_close()
        self.checkpoint_done(thread_id)
        self.sessions._event({"id": 123, "method": "item/commandExecution/requestApproval",
                              "params": {"threadId": thread_id}})
        self.worker.process_once()
        self.assertEqual(self.control.status(thread_id)["status"], "queued")
        self.complete_turn(thread_id)
        # Providers normally clear approvals on completion; protect against an
        # outstanding request reported by a runtime during that transition.
        with patch.object(self.sessions, "pending_requests", return_value=[{"id": 123}]):
            self.worker.process_once()
            self.assertEqual(self.store.threads.get(thread_id)["attention"], "now")
            self.assertEqual(self.control.status(thread_id)["status"], "queued")

    def test_close_when_done_waits_for_classification_and_respects_blocked_reply(self):
        thread_id = self.arm_success_close()
        self.checkpoint_done(thread_id)
        self.complete_turn(thread_id)
        with patch("agent_coord.thread_control.load_config", return_value={"enabled": True}):
            self.worker.process_once()
        self.assertEqual(self.control.status(thread_id)["status"], "queued")
        self.classify_reply(thread_id, "blocked")
        self.worker.process_once()
        self.assertEqual(self.control.status(thread_id)["status"], "cancelled")
        self.assertEqual(self.store.threads.get(thread_id)["attention"], "now")

    def test_close_when_done_accepts_classified_success_without_checkpoint(self):
        thread_id = self.arm_success_close()
        self.complete_turn(thread_id)
        self.classify_reply(thread_id, "done")
        self.worker.process_once()
        self.assertEqual(self.control.status(thread_id)["status"], "closed")

    def test_close_when_done_keeps_review_update_and_uncertain_classification_open(self):
        for choice in ("review", "update", "findings"):
            with self.subTest(choice=choice):
                thread_id = self.arm_success_close()
                self.checkpoint_done(thread_id)
                self.complete_turn(thread_id)
                self.classify_reply(thread_id, choice)
                self.worker.process_once()
                self.assertEqual(self.control.status(thread_id)["status"], "cancelled")
        thread_id = self.arm_success_close()
        self.checkpoint_done(thread_id)
        self.complete_turn(thread_id)
        thread = self.store.threads.get(thread_id)
        token = self.store.threads.attention.claim(thread)
        self.worker.process_once()
        self.assertEqual(self.control.status(thread_id)["status"], "queued")
        self.store.threads.attention.save(thread, token, error="context_unavailable")
        self.worker.process_once()
        self.assertEqual(self.control.status(thread_id)["status"], "cancelled")

    def test_close_when_done_can_be_disabled_and_new_input_cancels_it(self):
        for action in ("cancel", "steer", "queue"):
            with self.subTest(action=action):
                thread_id = self.arm_success_close()
                self.checkpoint_done(thread_id)
                if action == "cancel":
                    self.control.cancel(thread_id)
                elif action == "steer":
                    self.send(thread_id, {"message": "Another thing."})
                else:
                    self.sessions.queue.enqueue(thread_id, {"message": "Follow up."})
                self.assertEqual(self.control.status(thread_id)["status"], "cancelled")
                self.complete_turn(thread_id)
                self.worker.process_once()
                self.assertEqual(self.store.threads.get(thread_id)["attention"], "now")

    def test_close_when_done_requires_active_turn_and_does_not_replace_explicit_close(self):
        thread_id = self.create()
        with self.assertRaisesRegex(CoordinationError, "Start a task"):
            self.control.request_close(thread_id, after_turn=True, success_only=True)
        self.send(thread_id, {"message": "Fix it."})
        self.control.request_close(thread_id, after_turn=True)
        with self.assertRaisesRegex(CoordinationError, "different close"):
            self.control.request_close(thread_id, after_turn=True, success_only=True)

    def test_close_when_done_keeps_thread_open_if_classifier_never_finishes(self):
        thread_id = self.arm_success_close()
        self.checkpoint_done(thread_id)
        self.complete_turn(thread_id)
        with self.store._connection() as db:
            db.execute("UPDATE turn_completions SET completed_at = completed_at - 61 WHERE thread_id = ?", (thread_id,))
        with patch("agent_coord.thread_control.load_config", return_value={"enabled": True}):
            self.worker.process_once()
        self.assertEqual(self.control.status(thread_id)["status"], "cancelled")
        self.assertEqual(self.store.threads.get(thread_id)["attention"], "now")

    def test_upgrading_existing_close_requests_preserves_their_explicit_behavior(self):
        with self.store._connection() as db:
            db.execute("ALTER TABLE thread_close_requests DROP COLUMN success_only")
        control = ThreadControl(self.store)
        thread_id = self.create()
        self.send(thread_id, {"message": "Working."})
        control.request_close(thread_id, after_turn=True)
        self.complete_turn(thread_id, "failed")
        self.worker.process_once()
        self.assertEqual(control.status(thread_id)["status"], "closed")

    def test_cli_after_turn_is_durable_and_preserves_history_checkpoint_and_other_threads(self):
        thread_id, other = self.create(), self.create()
        self.send(thread_id, {"message": "Keep this conversation."})
        self.send(other, {"message": "Keep working."})
        self.store.begin_work(session_id=thread_id, scopes=["src/**"])
        self.store.threads.checkpoint(thread_id, {"phase": "discussion", "summary": "A proposal, still unapproved."})
        receipt = self.cli("close", thread_id, "--after-turn")
        self.assertEqual((receipt["status"], receipt["waiting_for"]), ("queued", "turn"))
        reopened = ThreadControl(CoordinationStore(self.root / "coord.sqlite3"))
        self.assertEqual(reopened.status(thread_id)["request_id"], receipt["request_id"])
        self.worker.process_once()
        self.assertIn(thread_id, self.sessions.active)
        self.complete_turn(thread_id)
        self.worker.process_once()
        self.assertEqual(self.cli("close-status", thread_id)["status"], "closed")
        thread = self.store.threads.get(thread_id)
        self.assertEqual(thread["attention"], "archived")
        self.assertEqual(thread["checkpoint"]["phase"], "discussion")
        self.assertEqual(thread["original_request"], "Keep this conversation.")
        self.assertEqual(self.store.get_session(thread_id)["write_scope"], [])
        self.assertEqual(self.sessions.read(thread_id)["thread"]["turns"][0]["status"], "completed")
        self.assertIn(other, self.sessions.active)
        self.assertFalse(any(method == "turn/interrupt" for method, _ in self.sessions.rpc.calls))

    def test_immediate_close_uses_existing_interrupt_then_archive(self):
        thread_id = self.create()
        self.send(thread_id, {"message": "Working"})
        self.cli("close", thread_id)
        self.worker.process_once()
        self.assertEqual(self.control.status(thread_id)["status"], "closed")
        methods = [method for method, _ in self.sessions.rpc.calls]
        self.assertLess(methods.index("turn/interrupt"), methods.index("thread/archive"))
        self.assertEqual(self.cli("close", thread_id)["status"], "closed")
        self.assertEqual(methods.count("thread/archive"), 1)

    def test_duplicate_and_cancel_commands(self):
        thread_id = self.create()
        first = self.cli("close", thread_id, "--after-turn")
        second = self.cli("close", thread_id, "--after-turn")
        self.assertEqual(first["request_id"], second["request_id"])
        with self.assertRaisesRegex(CoordinationError, "different close"):
            self.control.request_close(thread_id)
        self.assertEqual(self.cli("cancel-close", thread_id)["status"], "cancelled")
        self.worker.process_once()
        self.assertEqual(self.store.threads.get(thread_id)["attention"], "now")

    def test_steering_cancels_close_even_when_turn_identity_is_unchanged(self):
        thread_id = self.create()
        self.send(thread_id, {"message": "Working"})
        self.control.request_close(thread_id, after_turn=True)
        turn_key = self.store.threads.get(thread_id)["turn_key"]
        self.send(thread_id, {"message": "Actually, let's pivot."})
        self.assertEqual(self.store.threads.get(thread_id)["turn_key"], turn_key)
        self.assertEqual(self.control.status(thread_id)["status"], "cancelled")
        self.complete_turn(thread_id)
        self.worker.process_once()
        self.assertEqual(self.store.threads.get(thread_id)["attention"], "now")

    def test_queued_followup_cancels_close_before_dispatch(self):
        thread_id = self.create()
        self.send(thread_id, {"message": "Working"})
        self.control.request_close(thread_id, after_turn=True)
        self.sessions.queue.enqueue(thread_id, {"message": "One more thing"})
        self.assertEqual(self.control.status(thread_id)["status"], "cancelled")
        self.assertEqual(self.sessions.queue.list(thread_id)[0]["state"], "queued")

    def test_new_turn_cancels_but_duplicate_start_notification_does_not(self):
        thread_id = self.create()
        self.send(thread_id, {"message": "Working"})
        self.control.request_close(thread_id, after_turn=True)
        turn_id = self.store.threads.get(thread_id)["turn_id"]
        self.store.threads.start_turn(thread_id, turn_id=turn_id)
        self.assertEqual(self.control.status(thread_id)["status"], "queued")
        self.store.threads.start_turn(thread_id, turn_id="new-turn")
        self.assertEqual(self.control.status(thread_id)["status"], "cancelled")

    def test_reconcile_detects_new_turn_from_older_client(self):
        thread_id = self.create()
        self.control.request_close(thread_id, after_turn=True)
        with self.store._connection() as db:
            db.execute("UPDATE work_threads SET turn_key = 'new' WHERE thread_id = ?", (thread_id,))
        self.worker.process_once()
        self.assertEqual(self.control.status(thread_id)["status"], "cancelled")

    def test_early_browser_stop_hook_cannot_close_before_provider_completion(self):
        thread_id = self.create()
        self.send(thread_id, {"message": "Working"})
        self.control.request_close(thread_id, after_turn=True)
        handle({"hook_event_name": "Stop", "session_id": thread_id, "cwd": str(self.root)}, self.store)
        self.worker.process_once()
        self.assertEqual(self.control.status(thread_id)["status"], "queued")
        self.assertIn(thread_id, self.sessions.active)
        self.complete_turn(thread_id)
        self.worker.process_once()
        self.assertEqual(self.control.status(thread_id)["status"], "closed")

    def test_failure_is_reported_and_keeps_scope_and_visibility_then_can_retry(self):
        thread_id = self.create()
        self.store.begin_work(session_id=thread_id, scopes=["src/**"])
        first = self.control.request_close(thread_id)
        with patch.object(self.sessions.rpc, "request", side_effect=CoordinationError("Archive failed")):
            self.worker.process_once()
        status = self.control.status(thread_id)
        self.assertEqual((status["status"], status["error"]), ("failed", "Archive failed"))
        self.assertEqual(self.store.threads.get(thread_id)["attention"], "now")
        self.assertEqual(self.store.get_session(thread_id)["write_scope"], ["src/**"])
        retry = self.control.request_close(thread_id)
        self.assertNotEqual(first["request_id"], retry["request_id"])
        self.worker.process_once()
        self.assertEqual(self.control.status(thread_id)["status"], "closed")

    def test_offline_terminal_closes_without_runtime(self):
        self.store.register(session_id="terminal", client="codex", cwd=str(self.root))
        self.store.end_session("terminal")
        self.assertEqual(self.cli("close", "terminal")["status"], "closed")
        self.assertEqual(self.store.threads.get("terminal")["attention"], "archived")
        self.assertEqual(self.sessions.rpc.calls, [])

    def test_terminal_uses_existing_process_ownership_guard(self):
        self.store.register(session_id="terminal", client="codex", cwd=str(self.root))
        self.control.request_close("terminal")
        with patch("agent_coord.codex_app_server.stop_terminal_session", side_effect=CoordinationError("Exit in terminal")):
            self.worker.process_once()
        self.assertEqual(self.control.status("terminal")["error"], "Exit in terminal")
        self.assertEqual(self.store.threads.get("terminal")["attention"], "now")

    def test_external_close_is_durable_and_idempotent_without_ending_its_process(self):
        thread_id = "01a13b94-9dd4-7b12-acf6-7956501ddaf7"
        transcript = self.root / "sessions" / ("rollout-" + thread_id + ".jsonl")
        transcript.parent.mkdir()
        transcript.write_text(json.dumps({"type": "session_meta", "payload": {
            "id": thread_id, "source": "vscode"}}) + "\n")
        self.store.register(session_id=thread_id, client="codex", cwd=str(self.root))
        self.store.threads.start_turn(thread_id, turn_id="external-turn")
        self.store.touch(thread_id, turn_active=True)
        with patch.dict("os.environ", {"CODEX_HOME": str(self.root)}), \
                patch("agent_coord.session_close.os.kill") as kill:
            self.assertEqual(self.cli("close", thread_id)["status"], "queued")
            self.worker.process_once()
            self.assertEqual(self.cli("close-status", thread_id)["status"], "closed")
            self.assertEqual(self.cli("close", thread_id)["status"], "closed")
            # Closure through the UI may not have a durable CLI receipt.
            with self.store._connection() as db:
                db.execute("DELETE FROM thread_close_requests WHERE thread_id = ?", (thread_id,))
            self.assertEqual(self.cli("close-status", thread_id)["status"], "closed")
            self.sessions.reopen_work_thread(thread_id)
            request = self.control.request_close(thread_id)
            self.assertTrue(self.control.claim(request["request_id"], "dead-runtime"))
            self.sessions.close_work_thread(thread_id)
            with self.store._connection() as db:
                db.execute("UPDATE thread_close_requests SET updated_at = 0 WHERE thread_id = ?", (thread_id,))
            self.assertEqual(self.control.status(thread_id)["status"], "closed")
        kill.assert_not_called()
        self.assertTrue(self.store.get_session(thread_id)["turn_active"])
        self.assertIsNone(self.store.threads.get(thread_id)["turn_completion"])
        self.assertEqual(self.sessions.rpc.calls, [])

    def test_worker_does_not_take_another_workspace_or_live_browser_owner(self):
        thread_id = self.create()
        self.control.request_close(thread_id)
        for cwd in (self.root, self.root / "other"):
            with self.subTest(cwd=cwd):
                other = BrowserSessions(self.store, str(cwd), rpc_factory=test_codex_app_server.FakeCodex)
                worker = ThreadControlWorker(other)
                try:
                    worker.process_once()
                    self.assertEqual(self.control.status(thread_id)["status"], "queued")
                    self.assertEqual(other.rpc.calls, [])
                finally:
                    worker.close()
                    other.close()

    def test_database_owner_closes_cold_unended_session_without_starting_a_turn(self):
        thread_id = self.create()
        self.control.request_close(thread_id)
        with self.sessions.lock:
            self.sessions.loaded.discard(thread_id)
        self.assertIsNone(self.store.get_session(thread_id)["ended_at"])
        with patch("agent_coord.desktop_runtime._OWNED_DATABASES", {str(self.store.database_path.resolve())}):
            self.worker.process_once()
        self.assertEqual(self.control.status(thread_id)["status"], "closed")
        self.assertEqual(self.store.threads.get(thread_id)["attention"], "archived")
        self.assertFalse(any(method in {"turn/start", "thread/resume"} for method, _ in self.sessions.rpc.calls))

    def test_database_owner_does_not_assume_cold_active_execution_has_stopped(self):
        thread_id = self.create()
        self.send(thread_id, {"message": "Still running"})
        self.control.request_close(thread_id)
        with self.sessions.lock:
            self.sessions.loaded.discard(thread_id)
            self.sessions.active.pop(thread_id, None)
        with patch("agent_coord.desktop_runtime._OWNED_DATABASES", {str(self.store.database_path.resolve())}):
            self.worker.process_once()
        self.assertEqual(self.control.status(thread_id)["status"], "queued")
        self.assertTrue(self.store.get_session(thread_id)["turn_active"])

    def test_two_workers_can_claim_request_only_once(self):
        thread_id = self.create()
        request = self.control.request_close(thread_id)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda owner: self.control.claim(request["request_id"], owner), ["one", "two"]))
        self.assertEqual(sorted(results), [False, True])
        with self.assertRaisesRegex(CoordinationError, "already executing"):
            self.control.cancel(thread_id)

    def test_runtime_crash_is_reported_without_replaying_close(self):
        thread_id = self.create()
        request = self.control.request_close(thread_id)
        self.assertTrue(self.control.claim(request["request_id"], "dead-runtime"))
        with self.store._connection() as db:
            db.execute("UPDATE thread_close_requests SET updated_at = 0")
        status = self.control.status(thread_id)
        self.assertEqual(status["status"], "failed")
        self.assertIn("runtime stopped", status["error"])
        self.worker.process_once()
        self.assertEqual(self.sessions.rpc.calls[-1][0], "thread/start")

    def test_cli_works_with_fresh_runtime_without_server_url_or_browser_polling(self):
        thread_id = self.create()
        server = make_ui_server(self.store, port=0, cwd=str(self.root), browser_sessions=self.sessions)
        self.addCleanup(server.server_close)
        self.send(thread_id, {"message": "Finish before closing."})
        # Turn bookkeeping is asynchronous. A close queued before it lands records
        # no turn, and the late turn start then cancels it as superseding input
        # (seen on CI as 'cancelled' with turn_key null; tracked separately).
        recorded = time.monotonic() + 5
        while time.monotonic() < recorded:
            with self.store._connection() as db:
                if db.execute("SELECT turn_key FROM work_threads WHERE thread_id = ?", (thread_id,)).fetchone()["turn_key"]:
                    break
            threading.Event().wait(.02)
        self.cli("close", thread_id, "--after-turn")
        self.complete_turn(thread_id)
        deadline = time.monotonic() + 5
        while self.control.status(thread_id)["status"] == "queued" and time.monotonic() < deadline:
            threading.Event().wait(.02)
        # Await any in-flight archive too; a receipt never masquerades as completion.
        while self.control.status(thread_id)["status"] == "closing" and time.monotonic() < deadline:
            threading.Event().wait(.02)
        self.assertEqual(self.cli("close-status", thread_id)["status"], "closed")


class CloseWhenDoneHTTPTests(unittest.TestCase):
    send = ThreadControlTests.send
    import test_ui as _http
    setUp = _http.BrowserHTTPTests.setUp
    stop_server = _http.BrowserHTTPTests.stop_server
    request = _http.BrowserHTTPTests.request

    def test_enable_read_and_cancel_without_submitting_a_model_message(self):
        thread_id = self.sessions.create({"name": "Fix"})["session"]["thread_id"]
        self.send(thread_id, {"message": "Fix it."})
        path = "/api/browser/threads/" + thread_id + "/close-when-done"
        self.assertEqual(self.request(path)[1]["status"], "none")
        status, result = self.request(path, {"enabled": True})
        self.assertEqual(status, 200)
        self.assertTrue(result["success_only"])
        self.assertEqual(result["waiting_for"], "turn")
        self.assertEqual(self.request(path)[1]["request_id"], result["request_id"])
        self.assertEqual(self.request(path, {"enabled": True})[1]["request_id"], result["request_id"])
        self.assertEqual(self.request(path, {"enabled": False})[1]["status"], "cancelled")
        self.assertEqual(len(self.sessions.rpc.threads[thread_id]["turns"]), 1)

    def test_endpoint_rejects_invalid_settings_and_requires_csrf(self):
        thread_id = self.sessions.create({"name": "Fix"})["session"]["thread_id"]
        self.send(thread_id, {"message": "Fix it."})
        path = "/api/browser/threads/" + thread_id + "/close-when-done"
        for body in ({}, {"enabled": "true"}, {"enabled": True, "extra": True}):
            self.assertEqual(self.request(path, body)[0], 400)
        self.assertEqual(self.request(path, {"enabled": True}, {"X-Agent-Coord-Token": ""})[0], 403)

    def test_endpoint_requires_an_active_app_turn_and_restricts_workspace(self):
        thread_id = self.sessions.create({"name": "Fix"})["session"]["thread_id"]
        path = "/api/browser/threads/" + thread_id + "/close-when-done"
        self.assertEqual(self.request(path, {"enabled": True})[0], 400)
        self.store.register(session_id="outside", client="codex", cwd=str(self.root.parent))
        outside = "/api/browser/threads/outside/close-when-done"
        self.assertEqual(self.request(outside)[0], 400)
        self.assertEqual(self.request(outside, {"enabled": True})[0], 400)
