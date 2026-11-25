from __future__ import annotations

from contextlib import closing
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))

from agent_coord.search_store import MAX_EXCERPT_CHARACTERS, SearchStore
from agent_coord.store import CoordinationStore
from agent_coord.thread_search import search_threads


class SearchStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = CoordinationStore(self.root / "state.sqlite3")
        self.addCleanup(self.store.close_idle_connections)
        self.addCleanup(self.store.search_store.close)
        self.addCleanup(self.store.transcripts.close)
        self.search: SearchStore = self.store.search_store

    def test_isolated_schema_wal_pragmas_and_excerpt_bounds(self):
        long_prose = "Identifier_SubString_Token " * 60
        self.search.enqueue_turns(
            "t-alpha",
            [
                (
                    0,
                    {
                        "id": "turn-0",
                        "status": "completed",
                        "startedAt": 1234.5,
                        "items": [
                            {"id": "u1", "type": "userMessage", "content": [{"type": "text", "text": "User question about SubString"}]},
                            {"id": "a1", "type": "agentMessage", "text": long_prose},
                        ],
                    },
                )
            ],
        )
        self.assertTrue(self.search.flush())

        with closing(sqlite3.connect(self.search.path)) as db:
            journal = db.execute("PRAGMA journal_mode").fetchone()[0]
            sync = db.execute("PRAGMA synchronous").fetchone()[0]
            logical_tables = {
                row[0]
                for row in db.execute(
                    """SELECT name FROM sqlite_master
                       WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'search_fts_%'"""
                )
            }
            excerpt = db.execute("SELECT excerpt FROM search_turns WHERE thread_id='t-alpha' AND turn_seq=0").fetchone()[0]

        self.assertEqual(journal.lower(), "wal")
        self.assertEqual(sync, 1)  # NORMAL
        self.assertEqual(logical_tables, {"search_turns", "search_fts"})
        self.assertLessEqual(len(excerpt), MAX_EXCERPT_CHARACTERS)

    def test_in_progress_turns_skipped_until_finalized_and_my_messages_filtered(self):
        self.search.enqueue_turns(
            "t-live",
            [
                (
                    0,
                    {
                        "id": "turn-0",
                        "status": "inProgress",
                        "items": [{"id": "a", "type": "agentMessage", "text": "uncommitted streaming draft"}],
                    },
                )
            ],
        )
        self.assertTrue(self.search.flush())
        self.assertEqual(self.search.search_messages(["uncommitted"]), {})

        self.search.enqueue_turns(
            "t-live",
            [
                (
                    0,
                    {
                        "id": "turn-0",
                        "status": "completed",
                        "startedAt": 500.0,
                        "items": [
                            {"id": "u", "type": "userMessage", "content": [{"type": "text", "text": "only user asked zephyr"}]},
                            {"id": "a", "type": "agentMessage", "text": "assistant answered borealis"},
                        ],
                    },
                )
            ],
        )
        self.assertTrue(self.search.flush())

        all_borealis = self.search.search_messages(["borealis"], user_only=False)
        self.assertIn("t-live", all_borealis)
        user_borealis = self.search.search_messages(["borealis"], user_only=True)
        self.assertEqual(user_borealis, {})
        user_zephyr = self.search.search_messages(["zephyr"], user_only=True)
        self.assertIn("t-live", user_zephyr)

    def test_bm25_ranking_metadata_updates_deletion_and_state_lock_independence(self):
        for tid, text in (
            ("dense", "coordinator projection lock hierarchy coordinator projection lock"),
            ("sparse", "coordinator mentioned once and lock mentioned far away " + ("filler " * 80) + "projection"),
        ):
            self.store.register(session_id=tid, client="codex", cwd=str(self.root), name=tid)
            self.store.transcripts.save_turns(
                tid,
                {
                    "id": tid,
                    "turns": [
                        {
                            "id": "t0",
                            "status": "completed",
                            "startedAt": 100.0,
                            "items": [{"id": "a", "type": "agentMessage", "text": text}],
                        }
                    ],
                },
            )
        self.assertTrue(self.search.flush())

        # Hold an exclusive write transaction on state.sqlite3 while indexing metadata AND searching!
        blocker = sqlite3.connect(self.store.database_path, isolation_level=None)
        self.addCleanup(blocker.close)
        blocker.execute("BEGIN IMMEDIATE")
        try:
            self.search.enqueue_metadata("dense", title="Projection Lock Specialist", domain="storage")
            self.assertTrue(self.search.flush())
            ranked = search_threads(self.store, "coordinator projection lock", source="messages")
            self.assertEqual([r["thread_id"] for r in ranked["items"]], ["dense", "sparse"])
        finally:
            blocker.execute("ROLLBACK")

        self.search.delete_thread("dense")
        self.assertTrue(self.search.flush())
        self.assertEqual(set(self.search.search_messages(["coordinator", "projection", "lock"])), {"sparse"})

        # Removing the thread's transcript rows also excludes it from self-healing search_threads.
        with self.store._connection() as db:
            db.execute("DELETE FROM thread_turns WHERE thread_id = 'dense'")
        self.store.transcripts.forget("dense")
        after_delete = search_threads(self.store, "coordinator projection lock", source="messages")
        self.assertEqual([r["thread_id"] for r in after_delete["items"]], ["sparse"])

    def test_web_conversation_search_ui_contract(self):
        js_test = Path(__file__).with_name("test_web_conversation_search.js")
        if not js_test.exists():
            self.skipTest("test_web_conversation_search.js not present")
        result = subprocess.run(["node", "--test", str(js_test)], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
