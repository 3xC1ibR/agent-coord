from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))

from agent_coord.codex_app_server import BrowserSessions
from agent_coord.store import CoordinationError, CoordinationStore
from agent_coord.ui import make_ui_server
from test_codex_app_server import FakeCodex


class ThreadHandlingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.now = 1000.0
        self.store = CoordinationStore(self.root / "state.sqlite3", clock=lambda: self.now)
        self.store.register(session_id="terminal", client="codex", cwd=str(self.root))
        self.sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(self.sessions.close)

    def result(self, turn="one", phase="finished", actor="nobody", status="completed", thread_id="terminal"):
        self.now += 1
        self.store.threads.start_turn(thread_id, prompt="Investigate.", turn_id=turn)
        self.store.threads.checkpoint(thread_id, {
            "phase": phase, "summary": "A recommendation to consider.", "next_actor": actor,
            "next_action": "Choose an option." if actor == "user" else "",
        })
        self.store.threads.finish_turn(thread_id, status=status)
        self.store.touch(thread_id, turn_active=False)
        return self.sessions.work_thread(thread_id)

    @staticmethod
    def receipt(thread):
        return {"handled": True, "handled_checkpoint_id": (thread["checkpoint"] or {}).get("id", 0),
                "handled_completion_id": (thread["turn_completion"] or {}).get("id", 0)}

    def test_handling_survives_restart_and_preserves_context_pin_and_placement(self):
        self.result()
        before = self.sessions.update_work_thread("terminal", {"pinned": True, "attention": "later"})
        handled = self.sessions.update_work_thread("terminal", self.receipt(before))
        self.assertEqual(handled["response_state"], "available")
        self.assertFalse(handled["unhandled_response"])
        self.assertFalse(handled["can_handle_response"])
        for key in ("attention", "pinned", "title", "original_request", "checkpoints", "links", "unread", "updated_at"):
            self.assertEqual(handled[key], before[key], key)
        reopened = BrowserSessions(CoordinationStore(self.store.database_path), str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(reopened.close)
        saved = reopened.update_work_thread("terminal", {"attention": "now"})
        self.assertFalse(saved["needs_attention"])
        self.assertTrue(saved["pinned"])
        self.assertEqual(saved["handled_completion_id"], before["turn_completion"]["id"])

    def test_reading_a_finished_recommendation_does_not_handle_it(self):
        self.result()
        read = self.sessions.update_work_thread("terminal", {"seen": True})
        self.assertFalse(read["unread"])
        self.assertTrue(read["needs_attention"])
        self.assertTrue(read["can_handle_response"])
        self.assertEqual(read["handled_completion_id"], 0)

    def test_delayed_and_duplicate_receipts_never_handle_a_newer_response(self):
        first = self.result()
        second = self.result("two")
        receipt = self.receipt(first)
        for _ in range(2):
            current = self.sessions.update_work_thread("terminal", receipt)
            self.assertTrue(current["needs_attention"])
            self.assertTrue(current["unhandled_response"])
            self.assertEqual(current["handled_completion_id"], first["turn_completion"]["id"])
        handled = self.sessions.update_work_thread("terminal", self.receipt(second))
        self.assertFalse(handled["needs_attention"])
        self.sessions.update_work_thread("terminal", receipt)
        self.assertFalse(self.sessions.work_thread("terminal")["needs_attention"])
        self.assertTrue(self.result("three")["needs_attention"])

    def test_followup_consumes_previous_response_only_when_a_turn_starts(self):
        thread_id = self.sessions.create({})["session"]["thread_id"]
        self.sessions.rpc.fast_turn = True
        self.sessions.send(thread_id, {"message": "Investigate."})
        before = self.sessions.work_thread(thread_id)
        self.assertTrue(before["needs_attention"])
        self.sessions.send(thread_id, {"message": "/help"})
        self.assertTrue(self.sessions.work_thread(thread_id)["needs_attention"])
        self.sessions.rpc.fail_turn = True
        with self.assertRaises(CoordinationError):
            self.sessions.send(thread_id, {"message": "Continue."})
        self.assertTrue(self.sessions.work_thread(thread_id)["needs_attention"])
        self.sessions.rpc.fail_turn = False
        self.sessions.rpc.fast_turn = False
        self.sessions.send(thread_id, {"message": "Continue."})
        working = self.sessions.work_thread(thread_id)
        self.assertEqual(working["response_state"], "working")
        self.assertEqual(working["handled_completion_id"], before["turn_completion"]["id"])
        turn = self.sessions.rpc.threads[thread_id]["turns"][-1]
        turn["status"] = "completed"
        self.sessions._event({"method": "turn/completed", "params": {"threadId": thread_id, "turn": turn}})
        self.assertTrue(self.sessions.work_thread(thread_id)["needs_attention"])

    def test_required_answers_and_running_approvals_cannot_be_handled_away(self):
        required = self.result(actor="user")
        self.assertEqual(required["response_state"], "input")
        self.assertFalse(required["can_handle_response"])
        with self.assertRaises(CoordinationError):
            self.sessions.update_work_thread("terminal", self.receipt(required))
        with self.assertRaises(CoordinationError):
            self.store.threads.update("terminal", **self.receipt(required))
        browser = self.sessions.create({})["session"]["thread_id"]
        self.sessions.send(browser, {"message": "Work."})
        for approval in (False, True):
            if approval:
                self.sessions._event({"id": 42, "method": "item/commandExecution/requestApproval",
                                      "params": {"threadId": browser}})
            current = self.sessions.work_thread(browser)
            with self.assertRaises(CoordinationError):
                self.sessions.update_work_thread(browser, self.receipt(current))
        self.assertEqual(len(self.sessions.pending_requests(browser)), 1)
        self.assertEqual(self.sessions.rpc.writes, [])

    def test_invalid_or_cross_thread_receipts_are_atomic(self):
        before = self.result()
        self.store.register(session_id="other", client="codex", cwd=str(self.root))
        other = self.result(thread_id="other")
        bad = [{"handled": value} for value in (None, 1, "true", [])]
        bad += [{"handled": True}, {"handled_completion_id": 0}]
        bad += [{**self.receipt(before), "handled_completion_id": value}
                for value in (None, -1, True, "1", 10000, [], other["turn_completion"]["id"])]
        bad += [{**self.receipt(before), "handled_checkpoint_id": other["checkpoint"]["id"]}]
        for body in bad:
            with self.subTest(body=body), self.assertRaises(CoordinationError):
                self.sessions.update_work_thread("terminal", {**body, "title": "Wrong", "attention": "later"})
            self.assertEqual(self.sessions.work_thread("terminal"), before)

    def test_migration_resurfaces_read_finished_threads_without_changing_organization(self):
        self.result()
        before = self.sessions.update_work_thread("terminal", {"seen": True, "pinned": True})
        with self.store._connection() as db:
            db.execute("ALTER TABLE work_threads DROP COLUMN handled_checkpoint_id")
            db.execute("ALTER TABLE work_threads DROP COLUMN handled_completion_id")
        reopened = CoordinationStore(self.store.database_path)
        restored = reopened.threads.get("terminal", history=True)
        for key in ("title", "attention", "pinned", "checkpoints", "links", "unread", "seen_completion_id"):
            self.assertEqual(restored[key], before[key], key)
        self.assertTrue(restored["unhandled_response"])
        self.assertEqual(restored["handled_completion_id"], 0)

    def test_legacy_checkpoint_without_turn_receipt_can_be_handled(self):
        self.store.threads.checkpoint("terminal", {"phase": "discussion", "summary": "A saved recommendation."})
        thread = self.sessions.work_thread("terminal")
        self.assertTrue(thread["needs_attention"])
        self.assertIsNone(thread["turn_completion"])
        self.assertFalse(self.sessions.update_work_thread("terminal", self.receipt(thread))["needs_attention"])

    def test_handled_failure_recedes_and_new_failure_resurfaces(self):
        failed = self.result(status="failed")
        self.assertEqual(failed["response_state"], "failed")
        self.assertFalse(self.sessions.update_work_thread("terminal", self.receipt(failed))["needs_attention"])
        self.assertTrue(self.result("two", status="failed")["needs_attention"])

    def test_http_handling_and_reload_preserve_the_thread(self):
        before = self.result()
        server = make_ui_server(self.store, port=0, browser_sessions=self.sessions)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(worker.join)
        self.addCleanup(server.shutdown)
        base = "http://127.0.0.1:" + str(server.server_address[1]) + "/api/browser/"
        with urllib.request.urlopen(base + "config") as response:
            token = json.load(response)["token"]
        request = urllib.request.Request(base + "threads/terminal", data=json.dumps(self.receipt(before)).encode(),
                                        headers={"Content-Type": "application/json", "X-Agent-Coord-Token": token})
        with urllib.request.urlopen(request) as response:
            self.assertFalse(json.load(response)["needs_attention"])
        with urllib.request.urlopen(base + "threads") as response:
            listed = json.load(response)["data"]
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["response_state"], "available")
        self.assertEqual(listed[0]["original_request"], before["original_request"])

    @unittest.skipUnless(shutil.which("node"), "Node is required for browser response controls")
    def test_browser_response_controls(self):
        result = subprocess.run(["node", "--test", str(Path(__file__).with_name("test_web_thread_handling.js"))],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
