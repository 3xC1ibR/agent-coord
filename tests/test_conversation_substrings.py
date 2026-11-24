from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))

from agent_coord import conversation_substrings as substrings
from agent_coord.store import CoordinationStore


class SubstringCandidateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = CoordinationStore(Path(self.directory.name) / "state.sqlite3")
        if not substrings.ensure_schema(self.store):
            self.skipTest("SQLite trigram tokenizer unavailable")

    def seed(self, records):
        rows = (row for source, text in records for row in substrings._text_rows(source, text))
        for _ in substrings._stage_steps(self.store, rows):
            pass

    def candidates(self, terms):
        with self.store.reading(), self.store._connection() as db:
            return set(json.loads(substrings.candidates(db, terms, {})))

    def projections(self, count=2):
        records = []
        with self.store._connection() as db:
            db.execute("INSERT INTO conversation_search_heads VALUES ('documents','documents',NULL,'saved')")
            db.execute("INSERT INTO conversation_search_heads VALUES ('generations','generations',NULL,'saved')")
            db.execute("INSERT INTO conversation_search_routing VALUES ('legacy','[]')")
            for i in range(count):
                prose = "installation verification" if i < 2 else "unrelated conversation history"
                document = f"document-{i}"
                db.execute("INSERT INTO conversation_search_document_text VALUES (?,0,?,?)", (document, prose, prose))
                db.execute("INSERT INTO conversation_search_document_entries VALUES ('documents',?,'turn',?,'user',0,?)",
                           (i, str(i), document))
                db.execute("INSERT INTO conversation_search_entries VALUES ('generations',?,'turn',?,'user',0)", (i, str(i)))
                db.execute("INSERT INTO conversation_search_text_chunks VALUES ('generations',?,0,?,?)", (i, prose, prose))
                rowid = db.execute("INSERT INTO conversation_search_messages VALUES ('legacy','turn',?,'user',0,?,?,?)",
                                   (str(i), prose, prose, i)).lastrowid
                records.extend(((f"document:{document}", prose), (f"generation:generations:{i}", prose),
                                (f"legacy:{rowid}:1", prose)))
        return records

    def pending(self):
        with self.store.reading(), self.store._connection() as db:
            return [tuple(row) for row in db.execute("SELECT identity,source FROM conversation_substring_pending ORDER BY identity")]

    def messages(self, candidates, *, indexed=True, steps=None):
        with self.store.reading(), self.store._connection() as db:
            if steps is not None:
                def progress():
                    steps[0] += 100
                    return 0
                db.set_progress_handler(progress, 100)
            try:
                return [tuple(row) for row in db.execute("SELECT * FROM (" + substrings.message_source(
                    include_text=True, indexed=indexed) + ") ORDER BY thread_id,position",
                    {"substring_candidates": json.dumps(candidates)})]
            finally:
                db.set_progress_handler(None, 0)

    def test_indexed_lookup_does_not_scan_unrelated_ready_entries(self):
        records = self.projections(1000)
        self.seed(records)
        selected = self.candidates(["installation", "verification"])
        steps = [0]
        rows = self.messages(sorted(selected), steps=steps)
        self.assertEqual(len(rows), 6)
        # Count SQLite work, independent of filesystem warmth or machine load.
        # The old readiness-per-entry plan executes over 100,000 instructions.
        self.assertLess(steps[0], 10000)
        self.assertEqual(self.pending(), [])

    def test_missing_and_unusable_sources_remain_exact_without_fts_candidates(self):
        records = self.projections()
        self.seed(records)
        expected = self.messages([], indexed=False)
        with self.store._connection() as db:
            for i, (source, _) in enumerate(records):
                if i < 3:
                    db.execute("DELETE FROM conversation_substring_ready WHERE source=?", (source,))
                else:
                    db.execute("UPDATE conversation_substring_ready SET usable=0 WHERE source=?", (source,))
        self.assertEqual(len(self.pending()), 6)
        self.assertEqual(self.messages([]), expected)
        # A source in both FTS and fallback must not duplicate its messages.
        self.assertEqual(self.messages([source for source, _ in records]), expected)

    def test_entry_replacement_and_shared_documents_bound_pending_registry(self):
        self.projections()
        with self.store._connection() as db:
            db.execute("INSERT OR REPLACE INTO conversation_search_document_entries VALUES ('documents',1,'turn','1','user',0,'document-0')")
            self.assertEqual(db.execute("SELECT count(*) FROM conversation_substring_pending WHERE source='document:document-0'").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT count(*) FROM conversation_substring_pending WHERE source='document:document-1'").fetchone()[0], 0)
            db.execute("INSERT OR REPLACE INTO conversation_substring_ready VALUES ('document:document-0',1)")
            self.assertEqual(db.execute("SELECT count(*) FROM conversation_substring_pending WHERE source='document:document-0'").fetchone()[0], 0)
            db.execute("INSERT OR REPLACE INTO conversation_substring_ready VALUES ('document:document-0',0)")
            self.assertEqual(db.execute("SELECT count(*) FROM conversation_substring_pending WHERE source='document:document-0'").fetchone()[0], 2)
            db.execute("DELETE FROM conversation_search_document_entries WHERE position=0")
            self.assertEqual(db.execute("SELECT count(*) FROM conversation_substring_pending WHERE source='document:document-0'").fetchone()[0], 1)
            db.execute("DELETE FROM conversation_search_document_entries")
            self.assertEqual(db.execute("SELECT count(*) FROM conversation_substring_pending WHERE source LIKE 'document:%'").fetchone()[0], 0)

    def test_legacy_revisions_replacement_and_rowid_reuse_keep_exact_fallback(self):
        self.seed(self.projections(1))
        with self.store._connection() as db:
            rowid = db.execute("SELECT rowid FROM conversation_search_messages").fetchone()[0]
            db.execute("UPDATE conversation_search_messages SET text='changed',folded='changed'")
        self.assertEqual([source for _, source in self.pending()], [f"legacy:{rowid}:3"])
        self.assertEqual(self.messages([f"legacy:{rowid}:1"])[0][6], "changed")
        with self.store._connection() as db:
            db.execute("INSERT OR REPLACE INTO conversation_search_messages(rowid,thread_id,turn_id,item_id,role,timestamp,text,folded,position) "
                       "VALUES (99,'legacy','turn','0','user',0,'replacement','replacement',0)")
        self.assertEqual([source for _, source in self.pending()], ["legacy:99:1"])
        with self.store._connection() as db:
            db.execute("DELETE FROM conversation_search_messages")
            db.execute("INSERT INTO conversation_search_messages(rowid,thread_id,turn_id,item_id,role,timestamp,text,folded,position) "
                       "VALUES (99,'legacy','turn','other','user',0,'reused','reused',0)")
        self.assertEqual([source for _, source in self.pending()], ["legacy:99:3"])
        self.assertEqual(self.messages(["legacy:99:1"])[0][6], "reused")

    def test_pending_changes_roll_back_with_projection_and_readiness(self):
        self.projections()
        before = self.pending()
        with self.assertRaisesRegex(RuntimeError, "rollback"):
            with self.store._connection() as db:
                db.execute("DELETE FROM conversation_search_document_entries")
                db.execute("INSERT INTO conversation_substring_ready VALUES ('generation:generations:0',1)")
                raise RuntimeError("rollback")
        self.assertEqual(self.pending(), before)

    def test_pending_entries_preserve_publication_and_legacy_visibility(self):
        self.projections()
        with self.store._connection() as db:
            db.execute("INSERT INTO conversation_search_document_entries VALUES ('staging',0,'turn','staged','user',0,'document-0')")
        self.assertEqual(len(self.messages([])), 6)
        with self.store._connection() as db:
            db.execute("UPDATE conversation_search_heads SET generation='staging' WHERE thread_id='documents'")
        self.assertEqual(len(self.messages([])), 5)
        self.assertEqual(self.messages([]), self.messages([], indexed=False))
        with self.store._connection() as db:
            db.execute("DELETE FROM conversation_search_document_entries WHERE generation='documents'")
            db.execute("INSERT INTO conversation_search_dirty VALUES ('legacy')")
        self.assertEqual(len(self.pending()), 5)
        self.assertEqual(len(self.messages([])), 3)
        with self.store._connection() as db:
            db.execute("DELETE FROM conversation_search_dirty")
            db.execute("INSERT INTO conversation_search_heads VALUES ('legacy','empty',NULL,'saved')")
        self.assertEqual(len(self.messages([])), 3)
        with self.store._connection() as db:
            db.execute("DELETE FROM conversation_search_heads WHERE thread_id='legacy'")
        self.assertEqual(len(self.messages([])), 5)
        self.assertEqual(self.messages([]), self.messages([], indexed=False))

    def test_existing_projection_bootstrap_and_older_writer_statements(self):
        self.projections()
        with self.store._connection() as db:
            names = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'conversation_substring_%'")]
            for name in names:
                db.execute(f"DROP TRIGGER {name}")
            db.execute("DROP TABLE conversation_substring_pending")
        substrings._CAPABILITY.pop(self.store)
        self.assertTrue(substrings.ensure_schema(self.store))
        self.assertEqual(len(self.pending()), 6)
        with self.store._connection() as db:
            # Older clients install revision-only triggers with IF NOT EXISTS.
            db.execute("CREATE TRIGGER IF NOT EXISTS conversation_substring_legacy_insert AFTER INSERT ON conversation_search_messages BEGIN SELECT 1; END")
            db.execute("INSERT INTO conversation_search_messages VALUES ('legacy','turn','older','user',0,'new','new',2)")
        self.assertEqual(len(self.pending()), 7)
        restarted = CoordinationStore(self.store.database_path)
        self.assertTrue(substrings.ensure_schema(restarted))
        self.assertEqual(len(self.pending()), 7)

    def test_shared_suffix_does_not_select_unrelated_conversations(self):
        self.seed([(f"unrelated:{i}", "discussion of configuration and operation") for i in range(200)]
                  + [("matching", "installation verification")])
        self.assertEqual(self.candidates(["installation", "verification"]), {"matching"})

    def test_selected_terms_can_cross_parts_and_include_unicode(self):
        boundary = substrings.PART_CHARACTERS
        # "abc" is uniquely rare and crosses the part boundary; all other
        # grams of "abcdef" also occur in the distractor.
        prose = "x" * (boundary - 1) + "abcdef " + "y" * boundary + "verification strasse 𐐨bc"
        self.seed([("matching", prose), ("other", "bcdef configuration operation strasse 𐐨bd")])
        for terms in (["abcdef", "verification"], ["strasse", "𐐨bc"], ["abcdef"]):
            with self.subTest(terms=terms):
                self.assertEqual(self.candidates(terms), {"matching"})
        self.assertEqual(self.candidates(["nonexistent"]), set())

    def test_unavailable_vocabulary_reader_keeps_exact_fallback(self):
        with self.store.reading(), self.store._connection() as db:
            proxy = Mock(wraps=db)

            def execute(sql, *args):
                if "SELECT term, doc FROM conversation_substring_vocabulary" in sql:
                    raise sqlite3.OperationalError("no such tokenizer: trigram")
                return db.execute(sql, *args)

            proxy.execute.side_effect = execute
            measurement = {}
            self.assertIsNone(substrings.candidates(proxy, ["installation"], measurement))
            self.assertEqual(measurement, {"unavailable_query_fallbacks": 1})


if __name__ == "__main__":
    unittest.main()
