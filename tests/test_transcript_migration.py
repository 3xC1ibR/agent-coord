import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from contextlib import contextmanager
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))
from agent_coord.desktop_runtime import AdmissionGate, _OWNED_DATABASES
from agent_coord.history_persistence import HistoryPersistence, ensure_schema
from agent_coord.store import CoordinationStore
from agent_coord.transcript_migration import MigrationSuspended, TranscriptMigrationWorker


class TranscriptMigrationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = CoordinationStore(Path(directory.name) / "state.sqlite3")
        with self.store._connection() as db:
            db.execute("CREATE TABLE browser_history(thread_id TEXT PRIMARY KEY,history_json TEXT NOT NULL)")
            ensure_schema(db)
        self.history = {"id": "thread", "turns": [{"id": "turn", "items": [{"type": "agentMessage", "text": "saved"}]}]}
        HistoryPersistence(self.store).save("thread", self.history)
        self.sessions = SimpleNamespace(store=self.store, admission=AdmissionGate())
        self.owner = True
        self.ready = False
        self.captured = False
        _OWNED_DATABASES.add(str(self.store.database_path))
        self.addCleanup(_OWNED_DATABASES.discard, str(self.store.database_path))
        def capture(db):
            self.captured = True
        def search():
            self.assertTrue(self.captured)
            self.assertEqual(self.worker.transcripts.read_full("thread", require_active=False), self.history)
            return self.ready
        self.worker = TranscriptMigrationWorker(self.sessions, owner_guard=lambda: self.owner,
            install_capture=capture, search_handshake=search)

    def test_trial_owner_and_paused_admission_cannot_publish(self):
        self.owner = False
        with self.assertRaises(MigrationSuspended):
            self.worker.run_once()
        self.owner = True
        self.sessions.admission.pause()
        with self.assertRaises(MigrationSuspended):
            self.worker.run_once()
        with self.store.reading(), self.store._connection() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='transcript_generations'").fetchone())
        self.assertFalse(self.captured)

    def test_search_handshake_precedes_activation_and_receipts_resume(self):
        self.assertFalse(self.worker.run_once())
        self.assertIsNone(self.worker.transcripts.read_window("thread"))
        self.ready = True
        with patch("agent_coord.history_persistence.decode_history", side_effect=AssertionError("verified root decoded again")):
            self.assertTrue(self.worker.run_once())
        self.assertEqual(self.worker.transcripts.read_full("thread"), self.history)
        self.assertEqual(self.worker.phase, "active")

    def test_ownership_loss_after_file_preparation_prevents_publication(self):
        original = self.worker.transcripts._sync_directory
        def lose_owner(directory):
            original(directory)
            self.owner = False
        with patch.object(self.worker.transcripts, "_sync_directory", side_effect=lose_owner):
            with self.assertRaises(MigrationSuspended):
                self.worker.run_once()
        with self.store.reading(), self.store._connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM transcript_generations").fetchone()[0], 0)
        self.owner, self.ready = True, True
        self.assertTrue(self.worker.run_once())

    def test_current_source_change_requires_new_verification(self):
        self.ready = True
        self.worker.run_once()
        self.history = {**self.history, "turns": self.history["turns"] + [{"id": "new", "items": []}]}
        with self.store._connection() as db:
            db.execute("UPDATE browser_history SET history_json=? WHERE thread_id='thread'", (json.dumps(self.history),))
        self.assertIsNone(self.worker.transcripts.read_window("thread"))
        self.assertTrue(self.worker.run_once())
        self.assertEqual(self.worker.transcripts.read_full("thread"), self.history)

    def test_failed_source_does_not_starve_other_threads(self):
        HistoryPersistence(self.store).save("another", {"id": "another", "turns": []})
        self.worker.batch_size = 1
        original = self.worker.transcripts.migrate
        def corrupt_source(**kwargs):
            if kwargs.get("thread_ids") == ["another"]:
                raise ValueError("source integrity failure")
            return original(**kwargs)
        with patch.object(self.worker.transcripts, "migrate", side_effect=corrupt_source):
            self.assertFalse(self.worker.run_once())
            self.assertFalse(self.worker.run_once())
        self.assertEqual(self.worker.transcripts.read_full("thread", require_active=False), self.history)

    def test_cutover_requires_exclusive_fence_current_roots_and_search(self):
        self.ready = True
        self.worker.run_once()
        entered = []
        @contextmanager
        def fence():
            entered.append(True)
            yield
        with self.assertRaisesRegex(ValueError, "Search publication"):
            self.worker.prepare_cutover(fenced_state=fence, search_ready=lambda: False)
        result = self.worker.prepare_cutover(fenced_state=fence, search_ready=lambda: True)
        self.assertEqual(result["threads"], 1)
        self.assertTrue(result["rollback_uses_current_database"])
        self.assertEqual(len(entered), 2)
        self.worker.close()
        with self.assertRaises(MigrationSuspended):
            self.worker.prepare_cutover(fenced_state=fence, search_ready=lambda: True)


if __name__ == "__main__":
    unittest.main()
