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
