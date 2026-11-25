"""Expand-only SQLite schema compatibility for concurrent blue/green coordinator cutover."""
from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/agent-coord/scripts"))
from agent_coord import desktop_runtime  # noqa: E402
from agent_coord.store import CoordinationStore  # noqa: E402

RETIRED_STAGE3_PREFIXES = (
    "browser_history",
    "conversation_search",
    "conversation_substring",
    "coordinator_projection_payloads",
    "thread_search",
)


def _table_columns(connection: sqlite3.Connection, table: str) -> dict[str, tuple[str, int, object]]:
    return {
        row[1]: (row[2].upper(), int(row[3]), row[4])
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }


def _previous_git_revision() -> str | None:
    tags = subprocess.run(
        ["git", "tag", "-l", "v[0-9]*", "--sort=-v:refname"],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    if tags.returncode == 0:
        candidates = [line.strip() for line in tags.stdout.splitlines() if line.strip()]
        if candidates:
            return candidates[0]
    parent = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD~1"],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    if parent.returncode == 0 and parent.stdout.strip():
        return parent.stdout.strip()
    return None


class ExpandOnlySchemaCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="ribbon-schema-compat-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / "state.sqlite3"

    def test_previous_release_store_reads_and_writes_head_migrated_database(self):
        revision = _previous_git_revision()
        if revision is None:
            self.skipTest("Git history unavailable for predecessor schema extraction")

        previous_tree = self.root / "previous-release"
        previous_tree.mkdir()
        archive = subprocess.Popen(
            ["git", "archive", revision, "plugins/agent-coord/scripts"],
            cwd=ROOT, stdout=subprocess.PIPE,
        )
        try:
            extract = subprocess.run(["tar", "-x", "-C", str(previous_tree)], stdin=archive.stdout, check=True)
            self.assertEqual(extract.returncode, 0)
        finally:
            if archive.stdout is not None:
                archive.stdout.close()
            archive.wait(timeout=10)

        previous_pythonpath = str(previous_tree / "plugins/agent-coord/scripts")
        seed_script = """
import json, pathlib, sqlite3, sys
from contextlib import closing
db_path = pathlib.Path(sys.argv[1]).resolve()
from agent_coord import desktop_runtime
desktop_runtime._OWNED_DATABASES.add(str(db_path))
from agent_coord.store import CoordinationStore
store = CoordinationStore(db_path)
store.register(session_id="prev-session-1", client="codex", cwd=str(db_path.parent), name="Previous Release Session")
with closing(sqlite3.connect(db_path)) as conn:
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()]
    schema = {t: {r[1]: [r[2].upper(), int(r[3]), r[4]] for r in conn.execute(f"PRAGMA table_info({t})").fetchall()} for t in tables}
print(json.dumps(schema))
"""
        seeded = subprocess.run(
            [sys.executable, "-W", "error::ResourceWarning", "-c", seed_script, str(self.database)],
            env={**os.environ, "PYTHONPATH": previous_pythonpath},
            capture_output=True, text=True, timeout=20, check=True,
        )
        previous_schema = json.loads(seeded.stdout)

        # Migrate and write using HEAD's CoordinationStore.
        canonical = str(self.database.resolve())
        desktop_runtime._OWNED_DATABASES.add(canonical)
        self.addCleanup(desktop_runtime._OWNED_DATABASES.discard, canonical)
        head_store = CoordinationStore(self.database)
        self.assertEqual(head_store.get_session("prev-session-1")["name"], "Previous Release Session")
        head_store.register(session_id="head-session-2", client="claude", cwd=str(self.root), name="HEAD Session")

        # Verify expand-only column compatibility on all retained coordination tables.
        with closing(sqlite3.connect(self.database)) as migrated_db:
            for table, original_cols in previous_schema.items():
                if table.startswith(RETIRED_STAGE3_PREFIXES):
                    continue
                migrated_cols = _table_columns(migrated_db, table)
                self.assertTrue(
                    set(original_cols).issubset(migrated_cols),
                    f"HEAD dropped columns from {table}: {set(original_cols) - set(migrated_cols)}",
                )
                for added_col in set(migrated_cols) - set(original_cols):
                    _col_type, not_null, default_value = migrated_cols[added_col]
                    self.assertFalse(
                        not_null and default_value is None,
                        f"Added column {table}.{added_col} is NOT NULL without DEFAULT",
                    )

        # Verify the previous release's CoordinationStore can still read and write the HEAD-migrated database
        # during the blue/green overlap window before cutover completes.
        coexist_script = """
import json, pathlib, sys
db_path = pathlib.Path(sys.argv[1]).resolve()
from agent_coord import desktop_runtime
desktop_runtime._OWNED_DATABASES.add(str(db_path))
from agent_coord.store import CoordinationStore
store = CoordinationStore(db_path)
assert store.get_session("prev-session-1")["name"] == "Previous Release Session"
assert store.get_session("head-session-2")["name"] == "HEAD Session"
store.register(session_id="prev-during-overlap", client="codex", cwd=str(db_path.parent), name="Written By V1 During Warmup")
print(json.dumps({"ok": True}))
"""
        coexist = subprocess.run(
            [sys.executable, "-W", "error::ResourceWarning", "-c", coexist_script, str(self.database)],
            env={**os.environ, "PYTHONPATH": previous_pythonpath},
            capture_output=True, text=True, timeout=20, check=True,
        )
        self.assertTrue(json.loads(coexist.stdout)["ok"])
        self.assertEqual(head_store.get_session("prev-during-overlap")["name"], "Written By V1 During Warmup")

    def test_concurrent_wal_coexistence_between_active_and_standby_coordinators(self):
        canonical = str(self.database.resolve())
        desktop_runtime._OWNED_DATABASES.add(canonical)
        self.addCleanup(desktop_runtime._OWNED_DATABASES.discard, canonical)

        active_store = CoordinationStore(self.database)
        active_store.register(session_id="blue-thread", client="codex", cwd=str(self.root), name="Blue Active")
        standby_store = CoordinationStore(self.database)
        self.assertEqual(standby_store.get_session("blue-thread")["name"], "Blue Active")

        active_store.register(session_id="during-warmup", client="claude", cwd=str(self.root), name="Written During Warmup")
        self.assertEqual(standby_store.get_session("during-warmup")["name"], "Written During Warmup")


if __name__ == "__main__":
    unittest.main()
