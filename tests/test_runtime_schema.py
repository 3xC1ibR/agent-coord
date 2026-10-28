from __future__ import annotations
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))
from agent_coord import desktop_runtime as runtime
from agent_coord.store import CoordinationError, CoordinationStore


class RuntimeSchemaTests(unittest.TestCase):
    def test_new_cli_cannot_initialize_under_old_owner_even_with_inherited_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "state.sqlite3"
            CoordinationStore(database)
            directory = runtime.runtime_directory(database)
            owner = runtime.lock_file(directory / "owner.lock")
            try:
                with patch.object(runtime, "schema_fingerprint", return_value="candidate-schema"), patch.dict(os.environ, RIBBON_RUNTIME_OWNER=str(database)):
                    with patch.object(CoordinationStore, "_initialize") as initialize:
                        with self.assertRaisesRegex(CoordinationError, "different Ribbon Field runtime"):
                            CoordinationStore(database)
                        initialize.assert_not_called()
                # Matching CLI remains usable during work.
                CoordinationStore(database)
            finally:
                os.close(owner)
            with patch.object(runtime, "schema_fingerprint", return_value="candidate-schema"):
                CoordinationStore(database)
            self.assertEqual(runtime.read_json(directory / "schema.json")["fingerprint"], "candidate-schema")

    def test_schema_downgrade_is_rejected_without_opening_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "state.sqlite3"
            directory = runtime.runtime_directory(database)
            runtime.atomic_json(directory / "schema.json", {"schema":runtime.SCHEMA_VERSION + 1, "fingerprint":"future"})
            with self.assertRaisesRegex(CoordinationError, "downgrade is unsafe"):
                CoordinationStore(database)
            self.assertFalse(database.exists())

    def test_symlink_database_uses_same_owner_domain(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "state.sqlite3"
            CoordinationStore(database)
            alias = Path(temporary) / "alias.sqlite3"
            alias.symlink_to(database)
            self.assertEqual(runtime.runtime_directory(alias), runtime.runtime_directory(database))
