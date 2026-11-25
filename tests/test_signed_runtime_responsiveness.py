"""Release gates must catch uncertain acknowledgments, silence across cutover, and edge continuity."""
from contextlib import ExitStack, closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from validate_signed_runtime_responsiveness import SignedUpgrade, diagnostic_reason, match_snapshot_size


class SignedAcceptanceGateTests(unittest.TestCase):
    def test_browser_phase_change_preserves_loading_fixture_and_blocks_other_origins(self):
        source = (Path(__file__).resolve().parents[1] / "scripts" /
                  "observe_signed_conversation_interactions.mjs").read_text()
        code = source[source.index("const context ="):source.index("const interval =")]
        probe = r"""
const assert = require('node:assert/strict');
let configuration = {url:'http://127.0.0.1:41001/', visit_origin:null};
let handler;
const context = {route:async (_pattern, callback) => {handler = callback;}};
const browser = {newContext:async () => context};
const state = () => configuration;
(async () => {
  await new Function('browser','state', 'return (async () => {' + process.argv[1] + '})()')(browser,state);
  const request = (url, method='GET') => handler({request:() => ({url:() => url,method:() => method}),
    continue:() => 'allowed',abort:() => 'blocked'});
  assert.equal(request(configuration.url), 'allowed');
  assert.equal(request('http://127.0.0.1:41001/config'), 'allowed');
  assert.equal(request('http://127.0.0.1:41999/config'), 'blocked');
  assert.equal(request('https://example.invalid/'), 'blocked');
})().catch(error => {console.error(error);process.exitCode=1;});
"""
        result = subprocess.run(["node", "-e", probe, code], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_offline_snapshot_readers_close_before_the_upgrade(self):
        import validate_signed_runtime_responsiveness as gate

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "offline.sqlite3"
            with closing(sqlite3.connect(snapshot)) as db:
                db.executescript("""
                    CREATE TABLE work_threads(thread_id, attention);
                    CREATE TABLE sessions(session_id, client);
                    INSERT INTO sessions VALUES (1, 'codex');
                    WITH RECURSIVE n(i) AS (VALUES (1) UNION ALL SELECT i+1 FROM n WHERE i<650)
                    INSERT INTO work_threads SELECT 1, 'now' FROM n;
                """)
            connect = sqlite3.connect
            snapshot_readers = []

            def record_connection(path, *args, **kwargs):
                connection = connect(path, *args, **kwargs)
                if path == snapshot.resolve().as_uri() + "?mode=ro":
                    snapshot_readers.append(connection)
                return connection

            with mock.patch.object(gate.sqlite3, "connect", side_effect=record_connection):
                self.assertEqual(gate.saved_inventory(snapshot), [("codex", "now", 650)])
                with self.assertRaisesRegex(sqlite3.OperationalError, "no such table"):
                    gate.copy_saved_histories(snapshot, root / "fixture.sqlite3", root)
            self.assertEqual(len(snapshot_readers), 2)
            for index, connection in enumerate(snapshot_readers):
                with self.subTest(reader=index), self.assertRaisesRegex(sqlite3.ProgrammingError, "closed"):
                    connection.execute("SELECT 1")

    def test_rollback_capacity_failure_is_identifiable_without_recording_private_control_text(self):
        message = "Candidate validation failed: Insufficient free space for the update rollback snapshot."
        self.assertEqual(diagnostic_reason(message), "rollback-capacity")
        self.assertEqual(diagnostic_reason("Candidate validation failed: private error"), "candidate-validation")

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
            with closing(sqlite3.connect(run.database)) as db:
                db.execute("CREATE TABLE browser_submission_receipts (thread_id,request_id,payload_json,state)")
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
                    ("browser", "browser-receipt", json.dumps({"body": {"message": "browser-lost"}}), "completed"),
                )
                db.commit()
            run.records = mock.Mock(
                return_value=[
                    {"kind": "input", "message": {"method": "turn/steer", "params": {"threadId": "stream", "input": [{"text": key}]}}}
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


class TransitionObservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        run = self.run_fixture = object.__new__(SignedUpgrade)
        run.root = Path(self.temp.name)
        run.database = run.root / "private.sqlite3"
        run.peers = ["stream"]
        run.composer = "composer"
        run.report = {"samples": [], "violations": [], "receipts": []}

    def test_authenticates_each_page_at_edge_origin(self):
        run = self.run_fixture
        path = run.root / "browser-observations.jsonl"
        with path.open("w") as file:
            def log(page, start=10, origin="http://127.0.0.1:1234"):
                print(json.dumps(dict(page=page, start=start, status=200, phase="baseline", origin=origin,
                                      kind="browser_attachment")), file=file, flush=True)
            log("opening")
            log("receiving")
            log("composing", origin="http://127.0.0.1:5678")
            log("composing", start=9)
            self.assertFalse(run.browser_attached("http://127.0.0.1:1234/", 10, "baseline"))
            log("composing")
            self.assertTrue(run.browser_attached("http://127.0.0.1:1234/", 10, "baseline"))

    def test_upgrade_tail_and_missing_echo_fail_even_with_frequent_success(self):
        run = self.run_fixture
        run.records = mock.Mock(return_value=[{"kind": "output", "thread": "stream", "turn": "a", "pid": 1, "time": 1}])
        run.report.update(phase_bounds={"upgrade": [0, 4]}, wall_phase_bounds={"upgrade": [0, 4000]})
        for start in range(4):
            run.report["samples"].append({"phase": "upgrade", "kind": "send", "start": start,
                                          "end": start + (0.9 if start == 3 else 0.02), "outcome": "accepted"})
        run.summarize()
        self.assertLess(run.report["results"]["upgrade"]["send"]["maximum_seconds_without_success"], 5)
        self.assertIn("interaction latency budget exceeded", [v["reason"] for v in run.report["violations"] if v["kind"] == "send"])
        run.browser = mock.Mock()
        run.browser_err = mock.Mock()
        (run.root / "browser-errors.log").touch()
        rows = [{"phase": "upgrade", "kind": "browser_send", "ok": True, "start": i * 1000,
                 "requested": i * 1000, "responded": i * 1000 + 12, "end": i * 1000 + 800,
                 "own_ms": 5 if i else None, "other_ms": 620 if i == 3 else 18} for i in range(4)]
        (run.root / "browser-observations.jsonl").write_text("\n".join(map(json.dumps, rows)))
        run.finish_browser()
        self.assertLess(run.report["browser_results"]["upgrade"]["browser_send"]["maximum_seconds_without_success"], 5)
        self.assertEqual(run.report["browser_results"]["upgrade"]["browser_send"]["other_ms"]["p95"], 620)
        self.assertIn("other_ms", [v["kind"] for v in run.report["violations"]])
        self.assertIn("own_ms", [v["kind"] for v in run.report["violations"]])

    def test_quiescent_reconciliation_distinguishes_late_duplicate_lost_and_vanished(self):
        run = self.run_fixture
        with closing(sqlite3.connect(run.database)) as db:
            db.execute("CREATE TABLE browser_submission_receipts(thread_id,request_id,payload_json,state)")
            for key in ("late", "twice", "lost", "vanished"):
                db.execute("INSERT INTO browser_submission_receipts VALUES(?,?,?,?)",
                           ("stream", key, json.dumps({"body": {"message": key + "-private"}}), "accepted"))
                run.report["receipts"].append({"request_id": key, "http_outcome": "uncertain", "durable_states": ["accepted"]})
            db.commit()
        run.records = mock.Mock(return_value=[])
        run.observe_active_receipts("before_authorization")
        with closing(sqlite3.connect(run.database)) as db:
            db.execute("DELETE FROM browser_submission_receipts WHERE request_id='vanished'")
            db.execute("UPDATE browser_submission_receipts SET state='completed' WHERE request_id IN ('late','twice')")
            db.commit()
        run.records.return_value = [{"kind": "input", "message": {"method": "turn/steer",
            "params": {"input": [{"text": marker + "-private"}]}}} for marker in ("late", "twice", "twice")]
        run.observe_active_receipts("after_cleanup")
        self.assertEqual({v["request_id"] for v in run.report["violations"]}, {"twice", "lost", "vanished"})
        self.assertNotIn("-private", json.dumps(run.report))
        self.assertEqual({r["request_id"]: r["provider_inputs"] for r in
                          run.report["legacy_receipt_observations"]["after_cleanup"]}, {"late": 1, "twice": 2, "lost": 0})

    def test_pending_work_checks_original_approval_identity_and_exact_queue_effect(self):
        run = self.run_fixture
        run.report["pending_work"] = {"approval_thread": "approval", "queue_thread": "stream",
                                      "before": {"provider_requests": ["original"]}}
        records = [{"kind": "approval", "thread": "approval", "request": "original"},
                   {"kind": "approval_answer", "request": "original"},
                   {"kind": "turn", "thread": "stream", "role": "queued-after-stream"}]
        run.records = mock.Mock(return_value=records)
        self.assertTrue(run.pending_effects()["original_approval_answered_once"])
        self.assertTrue(run.pending_effects()["original_queue_delivered_once"])
        records[1]["request"] = "replacement"
        records.append(dict(records[2]))
        self.assertFalse(run.pending_effects()["original_approval_answered_once"])
        self.assertFalse(run.pending_effects()["original_queue_delivered_once"])
        run.pending_state = mock.Mock(return_value={"provider_requests": ["replacement"]})
        with self.assertRaisesRegex(AssertionError, "across the handover"):
            run.verify_pending_work()

    def test_control_timeline_records_only_matched_sanitized_edge_and_backend_state(self):
        import validate_signed_runtime_responsiveness as gate
        run = self.run_fixture
        directory = gate.runtime.runtime_directory(run.database)
        run.report["activation_request_id"] = "mine"
        (directory / "edge-route.json").write_text(json.dumps({
            "state": "cutover", "paused": False, "csrf_token": "private-secret"}))
        (directory / "update.json").write_text(json.dumps({
            "id": "mine", "phase": "failed",
            "error": "Accepted operations did not drain; private payload", "source": "private"}))
        stop = mock.Mock()
        stop.wait.return_value = True
        run.observe_control_files(stop)
        row, = run.report["transition_control_timeline"]
        self.assertEqual(row["edge"], {"state": "cutover", "paused": False})
        self.assertEqual(row["backend"], {"phase": "failed", "error": "admission-drain-timeout"})
        self.assertNotIn("private", json.dumps(row))


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
        self.assertEqual(run.address, ("http://127.0.0.1:1234/", "fixture"))

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
            side_effect=lambda *a, **k: self.target if (self.directory / "update.json").is_file() else self.source,
        ), mock.patch.object(self.gate.runtime, "control", side_effect=control):
            with self.assertRaisesRegex(AssertionError, "Could not verify"):
                run.swap_signed()
        self.assertEqual(run.report["guarded_control"]["phase"], "committed")
        self.assertNotIn("candidate_runtime", run.report)
        self.assertEqual(
            {e["operation"] for e in run.report["guarded_control_observation_errors"]},
            {"activate-update", "status"},
        )

    def test_surviving_edge_process_fails_cleanup(self):
        import validate_signed_runtime_responsiveness as gate
        with tempfile.TemporaryDirectory() as directory:
            run = object.__new__(SignedUpgrade)
            run.root = Path(directory).resolve()
            run.database = run.root / "db"
            run.report = {"passed": True, "violations": []}
            with mock.patch.object(gate.Validation, "close") as cleanup, \
                 mock.patch.object(gate, "processes_mentioning", return_value={12: "agent_coord.runtime_edge"}):
                run.close()
            cleanup.assert_called_once()
            self.assertFalse(run.report["passed"])
            self.assertEqual(run.report["edge_survivors"], [12])


if __name__ == "__main__":
    unittest.main()
