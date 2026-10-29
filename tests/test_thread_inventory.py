from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))

from agent_coord.attention import MODEL, POLICY_VERSION
from agent_coord.codex_app_server import BrowserSessions
from agent_coord.dispatch_runtime import DispatchRuntime
from agent_coord.store import CoordinationStore
from agent_coord.thread_inventory import ThreadInventory


class ThreadInventoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.now = 1000.0
        self.store = CoordinationStore(self.root / "state.sqlite3", clock=lambda: self.now)
        # Exercise the real inventory/status methods without starting providers
        # or background workers, which are unrelated to an inventory read.
        self.runtime = BrowserSessions.__new__(BrowserSessions)
        self.runtime.store = self.store
        self.runtime.cwd = None
        self.runtime.lock = threading.RLock()
        self.runtime.requests = {}
        self.runtime.active = {}
        self.runtime.loaded = set()
        self.runtime.inventory_lock = threading.Lock()
        self.runtime.inventory_generation = 0
        self.runtime.inventory_builds = {}
        self.runtime.creators_cache = None
        self.runtime.dispatch = DispatchRuntime.__new__(DispatchRuntime)
        self.runtime.dispatch.store = self.store
        with self.store._connection() as db:
            db.execute("CREATE TABLE browser_sessions (thread_id TEXT PRIMARY KEY)")
            db.execute("""CREATE TABLE dispatch_settings (thread_id TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 0, defaults_json TEXT NOT NULL DEFAULT '{}')""")

    def register(self, thread_id, *, browser=False, cwd=None):
        self.store.register(session_id=thread_id, client="codex", cwd=str(cwd or self.root))
        if browser:
            with self.store._connection() as db:
                db.execute("INSERT INTO browser_sessions VALUES (?)", (thread_id,))

    @contextmanager
    def trace(self):
        queries = []
        connections = []
        connect = self.store._connect

        def traced():
            connection = connect()
            connection.set_trace_callback(queries.append)
            connections.append(connection)
            return connection

        with patch.object(self.store, "_connect", side_effect=traced):
            yield queries, connections

    def test_inventory_query_count_does_not_grow_with_thread_count(self):
        for i in range(2):
            self.register(str(i), browser=True)
        with self.trace() as (small, small_connections):
            self.assertEqual(len(self.runtime.list_work_threads()), 2)
        for i in range(2, 102):
            self.register(str(i), browser=True)
        with self.trace() as (large, large_connections):
            self.assertEqual(len(self.runtime.list_work_threads()), 102)
        self.assertEqual(len(large), len(small))
        self.assertLess(len(large), 30)
        self.assertEqual((len(small_connections), len(large_connections)), (1, 1))
        self.assertFalse(any(query.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for query in large))

    def test_batch_matches_individual_reads_for_status_attention_and_lineage(self):
        for thread_id in ("parent", "running", "question", "result", "stale", "fork", "archived-fork"):
            self.register(thread_id, browser=True)
        group = self.store.threads.organization.create_project("Launch", str(self.root))
        self.store.threads.update("result", project_id=group["id"], pinned=True)
        self.store.threads.start_turn("result", prompt="Implement the change", turn_id="result-turn")
        self.store.threads.checkpoint("result", {"phase": "implementation", "summary": "Implemented."})
        checkpoint = self.store.threads.checkpoint("result", {"phase": "finished", "summary": "Delivered."})["checkpoint"]
        self.store.threads.finish_turn("result")
        completion = self.store.threads.get("result")["turn_completion"]
        self.store.threads.add_link("result", {"kind": "document", "target": "plan.md"})
        with self.store._connection() as db:
            db.execute("""INSERT INTO response_classification_cache
                (completion_id, checkpoint_id, model, policy_version, status, choice, confidence,
                 probabilities_json, attempts, updated_at)
                VALUES (?, ?, ?, ?, 'classified', 'done', 0.9, ?, 1, ?)""",
                (completion["id"], checkpoint["id"], MODEL, POLICY_VERSION, json.dumps({"done": 0.9}), self.now))
            db.execute("UPDATE sessions SET ended_at = ? WHERE session_id = 'parent'", (self.now,))
            db.execute("UPDATE sessions SET turn_active = 1 WHERE session_id = 'running'")
            db.execute("INSERT INTO dispatch_settings(thread_id, enabled) VALUES ('parent', 1)")
        self.store.threads.start_turn("stale", prompt="First", turn_id="old")
        self.store.threads.checkpoint("stale", {"phase": "planning", "summary": "Old plan."})
        self.now += 1
        self.store.threads.start_turn("stale", prompt="Second", turn_id="new")
        for thread_id in ("fork", "archived-fork"):
            self.store.threads.record_fork(thread_id, self.store.threads.get("parent"), turn_id="fork-point")
        self.store.threads.update("archived-fork", attention="archived", title="Renamed archived fork")
        self.runtime.loaded.add("parent")
        self.runtime.requests["approval"] = {"params": {"threadId": "question"}, "received_at": self.now}
        self.runtime.active["running"] = "live"
        for archived in (False, True):
            expected = [self.runtime.work_thread(thread["thread_id"], history=False)
                        for thread in self.store.threads.list(archived=archived)]
            self.assertEqual(self.runtime.list_work_threads(archived=archived), expected)

    def test_store_list_uses_the_same_batch_projection(self):
        self.register("a")
        self.register("b", cwd=self.root / "other")
        with self.store._connection() as db:
            batch = ThreadInventory(self.store.threads, db)
            actual = [batch.read(db, row) for row in batch.rows]
        self.assertEqual(actual, self.store.threads.list())
        with self.trace() as (queries, connections):
            self.assertEqual([thread["thread_id"] for thread in self.store.threads.list(cwd=str(self.root))], ["a"])
        self.assertEqual(len(connections), 1)
        self.assertLess(len(queries), 15)

    def test_inventory_does_not_resume_or_read_provider_conversations(self):
        self.register("saved", browser=True)
        with patch.object(self.runtime, "read", side_effect=AssertionError("provider read")), \
             patch.object(self.store, "get_session", side_effect=AssertionError("per-thread lookup")):
            self.assertEqual(self.runtime.list_work_threads()[0]["thread_id"], "saved")

    def test_concurrent_archive_cannot_split_the_inventory_snapshot(self):
        self.register("moving", browser=True)
        load = ThreadInventory.load_runtime

        def archive_then_load(inventory, db):
            with self.store._connection() as writer:
                writer.execute("UPDATE work_threads SET attention = 'archived', title = 'Changed' WHERE thread_id = 'moving'")
            load(inventory, db)

        with patch.object(ThreadInventory, "load_runtime", archive_then_load):
            listed = self.runtime.list_work_threads()
        self.assertEqual(listed[0]["thread_id"], "moving")
        self.assertEqual(listed[0]["attention"], "now")
        self.assertNotEqual(listed[0]["title"], "Changed")
        self.assertEqual(self.runtime.list_work_threads(), [])
        self.assertEqual(self.runtime.list_work_threads(archived=True)[0]["title"], "Changed")

    def test_legacy_repair_reopens_the_snapshot_before_writing(self):
        self.register("legacy")
        with self.store._connection() as db:
            db.execute("DELETE FROM thread_organization WHERE thread_id = 'legacy'")
        base = ThreadInventory._base
        changed = False

        def write_after_snapshot(inventory, db):
            nonlocal changed
            data = base(inventory, db)
            if not changed:
                changed = True
                with self.store._connection() as writer:
                    writer.execute("UPDATE work_threads SET title = 'New title' WHERE thread_id = 'legacy'")
            return data

        with patch.object(ThreadInventory, "_base", write_after_snapshot), \
             patch.object(self.store.threads.organization, "backfill", side_effect=AssertionError("global repair")):
            listed = self.runtime.list_work_threads()
        self.assertEqual(listed[0]["title"], "New title")
        with self.store._connection() as db:
            self.assertIsNotNone(db.execute("SELECT 1 FROM thread_organization WHERE thread_id = 'legacy'").fetchone())

    def test_due_snoozes_still_wake_before_filtering_the_inventory(self):
        self.register("later")
        self.store.threads.update("later", snoozed_until=self.now + 10)
        self.now += 11
        listed = self.runtime.list_work_threads()
        self.assertEqual(listed[0]["attention"], "now")
        self.assertTrue(listed[0]["snooze_due"])
        self.assertTrue(listed[0]["needs_attention"])


if __name__ == "__main__":
    unittest.main()
