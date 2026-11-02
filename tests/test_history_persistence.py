from __future__ import annotations

import copy
import json
import random
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import closing, contextmanager
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))

from agent_coord import history_persistence as history_storage
from agent_coord.history_persistence import (CHUNK_BYTES, INLINE_BYTES, MARKER, STAGE_BYTES,
    HistoryPersistence, HistoryUnavailableError, decode_history, ensure_schema,
    prepare_history, publish_history, read_history, stage_history)
from agent_coord.store import CoordinationStore


def large_history(identity="conversation", size=1024 * 1024):
    content = random.Random(71).randbytes(size).hex()
    return {"id": identity, "status": {"type": "idle"}, "extra": {"unicode": "λ雪"}, "turns": [
        {"id": "old", "status": "completed", "items": [
            {"id": "command", "type": "commandExecution", "aggregatedOutput": content},
            {"id": "answer", "type": "agentMessage", "text": "Saved answer"}]},
        {"id": "latest", "status": "inProgress", "items": [
            {"id": "request", "type": "userMessage", "content": [{"type": "text", "text": "Continue"}]}]}]}


class HistoryPersistenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.store = CoordinationStore(self.directory / "state.sqlite3")
        with self.store._connection() as db:
            db.execute("CREATE TABLE browser_history (thread_id TEXT PRIMARY KEY, history_json TEXT NOT NULL)")
            ensure_schema(db)
            db.execute("CREATE TABLE history_changes (thread_id TEXT)")
            for event in ("INSERT", "UPDATE"):
                db.execute(f"""CREATE TRIGGER history_changes_{event.lower()} AFTER {event} ON browser_history
                    BEGIN INSERT INTO history_changes VALUES (NEW.thread_id); END""")
        self.persistence = HistoryPersistence(self.store)

    def raw(self):
        with self.store.reading(), self.store._connection() as db:
            return db.execute("SELECT history_json FROM browser_history WHERE thread_id='conversation'").fetchone()[0]

    def test_large_history_is_lossless_and_small_legacy_history_stays_inline(self):
        small = {"id": "conversation", "turns": []}
        self.persistence.save("conversation", small)
        self.assertEqual(json.loads(self.raw()), small)
        self.assertEqual(decode_history(self.raw()), small)
        history = large_history()
        result = self.persistence.save("conversation", history)
        self.assertLess(len(self.raw()), 1000)
        self.assertEqual(json.loads(self.raw())[MARKER], 1)
        self.assertEqual(read_history(self.store, "conversation"), history)
        self.assertEqual(decode_history(self.raw(), database_path=self.store.database_path), history)
        self.assertGreater(result["staged_chunks"], 10)

    def test_one_item_change_reuses_the_large_previous_turn_and_unchanged_save_does_not_invalidate(self):
        history = large_history(size=4 * 1024 * 1024)
        initial = self.persistence.save("conversation", history)
        repeated = self.persistence.save("conversation", copy.deepcopy(history))
        self.assertEqual((repeated["staged_bytes"], repeated["published_bytes"], repeated["unchanged_snapshots"]),
                         (0, 0, 1))
        history["turns"][-1]["items"].append({"id": "next", "type": "agentMessage", "text": "A new answer"})
        history["turns"][-1]["status"] = "completed"
        changed = self.persistence.save("conversation", history)
        self.assertLess(changed["staged_bytes"], 4096)
        self.assertGreater(changed["reused_bytes"], initial["staged_bytes"] - 4096)
        self.assertLess(changed["published_bytes"], 1000)
        self.assertEqual(read_history(self.store, "conversation"), history)
        with self.store.reading(), self.store._connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM history_changes").fetchone()[0], 2)

    def test_appending_to_large_item_reuses_prefix_blocks(self):
        history = large_history()
        self.persistence.save("conversation", history)
        history["turns"][0]["items"][0]["aggregatedOutput"] += "\nlast output λ"
        result = self.persistence.save("conversation", history)
        self.assertLess(result["staged_bytes"], CHUNK_BYTES + 8192)
        self.assertGreater(result["reused_bytes"], 1024 * 1024)
        self.assertEqual(read_history(self.store, "conversation"), history)

    def test_retry_and_one_item_change_do_not_reserialize_unchanged_large_items(self):
        history = large_history()
        self.persistence.save("conversation", history)
        history["turns"][-1]["items"].append({"id": "next", "type": "agentMessage", "text": "New answer"})
        serialize = history_storage._json

        def bounded_preparation(value):
            if isinstance(value, dict):
                self.assertNotIn("aggregatedOutput", value, "the unchanged large item was serialized again")
                self.assertIsNot(value, history, "the entire history was serialized again")
            return serialize(value)

        with patch.object(history_storage, "_json", side_effect=bounded_preparation):
            with patch.object(history_storage, "publish_history", side_effect=sqlite3.OperationalError("database is locked")):
                with self.assertRaises(sqlite3.OperationalError):
                    self.persistence.save("conversation", history)
            self.persistence.save("conversation", history)
        self.assertEqual(read_history(self.store, "conversation"), history)

    def test_preparation_cache_is_bounded_and_can_release_a_conversation(self):
        with patch.object(history_storage, "PREPARATION_CACHE_THREADS", 1):
            self.persistence.save("one", large_history("one"))
            self.persistence.save("two", large_history("two"))
        self.assertEqual(self.persistence.status()["preparation_cache_threads"], 1)
        self.persistence.forget("two")
        self.assertEqual(self.persistence.status()["preparation_cache_bytes"], 0)
        with patch.object(history_storage, "PREPARATION_CACHE_BYTES", 1):
            self.persistence.save("one", large_history("one"))
        self.assertEqual(self.persistence.status()["preparation_cache_threads"], 0)

    def test_confirmed_chunks_skip_membership_reads_and_cache_only_committed_data(self):
        history = large_history()
        self.persistence.save("conversation", history)
        confirmed = set(self.persistence.confirmed)
        connection = self.store._connection

        class ObservedConnection:
            def __init__(self, db):
                self.db = db

            def execute(self, sql, values=()):
                if sql.startswith("SELECT digest FROM browser_history_chunks"):
                    self_test.assertFalse(confirmed.intersection(values))
                return self.db.execute(sql, values)

            def __getattr__(self, key):
                return getattr(self.db, key)

        @contextmanager
        def observed():
            with connection() as db:
                yield ObservedConnection(db)

        self_test = self
        history["turns"][-1]["items"].append({"id": "answer", "type": "agentMessage", "text": "New"})
        with patch.object(self.store, "_connection", observed):
            self.persistence.save("conversation", history)
        fresh = HistoryPersistence(self.store)
        with patch.object(history_storage, "stage_history", side_effect=sqlite3.OperationalError("database is locked")):
            with self.assertRaises(sqlite3.OperationalError):
                fresh.save("conversation", history)
        self.assertFalse(fresh.confirmed)
        with patch.object(history_storage, "CONFIRMED_CHUNKS", 3):
            fresh.save("conversation", history)
        self.assertLessEqual(fresh.status()["confirmed_chunks"], 3)

    def test_staging_is_bounded_and_preparation_does_not_hold_the_writer(self):
        batches = []
        connection = self.store._connection

        class ObservedConnection:
            def __init__(self, db):
                self.db, self.size = db, 0

            def execute(self, sql, values=()):
                if sql.startswith("INSERT OR IGNORE INTO browser_history_chunks"):
                    self.size += len(values[1])
                return self.db.execute(sql, values)

            def __getattr__(self, key):
                return getattr(self.db, key)

        @contextmanager
        def observed():
            with connection() as db:
                observed_db = ObservedConnection(db)
                yield observed_db
                if observed_db.size:
                    batches.append(observed_db.size)

        serialize = history_storage._json
        checked = []

        def prepare(value):
            if not checked:
                ready = threading.Event()

                def write():
                    with connection() as db:
                        db.execute("INSERT INTO history_changes VALUES ('unrelated')")
                    ready.set()

                worker = threading.Thread(target=write)
                worker.start()
                checked.append(ready.wait(1))
                worker.join(1)
            return serialize(value)

        with patch.object(self.store, "_connection", observed), patch.object(history_storage, "_json", prepare):
            self.persistence.save("conversation", large_history())
        self.assertEqual(checked, [True])
        self.assertGreater(len(batches), 4)
        self.assertLessEqual(max(batches), STAGE_BYTES)

    def test_failed_publication_keeps_previous_history_and_staged_blocks_allow_retry(self):
        old = {"id": "conversation", "turns": []}
        self.persistence.save("conversation", old)
        history = large_history()
        with patch.object(history_storage, "publish_history", side_effect=sqlite3.OperationalError("database is locked")):
            with self.assertRaises(sqlite3.OperationalError):
                self.persistence.save("conversation", history)
        self.assertEqual(self.persistence.status()["failures"], 1)
        self.assertGreater(self.persistence.status()["staged_bytes"], 1024 * 1024)
        self.assertEqual(read_history(self.store, "conversation"), old)
        restarted = HistoryPersistence(self.store)
        result = restarted.save("conversation", history)
        self.assertEqual(result["staged_bytes"], 0)
        self.assertEqual(read_history(self.store, "conversation"), history)

    def test_sqlite_backup_contains_complete_history_and_prior_root_remains_readable(self):
        history = large_history()
        self.persistence.save("conversation", history)
        original = self.raw()
        changed = copy.deepcopy(history)
        changed["turns"][0]["items"][1]["text"] = "Updated answer"
        self.persistence.save("conversation", changed)
        destination = self.directory / "backup.sqlite3"
        with closing(sqlite3.connect(self.store.database_path)) as source, closing(sqlite3.connect(destination)) as backup:
            source.backup(backup)
        self.assertEqual(decode_history(original, database_path=destination), history)
        self.assertEqual(decode_history(self.raw(), database_path=destination), changed)

    def test_interrupted_staging_never_exposes_incomplete_new_history(self):
        old = {"id": "conversation", "turns": []}
        self.persistence.save("conversation", old)
        history = large_history()
        prepared = prepare_history(history)

        def interrupt_after_commit(metrics):
            raise OSError("interrupted after a committed block batch")

        with self.assertRaisesRegex(OSError, "interrupted"):
            stage_history(self.store, prepared, on_commit=interrupt_after_commit)
        self.assertEqual(read_history(self.store, "conversation"), old)
        result = HistoryPersistence(self.store).save("conversation", history)
        self.assertGreater(result["reused_bytes"], 0)
        self.assertGreater(result["staged_bytes"], 0)
        self.assertEqual(read_history(self.store, "conversation"), history)

    def test_missing_or_corrupt_chunks_are_explicit_errors(self):
        self.persistence.save("conversation", large_history())
        root = json.loads(self.raw())["root"][0]
        with self.store._connection() as db:
            db.execute("UPDATE browser_history_chunks SET payload=? WHERE digest=?", (b"corrupt", root))
        with self.assertRaisesRegex(HistoryUnavailableError, "integrity"):
            read_history(self.store, "conversation")
        with self.store._connection() as db:
            db.execute("DELETE FROM browser_history_chunks WHERE digest=?", (root,))
        with self.assertRaisesRegex(HistoryUnavailableError, "missing"):
            decode_history(self.raw(), database_path=self.store.database_path)

    def test_existing_legacy_search_trigger_accepts_repeated_publication(self):
        with self.store._connection() as db:
            db.execute("CREATE TABLE legacy_dirty(thread_id TEXT PRIMARY KEY)")
            for event in ("INSERT", "UPDATE"):
                db.execute(f"""CREATE TRIGGER legacy_dirty_{event.lower()} AFTER {event} ON browser_history
                    BEGIN INSERT OR REPLACE INTO legacy_dirty VALUES(NEW.thread_id); END""")
        history = large_history()
        self.persistence.save("conversation", history)
        history["turns"][-1]["status"] = "completed"
        self.persistence.save("conversation", history)
        self.assertEqual(read_history(self.store, "conversation"), history)

    def test_decoder_opens_one_read_connection_and_batches_items(self):
        history = large_history()
        history["turns"][0]["items"].extend({"id": str(index), "text": "answer"} for index in range(400))
        self.persistence.save("conversation", history)
        connect = sqlite3.connect
        with patch.object(history_storage.sqlite3, "connect", wraps=connect) as opened:
            self.assertEqual(decode_history(self.raw(), database_path=self.store.database_path), history)
        self.assertEqual(opened.call_count, 1)


class BrowserHistoryPersistenceTests(unittest.TestCase):
    def setUp(self):
        from agent_coord.codex_app_server import BrowserSessions
        from test_codex_app_server import FakeCodex
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.store = CoordinationStore(self.directory / "state.sqlite3")
        self.sessions = BrowserSessions(self.store, str(self.directory), rpc_factory=FakeCodex)
        self.addCleanup(self.sessions.close)
        self.identity = self.sessions.create({"name": "History validation"})["session"]["thread_id"]
        self.assertTrue(self.sessions.persistence.flush(timeout=3))

    def test_all_provider_readers_and_snapshot_recovery_read_chunked_history(self):
        history = large_history(self.identity)
        history["turns"][-1]["status"] = "completed"
        self.sessions._remember_thread(history)
        self.assertTrue(self.sessions.persistence.flush(timeout=3))
        with self.sessions.lock:
            self.sessions.histories.pop(self.identity)
        self.assertEqual(self.sessions._history(self.identity)["turns"], history["turns"])
        self.assertEqual(self.sessions.read(self.identity, snapshot=True)["thread"]["turns"], history["turns"])
        for adapter in (self.sessions.claude, self.sessions.antigravity, self.sessions.kimi, self.sessions.muse):
            read = getattr(adapter, "_read_history", None) or adapter._history
            self.assertEqual(read(self.identity)["turns"], history["turns"], type(adapter).__name__)

    def test_activity_and_routing_read_complete_history_before_and_after_indexing(self):
        from agent_coord.activity_api import read_activity_history
        from agent_coord.conversation_search import refresh_messages
        from agent_coord.dispatch_snapshot import read_snapshot
        history = large_history(self.identity)
        self.sessions._remember_thread(history)
        self.assertTrue(self.sessions.persistence.flush(timeout=3))
        activity = read_activity_history(self.store, self.identity)
        self.assertIn("Saved answer", [message["text"] for message in activity["messages"]])
        for indexed in (False, True):
            if indexed:
                refresh_messages(self.store)
            snapshot = read_snapshot(self.store.database_path)
            conversation = next(row for row in snapshot["threads"] if row["thread_id"] == self.identity)
            self.assertIn("Saved answer", [message["text"] for message in conversation["messages"]])
        history["turns"][0]["items"][1]["text"] = "Revised answer"
        self.sessions._remember_thread(history)
        self.assertTrue(self.sessions.persistence.flush(timeout=3))
        refresh_messages(self.store)
        snapshot = read_snapshot(self.store.database_path)
        conversation = next(row for row in snapshot["threads"] if row["thread_id"] == self.identity)
        self.assertIn("Revised answer", [message["text"] for message in conversation["messages"]])
        self.assertNotIn("Saved answer", [message["text"] for message in conversation["messages"]])

    def test_terminal_save_retains_final_item_and_does_not_clear_newer_dirty_revision(self):
        history = large_history(self.identity)
        self.sessions._remember_thread(history)
        self.assertTrue(self.sessions.persistence.flush(timeout=3))
        saved = self.sessions.history_persistence.save
        changed = []

        def during_save(identity, snapshot):
            result = saved(identity, snapshot)
            if not changed:
                changed.append(True)
                with self.sessions.lock:
                    self.sessions.histories[identity]["turns"][-1]["items"].append(
                        {"id": "final", "type": "agentMessage", "text": "Final answer"})
                    self.sessions._save_history(identity)
            return result

        with patch.object(self.sessions.history_persistence, "save", side_effect=during_save):
            with self.sessions.lock:
                self.sessions._save_history(self.identity)
            self.assertTrue(self.sessions.persistence.flush(timeout=3))
            self.assertTrue(self.sessions.persistence.flush(timeout=3))
        self.assertNotIn(self.identity, self.sessions.dirty_histories)
        self.assertEqual(read_history(self.store, self.identity)["turns"][-1]["items"][-1]["text"], "Final answer")


if __name__ == "__main__":
    unittest.main()
