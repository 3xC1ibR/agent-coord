"""Replay ownership across passive staging, source replacement, and an explicit stop."""
from contextlib import ExitStack, closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))
from agent_coord import runtime_interaction_gateway as gateway


class SourceAuthorityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.database = self.root / "coordination.sqlite3"
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("CREATE TABLE browser_submission_receipts(thread_id,request_id,state,payload_hash)")
        self.target = str(self.root / "signed-candidate")
        self.source = {"instance": "source", "database": str(self.database), "url": "http://127.0.0.1:1/",
                       "payload": str(self.root / "signed-installed"), "control_token": "test-token"}
        self.record = self.source.copy()
        self.status = dict(self.record, status="serving", paused=False, pending=self.target, pending_confirmation=True)
        self.exchanges = []
        self.body = {"requestId": "original", "message": "accepted input"}
        resources = ExitStack()
        self.addCleanup(resources.close)
        resources.enter_context(patch.object(gateway.runtime, "discover", side_effect=lambda *_args, **_kw: self.record.copy()))
        resources.enter_context(patch.object(gateway.runtime, "control", side_effect=lambda *_args, **_kw: self.status.copy()))
        resources.enter_context(patch.object(gateway, "_exchange", side_effect=self.exchange))
        views = SimpleNamespace(authorize_composer=lambda *_: True, close=lambda: None)
        resources.enter_context(patch.object(gateway, "SavedRuntimeViews", return_value=views))
        self.g = self.restart()
        self.g.connect_backend()
        self.publish_discovery()

    def publish_discovery(self):
        gateway.runtime.atomic_json(self.g.runtime_directory / "discovery.json", self.record)

    def restart(self):
        instance = gateway.InteractionGateway(self.database, self.root / "gateway", self.target)
        self.addCleanup(instance.delivery_pool.shutdown, wait=True)
        return instance

    def exchange(self, _address, method, path, *args, **kwargs):
        if method == "GET":
            config = {"token": "browser", "remote": False,
                      "ordinaryTransferReceipts": self.record["payload"] == self.target}
            return 200, {}, json.dumps(config)
        self.exchanges.append((method,path))
        body = json.loads(args[0])
        semantic = {k:v for k,v in body.items() if k not in {"requestId","expectedTurnId","activity","windowId"}}
        digest = hashlib.sha256(json.dumps({"body":semantic,"queued":False}, sort_keys=True,
                                           separators=(",", ":")).encode()).hexdigest()
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("INSERT INTO browser_submission_receipts VALUES(?,?,'completed',?)",
                       ("thread",body["requestId"],digest))
            db.commit()
        return 200, {}, b"{}"

    def queued(self, owner=None):
        return (owner or self.g).intake.get("thread", "original")["state"]

    def accept(self):
        self.g.accept("thread", self.body, staged=True)

    def terminal(self):
        fence = {"request_id":"operation", "target":self.target, "source_instance":self.source["instance"],
                 "previous":self.source["payload"], "state":"prepared"}
        with self.g.condition:
            self.g._save_fence(fence)
        update = {"id":"operation", "phase":"committed", "database":str(self.database),
                  "previous":self.source["payload"], "target":self.target, "source_instance":"source"}
        self.g.runtime_directory.mkdir(exist_ok=True)
        gateway.runtime.atomic_json(self.g.runtime_directory / "update.json",update)
        self.record = dict(self.source, instance="terminal", payload=self.target, url="http://127.0.0.1:2/")
        self.status = dict(self.record, status="serving", paused=False, update=update)
        self.publish_discovery()

    def test_staged_restart_rejects_new_owner_new_payload_or_replaced_database_before_forward(self):
        self.accept()
        for change in ({"instance":"replacement"}, {"payload":self.target}, {"url":"http://127.0.0.1:2/"}):
            with self.subTest(change=change):
                self.record = {**self.source, **change}
                self.status.update(self.record)
                restarted = self.restart()
                with self.assertRaises(gateway.CoordinationError):
                    restarted.connect_backend()
                with self.assertRaises(gateway.CoordinationError):
                    self.g.drain_staged_source()
                self.assertEqual(self.queued(restarted), "queued")
                self.assertFalse(self.exchanges)
        self.record = self.source.copy()
        self.status.update(self.record)
        self.database.rename(self.root / "original.sqlite3")
        self.database.touch()
        restarted = self.restart()
        with self.assertRaises(gateway.CoordinationError):
            restarted.connect_backend()
        with self.assertRaises(gateway.CoordinationError):
            self.g.drain_staged_source()
        self.assertEqual(self.queued(), "queued")
        self.assertFalse(self.exchanges)

    def test_staged_admission_keeps_existing_receipt_and_refuses_to_accept_for_a_replacement(self):
        self.accept()
        self.record["instance"] = "new-owner"
        self.publish_discovery()
        with self.assertRaises(gateway.CoordinationError):
            self.g.accept("thread", dict(self.body, requestId="next"), staged=True)
        self.assertEqual(self.queued(), "queued")
        self.assertIsNone(self.g.intake.get("thread", "next"))
        self.assertFalse(self.exchanges)

    def test_rejection_of_authenticated_discovery_origin_without_receipt_claim(self):
        self.accept()
        self.status["url"] = "http://127.0.0.1:9/"
        with self.assertRaises(gateway.CoordinationError):
            self.restart().connect_backend()
        self.assertEqual(self.queued(), "queued")
        self.assertFalse(self.exchanges)

    def test_relay_never_sends_queries_or_credentials_to_a_replaced_coordinator(self):
        handler = object.__new__(gateway.GatewayHandler)
        handler.server = SimpleNamespace(gateway=self.g)
        handler.path = "/api/browser/provider-usage"
        handler.headers = {}
        self.record["instance"] = "replacement-at-the-old-url"
        self.publish_discovery()
        for method in ("GET", "POST"):
            with self.subTest(method=method), patch.object(gateway, "_exchange") as exchange:
                with self.assertRaises(gateway.CoordinationError):
                    handler.relay(method)
                exchange.assert_not_called()

    def test_terminal_must_authenticate_original_transition_and_pins_subsequent_restart(self):
        self.accept()
        self.terminal()
        self.status["update"]["source_instance"] = "unrelated"
        with self.assertRaises(RuntimeError):
            self.g.connect_backend(terminal=True)
        self.status["update"]["source_instance"] = "source"
        self.g.connect_backend(terminal=True)
        with self.g.condition:
            self.g._save_fence({**self.g.fence, "state":"draining"})
        restarted = self.restart()
        restarted.connect_backend(terminal=True)
        self.assertEqual(restarted.receiver.instance, "terminal")
        self.record["instance"] = self.status["instance"] = "another-terminal"
        with self.assertRaises(gateway.CoordinationError):
            self.restart().connect_backend(terminal=True)
        self.assertFalse(self.exchanges)

    def test_stopping_source_before_proof_leaves_queued_work_for_authorized_successor(self):
        self.accept()
        self.status["paused"] = True
        with self.assertRaisesRegex(gateway.CoordinationError, "paused"):
            self.g.drain_staged_source()
        self.assertEqual(self.queued(), "queued")
        self.assertFalse(self.exchanges)
        self.terminal()
        self.g.connect_backend(terminal=True)
        with self.g.condition:
            self.g._save_fence({**self.g.fence, "state":"draining"})
        self.g.intake.drain_to("thread", self.g.receiver, preflight=self.g._verify_delivery)
        self.assertEqual(self.queued(), "committed")
        self.assertIsNone(self.g.intake.drain_to("thread", self.g.receiver, preflight=self.g._verify_delivery))
        self.assertEqual(self.exchanges, [("POST","/api/browser/sessions/thread/update-input")])

    def test_stop_on_either_side_preserves_queued_work_without_a_destination_attempt(self):
        self.accept()
        self.terminal()
        self.g.connect_backend(terminal=True)
        with self.g.condition:
            self.g._save_fence({**self.g.fence, "state":"draining"})
        for identity in ("source", "terminal"):
            with self.subTest(identity=identity):
                gateway.runtime.atomic_json(self.g.runtime_directory / "stop-proof.json",
                                            {"instance":identity, "database":str(self.database)})
                with self.assertRaisesRegex(gateway.CoordinationError,"stopped"):
                    self.g.accept("thread",dict(self.body,requestId="second"))
                with self.assertRaisesRegex(gateway.CoordinationError,"stopped"):
                    self.g.intake.drain_to("thread",self.g.receiver,preflight=self.g._verify_delivery)
                self.assertEqual(self.queued(),"queued")
                self.assertFalse(self.exchanges)
        # An unrelated old stop proof does not cancel a new authorized transition.
        gateway.runtime.atomic_json(self.g.runtime_directory / "stop-proof.json", {"instance":"unrelated-old-run"})
        self.g.accept("thread", dict(self.body,requestId="second"))
        self.assertEqual(len(self.g.intake.outstanding("thread")), 2)

    def test_stop_after_commit_before_first_terminal_observation_blocks_admission(self):
        self.accept()
        self.terminal()
        gateway.runtime.atomic_json(self.g.runtime_directory / "stop-proof.json",
                                    {"instance": "exited-candidate", "database": str(self.database)})
        with self.assertRaisesRegex(gateway.CoordinationError, "stopped"):
            self.g.accept("thread", dict(self.body, requestId="second"))
        with self.assertRaisesRegex(gateway.CoordinationError, "stopped"):
            self.g.connect_backend(terminal=True)
        self.assertIsNone(self.g.intake.get("thread", "second"))
        self.assertFalse(self.exchanges)

    def test_stop_after_preflight_and_before_transport_restores_unattempted_request(self):
        self.accept()
        def stop_after_check(thread, body):
            self.g._verify_delivery(thread,body)
            self.g.runtime_directory.mkdir(exist_ok=True)
            gateway.runtime.atomic_json(self.g.runtime_directory / "stop-proof.json", {"instance":"source"})
        result = self.g.intake.drain_one("thread",lambda *_: None,self.g.legacy_forward,preflight=stop_after_check)
        self.assertEqual(result["state"],"queued")
        self.assertFalse(self.g.intake.legacy_attempts())
        self.assertFalse(self.exchanges)

    def test_terminal_negotiates_bounded_admission_check_and_stops_before_intent_when_paused(self):
        self.accept()
        self.terminal()
        self.status["transfer_admission_probe"] = 1
        self.g.connect_backend(terminal=True)
        self.g.fence["state"] = "draining"
        def admission(record, action, **_args):
            self.assertEqual(action, "transfer-admission")
            return {**self.record, "status": "serving", "paused": True}
        with patch.object(gateway.runtime, "control", side_effect=admission):
            with self.assertRaisesRegex(gateway.CoordinationError, "paused"):
                self.g.intake.drain_batch("thread", self.g.receiver, preflight=self.g._verify_delivery,
                        completed=self.fail, stopped=lambda: False)
        self.assertEqual(self.queued(), "queued")
        self.assertFalse(self.exchanges)

    def test_legacy_receivers_still_require_full_authenticated_status(self):
        self.accept()
        def check(record, action, **_args):
            self.assertEqual(action, "status")
            return {**self.status, "paused": True}
        with patch.object(gateway.runtime, "control", side_effect=check):
            with self.assertRaisesRegex(gateway.CoordinationError, "paused"):
                self.g.drain_staged_source()
        self.assertEqual(self.queued(), "queued")

    def test_runtime_admission_probe_reads_real_gate_without_readiness_or_database_metrics(self):
        runtime = gateway.runtime.Runtime.__new__(gateway.runtime.Runtime)
        runtime.trial = False
        runtime.database = str(self.database)
        runtime.instance = "terminal"
        runtime.payload = self.root
        admission = gateway.runtime.AdmissionGate()
        runtime.server = SimpleNamespace(browser_sessions=SimpleNamespace(admission=admission), server_port=3)
        runtime.server.browser_sessions.lock = threading.RLock()
        runtime.server.browser_sessions.active = {"one":"active"}
        for target, matches in (("other",False),("active",True)):
            result = runtime.dispatch({"action":"transfer-admission", "thread_id":"one","expected_turn_id":target})
            self.assertIs(result["matches_active_turn"],matches)
        runtime.server.browser_sessions.active.clear()
        self.assertFalse(runtime.dispatch({"action":"transfer-admission", "thread_id":"one",
                                         "expected_turn_id":"active"})["matches_active_turn"])
        with patch.object(gateway.runtime.Runtime, "status", side_effect=AssertionError("full status called")):
            for paused in [False, True, False]:
                admission.pause() if paused else admission.resume()
                response = runtime.dispatch({"action":"transfer-admission"})
                self.assertIs(response["paused"], paused)
                self.assertEqual(response["status"], "serving")
                self.assertEqual(response["url"], "http://127.0.0.1:3/")
                self.assertEqual(response["instance"], "terminal")
