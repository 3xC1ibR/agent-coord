from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))
from agent_coord.cli import _parser, run
from agent_coord.navigation import NavigationStore
from agent_coord.store import CoordinationError, CoordinationStore
from agent_coord.thread_search import search_threads


class ConversationSearchTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.store = CoordinationStore(self.root / "state.sqlite3", clock=lambda: 1000)
        with self.store._connection() as db:
            db.execute("CREATE TABLE browser_history (thread_id TEXT PRIMARY KEY, history_json TEXT NOT NULL)")

    def create(self, identity, text=None, *, role="user", attention="now", title="Unrelated title"):
        self.store.register(session_id=identity, client="codex", cwd=str(self.root), name=title)
        self.store.threads.update(identity, attention=attention)
        if text is not None:
            self.save(identity, text, role=role)

    def save(self, identity, text, *, role="user"):
        item = {"id": "message", "timelineAt": 1234, "type": "userMessage", "content": [{"type": "text", "text": text}]}
        if role == "assistant":
            item = {"id": "message", "type": "agentMessage", "text": text}
        history = {"turns": [{"id": "turn", "startedAt": 1200, "items": [item]}]}
        with self.store._connection() as db:
            db.execute("INSERT OR REPLACE INTO browser_history VALUES (?, ?)", (identity, json.dumps(history)))

    def test_jev_in_middle_of_closed_conversation_with_unrelated_metadata(self):
        self.create("overview", "we use jev to sort attention", attention="archived")
        self.create("assistant", "Jev can help", role="assistant")
        self.create("metadata", title="Jev research")
        page = search_threads(self.store, "jev", my_messages=True)
        self.assertEqual([i["thread_id"] for i in page["items"]], ["overview"])
        hit = page["items"][0]
        self.assertFalse(hit["context_match"])
        self.assertEqual(hit["message_count"], 1)
        match = hit["matches"][0]
        self.assertEqual(match["role"], "user")
        self.assertEqual(match["timestamp"], 1234)
        self.assertIn("jev", match["excerpt"])
        self.assertEqual(NavigationStore(self.store).resolve(match["url"])["route"],
                         {"kind": "thread", "id": "overview", "turn": "turn", "item": "message"})
        self.assertEqual({i["thread_id"] for i in search_threads(self.store, "jev")["items"]},
                         {"overview", "assistant", "metadata"})
        self.assertEqual([i["thread_id"] for i in search_threads(self.store, "jev", source="context")["items"]], ["metadata"])
        self.assertEqual(page["coverage"]["saved_histories"], 2)
        self.assertEqual(page["coverage"]["missing_histories"], 1)

    def test_index_updates_and_deletions_across_process_restarts(self):
        self.create("thread", "old Jev discussion")
        self.assertEqual(len(search_threads(self.store, "jev")["items"]), 1)
        self.save("thread", "new conversation about compaction")
        reopened = CoordinationStore(self.store.database_path)
        self.assertEqual(search_threads(reopened, "jev")["items"], [])
        self.assertEqual(len(search_threads(reopened, "compaction")["items"]), 1)
        with self.store._connection() as db:
            db.execute("DELETE FROM browser_history WHERE thread_id='thread'")
        page = search_threads(reopened, "compaction")
        self.assertEqual(page["items"], [])
        self.assertEqual(page["coverage"]["missing_histories"], 1)

    def test_no_tool_output_system_context_or_cross_message_false_matches(self):
        self.create("thread")
        history = {"turns": [{"id": "t", "items": [
            {"id": "tool", "type": "commandExecution", "aggregatedOutput": "Jev tools"},
            {"id": "context", "type": "userMessage", "content": [{"type": "text", "text": "# AGENTS.md\nJev instructions"}]},
            {"id": "wake", "type": "userMessage", "content": [{"type": "text", "text": "Check and handle your unread agent-coord messages."}]},
            {"id": "user", "type": "userMessage", "content": [{"type": "text", "text": "Jev"}]},
            {"id": "assistant", "type": "agentMessage", "text": "evidence verification"}]}]}
        with self.store._connection() as db:
            db.execute("INSERT INTO browser_history VALUES (?, ?)", ("thread", json.dumps(history)))
        self.assertEqual(search_threads(self.store, "jev tools")["items"], [])
        self.assertEqual(search_threads(self.store, "jev instructions")["items"], [])
        self.assertEqual(search_threads(self.store, "jev verification")["items"], [])
        self.assertEqual(search_threads(self.store, "unread", my_messages=True)["items"], [])
        self.assertEqual(search_threads(self.store, "verification", my_messages=True)["items"], [])
        self.assertEqual(search_threads(self.store, "jev")["items"][0]["message_count"], 1)

    def test_literal_unicode_keywords_and_bounded_excerpts(self):
        self.create("long", "x" * 20000 + " Straße JeV 100%_ " + "y" * 20000)
        hit = search_threads(self.store, "STRASSE jev 100%_")["items"][0]
        self.assertLess(len(hit["matches"][0]["excerpt"]), 325)
        self.assertIn("JeV", hit["matches"][0]["excerpt"])
        self.assertEqual(search_threads(self.store, "' OR 1=1 --")["items"], [])
        self.assertIn("Straße", search_threads(self.store, "strasse")["items"][0]["matches"][0]["excerpt"])

    def test_latest_excerpts_follow_conversation_order_without_timestamps(self):
        self.create("thread")
        history = {"turns": [{"id": "t", "items": [
            {"id": f"reverse-{9-i}", "type": "agentMessage", "text": f"Jev discussion {i}"}
            for i in range(5)]}]}
        with self.store._connection() as db:
            db.execute("INSERT INTO browser_history VALUES (?, ?)", ("thread", json.dumps(history)))
        hit = search_threads(self.store, "jev")["items"][0]
        self.assertEqual(hit["message_count"], 5)
        self.assertEqual([m["excerpt"] for m in hit["matches"]], [f"Jev discussion {i}" for i in (4, 3, 2)])

    def test_pagination_and_role_cursor_guard(self):
        for i in range(23):
            self.create(f"thread-{i:02}", "use Jev", attention="archived" if i % 2 else "now")
        cursor, found = None, []
        while True:
            page = search_threads(self.store, "jev", limit=4, cursor=cursor)
            found += [r["thread_id"] for r in page["items"]]
            cursor = page["next_cursor"]
            if cursor is None:
                break
            with self.assertRaises(CoordinationError):
                search_threads(self.store, "jev", cursor=cursor, my_messages=True)
        self.assertEqual(len(set(found)), 23)
        self.assertEqual(len(found), 23)
        page = search_threads(self.store, "jev", thread_ids=["thread-00"])
        self.assertEqual(page["coverage"]["threads"], 1)
        self.assertEqual(len(page["items"]), 1)
        self.assertEqual(search_threads(self.store, "jev", thread_ids=[])["items"], [])

    def test_corrupt_or_partial_history_does_not_hide_other_results(self):
        for identity, value in (("broken", "{oops"), ("partial", '{"historyUnavailable":true,"turns":[]}')):
            self.create(identity)
            with self.store._connection() as db:
                db.execute("INSERT INTO browser_history VALUES (?, ?)", (identity, value))
        self.create("good", "Jev is here")
        page = search_threads(self.store, "jev")
        self.assertEqual(len(page["items"]), 1)
        self.assertEqual(page["coverage"]["partial_histories"], 1)
        self.assertEqual(page["coverage"]["missing_histories"], 1)

    def test_no_provider_invocation_or_thread_state_mutation_and_cli_parity(self):
        self.create("thread", "can we use Jev?")
        before = self.store.threads.get("thread", history=True)
        with patch("subprocess.Popen", side_effect=AssertionError("Search started a provider")):
            cli = run(_parser().parse_args(["--db", str(self.store.database_path), "thread", "search", "jev", "--my-messages"]))
        self.assertEqual(cli, search_threads(self.store, "jev", my_messages=True))
        self.assertEqual(self.store.threads.get("thread", history=True), before)

    @unittest.skipUnless(shutil.which("node"), "Node is required for browser search tests")
    def test_browser_search(self):
        result = subprocess.run(["node", "--test", str(Path(__file__).with_name("test_web_conversation_search.js"))],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
