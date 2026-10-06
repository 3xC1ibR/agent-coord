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


class ThreadPinningTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = CoordinationStore(self.root / "state.sqlite3")
        self.store.register(session_id="terminal", client="codex", cwd=str(self.root))
        self.sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(self.sessions.close)

    def test_pin_and_unpin_survive_restart_without_changing_conversation(self):
        self.store.threads.start_turn("terminal", prompt="Investigate the overview.", turn_id="one")
        self.store.threads.checkpoint("terminal", {"phase": "investigation", "summary": "A saved finding."})
        self.store.threads.finish_turn("terminal")
        before = self.sessions.work_thread("terminal")
        self.assertIs(before["pinned"], False)
        pinned = self.sessions.update_work_thread("terminal", {"pinned": True})
        self.assertIs(pinned["pinned"], True)
        self.assertEqual({k: v for k, v in pinned.items() if k != "pinned"},
                         {k: v for k, v in before.items() if k != "pinned"})
        reopened = CoordinationStore(self.store.database_path)
        self.assertIs(reopened.threads.get("terminal")["pinned"], True)
        self.assertIs(reopened.threads.list()[0]["pinned"], True)
        self.sessions.update_work_thread("terminal", {"title": "Renamed", "seen": True})
        self.store.threads.start_turn("terminal", prompt="Continue.", turn_id="two")
        self.assertIs(reopened.threads.get("terminal")["pinned"], True)
        self.sessions.update_work_thread("terminal", {"pinned": False})
        self.assertIs(CoordinationStore(self.store.database_path).threads.get("terminal")["pinned"], False)
        self.assertEqual(self.sessions.rpc.calls, [])

    def test_pin_is_independent_of_now_later_and_closed_placement(self):
        for attention in ("now", "later", "archived"):
            with self.subTest(attention=attention):
                self.sessions.update_work_thread("terminal", {"attention": attention})
                pinned = self.sessions.update_work_thread("terminal", {"pinned": True})
                self.assertEqual(pinned["attention"], attention)
                listed = self.sessions.list_work_threads(archived=attention == "archived")
                self.assertIs(listed[0]["pinned"], True)
                unpinned = self.sessions.update_work_thread("terminal", {"pinned": False})
                self.assertEqual(unpinned["attention"], attention)
        self.sessions.update_work_thread("terminal", {"pinned": True, "attention": "now"})
        self.sessions.update_work_thread("terminal", {"attention": "archived"})
        self.assertEqual(self.sessions.list_work_threads(), [])
        reopened = self.sessions.update_work_thread("terminal", {"attention": "now"})
        self.assertIs(reopened["pinned"], True)

    def test_invalid_pin_values_do_not_partially_update_a_thread(self):
        before = self.sessions.work_thread("terminal")
        for value in (None, 0, 1, "false", [], {}):
            with self.subTest(value=value):
                with self.assertRaisesRegex(CoordinationError, "Pinned must be true or false"):
                    self.sessions.update_work_thread("terminal", {"pinned": value, "title": "Bad edit", "attention": "later"})
                with self.assertRaisesRegex(CoordinationError, "Pinned must be true or false"):
                    self.store.threads.update("terminal", pinned=value, title="Bad edit", seen=True)
                self.assertEqual(self.sessions.work_thread("terminal"), before)

    def test_existing_database_migrates_to_unpinned_without_losing_history(self):
        self.store.threads.start_turn("terminal", prompt="Keep this request.", turn_id="legacy")
        self.store.threads.checkpoint("terminal", {"phase": "planning", "summary": "Keep this plan."})
        self.store.threads.update("terminal", title="Legacy thread", attention="later")
        before = self.store.threads.get("terminal", history=True)
        with self.store._connection() as db:
            db.execute("ALTER TABLE work_threads DROP COLUMN pinned")
        reopened = CoordinationStore(self.store.database_path)
        self.assertEqual(reopened.threads.get("terminal", history=True), before)
        reopened.threads.update("terminal", pinned=True)
        self.assertIs(CoordinationStore(self.store.database_path).threads.get("terminal")["pinned"], True)

    def test_http_pin_update_for_terminal_and_browser_threads(self):
        browser_id = self.sessions.create({})["session"]["thread_id"]
        server = make_ui_server(self.store, port=0, browser_sessions=self.sessions)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(worker.join)
        self.addCleanup(server.shutdown)
        base = "http://127.0.0.1:" + str(server.server_address[1]) + "/api/browser/"
        with urllib.request.urlopen(base + "config") as response:
            token = json.load(response)["token"]

        def request(path, body=None):
            headers = {"Content-Type": "application/json", "X-Agent-Coord-Token": token}
            req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None, headers=headers)
            with urllib.request.urlopen(req) as response:
                return json.load(response)

        for thread_id in ("terminal", browser_id):
            with self.subTest(thread_id=thread_id):
                self.assertIs(request("threads/" + thread_id, {"pinned": True})["pinned"], True)
                self.assertIs(request("threads/" + thread_id)["pinned"], True)
                listed = next(t for t in request("threads")["data"] if t["thread_id"] == thread_id)
                self.assertIs(listed["pinned"], True)
                self.assertIs(request("threads/" + thread_id, {"pinned": False})["pinned"], False)
                with self.assertRaises(urllib.error.HTTPError) as error:
                    request("threads/" + thread_id, {"pinned": "false"})
                self.assertEqual(error.exception.code, 400)
                error.exception.close()

    @unittest.skipUnless(shutil.which("node"), "Node is required for browser pinning tests")
    def test_browser_pinning(self):
        result = subprocess.run(["node", "--test", str(Path(__file__).with_name("test_web_thread_pinning.js"))],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
