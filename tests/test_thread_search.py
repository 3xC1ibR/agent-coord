from __future__ import annotations

import inspect
import json
import random
import sqlite3
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))

from agent_coord import thread_search
from agent_coord.cli import _parser, run
from agent_coord.navigation import NavigationStore
from agent_coord.store import CoordinationError, CoordinationStore, _StoreConnection
from agent_coord.thread_inventory import listed_threads
from agent_coord.thread_search import search_browser_threads, search_threads


class ThreadSearchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.now = 1000.0
        self.store = CoordinationStore(self.root / "state.sqlite3", clock=lambda: self.now)
        self.addCleanup(self.store.close_idle_connections)

    def create(self, identity, *, title="Database specialist", request="Optimize writes", cwd=None, attention="now"):
        self.store.register(session_id=identity, client="codex", cwd=str(cwd or self.root), name=title)
        self.store.threads.capture_request(identity, request)
        self.store.threads.update(identity, attention=attention)
        return identity

    def checkpoint(self, thread, **values):
        return self.store.threads.checkpoint(
            thread,
            {
                "phase": "investigation",
                "summary": "Measured database latency",
                "next_action": "",
                "next_actor": "nobody",
                **values,
            },
        )

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
        self.assertEqual(
            NavigationStore(self.store).resolve(page["items"][0]["url"])["route"],
            {"kind": "thread", "id": "closed"},
        )

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
        self.checkpoint("old")
        page = search_threads(self.store, "specialist")
        self.assertEqual([(r["thread_id"], r["last_work_at"]) for r in page["items"]], [("new", 4000), ("old", 2000)])
        self.assertEqual(page["items"][0]["last_turn_status"], "completed")

    def test_phrase_and_exact_title_rank_before_scattered_title_keywords(self):
        self.create("a-exact", title="Radio alarm", request="Schedule audio")
        self.create("b-phrase", title="Repair radio alarm scheduling", request="Schedule audio")
        self.now = 5000
        self.create("z-scattered", title="Radio playback and alarm scheduling", request="Schedule audio")
        self.assertEqual(
            [r["thread_id"] for r in search_threads(self.store, "radio alarm")["items"]],
            ["a-exact", "b-phrase", "z-scattered"],
        )

    def test_pagination_has_no_duplicates_or_gaps_across_ties_and_restart(self):
        for index in range(13):
            self.create(f"thread-{index:02}", attention="archived" if index % 2 else "now")
        cursor, found = None, []
        while True:
            reopened = CoordinationStore(self.store.database_path)
            try:
                page = search_threads(reopened, "database", limit=3, cursor=cursor)
            finally:
                reopened.close_idle_connections()
            self.assertLessEqual(len(page["items"]), 3)
            found.extend(item["thread_id"] for item in page["items"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(found, [f"thread-{i:02}" for i in reversed(range(13))])

    def test_keywords_match_across_saved_context_and_artifacts_with_literal_sql_characters(self):
        self.create("match", title="Writer", request="Keep latency low")
        self.checkpoint(
            "match",
            summary="Profiled database",
            links=[{"kind": "document", "label": "Benchmark", "target": "docs/100%_results.md"}],
        )
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

    def test_app_context_advice_is_available_for_routing_without_handover_bloat(self):
        from test_codex_app_server import BrowserSessions, FakeCodex

        sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(sessions.close)
        thread = sessions.create({"name": "Database app specialist"})["session"]["thread_id"]
        self.store.threads.start_turn(thread, turn_id="research")
        report = {
            "recommendation": "reuse",
            "reason": "Useful source knowledge.",
            "reuse_for": ["Source questions"],
            "fresh_for": ["Independent review"],
            "handover": {
                "confirmed_facts": ["The index fits the query."],
                "constraints": [],
                "tried_and_failed": [],
                "open_questions": [],
            },
        }
        checkpoint = self.checkpoint(thread, context_lifecycle=report)["checkpoint"]
        result = search_threads(self.store, "database", app_only=True, source="context")["items"][0]
        self.assertEqual(
            result["context_lifecycle"],
            {
                "checkpoint_id": checkpoint["id"],
                "stale": False,
                **{key: report[key] for key in ("recommendation", "reason", "reuse_for", "fresh_for")},
            },
        )
        self.assertEqual(self.store.threads.get(thread)["checkpoint"]["context_lifecycle"]["handover"], report["handover"])
        self.store.threads.start_turn(thread, turn_id="follow-up")
        stale = search_threads(self.store, "database", app_only=True)["items"][0]
        self.assertTrue(stale["context_lifecycle"]["stale"])
        self.checkpoint(thread, summary="Starting a new task.")
        self.assertNotIn("context_lifecycle", search_threads(self.store, "database", app_only=True)["items"][0])

    def test_small_output_does_not_hydrate_inventory(self):
        for index in range(10):
            self.create(str(index), request="x" * 10000 + "database" + "y" * 10000)
            self.checkpoint(str(index), summary="x" * 1500 + "database" + "y" * 400)
        with patch.object(self.store.threads, "get", wraps=self.store.threads.get) as get:
            page = search_threads(self.store, "database", limit=2)
        self.assertEqual(get.call_count, 0)
        self.assertEqual(
            [item["url"] for item in page["items"]],
            [NavigationStore(self.store).link(thread=item["thread_id"])["url"] for item in page["items"]],
        )
        self.assertLess(len(json.dumps(page)), 4000)
        self.assertTrue(all("database" in r["original_request"] for r in page["items"]))

    def test_invalid_cursors_queries_and_limits_fail_clearly(self):
        for index in range(2):
            self.create(str(index))
        cursor = search_threads(self.store, "database", limit=1)["next_cursor"]
        for values in (
            {"cursor": "garbage"},
            {"cursor": "W10="},
            {"cursor": cursor, "app_only": True},
            {"cursor": cursor, "project_id": None},
            {"limit": 0},
            {"limit": 51},
            {"limit": True},
        ):
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
            result = run(
                _parser().parse_args(["--db", str(self.store.database_path), "thread", "search", "database", "--limit", "1"])
            )
        self.assertEqual([r["thread_id"] for r in result["items"]], ["closed"])


class SearchExecutionTests(unittest.TestCase):
    """Stage 3 isolated SearchStore + TranscriptStore execution and lock-freedom tests."""

    WORDS = [
        "radio", "Radio", "ALARM", "alarm", "straße", "STRASSE", "ﬁle", "file", "K", "k",
        "okc", "moe", "the", "and", "x",
    ]
    SPACES = [" ", " ", "  ", "\n", "\t", "\u00a0", "\u3000", " \n "]

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.now = 1793247626.5940921
        self.store = CoordinationStore(self.root / "state.sqlite3", clock=lambda: self.now)
        self.addCleanup(self.store.close_idle_connections)
        self.addCleanup(self.store.search_store.close)
        self.addCleanup(self.store.transcripts.close)

    def text(self, generator, count):
        return "".join(generator.choice(self.WORDS) + generator.choice(self.SPACES) for _ in range(count)).strip()

    def seed(self, count):
        generator = random.Random(20261029)
        for index in range(count):
            identity = f"thread-{index:03}"
            self.now += generator.choice([0, 0, 0.125, 1.0000001, 3.3])
            self.store.register(session_id=identity, client="codex", cwd=str(self.root), name="Seed")
            self.store.threads.capture_request(identity, self.text(generator, generator.randrange(1, 12)))
            with self.store._connection() as db:
                db.execute(
                    "UPDATE work_threads SET title = ? WHERE thread_id = ?",
                    (generator.choice(["radio alarm", self.text(generator, 3)]), identity),
                )
            if generator.random() < 0.7:
                self.store.threads.checkpoint(
                    identity,
                    {
                        "phase": "investigation",
                        "summary": self.text(generator, 6),
                        "next_action": self.text(generator, 2),
                        "next_actor": "user",
                        "links": [{"kind": "document", "label": self.text(generator, 2), "target": f"docs/{index}.md"}],
                    },
                )
            if generator.random() < 0.3:
                self.store.threads.update(identity, attention="archived")
            items = []
            for position in range(generator.randrange(0, 9)):
                text = generator.choice(
                    ["radio alarm", " Radio\nALARM ", "\u3000radio alarm\n", " Radio ", "\tx\n", self.text(generator, generator.randrange(1, 20))]
                )
                if generator.random() < 0.5:
                    items.append({"id": f"m{position}", "type": "agentMessage", "text": text})
                else:
                    items.append(
                        {
                            "id": f"m{position}",
                            "type": "userMessage",
                            "timelineAt": self.now + position / 7,
                            "content": [{"type": "text", "text": text}],
                        }
                    )
            self.store.transcripts.save_turns(
                identity,
                {"id": identity, "turns": [{"id": "turn", "status": "completed", "startedAt": self.now, "items": items}]},
            )
        self.assertTrue(self.store.search_store.flush())

    def pages(self, query, **options):
        found, cursor = [], None
        while True:
            page = search_threads(self.store, query, limit=4, cursor=cursor, **options)
            found += page["items"]
            cursor = page["next_cursor"]
            if cursor is None:
                return found

    def test_search_never_creates_legacy_tables_or_acquires_state_writer_lock(self):
        self.seed(12)
        with patch.object(_StoreConnection, "create_function", side_effect=AssertionError("Per-row Python callback")):
            page = search_threads(self.store, "radio")
        self.assertTrue(page["items"])
        self.assertNotIn("create_function", inspect.getsource(thread_search))

        with self.store.reading(), self.store._connection() as db:
            legacy = [
                row[0]
                for row in db.execute(
                    """SELECT name FROM sqlite_master
                       WHERE name LIKE 'conversation_search_%' OR name LIKE 'conversation_substring_%'"""
                )
            ]
        self.assertEqual(legacy, [])

        writer = sqlite3.connect(self.store.database_path, isolation_level=None)
        self.addCleanup(writer.close)
        writer.execute("BEGIN IMMEDIATE")
        try:
            locked_page = search_threads(self.store, "radio")
        finally:
            writer.execute("ROLLBACK")
        self.assertEqual([i["thread_id"] for i in locked_page["items"]], [i["thread_id"] for i in page["items"]])

    def test_current_indexes_bypass_store_writer_admission(self):
        self.seed(8)
        entered, release = threading.Event(), threading.Event()

        def writer():
            with self.store._connection() as db:
                db.execute("BEGIN IMMEDIATE")
                entered.set()
                if not release.wait(5):
                    raise AssertionError("Writer was not released")

        with ThreadPoolExecutor(max_workers=2) as workers:
            writing = workers.submit(writer)
            try:
                self.assertTrue(entered.wait(5))
                page = workers.submit(search_threads, self.store, "radio").result(timeout=2)
                self.assertTrue(page["items"])
            finally:
                release.set()
            writing.result()

    def test_streaming_turn_is_searchable_via_live_overlay_and_indexed_only_when_finalized(self):
        self.store.register(session_id="stream", client="codex", cwd=str(self.root), name="Stream")
        live_history = {
            "id": "stream",
            "turns": [{"id": "turn-live", "status": "inProgress", "items": [{"id": "m1", "type": "agentMessage", "text": "ephemeral live token"}]}],
        }
        self.store.transcripts.save_turns("stream", live_history)
        self.assertTrue(self.store.search_store.flush())
        # In-progress turns are never committed to search.sqlite3.
        self.assertEqual(self.store.search_store.indexed_thread_ids(), set())

        sessions = SimpleNamespace(
            cwd=str(self.root),
            lock=threading.RLock(),
            histories={"stream": live_history},
            active={"stream": "turn-live"},
            persistence=SimpleNamespace(submit=lambda *args, **kwargs: None),
        )
        live_result = search_browser_threads(self.store, sessions, {"q": ["ephemeral live token"]})
        self.assertEqual([r["thread_id"] for r in live_result["items"]], ["stream"])

        live_history["turns"][0]["status"] = "completed"
        self.store.transcripts.save_turns("stream", live_history)
        self.assertTrue(self.store.search_store.flush())
        self.assertEqual(self.store.search_store.indexed_thread_ids(), {"stream"})
        indexed_result = search_threads(self.store, "ephemeral live token", source="messages")
        self.assertEqual([r["thread_id"] for r in indexed_result["items"]], ["stream"])

    def test_unicode_casefold_short_terms_my_messages_and_pagination(self):
        self.seed(40)
        for query in ("radio alarm", "alarm radio", "radio", "strasse", "straße file", "file k", "x", "zzz"):
            for options in ({}, {"source": "context"}, {"source": "messages"}, {"my_messages": True}):
                with self.subTest(query=query, **options):
                    paged = self.pages(query, **options)
                    direct = search_threads(self.store, query, limit=50, **options)["items"]
                    self.assertEqual([i["thread_id"] for i in paged], [i["thread_id"] for i in direct])
                    if query == "zzz":
                        self.assertEqual(paged, [])

    def test_context_follows_external_writers_immediately_under_write_lock(self):
        self.store.register(session_id="a", client="codex", cwd=str(self.root), name="Plain")
        self.store.threads.capture_request("a", "Nothing yet")
        ids = lambda query: [item["thread_id"] for item in search_threads(self.store, query)["items"]]
        self.assertEqual(ids("harbor"), [])
        older = sqlite3.connect(self.store.database_path, isolation_level=None)
        self.addCleanup(older.close)
        older.execute("UPDATE work_threads SET title = 'Harbor plan' WHERE thread_id = 'a'")
        self.assertEqual(ids("harbor"), ["a"])
        self.store.threads.checkpoint(
            "a",
            {
                "phase": "investigation",
                "summary": "Pier lighting",
                "next_action": "",
                "next_actor": "nobody",
                "links": [{"kind": "document", "label": "Lantern", "target": "docs/l.md"}],
            },
        )
        self.assertEqual(ids("pier lantern"), ["a"])
        older.execute("DELETE FROM thread_links WHERE thread_id = 'a'")
        self.assertEqual(ids("lantern"), [])
        older.execute("UPDATE work_threads SET original_request = 'Dock  Schedule' WHERE thread_id = 'a'")
        older.execute("BEGIN IMMEDIATE")
        try:
            self.assertEqual(ids("dock"), ["a"])
        finally:
            older.execute("ROLLBACK")

    def test_route_boundary_matches_the_overview_without_projecting_threads(self):
        from test_codex_app_server import BrowserSessions, FakeCodex

        sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(sessions.close)
        repo, nested = self.root / "repo", self.root / "nested"
        (repo / ".git").mkdir(parents=True)
        nested.mkdir()
        for identity, directory, placement in (
            ("now", self.root, "now"),
            ("later", repo, "later"),
            ("closed", nested, "archived"),
            ("outside", self.root.parent / "elsewhere", "now"),
            ("parent", repo, "now"),
            ("worker", self.root, "now"),
        ):
            self.store.register(session_id=identity, client="codex", cwd=str(directory), name="Search target")
            self.store.threads.update(identity, attention=placement)
        project = self.store.threads.organization.create_project("Launch")
        self.store.threads.update("later", project_id=project["id"])
        with self.store._connection() as db:
            db.execute(
                """INSERT INTO delegations (delegation_id, parent_session_id, child_session_id, client, cwd,
                write_scope_json, instructions, status, mode, created_at, updated_at)
                VALUES ('d', 'parent', 'worker', 'codex', ?, '[]', 'Work', 'launched', 'terminal', 1, 1)""",
                (str(self.root),),
            )
        overview = sessions.list_work_threads() + sessions.list_work_threads(archived=True)
        with self.store._connection() as db:
            listed = listed_threads(self.store.threads, db, sessions.cwd)
        fields = ("thread_id", "attention", "project_cwd", "repository_id", "project_id")
        self.assertEqual(
            sorted(tuple(t[f] for f in fields) for t in listed),
            sorted(tuple(t[f] for f in fields) for t in overview),
        )
        self.assertEqual({t["thread_id"] for t in listed}, {"now", "later", "closed", "parent"})

        def previous_boundary(query):
            threads, scope = overview, query["scope"][0]
            if scope == "current":
                show = query.get("show", ["active"])[0]
                placement = "archived" if show == "archived" else "later" if show == "later" else "now"
                threads = [t for t in threads if t["attention"] == placement]
                for field in ("repository", "project"):
                    if query.get(field, [""])[0]:
                        selected = None if query[field][0] == "__none__" else query[field][0]
                        threads = [t for t in threads if t[field + "_id"] == selected]
            if scope != "all_projects" and "project_cwd" in query:
                threads = [t for t in threads if t["project_cwd"] == query["project_cwd"][0]]
            return sorted(t["thread_id"] for t in threads)

        with patch.object(sessions, "list_work_threads", side_effect=AssertionError("Projected the inventory")), patch.object(
            thread_search, "search_threads", wraps=thread_search.search_threads
        ) as search:
            for query in (
                {"scope": ["all"]},
                {"scope": ["all_projects"]},
                {"scope": ["current"]},
                {"scope": ["current"], "show": ["later"], "project": [project["id"]]},
                {"scope": ["current"], "show": ["archived"]},
                {"scope": ["current"], "repository": ["__none__"]},
                {"scope": ["all"], "project_cwd": [str(repo)]},
            ):
                with self.subTest(query=query):
                    search_browser_threads(self.store, sessions, {**query, "q": ["search target"]})
                    self.assertEqual(sorted(search.call_args.kwargs["thread_ids"]), previous_boundary(query))
        with patch.object(sessions, "list_work_threads", wraps=sessions.list_work_threads) as projected:
            search_browser_threads(self.store, sessions, {"q": ["search target"], "scope": ["current"], "show": ["attention"]})
        self.assertEqual(projected.call_count, 1)


if __name__ == "__main__":
    unittest.main()
