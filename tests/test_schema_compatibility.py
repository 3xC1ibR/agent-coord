"""The retained Build 98 storage API must survive the candidate schema."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from test_desktop_runtime import ROOT, runtime

BUILD_98_SOURCE = "3df2ef71a62fa466255d4f5aa73ea99998765be5"
SCRIPTS = Path("plugins/agent-coord/scripts")

INITIALIZE = """
import sys
from pathlib import Path
from agent_coord.store import CoordinationStore

database = Path(sys.argv[1])
stage = sys.argv[2]
store = CoordinationStore(database)
if stage == "previous":
    store.register(session_id="previous", client="codex", cwd=str(database.parent), name="Previous writer")
elif stage == "candidate":
    assert store.get_session("previous")["name"] == "Previous writer"
    store.register(session_id="candidate", client="codex", cwd=str(database.parent), name="Candidate writer")
elif stage == "previous-after":
    assert store.get_session("candidate")["name"] == "Candidate writer"
    store.register(session_id="previous", client="codex", cwd=str(database.parent), name="Previous after migration")
    assert store.get_session("previous")["name"] == "Previous after migration"
elif stage == "candidate-after":
    assert store.get_session("previous")["name"] == "Previous after migration"
"""


class PreviousSchemaCompatibilityTests(unittest.TestCase):
    def test_previous_store_reads_and_writes_state_migrated_by_candidate(self):
        with tempfile.TemporaryDirectory(prefix="ribbon-schema-compatibility-") as directory:
            root = Path(directory)
            previous = os.environ.get("RIBBON_PREVIOUS_SCHEMA_SOURCE")
            if not previous:
                archive = subprocess.run(["git", "archive", BUILD_98_SOURCE, str(SCRIPTS)], cwd=ROOT,
                                         capture_output=True, timeout=10)
                if archive.returncode:
                    if os.environ.get("RIBBON_SCHEMA_REQUIRE_PREDECESSOR") == "1":
                        self.fail(archive.stderr.decode())
                    self.skipTest("Build 98 source requires repository history")
                subprocess.run(["tar", "-xf", "-", "-C", str(root)], input=archive.stdout, check=True, timeout=10)
                previous = str(root / SCRIPTS)
            database = root / "state.sqlite3"
            private = runtime.runtime_directory(database)
            for stage, source in (("previous", previous), ("candidate", ROOT / SCRIPTS),
                                  ("previous-after", previous), ("candidate-after", ROOT / SCRIPTS)):
                with self.subTest(stage=stage):
                    result = subprocess.run([sys.executable, "-B", "-c", INITIALIZE, str(database), stage],
                                            env={**os.environ, "PYTHONPATH": str(source)},
                                            capture_output=True, text=True, timeout=20, cwd=root)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(json.loads((private / "schema.json").read_text())["schema"], 2)
