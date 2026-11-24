from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import fcntl
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import test_desktop_runtime  # Add the in-tree runtime to the import path.
from agent_coord import desktop_runtime, runtime_edge
from agent_coord.local_http import LocalHTTPServer
from agent_coord.runtime_cutover import RuntimeListeners
from agent_coord.ui import LoopbackHTTPServer


class StatusHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(204)
        self.end_headers()

    def log_message(self, *_):
        pass


class RuntimeCutoverTests(unittest.TestCase):
    def test_fence_waits_briefly_for_an_edge_lease_before_draining(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            owner = desktop_runtime.Runtime(root / "runtime.sqlite3", root / "predecessor", -1)
            sessions = SimpleNamespace(admission=desktop_runtime.AdmissionGate(), lock=threading.RLock(),
                active={}, projection=SimpleNamespace(error=None), muse=SimpleNamespace())
            owner.server = SimpleNamespace(browser_sessions=sessions)
            owner.edge_route = Mock()
            owner.pending = root / "candidate"
            (owner.directory / "edge.json").touch()
            owner.request_update("held-edge-lease", str(owner.pending))

            with runtime_edge.inflight_descriptor(owner.directory) as descriptor:
                fcntl.flock(descriptor, fcntl.LOCK_SH)
                self.assertFalse(owner.prepare_handover())
                self.assertFalse(sessions.admission.paused)
                owner.edge_route.assert_not_called()

                owner.update_drain_started -= desktop_runtime.UPDATE_ADMISSION_OPPORTUNITY_SECONDS
                self.assertFalse(owner.prepare_handover())
                self.assertTrue(sessions.admission.paused)
                owner.edge_route.assert_called_once_with(ready=False)

            self.assertTrue(owner.prepare_handover())

    def test_services_follow_listener_readiness_and_cancel_before_teardown(self):
        control = LocalHTTPServer(("127.0.0.1", 0), StatusHandler)
        self.addCleanup(control.server_close)
        browser = LoopbackHTTPServer(("127.0.0.1", 0), StatusHandler)
        self.addCleanup(browser.server_close)
        listeners = RuntimeListeners(control, browser)
        listeners.start()
        self.addCleanup(listeners.close)
        entered = threading.Event()
        release = threading.Event()
        started_late = threading.Event()

        def held_startup():
            entered.set()
            release.wait(5)

        browser.start_services((held_startup, started_late.set), background=True)
        try:
            self.assertTrue(entered.wait(2))
            for server in (control, browser):
                client = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
                try:
                    client.request("GET", "/")
                    with client.getresponse() as response:
                        self.assertEqual(response.status, 204)
                finally:
                    client.close()
            listeners.close()
            with ThreadPoolExecutor(max_workers=1) as pool:
                closing = pool.submit(browser.server_close)
                try:
                    self.assertTrue(browser._startup_cancelled.wait(2))
                    self.assertFalse(closing.done())
                finally:
                    release.set()
                closing.result(2)
            self.assertFalse(started_late.is_set())
        finally:
            release.set()
