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
from validate_signed_runtime_responsiveness import SignedUpgrade, diagnostic_reason, match_snapshot_size


class SignedAcceptanceGateTests(unittest.TestCase):
    def test_rollback_capacity_failure_is_identifiable_without_recording_private_control_text(self):
        message = "Candidate validation failed: Insufficient free space for the update rollback snapshot."
        self.assertEqual(diagnostic_reason(message), "rollback-capacity")
        self.assertEqual(diagnostic_reason("Candidate validation failed: private error"), "candidate-validation")

    def test_terminal_service_distinguishes_existing_backlog_and_canonical_work_from_new_arrivals(self):
        run = object.__new__(SignedUpgrade)
        run.peers, run.composer = ["active"], "idle"
        with tempfile.TemporaryDirectory() as folder:
            run.database = Path(folder) / "canonical.sqlite3"
            intake = Path(folder) / "intake.sqlite3"
            with closing(sqlite3.connect(intake)) as db:
                db.execute("CREATE TABLE intake(thread_id,request_id,state,created_at)")
                db.executemany("INSERT INTO intake VALUES (?,?,?,?)",
                    [("active","previous","committed",5),("active","busy","committed",40),
                     ("active","new","queued",63),("idle","later","uncertain",70)])
                db.commit()
            with closing(sqlite3.connect(run.database)) as db:
                db.execute("CREATE TABLE browser_submission_receipts(thread_id,request_id,state,created_at,updated_at)")
                db.executemany("INSERT INTO browser_submission_receipts VALUES (?,?,?,?,?)",
                    [("active","previous","completed",6,7),("active","busy","completed",56,57),
                     ("idle","later","accepted",70,70)])
                db.commit()
            service = run.terminal_receipt_service(intake, 50, 75)
            direct = service["direct"]
            self.assertEqual(direct["intake_arrivals_since_draining"], 1)
            self.assertEqual(direct["oldest_outstanding_age_s"], 12)
            self.assertEqual(direct["canonical_completed_since_draining"], 1)
            self.assertEqual(direct["canonical_completed_last_30s"], 1)
            self.assertEqual(direct["canonical_service_ms_p95"], 1000)
            self.assertEqual(service["browser"]["intake_outstanding"], 1)
            self.assertIsNone(service["browser"]["canonical_service_ms_p95"])

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
                        "message": {"method": "turn/steer", "params": {"threadId":"stream", "input": [{"text": key}]}},
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

    def test_authenticates_each_page_at_staged_origin_without_successful_old_send(self):
        run = self.run_fixture
        path = run.root / "browser-observations.jsonl"
        with path.open("w") as file:
            def log(page, start=10, origin="http://127.0.0.1:1234", continuity=True):
                print(json.dumps(dict(page=page, start=start, status=200, phase="gateway_source", origin=origin,
                                      kind="browser_attachment", continuity=continuity)), file=file, flush=True)
            log("opening")
            log("receiving")
            log("composing", origin="http://127.0.0.1:5678")
            log("composing", start=9)
            log("composing", continuity=False)
            self.assertFalse(run.browser_attached("http://127.0.0.1:1234/", 10, "gateway_source"))
            log("composing")
            self.assertTrue(run.browser_attached("http://127.0.0.1:1234/", 10, "gateway_source"))

    def test_upgrade_tail_and_missing_echo_fail_even_with_frequent_success(self):
        run = self.run_fixture
        run.records = mock.Mock(return_value=[{"kind": "output", "thread": "stream", "turn": "a", "pid": 1, "time": 1}])
        run.report.update(phase_bounds={"upgrade": [0, 4]}, wall_phase_bounds={"upgrade": [0, 4000]})
        for start in range(4):
            run.report["samples"].append({"phase": "upgrade", "kind": "send", "start": start,
                                          "end": start + (.9 if start == 3 else .02), "outcome": "accepted"})
        run.summarize()
        self.assertLess(run.report["results"]["upgrade"]["send"]["maximum_seconds_without_success"], 5)
        self.assertIn("interaction latency budget exceeded", [v["reason"] for v in run.report["violations"] if v["kind"] == "send"])
        run.browser = mock.Mock()
        run.browser_err = mock.Mock()
        (run.root / "browser-errors.log").touch()
        rows = [{"phase": "upgrade", "kind": "browser_send", "ok": True, "start": i*1000,
                 "requested": i*1000, "responded": i*1000+12, "end": i*1000+800,
                 "own_ms": 5 if i else None, "other_ms": 620 if i==3 else 18} for i in range(4)]
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

    def test_control_timeline_records_only_matched_sanitized_state(self):
        import validate_signed_runtime_responsiveness as gate
        run = self.run_fixture
        directory = gate.runtime.runtime_directory(run.database)
        gateway = directory / "interaction-gateway"
        gateway.mkdir()
        run.report["activation_request_id"] = "mine"
        (gateway / "transition.json").write_text(json.dumps({"request_id": "mine", "state": "prepared", "token": "private"}))
        (gateway / "activation-request.json").write_text(json.dumps({"request_id": "other", "phase": "rejected"}))
        (directory / "update.json").write_text(json.dumps({"id": "mine", "phase": "failed",
            "error": "Accepted operations did not drain; private payload", "source": "private"}))
        stop = mock.Mock()
        stop.wait.return_value = True
        run.observe_control_files(stop)
        row, = run.report["transition_control_timeline"]
        self.assertEqual(row["gateway"], {"state": "prepared"})
        self.assertEqual(row["backend"], {"phase": "failed", "error": "admission-drain-timeout"})
        self.assertNotIn("activation", row)
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

    def test_settlement_keeps_observing_net_progress_and_stops_when_backlog_is_flat(self):
        intake = self.directory / "interaction-gateway/message-intake.sqlite3"
        intake.parent.mkdir(parents=True)
        run = self.run_fixture
        with closing(sqlite3.connect(intake, isolation_level=None)) as db:
            db.execute("CREATE TABLE intake(state TEXT)")
            db.executemany("INSERT INTO intake VALUES ('queued')", [(), (), ()])
            elapsed = [0]
            def advance(_):
                elapsed[0] += 10
                if elapsed[0] in (90, 180):
                    db.execute("DELETE FROM intake WHERE rowid=(SELECT MIN(rowid) FROM intake)")
            with mock.patch.object(self.gate.time, "monotonic", side_effect=lambda: elapsed[0]), \
                 mock.patch.object(self.gate.time, "sleep", side_effect=advance):
                run.gateway_control = mock.Mock(side_effect=lambda _: {"settled": elapsed[0] >= 210})
                run.wait_for_intake_settlement(600)
                self.assertEqual(elapsed[0], 210)
                run.gateway_control = mock.Mock(return_value={"settled": False})
                with self.assertRaisesRegex(AssertionError, "durable intake reconciliation"):
                    run.wait_for_intake_settlement(600)
                self.assertEqual(elapsed[0], 330)

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

    def test_qualification_observes_a_full_size_copy_through_commit(self):
        run = self.run_fixture
        elapsed = [0]
        def advance(seconds):
            elapsed[0] += 5
            if elapsed[0] == 85:
                self.receipt = self.write_receipt(run.report["activation_request_id"], "committed")
            elif elapsed[0] == 55:
                self.write_receipt(run.report["activation_request_id"], "trial")
        def control(record, action, **body):
            if action == "activate-update":
                return self.write_receipt(body["request_id"], "stopping")
            return {**record, "status": "serving", "url": "http://127.0.0.1:1234/", "update": self.receipt}
        with mock.patch.object(self.gate.time, "monotonic", side_effect=lambda: elapsed[0]), \
             mock.patch.object(self.gate.time, "sleep", side_effect=advance), \
             mock.patch.object(self.gate.runtime, "discover", side_effect=[self.source, self.target]), \
             mock.patch.object(self.gate.runtime, "control", side_effect=control):
            run.swap_signed()
        self.assertEqual(run.report["phase_bounds"]["upgrade"], [0, 85])
        self.assertEqual(run.report["guarded_update_phase"], "committed")

    def test_gateway_uses_one_signed_authorization_and_stays_routed_until_durable_settlement(self):
        run = self.run_fixture
        gateway_address = ("http://127.0.0.1:4555/", "fixture-token")
        run.report.update(phase_bounds={"baseline": [0, 1]}, wall_phase_bounds={"baseline": [0, 1]})
        run.gateway_capable = mock.Mock(return_value=True)
        def route(held, target):
            self.assertEqual(target, self.new)
            run.address = gateway_address
            run.report["interaction_gateway"] = {"url": gateway_address[0]}
        run.route_source_interactions = mock.Mock(side_effect=route)
        def authorize(resources, arguments, *, body, timeout):
            self.assertEqual(resources, run.new[0])
            self.assertEqual(arguments, ["--runtime-command", "activate-update", "--runtime-interaction-continuity"])
            self.assertEqual(body, {"payload": self.new, "request_id": run.report["activation_request_id"]})
            self.assertEqual(run.address, gateway_address)
            self.receipt = self.write_receipt(body["request_id"], "committed")
            raise TimeoutError("reply lost after activation")
        run.signed_helper = mock.Mock(side_effect=authorize)
        run.gateway_control = mock.Mock(side_effect=[{"settled": False}, {"settled": True}])
        direct = {**self.target, "status": "serving", "url": "http://127.0.0.1:1234/"}
        def direct_status(record, action, **options):
            self.assertEqual((record, action), (self.target, "status"))
            return {**direct, "update": self.receipt}
        with mock.patch.object(self.gate.runtime, "discover", side_effect=[self.source, self.target]), \
             mock.patch.object(self.gate.runtime, "control", side_effect=direct_status):
            run.swap_signed()
        run.signed_helper.assert_called_once()
        self.assertEqual(run.gateway_control.call_count, 2)
        self.assertEqual(run.address, gateway_address)
        self.assertEqual(run.candidate_direct_address[0], direct["url"])
        self.assertTrue(run.report["interaction_gateway"]["settled"])

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


class GatewayReceiptGateTests(unittest.TestCase):
    def test_every_private_intake_record_requires_one_native_receipt_and_delivery(self):
        import validate_signed_runtime_responsiveness as gate
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            run = object.__new__(SignedUpgrade)
            run.database = root / "coord.sqlite3"
            run.peers, run.composer = ["direct"], "browser"
            run.report = {"interaction_gateway": {"url": "fixture"}, "samples": [], "violations": [],
                          "browser_observations": [], "receipts": []}
            with closing(sqlite3.connect(run.database)) as db:
                db.execute("CREATE TABLE browser_submission_receipts (thread_id,request_id,payload_json,state)")
                db.execute("INSERT INTO browser_submission_receipts VALUES (?,?,?,?)",
                           ("browser", "r1", json.dumps({"body":{"message":"browser marker"}}), "completed"))
                db.commit()
            intake = gate.runtime.runtime_directory(run.database) / "interaction-gateway"
            intake.mkdir()
            with closing(sqlite3.connect(intake / "message-intake.sqlite3")) as db:
                db.execute("CREATE TABLE intake (sequence INTEGER PRIMARY KEY,thread_id,request_id,body,state,created_at)")
                db.executemany("INSERT INTO intake VALUES (?,?,?,?,?,?)", [
                    (1, "browser", "r1", json.dumps({"requestId":"r1", "message":"browser marker"}), "committed", 2),
                    (2, "direct", "r2", json.dumps({"requestId":"r2", "message":"lost marker"}), "uncertain", 4)])
                db.commit()
            run.records = lambda: [{"kind":"input","message":{"method":"turn/start","params":{
                                        "threadId":"browser", "input":[{"text":"browser marker"}]}}}]
            run.report.update(baseline_endpoint="staged_gateway", interaction_routes=[
                {"at_ms": 0, "origin": "fixture", "phase": "baseline"},
                {"at_ms": 3000, "origin": "fixture", "phase": "upgrade"},
                {"at_ms": 5000, "origin": "fixture", "phase": "gateway_candidate"},
            ])
            run.verify_receipts()
            coverage = run.report["interaction_gateway"]["receipts"]
            self.assertEqual(coverage["intake_registrations_by_phase"],
                             {"staged_source": {"browser": 1}, "handoff": {"direct": 1}})
            self.assertEqual(coverage["confirmed_client_work_by_phase"]["handoff"], {"browser": 0, "direct": 0})
            self.assertIn("missing handoff intake receipt coverage", [v["kind"] for v in run.report["violations"]])
            failures = [r for r in run.report["violations"] if r["kind"] == "gateway receipt disagreement"]
            self.assertEqual([r["request_id"] for r in failures], ["r2"])
            self.assertEqual(run.report["interaction_gateway"]["receipts"]["total"], 2)
            self.assertEqual({r["request_id"] for r in run.report["receipts"]}, {"r1", "r2"})

    def test_exact_completed_target_and_receiver_turn_are_required_for_handoff_credit(self):
        import validate_signed_runtime_responsiveness as gate
        with tempfile.TemporaryDirectory() as directory:
            run = object.__new__(SignedUpgrade)
            run.database = Path(directory).resolve() / "fixture.sqlite3"
            run.peers, run.composer = ["direct"], "browser"
            run.report = {"baseline_endpoint":"staged_gateway", "interaction_routes":[
                {"at_ms":0, "origin":"fixture", "phase":"upgrade"}],
                "interaction_gateway":{"url":"fixture"}, "samples":[
                    {"request_id":"steer", "outcome":"accepted", "phase":"upgrade", "kind":"send", "origin":"fixture"}],
                "browser_observations":[{"kind":"browser_send", "request_id":"send", "marker":"new turn",
                    "status":200, "post_origin":"fixture", "phase":"upgrade"}], "receipts":[], "violations":[]}
            native = [("direct","steer",json.dumps({"body":{"message":"steer"}}),"completed","{}"),
                      ("browser","send",json.dumps({"body":{"message":"new turn"}}),"completed",
                       json.dumps({"turn":{"id":"new"}}))]
            with closing(sqlite3.connect(run.database)) as db:
                db.execute("CREATE TABLE browser_submission_receipts(thread_id,request_id,payload_json,state,result_json)")
                db.executemany("INSERT INTO browser_submission_receipts VALUES(?,?,?,?,?)", native)
                db.commit()
            intake = gate.runtime.runtime_directory(run.database) / "interaction-gateway"
            intake.mkdir()
            with closing(sqlite3.connect(intake / "message-intake.sqlite3")) as db:
                db.execute("CREATE TABLE intake(sequence INTEGER PRIMARY KEY,thread_id,request_id,body,state,created_at)")
                db.execute("INSERT INTO intake VALUES(1,'browser','send',?,'committed',1)", (json.dumps(
                    {"requestId":"send","message":"new turn","expectedTurnId":"previous"}),))
                db.commit()
            wire = [{"kind":"turn", "thread":"browser", "turn":"previous", "role":"first"},
                    {"kind":"complete", "thread":"browser", "turn":"previous"},
                    {"kind":"input","message":{"method":"turn/start","params":{
                        "threadId":"browser","input":[{"text":"new turn"}]}}},
                    {"kind":"turn", "thread":"browser", "turn":"new", "role":"new turn"},
                    {"kind":"input","message":{"method":"turn/steer","params":{
                        "threadId":"direct", "expectedTurnId":"stream", "input":[{"text":"steer"}]}}}]
            run.records = lambda: wire
            run.verify_receipts()
            self.assertEqual(run.report["interaction_gateway"]["receipts"]["confirmed_client_work_by_phase"]["handoff"],
                             {"browser":1,"direct":1})
            self.assertFalse(any(v["kind"] == "gateway receipt disagreement" for v in run.report["violations"]))
            for fault in ("newer_turn","missing_completion","wrong_result"):
                with self.subTest(fault=fault):
                    changed = list(wire)
                    if fault == "newer_turn":
                        changed.insert(2, {"kind":"turn","thread":"browser","turn":"newer","role":"unrelated"})
                    elif fault == "missing_completion":
                        changed.pop(1)
                    else:
                        changed[3] = dict(wire[3], turn="unrelated")
                    run.records = lambda: changed
                    run.report.update(violations=[], receipts=[])
                    run.verify_receipts()
                    self.assertTrue(any(v["kind"] == "gateway receipt disagreement" and v["request_id"] == "send"
                                        for v in run.report["violations"]))
                    self.assertEqual(run.report["interaction_gateway"]["receipts"]["confirmed_client_work_by_phase"]
                                     ["handoff"], {"browser":0,"direct":1})

    def test_capability_is_selected_from_signed_candidate_and_legacy_commands_are_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = object.__new__(SignedUpgrade)
            run.new = (root, {})
            run.signed_helper = mock.Mock()
            run.launch_signed(root, hold=True)
            run.signed_helper.assert_called_with(root, ["--runtime-bootstrap", "--runtime-confirm-updates"])
            module = root / "backend/agent_coord/runtime_gateway_lifecycle.py"
            module.parent.mkdir(parents=True)
            module.touch()
            run.launch_signed(root, hold=True)
            run.signed_helper.assert_called_with(root, ["--runtime-bootstrap", "--runtime-confirm-updates",
                                                       "--runtime-interaction-continuity"])
            run.launch_signed(root)
            run.signed_helper.assert_called_with(root, ["--runtime-bootstrap"])

    def test_unretired_or_surviving_private_gateway_fails_cleanup(self):
        import validate_signed_runtime_responsiveness as gate
        with tempfile.TemporaryDirectory() as directory:
            run = object.__new__(SignedUpgrade)
            run.root = Path(directory).resolve()
            run.database = run.root / "db"
            gateway = gate.runtime.runtime_directory(run.database) / "interaction-gateway"
            gateway.mkdir()
            (gateway / "gateway.json").touch()
            run.report = {"passed": True, "violations": []}
            run.gateway_control = mock.Mock(side_effect=[{"settled": False}, RuntimeError("HTTP 409")])
            with mock.patch.object(gate.Validation, "close") as cleanup, \
                 mock.patch.object(gate, "processes_mentioning", return_value={12: "--runtime-interaction-gateway"}):
                run.close()
            self.assertEqual(run.gateway_control.call_args_list, [mock.call("identity"), mock.call("retire", body={})])
            cleanup.assert_called_once()
            self.assertFalse(run.report["passed"])
            self.assertEqual(run.report["gateway_survivors"], [12])
