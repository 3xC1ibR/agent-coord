from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler
import threading
import unittest

import test_desktop_runtime  # Add the in-tree runtime to the import path.
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
