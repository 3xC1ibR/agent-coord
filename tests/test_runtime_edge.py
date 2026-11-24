from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import test_desktop_runtime
from agent_coord import runtime_edge, runtime_update
from agent_coord import desktop_runtime as runtime


FIRST = "a" * 32
SECOND = "b" * 32


def listener(test, server):
    thread = threading.Thread(target=server.serve_forever, args=(.01,), daemon=True)
    thread.start()
    test.addCleanup(server.server_close)
    test.addCleanup(thread.join, 3)
    test.addCleanup(server.shutdown)
    return server


def request(address, path, *, body=None, headers=None):
    with closing(HTTPConnection(runtime_edge.authority(address), timeout=5)) as client:
        client.request("POST" if body is not None else "GET", path, body, headers or {})
        with client.getresponse() as response:
            return response.status, response.read()


class EdgeProtocolTests(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory(prefix="ribbon-edge-protocol-")
        self.addCleanup(root.cleanup)
        self.directory = Path(root.name)
        self.edge = listener(self, runtime_edge.EdgeServer(self.directory, {}))
        self.address = self.edge.origin + "/"
        self.received = []
        self.disconnect = False
        self.refuse = False
        self.first = self.upstream(FIRST)
        self.second = self.upstream(SECOND)
        self.route(self.first, FIRST)

    def upstream(self, generation):
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                fixture.received.append((generation, self.path, self.headers))
                if self.path.startswith(runtime_edge.SSE):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    self.wfile.write(b"id: 11:42\ndata: {}\n\n")
                else:
                    body = json.dumps({"token": generation, "eventCursor": 9, "completionCursor": 40}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                fixture.received.append((generation, self.path, self.headers))
                if fixture.disconnect:
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
                if fixture.refuse and generation == FIRST:
                    self.send_response(503)
                    self.send_header(runtime_edge.FENCED_HEADER, generation)
                else:
                    self.send_response(201)
                self.send_header("Content-Length", "0")
                self.end_headers()

        server = listener(self, ThreadingHTTPServer(("127.0.0.1", 0), Handler))
        return f"http://127.0.0.1:{server.server_port}/"

    def route(self, address, generation, ready=True):
        runtime_edge.publish(self.directory, {"url": address, "instance": generation, "status": "serving"},
                             generation, ready=ready)

    def test_stable_token_and_generation_scoped_sse_resume(self):
        code, config = request(self.address, runtime_edge.CONFIG)
        self.assertEqual(code, 200)
        config = json.loads(config)
        self.assertEqual(config["token"], self.edge.browser_token)
        self.assertEqual(config["eventCursor"], FIRST + "/9")
        code, frame = request(self.address, runtime_edge.SSE + "?after=" + config["eventCursor"] + "&completion_after=40")
        self.assertEqual(code, 200)
        self.assertIn(b"id: " + FIRST.encode() + b"/11:42", frame)
        self.assertEqual(self.received[-1][2]["Last-Event-ID"], "9:40")
        self.route(self.second, SECOND)
        code, frame = request(self.address, runtime_edge.SSE, headers={"Last-Event-ID": FIRST + "/11:42"})
        self.assertEqual(code, 200)
        self.assertIn(b"id: " + SECOND.encode() + b"/11:42", frame)
        self.assertEqual(self.received[-1][2]["Last-Event-ID"], "0:42")
        code, _ = request(self.address, "/send", body=b"{}", headers={
            runtime_edge.TOKEN_HEADER: config["token"], "Origin": self.edge.origin})
        self.assertEqual(code, 201)
        self.assertEqual(self.received[-1][2][runtime_edge.TOKEN_HEADER], SECOND)
        self.assertEqual(self.received[-1][2]["Origin"], self.second.rstrip("/"))

    def test_origin_and_runtime_health_are_checked_before_forwarding(self):
        for headers in ({"Host": "external.invalid"}, {"Origin": "http://external.invalid"},
                        {"Forwarded": "host=127.0.0.1"}, {"Sec-Fetch-Site": "cross-site"},
                        {runtime_edge.TOKEN_HEADER: "wrong"}):
            with self.subTest(headers=headers):
                self.assertEqual(request(self.address, runtime_edge.CONFIG, headers=headers)[0], 403)
        self.assertEqual(request(self.address, runtime_edge.HEALTH)[0], 403)
        code, health = request(self.address, runtime_edge.HEALTH,
                               headers={runtime_edge.OBSERVER_HEADER: self.edge.observer})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(health)["instance"], FIRST)
        self.assertEqual(self.received, [])

    def test_fenced_post_can_move_to_successor_but_a_lost_reply_is_never_replayed(self):
        self.refuse = True
        with ThreadPoolExecutor(max_workers=1) as pool:
            sent = pool.submit(request, self.address, "/send", body=b"{}")
            deadline = time.monotonic() + 1
            while not self.received and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(self.received)
            self.route(self.second, SECOND)
            self.assertEqual(sent.result(timeout=3)[0], 201)
        self.assertEqual([entry[0] for entry in self.received][-1], SECOND)
        self.received.clear()
        self.disconnect = True
        self.assertEqual(request(self.address, "/send", body=b"{}")[0], 502)
        self.assertEqual([entry[0] for entry in self.received], [SECOND])

    def test_request_waits_for_the_fenced_route_to_open(self):
        self.route(self.first, FIRST, ready=False)
        with ThreadPoolExecutor(max_workers=1) as pool:
            sent = pool.submit(request, self.address, "/send", body=b"{}")
            time.sleep(.08)
            self.assertFalse(sent.done())
            self.assertEqual(self.received, [])
            self.route(self.second, SECOND)
            self.assertEqual(sent.result(timeout=3)[0], 201)
        self.assertEqual([entry[0] for entry in self.received], [SECOND])


class CoordinatorEdgeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_desktop_runtime.RuntimeTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def launch(self, resources, *arguments):
        fixture = self.fixture
        child = subprocess.run([sys.executable, "-B", str(resources / "backend.py"),
            "--runtime-bootstrap", "--runtime-stable-origin", "--resources", str(resources),
            "--db", str(fixture.database), *arguments],
            env={**fixture.env, "PYTHONPATH": str(resources / "backend")}, capture_output=True, text=True, timeout=25)
        self.assertEqual(child.returncode, 0, child.stderr + fixture.log())
        return json.loads(child.stdout)

    def test_origin_and_pending_approval_survive_three_coordinator_changes(self):
        fixture = self.fixture
        source = fixture.resources
        first = self.launch(source)
        address = first["edge_url"]
        observer = {runtime_edge.OBSERVER_HEADER: first["edge_observer"]}
        token = fixture.api(address, "api/browser/config")["token"]
        thread = fixture.api(address, "api/browser/sessions", {"cwd": str(fixture.root)})["session"]["thread_id"]
        conversation = f"api/browser/sessions/{thread}"
        fixture.api(address, conversation + "/messages", {"message": "one", "requestId": "one"})
        approval = fixture.wait(lambda: fixture.api(address, conversation)["requests"])[0]["key"]
        initial_epoch = runtime.control(runtime.discover(fixture.database), "status")["transport"]["epoch"]
        provider = int((fixture.root / "provider.pid").read_text())
        edge = runtime_edge.read_address(runtime.runtime_directory(fixture.database))
        for build in range(2, 5):
            resources = fixture.root / f"Build {build}/Resources"
            shutil.copytree(source, resources)
            metadata = runtime.read_json(resources / "backend.json")
            (resources / "backend.json").write_text(json.dumps({**metadata, "runtime_version": f"1+{build}"}))
            staged = self.launch(resources, "--runtime-confirm-updates")
            self.assertEqual(staged["edge_url"], address)
            record = runtime.discover(fixture.database)
            runtime.control(record, "activate-update", request_id=f"edge-{build}", payload=staged["pending"])
            failures = []
            def crossing():
                try:
                    code, response = request(address, "/" + conversation)
                    if code != 200:
                        failures.append((code, response.decode()))
                except (OSError, HTTPException) as exc:
                    failures.append(str(exc))
            with ThreadPoolExecutor(max_workers=4) as pool:
                deadline = time.monotonic() + 10
                samples = []
                while time.monotonic() < deadline:
                    samples.append(pool.submit(crossing))
                    code, data = request(address, runtime_edge.HEALTH, headers=observer)
                    health = json.loads(data)
                    if code == 200 and health.get("status") == "serving" and health.get("payload") == staged["pending"]:
                        break
                    time.sleep(.07)
                else:
                    self.fail("Candidate never served through the stable edge: " + fixture.log())
                for sample in samples:
                    sample.result(timeout=5)
            self.assertEqual(failures, [])
            self.assertEqual(health["edge_instance"], edge["instance"])
            self.assertEqual(health["update"]["phase"], "committed")
            self.assertEqual(fixture.api(address, "api/browser/config")["token"], token)
            current = runtime.control(runtime.discover(fixture.database), "status")
            self.assertEqual(current["transport"]["epoch"], initial_epoch + build - 1)
            self.assertEqual(provider, int((fixture.root / "provider.pid").read_text()))
            self.assertEqual(fixture.api(address, conversation)["requests"][0]["key"], approval)
        fixture.api(address, conversation + "/requests/" + approval, {"decision": "accept"})
        fixture.wait(lambda: any(call.get("id") == 900 and "result" in call for call in fixture.calls()))
        self.assertEqual(sum(call.get("method") == "turn/start" for call in fixture.calls()), 1)
        self.assertEqual(sum(call.get("id") == 900 and "result" in call for call in fixture.calls()), 1)
