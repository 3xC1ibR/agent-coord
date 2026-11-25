from __future__ import annotations

import copy
import hashlib
import json
import random
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))

from agent_coord import history_persistence as history_storage
from agent_coord.history_persistence import (
    CHUNK_BYTES,
    MARKER,
    HistoryPersistence,
    HistoryUnavailableError,
    SavedHistoryWindows,
    decode_history,
    ensure_schema,
    prepare_history,
    publish_history,
    read_history,
    save_history,
    saved_turn_status,
)
from agent_coord.history_window import RECENT_TURNS, recent_view
from agent_coord.store import CoordinationStore


def large_history(identity="conversation", size=512 * 1024):
    content = random.Random(71).randbytes(size).hex()
    return {
        "id": identity,
        "status": {"type": "idle"},
        "extra": {"unicode": "λ雪"},
        "turns": [
            {
                "id": "old",
                "status": "completed",
                "items": [
                    {"id": "command", "type": "commandExecution", "aggregatedOutput": content},
                    {"id": "answer", "type": "agentMessage", "text": "Saved answer"},
                ],
            },
            {
                "id": "latest",
                "status": "inProgress",
                "items": [
                    {"id": "request", "type": "userMessage", "content": [{"type": "text", "text": "Continue"}]},
                ],
            },
        ],
    }


def _store_legacy_chunked(db: sqlite3.Connection, thread_id: str, history: dict) -> str:
    """Write a legacy v1 chunked manifest into browser_history + browser_history_chunks for fallback tests."""
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS browser_history (thread_id TEXT PRIMARY KEY, history_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS browser_history_chunks (digest TEXT PRIMARY KEY, payload BLOB NOT NULL);
        """
    )

    def put_doc(doc) -> list[str]:
        raw = json.dumps(doc, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
        refs = []
        for offset in range(0, max(1, len(raw)), CHUNK_BYTES):
            piece = raw[offset : offset + CHUNK_BYTES]
            digest = hashlib.sha256(piece).hexdigest()
            db.execute("INSERT OR IGNORE INTO browser_history_chunks VALUES (?, ?)", (digest, piece))
            refs.append(digest)
        return refs

    turn_refs = []
    for turn in history.get("turns", []):
        item_refs = [put_doc(item) for item in turn.get("items", [])]
        t_meta = {k: v for k, v in turn.items() if k != "items"}
        turn_refs.append(put_doc({"metadata": t_meta, "items": item_refs}))
    root_meta = {k: v for k, v in history.items() if k != "turns"}
    root_ref = put_doc({"metadata": root_meta, "turns": turn_refs})
    manifest = json.dumps({MARKER: 1, "layout": "turns", "root": root_ref})
    db.execute("INSERT OR REPLACE INTO browser_history VALUES (?, ?)", (thread_id, manifest))
    return manifest


class HistoryPersistenceFacadeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.store = CoordinationStore(self.directory / "state.sqlite3")
        self.addCleanup(self.store.close_idle_connections)
        self.addCleanup(self.store.search_store.close)
        self.addCleanup(self.store.transcripts.close)
        with self.store._connection() as db:
            ensure_schema(db)
        self.persistence = HistoryPersistence(self.store)

    def test_save_and_read_history_roundtrip_via_transcript_store(self):
        small = {"id": "conversation", "turns": []}
        self.persistence.save("conversation", small)
        self.assertEqual(read_history(self.store, "conversation"), small)

        history = large_history()
        save_history(self.store, "conversation", history)
        self.assertEqual(read_history(self.store, "conversation"), history)
        self.assertEqual(saved_turn_status(self.store, "conversation", "old"), "completed")
        self.assertEqual(saved_turn_status(self.store, "conversation", "latest"), "inProgress")

    def test_incremental_turn_append_writes_only_changed_turns(self):
        history = large_history(size=256 * 1024)
        first = self.persistence.save("conversation", history)
        self.assertGreater(first["published_bytes"], 256 * 1024)
        repeated = self.persistence.save("conversation", copy.deepcopy(history))
        self.assertEqual(repeated["unchanged_snapshots"], 1)

        history["turns"][-1]["items"].append({"id": "next", "type": "agentMessage", "text": "A new answer"})
        history["turns"][-1]["status"] = "completed"
        changed = self.persistence.save("conversation", history)
        self.assertLess(changed["published_bytes"], 4096)
        self.assertEqual(read_history(self.store, "conversation"), history)

    def test_publish_history_commits_within_caller_transaction(self):
        history = large_history(size=64 * 1024)
        prepared = prepare_history(history)
        with self.store._connection() as db:
            self.assertTrue(publish_history(db, "conversation", prepared))
        self.assertEqual(read_history(self.store, "conversation"), history)

    def test_derived_callback_failure_does_not_reject_committed_history(self):
        def failed(identity, prepared):
            raise RuntimeError("projection unavailable")

        persistence = HistoryPersistence(self.store, on_saved=failed)
        history = large_history(size=32 * 1024)
        with self.assertLogs("agent_coord.history_persistence", level="ERROR"):
            persistence.save("conversation", history)
        self.assertEqual(read_history(self.store, "conversation"), history)
        self.assertEqual(persistence.status()["failures"], 0)
        self.assertEqual(persistence.status()["search_seed_failures"], 1)

    def test_legacy_inline_and_chunked_fallback_when_transcript_store_has_no_rows(self):
        inline = {"id": "legacy-inline", "turns": [{"id": "t1", "status": "completed", "items": [{"id": "a", "type": "agentMessage", "text": "Hello"}]}]}
        chunked = large_history("legacy-chunked", size=3 * CHUNK_BYTES)
        with self.store._connection() as db:
            db.execute("CREATE TABLE IF NOT EXISTS browser_history (thread_id TEXT PRIMARY KEY, history_json TEXT NOT NULL)")
            db.execute("INSERT INTO browser_history VALUES (?, ?)", ("legacy-inline", json.dumps(inline)))
            manifest = _store_legacy_chunked(db, "legacy-chunked", chunked)

        self.assertEqual(read_history(self.store, "legacy-inline"), inline)
        self.assertEqual(read_history(self.store, "legacy-chunked"), chunked)
        self.assertEqual(decode_history(manifest, database_path=self.store.database_path), chunked)
        self.assertEqual(saved_turn_status(self.store, "legacy-chunked", "old"), "completed")

        windows = SavedHistoryWindows()
        window = read_history(self.store, "legacy-chunked", windows=windows)
        self.assertEqual(window, recent_view(chunked))

        # Corrupting a chunk raises HistoryUnavailableError explicitly.
        root_digest = json.loads(manifest)["root"][0]
        with self.store._connection() as db:
            db.execute("UPDATE browser_history_chunks SET payload = ? WHERE digest = ?", (b"corrupt", root_digest))
        with self.assertRaisesRegex(HistoryUnavailableError, "integrity"):
            decode_history(manifest, database_path=self.store.database_path)


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

    def test_all_provider_readers_and_snapshot_recovery_read_transcript_store(self):
        history = large_history(self.identity, size=128 * 1024)
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

    def test_activity_and_routing_read_complete_history(self):
        from agent_coord.activity_api import read_activity_history
        from agent_coord.dispatch_snapshot import read_snapshot

        history = large_history(self.identity, size=64 * 1024)
        history["turns"][-1]["status"] = "completed"
        self.sessions._remember_thread(history)
        self.assertTrue(self.sessions.persistence.flush(timeout=3))
        self.assertTrue(self.store.search_store.flush())

        activity = read_activity_history(self.store, self.identity)
        self.assertIn("Saved answer", [message["text"] for message in activity["messages"]])

        snapshot = read_snapshot(self.store.database_path)
        conversation = next(row for row in snapshot["threads"] if row["thread_id"] == self.identity)
        self.assertIn("Saved answer", [message["text"] for message in conversation["messages"]])

        history["turns"][0]["items"][1]["text"] = "Revised answer"
        self.sessions._remember_thread(history)
        self.assertTrue(self.sessions.persistence.flush(timeout=3))
        self.assertTrue(self.store.search_store.flush())
        snapshot = read_snapshot(self.store.database_path)
        conversation = next(row for row in snapshot["threads"] if row["thread_id"] == self.identity)
        self.assertIn("Revised answer", [message["text"] for message in conversation["messages"]])
        self.assertNotIn("Saved answer", [message["text"] for message in conversation["messages"]])

    def test_terminal_save_retains_final_item_and_does_not_clear_newer_dirty_revision(self):
        history = large_history(self.identity, size=64 * 1024)
        self.sessions._remember_thread(history)
        self.assertTrue(self.sessions.persistence.flush(timeout=3))
        saved = self.sessions.history_persistence.save_steps
        changed = []

        def during_save(identity, snapshot):
            result = yield from saved(identity, snapshot)
            if not changed:
                changed.append(True)
                with self.sessions.lock:
                    self.sessions.histories[identity]["turns"][-1]["items"].append(
                        {"id": "final", "type": "agentMessage", "text": "Final answer"}
                    )
                    self.sessions._save_history(identity)
            return result

        with patch.object(self.sessions.history_persistence, "save_steps", side_effect=during_save):
            with self.sessions.lock:
                self.sessions._save_history(self.identity)
            self.assertTrue(self.sessions.persistence.flush(timeout=3))
            self.assertTrue(self.sessions.persistence.flush(timeout=3))
        self.assertNotIn(self.identity, self.sessions.dirty_histories)
        self.assertEqual(read_history(self.store, self.identity)["turns"][-1]["items"][-1]["text"], "Final answer")


if __name__ == "__main__":
    unittest.main()
