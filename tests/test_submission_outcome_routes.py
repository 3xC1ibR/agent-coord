"""Composer HTTP outcomes distinguish definite rejection from uncertainty."""
from contextlib import closing
from http.client import HTTPConnection
import json
import threading
from unittest import TestCase
from unittest.mock import patch

import test_ui
from agent_coord import runtime_edge
from agent_coord.codex_app_server import BrowserNotSubmittedError
from agent_coord.store import CoordinationError


class SubmissionOutcomeRouteTests(TestCase):
    setUp = test_ui.BrowserHTTPTests.setUp
    stop_server = test_ui.BrowserHTTPTests.stop_server
    request = test_ui.BrowserHTTPTests.request

    def post(self, port, thread, body, *, token=None, operation="messages"):
        headers = {"X-Agent-Coord-Token": token or self.token, "Content-Type": "application/json",
                   "Origin": f"http://127.0.0.1:{port}"}
        with closing(HTTPConnection("127.0.0.1", port, timeout=5)) as client:
            client.request("POST", f"/api/browser/sessions/{thread}/{operation}", json.dumps(body), headers)
            with client.getresponse() as response:
                return response.status, response.getheader(runtime_edge.FENCED_HEADER), json.load(response)

    def assert_unknown(self, response):
        status, fence, result = response
        self.assertEqual((status, fence), (409, None))
        self.assertEqual(result.get("code"), "submission_outcome_unknown")
        self.assertTrue(result.get("error"))

    def receipt_state(self, thread, request):
        with self.store.reading(), self.store._connection() as db:
            return db.execute("""SELECT state FROM browser_submission_receipts
                WHERE thread_id=? AND request_id=?""", (thread, request)).fetchone()[0]

    def test_completion_write_failure_keeps_the_original_identity_across_the_stable_edge(self):
        thread = self.sessions.create({})["session"]["thread_id"]
        body = {"requestId": "original", "message": "Run the check"}
        directory = self.root / "edge"
        directory.mkdir()
        edge = runtime_edge.EdgeServer(directory, {})
        worker = threading.Thread(target=edge.serve_forever)
        worker.start()
        self.addCleanup(worker.join, 3)
        self.addCleanup(edge.server_close)
        self.addCleanup(edge.shutdown)
        generation = "e" * 32
        runtime_edge.publish(directory, {"url": self.url + "/", "instance": generation,
                            "status": "serving"}, self.token, ready=True)
        self.server.edge_fenced_generation = generation
        with self.store._connection() as db:
            db.execute("""CREATE TRIGGER fail_submission_completion BEFORE UPDATE
                ON browser_submission_receipts WHEN NEW.state='completed'
                BEGIN SELECT RAISE(ABORT, 'controlled completion failure'); END""")
        with patch.object(self.sessions.rpc, "request", wraps=self.sessions.rpc.request) as provider:
            first = self.post(edge.server_port, thread, body, token=edge.browser_token)
            self.assertEqual(self.receipt_state(thread, "original"), "accepted")
            with self.store._connection() as db:
                db.execute("DROP TRIGGER fail_submission_completion")
            same = self.post(self.server.server_port, thread, body)
            effects = [call.args[0] for call in provider.call_args_list
                       if call.args[0] in {"turn/start", "turn/steer"}]
        self.assertEqual(effects, ["turn/start"])
        self.assertEqual(self.receipt_state(thread, "original"), "accepted")
        self.assert_unknown(first)
        self.assert_unknown(same)

    def test_uncertain_provider_response_keeps_the_original_identity(self):
        thread = self.sessions.create({})["session"]["thread_id"]
        body = {"requestId": "original", "message": "Run the check"}
        with patch.object(self.sessions, "_send_composer", side_effect=CoordinationError("Provider response was lost.")) as send:
            first = self.post(self.server.server_port, thread, body)
            same = self.post(self.server.server_port, thread, body)
        send.assert_called_once_with(thread, body)
        self.assertEqual(self.receipt_state(thread, "original"), "uncertain")
        self.assert_unknown(first)
        self.assert_unknown(same)

    def test_lost_steering_response_after_provider_effect_is_uncertain(self):
        thread = self.sessions.create({})["session"]["thread_id"]
        request = self.sessions.rpc.request
        _, _, started = self.post(self.server.server_port, thread, {"message": "Start a turn"})
        body = {"requestId": "steering", "message": "Apply this revision",
                "expectedTurnId": started["acceptedMessage"]["turnId"]}
        effects = []

        def lose_steering_response(method, parameters=None):
            result = request(method, parameters)
            if method == "turn/steer":
                effects.append(parameters["input"])
                raise CoordinationError("Provider response was lost.")
            return result

        with patch.object(self.sessions.rpc, "request", side_effect=lose_steering_response):
            first = self.post(self.server.server_port, thread, body)
            same = self.post(self.server.server_port, thread, body)
        self.assertEqual(len(effects), 1)
        self.assertEqual(self.receipt_state(thread, "steering"), "uncertain")
        self.assert_unknown(first)
        self.assert_unknown(same)

    def test_validation_error_before_provider_input_stays_definite(self):
        thread = self.sessions.create({})["session"]["thread_id"]
        body = {"requestId": "validation", "message": "/effort unsupported"}
        status, fence, result = self.post(self.server.server_port, thread, body)
        self.assertEqual((status, fence), (400, None))
        self.assertEqual(set(result), {"error"})
        self.assertIn("Reasoning effort is not supported", result["error"])
        self.assertEqual(self.receipt_state(thread, "validation"), "rejected")

    def test_queue_validation_before_insertion_stays_definite(self):
        thread = self.sessions.create({})["session"]["thread_id"]
        body = {"requestId": "validation", "message": ""}
        status, fence, result = self.post(self.server.server_port, thread, body, operation="queue")
        self.assertEqual((status, fence), (400, None))
        self.assertEqual(set(result), {"error"})
        self.assertEqual(self.receipt_state(thread, "validation"), "rejected")

    def test_queue_publication_failure_cannot_repeat_the_committed_insert(self):
        thread = self.sessions.create({})["session"]["thread_id"]
        body = {"requestId": "queue-original", "message": "Run the queued check"}
        publish = self.sessions._publish
        dispatch = self.sessions.queue._dispatch
        publications = []
        worker_ready, release_worker, delivered = (threading.Event() for _ in range(3))

        def fail_after_insert(method, parameters, **extra):
            if method == "browser/changed" and parameters.get("threadId") == thread and not publications:
                items = self.sessions.queue.list(thread)
                if items:
                    publications.append([item["id"] for item in items])
                    raise CoordinationError("Could not publish the committed queue change.")
            return publish(method, parameters, **extra)

        def observe_dispatch(identity):
            worker_ready.set()
            if release_worker.wait(3):
                dispatch(identity)
                delivered.set()

        with (
            patch.object(self.sessions, "_publish", side_effect=fail_after_insert),
            patch.object(self.sessions.queue, "_dispatch", side_effect=observe_dispatch),
            patch.object(self.sessions.rpc, "request", wraps=self.sessions.rpc.request) as provider,
        ):
            try:
                first = self.post(self.server.server_port, thread, body, operation="queue")
                same = self.post(self.server.server_port, thread, body, operation="queue")
                items = self.sessions.queue.list(thread)
                self.assertEqual(len(items), 1)
                self.assertEqual(publications, [[items[0]["id"]]])
                self.assertEqual(self.receipt_state(thread, "queue-original"), "uncertain")
                self.assert_unknown(first)
                self.assert_unknown(same)
                self.assertTrue(worker_ready.wait(2), "The committed queue item did not wake its worker.")
            finally:
                release_worker.set()
            self.assertTrue(delivered.wait(3))
            late = self.post(self.server.server_port, thread, body, operation="queue")
        effects = [call.args[0] for call in provider.call_args_list if call.args[0] in {"turn/start", "turn/steer"}]
        self.assertEqual(effects, ["turn/start"])
        self.assertEqual(self.sessions.queue.list(thread), [])
        self.assert_unknown(late)

    def test_proven_pre_submission_rejection_allows_same_identity_retry(self):
        thread = self.sessions.create({})["session"]["thread_id"]
        body = {"requestId": "original", "message": "Run the check"}
        with patch.object(self.sessions, "_send_composer", side_effect=BrowserNotSubmittedError("Input was not sent.")):
            first = self.post(self.server.server_port, thread, body)
        self.assertEqual(first, (400, None, {"error": "Input was not sent."}))
        self.assertEqual(self.receipt_state(thread, "original"), "rejected")
        status, fence, result = self.post(self.server.server_port, thread, body)
        self.assertEqual((status, fence), (200, None))
        self.assertEqual(result["acceptedMessage"]["requestId"], "original")
        self.assertEqual(self.receipt_state(thread, "original"), "completed")

    def test_open_reports_typed_workspace_denial_for_outside_and_missing_threads(self):
        outside = self.sessions.create({})["session"]["thread_id"]
        with self.store._connection() as db:
            db.execute("UPDATE browser_sessions SET cwd=? WHERE thread_id=?", (str(self.root.parent), outside))
        expected = (400, {"code": "workspace_access_denied", "error": "Work thread not found in this workspace."})
        for identity in (outside, "unregistered"):
            for path in (f"conversations/{identity}/open", f"sessions/{identity}"):
                with self.subTest(path=path):
                    self.assertEqual(self.request(f"/api/browser/{path}"), expected)
