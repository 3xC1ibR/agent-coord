import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))

from agent_coord import conversation_substrings as substrings
from agent_coord.store import CoordinationStore


class SearchWriteAdmissionTests(unittest.TestCase):
    def test_long_output_releases_the_writer_before_indexing_the_next_full_part(self):
        with tempfile.TemporaryDirectory() as directory:
            store = CoordinationStore(Path(directory) / "test.sqlite3")
            if not substrings.ensure_schema(store):
                self.skipTest("SQLite trigram indexing is unavailable")
            text = "a" * (substrings.PART_CHARACTERS - 1) + "xyz" + "b" * substrings.PART_CHARACTERS
            steps = substrings._stage_steps(store, substrings._text_rows("output", text))
            next(steps)
            with store._connection() as db:
                indexed = db.execute("SELECT part FROM conversation_substring_parts WHERE indexed=1").fetchall()
                self.assertEqual([row[0] for row in indexed], [0])
                self.assertIsNone(db.execute("SELECT 1 FROM conversation_substring_ready").fetchone())
                db.execute("CREATE TABLE foreground_request (id INTEGER PRIMARY KEY)")
                db.execute("INSERT INTO foreground_request VALUES (1)")
            for _ in steps:
                pass
            with store.reading(), store._connection() as db:
                self.assertEqual(db.execute("SELECT usable FROM conversation_substring_ready").fetchone()[0], 1)
                self.assertEqual(db.execute("""SELECT p.part FROM conversation_substring_terms f
                    JOIN conversation_substring_parts p ON p.id=f.rowid
                    WHERE conversation_substring_terms MATCH 'xyz'""").fetchone()[0], 1)
