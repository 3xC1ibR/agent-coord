import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))
from agent_coord.app_control import AppControl
from agent_coord.message_timeline import message_timeline
from agent_coord.snapshot_cache import prepare_snapshot_cache, read_timeline_json, read_usage
from agent_coord.store import CoordinationError, CoordinationStore


class SnapshotCacheTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.store = CoordinationStore(self.root / "state.sqlite3")
        AppControl(self.store)
        for identity in ("sender", "recipient", "other", "fourth"):
            self.store.register(session_id=identity, client="codex", cwd=str(self.root), name=identity)
        prepare_snapshot_cache(self.store)
        self.message = self.send()

    def send(self, sender="sender", recipient="recipient", **kwargs):
        return self.store.send_message(sender_session_id=sender, recipient_session_id=recipient,
                                       body="Private message", **kwargs)

    def timeline(self, identity="sender", cwd=None):
        return json.loads(read_timeline_json(self.store, identity, cwd))

    def test_same_response_is_serialized_once_and_unrelated_writes_do_not_invalidate(self):
        with patch("agent_coord.message_timeline.message_timeline", wraps=message_timeline) as produce:
            expected = self.timeline()
            self.assertEqual(self.timeline(), expected)
            self.send("other", "fourth")
            self.store.touch("sender")
            self.assertEqual(self.timeline(), expected)
            produce.assert_called_once()
        self.assertEqual(expected, message_timeline(self.store, "sender"))

    def test_independent_writer_invalidates_changed_message_and_reply(self):
        self.timeline()
        other = CoordinationStore(self.store.database_path)
        with other._connection() as db:
            db.execute("UPDATE messages SET body='Changed body' WHERE id=?", (self.message["id"],))
        self.assertEqual(self.timeline()[0]["body"], "Changed body")
        reply = self.send("recipient", "sender", thread_id=self.message["thread_id"])
        self.assertEqual(self.timeline()[0]["reply_id"], reply["id"])

    def test_delivery_wake_and_creation_receipts_invalidate_existing_bodies(self):
        self.timeline()
        with self.store._connection() as db:
            db.execute("INSERT INTO message_wake_attempts(message_id,session_id,attempted_at,outcome) VALUES (?,'recipient',1,'started')", (self.message["id"],))
        self.assertEqual(self.timeline()[0]["wake_outcome"], "started")
        with self.store._connection() as db:
            db.execute("INSERT INTO message_delivery_attempts(message_id,turn_id,attempted_at,full_body) VALUES (?,'turn',1,0)", (self.message["id"],))
        self.assertEqual(self.timeline()[0]["status"], "Delivery unconfirmed")
        with self.store._connection() as db:
            db.execute("UPDATE message_delivery_attempts SET error='Uncertain'")
        self.assertEqual(self.timeline()[0]["status"], "Delivery uncertain")
        with self.store._connection() as db:
            db.execute("""INSERT INTO app_control_requests(request_id,operation,sender_session_id,cwd,thread_id,payload_json,status,result_json,created_at,updated_at)
                VALUES ('creation','create','sender',?,'recipient','{}','completed',?,1,1)""",
                (str(self.root), json.dumps({"message_id": self.message["id"]})))
        self.assertEqual(self.timeline()[0]["creation_request_id"], "creation")
        with self.store._connection() as db:
            db.execute("DELETE FROM app_control_requests")
            db.execute("DELETE FROM message_delivery_attempts")
            db.execute("DELETE FROM message_wake_attempts")
        row = self.timeline()[0]
        self.assertIsNone(row["creation_request_id"])
        self.assertIsNone(row["delivery_attempted_at"])
        self.assertIsNone(row["wake_outcome"])

    def test_moving_a_message_invalidates_old_and_new_recipient(self):
        self.timeline("recipient")
        self.timeline("other")
        with self.store._connection() as db:
            db.execute("UPDATE messages SET recipient_session_id='other' WHERE id=?", (self.message["id"],))
        self.assertEqual(self.timeline("recipient"), [])
        self.assertEqual(self.timeline("other")[0]["body"], "Private message")

    def test_participant_title_workspace_and_exchange_changes_remain_current(self):
        self.timeline()
        with self.store._connection() as db:
            db.execute("UPDATE work_threads SET title='New participant title' WHERE thread_id='recipient'")
        self.assertEqual(self.timeline()[0]["counterpart"]["title"], "New participant title")
        self.timeline(cwd=str(self.root))
        with self.store._connection() as db:
            db.execute("UPDATE sessions SET cwd=? WHERE session_id='recipient'", (str(self.root.parent),))
        self.assertFalse(self.timeline(cwd=str(self.root))[0]["counterpart"]["available"])
        with self.store._connection() as db:
            db.execute("UPDATE message_threads SET closed_at=1 WHERE thread_id=?", (self.message["thread_id"],))
        self.assertEqual(self.timeline()[0]["status"], "Exchange closed")
        with self.store._connection() as db:
            db.execute("UPDATE sessions SET cwd=? WHERE session_id='sender'", (str(self.root.parent),))
        with self.assertRaises(CoordinationError):
            self.timeline(cwd=str(self.root))

    def test_exchange_rename_invalidates_losing_and_gaining_participants(self):
        other = self.send("other", "fourth")
        with self.store._connection() as db:
            db.execute("UPDATE message_threads SET closed_at=1 WHERE thread_id=?", (self.message["thread_id"],))
            db.execute("DELETE FROM message_threads WHERE thread_id=?", (other["thread_id"],))
        self.assertEqual(self.timeline()[0]["status"], "Exchange closed")
        self.assertNotEqual(self.timeline("other")[0]["status"], "Exchange closed")
        with self.store._connection() as db:
            db.execute("UPDATE message_threads SET thread_id=? WHERE thread_id=?",
                       (other["thread_id"], self.message["thread_id"]))
        self.assertIsNone(self.timeline()[0]["closed_at"])
        self.assertEqual(self.timeline("other")[0]["closed_at"], 1)

    def test_usage_changes_invalidate_both_old_and_new_thread(self):
        with self.store._connection() as db:
            db.execute("INSERT INTO thread_usage_notes VALUES ('sender','codex','native','First note',1)")
        first = read_usage(self.store, "sender")
        read_usage(self.store, "recipient")
        self.assertEqual(first["note"], "First note")
        with self.store._connection() as db:
            db.execute("UPDATE thread_usage_notes SET thread_id='recipient',note='Moved note'")
        self.assertNotEqual(read_usage(self.store, "sender")["note"], "First note")
        self.assertEqual(read_usage(self.store, "recipient")["note"], "Moved note")
        with self.store._connection() as db:
            db.execute("UPDATE sessions SET client='claude' WHERE session_id='recipient'")
        self.assertEqual(read_usage(self.store, "recipient")["provider"], "claude")

    def test_usage_returns_detached_exact_summary_and_skips_other_thread_updates(self):
        with patch.object(self.store.usage, "summary", wraps=self.store.usage.summary) as produce:
            expected = read_usage(self.store, "sender")
            expected["totals"]["injected"] = 42
            with self.store._connection() as db:
                db.execute("INSERT INTO thread_usage_notes VALUES ('other','codex','native','Other note',1)")
            current = read_usage(self.store, "sender")
            self.assertNotIn("injected", current["totals"])
            produce.assert_called_once()
        self.assertEqual(current, self.store.usage.summary("sender"))

    def test_populated_usage_epochs_baseline_context_and_coverage_remain_exact(self):
        usage = self.store.usage
        identity = dict(provider="codex", native_id="first")
        def parity():
            self.assertEqual(read_usage(self.store, "sender"), usage.summary("sender"))
            self.assertEqual(read_usage(self.store, "sender"), usage.summary("sender"))
        parity()
        usage.record_snapshot("sender", **identity, source_key="native", coverage="complete",
                              totals={"input_tokens": 100, "output_tokens": 20, "total_tokens": 120})
        parity()
        usage.record_context("sender", **identity, used=100, limit=1000)
        parity()
        with self.store._connection() as db:
            db.execute("UPDATE work_threads SET forked_from_thread_id='other' WHERE thread_id='sender'")
        parity()
        usage.set_baseline("sender", **identity, totals={"input_tokens": 40, "output_tokens": 10, "total_tokens": 50})
        parity()
        usage.start_epoch("sender", provider="codex", native_id="second", previous_native_id="first")
        parity()
        usage.record_delta("sender", provider="codex", native_id="second", event_id="event",
                           totals={"input_tokens": 5, "output_tokens": 2, "total_tokens": 7}, coverage="complete")
        parity()
        usage.record_unavailable("sender", provider="codex", native_id="second", note="Partial source")
        parity()

    def test_rollback_cannot_publish_a_reusable_uncommitted_cache_entry(self):
        expected = read_usage(self.store, "sender")
        cache = self.store.snapshot_cache
        try:
            with self.store._connection() as db:
                db.execute("INSERT INTO thread_usage_notes VALUES ('sender','codex','native','Rolled back',1)")
                body = cache.read(self.store, "usage", "sender", None, lambda: {"uncommitted": True})
                self.assertEqual(json.loads(body), {"uncommitted": True})
                raise RuntimeError("rollback")
        except RuntimeError:
            pass
        self.assertEqual(read_usage(self.store, "sender"), expected)
        with self.store._connection() as db:
            db.execute("INSERT INTO thread_usage_notes VALUES ('sender','codex','native','Committed',1)")
        self.assertEqual(read_usage(self.store, "sender")["note"], "Committed")

    def test_unknown_schema_change_disables_cache_without_read_side_ddl(self):
        self.timeline()
        with self.store._connection() as db:
            db.execute("DROP TRIGGER snapshot_cache_messages_update_timeline")
            db.execute("UPDATE messages SET body='After schema change' WHERE id=?", (self.message["id"],))
        with self.store.reading():
            self.assertEqual(self.timeline()[0]["body"], "After schema change")
        with self.store.reading(), self.store._connection() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='snapshot_cache_messages_update_timeline'").fetchone())

    def test_database_replacement_never_reuses_the_previous_file_cache(self):
        self.timeline()
        other = CoordinationStore(self.root / "replacement.sqlite3")
        AppControl(other)
        for identity in ("sender", "recipient"):
            other.register(session_id=identity, client="codex", cwd=str(self.root), name=identity)
        other.send_message(sender_session_id="sender", recipient_session_id="recipient", body="Replacement file")
        other.snapshot_cache = self.store.snapshot_cache
        self.assertEqual(json.loads(read_timeline_json(other, "sender", None))[0]["body"], "Replacement file")

    def test_cache_retention_is_bounded_by_bytes(self):
        cache = self.store.snapshot_cache
        cache.MAX_BYTES = 128
        self.timeline()
        self.assertLessEqual(cache.bytes, 128)
        self.assertEqual(len(cache.entries), 0)

    def test_commit_during_projection_is_not_cached_under_an_old_revision(self):
        other = CoordinationStore(self.store.database_path)
        calls = 0
        def produce(*args):
            nonlocal calls
            calls += 1
            result = message_timeline(*args)
            if calls == 1:
                with other._connection() as db:
                    db.execute("UPDATE messages SET body='Concurrent commit' WHERE id=?", (self.message["id"],))
            return result
        with patch("agent_coord.message_timeline.message_timeline", side_effect=produce):
            self.assertEqual(self.timeline()[0]["body"], "Private message")
            self.assertEqual(self.timeline()[0]["body"], "Concurrent commit")
            self.assertEqual(self.timeline()[0]["body"], "Concurrent commit")
            self.assertEqual(calls, 2)


if __name__ == "__main__":
    unittest.main()
