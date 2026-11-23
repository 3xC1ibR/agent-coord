"""Release gates must catch uncertain acknowledgments and silence across phase boundaries."""
from pathlib import Path
import json
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from validate_signed_runtime_responsiveness import SignedUpgrade


class SignedAcceptanceGateTests(unittest.TestCase):
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
