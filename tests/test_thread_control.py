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

    def complete_turn(self, thread_id):
        turn = self.sessions.rpc.threads[thread_id]["turns"][-1]
        turn["status"] = "completed"
        self.sessions._event({"method": "turn/completed", "params": {"threadId": thread_id, "turn": turn}})

    def test_cli_after_turn_is_durable_and_preserves_history_checkpoint_and_other_threads(self):
        thread_id, other = self.create(), self.create()
        self.sessions.send(thread_id, {"message": "Keep this conversation."})
        self.sessions.send(other, {"message": "Keep working."})
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
        self.sessions.send(thread_id, {"message": "Working"})
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
        self.sessions.send(thread_id, {"message": "Working"})
        self.control.request_close(thread_id, after_turn=True)
        turn_key = self.store.threads.get(thread_id)["turn_key"]
        self.sessions.send(thread_id, {"message": "Actually, let's pivot."})
        self.assertEqual(self.store.threads.get(thread_id)["turn_key"], turn_key)
        self.assertEqual(self.control.status(thread_id)["status"], "cancelled")
        self.complete_turn(thread_id)
        self.worker.process_once()
        self.assertEqual(self.store.threads.get(thread_id)["attention"], "now")

    def test_queued_followup_cancels_close_before_dispatch(self):
        thread_id = self.create()
        self.sessions.send(thread_id, {"message": "Working"})
        self.control.request_close(thread_id, after_turn=True)
        self.sessions.queue.enqueue(thread_id, {"message": "One more thing"})
        self.assertEqual(self.control.status(thread_id)["status"], "cancelled")
        self.assertEqual(self.sessions.queue.list(thread_id)[0]["state"], "queued")

    def test_new_turn_cancels_but_duplicate_start_notification_does_not(self):
        thread_id = self.create()
        self.sessions.send(thread_id, {"message": "Working"})
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
        self.sessions.send(thread_id, {"message": "Working"})
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
        self.sessions.send(thread_id, {"message": "Finish before closing."})
        self.cli("close", thread_id, "--after-turn")
        self.complete_turn(thread_id)
        deadline = time.monotonic() + 5
        while self.control.status(thread_id)["status"] == "queued" and time.monotonic() < deadline:
            threading.Event().wait(.02)
        # Await any in-flight archive too; a receipt never masquerades as completion.
        while self.control.status(thread_id)["status"] == "closing" and time.monotonic() < deadline:
            threading.Event().wait(.02)
        self.assertEqual(self.cli("close-status", thread_id)["status"], "closed")
