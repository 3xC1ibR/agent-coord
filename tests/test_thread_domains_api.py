from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))

from agent_coord.codex_app_server import BrowserSessions
from agent_coord.store import CoordinationError, CoordinationStore
from test_codex_app_server import FakeCodex


class ThreadDomainAPITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = CoordinationStore(self.root / "coord.sqlite3")
        self.sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(self.sessions.close)

    def test_overview_serializes_domains_and_scope_fallback_for_all_providers(self):
        browser_id = self.sessions.create({})["session"]["thread_id"]
        self.store.threads.checkpoint(browser_id, {"phase": "discussion", "summary": "Exploring.", "domain": "web"})
        for provider in ("codex", "claude", "kimi", "antigravity"):
            self.store.register(session_id=provider, client=provider, cwd=str(self.root))
            self.store.begin_work(session_id=provider, scopes=[f"{provider}/**"])
            self.store.end_work(provider)
        threads = {thread["thread_id"]: thread for thread in self.sessions.list_work_threads()}
        self.assertEqual((threads[browser_id]["domain"], threads[browser_id]["domain_source"]), ("web", "agent"))
        for provider in ("codex", "claude", "kimi", "antigravity"):
            self.assertEqual((threads[provider]["domain"], threads[provider]["domain_source"], threads[provider]["scope_cluster"]),
                             (provider, "derived", provider))

    def test_user_dialog_override_and_reset_survive_service_restart(self):
        thread_id = self.sessions.create({})["session"]["thread_id"]
        self.store.begin_work(session_id=thread_id, scopes=["web/**"])
        self.store.end_work(thread_id)
        self.sessions.checkpoint_work_thread(thread_id, {"phase": "discussion", "summary": "User note.", "domain": "UI"})
        self.sessions.close()
        reopened_store = CoordinationStore(self.store.database_path)
        reopened = BrowserSessions(reopened_store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(reopened.close)
        reopened_store.threads.checkpoint(thread_id, {"phase": "investigation", "summary": "Agent progress.", "domain": "backend"})
        thread = reopened.work_thread(thread_id)
        self.assertEqual((thread["domain"], thread["domain_source"]), ("ui", "user"))
        self.assertEqual(thread["checkpoint"]["summary"], "Agent progress.")
        self.assertEqual(thread["attention"], "now")
        thread = reopened.checkpoint_work_thread(thread_id, {"phase": "discussion", "summary": "Reset.", "domain": None})
        self.assertEqual((thread["domain"], thread["domain_source"]), ("web", "derived"))
        reopened_store.threads.checkpoint(thread_id, {"phase": "investigation", "summary": "Moved to docs.", "domain": "docs"})
        self.assertEqual(reopened.work_thread(thread_id)["domain_source"], "agent")

    def test_thread_update_supports_override_reset_and_omission(self):
        thread_id = self.sessions.create({})["session"]["thread_id"]
        thread = self.sessions.update_work_thread(thread_id, {"domain": " Web "})
        self.assertEqual((thread["domain"], thread["domain_source"]), ("web", "user"))
        self.assertEqual(self.sessions.update_work_thread(thread_id, {"title": "My project"})["domain"], "web")
        with self.assertRaisesRegex(CoordinationError, "Thread domain"):
            self.sessions.update_work_thread(thread_id, {"domain": ""})
        self.assertEqual(self.sessions.work_thread(thread_id)["domain"], "web")
        thread = self.sessions.update_work_thread(thread_id, {"domain": None})
        self.assertIsNone(thread["domain"])
        self.assertIsNone(thread["domain_source"])

    def test_domain_serialization_does_not_change_attention_or_work_phase(self):
        self.store.register(session_id="terminal", client="codex", cwd=str(self.root))
        self.store.threads.checkpoint("terminal", {"phase": "investigation", "summary": "Needs a decision.",
                                                  "next_action": "Choose the approach.", "next_actor": "user"})
        before = self.sessions.work_thread("terminal")
        self.assertTrue(before["needs_attention"])
        self.sessions.update_work_thread("terminal", {"domain": "backend"})
        listed = next(thread for thread in self.sessions.list_work_threads() if thread["thread_id"] == "terminal")
        self.assertEqual((listed["domain"], listed["domain_source"], listed["scope_cluster"]), ("backend", "user", None))
        for key in ("attention", "needs_attention", "attention_reason", "attention_key", "work_phase", "response_state"):
            self.assertEqual(listed[key], before[key], key)

    @unittest.skipUnless(shutil.which("node"), "Node is required for checkpoint dialog tests")
    def test_browser_checkpoint_dialog(self):
        result = subprocess.run(["node", "--test", str(Path(__file__).with_name("test_web_checkpoint.js"))],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
