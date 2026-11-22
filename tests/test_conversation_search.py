from __future__ import annotations

import json
import sqlite3
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))
from agent_coord.cli import _parser, run
from agent_coord import conversation_search
from agent_coord.conversation_search import prepare_messages, publish_messages, refresh_messages, sync_messages
from agent_coord.navigation import NavigationStore
from agent_coord.store import CoordinationError, CoordinationStore, _StoreCursor
from agent_coord.thread_search import search_threads


class ConversationSearchTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.store = CoordinationStore(self.root / "state.sqlite3", clock=lambda: 1000)
        with self.store._connection() as db:
            db.execute("CREATE TABLE browser_history (thread_id TEXT PRIMARY KEY, history_json TEXT NOT NULL)")

    def create(self, identity, text=None, *, role="user", attention="now", title="Unrelated title"):
        self.store.register(session_id=identity, client="codex", cwd=str(self.root), name=title)
        self.store.threads.update(identity, attention=attention)
        if text is not None:
            self.save(identity, text, role=role)

    def limit_variables(self, connection, maximum):
        if hasattr(connection, "setlimit"):
            connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, maximum)
            return
        # Python 3.10 cannot change SQLite limits. Enforce the bound for these
        # generated parameter lists so the same batching tests still run.
        execute = _StoreCursor.execute
        def bounded_execute(cursor, sql, parameters=()):
            if len(parameters) > maximum:
                raise sqlite3.OperationalError("too many SQL variables")
            return execute(cursor, sql, parameters)
        patcher = patch.object(_StoreCursor, "execute", bounded_execute)
        patcher.start()
        self.addCleanup(patcher.stop)

    def save(self, identity, text, *, role="user"):
        item = {"id": "message", "timelineAt": 1234, "type": "userMessage", "content": [{"type": "text", "text": text}]}
        if role == "assistant":
            item = {"id": "message", "type": "agentMessage", "text": text}
        history = {"turns": [{"id": "turn", "startedAt": 1200, "items": [item]}]}
        with self.store._connection() as db:
            db.execute("INSERT OR REPLACE INTO browser_history VALUES (?, ?)", (identity, json.dumps(history)))

    def test_jev_in_middle_of_closed_conversation_with_unrelated_metadata(self):
        self.create("overview", "we use jev to sort attention", attention="archived")
        self.create("assistant", "Jev can help", role="assistant")
        self.create("metadata", title="Jev research")
        page = search_threads(self.store, "jev", my_messages=True)
        self.assertEqual([i["thread_id"] for i in page["items"]], ["overview"])
        hit = page["items"][0]
        self.assertFalse(hit["context_match"])
        self.assertEqual(hit["message_count"], 1)
        match = hit["matches"][0]
        self.assertEqual(match["role"], "user")
        self.assertEqual(match["timestamp"], 1234)
        self.assertIn("jev", match["excerpt"])
        self.assertEqual(NavigationStore(self.store).resolve(match["url"])["route"],
                         {"kind": "thread", "id": "overview", "turn": "turn", "item": "message"})
        self.assertEqual({i["thread_id"] for i in search_threads(self.store, "jev")["items"]},
                         {"overview", "assistant", "metadata"})
        self.assertEqual([i["thread_id"] for i in search_threads(self.store, "jev", source="context")["items"]], ["metadata"])
        self.assertEqual(page["coverage"]["saved_histories"], 2)
        self.assertEqual(page["coverage"]["missing_histories"], 1)

    def test_index_updates_and_deletions_across_process_restarts(self):
        self.create("thread", "old Jev discussion")
        self.assertEqual(len(search_threads(self.store, "jev")["items"]), 1)
        self.save("thread", "new conversation about compaction")
        reopened = CoordinationStore(self.store.database_path)
        self.assertEqual(search_threads(reopened, "jev")["items"], [])
        self.assertEqual(len(search_threads(reopened, "compaction")["items"]), 1)
        with self.store._connection() as db:
            db.execute("DELETE FROM browser_history WHERE thread_id='thread'")
        page = search_threads(reopened, "compaction")
        self.assertEqual(page["items"], [])
        self.assertEqual(page["coverage"]["missing_histories"], 1)

    def test_no_tool_output_system_context_or_cross_message_false_matches(self):
        self.create("thread")
        history = {"turns": [{"id": "t", "items": [
            {"id": "tool", "type": "commandExecution", "aggregatedOutput": "Jev tools"},
            {"id": "context", "type": "userMessage", "content": [{"type": "text", "text": "# AGENTS.md\nJev instructions"}]},
            {"id": "wake", "type": "userMessage", "content": [{"type": "text", "text": "Check and handle your unread agent-coord messages."}]},
            {"id": "runtime-wake", "type": "userMessage", "content": [{"type": "text", "text": "[Automatic Agent Coord inbox wake — not a human message]\nCheck and handle your unread agent-coord messages. This prompt was generated by the runtime because agent messages are pending."}]},
            {"id": "user", "type": "userMessage", "content": [{"type": "text", "text": "Jev"}]},
            {"id": "assistant", "type": "agentMessage", "text": "evidence verification"}]}]}
        with self.store._connection() as db:
            db.execute("INSERT INTO browser_history VALUES (?, ?)", ("thread", json.dumps(history)))
        self.assertEqual(search_threads(self.store, "jev tools")["items"], [])
        self.assertEqual(search_threads(self.store, "jev instructions")["items"], [])
        self.assertEqual(search_threads(self.store, "jev verification")["items"], [])
        self.assertEqual(search_threads(self.store, "unread", my_messages=True)["items"], [])
        self.assertEqual(search_threads(self.store, "verification", my_messages=True)["items"], [])
        self.assertEqual(search_threads(self.store, "jev")["items"][0]["message_count"], 1)

    def test_literal_unicode_keywords_and_bounded_excerpts(self):
        self.create("long", "x" * 20000 + " Straße JeV 100%_ " + "y" * 20000)
        hit = search_threads(self.store, "STRASSE jev 100%_")["items"][0]
        self.assertLess(len(hit["matches"][0]["excerpt"]), 325)
        self.assertIn("JeV", hit["matches"][0]["excerpt"])
        self.assertEqual(search_threads(self.store, "' OR 1=1 --")["items"], [])
        self.assertIn("Straße", search_threads(self.store, "strasse")["items"][0]["matches"][0]["excerpt"])

    def test_latest_excerpts_follow_conversation_order_without_timestamps(self):
        self.create("thread")
        history = {"turns": [{"id": "t", "items": [
            {"id": f"reverse-{9-i}", "type": "agentMessage", "text": f"Jev discussion {i}"}
            for i in range(5)]}]}
        with self.store._connection() as db:
            db.execute("INSERT INTO browser_history VALUES (?, ?)", ("thread", json.dumps(history)))
        hit = search_threads(self.store, "jev")["items"][0]
        self.assertEqual(hit["message_count"], 5)
        self.assertEqual([m["excerpt"] for m in hit["matches"]], [f"Jev discussion {i}" for i in (4, 3, 2)])

    def test_pagination_and_role_cursor_guard(self):
        for i in range(23):
            self.create(f"thread-{i:02}", "use Jev", attention="archived" if i % 2 else "now")
        cursor, found = None, []
        while True:
            page = search_threads(self.store, "jev", limit=4, cursor=cursor)
            found += [r["thread_id"] for r in page["items"]]
            cursor = page["next_cursor"]
            if cursor is None:
                break
            with self.assertRaises(CoordinationError):
                search_threads(self.store, "jev", cursor=cursor, my_messages=True)
        self.assertEqual(len(set(found)), 23)
        self.assertEqual(len(found), 23)
        page = search_threads(self.store, "jev", thread_ids=["thread-00"])
        self.assertEqual(page["coverage"]["threads"], 1)
        self.assertEqual(len(page["items"]), 1)
        self.assertEqual(search_threads(self.store, "jev", thread_ids=[])["items"], [])

    def test_phrase_then_proximity_rank_ahead_of_newer_scattered_matches(self):
        self.create("a-phrase", "We fixed the radio alarm yesterday.")
        self.create("b-nearby", "We fixed the alarm for the radio yesterday.")
        self.create("z-scattered", "The radio " + "other work " * 80 + " alarm is ready.")
        self.store.threads.checkpoint("z-scattered", {"phase": "finished", "summary": "Recent work."})
        page = search_threads(self.store, "radio alarm", source="messages", limit=1)
        self.assertEqual(page["items"][0]["thread_id"], "a-phrase")
        page = search_threads(self.store, "radio alarm", source="messages", limit=1, cursor=page["next_cursor"])
        self.assertEqual(page["items"][0]["thread_id"], "b-nearby")
        page = search_threads(self.store, "radio alarm", source="messages", limit=1, cursor=page["next_cursor"])
        self.assertEqual(page["items"][0]["thread_id"], "z-scattered")
        self.assertIsNone(page["next_cursor"])

    def test_best_message_and_passage_are_visible_even_when_older(self):
        self.create("thread")
        history = {"turns": [{"id": "turn", "items": [
            {"id": "best", "type": "agentMessage", "text": "radio " + "unrelated " * 150 + "The radio alarm is scheduled."},
            *[{"id": f"new-{i}", "type": "agentMessage", "text": "radio " + "unrelated " * 80 + "alarm"} for i in range(4)]
        ]}]}
        with self.store._connection() as db:
            db.execute("INSERT INTO browser_history VALUES (?, ?)", ("thread", json.dumps(history)))
        hit = search_threads(self.store, "radio alarm")["items"][0]
        self.assertEqual(hit["message_count"], 5)
        self.assertEqual(hit["matches"][0]["item_id"], "best")
        self.assertIn("The radio alarm is scheduled.", hit["matches"][0]["excerpt"])
        self.assertLessEqual(len(hit["matches"][0]["excerpt"]), 322)

    def test_my_messages_ranks_only_user_evidence(self):
        self.create("strong", "We fixed the radio alarm.")
        self.create("weak", "radio " + "unrelated " * 80 + " alarm", title="Radio alarm")
        self.store.threads.checkpoint("weak", {"phase": "finished", "summary": "Radio alarm"})
        self.assertEqual(search_threads(self.store, "radio alarm", my_messages=True)["items"][0]["thread_id"], "strong")

    def test_ranked_excerpts_preserve_unicode_and_literal_keywords(self):
        self.create("thread", "Straße " + "ß " * 400 + "Straße & a+b works.")
        hit = search_threads(self.store, "STRASSE a+b")["items"][0]
        self.assertIn("Straße & a+b works.", hit["matches"][0]["excerpt"])

    def test_relevance_examples_retrieve_expected_thread_in_first_three(self):
        queries = ["radio alarm", "plugin refresh", "thread search", "native app", "release validation",
                   "database latency", "message queue", "mobile layout", "dark mode", "file preview",
                   "saved filters", "permission settings", "context lifecycle", "project tabs",
                   "usage limits", "browser history"]
        for i, query in enumerate(queries):
            first, second = query.split()
            self.create(f"expected-{i}", "We completed the " + query + " work.", attention="archived")
            self.create(f"noise-{i}", first + " " + "other work " * 60 + second)
        for i, query in enumerate(queries):
            with self.subTest(query=query):
                hits = search_threads(self.store, query)["items"]
                self.assertIn(f"expected-{i}", [hit["thread_id"] for hit in hits[:3]])
                self.assertEqual(hits[0]["thread_id"], f"expected-{i}")

    def test_corrupt_or_partial_history_does_not_hide_other_results(self):
        for identity, value in (("broken", "{oops"), ("partial", '{"historyUnavailable":true,"turns":[]}')):
            self.create(identity)
            with self.store._connection() as db:
                db.execute("INSERT INTO browser_history VALUES (?, ?)", (identity, value))
        self.create("good", "Jev is here")
        page = search_threads(self.store, "jev")
        self.assertEqual(len(page["items"]), 1)
        self.assertEqual(page["coverage"]["partial_histories"], 1)
        self.assertEqual(page["coverage"]["missing_histories"], 1)

    def test_rebuild_uses_few_statements_and_current_indexes_stay_read_only(self):
        self.create("small", "Jev once")
        self.create("large")
        # Later duplicate IDs replace earlier ones; 261 rows cross several insert batches.
        items = [{"id": f"m{i % 230}", "type": "agentMessage", "text": f"Jev {i}"} for i in range(260)]
        items.append({"id": "timed", "type": "userMessage", "timelineAt": 1793247626.5940921,
                      "content": [{"type": "text", "text": "Timed Jev"}]})
        with self.store._connection() as db:
            db.execute("INSERT INTO browser_history VALUES (?, ?)",
                       ("large", json.dumps({"turns": [{"id": "turn", "items": items}]})))
        db = sqlite3.connect(self.store.database_path)
        self.addCleanup(db.close)
        db.row_factory = sqlite3.Row
        statements = []
        db.set_trace_callback(statements.append)
        with db:
            sync_messages(db)
        self.assertLess(len(statements), 25, statements)
        rows = db.execute("""SELECT item_id, text, position, timestamp FROM conversation_search_messages
                             WHERE thread_id = 'large' ORDER BY position""").fetchall()
        self.assertEqual(len(rows), 231)
        self.assertEqual(dict(rows[0]), {"item_id": "m30", "text": "Jev 30", "position": 30, "timestamp": None})
        self.assertEqual(dict(rows[-1]), {"item_id": "timed", "text": "Timed Jev", "position": 260,
                                          "timestamp": 1793247626.5940921})
        self.assertEqual(db.execute("SELECT text FROM conversation_search_messages WHERE item_id = 'm0'").fetchone()[0], "Jev 230")
        statements.clear()
        sync_messages(db)
        self.assertEqual(len(statements), 1)
        self.assertFalse(db.in_transaction)
        hit = next(i for i in search_threads(self.store, "timed jev")["items"] if i["thread_id"] == "large")
        self.assertEqual(hit["matches"][0]["timestamp"], 1793247626.5940921)
        self.assertEqual(hit["message_count"], 1)

    def test_no_provider_invocation_or_thread_state_mutation_and_cli_parity(self):
        self.create("thread", "can we use Jev?")
        before = self.store.threads.get("thread", history=True)
        with patch("subprocess.Popen", side_effect=AssertionError("Search started a provider")):
            cli = run(_parser().parse_args(["--db", str(self.store.database_path), "thread", "search", "jev", "--my-messages"]))
        self.assertEqual(cli, search_threads(self.store, "jev", my_messages=True))
        self.assertEqual(self.store.threads.get("thread", history=True), before)

    def test_search_preparation_does_not_block_an_unrelated_writer(self):
        self.create("thread", "Search preparation")
        entered, release, written = threading.Event(), threading.Event(), threading.Event()
        original = conversation_search.messages

        def prepare(history):
            entered.set()
            if not release.wait(5):
                raise AssertionError("Preparation was not released")
            return original(history)

        def write():
            with self.store._connection() as db:
                db.execute("UPDATE sessions SET name='Independent write' WHERE session_id='thread'")
            written.set()

        with ThreadPoolExecutor(max_workers=2) as workers, patch.object(conversation_search, "messages", prepare):
            search = workers.submit(search_threads, self.store, "preparation")
            try:
                self.assertTrue(entered.wait(5), "Search did not reach history preparation")
                writer = workers.submit(write)
                self.assertTrue(written.wait(2), "History preparation retained writer admission or a SQLite write lock")
                writer.result()
            finally:
                release.set()
            self.assertEqual([item["thread_id"] for item in search.result()["items"]], ["thread"])

    def test_stale_preparation_cannot_publish_or_clear_a_newer_invalidation(self):
        self.create("thread", "Original content")
        refresh_messages(self.store)
        self.save("thread", "Prepared content")
        prepared = prepare_messages(self.store, "thread")
        self.save("thread", "Newer content")
        # An old installed runtime may independently drain its dirty queue.
        with self.store._connection() as db:
            sync_messages(db)
        self.assertFalse(publish_messages(self.store, prepared))
        self.assertEqual(conversation_search.search_metrics(self.store)["publish"]["discarded"], 1)
        with self.store.reading(), self.store._connection() as db:
            revision = db.execute("SELECT revision,indexed_revision FROM conversation_search_revisions").fetchone()
            self.assertNotEqual(revision[0], revision[1])
            self.assertEqual(db.execute("SELECT text FROM conversation_search_message_index").fetchone()[0], "Original content")
            self.assertIsNone(db.execute("SELECT 1 FROM conversation_search_routing_index").fetchone())
        self.assertEqual(search_threads(self.store, "prepared")["items"], [])
        self.assertEqual(search_threads(self.store, "newer")["items"][0]["thread_id"], "thread")

    def test_delete_and_reinsert_cannot_reuse_a_prepared_revision(self):
        self.create("thread", "Repeated content")
        refresh_messages(self.store)
        prepared = prepare_messages(self.store, "thread")
        with self.store._connection() as db:
            db.execute("DELETE FROM browser_history WHERE thread_id='thread'")
        self.save("thread", "Repeated content")
        self.assertFalse(publish_messages(self.store, prepared))
        refresh_messages(self.store)
        with self.store._connection() as db:
            db.execute("DELETE FROM browser_history WHERE thread_id='thread'")
        deletion = prepare_messages(self.store, "thread")
        self.save("thread", "Restored content")
        self.assertFalse(publish_messages(self.store, deletion))
        self.assertEqual(search_threads(self.store, "restored")["items"][0]["thread_id"], "thread")

    def test_large_conversation_stages_bounded_rows_and_bytes_before_atomic_visibility(self):
        self.create("thread", "Original content")
        refresh_messages(self.store)
        # Include one oversized message and a match across the chunk boundary.
        text = "𐐀" * (conversation_search._TEXT_CHARACTERS - 3) + "complete boundary" + "ß" * 180000
        items = [{"id": f"message-{i}", "type": "agentMessage", "text": text if i == 0 else "Replacement " + "x" * 18000}
                 for i in range(140)]
        with self.store._connection() as db:
            db.execute("UPDATE browser_history SET history_json=? WHERE thread_id='thread'",
                       (json.dumps({"turns": [{"id": "turn", "items": items}]}),))
        stage = conversation_search._stage_batch
        counts = []

        def inspect_batch(store, generation, batch):
            size = sum(sum(len(v.encode("utf-8")) if isinstance(v, str) else 8 for v in row) for _, row in batch)
            counts.append((len(batch), size))
            self.assertLessEqual(len(batch), conversation_search._STAGE_ROWS)
            self.assertLessEqual(size, conversation_search._STAGE_BYTES)
            result = stage(store, generation, batch)
            with store.reading(), store._connection() as db:
                self.assertEqual([r[0] for r in db.execute("SELECT text FROM conversation_search_message_index")],
                                 ["Original content"], "A partial generation became visible")
            return result

        with patch.object(conversation_search, "_stage_batch", inspect_batch):
            refresh_messages(self.store)
        self.assertGreater(len(counts), 10)
        metrics = conversation_search.search_metrics(self.store)
        self.assertEqual(metrics["stage"]["max_rows"], max(count[0] for count in counts))
        self.assertEqual(metrics["stage"]["max_bytes"], max(count[1] for count in counts))
        self.assertEqual(metrics["publish"]["published"], 2)
        hit = search_threads(self.store, "complete boundary")["items"][0]
        self.assertEqual(hit["message_count"], 1)
        self.assertIn("complete boundary", hit["matches"][0]["excerpt"])
        with self.store.reading(), self.store._connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM conversation_search_message_index").fetchone()[0], 140)
            self.assertEqual(db.execute("SELECT text FROM conversation_search_message_index WHERE item_id='message-0'").fetchone()[0], text)

    def test_interrupted_stage_is_invisible_and_recovered_after_restart(self):
        self.create("thread", "Original content")
        refresh_messages(self.store)
        self.save("thread", "Replacement content " + "x" * 400000)
        stage = conversation_search._stage_batch
        staged = []

        def interrupt(store, generation, batch):
            result = stage(store, generation, batch)
            staged.append((generation, batch))
            raise RuntimeError("Simulated process interruption")

        with patch.object(conversation_search, "_stage_batch", interrupt), self.assertRaisesRegex(RuntimeError, "interruption"):
            refresh_messages(self.store)
        with self.store._connection() as db:
            self.assertEqual(db.execute("SELECT text FROM conversation_search_message_index").fetchone()[0], "Original content")
            db.execute("UPDATE conversation_search_generations SET expires_at=0 WHERE state='staging'")
        reopened = CoordinationStore(self.store.database_path)
        conversation_search._cleanup_messages(reopened)
        self.assertFalse(stage(reopened, *staged[0]), "An expired, partially collected generation resumed")
        refresh_messages(reopened)
        self.assertEqual(search_threads(reopened, "replacement")["items"][0]["thread_id"], "thread")
        with reopened.reading(), reopened._connection() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM conversation_search_generations WHERE generation=?", (staged[0][0],)).fetchone())

    def test_reader_snapshot_survives_publication_and_old_generation_cleanup(self):
        self.create("thread", "Original content")
        refresh_messages(self.store)
        reader = sqlite3.connect(self.store.database_path)
        self.addCleanup(reader.close)
        reader.execute("BEGIN")
        self.assertEqual(reader.execute("SELECT text FROM conversation_search_message_index").fetchone()[0], "Original content")
        self.save("thread", "Replacement content")
        refresh_messages(self.store)
        self.assertEqual(reader.execute("SELECT text FROM conversation_search_message_index").fetchone()[0], "Original content")
        reader.commit()
        self.assertEqual(reader.execute("SELECT text FROM conversation_search_message_index").fetchone()[0], "Replacement content")

    def test_refresh_does_not_chase_newly_arriving_histories(self):
        self.create("thread", "Initial content")
        prepare = conversation_search.prepare_messages

        def arriving(store, identity):
            prepared = prepare(store, identity)
            self.create("later", "Arrived during preparation")
            return prepared

        with patch.object(conversation_search, "prepare_messages", side_effect=arriving) as preparation:
            refresh_messages(self.store)
        self.assertEqual(preparation.call_count, 1)
        with self.store.reading(), self.store._connection() as db:
            self.assertEqual([r[0] for r in db.execute("SELECT thread_id FROM conversation_search_heads")], ["thread"])
        self.assertEqual(search_threads(self.store, "arrived")["items"][0]["thread_id"], "later")

    def test_legacy_and_generation_projections_can_alternate(self):
        self.create("thread", "Initial content")
        refresh_messages(self.store)
        with self.store._connection() as db:
            sync_messages(db)
            self.assertEqual(db.execute("SELECT text FROM conversation_search_messages").fetchone()[0], "Initial content")
        self.save("thread", "Modern content")
        refresh_messages(self.store)
        with self.store._connection() as db:
            self.assertIsNotNone(db.execute("SELECT 1 FROM conversation_search_dirty").fetchone())
            sync_messages(db)
            self.assertEqual(db.execute("SELECT text FROM conversation_search_messages").fetchone()[0], "Modern content")
        self.save("thread", "Legacy content")
        with self.store._connection() as db:
            sync_messages(db)
        refresh_messages(self.store)
        self.assertEqual(search_threads(self.store, "legacy")["items"][0]["thread_id"], "thread")

    def test_clean_legacy_projection_migrates_without_reparsing_history(self):
        for index in range(4):
            self.create(f"thread-{index}", "Migration needle " + str(index))
        with self.store._connection() as db:
            sync_messages(db)
        with patch.object(conversation_search, "prepare_messages", side_effect=AssertionError("Clean legacy history reparsed")):
            page = search_threads(self.store, "migration needle")
        self.assertEqual(len(page["items"]), 4)
        self.assertEqual(page["coverage"]["saved_histories"], 4)
        with self.store.reading(), self.store._connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM conversation_search_heads").fetchone()[0], 0)
        self.save("thread-1", "Revised needle")
        with patch.object(conversation_search, "prepare_messages", wraps=prepare_messages) as prepared:
            page = search_threads(self.store, "needle")
        self.assertEqual([call.args[1] for call in prepared.call_args_list], ["thread-1"])
        self.assertEqual(len(page["items"]), 4)
        self.assertEqual(search_threads(self.store, "revised")["items"][0]["thread_id"], "thread-1")

    def test_migration_rebuilds_only_dirty_legacy_histories_and_retains_deletions(self):
        for identity in ("clean", "dirty", "deleted"):
            self.create(identity, identity + " needle")
        with self.store._connection() as db:
            sync_messages(db)
            db.execute("DELETE FROM browser_history WHERE thread_id='deleted'")
        self.save("dirty", "Revised needle")
        with patch.object(conversation_search, "prepare_messages", wraps=prepare_messages) as prepared:
            page = search_threads(self.store, "needle")
        self.assertEqual({call.args[1] for call in prepared.call_args_list}, {"dirty", "deleted"})
        self.assertEqual({item["thread_id"] for item in page["items"]}, {"clean", "dirty"})
        with patch.object(conversation_search, "prepare_messages", side_effect=AssertionError("Indexed deletion repeated")):
            self.assertEqual(len(search_threads(self.store, "needle")["items"]), 2)

    def test_generation_view_upgrade_preserves_published_data(self):
        self.create("thread", "Published needle")
        refresh_messages(self.store)
        with self.store._connection() as db:
            db.execute("UPDATE conversation_search_projection_version SET version=1")
            db.execute("DROP VIEW conversation_search_message_index")
            db.execute("CREATE VIEW conversation_search_message_index AS SELECT * FROM conversation_search_messages")
        with patch.object(conversation_search, "prepare_messages", side_effect=AssertionError("Published generation reparsed")):
            self.assertEqual(search_threads(self.store, "needle")["items"][0]["thread_id"], "thread")

    def save_chunked(self, identity, history, *, previous=None, seed=False):
        from agent_coord import history_persistence as storage
        prepared = storage.prepare_history(history, previous=previous)
        with self.store._connection() as db:
            storage.ensure_schema(db)
        storage.stage_history(self.store, prepared)
        if seed:
            for _ in conversation_search.seed_prepared_history_steps(self.store, prepared):
                # Cooperative persistence must be able to yield after every
                # batch without retaining writer admission or a transaction.
                self.assertIsNone(self.store._writer.active_operation)
        with self.store._connection() as db:
            storage.publish_history(db, identity, prepared)
        return prepared

    def test_separate_small_substring_batches_keep_merging_between_calls(self):
        from agent_coord import conversation_substrings as substrings
        if not substrings.ensure_schema(self.store):
            self.skipTest("SQLite trigram indexing is unavailable")
        batches = 120
        for index in range(batches):
            rows = substrings._text_rows(str(index), "Searchable revision " + str(index))
            for _ in substrings._stage_steps(self.store, rows):
                self.assertIsNone(self.store._writer.active_operation)
        metrics = conversation_search.search_metrics(self.store)
        self.assertEqual(metrics["substring_stage"]["count"], batches)
        self.assertEqual(metrics["substring_merge"]["count"], batches)
        self.assertEqual(metrics["substring_merge"]["page_budget"], batches * substrings.MERGE_PAGES)
        with self.store.reading(), self.store._connection() as db:
            segments = db.execute("SELECT COUNT(DISTINCT segid) FROM conversation_substring_terms_idx").fetchone()[0]
        self.assertLess(segments, batches // 4)

    def test_substring_coverage_does_not_clear_backfill_failure(self):
        from agent_coord import conversation_substrings as substrings
        if not substrings.ensure_schema(self.store):
            self.skipTest("SQLite trigram indexing is unavailable")
        with patch.object(substrings, "_legacy_rows", side_effect=RuntimeError("Backfill interrupted")):
            with self.assertRaisesRegex(RuntimeError, "Backfill interrupted"):
                list(substrings.backfill_steps(self.store))
        substrings._coverage(self.store)
        metrics = substrings.metrics(self.store)
        self.assertEqual(metrics["remaining_sources"], 0)
        self.assertEqual(metrics["state"], "failed")
        self.assertEqual(metrics["failures"], 1)
        self.assertFalse(metrics["backfill_running"])

    def test_concurrent_substring_metrics_share_one_coverage_refresh(self):
        from agent_coord import conversation_substrings as substrings
        if not substrings.ensure_schema(self.store):
            self.skipTest("SQLite trigram indexing is unavailable")
        entered, release = threading.Event(), threading.Event()
        coverage = substrings._coverage

        def held_coverage(store):
            entered.set()
            self.assertTrue(release.wait(5))
            coverage(store)

        with patch.object(substrings, "_coverage", side_effect=held_coverage) as refreshed, ThreadPoolExecutor() as executor:
            first = executor.submit(substrings.metrics, self.store)
            try:
                self.assertTrue(entered.wait(5))
                # A slow observation must remain shared even after its normal
                # one-second refresh interval has elapsed.
                later = substrings.time.monotonic() + 2
                with patch.object(substrings.time, "monotonic", return_value=later):
                    substrings.metrics(self.store)
                self.assertEqual(refreshed.call_count, 1)
            finally:
                release.set()
            self.assertEqual(first.result(timeout=5)["remaining_sources"], 0)

    def test_immutable_documents_reuse_large_items_and_only_stage_changed_text(self):
        from agent_coord import history_persistence as storage
        self.create("thread")
        text = "𐐀" * (conversation_search._TEXT_CHARACTERS - 3) + "complete boundary" + "ß" * 100000
        history = {"turns": [{"id": "turn", "items": [
            {"id": "large", "type": "agentMessage", "text": text},
            {"id": "tool", "type": "commandExecution", "output": "excluded output " * 30000},
            {"id": "changing", "type": "agentMessage", "text": "Initial prose"}]}]}
        prepared = self.save_chunked("thread", history)
        refresh_messages(self.store)
        before = conversation_search.search_metrics(self.store)
        history["turns"][0]["items"][-1]["text"] = "Replacement prose"
        changed = self.save_chunked("thread", history, previous=storage.compact_history(prepared))
        with patch.object(storage, "_documents", wraps=storage._documents) as documents:
            refresh_messages(self.store)
        requested = [reference for call in documents.call_args_list for reference in call.args[1]]
        self.assertNotIn(prepared.documents[(0, 0)].references, requested)
        self.assertNotIn(prepared.documents[(0, 1)].references, requested)
        self.assertIn(changed.documents[(0, 2)].references, requested)
        after = conversation_search.search_metrics(self.store)
        self.assertEqual(after["document_read"]["decoded_documents"] - before["document_read"]["decoded_documents"], 1)
        self.assertLess(after["documents"]["bytes"] - before["documents"]["bytes"], 1024)
        self.assertLess(after["stage"]["bytes"] - before["stage"]["bytes"], 100000)
        with self.store.reading(), self.store._connection() as db:
            self.assertEqual(db.execute("SELECT text FROM conversation_search_message_index WHERE item_id='large'").fetchone()[0], text)
        hit = search_threads(self.store, "complete boundary")["items"][0]
        self.assertEqual(hit["message_count"], 1)
        self.assertIn("complete boundary", hit["matches"][0]["excerpt"])
        self.assertEqual(search_threads(self.store, "SSSS")["items"][0]["thread_id"], "thread")
        self.assertEqual(search_threads(self.store, "excluded output")["items"], [])
        self.assertEqual(search_threads(self.store, "replacement prose")["items"][0]["thread_id"], "thread")

    def test_prepared_history_seeding_avoids_item_decode_on_first_search(self):
        from agent_coord import history_persistence as storage
        self.create("thread")
        history = {"turns": [{"items": [
            {"type": "commandExecution", "output": "not prose " * 40000},
            {"type": "agentMessage", "text": "Seeded searchable prose"}]}]}
        prepared = self.save_chunked("thread", history, seed=True)
        with patch.object(storage, "_documents", wraps=storage._documents) as documents:
            hit = search_threads(self.store, "seeded searchable")["items"][0]
        self.assertEqual(hit["thread_id"], "thread")
        requested = [reference for call in documents.call_args_list for reference in call.args[1]]
        for position, document in prepared.documents.items():
            if len(position) == 2:
                self.assertNotIn(document.references, requested)
        self.assertEqual(conversation_search.search_metrics(self.store)["document_read"]["decoded_documents"], 0)

    def test_document_cache_preserves_contextual_ids_timestamps_and_routing_duplicates(self):
        self.create("thread")
        item = {"type": "agentMessage", "text": "Repeated shared document"}
        history = {"turns": [
            {"startedAt": 42, "items": [
                {"id": "duplicate", "type": "agentMessage", "text": "Discarded earlier prose"},
                {"id": "duplicate", "type": "agentMessage", "text": "Retained later prose"}, item,
                {"type": "commandExecution", "output": "z" * 300000}]},
            {"startedAt": 99, "items": [item]}]}
        self.save_chunked("thread", history, seed=True)
        refresh_messages(self.store)
        with self.store.reading(), self.store._connection() as db:
            rows = [tuple(row) for row in db.execute("""SELECT turn_id,item_id,timestamp FROM
                conversation_search_message_index WHERE text='Repeated shared document' ORDER BY position""")]
            evidence = json.loads(db.execute("SELECT evidence_json FROM conversation_search_routing_index").fetchone()[0])
        self.assertEqual(rows, [("search-turn-0", "search-item-2", 42), ("search-turn-1", "search-item-0", 99)])
        self.assertEqual(len(evidence["messages"]), 4)
        self.assertEqual(search_threads(self.store, "discarded earlier")["items"], [])
        self.assertEqual(search_threads(self.store, "retained later")["items"][0]["thread_id"], "thread")

    def test_interrupted_document_seeding_has_no_complete_header_and_resumes(self):
        from agent_coord import history_persistence as storage
        self.create("thread", "Original content")
        refresh_messages(self.store)
        text = "Long replacement " + "ß" * 350000
        prepared = storage.prepare_history({"turns": [{"items": [{"type": "agentMessage", "text": text}]}]})
        with self.store._connection() as db:
            storage.ensure_schema(db)
        storage.stage_history(self.store, prepared)
        steps = conversation_search.seed_prepared_history_steps(self.store, prepared)
        next(steps)  # schema
        next(steps)  # cache lookup
        next(steps)  # first bounded batch, before completion header
        steps.close()
        with self.store.reading(), self.store._connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM conversation_search_documents").fetchone()[0], 0)
            self.assertGreater(db.execute("SELECT COUNT(*) FROM conversation_search_document_text").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT text FROM conversation_search_message_index").fetchone()[0], "Original content")
        reopened = CoordinationStore(self.store.database_path)
        for _ in conversation_search.seed_prepared_history_steps(reopened, prepared):
            pass
        with reopened._connection() as db:
            storage.publish_history(db, "thread", prepared)
        refresh_messages(reopened)
        with reopened.reading(), reopened._connection() as db:
            self.assertEqual(db.execute("SELECT text FROM conversation_search_message_index").fetchone()[0], text)
        metrics = conversation_search.search_metrics(reopened)["documents"]
        self.assertLessEqual(metrics["max_bytes"], conversation_search._STAGE_BYTES)
        self.assertLessEqual(metrics["max_rows"], conversation_search._DOCUMENT_STAGE_ROWS)

    def test_prose_staging_uses_smaller_transactions_without_truncating_text(self):
        text = 'Complete prose ' * 30000
        documents = [conversation_search._document_rows('document', {'id': 'answer', 'type': 'agentMessage', 'text': text})]
        conversation_search._ensure_document_cache(self.store)
        for _ in conversation_search._stage_document_steps(self.store, documents):
            self.assertIsNone(self.store._writer.active_operation)
        metrics = conversation_search.search_metrics(self.store)['documents']
        self.assertLessEqual(metrics['max_bytes'], conversation_search._DOCUMENT_STAGE_BYTES)
        with self.store.reading(), self.store._connection() as db:
            restored = ''.join(row[0] for row in db.execute(
                'SELECT text FROM conversation_search_document_text WHERE document=? ORDER BY part', ('document',)))
        self.assertEqual(restored, text)

    def test_negative_document_cache_uses_three_commits_for_1025_items_with_999_variable_limit(self):
        from agent_coord import history_persistence as storage
        history = {"turns": [{"items": [{"id": str(i), "type": "commandExecution", "output": "Tool result"}
                                        for i in range(1025)]}]}
        prepared = storage.prepare_history(history)
        with self.store._connection() as db:
            self.limit_variables(db, 999)
        for _ in conversation_search.seed_prepared_history_steps(self.store, prepared):
            self.assertIsNone(self.store._writer.active_operation)
        metrics = conversation_search.search_metrics(self.store)["documents"]
        self.assertEqual(metrics["count"], 3)
        self.assertEqual(metrics["rows"], 1025)
        self.assertEqual(metrics["max_rows"], 512)
        self.assertLessEqual(metrics["max_bytes"], conversation_search._STAGE_BYTES)
        with self.store.reading(), self.store._connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM conversation_search_documents WHERE role IS NULL").fetchone()[0], 1025)
        # Retrying an already seeded source performs no additional commits.
        for _ in conversation_search.seed_prepared_history_steps(self.store, prepared):
            pass
        self.assertEqual(conversation_search.search_metrics(self.store)["documents"]["count"], 3)

    def test_document_cache_failure_rolls_back_all_statements_in_a_large_batch(self):
        from agent_coord import history_persistence as storage
        history = {"turns": [{"items": [{"id": str(i), "type": "commandExecution", "output": "Tool result"}
                                        for i in range(1025)]}]}
        prepared = storage.prepare_history(history)
        failure = conversation_search._document_key(prepared.documents[(0, 800)].references)
        conversation_search._ensure_document_cache(self.store)
        with self.store._connection() as db:
            self.limit_variables(db, 999)
            db.execute("CREATE TABLE document_cache_failure (document TEXT PRIMARY KEY)")
            db.execute("INSERT INTO document_cache_failure VALUES (?)", (failure,))
            db.execute("""CREATE TRIGGER reject_document BEFORE INSERT ON conversation_search_documents
                WHEN EXISTS (SELECT 1 FROM document_cache_failure WHERE document=NEW.document)
                BEGIN SELECT RAISE(ABORT, 'interrupted document batch'); END""")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "interrupted document batch"):
            for _ in conversation_search.seed_prepared_history_steps(self.store, prepared):
                pass
        with self.store._connection() as db:
            # The first complete batch survives; statements preceding the
            # failure in the second batch must all have rolled back together.
            self.assertEqual(db.execute("SELECT COUNT(*) FROM conversation_search_documents").fetchone()[0], 512)
            db.execute("DROP TRIGGER reject_document")
        for _ in conversation_search.seed_prepared_history_steps(self.store, prepared):
            pass
        with self.store.reading(), self.store._connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM conversation_search_documents").fetchone()[0], 1025)

    def test_cached_headers_are_covering_and_only_final_routing_excerpts_are_loaded(self):
        self.create("thread")
        items = [{"id": f"message-{i}", "type": "agentMessage", "text": f"Message {i} " + "x" * 6100}
                 if i % 2 else {"id": f"message-{i}", "type": "userMessage",
                                "content": [{"type": "text", "text": f"Message {i} " + "x" * 6100}]}
                 for i in range(240)]
        items.insert(160, {"type": "commandExecution", "output": "Tool output " * 30000})
        items.extend([items[-1], items[-1]])
        history = {"turns": [{"startedAt": 42, "items": items}]}
        prepared = self.save_chunked("thread", history, seed=True)
        # Upgrade an already populated cache that predates the compact index.
        with self.store._connection() as db:
            db.execute("DROP INDEX conversation_search_document_headers")
        headers = conversation_search._cached_documents

        def compact_headers(db, identities, **options):
            result = headers(db, identities, **options)
            if options.get("metadata", True):
                self.assertTrue(all(row[4] is None for row in result.values()))
            return result

        with patch.object(conversation_search, "_cached_documents", side_effect=compact_headers), patch.object(
                conversation_search, "_cached_document_excerpts", wraps=conversation_search._cached_document_excerpts) as excerpts:
            refresh_messages(self.store)
        self.assertEqual(excerpts.call_count, 1)
        requested = excerpts.call_args.args[1]
        self.assertEqual(len(requested), 198)  # 200 occurrences, including two repeated documents.
        self.assertNotIn(conversation_search._document_key(prepared.documents[(0, 0)].references), requested)
        with self.store.reading(), self.store._connection() as db:
            evidence = json.loads(db.execute("SELECT evidence_json FROM conversation_search_routing_index").fetchone()[0])
            plan = db.execute("""EXPLAIN QUERY PLAN SELECT
                json_group_array(json_array(document,item_id,role,timestamp,NULL,text_length))
                FROM conversation_search_documents INDEXED BY conversation_search_document_headers
                WHERE document IN (?)""", (next(iter(requested)),)).fetchall()
        self.assertEqual(evidence, conversation_search.routing_history(history))
        self.assertTrue(any("USING COVERING INDEX conversation_search_document_headers" in row[3] for row in plan), plan)
        metrics = conversation_search.search_metrics(self.store)["routing_read"]
        self.assertEqual(metrics["documents"], 198)
        self.assertEqual(metrics["characters"], 198 * 6000)

    def test_search_retries_revision_rejection_before_returning_results(self):
        self.create("thread", "Original content")
        refresh_messages(self.store)
        self.save("thread", "Intermediate content")
        publish = conversation_search.publish_messages
        calls = []

        def changed(store, prepared):
            calls.append(prepared)
            if len(calls) == 1:
                self.save("thread", "Newest revision")
            return publish(store, prepared)

        with patch.object(conversation_search, "publish_messages", side_effect=changed):
            page = search_threads(self.store, "newest revision")
        self.assertEqual(len(calls), 2)
        self.assertEqual(page["items"][0]["thread_id"], "thread")
        self.assertEqual(conversation_search.search_metrics(self.store)["refresh"]["revision_conflicts"], 1)

    def test_continuous_revision_rejection_never_returns_an_older_head_as_fresh(self):
        self.create("thread", "Original content")
        refresh_messages(self.store)
        self.save("thread", "Changing content")
        publish = conversation_search.publish_messages
        calls = []

        def changed(store, prepared):
            calls.append(prepared)
            self.save("thread", "Changing revision " + str(len(calls)))
            return publish(store, prepared)

        with patch.object(conversation_search, "publish_messages", side_effect=changed):
            with self.assertRaisesRegex(CoordinationError, "changed repeatedly"):
                search_threads(self.store, "original")
        self.assertEqual(len(calls), 3)

    @unittest.skipUnless(shutil.which("node"), "Node is required for browser search tests")
    def test_browser_search(self):
        result = subprocess.run(["node", "--test", str(Path(__file__).with_name("test_web_conversation_search.js"))],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
