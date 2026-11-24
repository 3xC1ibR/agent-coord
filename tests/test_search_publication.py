"""Crash and concurrency coverage for derived search publication."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))
from agent_coord.search_publication import SearchPublicationWorker, install_change_capture
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
