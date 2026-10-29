from __future__ import annotations

import heapq
import inspect
import json
import random
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))

from agent_coord import thread_search
from agent_coord.cli import _parser, run
from agent_coord.navigation import NavigationStore
from agent_coord.store import CoordinationError, CoordinationStore, _StoreConnection
from agent_coord.thread_inventory import listed_threads
from agent_coord.thread_search import search_browser_threads, search_threads


def _reference_span(folded, terms, phrase):
    # The pre-SQL ranking, kept verbatim so the reference cannot drift with the module.
    if phrase and (position := folded.find(phrase)) >= 0:
        return position, position + len(phrase)
    positions = [(folded.find(term), i) for i, term in enumerate(terms)]
    if not positions or any(position < 0 for position, _ in positions):
        return None
    heapq.heapify(positions)
    right = max(position + len(terms[i]) for position, i in positions)
    best = (positions[0][0], right)
    while positions:
        left, i = heapq.heappop(positions)
        if right - left < best[1] - best[0]:
            best = (left, right)
        following = folded.find(terms[i], left + 1)
        if following < 0:
            break
        heapq.heappush(positions, (following, i))
        right = max(right, following + len(terms[i]))
    return best


def _reference_relevance(value, terms, phrase):
    folded = " ".join((value or "").casefold().split())
    span = _reference_span(folded, terms, phrase)
    if span is None:
        return 0
    gap = max(0, span[1] - span[0] - sum(map(len, terms)))
    return 1 + max(0, 24 - gap // 8) + 32 * (phrase in folded) + 16 * (folded == phrase)


def reference_search(store, query, *, source="all", my_messages=False):
    """Rank every thread by brute force with the previous search semantics."""
    terms = list(dict.fromkeys(query.casefold().split()))
    phrase = " ".join(query.casefold().split())
    source = "messages" if my_messages else source
    relevance = lambda value: _reference_relevance(value, terms, phrase)
    found = []
    with store._connection() as db:
        for thread in db.execute("""SELECT t.* FROM work_threads t JOIN sessions s ON s.session_id = t.thread_id
                JOIN work_projects w ON w.id = t.project_id JOIN thread_organization o ON o.thread_id = t.thread_id"""):
            identity = thread["thread_id"]
            checkpoint = db.execute("SELECT * FROM thread_checkpoints WHERE thread_id = ? ORDER BY id DESC LIMIT 1",
                                    (identity,)).fetchone()
            done = db.execute("SELECT * FROM turn_completions WHERE thread_id = ? ORDER BY id DESC LIMIT 1",
                              (identity,)).fetchone()
            summary = (checkpoint["summary"] if checkpoint else "") + " " + (checkpoint["next_action"] if checkpoint else "")
            fields = {"title": thread["title"], "summary": summary, "request": thread["original_request"]}
            artifacts = [row[0] + " " + row[1] for row in db.execute(
                "SELECT label, target FROM thread_links WHERE thread_id = ?", (identity,))]
            folded = {name: value.casefold() for name, value in fields.items()}
            context = all(any(term in value for value in folded.values())
                          or any(term in link.casefold() for link in artifacts) for term in terms)
            messages = [row for row in db.execute(
                "SELECT * FROM conversation_search_messages WHERE thread_id = ?", (identity,))
                if all(term in row["folded"] for term in terms) and (not my_messages or row["role"] == "user")]
            if not {"context": context, "messages": bool(messages)}.get(source, context or messages):
                continue
            score = 0
            if source != "messages":
                for term in terms:
                    score += (8 * (term in folded["title"]) + 4 * (term in folded["summary"]) + 2 * (term in folded["request"])
                              + any(term in link.casefold() for link in artifacts))
                score += 3 * relevance(fields["title"]) + relevance(fields["summary"]) + relevance(fields["request"])
            if source != "context":
                score += max((relevance(row["text"]) for row in messages), default=0)
            ranked = sorted(messages, key=lambda row: (relevance(row["text"]), row["position"]), reverse=True)
            found.append({"thread_id": identity, "score": score,
                          "last_work_at": max(thread["created_at"], thread["turn_started_at"] or 0,
                                              done["completed_at"] if done else 0, checkpoint["created_at"] if checkpoint else 0),
                          "message_count": len(messages) if source != "context" else 0,
                          "matches": [(row["turn_id"], row["item_id"], row["timestamp"]) for row in ranked[:3]]
                          if source != "context" else []})
    return sorted(found, key=lambda item: (item["score"], item["last_work_at"], item["thread_id"]), reverse=True)


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

    def test_phrase_and_exact_title_rank_before_scattered_title_keywords(self):
        self.create("a-exact", title="Radio alarm", request="Schedule audio")
        self.create("b-phrase", title="Repair radio alarm scheduling", request="Schedule audio")
        self.now = 5000
        self.create("z-scattered", title="Radio playback and alarm scheduling", request="Schedule audio")
        self.assertEqual([r["thread_id"] for r in search_threads(self.store, "radio alarm")["items"]],
                         ["a-exact", "b-phrase", "z-scattered"])

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

    def test_app_context_advice_is_available_for_routing_without_handover_bloat(self):
        from test_codex_app_server import BrowserSessions, FakeCodex
        sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(sessions.close)
        thread = sessions.create({"name": "Database app specialist"})["session"]["thread_id"]
        self.store.threads.start_turn(thread, turn_id="research")
        report = {"recommendation": "reuse", "reason": "Useful source knowledge.",
                  "reuse_for": ["Source questions"], "fresh_for": ["Independent review"],
                  "handover": {"confirmed_facts": ["The index fits the query."], "constraints": [],
                               "tried_and_failed": [], "open_questions": []}}
        checkpoint = self.checkpoint(thread, context_lifecycle=report)["checkpoint"]
        result = search_threads(self.store, "database", app_only=True, source="context")["items"][0]
        self.assertEqual(result["context_lifecycle"], {"checkpoint_id": checkpoint["id"], "stale": False,
            **{key: report[key] for key in ("recommendation", "reason", "reuse_for", "fresh_for")}})
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
        # Navigation links are built from the returned page without hydrating threads.
        with patch.object(self.store.threads, "get", wraps=self.store.threads.get) as get:
            page = search_threads(self.store, "database", limit=2)
        self.assertEqual(get.call_count, 0)
        self.assertEqual([item["url"] for item in page["items"]],
                         [NavigationStore(self.store).link(thread=item["thread_id"])["url"] for item in page["items"]])
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


class SearchExecutionTests(unittest.TestCase):
    """Timing-independent guards for SQLite-native ranking and the app boundary."""

    WORDS = ["radio", "Radio", "ALARM", "alarm", "straße", "STRASSE", "ﬁle", "file", "K", "k",
             "okc", "moe", "the", "and", "x"]
    SPACES = [" ", " ", " ", "  ", "\n", "\t", " ", "　", " \n "]

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.now = 1793247626.5940921
        self.store = CoordinationStore(self.root / "state.sqlite3", clock=lambda: self.now)
        with self.store._connection() as db:
            db.execute("CREATE TABLE browser_history (thread_id TEXT PRIMARY KEY, history_json TEXT NOT NULL)")

    def text(self, generator, count):
        return "".join(generator.choice(self.WORDS) + generator.choice(self.SPACES) for _ in range(count)).strip()

    def seed(self, count):
        """Threads whose context and messages mix case, Unicode folding and spacing."""
        generator = random.Random(20261029)
        for index in range(count):
            identity = f"thread-{index:03}"
            # Repeated times exercise the thread ID tie-break.
            self.now += generator.choice([0, 0, 0.125, 1.0000001, 3.3])
            self.store.register(session_id=identity, client="codex", cwd=str(self.root), name="Seed")
            self.store.threads.capture_request(identity, self.text(generator, generator.randrange(1, 12)))
            with self.store._connection() as db:
                db.execute("UPDATE work_threads SET title = ? WHERE thread_id = ?",
                           (generator.choice(["radio alarm", self.text(generator, 3)]), identity))
            if generator.random() < 0.7:
                self.store.threads.checkpoint(identity, {"phase": "investigation", "summary": self.text(generator, 6),
                    "next_action": self.text(generator, 2), "next_actor": "user",
                    "links": [{"kind": "document", "label": self.text(generator, 2), "target": f"docs/{index}.md"}]})
            if generator.random() < 0.3:
                self.store.threads.update(identity, attention="archived")
            items = []
            for position in range(generator.randrange(0, 9)):
                text = generator.choice(["radio alarm", " Radio\nALARM ", "　radio alarm\n", " Radio ", "\tx\n",
                                         self.text(generator, generator.randrange(1, 30))])
                if generator.random() < 0.5:
                    items.append({"id": f"m{position}", "type": "agentMessage", "text": text})
                else:
                    items.append({"id": f"m{position}", "type": "userMessage", "timelineAt": self.now + position / 7,
                                  "content": [{"type": "text", "text": text}]})
            with self.store._connection() as db:
                db.execute("INSERT INTO browser_history VALUES (?, ?)", (identity, json.dumps(
                    {"turns": [{"id": "turn", "startedAt": self.now, "items": items}]})))

    def pages(self, query, **options):
        found, cursor = [], None
        while True:
            page = search_threads(self.store, query, limit=4, cursor=cursor, **options)
            found += page["items"]
            cursor = page["next_cursor"]
            if cursor is None:
                return found

    def test_ranking_pages_and_excerpts_match_previous_semantics(self):
        self.seed(70)
        for query in ("radio alarm", "alarm radio", "radio radio", "radio", "strasse", "straße file", "file k",
                      "okc moe the", "the and", "x", "zzz"):
            for options in ({}, {"source": "context"}, {"source": "messages"}, {"my_messages": True}):
                with self.subTest(query=query, **options), \
                        patch.object(thread_search, "_MEASURE_BATCH", 3), \
                        patch.object(thread_search, "_relevance", wraps=thread_search._relevance) as measured:
                    found = self.pages(query, **options)
                    expected = reference_search(self.store, query, **options)
                    self.assertEqual([(i["thread_id"], i["score"], i["last_work_at"], i["message_count"]) for i in found],
                                     [(i["thread_id"], i["score"], i["last_work_at"], i["message_count"]) for i in expected])
                    self.assertEqual([[(m["turn_id"], m["item_id"], m["timestamp"]) for m in i["matches"]] for i in found],
                                     [i["matches"] for i in expected])
                    if " " not in query:
                        # One keyword is its own phrase: SQLite settles every score.
                        self.assertEqual(measured.call_count, 0)
        with patch.object(thread_search, "_relevance", wraps=thread_search._relevance) as measured:
            self.pages("radio alarm")
        self.assertGreater(measured.call_count, 0, "The fixture must exercise proximity measurement")

    def test_search_uses_fixed_statements_without_python_callbacks(self):
        self.seed(12)
        statements = []
        connect = self.store._connect

        def traced():
            connection = connect()
            connection.set_trace_callback(statements.append)
            return connection

        counts = {}
        with patch.object(_StoreConnection, "create_function", side_effect=AssertionError("Per-row Python callback")), \
                patch.object(self.store, "_connect", traced):
            search_threads(self.store, "radio")
            for query in ("radio", "alarm", "x", "zzzz"):
                statements.clear()
                page = search_threads(self.store, query, limit=50)
                counts[query, len(page["items"])] = len(statements)
        # The statement count is independent of how many conversations and messages match.
        self.assertEqual(len(set(counts.values())), 1, counts)
        self.assertLessEqual(next(iter(counts.values())), 14, statements)
        self.assertNotIn("create_function", inspect.getsource(thread_search))

    def test_current_indexes_are_searched_without_the_writer_lock(self):
        self.seed(8)
        search_threads(self.store, "radio")
        writer = sqlite3.connect(self.store.database_path, isolation_level=None)
        self.addCleanup(writer.close)
        writer.execute("BEGIN IMMEDIATE")
        try:
            # A write would wait for the busy timeout and then fail.
            page = search_threads(self.store, "radio")
        finally:
            writer.execute("ROLLBACK")
        self.assertTrue(page["items"])

    def test_context_follows_other_writers_and_a_busy_writer_only_defers_the_cache(self):
        self.store.register(session_id="a", client="codex", cwd=str(self.root), name="Plain")
        self.store.threads.capture_request("a", "Nothing yet")
        ids = lambda query: [item["thread_id"] for item in search_threads(self.store, query)["items"]]
        self.assertEqual(ids("harbor"), [])
        older = sqlite3.connect(self.store.database_path, isolation_level=None)
        self.addCleanup(older.close)
        # An older runtime changes saved context without updating the projection.
        older.execute("UPDATE work_threads SET title = 'Harbor plan' WHERE thread_id = 'a'")
        self.assertEqual(ids("harbor"), ["a"])
        self.store.threads.checkpoint("a", {"phase": "investigation", "summary": "Pier lighting", "next_action": "",
            "next_actor": "nobody", "links": [{"kind": "document", "label": "Lantern", "target": "docs/l.md"}]})
        self.assertEqual(ids("pier lantern"), ["a"])
        older.execute("DELETE FROM thread_links WHERE thread_id = 'a'")
        self.assertEqual(ids("lantern"), [])
        stored = lambda: older.execute(
            "SELECT request FROM conversation_search_context WHERE thread_id = 'a'").fetchone()
        self.assertEqual(stored(), ("nothing yet",))
        older.execute("UPDATE work_threads SET original_request = 'Dock  Schedule' WHERE thread_id = 'a'")
        self.assertIsNone(stored())
        older.execute("BEGIN IMMEDIATE")
        try:
            self.assertEqual(ids("dock"), ["a"])
        finally:
            older.execute("ROLLBACK")
        self.assertIsNone(stored())
        self.assertEqual(ids("dock"), ["a"])
        self.assertEqual(stored(), ("dock schedule",))

    def test_context_computed_before_a_concurrent_change_is_not_stored(self):
        self.store.register(session_id="a", client="codex", cwd=str(self.root), name="Plain")
        self.store.threads.capture_request("a", "Harbor survey")
        changed = []
        save = thread_search._save_context

        def change_then_save(db, overlay):
            # Another runtime commits between this search's snapshot and its cache write.
            if overlay and not changed:
                changed.append(True)
                with sqlite3.connect(self.store.database_path) as other:
                    other.execute("UPDATE work_threads SET original_request = 'Dock survey' WHERE thread_id = 'a'")
            save(db, overlay)

        with patch.object(thread_search, "_save_context", change_then_save):
            self.assertEqual([i["thread_id"] for i in search_threads(self.store, "harbor")["items"]], ["a"])
        with self.store._connection() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM conversation_search_context WHERE thread_id = 'a'").fetchone())
        self.assertEqual(search_threads(self.store, "harbor")["items"], [])
        self.assertEqual([i["thread_id"] for i in search_threads(self.store, "dock")["items"]], ["a"])

    def test_route_boundary_matches_the_overview_without_projecting_threads(self):
        from test_codex_app_server import BrowserSessions, FakeCodex
        sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(sessions.close)
        repo, nested = self.root / "repo", self.root / "nested"
        (repo / ".git").mkdir(parents=True)
        nested.mkdir()
        for identity, directory, placement in (("now", self.root, "now"), ("later", repo, "later"),
                                               ("closed", nested, "archived"), ("outside", self.root.parent / "elsewhere", "now"),
                                               ("parent", repo, "now"), ("worker", self.root, "now")):
            self.store.register(session_id=identity, client="codex", cwd=str(directory), name="Search target")
            self.store.threads.update(identity, attention=placement)
        project = self.store.threads.organization.create_project("Launch")
        self.store.threads.update("later", project_id=project["id"])
        with self.store._connection() as db:
            db.execute("""INSERT INTO delegations (delegation_id, parent_session_id, child_session_id, client, cwd,
                write_scope_json, instructions, status, mode, created_at, updated_at)
                VALUES ('d', 'parent', 'worker', 'codex', ?, '[]', 'Work', 'launched', 'terminal', 1, 1)""", (str(self.root),))
        overview = sessions.list_work_threads() + sessions.list_work_threads(archived=True)
        with self.store._connection() as db:
            listed = listed_threads(self.store.threads, db, sessions.cwd)
        fields = ("thread_id", "attention", "project_cwd", "repository_id", "project_id")
        self.assertEqual(sorted(tuple(t[f] for f in fields) for t in listed),
                         sorted(tuple(t[f] for f in fields) for t in overview))
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

        with patch.object(sessions, "list_work_threads", side_effect=AssertionError("Projected the inventory")), \
                patch.object(thread_search, "search_threads", wraps=thread_search.search_threads) as search:
            for query in ({"scope": ["all"]}, {"scope": ["all_projects"]}, {"scope": ["current"]},
                          {"scope": ["current"], "show": ["later"], "project": [project["id"]]},
                          {"scope": ["current"], "show": ["archived"]}, {"scope": ["current"], "repository": ["__none__"]},
                          {"scope": ["all"], "project_cwd": [str(repo)]}):
                with self.subTest(query=query):
                    search_browser_threads(self.store, sessions, {**query, "q": ["search target"]})
                    self.assertEqual(sorted(search.call_args.kwargs["thread_ids"]), previous_boundary(query))
        # Attention and stage are live projections, built once when the view filters on them.
        with patch.object(sessions, "list_work_threads", wraps=sessions.list_work_threads) as projected:
            search_browser_threads(self.store, sessions, {"q": ["search target"], "scope": ["current"], "show": ["attention"]})
        self.assertEqual(projected.call_count, 1)

    def test_numbers_cross_sqlite_json_exactly(self):
        generator = random.Random(7)
        values = [0.0, 1.0, 1234, -2.5, 1793247626.5940921, 2.0 ** -40, *(generator.uniform(1.6e9, 1.9e9) for _ in range(2000)),
                  *(generator.uniform(0.001, 1) for _ in range(500)), 1e-30, 2.0 ** 70, None]
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.execute("CREATE TABLE numbers (value)")
        db.executemany("INSERT INTO numbers VALUES (?)", [(value,) for value in values])
        encoded = json.loads(db.execute(f"SELECT json_group_array({thread_search._number('value')}) FROM "
                                        "(SELECT value FROM numbers ORDER BY rowid)").fetchone()[0])
        decoded = [thread_search._decode_number(value) for value in encoded]
        self.assertEqual(decoded[:-3], values[:-3])
        self.assertEqual([type(value) for value in decoded], [type(value) for value in values])
        self.assertAlmostEqual(decoded[-3], 1e-30, delta=1e-44)
        self.assertAlmostEqual(decoded[-2] / 2.0 ** 70, 1.0, places=12)
        self.assertIsNone(decoded[-1])

    def test_whitespace_separators_cover_every_unicode_space(self):
        self.assertEqual(thread_search._SPACE, "".join(chr(code) for code in range(sys.maxunicode + 1) if chr(code).isspace()))


if __name__ == "__main__":
    unittest.main()
