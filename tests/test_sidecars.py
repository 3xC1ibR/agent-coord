from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from test_codex_app_server import BrowserSessions, CoordinationStore, FakeCodex, PLUGIN_SCRIPTS
from agent_coord.app_control import AppControl, AppControlWorker
from agent_coord.sidecars import Sidecars, SidecarRuntime
from agent_coord.store import CoordinationError
from agent_coord.ui import make_ui_server


class SidecarTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.now = 1700000000.0
        self.store = CoordinationStore(self.root / "state.sqlite3", clock=lambda: self.now)
        self.sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(self.sessions.close)
        self.worker = AppControlWorker(self.sessions)
        self.addCleanup(self.worker.close)
        self.control = self.worker.control
        self.target = self.sessions.create({"name": "Checkout", "cwd": str(self.root)})["session"]["thread_id"]
        self.store.threads.capture_request(self.target, "Implement checkout and pass the payment tests.")
        self.store.register(session_id="owner", client="codex", cwd=str(self.root))
        self.runtime = self.sessions.sidecars
        self.watches = self.runtime.watches

    def attach(self, **settings):
        payload = dict(target_id=self.target, purpose="Verify checkout acceptance criteria", **settings)
        receipt = self.control.request("sidecar", "owner", payload)
        self.worker.process_once()
        receipt = self.control.status(receipt["request_id"])
        self.assertEqual(receipt["status"], "completed", receipt)
        return receipt["result"]

    def review(self, watch):
        self.runtime.process_once()
        return self.watches.get(watch["id"])["review_id"]

    def report(self, watch, review, **values):
        return self.watches.report(watch["id"], review, watch["sidecar_id"],
                                   finding=values.pop("finding", "Tests are missing."), **values)

    def finish_target(self, text="Implementation finished"):
        self.store.threads.start_turn(self.target, turn_id="target-" + str(self.now))
        turn_id = self.store.threads.get(self.target)["turn_key"]
        turn = {"id": turn_id, "status": "completed", "items": [{"id": "answer", "type": "agentMessage", "text": text}]}
        with self.sessions.lock:
            self.sessions._history(self.target)["turns"].append(copy.deepcopy(turn))
        self.store.threads.finish_turn(self.target, turn_id=turn_id, status="completed")
        self.store.touch(self.target, turn_active=False)
        self.now += 11
        return turn_id

    def test_creation_is_dedicated_no_permissions_inheritance_and_no_target_message(self):
        self.sessions.update(self.target, {"yolo": True})
        watch = self.attach(mode="message")
        self.assertNotEqual(watch["sidecar_id"], self.target)
        self.assertFalse(self.control.settings(watch["sidecar_id"])["yolo"])
        self.assertEqual(self.store.inbox(self.target, mark_delivered=False), [])
        review = self.review(watch)
        evidence = self.watches.evidence(watch["id"], review, watch["sidecar_id"])
        self.assertIn("payment tests", evidence["assignment"]["request"])
        message = self.store.inbox(watch["sidecar_id"], mark_delivered=False)[0]
        self.assertIn("untrusted evidence", message["body"])
        self.assertIn("sidecar report", message["body"])
        self.assertFalse(message["reply_required"])

    def test_standing_watch_restart_coalescing_and_idempotent_report(self):
        watch = self.attach()
        first = self.review(watch)
        self.runtime.process_once()
        self.assertEqual(self.watches.get(watch["id"])["reviews"], 1)
        self.report(watch, first, verdict="watching")
        self.report(watch, first, verdict="watching")
        with self.assertRaises(CoordinationError):
            self.report(watch, first, verdict="complete")
        self.finish_target("Some work")
        self.finish_target("More work")
        restarted = SidecarRuntime(self.sessions)
        restarted.process_once()
        current = self.watches.get(watch["id"])
        self.assertEqual(current["reviews"], 2)
        evidence = self.watches.evidence(watch["id"], current["review_id"], watch["sidecar_id"])
        self.assertEqual(len(evidence["turns"]), 2)
        self.assertNotEqual(current["review_id"], first)
        restarted.process_once()
        self.assertEqual(self.watches.get(watch["id"])["reviews"], 2)

    def test_notify_authority_and_message_budget_cannot_be_bypassed_by_send(self):
        watch = self.attach(max_interventions=1)
        review = self.review(watch)
        with self.assertRaisesRegex(CoordinationError, "cannot send"):
            self.report(watch, review, verdict="watching", message="Run tests")
        with self.assertRaisesRegex(CoordinationError, "sidecar report"):
            self.store.send_message(sender_session_id=watch["sidecar_id"], recipient_session_id=self.target,
                                    body="Run tests", reply_required=False)
        self.report(watch, review, verdict="attention")
        self.assertEqual(self.watches.get(watch["id"])["status"], "needs_attention")

    def test_completion_racing_snapshot_is_not_consumed_and_idle_does_not_read_history(self):
        watch = self.attach()
        original = self.runtime.snapshot
        def racing(row):
            snapshot = original(row)
            self.finish_target("Result arrived during evidence capture")
            return snapshot
        with patch.object(self.runtime, "snapshot", side_effect=racing):
            self.runtime.process_once()
        self.assertEqual(self.watches.get(watch["id"])["reviews"], 0)
        self.report(watch, self.review(watch), verdict="watching")
        with patch.object(self.runtime, "snapshot", side_effect=AssertionError("Idle watches must not copy history")):
            self.runtime.process_once()

    def test_http_attachment_controls_and_workspace_isolation(self):
        server = make_ui_server(self.store, host="127.0.0.1", port=0, cwd=str(self.root), browser_sessions=self.sessions)
        for component in (server.app_control, self.sessions.inbox_wake, self.sessions.sidecars):
            component.close()
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base = "http://127.0.0.1:" + str(server.server_address[1])
        def get(path):
            with urllib.request.urlopen(base + path) as response:
                return json.load(response)
        token = get("/api/browser/config")["token"]
        path = "/api/browser/threads/" + self.target + "/sidecars"
        def post(body, auth=token):
            request = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json", "X-Agent-Coord-Token": auth})
            with urllib.request.urlopen(request) as response:
                return json.load(response)
        try:
            queued = post({"purpose": "Verify checkout", "request_id": "http-watch"})
            self.assertEqual(queued["status"], "queued")
            self.assertEqual(get(path)["requests"][0]["request_id"], "http-watch")
            self.worker.process_once()
            watch = get(path)["watches"][0]
            self.assertEqual(watch["target_id"], self.target)
            self.assertEqual(post({"id": watch["id"], "action": "pause"})["state"], "paused")
            self.assertEqual(post({"id": watch["id"], "action": "detach"})["state"], "detached")
            with self.assertRaises(urllib.error.HTTPError) as rejected:
                post({"purpose": "Bad token"}, auth="invalid")
            self.assertEqual(rejected.exception.code, 403)
            rejected.exception.close()
            self.store.register(session_id="outside", client="codex", cwd=str(self.root.parent))
            with self.assertRaises(urllib.error.HTTPError) as outside:
                get("/api/browser/threads/outside/sidecars")
            outside.exception.close()
            for asset in ("sidecars.js", "sidecars.css"):
                with urllib.request.urlopen(base + "/" + asset) as response:
                    self.assertEqual(response.status, 200)
        finally:
            server.shutdown(); server.server_close(); worker.join(5)

    def test_corrective_message_is_once_and_budget_is_durable(self):
        watch = self.attach(mode="message", max_interventions=1)
        review = self.review(watch)
        self.report(watch, review, verdict="watching", message="Run tests")
        self.report(watch, review, verdict="watching", message="Run tests")
        self.assertEqual(len(self.store.inbox(self.target, mark_delivered=False)), 1)
        self.finish_target()
        review = self.review(watch)
        with self.assertRaisesRegex(CoordinationError, "cannot send"):
            self.report(watch, review, verdict="watching", message="Try again")
        self.assertEqual(self.watches.get(watch["id"])["interventions"], 1)

    def test_corrective_message_wakes_target_and_arrives_in_the_new_turn(self):
        watch = self.attach(mode="message")
        self.report(watch, self.review(watch), verdict="watching", message="Run the payment tests")
        self.sessions.inbox_wake.process_once()
        self.assertIn(self.target, self.sessions.active)
        self.sessions.persistence.flush()
        received = self.store.inbox(self.target)
        self.assertEqual(len(received), 1)
        self.assertIn("Run the payment tests", received[0]["body"])

    def test_queued_intervention_is_discarded_after_new_user_task_or_detach(self):
        for change in ("new-task", "detach", "stop"):
            with self.subTest(change=change):
                watch = self.attach(mode="message")
                self.report(watch, self.review(watch), verdict="watching", message="Old correction")
                if change == "new-task":
                    self.store.threads.start_turn(self.target, turn_id="new-user-task")
                elif change == "stop":
                    self.sessions.inbox_wake.pause(self.target, "Stopped by user", kind="stop")
                else:
                    self.watches.change(watch["id"], "detach", caller="owner")
                self.assertEqual(self.store.inbox(self.target, mark_delivered=False), [])
                self.sessions.inbox_wake.resume(self.target)
                self.store.touch(self.target, turn_active=False)

    def test_retry_existing_attachment_after_it_starts_is_idempotent(self):
        peer = self.sessions.create({"name": "Reviewer", "cwd": str(self.root)})["session"]["thread_id"]
        payload = dict(target_id=self.target, purpose="Verify checkout", existing_id=peer)
        request = self.control.request("sidecar", "owner", payload, request_id="retry-existing")
        self.worker.process_once()
        self.store.touch(peer, turn_active=True)
        again = self.control.request("sidecar", "owner", payload, request_id="retry-existing")
        self.assertEqual(again["status"], "completed")
        self.assertEqual(len(self.watches.list(self.target)), 1)
        with self.assertRaises(CoordinationError):
            self.control.request("sidecar", "owner", dict(payload, purpose="Another purpose"), request_id=request["request_id"])

    def test_stale_evidence_and_stop_prevent_correction(self):
        watch = self.attach(mode="message")
        review = self.review(watch)
        self.finish_target("New evidence")
        with self.assertRaisesRegex(CoordinationError, "activity changed"):
            self.report(watch, review, verdict="watching", message="Do old work")
        self.report(watch, review, verdict="watching")
        review = self.review(watch)
        self.sessions.inbox_wake.pause(self.target, "Stopped by user", kind="stop")
        with self.assertRaisesRegex(CoordinationError, "activity changed"):
            self.report(watch, review, verdict="watching", message="Continue")
        self.assertEqual(self.watches.get(watch["id"])["status"], "paused")

    def test_pause_detach_invalidate_running_review_and_suppress_pending_wake(self):
        for action in ("pause", "detach"):
            with self.subTest(action=action):
                watch = self.attach(mode="message")
                review = self.review(watch)
                self.watches.change(watch["id"], action, caller="owner")
                with self.assertRaisesRegex(CoordinationError, "no longer active"):
                    self.report(watch, review, verdict="watching", message="Continue")
                self.assertEqual(self.store.inbox(watch["sidecar_id"], mark_delivered=False), [])
                self.sessions.inbox_wake.process_once()
                self.assertNotIn(watch["sidecar_id"], self.sessions.active)

    def test_complete_ends_assignment_continuous_keeps_watching(self):
        for continuous in (False, True):
            watch = self.attach(continuous=continuous)
            review = self.review(watch)
            self.report(watch, review, verdict="complete", finding="All acceptance checks passed.")
            self.finish_target()
            self.runtime.process_once()
            current = self.watches.get(watch["id"])
            self.assertEqual(current["reviews"], 2 if continuous else 1)
            self.assertEqual(current["state"], "active" if continuous else "complete")

    def test_review_budget_and_missing_report_require_attention(self):
        watch = self.attach(max_reviews=1)
        self.report(watch, self.review(watch), verdict="watching")
        self.finish_target()
        self.runtime.process_once()
        self.assertEqual(self.watches.get(watch["id"])["state"], "attention")
        with self.assertRaisesRegex(CoordinationError, "budget"):
            self.watches.change(watch["id"], "resume", caller="owner")
        other = self.attach()
        review = self.review(other)
        self.store.threads.start_turn(other["sidecar_id"], turn_id="review-turn")
        self.store.touch(other["sidecar_id"], turn_active=True)
        self.store.inbox(other["sidecar_id"])
        self.store.threads.finish_turn(other["sidecar_id"], turn_id="review-turn")
        self.runtime.process_once()
        self.assertIn("without a report", self.watches.get(other["id"])["finding"])

    def test_existing_agent_roles_access_and_evidence_bounds(self):
        peer = self.sessions.create({"name": "Reviewer", "cwd": str(self.root)})["session"]["thread_id"]
        watch = self.attach(existing_id=peer)
        self.assertEqual(watch["sidecar_id"], peer)
        with self.assertRaises(CoordinationError):
            self.attach(existing_id=peer)
        with self.assertRaises(CoordinationError):
            self.control.request("sidecar", peer, dict(target_id=self.target, purpose="Recursive watch"))
        self.finish_target("x" * 100000)
        review = self.review(watch)
        with self.assertRaises(CoordinationError):
            self.watches.evidence(watch["id"], review, "owner")
        evidence = self.watches.evidence(watch["id"], review, peer)
        self.assertTrue(evidence["truncated"])
        self.assertLess(len(json.dumps(evidence)), 32000)
        with self.assertRaises(CoordinationError):
            self.watches.change(watch["id"], "detach", caller=peer)

    def test_cli_attach_and_evidence_share_runtime_and_identity(self):
        def cli(*args, caller="owner"):
            completed = subprocess.run([str(PLUGIN_SCRIPTS / "agent-coord"), "--db", str(self.store.database_path), "sidecar", *args],
                cwd=self.root, env=dict(os.environ, AGENT_COORD_SESSION_ID=caller), capture_output=True, text=True, timeout=10)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            return json.loads(completed.stdout)
        queued = cli("attach", "--target", self.target, "--purpose", "Verify checkout", "--request-id", "cli-attachment")
        self.assertEqual(queued["status"], "queued")
        self.worker.process_once()
        watch = self.control.status(queued["request_id"])["result"]
        review = self.review(watch)
        evidence = cli("evidence", watch["id"], "--review", str(review), caller=watch["sidecar_id"])
        self.assertEqual(evidence["target_id"], self.target)
        cli("report", watch["id"], "--review", str(review), "--verdict", "complete", "--finding", "Verified tests", caller=watch["sidecar_id"])
        self.assertEqual(cli("show", watch["id"])["state"], "complete")


if __name__ == "__main__":
    unittest.main()
