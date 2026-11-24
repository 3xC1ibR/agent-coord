"""Session creation recovery holds one identity across browser retries."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from http.client import HTTPConnection, RemoteDisconnected
import json
import threading
from unittest import TestCase
from unittest.mock import patch

import test_ui


class SessionCreationReceiptTests(TestCase):
    setUp = test_ui.BrowserHTTPTests.setUp
    stop_server = test_ui.BrowserHTTPTests.stop_server
    request = test_ui.BrowserHTTPTests.request
    route = "/api/browser/sessions"

    def provider_effects(self, request):
        return [call.args[0] for call in request.call_args_list if call.args[0] in {"thread/start", "turn/start"}]

    def assert_unknown(self, response):
        status, body = response
        self.assertEqual(status, 409)
        self.assertEqual(body.get("code"), "submission_outcome_unknown")
        self.assertTrue(body.get("error"))

    def test_same_identity_returns_original_result_and_rejects_changed_settings(self):
        settings = {"requestId": "creation", "name": "Original", "model": "other-model", "effort": "low"}
        with patch.object(self.sessions.rpc, "request", wraps=self.sessions.rpc.request) as provider:
            first = self.request(self.route, settings)
            replay = self.request(self.route, dict(reversed(tuple(settings.items()))))
            changed = self.request(self.route, {**settings, "name": "Changed"})
        self.assertEqual(replay, first)
        self.assertEqual(first[0], 201)
        self.assertEqual((first[1]["session"]["model"], first[1]["session"]["effort"]), ("other-model", "low"))
        self.assertEqual(changed[0], 400)
        self.assertEqual(set(changed[1]), {"error"})
        self.assertIn("different settings", changed[1]["error"])
        self.assertEqual(self.provider_effects(provider), ["thread/start"])

    def test_concurrent_same_identity_cannot_repeat_inflight_creation(self):
        settings = {"requestId": "inflight", "name": "Concurrent creation"}
        entered, release = threading.Event(), threading.Event()
        native = self.sessions.rpc.request
        effects = []

        def hold_creation(method, params=None):
            if method == "thread/start":
                self.assertNotEqual(self.store._writer.owner, threading.get_ident())
                effects.append(method)
                entered.set()
                if not release.wait(4):
                    raise RuntimeError("Timed out waiting for the creation retry")
            return native(method, params)

        with patch.object(self.sessions.rpc, "request", side_effect=hold_creation), ThreadPoolExecutor(1) as pool:
            original = pool.submit(self.request, self.route, settings)
            try:
                self.assertTrue(entered.wait(2))
                self.assert_unknown(self.request(self.route, settings))
            finally:
                release.set()
            first = original.result(timeout=4)
        self.assertEqual(first[0], 201)
        self.assertEqual(self.request(self.route, settings), first)
        self.assertEqual(effects, ["thread/start"])

    def test_lost_completed_creation_response_recovers_one_session_and_first_message(self):
        settings = {"requestId": "creation", "name": "Recovered session"}
        handler = self.server.RequestHandlerClass
        write_json = handler._json
        lost = []

        def omit_first_creation_response(request, status, value):
            if status == 201 and request.path == self.route and not lost:
                lost.append(value)
                request.close_connection = True
                return
            write_json(request, status, value)

        headers = {"X-Agent-Coord-Token": self.token, "Content-Type": "application/json"}
        with patch.object(handler, "_json", omit_first_creation_response), patch.object(
                self.sessions.rpc, "request", wraps=self.sessions.rpc.request) as provider:
            with closing(HTTPConnection("127.0.0.1", self.server.server_port, timeout=4)) as connection:
                connection.request("POST", self.route, json.dumps(settings), headers)
                with self.assertRaises(RemoteDisconnected):
                    connection.getresponse()
            status, recovered = self.request(self.route, settings)
            self.assertEqual((status, recovered), (201, lost[0]))
            path = f"{self.route}/{recovered['session']['thread_id']}/messages"
            message = {"requestId": "first-message", "message": "First input after recovery"}
            sent = self.request(path, message)
            self.assertEqual(self.request(path, message), sent)
            self.assertEqual(sent[0], 200)
        self.assertEqual(self.provider_effects(provider), ["thread/start", "turn/start"])

    def test_failed_completion_write_keeps_creation_unconfirmed_without_replaying_it(self):
        settings = {"requestId": "creation", "name": "Created before receipt failure"}
        with self.store._connection() as db:
            db.execute("""CREATE TRIGGER fail_creation_completion BEFORE UPDATE ON browser_session_creation_receipts
                BEGIN SELECT RAISE(ABORT, 'controlled creation completion failure'); END""")
        with patch.object(self.sessions.rpc, "request", wraps=self.sessions.rpc.request) as provider:
            self.assert_unknown(self.request(self.route, settings))
            with self.store._connection() as db:
                db.execute("DROP TRIGGER fail_creation_completion")
            self.assert_unknown(self.request(self.route, settings))
        self.assertEqual(self.provider_effects(provider), ["thread/start"])
        with self.store.reading(), self.store._connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM browser_sessions").fetchone()[0], 1)
            row = db.execute("SELECT thread_id, result_json FROM browser_session_creation_receipts WHERE request_id=?",
                             (settings["requestId"],)).fetchone()
        self.assertEqual(tuple(row), (None, None))

    def test_completed_replay_requires_current_workspace_access(self):
        settings = {"requestId": "creation", "name": "Moved conversation"}
        status, original = self.request(self.route, settings)
        self.assertEqual(status, 201)
        with self.store._connection() as db:
            db.execute("UPDATE browser_sessions SET cwd=? WHERE thread_id=?",
                       (str(self.root.parent), original["session"]["thread_id"]))
        with patch.object(self.sessions.rpc, "request", wraps=self.sessions.rpc.request) as provider:
            self.assertEqual(self.request(self.route, settings), (400, {
                "code": "workspace_access_denied", "error": "Work thread not found in this workspace.",
            }))
        self.assertEqual(self.provider_effects(provider), [])

    def test_capability_preserves_legacy_creation_and_invalid_settings_can_be_corrected(self):
        self.assertEqual(self.request("/api/browser/config")[1].get("sessionCreationReceipts"), 1)
        invalid = {"requestId": "correction", "name": 123}
        self.assertEqual(self.request(self.route, invalid)[0], 400)
        self.assertEqual(self.request(self.route, {**invalid, "name": "Corrected settings"})[0], 201)
        first = self.request(self.route, {"name": "Legacy"})
        second = self.request(self.route, {"name": "Legacy"})
        self.assertEqual((first[0], second[0]), (201, 201))
        self.assertNotEqual(first[1]["session"]["thread_id"], second[1]["session"]["thread_id"])
