from __future__ import annotations

import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))

from agent_coord.observer_queries import METADATA_FIELDS, metadata
from agent_coord.store import CoordinationError, CoordinationStore


class ObserverQueryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = CoordinationStore(self.root / "state.sqlite3", clock=lambda: 1000)
        for identity, client in (("first", "codex"), ("second", "claude")):
            (self.root / identity).mkdir()
            self.store.register(session_id=identity, client=client, cwd=str(self.root / identity), name=identity.title())

    def test_batch_preserves_requested_order_and_returns_only_metadata(self):
        result = metadata(self.store, ["second", "absent", "first", "second"])
        self.assertEqual([row["thread_id"] for row in result["data"]], ["second", "first"])
        self.assertEqual(result["missing"], ["absent"])
        self.assertEqual(result["data"][0]["client"], "claude")
        self.assertTrue(all(set(row) == set(METADATA_FIELDS) for row in result["data"]))

    def test_workspace_boundary_does_not_expose_other_threads(self):
        result = metadata(self.store, ["first", "second", "absent"], allowed_cwd=str(self.root / "first"))
        self.assertEqual([row["thread_id"] for row in result["data"]], ["first"])
        self.assertEqual(result["missing"], ["second", "absent"])

    def test_batch_uses_one_select_and_remains_available_during_a_write(self):
        statements = []
        connect = self.store._connect

        def traced_connection():
            connection = connect()
            connection.set_trace_callback(statements.append)
            return connection

        with sqlite3.connect(self.store.database_path) as writer:
            writer.execute("BEGIN IMMEDIATE")
            try:
                with patch.object(self.store, "_connect", traced_connection), \
                        patch.object(self.store.threads, "get", side_effect=AssertionError("per-thread projection")), \
                        patch.object(self.store.threads, "ensure", side_effect=AssertionError("read-time repair")):
                    start = time.monotonic()
                    result = metadata(self.store, ["first", "second"])
                    self.assertLess(time.monotonic() - start, 1.0)
                self.assertEqual(len(result["data"]), 2)
            finally:
                writer.rollback()
        reads = [statement for statement in statements if statement.lstrip().upper().startswith("SELECT")]
        self.assertEqual(len(reads), 1)
        self.assertFalse(any(statement.lstrip().split(" ", 1)[0].upper() in {"INSERT", "UPDATE", "DELETE", "CREATE", "ALTER"}
                             for statement in statements))

    def test_expired_snooze_is_projected_without_changing_stored_attention(self):
        self.store.threads.update("first", snoozed_until=1100)
        self.store.clock = lambda: 1200
        self.assertEqual(metadata(self.store, ["first"])["data"][0]["attention"], "now")
        with self.store._connection() as db:
            self.assertEqual(db.execute("SELECT attention FROM work_threads WHERE thread_id = 'first'").fetchone()[0], "later")

    def test_batch_limits_and_empty_requests(self):
        self.assertEqual(metadata(self.store, []), {"data": [], "missing": []})
        for invalid in (None, "first", ["first"] * 201, [None], [False], [""], [" "], ["x" * 257], ["a\0b"]):
            with self.subTest(invalid=type(invalid).__name__):
                with self.assertRaises(CoordinationError):
                    metadata(self.store, invalid)


if __name__ == "__main__":
    unittest.main()
