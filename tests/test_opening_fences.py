"""Opening routes agree on runtime admission and managed-workspace access."""
from contextlib import closing
from http.client import HTTPConnection
import json
from unittest import TestCase
from unittest.mock import patch

import test_ui
from agent_coord.codex_app_server import BrowserBusyError
from agent_coord.runtime_edge import FENCED_HEADER


class OpeningFenceTests(TestCase):
    setUp = test_ui.BrowserHTTPTests.setUp
    stop_server = test_ui.BrowserHTTPTests.stop_server
    request = test_ui.BrowserHTTPTests.request

    def opening_routes(self, thread_id):
        session = f"/api/browser/sessions/{thread_id}"
        recent = {"X-Agent-Coord-History-View": "recent"}
        return (
            ("composite", f"/api/browser/conversations/{thread_id}/open", {}),
            ("revision-aware", session, {**recent, "X-Agent-Coord-Conversation-Revisions": "1"}),
            ("serialized recent", session, recent),
            ("serialized complete", session, {}),
        )

    def read(self, path, headers):
        with closing(HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)) as client:
            client.request("GET", path, headers=headers)
            with client.getresponse() as response:
                return response.status, response.getheader(FENCED_HEADER), json.load(response)

    def test_opening_routes_signal_a_generation_fence_to_the_stable_edge(self):
        identity = self.sessions.create({})["session"]["thread_id"]
        generation = "e" * 32
        self.server.edge_fenced_generation = generation
        self.sessions.admission.pause()
        self.addCleanup(self.sessions.admission.resume)
        for name, path, headers in self.opening_routes(identity):
            with self.subTest(opening=name):
                status, fenced, body = self.read(path, headers)
                self.assertEqual((status, fenced), (503, generation))
                self.assertEqual(set(body), {"error"})
        self.sessions.admission.resume()
        for name, path, headers in self.opening_routes(identity):
            with self.subTest(reopened=name):
                status, fenced, body = self.read(path, headers)
                self.assertEqual((status, fenced), (200, None))
                self.assertEqual(body["session"]["thread_id"], identity)

    def test_paused_standalone_opening_returns_maintenance_guidance(self):
        identity = self.sessions.create({})["session"]["thread_id"]
        self.sessions.admission.notice = "This update needs review before opening conversations."
        self.sessions.admission.pause()
        self.addCleanup(self.sessions.admission.resume)
        for name, path, headers in self.opening_routes(identity):
            with self.subTest(opening=name):
                status, fenced, body = self.read(path, headers)
                self.assertEqual((status, fenced), (503, None))
                self.assertEqual(body, {"error": self.sessions.admission.notice})

    def test_error_after_admission_does_not_signal_a_generation_fence(self):
        identity = self.sessions.create({})["session"]["thread_id"]
        self.server.edge_fenced_generation = "e" * 32
        message = "Conversation state is still being saved."

        with patch.object(self.sessions.conversation_open, "_read", side_effect=BrowserBusyError(message)):
            status, fenced, body = self.read(f"/api/browser/conversations/{identity}/open", {})
        self.assertEqual((status, fenced, body), (400, None, {"error": message}))

    def test_saved_browser_worker_is_excluded_from_every_opening_route(self):
        identity = self.sessions.create({})["session"]["thread_id"]
        parent = "delegating-parent"
        self.store.register(session_id=parent, client="codex", cwd=str(self.root))
        delegation = self.store.create_delegation(parent_session_id=parent, cwd=str(self.root),
            scopes=["worker.py"], instructions="Work inside the managed scope.", mode="reviewed")
        self.store.attach_delegation(delegation["delegation_id"], identity)

        status, fenced, body = self.read(f"/api/browser/sessions/{identity}?limit=4", {})
        self.assertEqual((status, fenced), (400, None))
        self.assertEqual(body, {"code": "workspace_access_denied", "error": "Work thread not found in this workspace."})
        for name, path, headers in self.opening_routes(identity):
            with self.subTest(opening=name):
                self.assertEqual(self.read(path, headers), (status, fenced, body))
