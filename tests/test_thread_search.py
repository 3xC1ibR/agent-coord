from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))

from agent_coord.cli import _parser, run
from agent_coord.navigation import NavigationStore
from agent_coord.store import CoordinationError, CoordinationStore
from agent_coord.thread_search import search_threads


class ThreadSearchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.now = 1000.0
        self.store = CoordinationStore(self.root / "state.sqlite3", clock=lambda: self.now)

    def create(self, identity, *, title="Database specialist", request="Optimize writes", cwd=None, attention="now"):
        self.store.register(session_id=identity, client="codex", cwd=str(cwd or self.root), name=title)
        self.store.threads.capture_request(identity, request)
        self.store.threads.update(identity, attention=attention)
        return identity

    def checkpoint(self, thread, **values):
        return self.store.threads.checkpoint(thread, {"phase": "investigation", "summary": "Measured database latency",
            "next_action": "", "next_actor": "nobody", **values})

    def test_closed_context_is_ranked_by_relevance_then_meaningful_work(self):
        self.create("closed", attention="archived")
        self.now = 2000
        self.create("recent", title="Unrelated feature", request="Database is one dependency")
        self.create("irrelevant", title="Recent and unrelated", request="Make a logo")
        self.now = 3000
        self.store.threads.update("closed", title="Database expert", pinned=True)
        page = search_threads(self.store, "database")
        self.assertEqual([r["thread_id"] for r in page["items"]], ["closed", "recent"])
        self.assertEqual([r["last_work_at"] for r in page["items"]], [1000, 2000])
        self.assertEqual(page["items"][0]["attention"], "archived")
        self.assertIsNone(page["next_cursor"])
        self.assertEqual(NavigationStore(self.store).resolve(page["items"][0]["url"])["route"],
                         {"kind": "thread", "id": "closed"})

    def test_work_completion_and_changed_checkpoint_are_fresh_but_refresh_is_not(self):
        self.create("old")
        self.create("new")
        self.now = 2000
        self.checkpoint("old")
        self.now = 3000
        self.store.threads.start_turn("new", prompt="Review current code", turn_id="turn")
        self.now = 4000
        self.store.threads.finish_turn("new", turn_id="turn", status="completed")
        self.now = 5000
        self.checkpoint("old")  # identical checkpoint only refreshes metadata
        page = search_threads(self.store, "specialist")
        self.assertEqual([(r["thread_id"], r["last_work_at"]) for r in page["items"]], [("new", 4000), ("old", 2000)])
        self.assertEqual(page["items"][0]["last_turn_status"], "completed")

    def test_pagination_has_no_duplicates_or_gaps_across_ties_and_restart(self):
        for index in range(13):
            self.create(f"thread-{index:02}", attention="archived" if index % 2 else "now")
        cursor, found = None, []
        while True:
            page = search_threads(CoordinationStore(self.store.database_path), "database", limit=3, cursor=cursor)
            self.assertLessEqual(len(page["items"]), 3)
            found.extend(item["thread_id"] for item in page["items"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(found, [f"thread-{i:02}" for i in reversed(range(13))])

    def test_keywords_match_across_saved_context_and_artifacts_with_literal_sql_characters(self):
        self.create("match", title="Writer", request="Keep latency low")
        self.checkpoint("match", summary="Profiled database", links=[{
            "kind": "document", "label": "Benchmark", "target": "docs/100%_results.md"}])
        page = search_threads(self.store, "writer database latency benchmark")
        self.assertEqual([r["thread_id"] for r in page["items"]], ["match"])
        self.assertEqual(len(search_threads(self.store, "100%_")["items"]), 1)
        self.assertEqual(search_threads(self.store, "100___")["items"], [])
        self.assertEqual(search_threads(self.store, "' OR 1=1 --")["items"], [])

    def test_filters_and_global_discovery(self):
        repo = self.root / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        self.create("repository", cwd=repo)
        self.create("unassigned")
        project = self.store.threads.organization.create_project("Launch")
        self.store.threads.update("repository", project_id=project["id"])
        repository = self.store.threads.get("repository")["repository_id"]
        self.assertEqual(len(search_threads(self.store, "database")["items"]), 2)
        for filters in ({"cwd": str(repo)}, {"repository_id": repository}, {"project_id": project["id"]}):
            self.assertEqual([r["thread_id"] for r in search_threads(self.store, "database", **filters)["items"]], ["repository"])
        for filters in ({"repository_id": None}, {"project_id": None}):
            self.assertEqual([r["thread_id"] for r in search_threads(self.store, "database", **filters)["items"]], ["unassigned"])
        self.assertEqual(search_threads(self.store, "database", app_only=True)["items"], [])

    def test_app_only_and_pause_visibility(self):
        from test_codex_app_server import BrowserSessions, FakeCodex
        sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(sessions.close)
        thread = sessions.create({"name": "Database app specialist"})["session"]["thread_id"]
        sessions.close_work_thread(thread)
        sessions.inbox_wake.pause(thread, "Stopped by user")
        self.create("terminal")
        rows = search_threads(self.store, "database", app_only=True)["items"]
        self.assertEqual([r["thread_id"] for r in rows], [thread])
        self.assertTrue(rows[0]["app_conversation"])
        self.assertTrue(rows[0]["wake_paused"])
        self.assertEqual(rows[0]["wake_pause_reason"], "Stopped by user")

    def test_small_output_does_not_hydrate_inventory(self):
        for index in range(10):
            self.create(str(index), request="x" * 10000 + "database" + "y" * 10000)
            self.checkpoint(str(index), summary="x" * 1500 + "database" + "y" * 400)
        # Hydration for navigation links is limited to the returned page.
        with patch.object(self.store.threads, "get", wraps=self.store.threads.get) as get:
            page = search_threads(self.store, "database", limit=2)
        self.assertEqual(get.call_count, 2)
        self.assertLess(len(json.dumps(page)), 4000)
        self.assertTrue(all("database" in r["original_request"] for r in page["items"]))

    def test_invalid_cursors_queries_and_limits_fail_clearly(self):
        for index in range(2):
            self.create(str(index))
        cursor = search_threads(self.store, "database", limit=1)["next_cursor"]
        for values in ({"cursor": "garbage"}, {"cursor": "W10="}, {"cursor": cursor, "app_only": True},
                       {"cursor": cursor, "project_id": None}, {"limit": 0}, {"limit": 51}, {"limit": True}):
            with self.subTest(values=values), self.assertRaises(CoordinationError):
                search_threads(self.store, "database", **values)
        for query in ("", " " * 5, "x" * 501, "a b c d e f g h i j k"):
            with self.assertRaises(CoordinationError):
                search_threads(self.store, query)
        with self.assertRaises(CoordinationError):
            search_threads(self.store, "specialist", cursor=cursor)

    def test_cli_needs_no_caller_and_defaults_to_all_placements(self):
        self.create("closed", attention="archived")
        with patch.dict("os.environ", {}, clear=True):
            result = run(_parser().parse_args(["--db", str(self.store.database_path), "thread", "search", "database", "--limit", "1"]))
        self.assertEqual([r["thread_id"] for r in result["items"]], ["closed"])


if __name__ == "__main__":
    unittest.main()
