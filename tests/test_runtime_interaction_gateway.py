"""End-to-end behavior of the local update listener, separate from the runtime owner."""
from contextlib import closing
import http.client
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))
from agent_coord import runtime_interaction_gateway as gateway
from agent_coord.runtime_message_intake import RuntimeMessageIntake


class GatewayTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        db = root / "fixture.sqlite3"
        with closing(sqlite3.connect(db)) as connection:
            connection.execute("CREATE TABLE coordinator_events (sequence INTEGER PRIMARY KEY)")
            connection.execute("CREATE TABLE turn_completions (id INTEGER PRIMARY KEY)")
            connection.execute("CREATE TABLE browser_submission_receipts (thread_id,request_id,state,payload_hash,created_at)")
        directory = root / "transition"
        self.g = gateway.InteractionGateway(db, directory, root / "candidate")
        self.g.config = {"workspaceRoot": None}
        self.g.control_token, self.g.token, self.g.opened_at = "control", "browser", 100
        self.g.backend = ("http://127.0.0.1:1/", "upstream", "old", str(root / "source"))
        self.g.views = self
        self.server = gateway.ThreadingHTTPServer(("127.0.0.1", 0), gateway.GatewayHandler)
        self.server.gateway = self.g
        self.g.url = f"http://127.0.0.1:{self.server.server_port}/"
        self.worker = threading.Thread(target=self.server.serve_forever)
        self.worker.start()
        self.addCleanup(self.close)
        self.request = dict(source_instance="old", request_id="update", target=self.g.payload)
        self.current = {"status":"serving", "database": str(self.g.database), "url": self.g.backend[0], "instance": "old", "payload": self.g.backend[3], "pending": self.g.payload, "pending_confirmation": True}
        self.g.source = self.g._authority(self.current)
        gateway.runtime.atomic_json(self.g.runtime_directory / "discovery.json", self.current)
        self.enterContext(patch.object(gateway.runtime, "discover", return_value=self.current))
        self.enterContext(patch.object(gateway.runtime, "control", return_value=self.current))

    def close(self):
        self.g.stop.set()
        with self.g.condition:
            self.g.condition.notify_all()
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(2)

    def authorize_thread(self, identity):
        if identity != "one":
            raise gateway.CoordinationError("Unknown thread")

    def authorize_composer(self, identity, body):
        self.authorize_thread(identity)

    def get(self, route, query=None, **opts):
        if route == "threads/one":
            return {"thread_id": "one", "browser_session": True}
        if route == "sessions/one":
            return {"thread": {"id": "one", "turns": [{"id": "last"}]}}
        raise gateway.CoordinationError("Unknown thread")

    def http(self, method, path, body=None, headers=None):
        with closing(http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=4)) as connection:
            connection.request(method, path, json.dumps(body) if body is not None else None,
                               {"Content-Type": "application/json", **(headers or {})})
            response = connection.getresponse()
            return response.status, json.loads(response.read())

    def prepare(self):
        with patch.object(gateway.runtime, "discover", return_value=self.current), patch.object(
                gateway.runtime, "control", return_value=self.current):
            return self.http("POST", "/_runtime/prepare", self.request, {"Authorization": "Bearer control"})

    def test_terminal_receiver_owns_turn_handoff_after_earlier_inputs_resolve(self):
        import hashlib
        from agent_coord.runtime_message_intake import OrdinaryComposerReceiver
        self.current.update(instance="new", payload=self.g.payload)
        self.g.backend = (*self.g.backend[:2], "new", self.g.payload)
        self.g.terminal = self.g._authority(self.current)
        gateway.runtime.atomic_json(self.g.runtime_directory / "discovery.json", self.current)
        self.g.fence = {**self.request, "state": "draining"}
        result = {"turn": {"id": "successor"}, "steered": False}
        first = {"requestId": "before", "message": "first"}
        body = {"requestId": "after", "message": "second", "expectedTurnId": "last-completed"}
        seen = []
        self.g.intake.accept("one", first)
        self.g.intake.accept("other", {"requestId": "unrelated", "message": "pending"})
        with closing(sqlite3.connect(self.g.database)) as db:
            db.execute("ALTER TABLE browser_submission_receipts ADD COLUMN result_json")
        def forward(thread, original):
            seen.append(original)
            data = json.dumps({"body": {"message": original["message"]}, "queued": False},
                              separators=(",", ":"), sort_keys=True)
            digest = hashlib.sha256(data.encode()).hexdigest()
            with closing(sqlite3.connect(self.g.database)) as db:
                db.execute("INSERT INTO browser_submission_receipts VALUES (?,?,?,?,1,?)",
                           (thread, original["requestId"], "completed", digest, json.dumps(result)))
                db.commit()
            # The transport ended after the receiver committed its outcome.
            raise ConnectionResetError()
        self.g.receiver = OrdinaryComposerReceiver(self.g.database, "new", self.g.payload, forward)
        auth = {"X-Agent-Coord-Token": "browser"}
        endpoint = "/api/browser/sessions/one/messages"
        def authorize(thread, original, *, receiver_owns_turn=False):
            self.authorize_thread(thread)
            if original.get("expectedTurnId") and not receiver_owns_turn:
                raise gateway.CoordinationError("The active turn changed. Refresh before sending.")
        self.g.views.authorize_composer = authorize
        self.assertEqual(self.http("POST", endpoint, body, auth)[0], 409)
        self.assertEqual(seen, [])
        with self.g.intake._connection() as db:
            db.execute("UPDATE intake SET state='committed' WHERE thread_id='one'")
        self.assertEqual(self.http("POST", endpoint, body, auth), (200, result))
        self.assertEqual(self.http("POST", endpoint, body, auth), (200, result))
        self.assertEqual(self.http("POST", endpoint, {**body, "expectedTurnId":"other"}, auth)[0], 409)
        self.assertEqual(seen, [body])
        self.assertEqual(self.http("POST", endpoint, first, auth)[1]["transferSubmission"]["state"], "committed")
        self.assertEqual(self.g.intake.pending_threads(), ["other"])
        self.current["paused"] = True
        self.assertNotEqual(self.http("POST", endpoint, {**body, "requestId":"stopped"}, auth)[0], 200)
        self.assertEqual(seen, [body])

    def test_concurrent_arrivals_keep_fifo_while_predecessor_is_being_authorized(self):
        from concurrent.futures import ThreadPoolExecutor
        import time
        self.assertEqual(self.prepare()[0], 200)
        entering = threading.Event()
        release = threading.Event()
        calls = []
        def authorize(thread, body):
            calls.append(body["requestId"])
            if body["requestId"] == "first":
                entering.set()
                self.assertTrue(release.wait(2))
        path, auth = "/api/browser/sessions/one/messages", {"X-Agent-Coord-Token":"browser"}
        with patch.object(self.g.views, "authorize_composer", side_effect=authorize), ThreadPoolExecutor(2) as pool:
            first = pool.submit(self.http, "POST", path, {"requestId":"first", "message":"one"}, auth)
            self.assertTrue(entering.wait(2))
            second = pool.submit(self.http, "POST", path, {"requestId":"second", "message":"two"}, auth)
            until = time.monotonic() + 1.5
            while time.monotonic() < until:
                with self.g.condition:
                    if len(self.g.composer_waiters.get("one", ())) == 2:
                        break
                time.sleep(.005)
            self.assertEqual(calls, ["first"])
            release.set()
            self.assertEqual([future.result()[0] for future in (first, second)], [202,202])
            self.assertEqual([r["requestId"] for r in self.g.intake.outstanding("one")], ["first","second"])

    def test_pending_input_survives_restart_and_is_observable_while_coordinator_is_offline(self):
        self.assertEqual(self.prepare()[0], 200)
        body = {"message": "pending text", "requestId": "stable", "expectedTurnId": "original"}
        for _ in range(2):
            status, value = self.http("POST", "/api/browser/sessions/one/messages", body, {"X-Agent-Coord-Token": "browser"})
            self.assertEqual(status, 202)
            self.assertEqual(value["transferSubmission"]["state"], "queued")
        status, view = self.http("GET", "/api/browser/sessions/one")
        self.assertEqual((status, view["thread"]["turns"][0]["id"], view["transferReadSource"]), (200, "last", "saved"))
        self.assertEqual([r["requestId"] for r in view["transferSubmissions"]], ["stable"])
        self.assertEqual(RuntimeMessageIntake(self.g.directory).get("one", "stable")["expectedTurnId"], "original")
        status, result = self.http("GET", "/api/browser/update-submissions?thread=one&after=0")
        self.assertEqual(status, 200)
        self.assertFalse(result["more"])
        self.assertEqual([m["message"] for m in result["submissions"]], ["pending text"])
        status, later = self.http("GET", "/api/browser/update-submissions?thread=one&after=" + str(result["cursor"]))
        self.assertEqual((status, later["submissions"]), (200, []))
        self.assertEqual(self.http("GET", "/api/browser/update-submissions?thread=other")[0], 409)

    def test_staged_message_acknowledges_durably_while_legacy_delivery_is_blocked(self):
        import hashlib
        entered, release = threading.Event(), threading.Event()
        forwarded = []
        body = {"message":"original input", "requestId":"key"}
        path = "/api/browser/sessions/one/messages"
        auth = {"X-Agent-Coord-Token":"browser"}
        def legacy(thread, data):
            forwarded.append(data["requestId"])
            encoded = json.dumps({"body":{"message":data["message"]},"queued":False},
                                 separators=(",",":"),sort_keys=True).encode()
            digest = hashlib.sha256(encoded).hexdigest()
            with closing(sqlite3.connect(self.g.database)) as db:
                db.execute("INSERT INTO browser_submission_receipts VALUES (?,?,?,?,101)", (thread,data["requestId"],"accepted",digest))
                db.commit()
                entered.set()
                release.wait(5)
                db.execute("UPDATE browser_submission_receipts SET state='completed' WHERE request_id=?", (data["requestId"],))
                db.commit()
        with patch.object(self.g, "legacy_forward", side_effect=legacy):
            self.assertEqual(self.http("POST",path,body,auth)[0], 202)
            worker = threading.Thread(target=self.g.drain_staged_source)
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertEqual(self.http("POST",path,body,auth)[0], 202)
                second = {"message":"next input", "requestId":"second"}
                self.assertEqual(self.http("POST",path,second,auth)[0], 202)
                with self.g.condition:
                    self.g._save_fence({**self.request, "previous":self.current["payload"], "state":"prepared"})
            finally:
                release.set()
                worker.join(5)
            self.assertEqual(self.prepare()[0],200)
            self.assertEqual(forwarded,["key"])
            self.assertEqual(self.g.intake.get("one","key")["state"],"committed")
            self.assertEqual(self.g.intake.get("one","second")["state"],"queued")

    def test_receipt_signals_identify_only_authorized_watch_keys_and_recover_after_a_gap(self):
        self.assertEqual(self.prepare()[0], 200)
        path = "/api/browser/update-submissions?thread=one&after=0"
        status, page = self.http("GET", path)
        self.assertEqual(status, 200)
        key = page["watchKey"]
        self.assertNotEqual(key, "one")
        first = self.g.revision
        self.g.accept("one", {"requestId": "a", "message": "first"})
        self.g.receipt_completed("other")
        signal = self.g.receipt_signal(first, self.g.revision)
        self.assertIn(key, signal["watchKeys"])
        self.assertEqual(len(signal["watchKeys"]), 2)
        self.assertNotIn("other", signal["watchKeys"])
        self.assertEqual(self.g.receipt_signal(first + 1, self.g.revision)["watchKeys"],
                         [self.g.watch_key("other")])
        self.assertNotIn("watchKeys", self.g.receipt_signal(None, self.g.revision))
        for unused in range(300):
            self.g.receipt_completed("other")
        self.assertNotIn("watchKeys", self.g.receipt_signal(first, self.g.revision))
        self.assertEqual(self.http("GET", path.replace("thread=one", "thread=other"))[0], 409)

    def test_config_uses_current_provider_cursor_on_each_new_connection(self):
        self.assertEqual(self.http("GET", "/api/browser/config")[1]["eventCursor"], 0)
        with closing(sqlite3.connect(self.g.database)) as db:
            db.execute("INSERT INTO coordinator_events VALUES(21)")
            db.execute("INSERT INTO turn_completions VALUES(17)")
            db.commit()
        config = self.http("GET", "/api/browser/config")[1]
        self.assertEqual((config["eventCursor"],config["completionCursor"]), (21,17))

    def test_preparation_release_preserves_new_work_and_requires_original_owner(self):
        self.assertEqual(self.prepare()[0], 200)
        self.g.accept("one", {"requestId":"queued", "message":"saved"})
        current = dict(self.current, status="serving")
        with patch.object(gateway.runtime, "discover", return_value=current), patch.object(gateway.runtime, "control", return_value=current):
            path = "/_runtime/release-preparation"
            auth = {"Authorization":"Bearer control"}
            with patch.object(gateway, "runtime_update_record", return_value={"id":"update","phase":"accepted"}):
                self.assertEqual(self.http("POST", path, self.request, auth)[0], 409)
            self.assertEqual(self.http("POST", path, self.request, auth)[0], 200)
            self.assertEqual(self.http("POST", path, self.request, auth)[0], 200)
            self.assertEqual(self.g.fence["state"], "source_draining")
            self.assertEqual(self.g.intake.get("one","queued")["state"], "queued")

    def test_pending_transition_respects_current_scope_stop_and_control_identity(self):
        self.assertEqual(self.http("POST", "/_runtime/prepare", self.request, {"X-Agent-Coord-Token":"browser"})[0], 403)
        self.assertEqual(self.prepare()[0], 200)
        inputs = {"requestId":"id", "message":"preserve this"}
        headers = {"X-Agent-Coord-Token":"browser"}
        self.assertEqual(self.http("POST", "/api/browser/sessions/other/messages", inputs, headers)[0], 409)
        self.assertEqual(self.http("POST", "/api/browser/sessions/one/messages", inputs, {})[0], 403)
        self.assertEqual(self.http("GET", "/api/browser/config", headers={"X-Forwarded-For":"127.0.0.1"})[0], 403)
        gateway.runtime.atomic_json(self.g.runtime_directory / "stop-proof.json", {"instance":"old"})
        self.assertEqual(self.http("POST", "/api/browser/sessions/one/messages", inputs, headers)[0], 409)
        self.assertFalse(self.g.intake.pending_threads())

    def test_unresolved_source_receipt_withholds_authorization(self):
        with closing(sqlite3.connect(self.g.database)) as db:
            db.execute("INSERT INTO browser_submission_receipts VALUES ('one','pre','uncertain','hash',101)")
            db.commit()
        self.assertEqual(self.prepare()[0], 409)
        self.assertEqual(self.g.fence["state"], "prepared")

    def test_source_acceptance_in_flight_finishes_before_authorization(self):
        with closing(sqlite3.connect(self.g.database)) as db:
            db.execute("INSERT INTO browser_submission_receipts VALUES ('one','in-flight','accepted','hash',101)")
            db.commit()
        def finish():
            with self.g.condition:
                self.g.condition.wait_for(lambda: bool(self.g.fence), 3)
            with closing(sqlite3.connect(self.g.database)) as db:
                db.execute("UPDATE browser_submission_receipts SET state='completed' WHERE request_id='in-flight'")
                db.commit()
        worker = threading.Thread(target=finish)
        worker.start()
        try:
            self.assertEqual(self.prepare()[0],200)
        finally:
            worker.join(3)

    def test_repeated_notifications_read_only_changed_receipts(self):
        intake = self.g.intake
        intake.accept("one", {"requestId":"first", "message":"a"})
        after, first = intake.thread_changes("one", 0)
        self.assertEqual([r["requestId"] for r in first], ["first"])
        intake.accept("one", {"requestId":"second", "message":"b"})
        after, second = intake.thread_changes("one", after)
        self.assertEqual([r["requestId"] for r in second], ["second"])
        self.assertEqual(intake.thread_changes("one", after), (after, []))


if __name__ == "__main__":
    unittest.main()
