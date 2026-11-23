from __future__ import annotations

import copy
from argparse import Namespace
import json
import tempfile
import subprocess
from pathlib import Path
import sys
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import publish_macos_release as publisher
import release_workload_receipt as gate


class ReleaseWorkloadTests(unittest.TestCase):
    def setUp(self):
        self.run = {"id": 99, "run_attempt": 3, "head_sha": "a"*40}
        self.release = {"version": "1.2.3", "build_number": "4", "source_commit": self.run["head_sha"],
                        "run_url": f"https://github.com/{gate.REPO}/actions/runs/99",
                        "updates": {a: {"sha256": "b"*64} for a in gate.ARCHITECTURES},
                        "downloads": {a: {"sha256": "c"*64} for a in gate.ARCHITECTURES}}
        self.receipts = [
            {"context": gate.context(self.run, "1.2.3", "4", a, "b"*64, "c"*64),
             "state": "success", "creator": {"id": gate.OPERATOR_ID},
             "description": gate.DESCRIPTION, "target_url": gate.target(self.run)}
            for a in gate.ARCHITECTURES
        ]
        self.candidate = Path("/example/Ribbon Field.app")
        self.report = {
            "schema_version": 1, "workload_contract": "ribbon-field-saved-interaction-v1",
            "passed": True, "mode": "signed_guarded_upgrade", "guarded_update_phase": "committed",
            "violations": [],
            "signed_inputs": [{}, {"application": str(self.candidate), "version": "1.2.3+4",
                                    "payload_digest": "e"*64}],
            "candidate_runtime": {"payload": "e"*64, "architecture": "arm64"},
            "baseline_proof": {"version": "0.1.12+63", "red": True, "architecture": "arm64",
                               "payload_digest": gate.AFFECTED_PAYLOADS["arm64"]},
            "saved_histories": [{"fixture_id": key, "size_band": key, "source_bytes": size,
                                 "sanitized_bytes": size, "turns": 5}
                                for key, size in (("medium", 256 * 1024), ("large", 10 * 1024**2))],
            "samples": [{"phase": "candidate", "kind": "history", "outcome": "success",
                         "fixture": key} for key in ("medium", "large")],
            "browser_results": {"upgrade": {"browser_open": {"attempts": 1}, "browser_send": {"attempts": 1}}},
            "stream_identity_counts": {str(i): 1 for i in range(6)},
            "observed_stream_events": {p: {str(i): 15 for i in range(6)}
                                       for p in ("baseline", "upgrade", "candidate")},
            "workload": {"claude_streams": 2, "initial_text_bytes_per_claude": 10 * 1024**2,
                         "coverage": {"saved_histories_reopened": 2, "active_streams": 6, "large_streams": 2,
                                      "large_stream_bytes": 20 * 1024**2,
                                      "accepted_verified": 1, "browser_opens": 3, "browser_sends": 3}},
            "receipts": [{"request_id": "r", "http_outcome": "accepted", "durable_states": ["completed"],
                          "provider_deliveries": 1}],
        }

    def check_private(self, report):
        gate.verify_report(report, candidate=self.candidate, version="1.2.3", build="4",
                           architecture="arm64", digest="e"*64)

    def check_public(self, release, statuses, run=None):
        with mock.patch.object(gate, "release_run", return_value=run or self.run), \
             mock.patch.object(gate, "github", return_value=statuses), \
             mock.patch.dict("os.environ", {"GITHUB_RUN_ID": "99", "GITHUB_RUN_ATTEMPT": "3"}):
            gate.require_release_receipts(release)

    def test_matching_release_and_workload(self):
        self.check_private(self.report)
        self.check_public(self.release, self.receipts)

    def test_revision_attempt_architecture_and_rebuilt_files_are_bound(self):
        original = self.receipts[0]["context"]
        for index, value in enumerate([dict(self.run, id=100), "1.2.4", "5", "x86_64", "d"*64, "e"*64]):
            args = [self.run, "1.2.3", "4", "arm64", "b"*64, "c"*64]
            args[index] = value
            self.assertNotEqual(original, gate.context(*args))
        for field, value in [("run_attempt", 4), ("head_sha", "d"*40)]:
            with self.assertRaises(ValueError):
                self.check_public(self.release, self.receipts, dict(self.run, **{field: value}))
        for asset in ("updates", "downloads"):
            release = copy.deepcopy(self.release)
            release[asset]["arm64"]["sha256"] = "f"*64
            with self.assertRaises(ValueError):
                self.check_public(release, self.receipts)

    def test_refuses_missing_forged_or_superseded_status(self):
        with self.assertRaises(ValueError):
            self.check_public(self.release, self.receipts[:1])
        for change in ({"creator": {"id": 42}}, {"state": "failure"}, {"target_url": "other"},
                       {"description": "unverified"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.check_public(self.release, [dict(self.receipts[0], **change), *self.receipts])

    def test_private_gate_rejects_wrong_candidate_loss_duplicates_or_idle_workload(self):
        for path, value in [
            (["passed"], False), (["mode"], "candidate_standalone_diagnostic"),
            (["guarded_update_phase"], "pending"), (["candidate_runtime", "payload"], "wrong"),
            (["candidate_runtime", "architecture"], "x86_64"),
            (["baseline_proof", "payload_digest"], "a"*64), (["saved_histories"], []),
            (["saved_histories", 0, "source_bytes"], 256 * 1024 - 1),
            (["saved_histories", 1, "sanitized_bytes"], 10 * 1024**2 - 1),
            (["saved_histories", 1, "source_bytes"], 36 * 1024**2 + 1),
            (["workload", "initial_text_bytes_per_claude"], 5 * 1024**2),
            (["workload", "coverage", "large_stream_bytes"], 20 * 1024**2 - 1),
            (["observed_stream_events", "upgrade", "0"], 0), (["stream_identity_counts", "0"], 2),
            (["samples"], []), (["violations"], [{"phase": "upgrade"}]),
            (["browser_results", "upgrade", "browser_send", "attempts"], 0),
            (["workload", "coverage", "large_streams"], 1), (["workload", "coverage", "browser_sends"], True),
            (["receipts"], []), (["receipts", 0, "provider_deliveries"], 0),
            (["receipts", 0, "provider_deliveries"], 2), (["receipts", 0, "durable_states"], []),
        ]:
            with self.subTest(path=path):
                report = copy.deepcopy(self.report)
                node = report
                for key in path[:-1]:
                    node = node[key]
                node[path[-1]] = value
                with self.assertRaises(ValueError):
                    self.check_private(report)
        duplicate = copy.deepcopy(self.report)
        duplicate["receipts"] *= 2
        with self.assertRaises(ValueError):
            self.check_private(duplicate)

    def test_operator_never_submits_status_after_failed_or_incomplete_measurement(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            previous = directory / "previous.app"
            previous.mkdir()
            snapshot = directory / "offline.sqlite3"
            snapshot.touch()
            report = directory / "report.json"
            args = Namespace(run=99, architecture="arm64", previous=previous, snapshot=snapshot,
                             report=report, affected_report=None, playwright_module=None)

            for failure in ("driver failed", "incomplete report"):
                def execute(command, **kwargs):
                    if command[:3] == ["gh", "run", "download"]:
                        assets = Path(command[-1])
                        archive = assets / "Ribbon-Field-1.2.3-arm64.zip"
                        archive.write_bytes(b"test archive")
                        archive.with_suffix(".dmg").write_bytes(b"test image")
                        archive.with_suffix(".dmg.provenance.json").write_text(json.dumps({
                            "version": "1.2.3", "source_commit": self.run["head_sha"], "build_number": "4",
                            "run_url": self.release["run_url"],
                        }))
                    elif command[0] == sys.executable:
                        if failure == "driver failed":
                            raise subprocess.CalledProcessError(1, command)
                        report.write_text(json.dumps(dict(self.report, passed=False)))
                    return subprocess.CompletedProcess(command, 1 if "--is-inside-work-tree" in command else 0)

                with self.subTest(failure=failure), mock.patch.object(gate.platform, "system", return_value="Darwin"), \
                     mock.patch.object(gate, "github", return_value={"id": gate.OPERATOR_ID}) as api, \
                     mock.patch.object(gate, "release_run", return_value=self.run), \
                     mock.patch.object(gate.subprocess, "check_output", return_value=self.run["head_sha"]), \
                     mock.patch.object(gate.subprocess, "run", side_effect=execute), \
                     mock.patch.object(gate, "verify_installer_app"), \
                     self.assertRaises((ValueError, subprocess.CalledProcessError)):
                    gate.run_operator_gate(args)
                api.assert_called_once_with("user")

    def test_unmatched_signed_installer_detaches_before_refusing(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with mock.patch.object(gate, "signed_code_hash", side_effect=["old", "new"]), \
                 mock.patch.object(gate.subprocess, "run") as command, self.assertRaises(ValueError):
                gate.verify_installer_app(self.candidate, directory/"candidate.dmg", directory)
            self.assertEqual(command.call_args.args[0][:2], ["/usr/bin/hdiutil", "detach"])

    def test_publisher_does_not_touch_storage_on_missing_receipts(self):
        argv = ["publisher", "--directory", ".", "--version", "1.2.3", "--commit", "a"*40, "--publish"]
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.object(publisher, "release_files", return_value=([], self.release)) as validate, \
             mock.patch.object(gate, "require_release_receipts", side_effect=ValueError("missing")), \
             mock.patch.object(publisher, "R2") as storage, self.assertRaisesRegex(ValueError, "missing"):
            publisher.main()
        self.assertIs(validate.call_args.kwargs["require_updates"], True)
        storage.assert_not_called()
