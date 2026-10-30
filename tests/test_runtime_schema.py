from __future__ import annotations
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import closing
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))
from agent_coord import desktop_runtime as runtime
from agent_coord.runtime_requests import owner_operation, owns_database
from agent_coord.store import CoordinationError, CoordinationStore


class RuntimeSchemaTests(unittest.TestCase):
    def test_current_schema_reopens_without_initialization_while_writer_is_held(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "state.sqlite3"
            original = CoordinationStore(database)
            original.register(session_id="saved", client="codex", cwd=temporary)
            with closing(sqlite3.connect(database)) as writer:
                writer.execute("BEGIN IMMEDIATE")
                try:
                    with patch.object(CoordinationStore, "_initialize", side_effect=AssertionError("Repeated core initialization")) as initialize:
                        started = time.monotonic()
                        for read_only in (False, True):
                            reopened = CoordinationStore(database, read_only=read_only)
                            with reopened.reading():
                                self.assertEqual(reopened.threads.get("saved")["thread_id"], "saved")
                        self.assertLess(time.monotonic() - started, 1.0)
                        initialize.assert_not_called()
                finally:
                    writer.rollback()

    def test_standalone_schema_change_invalidates_the_completed_migration_cookie(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "state.sqlite3"
            original = CoordinationStore(database)
            original.register(session_id="saved", client="codex", cwd=temporary)
            fingerprint = runtime.schema_fingerprint()
            with closing(sqlite3.connect(database)) as db:
                db.execute("CREATE TABLE supplemental_records (id INTEGER PRIMARY KEY)")
            self.assertFalse(runtime.schema_initialized(database, fingerprint))
            with patch.object(CoordinationStore, "_initialize", autospec=True,
                              side_effect=CoordinationStore._initialize) as initialize:
                reopened = CoordinationStore(database)
            initialize.assert_called_once()
            self.assertEqual(reopened.get_session("saved")["session_id"], "saved")
            self.assertTrue(runtime.schema_initialized(database, fingerprint))
            with reopened.reading(), reopened._connection() as db:
                self.assertEqual(db.execute("SELECT schema_cookie FROM coordination_schema WHERE id=1").fetchone()[0],
                                 db.execute("PRAGMA schema_version").fetchone()[0])

    def test_matching_live_runtime_accepts_optional_feature_tables_without_reinitializing(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "state.sqlite3"
            original = CoordinationStore(database)
            original.register(session_id="saved", client="codex", cwd=temporary)
            directory = runtime.runtime_directory(database)
            owner = runtime.lock_file(directory / "owner.lock")
            try:
                with closing(sqlite3.connect(database)) as db:
                    db.execute("CREATE TABLE optional_runtime_records (id INTEGER PRIMARY KEY)")
                self.assertFalse(runtime.schema_initialized(database, runtime.schema_fingerprint()))
                for local_owner in (False, True):
                    with self.subTest(local_owner=local_owner), \
                            patch.object(runtime, "_OWNED_DATABASES", {str(database.resolve())} if local_owner else set()), \
                            patch.object(CoordinationStore, "_initialize", side_effect=AssertionError("Repeated live migration")) as initialize:
                        for read_only in (False, True):
                            reopened = CoordinationStore(database, read_only=read_only)
                            with reopened.reading():
                                self.assertEqual(reopened.get_session("saved")["session_id"], "saved")
                        initialize.assert_not_called()
            finally:
                os.close(owner)

    def test_request_ownership_is_reentrant_during_initialization(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "state.sqlite3"
            with patch.object(runtime, "lock_file", wraps=runtime.lock_file) as locking:
                with owner_operation(database) as owner:
                    self.assertTrue(owner)
                    self.assertTrue(owns_database(database))
                    store = CoordinationStore(database)
                    store.register(session_id="saved", client="codex", cwd=temporary)
                    self.assertTrue(owns_database(database))
                self.assertFalse(owns_database(database))
            owner_locks = [call for call in locking.call_args_list if Path(call.args[0]).name == "owner.lock"]
            self.assertEqual(len(owner_locks), 1)
            self.assertEqual(store.get_session("saved")["session_id"], "saved")

    def test_readonly_store_and_nested_read_boundaries_reject_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "state.sqlite3"
            writable = CoordinationStore(database)
            writable.register(session_id="saved", client="codex", cwd=temporary, name="Original name")
            readonly = CoordinationStore(database, read_only=True)
            with readonly._connection() as db:
                self.assertEqual(db.execute("PRAGMA query_only").fetchone()[0], 1)
                with self.assertRaisesRegex(sqlite3.OperationalError, "readonly"):
                    db.execute("UPDATE sessions SET name = 'Unexpected write' WHERE session_id = 'saved'")
            with writable.reading(), writable._connection() as outer:
                with writable.reading(), writable._connection() as inner:
                    with self.assertRaisesRegex(sqlite3.OperationalError, "readonly"):
                        inner.execute("UPDATE sessions SET name = 'Unexpected nested write' WHERE session_id = 'saved'")
                self.assertEqual(outer.execute("PRAGMA query_only").fetchone()[0], 1)
            self.assertEqual(writable.get_session("saved")["name"], "Original name")
            # The outer read boundary restores normal mutation access.
            writable.register(session_id="saved", client="codex", cwd=temporary, name="Updated name")
            self.assertEqual(writable.get_session("saved")["name"], "Updated name")

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
