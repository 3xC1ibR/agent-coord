import copy
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))
from agent_coord.store import CoordinationStore
from agent_coord.history_persistence import HistoryPersistence, ensure_schema, read_history
from agent_coord.transcript_store import TranscriptStore


class TranscriptStoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = CoordinationStore(Path(directory.name) / "state.sqlite3")
        with self.store._connection() as db:
            db.execute("CREATE TABLE browser_history(thread_id TEXT PRIMARY KEY, history_json TEXT NOT NULL)")
            ensure_schema(db)
        self.transcripts = TranscriptStore(self.store)
        self.history = {"id": "thread", "metadata": "λ雪", "turns": [
            {"id": str(i), "status": "completed", "items": [
                {"id": "tool", "type": "commandExecution", "output": "雪" * 2000},
                {"id": "answer", "type": "agentMessage", "text": str(i)}]}
            for i in range(83)]}
        self.persistence = HistoryPersistence(self.store)
        self.persistence.save("thread", self.history)

    def test_copy_activation_pagination_and_previous_build(self):
        reports = self.transcripts.migrate()
        self.assertEqual(reports[0]["items"], 166)
        self.assertIsNone(self.transcripts.read_window("thread"))
        self.transcripts.migrate(activate=True)
        self.assertEqual(self.transcripts.read_full("thread"), self.history)
        page = self.transcripts.read_window("thread")
        self.assertEqual(page["beforeSeq"], 43)
        self.assertEqual(len(page["turns"]), 40)
        self.assertTrue(page["turns"][0]["items"][0]["abridged"])
        second = self.transcripts.read_window("thread", before_seq=page["beforeSeq"])
        self.assertEqual(second["beforeSeq"], 3)
        target = self.transcripts.read_window("thread", turn_id="20", limit=5)
        self.assertEqual([t["id"] for t in target["turns"]], [str(i) for i in range(16, 21)])
        self.assertIsNone(self.transcripts.read_window("thread", turn_id="absent"))
        checkpoint = self.transcripts.read_checkpoint("thread")
        self.assertEqual(checkpoint["history"], self.history)
        self.assertEqual(checkpoint["checkpoint"], 0)
        self.assertEqual(self.transcripts.read_full_turn("thread", "0"), self.history["turns"][0])
        legacy = {"id": "thread", "turns": []}
        with self.store._connection() as db:
            db.execute("UPDATE browser_history SET history_json=?", (json.dumps(legacy),))
        self.assertIsNone(self.transcripts.read_window("thread"))
        self.assertEqual(read_history(self.store, "thread"), legacy)

    def test_restart_after_unpublished_files(self):
        with patch.object(self.transcripts, "_sync_directory", side_effect=OSError("simulated crash")):
            with self.assertRaises(OSError):
                self.transcripts.migrate()
        restarted = TranscriptStore(self.store)
        restarted.migrate(activate=True)
        self.assertEqual(restarted.read_full("thread"), self.history)
        first = sorted(restarted.directory("thread").glob("*.jsonl"))
        restarted.migrate(activate=True)
        self.assertEqual(sorted(restarted.directory("thread").glob("*.jsonl")), first)

    def test_normal_save_keeps_both_readers_current(self):
        self.transcripts.migrate(activate=True)
        changed = copy.deepcopy(self.history)
        changed["turns"][-1]["items"][1]["text"] = "replacement"
        self.persistence.save("thread", changed)
        self.assertEqual(self.transcripts.read_full("thread"), changed)
        from agent_coord.history_persistence import decode_history
        with self.store.reading(), self.store._connection() as db:
            raw = db.execute("SELECT history_json FROM browser_history").fetchone()[0]
            self.assertEqual(decode_history(raw, db=db), changed)

    def test_readers_never_observe_partial_generation(self):
        self.transcripts.migrate(activate=True)
        errors = []
        def reader():
            try:
                for _ in range(20):
                    value = self.transcripts.read_full("thread")
                    self.assertIn(len(value["turns"]), (82, 83))
            except Exception as error:
                errors.append(error)
        worker = threading.Thread(target=reader)
        worker.start()
        self.transcripts.save("thread", {**self.history, "turns": self.history["turns"][:-1]}, activate=True)
        worker.join()
        self.assertEqual(errors, [])

    def test_append_proportional_and_private_surrogate_round_trip(self):
        self.transcripts.migrate(activate=True)
        path = self.transcripts.directory("thread") / "turns.full.jsonl"
        initial_size = path.stat().st_size
        turn = {"id": "83", "items": [{"text": "\ud800雪"}]}
        from agent_coord.transcript_store import encode
        self.transcripts.save_turns("thread", {83: turn}, metadata={"id": "thread"}, turn_count=84)
        self.assertEqual(path.stat().st_size - initial_size, len(encode(turn)) + 1)
        self.transcripts.save_turns("thread", {83: turn}, metadata={"id": "thread"}, turn_count=84)
        self.assertEqual(path.stat().st_size - initial_size, len(encode(turn)) + 1)
        self.assertEqual(self.transcripts.read_full_turn("thread", "83"), turn)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        with self.store.reading(), self.store._connection() as db:
            self.assertEqual(db.execute("SELECT item_count FROM transcript_generations").fetchone()[0], 167)

    def test_restart_between_threads_and_corruption_detection(self):
        self.persistence.save("second", {"id": "second", "turns": []})
        original = self.transcripts.save
        calls = []
        def interrupted(*args, **kwargs):
            calls.append(args[0])
            if len(calls) == 2:
                raise OSError("migration interrupted")
            return original(*args, **kwargs)
        with patch.object(self.transcripts, "save", side_effect=interrupted):
            with self.assertRaises(OSError):
                self.transcripts.migrate()
        restarted = TranscriptStore(self.store)
        self.assertEqual(len(restarted.migrate(activate=True)), 2)
        path = restarted.directory("thread") / "turns.full.jsonl"
        with path.open("r+b") as file:
            file.write(b"X")
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            restarted.read_full("thread")

    def test_bounded_legacy_revision_and_reversible_compaction(self):
        self.transcripts.migrate(activate=True)
        with self.store.reading(), self.store._connection() as db:
            self.assertLess(len(db.execute("SELECT source_root FROM transcript_generations").fetchone()[0]), 32)
        self.transcripts.deactivate()
        self.assertIsNone(self.transcripts.read_window("thread"))
        self.transcripts.migrate(activate=True)
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        from migrate_transcript_storage import compact, owned_copy
        compacted = Path(compact(self.store.database_path))
        compact_store = CoordinationStore(compacted)
        self.assertEqual(read_history(compact_store, "thread"), self.history)
        updated = {**self.history, "turns": self.history["turns"] + [{"id": "after-cutover", "items": []}]}
        HistoryPersistence(compact_store).save("thread", updated)
        self.assertEqual(read_history(compact_store, "thread"), updated)
        self.assertEqual(read_history(self.store, "thread"), self.history)
        unrelated = self.store.database_path.parent / "unrelated.sqlite3"
        unrelated.write_bytes(b"unrelated")
        with self.assertRaisesRegex(ValueError, "ownership receipt"):
            owned_copy(self.store.database_path, unrelated)
        self.assertEqual(unrelated.read_bytes(), b"unrelated")

    def test_external_indexed_pages_are_query_only_and_workspace_scoped(self):
        from types import SimpleNamespace
        from agent_coord.codex_app_server import BrowserSessions
        from agent_coord.store import CoordinationError
        self.store.register(session_id="thread", client="codex", cwd=str(self.store.database_path.parent))
        self.transcripts.migrate(activate=True)
        browser = object.__new__(BrowserSessions)
        browser.store = self.store
        browser.cwd = str(self.store.database_path.parent)
        browser.reviews = SimpleNamespace(public_history=lambda identity, history: history)
        self.assertEqual(browser.read_history_window("thread", turn_id="20", limit=5)["turns"][-1]["id"], "20")
        self.assertEqual(browser.read_history_turn("thread", "20"), self.history["turns"][20])
        self.assertIsNone(browser.read_history_window("thread", turn_id="missing"))
        outside = str(self.store.database_path.parent / "elsewhere")
        with self.store._connection() as db:
            db.execute("CREATE TABLE browser_sessions(thread_id TEXT PRIMARY KEY, cwd TEXT)")
            db.execute("INSERT INTO browser_sessions VALUES ('thread', ?)", (outside,))
        with self.assertRaises(CoordinationError):
            browser.read_history_window("thread")
        with self.assertRaises(CoordinationError):
            browser.read_history_turn("thread", "20")
        with self.store._connection() as db:
            db.execute("UPDATE browser_sessions SET cwd=?", (browser.cwd,))
            db.execute("UPDATE sessions SET cwd=? WHERE session_id='thread'", (outside,))
        with self.assertRaises(CoordinationError):
            browser.read_history_window("thread")
        with self.assertRaises(CoordinationError):
            browser.read_history_turn("thread", "20")
        browser.cwd = outside
        with self.assertRaises(CoordinationError):
            browser.read_history_window("thread")
        with self.assertRaises(CoordinationError):
            browser.read_history_turn("thread", "20")

    def test_source_instance_fence_allows_only_expected_reset(self):
        self.transcripts.save("thread", self.history, activate=True, checkpoint=10, source_instance="first")
        self.transcripts.save("thread", self.history, checkpoint=1, source_instance="second", expected_source_instance="first")
        state = self.transcripts.read_checkpoint("thread")
        self.assertEqual((state["source_instance"], state["checkpoint"]), ("second", 1))
        with self.assertRaisesRegex(ValueError, "fence changed"):
            self.transcripts.save("thread", self.history, checkpoint=11, source_instance="first")

    def test_cursor_failure_rolls_back_publication(self):
        self.transcripts.migrate(activate=True)
        def fail(db):
            db.execute("UPDATE browser_history SET history_json='{}'")
            raise RuntimeError("cursor failure")
        with self.assertRaises(RuntimeError):
            self.transcripts.save("thread", {"id": "thread", "turns": []}, checkpoint=5, on_publish=fail)
        self.assertEqual(self.transcripts.read_full("thread"), self.history)
        self.assertEqual(read_history(self.store, "thread"), self.history)

    def test_journal_does_not_write_sqlite(self):
        with patch.object(self.store, "_connection", side_effect=AssertionError("SQLite on delta")):
            self.transcripts.append_live("thread", {"sequence": 1})
            self.assertEqual(list(self.transcripts.replay_live("thread")), [{"sequence": 1}])

    def test_journal_torn_tail_and_monotonic_checkpoint(self):
        self.transcripts.append_live("thread", {"sequence": 1, "text": "λ"})
        with (self.transcripts.directory("thread") / "live.jsonl").open("ab") as file:
            file.write(b'{"torn":')
        self.transcripts.append_live("thread", {"sequence": 2, "text": "complete"})
        self.assertEqual([x["sequence"] for x in self.transcripts.replay_live("thread")], [1, 2])
        self.transcripts.save("thread", self.history, activate=True, checkpoint=2)
        with self.assertRaises(ValueError):
            self.transcripts.save("thread", self.history, checkpoint=1)
        self.assertEqual(list(self.transcripts.replay_live("thread", after_seq=2)), [])


if __name__ == "__main__":
    unittest.main()
