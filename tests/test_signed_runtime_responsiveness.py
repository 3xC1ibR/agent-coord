"""Release gates must catch uncertain acknowledgments and silence across phase boundaries."""
from contextlib import closing, ExitStack
from pathlib import Path
import json
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from validate_signed_runtime_responsiveness import SignedUpgrade, match_snapshot_size


class SignedAcceptanceGateTests(unittest.TestCase):
    def test_guarded_upgrade_preserves_database_copy_size_without_private_tables(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            source = Path(directory) / "offline.sqlite3"
            fixture = Path(directory) / "private.sqlite3"
            backup = Path(directory) / "rollback.sqlite3"
            original = stack.enter_context(closing(sqlite3.connect(source, isolation_level=None)))
            original.execute("PRAGMA journal_mode=WAL")
            original.execute("PRAGMA wal_autocheckpoint=0")
            original.execute("CREATE TABLE private_jobs (command BLOB)")
            original.execute("INSERT INTO private_jobs VALUES (zeroblob(10000000))")
            with closing(sqlite3.connect(fixture)) as db:
                db.execute("CREATE TABLE saved_messages (id INTEGER PRIMARY KEY)")
            evidence = match_snapshot_size(source, fixture)
            self.assertGreater(evidence["source_database_bytes"], source.stat().st_size)
            self.assertGreaterEqual(evidence["fixture_database_bytes"], evidence["source_database_bytes"])
            self.assertEqual(match_snapshot_size(source, fixture), evidence)
            with closing(sqlite3.connect(fixture)) as db, closing(sqlite3.connect(backup)) as target:
                self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='private_jobs'").fetchone())
                db.backup(target)
            self.assertEqual(backup.stat().st_size, evidence["fixture_database_bytes"])

    def test_browser_send_intent_does_not_count_as_a_completed_interaction(self):
        with tempfile.TemporaryDirectory() as directory:
            run = object.__new__(SignedUpgrade)
            run.root = Path(directory)
            run.browser = mock.Mock()
            run.browser_err = mock.Mock()
            (run.root / "browser-errors.log").touch()
            rows = [
                {"phase": "upgrade", "kind": "browser_send_intent", "marker": "request", "start": 100},
                {"phase": "upgrade", "kind": "browser_send", "ok": False, "start": 100, "end": 300},
            ]
            (run.root / "browser-observations.jsonl").write_text("\n".join(map(json.dumps, rows)))
            run.report = {"phase_bounds": {"upgrade": [0, 6]}, "wall_phase_bounds": {"upgrade": [0, 6000]},
                          "violations": []}
            run.finish_browser()
            result = run.report["browser_results"]["upgrade"]["browser_send"]
            self.assertEqual((result["attempts"], result["successes"]), (1, 0))
            self.assertEqual(result["maximum_seconds_without_success"], 6)
            self.assertTrue(any(v["kind"] == "browser_send" for v in run.report["violations"]))

    def test_durable_acceptance_is_checked_even_when_the_client_timed_out(self):
        with tempfile.TemporaryDirectory() as root:
            run = object.__new__(SignedUpgrade)
            run.database = Path(root) / "private.sqlite3"
            run.peers = ["stream"]
            run.composer = "browser"
            run.report = {
                "samples": [],
                "receipts": [],
                "violations": [],
                "browser_observations": [
                    {"kind": "browser_send_intent", "marker": "browser-lost", "phase": "upgrade"}
                ],
            }
            with sqlite3.connect(run.database) as db:
                db.execute(
                    "CREATE TABLE browser_submission_receipts (thread_id,request_id,payload_json,state)"
                )
                for key, state in [
                    ("healthy", "completed"),
                    ("missing", "completed"),
                    ("duplicate", "completed"),
                    ("incomplete", "accepted"),
                ]:
                    run.report["samples"].append(
                        {"kind": "send", "request_id": key, "phase": "upgrade", "outcome": "uncertain"}
                    )
                    db.execute(
                        "INSERT INTO browser_submission_receipts VALUES(?,?,?,?)",
                        ("stream", key, json.dumps({"body": {"message": key}}), state),
                    )
                db.execute(
                    "INSERT INTO browser_submission_receipts VALUES(?,?,?,?)",
                    (
                        "browser",
                        "browser-receipt",
                        json.dumps({"body": {"message": "browser-lost"}}),
                        "completed",
                    ),
                )
            run.records = mock.Mock(
                return_value=[
                    {
                        "kind": "input",
                        "message": {"method": "turn/steer", "params": {"input": [{"text": key}]}},
                    }
                    for key in ["healthy", "duplicate", "duplicate", "incomplete"]
                ]
            )
            run.verify_receipts()
            self.assertEqual(
                {e["request_id"] for e in run.report["violations"]},
                {"missing", "duplicate", "incomplete", "browser-receipt"},
            )
            self.assertEqual(len(run.report["receipts"]), 5)

    def test_successful_requests_on_each_side_do_not_hide_an_outage(self):
        run = object.__new__(SignedUpgrade)
        run.peers = ["stream"]
        run.records = mock.Mock(
            return_value=[{"kind": "output", "thread": "stream", "turn": "turn", "pid": 123, "time": 3}]
        )
        run.report = {
            "samples": [
                {"kind": "inventory", "phase": "baseline", "start": 0.9, "end": 1, "outcome": "success"},
                {"kind": "inventory", "phase": "upgrade", "start": 2.1, "end": 6.5, "outcome": "rejected"},
                {"kind": "inventory", "phase": "candidate", "start": 6.8, "end": 7, "outcome": "success"},
            ],
            "phase_bounds": {"upgrade": (2, 6.7)},
            "violations": [],
        }
        run.summarize()
        self.assertEqual(run.report["results"]["upgrade"]["inventory"]["maximum_seconds_without_success"], 6)
        self.assertTrue(any(v["kind"] == "inventory" for v in run.report["violations"]))


class GuardedAuthorizationTests(unittest.TestCase):
    def setUp(self):
        import validate_signed_runtime_responsiveness as gate

        self.gate = gate
        resources = ExitStack()
        self.addCleanup(resources.close)
        root = Path(resources.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.run_fixture = run = object.__new__(SignedUpgrade)
        run.database = root / "db.sqlite3"
        self.directory = gate.runtime.runtime_directory(run.database)
        self.old = str(self.directory / "payloads" / "old")
        self.new = str(self.directory / "payloads" / "new")
        self.source = {"payload": self.old, "instance": "source-owner"}
        self.target = {"payload": self.new, "instance": "candidate-owner"}
        run.old = (root / "old", {"payload_digest": "old"})
        run.new = (root / "new", {"payload_digest": "new"})
        run.report = {"initial_runtime": {"payload": "old", "instance": "source-owner"}}
        run.launch_signed = mock.Mock(
            return_value={**self.source, "pending": self.new, "pending_confirmation": True}
        )
        run.publish_phase = mock.Mock()
        run.status = mock.Mock(side_effect=AssertionError("Unnecessary full-status scan"))
        run.identity = mock.Mock(return_value=self.target)
        resources.enter_context(mock.patch.object(gate, "fetch", return_value={"token": "fixture"}))
        resources.enter_context(mock.patch.object(gate.time, "sleep"))
        # Move the monotonic deadline forward deterministically.
        ticks = iter(range(1000))
        resources.enter_context(mock.patch.object(gate.time, "monotonic", side_effect=lambda: next(ticks)))

    def write_receipt(self, request, phase, **fields):
        receipt = {
            "id": request,
            "phase": phase,
            "source_instance": self.source["instance"],
            "previous": self.old,
            "target": self.new,
            "database": str(self.run_fixture.database),
            **fields,
        }
        (self.directory / "update.json").write_text(json.dumps(receipt))
        return receipt

    def test_capture_retains_observed_drain_timing_after_the_final_receipt(self):
        run = self.run_fixture
        run.report["activation_request_id"] = "request"
        self.write_receipt("request", "accepted", updated_at=100)
        run.capture_update()
        self.write_receipt("request", "draining", updated_at=166, admission_drain_started_at=166)
        run.capture_update()
        run.capture_update()
        self.write_receipt("request", "accepted", updated_at=167)
        run.capture_update()
        self.write_receipt("request", "draining", updated_at=168, admission_drain_started_at=168)
        run.capture_update()
        self.write_receipt("request", "committed", updated_at=170, admission_drain_seconds=0.5)
        run.capture_update()
        observations = run.report["guarded_control_observations"]
        self.assertEqual([row["phase"] for row in observations],
                         ["accepted", "draining", "accepted", "draining", "committed"])
        self.assertEqual(observations[1]["admission_drain_started_at"] - observations[0]["updated_at"], 66)
        self.assertEqual(run.report["guarded_control"]["phase"], "committed")

    def test_guarded_attach_authorizes_without_reading_full_status_on_source(self):
        run = self.run_fixture

        def control(record, action, **body):
            if record == self.source:
                self.assertEqual(action, "activate-update")
                self.receipt = self.write_receipt(body["request_id"], "committed")
                return {**record, "update": self.receipt}
            self.assertEqual(record, self.target)
            self.assertEqual(action, "status")
            return {**record, "status": "serving", "url": "http://127.0.0.1:1234/", "update": self.receipt}

        with mock.patch.object(
            self.gate.runtime, "discover", side_effect=[self.source, self.target]
        ), mock.patch.object(self.gate.runtime, "control", side_effect=control) as controls:
            run.swap_signed()
        self.assertEqual(controls.call_count, 2)
        self.assertEqual(run.report["guarded_update_phase"], "committed")
        self.assertEqual(run.report["guarded_control"]["id"], run.report["activation_request_id"])

    def test_changed_source_instance_is_never_authorized(self):
        run = self.run_fixture
        with mock.patch.object(
            self.gate.runtime, "discover", return_value={**self.source, "instance": "other"}
        ), mock.patch.object(self.gate.runtime, "control") as controls:
            with self.assertRaisesRegex(RuntimeError, "changed before authorization"):
                run.swap_signed()
        controls.assert_not_called()

    def test_terminal_receipt_survives_lost_authorization_ack_and_candidate_status_timeout(self):
        run = self.run_fixture

        def control(record, action, **body):
            if action == "activate-update":
                self.write_receipt(body["request_id"], "committed")
            raise TimeoutError(action)

        with mock.patch.object(
            self.gate.runtime,
            "discover",
            side_effect=lambda *a, **k: self.target
            if (self.directory / "update.json").is_file()
            else self.source,
        ), mock.patch.object(self.gate.runtime, "control", side_effect=control):
            with self.assertRaisesRegex(AssertionError, "Could not verify"):
                run.swap_signed()
        self.assertEqual(run.report["guarded_control"]["phase"], "committed")
        self.assertNotIn("candidate_runtime", run.report)
        self.assertEqual(
            {e["operation"] for e in run.report["guarded_control_observation_errors"]},
            {"activate-update", "status"},
        )
