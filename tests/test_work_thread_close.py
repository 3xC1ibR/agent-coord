from __future__ import annotations

import threading
import unittest
from unittest.mock import patch

import test_codex_app_server
import test_ui
from agent_coord.codex_app_server import BrowserSessions, BrowserBusyError
from agent_coord.store import CoordinationError


class WorkThreadCloseTests(unittest.TestCase):
    setUp = test_codex_app_server.BrowserSessionTests.setUp
    create = test_codex_app_server.BrowserSessionTests.create

    def test_close_stops_turn_releases_scope_and_preserves_history_and_checkpoint(self):
        thread_id = self.create()
        other = self.create()
        self.sessions.send(thread_id, {"message": "Keep this conversation."})
        self.sessions.send(other, {"message": "Leave this running."})
        self.store.begin_work(session_id=thread_id, scopes=["src/**"])
        self.sessions.checkpoint_work_thread(thread_id, {"phase": "discussion", "summary": "A useful result."})
        self.sessions.link_work_thread(thread_id, {"kind": "document", "target": "README.md"})
        closed = self.sessions.close_work_thread(thread_id)
        self.assertEqual(closed["attention"], "archived")
        self.assertEqual(closed["checkpoint"]["phase"], "discussion")
        self.assertEqual(closed["links"][0]["target"], str(self.root / "README.md"))
        self.assertEqual(closed["original_request"], "Keep this conversation.")
        self.assertEqual(self.store.get_session(thread_id)["presence"], "offline")
        self.assertEqual(self.store.get_session(thread_id)["write_scope"], [])
        self.assertNotIn(thread_id, self.sessions.loaded)
        self.assertNotIn(thread_id, self.sessions.active)
        self.assertIn(other, self.sessions.active)
        methods = [m for m, p in self.sessions.rpc.calls if p.get("threadId") == thread_id]
        self.assertLess(methods.index("turn/interrupt"), methods.index("thread/archive"))
        detail = self.sessions.read(thread_id)
        self.assertEqual(detail["thread"]["turns"][0]["status"], "interrupted")
        self.assertNotIn(thread_id, self.sessions.loaded)
        self.assertFalse(closed["needs_attention"])
        self.assertEqual([t["thread_id"] for t in self.sessions.list_work_threads()], [other])

    def test_close_and_reopen_are_idempotent_and_survive_restart(self):
        thread_id = self.create()
        self.sessions.rpc.fast_turn = True
        self.sessions.send(thread_id, {"message": "History stays."})
        self.sessions.close_work_thread(thread_id)
        self.sessions.close_work_thread(thread_id)
        self.assertEqual(sum(m == "thread/archive" for m, _ in self.sessions.rpc.calls), 1)
        self.sessions.close()
        reopened = BrowserSessions(self.store, str(self.root), rpc_factory=test_codex_app_server.FakeCodex)
        self.addCleanup(reopened.close)
        reopened.rpc.threads = self.sessions.rpc.threads
        self.assertEqual(reopened.list_work_threads(), [])
        self.assertEqual(reopened.list_work_threads(archived=True)[0]["thread_id"], thread_id)
        reopened.reopen_work_thread(thread_id)
        reopened.reopen_work_thread(thread_id)
        self.assertEqual(sum(m == "thread/unarchive" for m, _ in reopened.rpc.calls), 1)
        reopened.send(thread_id, {"message": "Continue."})
        self.assertEqual(len(reopened.read(thread_id)["thread"]["turns"]), 2)

    def test_shutdown_failure_keeps_thread_open_and_scope_owned(self):
        thread_id = self.create()
        self.store.begin_work(session_id=thread_id, scopes=["src/**"])
        original = self.sessions.rpc.request
        def request(method, params=None):
            if method == "thread/archive":
                raise CoordinationError("Shutdown failed")
            return original(method, params)
        with patch.object(self.sessions.rpc, "request", side_effect=request):
            with self.assertRaisesRegex(CoordinationError, "Shutdown failed"):
                self.sessions.close_work_thread(thread_id)
        self.assertEqual(self.sessions.work_thread(thread_id)["attention"], "now")
        self.assertEqual(self.store.get_session(thread_id)["write_scope"], ["src/**"])

    def test_close_waits_for_asynchronous_interruption_and_clears_pending_requests(self):
        thread_id = self.create()
        self.sessions.send(thread_id, {"message": "Working"})
        self.sessions._event({"id": 123, "method": "item/commandExecution/requestApproval", "params": {"threadId": thread_id}})
        original = self.sessions.rpc.request
        timers = []
        def request(method, params=None):
            if method == "turn/interrupt":
                timer = threading.Timer(.02, original, args=(method, params))
                timers.append(timer)
                timer.start()
                return {}
            if method == "thread/archive":
                self.assertNotIn(thread_id, self.sessions.active)
            return original(method, params)
        try:
            with patch.object(self.sessions.rpc, "request", side_effect=request):
                self.sessions.close_work_thread(thread_id)
        finally:
            for timer in timers:
                timer.join(1)
        self.assertEqual(self.sessions.pending_requests(thread_id), [])

    def test_terminal_close_does_not_hide_a_process_shutdown_failure(self):
        self.store.register(session_id="terminal", client="codex", cwd=str(self.root))
        with patch("agent_coord.codex_app_server.stop_terminal_session", side_effect=CoordinationError("Exit in terminal")):
            with self.assertRaisesRegex(CoordinationError, "Exit in terminal"):
                self.sessions.close_work_thread("terminal")
        self.assertEqual(self.sessions.work_thread("terminal")["attention"], "now")
        self.store.end_session("terminal")
        self.sessions.close_work_thread("terminal")
        self.assertEqual(self.sessions.work_thread("terminal")["attention"], "archived")
        self.assertEqual(self.sessions.rpc.calls, [])

    def test_interrupt_timeout_keeps_thread_open_and_does_not_archive(self):
        thread_id = self.create()
        self.sessions.send(thread_id, {"message": "Still working"})
        original = self.sessions.rpc.request
        def request(method, params=None):
            return {} if method == "turn/interrupt" else original(method, params)
        with patch.object(self.sessions.rpc, "request", side_effect=request):
            with self.assertRaisesRegex(BrowserBusyError, "still stopping"):
                self.sessions.close_work_thread(thread_id, stop_timeout=.01)
        self.assertEqual(self.sessions.work_thread(thread_id)["attention"], "now")
        self.assertIn(thread_id, self.sessions.active)
        self.assertFalse(any(m == "thread/archive" for m, _ in self.sessions.rpc.calls))

    def test_workspace_boundary_applies_to_close_and_reopen(self):
        thread_id = self.create()
        other = self.root / "other"
        other.mkdir()
        isolated = BrowserSessions(self.store, str(other), rpc_factory=test_codex_app_server.FakeCodex)
        self.addCleanup(isolated.close)
        for operation in (isolated.close_work_thread, isolated.reopen_work_thread):
            with self.assertRaises(CoordinationError):
                operation(thread_id)
        self.assertEqual(isolated.rpc.calls, [])


class CloseHTTPTests(unittest.TestCase):
    setUp = test_ui.BrowserHTTPTests.setUp
    stop_server = test_ui.BrowserHTTPTests.stop_server
    request = test_ui.BrowserHTTPTests.request

    def test_close_reopen_routes_and_csrf(self):
        _, created = self.request("/api/browser/sessions", {})
        thread_id = created["session"]["thread_id"]
        path = "/api/browser/threads/" + thread_id
        self.assertEqual(self.request(path + "/close", {}, {"X-Agent-Coord-Token": "wrong"})[0], 403)
        self.assertEqual(self.request(path + "/close", {"surprise": True})[0], 400)
        self.assertEqual(self.request(path + "/close", {})[0], 200)
        self.assertEqual(self.request("/api/browser/threads")[1]["data"], [])
        self.assertEqual(len(self.request("/api/browser/threads?archived=true")[1]["data"]), 1)
        self.assertEqual(self.request(path + "/reopen", {})[0], 200)
        self.assertEqual(len(self.request("/api/browser/threads")[1]["data"]), 1)
