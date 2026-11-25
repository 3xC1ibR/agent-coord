from __future__ import annotations

import copy
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from agent_coord.history_persistence import CHUNK_BYTES, MARKER, read_history
from agent_coord.history_window import PREVIEW_CHARACTERS
from agent_coord.store import CoordinationStore
from agent_coord.thread_search import search_threads
from agent_coord.transcript_store import (
    TranscriptStore,
    migrate_database,
    needs_migration,
    safe_thread_id,
    thread_directory,
)
import migrate_transcript_storage


def _make_turn(seq: int, *, status: str = "completed", tool_chars: int = 2048) -> dict:
    return {
        "id": f"turn-{seq}",
        "status": status,
        "startedAt": 1000.0 + seq * 10,
        "completedAt": None if status == "inProgress" else 1005.0 + seq * 10,
        "items": [
            {
                "id": f"user-{seq}",
                "type": "userMessage",
                "content": [{"type": "text", "text": f"Prompt {seq}: " + "p" * 700}],
            },
            {
                "id": f"cmd-{seq}",
                "type": "commandExecution",
                "command": "pytest -q",
                "status": "completed",
                "aggregatedOutput": f"out-{seq}-" + ("x" * tool_chars),
                "commandActions": [{"type": "exec", "command": "pytest"}] * (14 if tool_chars > 512 else 2),
            },
            {
                "id": f"ans-{seq}",
                "type": "agentMessage",
                "text": f"Answer {seq}: " + "a" * 700,
            },
        ],
    }


def _store_legacy_chunked_manifest(db: sqlite3.Connection, thread_id: str, history: dict) -> None:
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


class TranscriptStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = CoordinationStore(self.root / "state.sqlite3")
        self.addCleanup(self.store.close_idle_connections)
        self.addCleanup(self.store.search_store.close)
        self.addCleanup(self.store.transcripts.close)
        self.transcripts: TranscriptStore = self.store.transcripts

    def test_sharded_directory_layout_and_collision_safe_thread_ids(self):
        standard = "9f8e7d6c-1234-4abc-8def-001122334455"
        directory = thread_directory(self.store.database_path, standard)
        self.assertEqual(directory, self.root / "transcripts" / "9f" / standard)

        unusual_a = "agent/alpha:1"
        unusual_b = "agent_alpha_1"
        self.assertNotEqual(safe_thread_id(unusual_a), safe_thread_id(unusual_b))
        self.assertNotIn("/", safe_thread_id("../escape/attempt"))

    def test_compact_abridgement_blob_refs_and_lossless_full_turn_pread(self):
        turns = [_make_turn(0, tool_chars=3000), _make_turn(1, tool_chars=120)]
        self.transcripts.save_turns("t-1", {"id": "t-1", "turns": turns})

        tdir = self.transcripts.directory_for("t-1")
        self.assertTrue((tdir / "turns.compact.jsonl").exists())
        self.assertTrue((tdir / "turns.full.jsonl").exists())

        window = self.transcripts.read_window("t-1", limit=40)
        self.assertEqual(len(window["turns"]), 2)
        t0_user, t0_cmd, t0_ans = window["turns"][0]["items"]
        self.assertEqual(t0_user, turns[0]["items"][0])
        self.assertEqual(t0_ans, turns[0]["items"][2])
        self.assertTrue(t0_cmd["abridged"])
        self.assertEqual(len(t0_cmd["aggregatedOutput"]), PREVIEW_CHARACTERS)
        self.assertEqual(t0_cmd["blobRef"]["turnId"], "turn-0")
        self.assertGreater(t0_cmd["blobRef"]["length"], len(t0_cmd["aggregatedOutput"]))

        # Short tool output stays unabridged without blobRef.
        self.assertNotIn("abridged", window["turns"][1]["items"][1])

        # Lossless full turn pread recovers exact 3,000-char tool output.
        full_t0 = self.transcripts.read_turn_full("t-1", "turn-0")
        self.assertEqual(full_t0, turns[0])

    def test_o1_incremental_save_never_reserializes_unchanged_prior_turns(self):
        turns = [_make_turn(seq, tool_chars=1024) for seq in range(15)]
        first = self.transcripts.save_turns("inc", {"id": "inc", "turns": turns})
        self.assertEqual(first["written_turns"], 15)

        tdir = self.transcripts.directory_for("inc")
        size_before = (tdir / "turns.full.jsonl").stat().st_size

        turns.append(_make_turn(15, tool_chars=1024))
        dumps_calls = []
        real_dumps = json.dumps

        def counting_dumps(obj, *args, **kwargs):
            if isinstance(obj, dict) and str(obj.get("id", "")).startswith("turn-"):
                dumps_calls.append(obj["id"])
            return real_dumps(obj, *args, **kwargs)

        with patch("agent_coord.transcript_store.json.dumps", side_effect=counting_dumps):
            second = self.transcripts.save_turns("inc", {"id": "inc", "turns": turns})

        self.assertEqual(second["written_turns"], 1)
        # Only turn-15 (full + compact) is serialized when appending turn 15!
        self.assertTrue(all(tid == "turn-15" for tid in dumps_calls), dumps_calls)
        self.assertGreater((tdir / "turns.full.jsonl").stat().st_size, size_before)
        self.assertEqual(self.transcripts.read_full_history("inc")["turns"], turns)

    def test_lock_free_window_upward_pagination_and_pread_coalescing_under_sqlite_lock(self):
        turns = [_make_turn(seq, tool_chars=600) for seq in range(95)]
        self.transcripts.save_turns("paged", {"id": "paged", "turns": turns})

        blocker = sqlite3.connect(self.store.database_path, isolation_level=None)
        self.addCleanup(blocker.close)
        blocker.execute("BEGIN IMMEDIATE")
        try:
            pread_calls = []
            real_pread = os.pread

            def count_pread(fd, length, offset):
                pread_calls.append((length, offset))
                return real_pread(fd, length, offset)

            with patch("agent_coord.transcript_store.os.pread", side_effect=count_pread):
                p1 = self.transcripts.read_window("paged", limit=40)
            self.assertEqual(len(pread_calls), 1, "Window read must execute one coalesced os.pread()")
            self.assertEqual([t["id"] for t in p1["turns"]], [f"turn-{i}" for i in range(55, 95)])
            self.assertEqual(p1["earlierTurns"], 55)
            self.assertEqual(p1["beforeSeq"], 55)

            p2 = self.transcripts.read_window("paged", before_seq=p1["beforeSeq"], limit=40)
            self.assertEqual([t["id"] for t in p2["turns"]], [f"turn-{i}" for i in range(15, 55)])
            self.assertEqual(p2["earlierTurns"], 15)
            self.assertEqual(p2["beforeSeq"], 15)

            p3 = self.transcripts.read_window("paged", before_seq=p2["beforeSeq"], limit=40)
            self.assertEqual([t["id"] for t in p3["turns"]], [f"turn-{i}" for i in range(0, 15)])
            self.assertEqual(p3["earlierTurns"], 0)
            self.assertNotIn("beforeSeq", p3)
        finally:
            blocker.execute("ROLLBACK")

    def test_live_jsonl_append_recovery_and_zero_byte_truncation_on_commit(self):
        tid = "live-thread"
        self.transcripts.append_live(
            tid,
            {
                "sequence": 1,
                "stream": "stdout",
                "data": json.dumps({"method": "turn/started", "params": {"threadId": tid, "turn": {"id": "live-1", "status": "inProgress", "items": []}}}),
            },
        )
        self.transcripts.append_live(
            tid,
            {
                "sequence": 2,
                "stream": "stdout",
                "data": json.dumps({"method": "item/started", "params": {"threadId": tid, "turnId": "live-1", "item": {"id": "msg-1", "type": "agentMessage", "text": "Hello"}}}),
            },
        )
        self.transcripts.append_live(
            tid,
            {
                "sequence": 3,
                "stream": "stdout",
                "data": json.dumps({"method": "item/agentMessage/delta", "params": {"threadId": tid, "turnId": "live-1", "itemId": "msg-1", "delta": " Stage 3"}}),
            },
        )
        live_path = self.transcripts.directory_for(tid) / "live.jsonl"
        self.assertGreater(live_path.stat().st_size, 0)

        recovered = self.transcripts.read_full_history(tid)
        self.assertEqual(recovered["turns"][0]["id"], "live-1")
        self.assertEqual(recovered["turns"][0]["items"][0]["text"], "Hello Stage 3")

        # Finalizing the turn truncates live.jsonl immediately to 0 bytes.
        finalized = copy.deepcopy(recovered)
        finalized["turns"][0]["status"] = "completed"
        self.transcripts.save_turns(tid, finalized)
        self.assertEqual(live_path.stat().st_size, 0)

    def test_migration_compacts_legacy_database_verifies_parity_and_is_idempotent(self):
        db_path = self.root / "legacy.sqlite3"
        legacy_store = CoordinationStore(db_path)
        for idx in range(6):
            tid = f"conv-{idx}"
            legacy_store.register(session_id=tid, client="codex", cwd=str(self.root), name=f"Legacy {idx}")
            legacy_store.threads.capture_request(tid, f"Migrate legacy conversation {idx}")
        legacy_store.close_idle_connections()
        legacy_store.search_store.close()
        legacy_store.transcripts.close()

        expected_histories = {}
        with sqlite3.connect(db_path) as db:
            db.executescript(
                """
                CREATE TABLE browser_history (thread_id TEXT PRIMARY KEY, history_json TEXT NOT NULL);
                CREATE TABLE browser_history_chunks (digest TEXT PRIMARY KEY, payload BLOB NOT NULL);
                CREATE TABLE coordinator_projection_payloads (digest TEXT PRIMARY KEY, compressed BLOB NOT NULL);
                CREATE TABLE conversation_search_messages (thread_id TEXT, item_id TEXT, text TEXT);
                CREATE TABLE conversation_substring_postings (term TEXT, source INTEGER);
                CREATE TRIGGER conversation_search_dirty_insert AFTER INSERT ON browser_history BEGIN SELECT 1; END;
                """
            )
            # Insert 4 MB of legacy chunk/search/projection bloat so VACUUM INTO reclaims physical disk space.
            db.execute("INSERT INTO coordinator_projection_payloads VALUES ('deadbeef', ?)", (os.urandom(2 * 1024 * 1024),))
            db.execute("INSERT INTO conversation_search_messages VALUES ('conv-0', 'm', ?)", ("bloat " * 300000,))

            for idx in range(5):
                tid = f"conv-{idx}"
                hist = {"id": tid, "turns": [_make_turn(0, tool_chars=4000), _make_turn(1, tool_chars=800)]}
                expected_histories[tid] = hist
                if idx % 2 == 0:
                    _store_legacy_chunked_manifest(db, tid, hist)
                else:
                    db.execute("INSERT INTO browser_history VALUES (?, ?)", (tid, json.dumps(hist)))
        with sqlite3.connect(db_path) as db:
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        # Also create one flat pre-Stage-3 <db>.transcripts directory entry.
        flat_dir = Path(f"{db_path}.transcripts") / "conv-5"
        flat_dir.mkdir(parents=True)
        flat_hist = {"id": "conv-5", "turns": [_make_turn(0, tool_chars=1200)]}
        expected_histories["conv-5"] = flat_hist
        (flat_dir / "turns.jsonl").write_text(json.dumps(flat_hist["turns"][0]) + "\n", encoding="utf-8")

        size_before = db_path.stat().st_size
        self.assertGreater(size_before, 3 * 1024 * 1024)
        self.assertTrue(needs_migration(db_path))

        rc = migrate_transcript_storage.main(["--database", str(db_path)])
        self.assertEqual(rc, 0)
        self.assertFalse(needs_migration(db_path))

        size_after = db_path.stat().st_size
        self.assertLess(size_after, size_before // 4)

        with sqlite3.connect(db_path) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            triggers = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
        self.assertNotIn("browser_history", tables)
        self.assertNotIn("browser_history_chunks", tables)
        self.assertNotIn("coordinator_projection_payloads", tables)
        self.assertFalse(any(t.startswith(("conversation_search_", "conversation_substring_")) for t in tables))
        self.assertNotIn("conversation_search_dirty_insert", triggers)
        self.assertIn("thread_turns", tables)

        migrated_store = CoordinationStore(db_path)
        try:
            for tid, expected in expected_histories.items():
                actual = read_history(migrated_store, tid)
                self.assertEqual(len(actual["turns"]), len(expected["turns"]))
                self.assertEqual(actual["turns"], expected["turns"])
            found = search_threads(migrated_store, "Prompt 0", source="messages")
            self.assertEqual(len(found["items"]), 6)
        finally:
            migrated_store.transcripts.close()
            migrated_store.search_store.close()
            migrated_store.close_idle_connections()

        # Second invocation is a clean idempotent no-op.
        second = migrate_database(db_path)
        self.assertEqual(second["threads_migrated"], 0)

    def test_duplicate_turn_ids_preserve_every_turn_across_save_and_migration(self):
        db_path = self.root / "duplicate-turn-ids.sqlite3"
        turns = []
        for idx in range(12):
            turn = _make_turn(idx, tool_chars=600)
            turn["id"] = f"colliding-{idx % 4}"
            turns.append(turn)
        history = {"id": "dup-thread", "turns": turns}

        with sqlite3.connect(db_path) as db:
            db.executescript("""
                CREATE TABLE browser_history (thread_id TEXT PRIMARY KEY, history_json TEXT NOT NULL);
            """)
            db.execute("INSERT INTO browser_history VALUES (?, ?)", ("dup-thread", json.dumps(history)))

        report = migrate_database(db_path)
        self.assertEqual(report["threads_migrated"], 1)
        self.assertEqual(report["turns_migrated"], 12)

        store = CoordinationStore(db_path)
        try:
            full = read_history(store, "dup-thread")
            self.assertEqual(len(full["turns"]), 12)
            self.assertEqual(full["turns"], turns)
            window = store.transcripts.read_window("dup-thread", limit=5)
            self.assertEqual(len(window["turns"]), 5)
            self.assertEqual(window["earlierTurns"], 7)
        finally:
            store.transcripts.close()
            store.search_store.close()
            store.close_idle_connections()


if __name__ == "__main__":
    unittest.main()
