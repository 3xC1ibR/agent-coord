"""Crash and concurrency coverage for derived search publication."""
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import test_ui

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))
from agent_coord.search_publication import SearchPublicationWorker, install_change_capture, prepare_search_publication
from agent_coord.search_store import SearchStore
from agent_coord.store import CoordinationStore
from agent_coord.transcript_store import TranscriptStore, ensure_schema


class SearchPublicationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = CoordinationStore(Path(directory.name) / "state.sqlite3")
        self.index = SearchStore.for_store(self.store)
        self.addCleanup(self.index.close)
        with self.store._connection() as db:
            ensure_schema(db)
            install_change_capture(db)
        self.store.register(session_id="example", client="codex", cwd=directory.name, name="Initial title")
        self.worker = SearchPublicationWorker(self.store, self.index)

    def test_rollback_rename_and_restart(self):
        self.worker.process_batch()
        with self.assertRaises(RuntimeError), self.store._connection() as db:
            db.execute("UPDATE work_threads SET title='rolled back' WHERE thread_id='example'")
            raise RuntimeError
        self.store.threads.update("example", title="Revised title", domain="search")
        resumed = SearchPublicationWorker(self.store, self.index)
        resumed.process_batch()
        with self.index.connection() as db:
            row = db.execute("SELECT title,domain FROM search_threads WHERE thread_id='example'").fetchone()
            self.assertEqual(tuple(row), ("revised title", "search"))
        with patch.object(self.index, "enqueue_metadata", side_effect=AssertionError("duplicate delivery")):
            replay = SearchPublicationWorker(self.store, self.index)
            replay.process_batch()
            self.assertFalse(replay.retry)

    def test_faulted_history_replays_after_restart_while_metadata_advances(self):
        turn = {"id": "turn-1", "status": "completed", "items": [
            {"id": "answer", "type": "agentMessage", "text": "indexed prose"}]}
        TranscriptStore(self.store).save("example", {"id": "example", "turns": [turn]}, activate=True)
        with patch.object(self.index, "enqueue_history", side_effect=OSError("simulated I/O failure")), self.assertLogs(level="ERROR"):
            self.worker.process_batch()
        self.assertIn(("example", "history"), self.worker.retry)
        self.store.threads.update("example", title="Title after failure")
        self.worker.process_batch()
        resumed = SearchPublicationWorker(self.store, self.index)
        resumed.process_batch()
        with self.index.connection() as db:
            self.assertEqual(db.execute("SELECT title FROM search_threads").fetchone()[0], "title after failure")
            self.assertEqual(db.execute("SELECT excerpt FROM search_turns").fetchone()[0], "indexed prose")
        self.assertFalse(resumed.retry)

    def test_rapid_revisions_coalesce_without_affecting_unrelated_updates(self):
        for i in range(4):
            self.store.threads.update("example", title=f"Revision {i}")
        with self.store.reading(), self.store._connection() as db:
            changes = [tuple(row) for row in db.execute("SELECT sequence,kind FROM search_publications")]
        self.store.threads.update("example", pinned=True)
        with self.store.reading(), self.store._connection() as db:
            self.assertEqual(changes, [tuple(row) for row in db.execute("SELECT sequence,kind FROM search_publications")])
        self.assertEqual(len(changes), 1)
        self.assertGreater(changes[0][0], 1)
        self.worker.process_batch()
        with self.index.connection() as db:
            self.assertEqual(db.execute("SELECT title FROM search_threads").fetchone()[0], "revision 3")


    def test_activation_replays_changes_made_after_verified_baseline(self):
        stopped = threading.Event()
        with patch.object(self.index, "matches_record", wraps=self.index.matches_record) as verify:
            self.assertTrue(prepare_search_publication(self.store, stopped=stopped, owner_guard=lambda: True))
            self.assertEqual(verify.call_count, 1)
        self.assertTrue(self.index.active)
        with self.index.connection() as db:
            self.assertEqual(db.execute("SELECT status FROM search_threads WHERE thread_id='example'").fetchone()[0],
                             "unavailable")
        self.store.threads.update("example", title="After activation")
        self.worker.process_batch()
        with self.index.connection() as db:
            self.assertEqual(db.execute("SELECT title FROM search_threads").fetchone()[0], "after activation")

    def test_interrupted_baseline_reuses_verified_threads_and_rechecks_changed_versions(self):
        self.store.register(session_id="second", client="codex", cwd="/", name="Second")
        stopped = threading.Event()
        check = self.index.matches_record
        def stop_after_one(db, record):
            stopped.set()
            return check(db, record)
        with patch.object(self.index, "matches_record", side_effect=stop_after_one):
            self.assertFalse(prepare_search_publication(self.store, stopped=stopped, owner_guard=lambda: True))
        self.assertFalse(self.index.active)
        with self.index.connection() as db:
            done = db.execute("SELECT thread_id FROM search_baseline_receipts").fetchone()[0]
        stopped.clear()
        self.assertFalse(prepare_search_publication(self.store, stopped=stopped, owner_guard=lambda: False))
        def change_during_verify(db, record):
            self.assertNotEqual(record["thread_id"], done)
            self.store.threads.update(record["thread_id"], title="Changed during verification")
            return check(db, record)
        with patch.object(self.index, "matches_record", side_effect=change_during_verify):
            self.assertFalse(prepare_search_publication(self.store, stopped=stopped, owner_guard=lambda: True))
        self.assertFalse(self.index.active)
        with patch.object(self.index, "matches_record", wraps=check) as verify:
            self.assertTrue(prepare_search_publication(self.store, stopped=stopped, owner_guard=lambda: True))
            self.assertEqual(verify.call_count, 1)
        with self.index.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM search_threads WHERE title='changed during verification'").fetchone()[0], 1)


class SearchRuntimeLifecycleTests(unittest.TestCase):
    setUp = test_ui.BrowserHTTPTests.setUp
    stop_server = test_ui.BrowserHTTPTests.stop_server
    request = test_ui.BrowserHTTPTests.request

    def test_committed_server_attaches_worker_after_verifying_a_new_index(self):
        from agent_coord import desktop_runtime

        self.assertIsNone(self.server.search_publications)
        path = str(self.store.database_path)
        desktop_runtime._OWNED_DATABASES.add(path)
        self.addCleanup(desktop_runtime._OWNED_DATABASES.discard, path)
        self.store.register(session_id="baseline", client="codex", cwd=str(self.root), name="Before activation")
        self.server.start_storage_maintenance(lambda: True)
        deadline = time.monotonic() + 10
        while self.server.search_publications is None and time.monotonic() < deadline:
            time.sleep(.02)
        worker = self.server.search_publications
        self.assertIsNotNone(worker)
        self.assertTrue(worker.index.active)
        self.store.threads.update("baseline", title="After verification")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with worker.index.connection(readonly=True) as db:
                row = db.execute("SELECT title FROM search_threads WHERE thread_id='baseline'").fetchone()
            if row is not None and row[0] == "after verification":
                break
            time.sleep(.02)
        else:
            self.fail("The committed server did not publish the updated thread")
