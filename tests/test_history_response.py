import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))
from agent_coord.codex_app_server import BrowserSessions
from agent_coord.history_persistence import HistoryPersistence
from agent_coord.history_response import read_snapshot_json
from agent_coord.store import CoordinationError, CoordinationStore
from agent_coord.transcript_store import TranscriptStore
from test_codex_app_server import FakeCodex


class HistoryResponseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = CoordinationStore(self.root / "state.sqlite3")
        self.sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(self.sessions.close)
        self.identity = self.sessions.create({"name": "Saved history"})["session"]["thread_id"]
        self.sessions.persistence.flush(timeout=3)
        self.history = {"id": self.identity, "turns": [
            {"id": str(i), "status": "completed", "items": [
                {"id": "answer", "type": "agentMessage", "text": "雪" * 2000},
                {"id": "tool", "type": "commandExecution", "aggregatedOutput": "x" * 16000}]}
            for i in range(43)]}
        HistoryPersistence(self.store).save(self.identity, self.history)
        TranscriptStore(self.store).migrate(activate=True)
        self.sessions.histories.clear()

    def normal(self):
        return self.sessions.read(self.identity, recent=True, snapshot=True)

    def serialized(self, **kwargs):
        return json.loads(read_snapshot_json(self.sessions, self.identity, recent=True, **kwargs))

    def test_normal_fields_and_receipt_identities_without_item_decoding(self):
        expected = self.normal()
        loads = json.loads
        def bounded(value, *args, **kwargs):
            if '"items"' in str(value):
                self.fail("Serialized opening decoded transcript items")
            return loads(value, *args, **kwargs)
        with patch("agent_coord.transcript_store.json.loads", side_effect=bounded), \
                patch.object(self.sessions.submissions, "accepted", wraps=self.sessions.submissions.accepted) as accepted:
            body = read_snapshot_json(self.sessions, self.identity, recent=True)
        self.assertEqual(loads(body), expected)
        self.assertEqual([turn["id"] for turn in accepted.call_args.args[1]["turns"]],
                         [str(i) for i in range(3, 43)])

    def test_complete_and_resident_histories_keep_full_items(self):
        self.assertEqual(json.loads(read_snapshot_json(self.sessions, self.identity))["thread"], self.history)
        resident = copy.deepcopy(self.history)
        resident["turns"][-1]["items"][0]["text"] = "new event"
        self.sessions.histories[self.identity] = resident
        self.assertEqual(self.serialized(), self.normal())
        self.assertEqual(self.serialized()["thread"]["turns"][-1]["items"][0]["text"], "new event")

    def test_previous_build_write_invalidates_index(self):
        replacement = {"id": self.identity, "turns": []}
        with self.store._connection() as db:
            db.execute("UPDATE browser_history SET history_json=? WHERE thread_id=?", (json.dumps(replacement), self.identity))
        self.assertEqual(self.serialized(), self.normal())
        self.assertEqual(self.serialized()["thread"]["turns"], [])

    def test_review_projection_keeps_items(self):
        def public(identity, history):
            result = copy.deepcopy(history)
            result["turns"][-1]["items"][0]["text"] = "public review"
            return result
        with patch.object(self.sessions.reviews, "list", return_value=[{"id": "review"}]), \
                patch.object(self.sessions.reviews, "public_history", side_effect=public):
            self.assertEqual(self.serialized(), self.normal())

    def test_review_appearing_after_initial_check_rebuilds_full_history(self):
        checks = 0
        def reviews(identity):
            nonlocal checks
            checks += 1
            return [] if checks == 1 else [{"id": "review"}]
        def public(identity, history):
            result = copy.deepcopy(history)
            if "items" in result["turns"][-1]:
                result["turns"][-1]["items"][0]["text"] = "public review"
            return result
        with patch.object(self.sessions.reviews, "list", side_effect=reviews), \
                patch.object(self.sessions.reviews, "public_history", side_effect=public):
            self.assertEqual(self.serialized()["thread"]["turns"][-1]["items"][0]["text"], "public review")

    def test_external_history_public_fields_and_workspace(self):
        with self.store._connection() as db:
            db.execute("DELETE FROM browser_sessions WHERE thread_id=?", (self.identity,))
        self.assertEqual(self.serialized(), self.normal())
        self.assertTrue(self.serialized()["thread"]["historyPagination"])
        self.sessions.cwd = str(self.root / "other")
        with self.assertRaises(CoordinationError):
            self.serialized()

    def test_browser_workspace_and_controls(self):
        self.assertIn("close_when_done", self.serialized(controls=True)["work_thread"])
        with self.store._connection() as db:
            db.execute("UPDATE browser_sessions SET cwd=? WHERE thread_id=?", (str(self.root.parent / "other"), self.identity))
        with self.assertRaises(CoordinationError):
            self.serialized()

    def test_repeated_publications_do_not_readmit_schema_ddl(self):
        from agent_coord.transcript_store import ensure_schema
        transcripts = TranscriptStore(self.store)
        transcripts.save(self.identity, self.history)
        with patch("agent_coord.transcript_store.ensure_schema", wraps=ensure_schema) as initialize:
            # A fresh TranscriptStore shares the validated schema hint, but
            # schema changes invalidate it even with the same store object.
            TranscriptStore(self.store).save(self.identity, self.history)
            initialize.assert_not_called()
            with self.store._connection() as db:
                db.execute("CREATE TABLE fixture_schema_revision(value)")
            TranscriptStore(self.store).save(self.identity, self.history)
            initialize.assert_called_once()

    def test_runtime_admission_pause_rejects_serialized_opening(self):
        from agent_coord.codex_app_server import BrowserBusyError
        self.sessions.admission.pause()
        with self.assertRaises(BrowserBusyError):
            self.serialized()
        self.sessions.admission.resume()

    def test_cached_timeline_keeps_runtime_fields_current_and_access_checked(self):
        from agent_coord.snapshot_cache import prepare_snapshot_cache
        from agent_coord.message_timeline import message_timeline
        self.store.register(session_id="peer", client="codex", cwd=str(self.root), name="Peer")
        self.store.send_message(sender_session_id="peer", recipient_session_id=self.identity, body="Coordination body")
        prepare_snapshot_cache(self.store)
        self.assertEqual(self.serialized(), self.normal())
        self.sessions.active[self.identity] = "new-live-turn"
        expected = self.normal()
        with patch("agent_coord.message_timeline.message_timeline", wraps=message_timeline) as projected:
            actual = self.serialized()
            projected.assert_not_called()
        self.assertEqual(actual, expected)
        self.assertEqual(actual["activeTurn"], "new-live-turn")
        self.store.send_message(sender_session_id="peer", recipient_session_id=self.identity, body="New coordination body")
        self.assertEqual(len(self.serialized()["coordinationMessages"]), 2)
        self.sessions.cwd = str(self.root / "outside")
        with self.assertRaises(CoordinationError):
            self.serialized()

    def test_reply_lookup_does_not_scan_other_exchanges(self):
        from agent_coord.message_timeline import _TIMELINE_ROWS
        with self.store._connection() as db:
            db.execute("""INSERT INTO messages(sender_session_id,recipient_session_id,body,created_at,thread_id,classification)
                VALUES (?,?,'first',1,'selected','informational')""", (self.identity, self.identity))
            identity = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            db.executemany("""INSERT INTO messages(sender_session_id,recipient_session_id,body,created_at,thread_id,classification)
                VALUES (?,?,'unrelated',1,?,'informational')""",
                [(self.identity, self.identity, "unrelated-" + str(i)) for i in range(4000)])
            # Match an upgraded database: this later-added recipient index is
            # newer than the exchange index, unlike fresh schema creation.
            db.execute("DROP INDEX messages_recipient_sequence_idx")
            db.execute("CREATE INDEX messages_recipient_sequence_idx ON messages(recipient_session_id,id)")
        # Bound VM work, not wall time, so host contention cannot excuse a
        # full scan. The source query exceeds this bound on the same fixture.
        import sqlite3
        db = sqlite3.connect(self.store.database_path)
        try:
            db.set_progress_handler(lambda: 1, 10000)
            row = db.execute(_TIMELINE_ROWS + " WHERE m.id IN (SELECT value FROM json_each(?))",
                             (json.dumps([identity]),)).fetchone()
            self.assertIsNotNone(row)
            self.assertIsNone(row[-1])
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
