from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))
from agent_coord.store import CoordinationError, CoordinationStore
from agent_coord.codex_app_server import BrowserBusyError
from agent_coord.desktop_runtime import AdmissionGate
from agent_coord.stoic_integration import StoicIntegration, StoicRequestError, StoicClient


class FakeExternalWork:
    def __init__(self, store):
        self.sessions = SimpleNamespace(store=store, admission=AdmissionGate())
        self.rows = {}
        self.launches = 0
        self.lose_submission_response = False

    def get(self, source, request_id):
        return copy.deepcopy(self.rows.get((source, request_id)))

    def submit(self, source, request_id, settings, prompt, name=None, *, request_text=None):
        key = source, request_id
        if key not in self.rows:
            self.launches += 1
            self.rows[key] = {"state": "running", "thread_id": "thread-" + request_id,
                              "turn_id": "turn-1", "result": None, "error": None,
                              "settings": settings, "prompt": prompt, "name": name, "request_text": request_text}
        if self.lose_submission_response:
            self.lose_submission_response = False
            raise CoordinationError("Provider response lost")
        return self.get(*key)

    def cancel(self, source, request_id):
        self.rows[source, request_id]["state"] = "cancelled"
        return self.get(source, request_id)


class FakeStoic:
    def __init__(self):
        self.runs = {"run-1": {"id": "run-1", "name": "Daily economy brief", "prompt": "Write a brief.",
                    "settings": {"client": "claude", "model": "requested-model", "effort": "xhigh",
                                 "permission_mode": "bypassPermissions", "cwd": "/workspace"}}}
        self.calls = []
        self.claims = {}
        self.results = {}
        self.receipts = {}
        self.lose_claim_response = False
        self.lose_result_response = False
        self.heartbeat_error = None
        self.cancel_requested = False
        self.heartbeat_status = "running"
        self.feed_error = None

    def request(self, method, path, body=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        if path.startswith("/api/event-feed"):
            if self.feed_error:
                raise StoicRequestError("Cursor expired", self.feed_error)
            return {"events": [], "next_cursor": "42", "has_more": False}
        if path.startswith("/api/execution/runs?"):
            offset = int(parse_qs(urlsplit(path).query).get("offset", [0])[0])
            return {"runs": [run for key, run in self.runs.items() if key not in self.claims][offset:offset + 100]}
        if method == "GET" and path.startswith("/api/execution/runs/"):
            identity = path.split("/")[-1]
            run = copy.deepcopy(self.runs[identity])
            if identity in self.claims:
                run["runner_id"] = self.claims[identity]["runner_id"]
            return run
        run_id, action = path.split("?")[0].split("/")[-2:]
        if action == "claim":
            prior = self.claims.setdefault(run_id, copy.deepcopy(body))
            if body != prior:
                raise AssertionError("Claim identity changed on retry")
            if self.lose_claim_response:
                self.lose_claim_response = False
                raise StoicRequestError("Claim response lost")
            return {"run": self.runs[run_id], "claim_token": "claim-token", "lease_expires_at": "2030-01-01T00:00:00Z"}
        if action == "heartbeat":
            if self.heartbeat_error:
                raise StoicRequestError("Ownership rejected", self.heartbeat_error)
            return {"status": "cancelled" if self.cancel_requested else self.heartbeat_status}
        if action == "result":
            previous = self.receipts.setdefault(body["receipt_id"], copy.deepcopy(body))
            if previous != body:
                raise AssertionError("Result identity or payload changed on retry")
            self.results[run_id] = copy.deepcopy(body)
            if self.lose_result_response:
                self.lose_result_response = False
                raise StoicRequestError("Result response lost")
            return {"accepted": True}
        raise AssertionError((method, path, body))


class StoicIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.now = 1800000000.0
        self.store = CoordinationStore(self.root / "state.sqlite3", clock=lambda: self.now)
        self.engine = FakeExternalWork(self.store)
        self.remote = FakeStoic()
        self.connector = self.restart()

    def restart(self):
        connector = StoicIntegration(self.engine, client_factory=lambda *_: self.remote)
        self.addCleanup(connector.close)
        return connector

    def enable(self, **settings):
        self.connector.configure({"enabled": True, "allowed_roots": ["/workspace"],
                                  "allowed_permission_modes": {"claude": ["bypassPermissions"]}, **settings})

    def launch(self):
        self.enable()
        self.connector.process_once()
        self.connector.process_once()

    def complete(self, state="succeeded"):
        row = next(iter(self.engine.rows.values()))
        row.update(state=state, result={"text": "The brief", "artifacts": []})

    def test_job_titles_use_the_local_scheduled_day(self):
        template = {**self.remote.runs["run-1"], "source": "job"}
        self.remote.runs = {
            f"run-{day}": {**template, "id": f"run-{day}", "occurrence_id": str(day),
                           "scheduled_at": f"2026-11-{day}T00:30:00Z"}
            for day in (22, 23)
        }
        try:
            with patch.dict(os.environ, {"TZ": "America/Chicago"}):
                time.tzset()
                self.launch()
        finally:
            time.tzset()
        self.assertEqual({run: row["name"] for (_, run), row in self.engine.rows.items()}, {
            "run-22": "Daily economy brief · Sat Nov 21, 2026",
            "run-23": "Daily economy brief · Sun Nov 22, 2026",
        })

    def test_unnamed_occurrence_passes_saved_instructions_for_title(self):
        run = self.remote.runs["run-1"]
        run.pop("name")
        run.update(source="job", occurrence_id="today", scheduled_at="2026-11-21T20:36:00Z")
        self.launch()
        row = next(iter(self.engine.rows.values()))
        self.assertIsNone(row["name"])
        self.assertEqual(row["request_text"], run["prompt"])
        self.assertTrue(row["prompt"].startswith("Stoic execution context"))
        self.assertTrue(row["prompt"].endswith(run["prompt"]))

    def test_disabled_by_default_and_no_network(self):
        self.connector.process_once()
        self.assertFalse(self.connector.status()["enabled"])
        self.assertEqual(self.remote.calls, [])

    def test_maintenance_defers_claims_and_pre_admission_refusal(self):
        self.enable()
        self.connector.process_once()
        self.engine.sessions.admission.pause()
        self.connector.process_once()
        self.assertEqual(self.remote.claims, {})
        self.engine.sessions.admission.resume()
        with patch.object(self.engine, "submit", side_effect=BrowserBusyError("Maintenance")):
            self.connector.process_once()
        self.assertEqual(self.engine.launches, 0)
        self.assertEqual(self.remote.results, {})
        self.connector.process_once()
        self.assertEqual(self.engine.launches, 1)

    def test_remote_terminal_receipt_never_relaunches_missing_local_work(self):
        for status in ("completed", "failed"):
            with self.subTest(status=status):
                self.remote.heartbeat_status = status
                self.launch()
                self.assertEqual(self.engine.launches, 0)
                self.assertEqual(self.connector.status()["runs"], {"published": 1})

    def test_unknown_remote_state_does_not_launch(self):
        self.remote.heartbeat_status = "pending"
        self.launch()
        self.assertEqual(self.engine.launches, 0)
        self.assertIn("execution state", self.connector.status()["last_error"])

    def test_exact_settings_and_single_launch_across_restart(self):
        self.launch()
        self.assertEqual(self.engine.launches, 1)
        self.assertEqual(next(iter(self.engine.rows.values()))["settings"], self.remote.runs["run-1"]["settings"])
        self.connector = self.restart()
        self.connector.process_once()
        self.assertEqual(self.engine.launches, 1)
        self.complete()
        self.connector.process_once()
        self.assertEqual(self.remote.results["run-1"]["result"]["text"], "The brief")
        self.assertEqual(self.connector.status()["runs"], {"published": 1})

    def test_lost_claim_response_reuses_persisted_claim_id(self):
        self.enable()
        self.connector.process_once()
        self.remote.lose_claim_response = True
        self.connector.process_once()
        self.assertEqual(self.engine.launches, 0)
        self.connector = self.restart()
        self.connector.process_once()
        claims = [x[2] for x in self.remote.calls if x[1].endswith("/claim")]
        self.assertEqual(len(claims), 2)
        self.assertEqual(claims[0], claims[1])
        self.assertEqual(self.engine.launches, 1)

    def test_lost_result_response_reuses_exact_receipt_after_restart(self):
        self.launch()
        self.complete()
        self.remote.lose_result_response = True
        self.connector.process_once()
        self.assertEqual(self.connector.status()["runs"], {"reporting": 1})
        self.connector = self.restart()
        self.connector.process_once()
        results = [x[2] for x in self.remote.calls if x[1].split("?")[0].endswith("/result")]
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0], results[1])
        self.assertEqual(self.engine.launches, 1)

    def test_lost_engine_response_does_not_duplicate_assignment(self):
        self.engine.lose_submission_response = True
        self.launch()
        self.connector.process_once()
        self.assertEqual(self.engine.launches, 1)

    def test_rejected_ownership_cannot_launch_and_cancels_existing_work(self):
        self.remote.heartbeat_error = 409
        self.launch()
        self.assertEqual(self.engine.launches, 0)
        self.assertEqual(self.connector.status()["runs"], {"conflict": 1})

    def test_revoked_ownership_stops_running_session(self):
        self.launch()
        self.remote.heartbeat_error = 409
        self.connector.process_once()
        self.assertEqual(next(iter(self.engine.rows.values()))["state"], "cancelled")

    def test_remote_cancellation_before_launch(self):
        self.remote.cancel_requested = True
        self.launch()
        self.assertEqual(self.engine.launches, 0)
        self.assertEqual(self.remote.results["run-1"]["status"], "cancelled")

    def test_disable_stops_new_claims_but_publishes_existing_result(self):
        self.launch()
        self.connector.configure({"enabled": False})
        self.complete()
        self.connector.process_once()
        self.assertEqual(self.remote.results["run-1"]["status"], "completed")

    def test_uncertain_execution_is_reported_without_relaunch(self):
        self.launch()
        self.complete("uncertain")
        self.connector.process_once()
        self.assertEqual(self.remote.results["run-1"]["status"], "uncertain")
        self.connector.process_once()
        self.assertEqual(self.engine.launches, 1)

    def test_pending_scan_recovers_with_expired_feed_cursor(self):
        self.remote.feed_error = 410
        self.launch()
        self.assertEqual(self.engine.launches, 1)

    def test_uncertain_result_can_reconcile_to_success_without_relaunch(self):
        self.launch()
        self.complete("uncertain")
        self.connector.process_once()
        uncertain_receipt = self.remote.results["run-1"]["receipt_id"]
        self.connector = self.restart()
        self.connector.process_once()
        self.complete()
        self.connector.process_once()
        self.assertEqual(self.remote.results["run-1"]["status"], "completed")
        self.assertNotEqual(self.remote.results["run-1"]["receipt_id"], uncertain_receipt)
        self.assertEqual(self.engine.launches, 1)

    def test_concurrency_limit_prevents_claiming_all_available_runs(self):
        for i in range(2, 6):
            self.remote.runs[f"run-{i}"] = {**self.remote.runs["run-1"], "id": f"run-{i}"}
        self.enable(max_concurrent_runs=1)
        self.connector.process_once()
        self.connector.process_once()
        self.assertEqual(self.engine.launches, 1)
        self.assertEqual(len(self.remote.claims), 1)

    def test_connection_cannot_change_under_outstanding_run(self):
        self.launch()
        with self.assertRaisesRegex(CoordinationError, "existing Stoic runs"):
            self.connector.configure({"base_url": "https://other.example"})

    def test_invalid_connection_configuration(self):
        for body in ({"base_url": "http://example.com"}, {"base_url": "https://user:password@example.com"},
                     {"base_url": "https://example.com/path"}, {"enabled": 1}, {"token": "secret"},
                     {"max_concurrent_runs": True}, {"max_concurrent_runs": 0}):
            with self.subTest(body=body), self.assertRaises(CoordinationError):
                self.connector.configure(body)

    def test_credential_content_never_appears_in_status_or_database(self):
        token = self.root / "token"
        token.write_text("sensitive-token-value")
        self.connector.configure({"token_file": str(token)})
        self.assertNotIn("sensitive-token-value", json.dumps(self.connector.status()))
        with self.store.reading(), self.store._connection() as db:
            dump = "\n".join(db.iterdump())
        self.assertNotIn("sensitive-token-value", dump)

    def test_missing_credential_error_excludes_file_contents(self):
        with self.assertRaisesRegex(StoicRequestError, "credential file is unavailable"):
            StoicClient("https://example.com", str(self.root / "absent")).request("GET", "/api/execution/runs")


    def test_remote_full_access_requires_local_policy(self):
        self.connector.configure({"enabled": True})
        self.connector.process_once()
        self.connector.process_once()
        self.assertEqual(self.engine.launches, 0)
        self.assertEqual(self.remote.results, {})
        self.assertEqual(self.remote.claims, {})
        self.assertIn("local Stoic execution policy", self.connector.last_error)
        self.assertEqual(self.connector.status()["runs"], {"denied": 1})
        self.assertEqual(self.connector.status()["denied_runs"][0]["run_id"], "run-1")
        self.enable()
        self.connector.process_once()
        self.assertEqual(self.engine.launches, 1)

    def test_policy_enforces_provider_permissions_models_and_resolved_paths(self):
        settings = self.remote.runs["run-1"]["settings"]
        self.enable(allowed_permission_modes={"claude": ["default"]})
        with self.assertRaisesRegex(CoordinationError, "permission mode"):
            self.connector._authorize_settings(self.connector._configuration(), settings)
        self.enable(allowed_models={"claude": ["other-model"]})
        with self.assertRaisesRegex(CoordinationError, "model"):
            self.connector._authorize_settings(self.connector._configuration(), settings)
        outside = self.root / "outside"
        allowed = self.root / "allowed"
        outside.mkdir()
        allowed.mkdir()
        (allowed / "escape").symlink_to(outside, target_is_directory=True)
        self.enable(allowed_roots=[str(allowed)], allowed_models={})
        with self.assertRaisesRegex(CoordinationError, "working directory"):
            self.connector._authorize_settings(self.connector._configuration(), {**settings, "cwd": str(allowed / "escape")})

    def test_uncertain_bindings_do_not_consume_execution_capacity(self):
        self.remote.runs["run-2"] = {**self.remote.runs["run-1"], "id": "run-2"}
        self.launch()
        self.assertEqual(self.engine.launches, 2)
        for row in self.engine.rows.values():
            row["state"] = "uncertain"
        self.connector.process_once()
        self.remote.runs["run-3"] = {**self.remote.runs["run-1"], "id": "run-3"}
        self.connector.next_scan = 0
        self.connector.process_once()
        self.connector.process_once()
        self.assertEqual(self.engine.launches, 3)
        self.assertEqual(self.connector.status()["runs"], {"uncertain": 2, "submitted": 1})

    def test_before_launch_lease_expiry_is_certain_failure(self):
        self.remote.heartbeat_status = "uncertain"
        self.launch()
        self.assertEqual(self.engine.launches, 0)
        self.assertEqual(self.remote.results["run-1"]["status"], "failed")
        self.assertIn("did not start", self.remote.results["run-1"]["error"])

    def test_disallowed_offers_do_not_use_capacity_for_allowed_work(self):
        allowed = copy.deepcopy(self.remote.runs["run-1"])
        self.remote.runs["run-1"]["settings"]["cwd"] = "/outside"
        self.remote.runs["run-2"] = {**copy.deepcopy(self.remote.runs["run-1"]), "id": "run-2"}
        self.remote.runs["run-3"] = {**allowed, "id": "run-3"}
        self.launch()
        self.assertEqual(self.engine.launches, 1)
        self.assertEqual(set(self.remote.claims), {"run-3"})

    def test_terminal_denied_offer_is_retired_without_claim(self):
        self.enable(allowed_roots=[])
        self.connector.process_once()
        self.connector.process_once()
        self.assertEqual(self.connector.status()["runs"], {"denied": 1})
        self.remote.runs["run-1"]["status"] = "cancelled"
        self.connector.process_once()
        self.assertEqual(self.connector.status()["runs"], {"published": 1})
        self.assertEqual(self.connector.status()["denied_runs"], [])
        self.assertEqual(self.remote.claims, {})

    def test_lost_claim_then_narrowed_policy_recovers_and_fails_owned_run(self):
        self.enable()
        self.connector.process_once()
        self.remote.lose_claim_response = True
        self.connector.process_once()
        self.assertEqual(self.engine.launches, 0)
        self.enable(allowed_roots=[])
        self.connector.process_once()
        self.assertEqual(self.remote.results["run-1"]["status"], "failed")
        self.assertEqual(self.engine.launches, 0)
        self.assertEqual(self.connector.status()["runs"], {"published": 1})

    def test_discovery_pages_past_denied_offers(self):
        allowed = copy.deepcopy(self.remote.runs["run-1"])
        denied = {**allowed, "settings": {**allowed["settings"], "cwd": "/outside"}}
        self.remote.runs = {f"denied-{index}": {**denied, "id": f"denied-{index}"} for index in range(100)}
        self.remote.runs["allowed"] = {**allowed, "id": "allowed"}
        self.launch()
        self.assertEqual(self.engine.launches, 0)
        self.connector.next_scan = 0
        self.connector.process_once()
        self.now += 1
        self.connector.process_once()
        self.connector.process_once()
        self.assertEqual(set(self.remote.claims), {"allowed"})

    def test_disable_defers_an_owned_run_not_yet_submitted(self):
        self.enable()
        self.connector.process_once()
        with patch.object(self.engine, "submit", side_effect=BrowserBusyError("Maintenance")):
            self.connector.process_once()
        self.connector.configure({"enabled": False})
        self.connector.process_once()
        self.assertEqual(self.engine.launches, 0)
        self.assertEqual(self.remote.results, {})

    def test_one_broken_binding_does_not_starve_other_results(self):
        self.remote.runs["run-2"] = {**self.remote.runs["run-1"], "id": "run-2"}
        self.launch()
        for row in self.engine.rows.values():
            row.update(state="succeeded", result={"text": "Done", "artifacts": []})
        original = self.engine.get
        def fail_first(source, run_id):
            if run_id == "run-1":
                raise CoordinationError("Injected binding failure")
            return original(source, run_id)
        with patch.object(self.engine, "get", side_effect=fail_first):
            self.connector.process_once()
        self.assertEqual(self.remote.results["run-2"]["status"], "completed")
        self.assertNotIn("run-1", self.remote.results)
        self.connector.process_once()
        self.assertEqual(self.remote.results["run-1"]["status"], "completed")


if __name__ == "__main__":
    unittest.main()
