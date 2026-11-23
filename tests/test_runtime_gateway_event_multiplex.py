"""The staged predecessor and continuity receipts share one local EventSource."""
from contextlib import suppress
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from io import BytesIO
import queue
import socket
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))
from agent_coord.runtime_interaction_gateway import GatewayHandler, _ProviderEventFrames


class SourceHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        source = self.server
        source.cursor = self.headers.get("Last-Event-ID")
        source.requested.set()
        if not source.allow_headers.wait(5):
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.connection.settimeout(2)
        try:
            while True:
                try:
                    chunk = source.chunks.get(timeout=.2)
                except queue.Empty:
                    if source.stopped.is_set():
                        return
                    continue
                if chunk is None:
                    return
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError, queue.Empty):
            return


class LocalHandler(GatewayHandler):
    def do_GET(self):
        if self.path == "/fixture":
            return self.answer(200, b"<title>Receipt observation</title>", "text/html")
        return super().do_GET()


class GatewayMultiplexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = self.server(SourceHandler)
        self.source.chunks = queue.Queue()
        self.source.requested = threading.Event()
        self.source.stopped = threading.Event()
        self.source.allow_headers = threading.Event()
        self.gateway = self.server(LocalHandler)
        self.owner = SimpleNamespace(directory=Path(self.temp.name), stop=threading.Event(),
                                     condition=threading.Condition(), revision=0, fence={},
                                     backend=(f"http://127.0.0.1:{self.source.server_port}/",),
                                     upstream_streams=set(), url=f"http://127.0.0.1:{self.gateway.server_port}/")
        self.owner.receipt_signal = lambda previous, current: {"revision": current}
        self.owner.verify_origin = lambda _backend: None
        self.owner.staged_source = lambda: not self.owner.fence
        self.gateway.gateway = self.owner
        self.addCleanup(self.stop_sources)

    def server(self, handler):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02})
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def stop_sources(self):
        self.owner.stop.set()
        self.source.stopped.set()
        self.source.allow_headers.set()
        self.source.chunks.put(None)
        with self.owner.condition:
            self.owner.condition.notify_all()

    def observe(self, cursor="5:3"):
        connection = HTTPConnection("127.0.0.1", self.gateway.server_port, timeout=3)
        self.addCleanup(connection.close)
        connection.request("GET", "/api/browser/events?after=1", headers={"Last-Event-ID": cursor})
        response = connection.getresponse()
        self.addCleanup(response.close)
        self.assertEqual(response.status, 200)
        self.assertEqual(self.frame(response), b"retry: 250\n\n")
        return response

    def revision(self):
        with self.owner.condition:
            self.owner.revision += 1
            self.owner.condition.notify_all()
        return self.owner.revision

    @staticmethod
    def frame(response):
        result = bytearray()
        while True:
            line = response.readline()
            result.extend(line)
            if line in (b"\n", b"\r\n", b""):
                return bytes(result)

    def signal(self, response, revision):
        self.assertEqual(self.frame(response), f'event: update-submission-change\ndata: {{"revision":{revision}}}\n\n'.encode())

    def no_upstream(self):
        limit = time.monotonic() + 4
        while time.monotonic() < limit:
            with self.owner.condition:
                if not self.owner.upstream_streams and not any(t.name == "rf-staged-event-reader" for t in threading.enumerate()):
                    break
            time.sleep(.02)
        self.assertFalse(self.owner.upstream_streams)
        self.assertFalse(any(t.is_alive() and t.name == "rf-staged-event-reader" for t in threading.enumerate()))

    def test_draining_serving_candidate_streams_provider_and_receipt_events(self):
        self.owner.fence = {"state": "draining"}
        self.owner.backend = (*self.owner.backend, "token", "candidate-instance", "/candidate")
        self.owner.terminal = {"instance": "candidate-instance", "payload": "/candidate"}
        self.owner.payload = "/candidate"
        self.owner.receiver = SimpleNamespace(instance="candidate-instance", payload="/candidate")
        self.source.allow_headers.set()
        response = self.observe()
        self.signal(response, 0)
        self.assertTrue(self.source.requested.wait(1))
        frame = b"id: 17:4\ndata: completed\n\n"
        self.source.chunks.put(frame)
        self.assertEqual(self.frame(response), frame)
        self.signal(response, self.revision())

    def test_stalled_headers_and_partial_provider_frame_do_not_hold_receipts_or_replace_cursor(self):
        response = self.observe()
        self.signal(response, 0)
        self.assertTrue(self.source.requested.wait(2))
        self.assertEqual(self.source.cursor, "5:3")
        started = time.monotonic()
        self.signal(response, self.revision())
        self.assertLess(time.monotonic() - started, 1)
        self.source.allow_headers.set()
        first = b"id: 7:3\r\ndata: provider"
        self.source.chunks.put(first)
        self.signal(response, self.revision())
        self.source.chunks.put(b" resumed\r")
        self.source.chunks.put(b"\n\r")
        self.source.chunks.put(b"\n")
        self.assertEqual(self.frame(response), first + b" resumed\r\n\r\n")
        self.source.chunks.put(b"id: 8:3\ndata: incomplete")
        self.source.chunks.put(None)
        self.assertEqual(response.read(), b"")
        self.no_upstream()

    def test_large_incomplete_frame_spills_privately_and_is_forwarded_byte_for_byte(self):
        rolled = threading.Event()
        original = tempfile.SpooledTemporaryFile
        class ObservedSpool(original):
            def rollover(self):
                super().rollover()
                rolled.set()
        with patch.object(tempfile, "SpooledTemporaryFile", ObservedSpool):
            response = self.observe()
            self.signal(response, 0)
            self.source.allow_headers.set()
            prefix = b"id: 11:4\ndata: large provider " + b"x" * (2 * 1024 * 1024)
            self.source.chunks.put(prefix)
            self.assertTrue(rolled.wait(3))
            self.signal(response, self.revision())
            self.source.chunks.put(b"\n\n")
            self.assertEqual(self.frame(response), prefix + b"\n\n")
            self.source.chunks.put(None)
            self.assertEqual(response.read(), b"")
            self.no_upstream()

    def test_overflow_streams_complete_frames_without_unbounded_private_storage(self):
        spool, client = BytesIO(), BytesIO()
        frames = _ProviderEventFrames(spool, client, lambda: True)
        frames.spool_limit = 50
        tokens = [b"id: 91:4\r\n", b"data: " + b"x" * 20, b"y" * 60, b"z" * 60, b"\r", b"\n\r", b"\n"]
        for token in tokens:
            frames.receive(token)
            self.assertLessEqual(len(spool.getvalue()), frames.spool_limit)
        self.assertEqual(client.getvalue(), b"".join(tokens))
        self.assertFalse(frames.streaming)

    def test_cutover_during_backpressured_frame_flush_preserves_last_complete_cursor(self):
        complete = b"id: 5:2\ndata: " + b"x" * (_ProviderEventFrames.block_size * 4)
        active = threading.Event()
        active.set()
        class Cutover(BytesIO):
            def write(self, data):
                result = super().write(data)
                active.clear()
                return result
        spool, client = BytesIO(), Cutover()
        frames = _ProviderEventFrames(spool, client, active.is_set)
        frames.receive(complete)
        with self.assertRaises(ConnectionAbortedError):
            frames.receive(b"\n\n")
        self.assertEqual(client.getvalue(), complete[:_ProviderEventFrames.block_size])
        self.assertNotIn(b"\n\n", client.getvalue())

    def test_stalled_overflow_reconnects_at_last_complete_provider_cursor(self):
        self.source.allow_headers.set()
        with patch.object(_ProviderEventFrames, "spool_limit", 50):
            response = self.observe()
            self.signal(response, 0)
            self.source.chunks.put(b"id: 9:3\ndata: first\n\n")
            self.assertEqual(self.frame(response), b"id: 9:3\ndata: first\n\n")
            prefix = b"id: 11:4\ndata: " + b"z" * 70
            self.source.chunks.put(prefix)
            self.assertEqual(response.read(len(prefix)), prefix)
            self.revision()
            self.assertEqual(response.read(), b"")
            self.no_upstream()
            resumed = self.observe("9:3")
            self.signal(resumed, self.owner.revision)

    def test_cutover_releases_idle_source_and_offline_stream_retains_signal(self):
        self.source.allow_headers.set()
        response = self.observe()
        self.signal(response, 0)
        self.assertTrue(self.source.requested.wait(2))
        with self.owner.condition:
            self.owner.fence = {"state": "prepared"}
            # Same fence used by the production coordinator.
            for upstream in self.owner.upstream_streams:
                with suppress(OSError):
                    upstream.shutdown(socket.SHUT_RDWR)
            self.owner.condition.notify_all()
        self.assertEqual(response.read(), b"")
        self.no_upstream()
        offline = self.observe("11:4")
        self.signal(offline, self.owner.revision)
        self.signal(offline, self.revision())

    def test_blocked_downstream_disconnect_releases_reader_and_source_socket(self):
        self.source.allow_headers.set()
        response = self.observe()
        self.signal(response, 0)
        self.assertTrue(self.source.requested.wait(2))
        frame = b"id: 12:4\ndata: " + b"x" * (512 * 1024) + b"\n\n"
        for _ in range(48):
            self.source.chunks.put(frame)
        # Do not consume the provider output. A reset or expired write timeout
        # must free any reader blocked on upstream input or its bounded queue.
        response.close()
        self.no_upstream()
