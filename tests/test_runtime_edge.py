from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler
import json
import math
import os
from pathlib import Path
import shlex
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import test_desktop_runtime
from agent_coord import runtime_edge, runtime_update
from agent_coord import desktop_runtime as runtime
from agent_coord.local_http import LocalHTTPServer


FIRST = "a" * 32
SECOND = "b" * 32


def listener(test, server):
    thread = threading.Thread(target=server.serve_forever, args=(.01,), daemon=True)
    thread.start()
    test.addCleanup(server.server_close)
    test.addCleanup(thread.join, 3)
    test.addCleanup(server.shutdown)
    return server


def request(address, path, *, body=None, headers=None, timings=None):
    with closing(HTTPConnection(runtime_edge.authority(address), timeout=5)) as client:
        client.request("POST" if body is not None else "GET", path, body, headers or {})
        with client.getresponse() as response:
            if timings is not None:
                component = response.getheader("Server-Timing", "")
                if component.startswith("submission;dur="):
                    timings["coordinator_ms"] = float(component.removeprefix("submission;dur="))
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

        server = listener(self, LocalHTTPServer(("127.0.0.1", 0), Handler))
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

    def test_readiness_observation_respects_the_coordinator_fence(self):
        observer = {runtime_edge.OBSERVER_HEADER: self.edge.observer}
        self.assertEqual(request(self.address, runtime_edge.READY)[0], 403)
        self.assertEqual(request(self.address, runtime_edge.READY, headers=observer)[0], 200)
        self.route(self.first, FIRST, ready=False)
        self.assertEqual(request(self.address, runtime_edge.READY, headers=observer)[0], 503)
        self.assertEqual(request(self.address, runtime_edge.HEALTH, headers=observer)[0], 200)

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

    def launch(self, resources, *arguments, stable=True):
        fixture = self.fixture
        command = runtime.payload_command(resources)
        try:
            child = subprocess.run([command[0], "-B", *command[1:],
                "--runtime-bootstrap", *(["--runtime-stable-origin"] if stable else []), "--resources", str(resources),
                "--db", str(fixture.database), *arguments],
                env={**fixture.env, "PYTHONPATH": str(resources / "backend"), "PYTHONDONTWRITEBYTECODE": "1"},
                capture_output=True, text=True, timeout=90 if resources != fixture.resources and not stable else 25)
        except subprocess.TimeoutExpired as error:
            self.fail(str(error) + "\n" + fixture.log())
        self.assertEqual(child.returncode, 0, child.stderr + fixture.log())
        return json.loads(child.stdout)

    def test_predecessor_adopts_the_stable_supervisor_without_restarting_provider(self):
        from test_schema_compatibility import BUILD_98_SOURCE
        fixture = self.fixture
        actual = os.environ.get("RIBBON_BUILD_98_RESOURCES")
        if actual:
            previous = Path(actual)
        else:
            previous = fixture.root / "Build 98/Resources"
            shutil.copytree(fixture.resources, previous)
            relative = "plugins/agent-coord/scripts/agent_coord/desktop_runtime.py"
            source = subprocess.run(["git", "show", f"{BUILD_98_SOURCE}:{relative}"],
                                    cwd=test_desktop_runtime.ROOT, capture_output=True, timeout=10)
            if source.returncode:
                if os.environ.get("RIBBON_SCHEMA_REQUIRE_PREDECESSOR") == "1":
                    self.fail(source.stderr.decode())
                self.skipTest("Build 98 source requires repository history")
            (previous / "backend/agent_coord/desktop_runtime.py").write_bytes(source.stdout)
            metadata = runtime.read_json(previous / "backend.json")
            (previous / "backend.json").write_text(json.dumps({**metadata, "runtime_version": "0.1.46+98"}))
        first = self.launch(previous, stable=False)
        thread = fixture.api(first["url"], "api/browser/sessions", {"cwd": str(fixture.root)})["session"]["thread_id"]
        conversation = f"api/browser/sessions/{thread}"
        fixture.api(first["url"], conversation + "/messages", {"message": "one", "requestId": "first-approval"})
        approval = fixture.wait(lambda: fixture.api(first["url"], conversation)["requests"])[0]["key"]
        directory = runtime.runtime_directory(fixture.database)
        predecessor = runtime.read_json(directory / "supervisor.json")["pid"]
        native_pid = int((fixture.root / "provider.pid").read_text())
        staged = self.launch(fixture.resources, "--runtime-confirm-updates")
        self.assertTrue(staged["pending_confirmation"])
        self.assertEqual(fixture.api(staged["edge_url"], conversation)["requests"][0]["key"], approval)
        token = fixture.api(staged["edge_url"], "api/browser/config")["token"]
        record = runtime.discover(fixture.database)
        adoption = runtime.adopt_predecessor(record, "adopt-build-98", staged["pending"])
        self.assertEqual(adoption["update"]["id"], "adopt-build-98")
        def new_supervisor():
            supervisor = runtime.read_json(directory / "supervisor.json")
            return supervisor if supervisor.get("stable_prewarm") == 1 else None
        successor = fixture.wait(new_supervisor, timeout=20)
        self.assertNotEqual(successor["pid"], predecessor)
        stable = self.launch(fixture.resources)
        self.assertEqual(stable["edge_url"], staged["edge_url"])
        self.assertEqual(stable["update"]["id"], "adopt-build-98")
        self.assertEqual(stable["update"]["phase"], "committed")
        self.assertEqual(int((fixture.root / "provider.pid").read_text()), native_pid)
        self.assertEqual(fixture.api(stable["edge_url"], conversation)["requests"][0]["key"], approval)
        self.assertEqual(fixture.api(stable["edge_url"], "api/browser/config")["token"], token)

    def test_origin_and_pending_approval_survive_three_coordinator_changes(self):
        self.exercise_mixed_origin(range(2, 5))

    def exercise_mixed_origin(self, builds):
        fixture = self.fixture
        source = fixture.resources
        executable = fixture.root / "bin/codex"
        provider_fixture = test_desktop_runtime.ROOT / "tests/fixtures/runtime_live_provider.py"
        executable.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(provider_fixture))} codex\n")

        def wire():
            return [json.loads(line) for line in (fixture.root / "provider.jsonl").read_text().splitlines()]

        first = self.launch(source)
        address = first["edge_url"]
        observer = {runtime_edge.OBSERVER_HEADER: first["edge_observer"]}
        token = fixture.api(address, "api/browser/config")["token"]

        def conversation_for(role):
            thread = fixture.api(address, "api/browser/sessions", {"cwd": str(fixture.root)})["session"]["thread_id"]
            path = f"api/browser/sessions/{thread}"
            fixture.api(address, path + "/messages", {"message": role, "requestId": "start-" + role})
            return thread, path

        _, conversation = conversation_for("approval")
        approval = fixture.wait(lambda: fixture.api(address, conversation)["requests"])[0]["key"]
        streams = dict(conversation_for("stream") for _ in range(5))
        def streaming_turns():
            records = {row["thread"]: (row["pid"], row["turn"]) for row in wire()
                       if row["kind"] == "turn" and row["role"] == "stream"}
            return records if len(records) == len(streams) else None
        original = fixture.wait(streaming_turns)
        self.assertEqual(original.keys(), streams.keys())
        for thread, path in streams.items():
            fixture.wait(lambda: fixture.api(address, path)["activeTurn"] == original[thread][1])
        initial_epoch = runtime.control(runtime.discover(fixture.database), "status")["transport"]["epoch"]
        edge = runtime_edge.read_address(runtime.runtime_directory(fixture.database))
        cutovers = []
        components = []
        sent_messages, send_samples, offered = {}, [], []
        rotation = tuple(streams.items())
        for build in builds:
            resources = fixture.root / f"Build {build}/Resources"
            shutil.copytree(source, resources)
            metadata = runtime.read_json(resources / "backend.json")
            (resources / "backend.json").write_text(json.dumps({**metadata, "runtime_version": f"1+{build}"}))
            staged = self.launch(resources, "--runtime-confirm-updates")
            self.assertEqual(staged["edge_url"], address)
            record = runtime.discover(fixture.database)
            stop = threading.Event()
            started = threading.Event()

            def crossing(thread, path, message=None):
                began = time.monotonic()
                timings = {"started_at": began}
                body = ({"message": message, "requestId": message, "expectedTurnId": original[thread][1]}
                        if message else None)
                code, response = request(address, "/" + path + ("/messages" if body else ""),
                    body=json.dumps(body).encode() if body else None,
                    headers={"Content-Type": "application/json", runtime_edge.TOKEN_HEADER: token} if body else None,
                    timings=timings)
                return code, response, body, thread, path, {**timings, "total_ms": (time.monotonic() - began) * 1000}

            def schedule(pool):
                samples = []
                frequency = 10
                origin = time.monotonic()
                for sample in range(12 * frequency):
                    if stop.wait(max(0, origin + sample / frequency - time.monotonic())):
                        break
                    thread, path = rotation[sample % len(rotation)]
                    samples.append(pool.submit(crossing, thread, path, f"steer-{build}-{sample}"))
                    if sample % 2 == 0:
                        samples.append(pool.submit(crossing, thread, path))
                    started.set()
                return [sample.result(timeout=5) for sample in samples]

            with ThreadPoolExecutor(max_workers=32) as pool:
                traffic = pool.submit(schedule, pool)
                try:
                    self.assertTrue(started.wait(2))
                    runtime.control(record, "activate-update", request_id=f"edge-{build}", payload=staged["pending"])
                    def serving():
                        code, data = request(address, runtime_edge.HEALTH, headers=observer)
                        value = json.loads(data)
                        if code == 200 and value.get("status") == "serving" and value.get("payload") == staged["pending"]:
                            return value
                    health = fixture.wait(serving, timeout=10)
                    time.sleep(.35)  # Include resumed traffic at the same offered rate.
                finally:
                    stop.set()
                samples = traffic.result(timeout=6)
            failures = [(code, response.decode()) for code, response, *_ in samples if code != 200]
            self.assertEqual(failures, [])
            offers = Counter("send" if body else "open" for _, _, body, *_ in samples)
            self.assertGreaterEqual(offers["send"], 5)
            self.assertEqual(offers["open"], math.ceil(offers["send"] / 2))
            offered.append(dict(offers))
            state = health["update"]
            for _, response, body, thread, path, timings in samples:
                value = json.loads(response)
                if body:
                    self.assertEqual(value["acceptedMessage"]["turnId"], original[thread][1])
                    sent_messages[body["requestId"]] = (thread, path, body)
                    send_samples.append({**timings, "switch": build,
                        "relative_to_fence_ms": (timings["started_at"] - state["edge_admission_closed_at"]) * 1000})
                else:
                    self.assertEqual(value["activeTurn"], original[thread][1])
            self.assertEqual(health["edge_instance"], edge["instance"])
            self.assertEqual(health["update"]["phase"], "committed")
            cutovers.append(health["edge_handover_ms"])
            timestamps = [state[key] for key in ("edge_admission_closed_at", "edge_admission_drained_at",
                "edge_projection_saved_at", "edge_source_exited_at", "edge_snapshot_finished_at",
                "edge_candidate_ready_at")]
            intervals = [round((end - start) * 1000, 2) for start, end in zip(timestamps, timestamps[1:])]
            components.append(dict(zip(("drain_ms", "projection_ms", "source_exit_ms", "snapshot_ms",
                                        "candidate_ready_ms"), intervals)))
            self.assertEqual(fixture.api(address, "api/browser/config")["token"], token)
            current = runtime.control(runtime.discover(fixture.database), "status")
            self.assertEqual(current["transport"]["epoch"], initial_epoch + build - 1)
            self.assertEqual(fixture.api(address, conversation)["requests"][0]["key"], approval)
            latest_output = [row for row in wire() if row["kind"] == "output"
                             and state["edge_admission_closed_at"] <= row["time"]]
            for thread, (pid, turn) in original.items():
                output = [row for row in latest_output if row["thread"] == thread]
                self.assertTrue(output, f"Stream {thread} produced no output after fence {build}")
                self.assertEqual({(row["pid"], row["turn"]) for row in output}, {(pid, turn)})

        # A retry of the first accepted send after all upgrades must resolve
        # from the durable receipt without a second native request.
        _, path, body = next(iter(sent_messages.values()))
        self.assertEqual(fixture.api(address, path + "/messages", body)["acceptedMessage"]["message"], body["message"])
        fixture.api(address, conversation + "/requests/" + approval, {"decision": "accept"})
        (fixture.root / "release").touch()
        fixture.wait(lambda: sum(row["kind"] == "complete" for row in wire()) == 6)
        self.assertEqual(sum(row["kind"] == "approval_answer" for row in wire()), 1)
        deliveries = [(row["pid"], call["threadId"], call["expectedTurnId"], call["input"][0]["text"])
                      for row in wire() if row["kind"] == "input" and row["message"].get("method") == "turn/steer"
                      for call in (row["message"]["params"],)]
        self.assertCountEqual(deliveries, [(original[thread][0], thread, original[thread][1], message)
                                          for message, (thread, _, _) in sent_messages.items()])
        with closing(sqlite3.connect(fixture.database.as_uri() + "?mode=ro", uri=True)) as database:
            rows = database.execute("SELECT request_id, thread_id, state FROM browser_submission_receipts "
                                    "WHERE request_id LIKE 'steer-%'").fetchall()
        self.assertCountEqual(rows, [(message, thread, "completed")
                                     for message, (thread, _, _) in sent_messages.items()])
        for cutover in cutovers:
            self.assertGreater(cutover, 0)
        send_samples.sort(key=lambda sample: sample["total_ms"])
        send_p99 = send_samples[math.ceil(.99 * len(send_samples)) - 1]["total_ms"]
        if report := os.environ.get("RIBBON_EDGE_REPORT"):
            Path(report).write_text(json.dumps({"cutover_ms": cutovers, "accepted": len(sent_messages),
                "provider_deliveries": len(deliveries), "http_failures": 0, "streams": len(streams),
                "send_p99_ms": send_p99, "slowest_sends": send_samples[-5:],
                "offered": offered, "phases": components}, indent=2) + "\n")
        if os.environ.get("RIBBON_REQUIRE_EDGE_BUDGET") == "1":
            self.assertLess(max(cutovers), 250, f"Coordinator handover milliseconds: {cutovers}")
            self.assertLess(send_p99, 150, "Send latency includes waiting for coordinator admission")

    def test_supervisor_restores_a_lost_listener_at_its_original_origin(self):
        fixture = self.fixture
        first = self.launch(fixture.resources)
        directory = runtime.runtime_directory(fixture.database)
        edge = runtime_edge.read_address(directory)
        token = fixture.api(edge["url"], "api/browser/config")["token"]
        os.kill(edge["pid"], signal.SIGTERM)
        def resumed():
            record = runtime_edge.read_address(directory)
            if record["instance"] != edge["instance"]:
                try:
                    observed = runtime_edge.probe(record)
                except (OSError, HTTPException):
                    return None
                if observed and observed.get("status") == "serving":
                    return record
            return None
        replacement = fixture.wait(resumed, timeout=10)
        self.assertEqual(replacement["url"], first["edge_url"])
        self.assertEqual(replacement["observer"], first["edge_observer"])
        self.assertEqual(fixture.api(replacement["url"], "api/browser/config")["token"], token)
