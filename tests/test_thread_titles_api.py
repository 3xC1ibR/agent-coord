from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))

from agent_coord.codex_app_server import BrowserSessions
from agent_coord.store import CoordinationStore
from test_codex_app_server import FakeCodex


class ThreadTitleAPITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = CoordinationStore(self.root / "coord.sqlite3")
        self.sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(self.sessions.close)

    def checkpoint(self, thread_id, title):
        return self.store.threads.checkpoint(thread_id, {
            "phase": "discussion", "summary": "Compared the available approaches.", "title": title,
        })

    def test_agent_title_is_visible_in_overview_for_browser_and_terminal_threads(self):
        thread_id = self.sessions.create({})["session"]["thread_id"]
        self.sessions.rpc.fast_turn = True
        self.sessions.send(thread_id, {"message": "Please compare the database options."})
        self.store.register(session_id="terminal", client="codex", cwd=str(self.root))
        self.store.threads.start_turn("terminal", prompt="Can we improve session naming?")
        self.checkpoint(thread_id, "Database write performance")
        self.checkpoint("terminal", "Session naming improvements")
        threads = {thread["thread_id"]: thread for thread in self.sessions.list_work_threads()}
        self.assertEqual(threads[thread_id]["title"], "Database write performance")
        self.assertEqual(threads["terminal"]["title"], "Session naming improvements")
        self.assertEqual(threads[thread_id]["original_request"], "Please compare the database options.")
        self.assertEqual(threads["terminal"]["original_request"], "Can we improve session naming?")

    def test_browser_rename_protects_user_title_across_service_restart(self):
        thread_id = self.sessions.create({})["session"]["thread_id"]
        self.checkpoint(thread_id, "Database write performance")
        self.sessions.update_work_thread(thread_id, {"title": "My database project"})
        self.sessions.close()
        store = CoordinationStore(self.store.database_path)
        reopened = BrowserSessions(store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(reopened.close)
        store.threads.checkpoint(thread_id, {
            "phase": "finished", "summary": "Comparison complete.", "title": "An unwanted new name",
        })
        thread = reopened.work_thread(thread_id)
        self.assertEqual(thread["title"], "My database project")
        self.assertEqual(thread["title_source"], "user")
        self.assertEqual(thread["checkpoint"]["summary"], "Comparison complete.")

    def test_user_checkpoint_can_name_thread_and_agent_cannot_replace_it(self):
        thread_id = self.sessions.create({})["session"]["thread_id"]
        self.sessions.checkpoint_work_thread(thread_id, {
            "phase": "discussion", "summary": "My note.", "title": "My chosen title",
        })
        self.checkpoint(thread_id, "An unwanted new name")
        self.assertEqual(self.sessions.work_thread(thread_id)["title"], "My chosen title")

    def test_browser_launch_explains_checkpoint_naming_and_user_precedence(self):
        self.sessions.create({})
        options = next(params for method, params in self.sessions.rpc.calls if method == "thread/start")
        instructions = options["developerInstructions"]
        self.assertIn("optional title", instructions)
        self.assertIn("first meaningful checkpoint", instructions)
        self.assertIn("cannot be overwritten", instructions)


if __name__ == "__main__":
    unittest.main()
