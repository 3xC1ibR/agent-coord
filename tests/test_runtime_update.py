from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from test_desktop_runtime import runtime
from agent_coord import runtime_update


class UpdateTransactionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database = str(Path(temporary.name) / "state.sqlite3")
        self.directory = runtime.runtime_directory(self.database)
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("CREATE TABLE state(value TEXT)")
            db.execute("INSERT INTO state VALUES ('original')")
            db.commit()
        runtime.atomic_json(self.directory / "schema.json", {"schema": 2, "fingerprint": "original"})
        self.update = runtime_update.write(self.directory, {
            "id": "transaction", "phase": "stopping", "previous": "/previous", "target": "/candidate"})

    def test_rollback_restores_database_and_schema_and_is_repeatable(self):
        update = runtime_update.snapshot(self.database, self.directory, self.update)
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("UPDATE state SET value='migrated'")
            db.commit()
        runtime.atomic_json(self.directory / "schema.json", {"schema": 999})
        for _ in range(2):
            runtime_update.restore(self.database, self.directory, update)
            with closing(sqlite3.connect(self.database)) as db:
                self.assertEqual(db.execute("SELECT value FROM state").fetchone()[0], "original")
            self.assertEqual(runtime.read_json(self.directory / "schema.json")["schema"], 2)

    def test_stale_failure_write_preserves_stop_cancellation(self):
        runtime_update.write(self.directory, self.update, cancelled=True)
        result = runtime_update.write(self.directory, self.update, phase="rolled-back", error="failure")
        self.assertTrue(result["cancelled"])

    def test_cancellation_survives_concurrent_phase_replacement(self):
        original = runtime.atomic_json
        def interleaved(path, value):
            if path.name == "update.json" and value.get("phase") == "trial":
                runtime_update.write(self.directory, self.update, cancelled=True)
            original(path, value)
        with patch.object(runtime, "atomic_json", side_effect=interleaved):
            runtime_update.write(self.directory, self.update, phase="trial")
        self.assertTrue(runtime_update.read(self.directory)["cancelled"])

    def test_crash_before_snapshot_restores_previous_without_replaying_work(self):
        self.assertEqual(runtime_update.recover(self.database, self.directory, Path("/candidate")), Path("/previous"))
        self.assertTrue(runtime_update.read(self.directory)["recovery_uncertain"])

    def test_crash_after_trial_migration_restores_before_booting(self):
        update = runtime_update.snapshot(self.database, self.directory, self.update)
        runtime_update.write(self.directory, update, phase="trial")
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("UPDATE state SET value='trial'")
            db.commit()
        self.assertEqual(runtime_update.recover(self.database, self.directory, Path("/candidate")), Path("/previous"))
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT value FROM state").fetchone()[0], "original")

    def test_trial_control_cannot_open_admission(self):
        candidate = runtime.Runtime(self.database, self.directory, -1, commit_fd=999)
        for action in ("resume", "attach", "client-request", "activate-update"):
            with self.subTest(action=action), self.assertRaisesRegex(RuntimeError, "not been committed"):
                candidate.dispatch({"action": action})

    def test_stop_blocks_both_candidate_activation_and_resume(self):
        candidate = runtime.Runtime(self.database, self.directory, -1, commit_fd=999)
        gate = runtime.AdmissionGate()
        gate.pause()
        candidate.server = SimpleNamespace(browser_sessions=SimpleNamespace(admission=gate))
        runtime_update.write(self.directory, self.update, phase="committed", target=str(candidate.payload))
        candidate.stopping.set()
        candidate.maintenance = True
        with self.assertRaisesRegex(RuntimeError, "not committed"):
            candidate.activate_committed()
        candidate.resume_work()
        self.assertTrue(gate.paused)

    def test_fallback_stop_reaches_candidate_that_committed_during_discovery_outage(self):
        runtime_update.write(self.directory, self.update, phase="trial", source_instance="previous-instance")
        current = {"instance": "committed-instance"}
        lock = runtime.lock_file
        def commit_before_cancel(path, **kwargs):
            if path.name == "update-control.lock":
                runtime_update.write(self.directory, self.update, phase="committed")
            return lock(path, **kwargs)
        with (patch.object(runtime, "discover", side_effect=[RuntimeError("handover"), current]),
              patch.object(runtime, "lock_file", side_effect=commit_before_cancel),
              patch.object(runtime, "confirm_stopped", side_effect=[False, True]),
              patch.object(runtime, "control") as control,
              patch("builtins.print")):
            self.assertEqual(runtime.main(["--db", self.database, "--runtime-command", "stop"]), 0)
        control.assert_called_once_with(current, "stop", timeout=2)
        self.assertTrue(runtime_update.read(self.directory)["cancelled"])

    def test_failed_candidate_does_not_become_boot_default(self):
        runtime_update.write(self.directory, self.update, phase="rolled-back")
        self.assertEqual(runtime_update.recover(self.database, self.directory, Path("/candidate")), Path("/previous"))

    def test_later_idle_handover_supersedes_transaction_boot_choice(self):
        runtime_update.write(self.directory, self.update, phase="committed", superseded_by="/later")
        self.assertEqual(runtime_update.recover(self.database, self.directory, Path("/candidate")), Path("/later"))
