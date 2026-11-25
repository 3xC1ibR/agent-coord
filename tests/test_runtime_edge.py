"""Unit and live cutover benchmark tests for the permanent loopback edge proxy."""
from __future__ import annotations

from contextlib import closing
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/agent-coord/scripts"))
from agent_coord import desktop_runtime as runtime  # noqa: E402
from agent_coord.local_http import LocalHTTPServer  # noqa: E402
from agent_coord.runtime_edge import EdgeProxy  # noqa: E402
from test_desktop_runtime import write_fake_provider  # noqa: E402


class _UpstreamStub:
    def __init__(self, csrf_token: str = "upstream-csrf-1"):
        self.csrf_token = csrf_token
        self.hang = False
        self.drop_posts = False
        self.return_handover_503 = False
        self.requests: list[tuple[str, str, dict[str, str], bytes]] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def _handle(self):
                if stub.hang:
                    time.sleep(0.8)
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length) if length > 0 else b""
                if stub.drop_posts and self.command == "POST":
                    self.connection.close()
                    return
                if stub.return_handover_503 and self.command in {"POST", "PATCH"}:
                    stub.return_handover_503 = False
                    raw = json.dumps({"error": "Runtime maintenance is pausing new work. Try again shortly."}).encode()
                    self.send_response(503)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                stub.requests.append((self.command, self.path, dict(self.headers), body))
                if self.path == "/api/browser/config":
                    payload = json.dumps({"token": stub.csrf_token, "upstreamPort": stub.port}).encode()
                    try:
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(payload)))
                        self.end_headers()
                        if self.command != "HEAD":
                            self.wfile.write(payload)
                    except OSError:
                        pass
                    return
                if self.path == "/api/browser/events":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    self.wfile.write(b"id: 1\ndata: {\"seq\":1}\n\n")
                    self.wfile.flush()
                    return
                payload = json.dumps({
                    "method": self.command,
                    "path": self.path,
                    "receivedToken": self.headers.get("X-Agent-Coord-Token"),
                    "receivedHost": self.headers.get("Host"),
                    "receivedOrigin": self.headers.get("Origin"),
                    "body": body.decode("utf-8") if body else "",
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Security-Policy", f"frame-ancestors http://127.0.0.1:{stub.port}")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(payload)

            do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _handle

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()

    def close(self):
        self.hang = False
        self.server.shutdown()
        self.thread.join(timeout=3)
        self.server.server_close()


class EdgeProxyProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="ribbon-edge-unit-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.runtime_dir = self.root / ".state.sqlite3.runtime"
        self.runtime_dir.mkdir(parents=True)

    def _start_edge(self, preferred_port: int = 0) -> EdgeProxy:
        edge = EdgeProxy(self.runtime_dir, str(self.root / "state.sqlite3"), preferred_port, "edge-stable-csrf")
        ready_r, ready_w = os.pipe()
        thread = threading.Thread(target=edge.serve, args=(ready_w,), daemon=True)
        thread.start()
        self.assertEqual(os.read(ready_r, 16), b"ready\n")
        os.close(ready_r)

        def cleanup():
            edge.stopping.set()
            edge.server.shutdown()
            thread.join(timeout=3)
            edge.server.server_close()

        self.addCleanup(cleanup)
        return edge

    def _set_route(self, instance: str, port: int, csrf_token: str, state: str = "serving", paused: bool = False):
        runtime.atomic_json(self.runtime_dir / "edge-route.json", {
            "state": state,
            "instance": instance,
            "upstream_port": port,
            "csrf_token": csrf_token,
            "paused": paused,
            "health": {"instance": instance, "pid": os.getpid(), "payload": "/payload"},
            "updated_at": time.monotonic(),
        })

    def test_port_zero_fallback_when_preferred_port_is_in_use(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen(1)
            busy_port = occupied.getsockname()[1]
            edge = self._start_edge(preferred_port=busy_port)
            self.assertNotEqual(edge.port, busy_port)
            self.assertGreater(edge.port, 0)

    def test_all_http_methods_host_validation_csp_rewrite_and_csrf_translation(self):
        upstream_v1 = _UpstreamStub("v1-secret-csrf")
        self.addCleanup(upstream_v1.close)
        edge = self._start_edge()
        self._set_route("v1", upstream_v1.port, upstream_v1.csrf_token)

        # Config rewrites upstream token to edge's stable token on both 127.0.0.1 and localhost.
        for host_name in ("127.0.0.1", "localhost"):
            with closing(http.client.HTTPConnection("127.0.0.1", edge.port, timeout=5)) as conn:
                conn.request("GET", "/api/browser/config", headers={"Host": f"{host_name}:{edge.port}"})
                resp = conn.getresponse()
                data = json.loads(resp.read())
                self.assertEqual(resp.status, 200)
                self.assertEqual(data["token"], "edge-stable-csrf")

        # Invalid Host and X-Forwarded-For are rejected with 403.
        with closing(http.client.HTTPConnection("127.0.0.1", edge.port, timeout=5)) as conn:
            conn.request("GET", "/api/browser/config", headers={"Host": "evil.example:80"})
            self.assertEqual(conn.getresponse().status, 403)
        with closing(http.client.HTTPConnection("127.0.0.1", edge.port, timeout=5)) as conn:
            conn.request("GET", "/api/browser/config",
                         headers={"Host": f"127.0.0.1:{edge.port}", "X-Forwarded-For": "1.2.3.4"})
            self.assertEqual(conn.getresponse().status, 403)

        # Missing or wrong CSRF token on mutating methods is rejected with 403; /api/external-work is allowed.
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with closing(http.client.HTTPConnection("127.0.0.1", edge.port, timeout=5)) as conn:
                conn.request(method, "/api/browser/integrations/stoic", body=b"{}",
                             headers={"Host": f"127.0.0.1:{edge.port}", "Content-Type": "application/json",
                                      "Content-Length": "2", "X-Agent-Coord-Token": "wrong"})
                self.assertEqual(conn.getresponse().status, 403, method)

        with closing(http.client.HTTPConnection("127.0.0.1", edge.port, timeout=5)) as conn:
            conn.request("POST", "/api/external-work", body=b'{"task":"ping"}',
                         headers={"Host": f"127.0.0.1:{edge.port}", "Content-Type": "application/json",
                                  "Content-Length": "15", "Authorization": "Bearer ext-token"})
            self.assertEqual(conn.getresponse().status, 200)

        # Verify GET, HEAD, POST, PUT, PATCH, DELETE, OPTIONS all forward and translate CSRF across v1 -> v2!
        upstream_v2 = _UpstreamStub("v2-secret-csrf")
        self.addCleanup(upstream_v2.close)
        self._set_route("v2", upstream_v2.port, upstream_v2.csrf_token)

        for method in ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
            body = b'{"enabled":true}' if method in {"POST", "PUT", "PATCH", "DELETE"} else None
            headers = {
                "Host": f"localhost:{edge.port}",
                "Origin": f"http://localhost:{edge.port}",
                "X-Agent-Coord-Token": "edge-stable-csrf",
            }
            if body is not None:
                headers["Content-Type"] = "application/json"
                headers["Content-Length"] = str(len(body))
            with closing(http.client.HTTPConnection("127.0.0.1", edge.port, timeout=5)) as conn:
                conn.request(method, "/api/browser/integrations/stoic", body=body, headers=headers)
                resp = conn.getresponse()
                raw = resp.read()
                self.assertEqual(resp.status, 200, method)
                self.assertIn(f"http://localhost:{edge.port}", resp.getheader("Content-Security-Policy", ""))
                if method != "HEAD":
                    parsed = json.loads(raw)
                    self.assertEqual(parsed["method"], method)
                    self.assertEqual(parsed["receivedToken"], "v2-secret-csrf")
                    self.assertEqual(parsed["receivedOrigin"], f"http://localhost:{upstream_v2.port}")

    def test_pre_admission_socket_close_and_handover_503_retry_transparently_onto_v2(self):
        upstream_v1 = _UpstreamStub("v1-csrf")
        upstream_v2 = _UpstreamStub("v2-csrf")
        self.addCleanup(upstream_v1.close)
        self.addCleanup(upstream_v2.close)
        edge = self._start_edge()
        upstream_v1.drop_posts = True
        self._set_route("v1", upstream_v1.port, upstream_v1.csrf_token)

        def flip_to_v2():
            time.sleep(0.03)
            self._set_route("v2", upstream_v2.port, upstream_v2.csrf_token)

        threading.Thread(target=flip_to_v2, daemon=True).start()
        with closing(http.client.HTTPConnection("127.0.0.1", edge.port, timeout=5)) as conn:
            conn.request("POST", "/api/browser/sessions/t1/messages", body=b'{"message":"hi"}',
                         headers={"Host": f"127.0.0.1:{edge.port}", "Content-Type": "application/json",
                                  "Content-Length": "16", "X-Agent-Coord-Token": "edge-stable-csrf"})
            resp = conn.getresponse()
            parsed = json.loads(resp.read())
            self.assertEqual(resp.status, 200)
            self.assertEqual(parsed["receivedToken"], "v2-csrf")

        # Also test transient handover 503 retry onto v1/v2 when not explicitly paused.
        upstream_v2.return_handover_503 = True
        with closing(http.client.HTTPConnection("127.0.0.1", edge.port, timeout=5)) as conn:
            conn.request("PATCH", "/api/browser/integrations/stoic", body=b'{"enabled":false}',
                         headers={"Host": f"127.0.0.1:{edge.port}", "Content-Type": "application/json",
                                  "Content-Length": "17", "X-Agent-Coord-Token": "edge-stable-csrf"})
            resp = conn.getresponse()
            resp.read()
            self.assertEqual(resp.status, 200)

    def test_health_endpoint_sub_millisecond_and_deadlock_detection(self):
        upstream = _UpstreamStub("v1-csrf")
        self.addCleanup(upstream.close)
        edge = self._start_edge()
        self._set_route("v1", upstream.port, upstream.csrf_token)

        durations_ms = []
        for _ in range(40):
            start = time.perf_counter()
            with closing(http.client.HTTPConnection("127.0.0.1", edge.port, timeout=2)) as conn:
                conn.request("GET", "/_runtime/health", headers={"Host": f"127.0.0.1:{edge.port}"})
                resp = conn.getresponse()
                body = json.loads(resp.read())
                self.assertEqual(resp.status, 200)
                self.assertEqual(body["status"], "serving")
            durations_ms.append((time.perf_counter() - start) * 1000.0)
        durations_ms.sort()
        self.assertLess(durations_ms[len(durations_ms) // 2], 1.0, f"Median health latency {durations_ms[len(durations_ms)//2]:.3f} ms >= 1 ms")

        # Hang upstream and verify background probe marks instance unavailable within ~1s.
        upstream.hang = True
        deadline = time.monotonic() + 2.5
        status_code = 200
        while time.monotonic() < deadline:
            with closing(http.client.HTTPConnection("127.0.0.1", edge.port, timeout=2)) as conn:
                conn.request("GET", "/_runtime/health", headers={"Host": f"127.0.0.1:{edge.port}"})
                resp = conn.getresponse()
                resp.read()
                status_code = resp.status
                if status_code == 503:
                    break
            time.sleep(0.05)
        self.assertEqual(status_code, 503)


LIVE_STREAM_PROVIDER = r'''
import copy, json, os, pathlib, sys, threading, time
root = pathlib.Path(os.environ['RIBBON_FAKE_ROOT'])
(root / 'provider.pid').write_text(str(os.getpid()))
lock = threading.RLock()
threads = {}
counter = 0
def emit(value):
    with lock:
        print(json.dumps(value), flush=True)
def stream_work(identity, turn):
    idx = 0
    while not (root / 'release-held-work').exists():
        idx += 1
        emit({'method':'item/agentMessage/delta','params':{'threadId':identity,'turnId':turn['id'],'itemId':'out','delta':f'chunk-{idx} '}})
        time.sleep(0.05)
    item = {'id':'out','type':'agentMessage','text':'done'}
    turn['items'].append(item)
    turn['status'] = 'completed'
    emit({'method':'item/completed','params':{'threadId':identity,'turnId':turn['id'],'item':item}})
    emit({'method':'turn/completed','params':{'threadId':identity,'turn':copy.deepcopy(turn)}})
for line in sys.stdin:
    req = json.loads(line)
    if 'id' not in req:
        continue
    method, p = req.get('method'), req.get('params') or {}
    identity = p.get('threadId', 'fake-thread')
    result = {}
    if method == 'thread/start':
        threads[identity] = {'id':identity,'cwd':p['cwd'],'turns':[]}
    if method in ('thread/start','thread/resume','thread/read'):
        threads.setdefault(identity, {'id':identity,'cwd':p.get('cwd',str(root)),'turns':[]})
        result = {'thread':copy.deepcopy(threads[identity]),'model':'fake-model','reasoningEffort':'low'}
    if method == 'model/list':
        result = {'data':[]}
    if method == 'turn/start':
        counter += 1
        turn = {'id':f'turn-{counter}','status':'inProgress','items':[{'id':'u','type':'userMessage','content':p['input']}]}
        threads[identity]['turns'].append(turn)
        emit({'id':req['id'],'result':{'turn':copy.deepcopy(turn)}})
        emit({'method':'turn/started','params':{'threadId':identity,'turn':copy.deepcopy(turn)}})
        threading.Thread(target=stream_work, args=(identity, turn), daemon=True).start()
        continue
    if method == 'turn/steer':
        emit({'id':req['id'],'result':{'steered':True}})
        continue
    emit({'id':req['id'],'result':result})
'''


class LiveTripleUpgradeBenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="ribbon-edge-bench-")
        self.root = Path(self.temporary.name).resolve()
        self.database = self.root / "state.sqlite3"
        self.resources = self.root / "Gen1/Contents/Resources"
        self.resources.mkdir(parents=True)
        spec = importlib.util.spec_from_file_location("edge_bench_build", ROOT / "desktop/macos/build.py")
        build = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(build)
        build.copy_backend(self.resources)
        shutil.copyfile(ROOT / "desktop/macos/backend.py", self.resources / "backend.py")
        (self.resources / "backend.json").write_text(json.dumps({
            "python": sys.executable, "database": None, "path": None,
            **runtime.compatibility_metadata("1.0.0+1"),
        }))
        tools = self.root / "bin"
        tools.mkdir()
        (tools / "codex").write_text(
            "#!/bin/sh\n"
            f"'''exec' \"{sys.executable}\" \"$0\" \"$@\"\n"
            "' '''\n" + LIVE_STREAM_PROVIDER
        )
        (tools / "codex").chmod(0o755)
        self.env = dict(
            os.environ,
            PYTHONPATH=str(self.resources / "backend"),
            PATH=str(tools) + os.pathsep + os.environ["PATH"],
            RIBBON_FAKE_ROOT=str(self.root),
            HOME=str(self.root),
            PYTHONWARNINGS="error::ResourceWarning",
        )

    def tearDown(self):
        try:
            (self.root / "release-held-work").touch()
            directory = runtime.runtime_directory(self.database)
            discovery = directory / "discovery.json"
            if discovery.exists():
                try:
                    runtime.control(runtime.read_json(discovery), "stop", timeout=8)
                except (OSError, ValueError, RuntimeError):
                    pass
            try:
                runtime.stop_retained_transport(self.database)
            except (OSError, ValueError, RuntimeError):
                pass
            deadline = time.monotonic() + 5
            while not runtime.owner_available(directory) and time.monotonic() < deadline:
                time.sleep(0.05)
            for name in ("supervisor.json", "discovery.json", "runtime-edge/owner.json", "provider-transport/owner.json"):
                info_path = directory / name
                if info_path.exists():
                    try:
                        info = runtime.read_json(info_path)
                        for key in ("pid", "coordinator_pid"):
                            pid = info.get(key)
                            if isinstance(pid, int) and pid > 1:
                                try:
                                    os.kill(pid, signal.SIGKILL)
                                except OSError:
                                    pass
                    except (OSError, ValueError):
                        pass
            time.sleep(0.05)
        finally:
            shutil.rmtree(self.temporary.name, ignore_errors=True)
            self.temporary.cleanup()

    def _bootstrap(self, resources_dir: Path, *, confirm: bool = False) -> dict:
        cmd = [sys.executable, "-B", str(resources_dir / "backend.py"), "--runtime-bootstrap",
               "--resources", str(resources_dir), "--db", str(self.database)]
        if confirm:
            cmd.append("--runtime-confirm-updates")
        proc = subprocess.run(cmd, env=dict(self.env, PYTHONPATH=str(resources_dir / "backend")),
                              text=True, capture_output=True, timeout=40)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("ResourceWarning", proc.stderr)
        return json.loads(proc.stdout)

    def _http_json(self, edge_url: str, method: str, path: str, token: str = "", body: dict | None = None) -> tuple[int, dict]:
        headers = {}
        if token:
            headers["X-Agent-Coord-Token"] = token
        raw = None
        if body is not None:
            raw = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(edge_url + path, data=raw, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as err:
            detail = err.read().decode("utf-8", errors="replace")[:500]
            err.close()
            raise RuntimeError(f"HTTP {err.code} on {method} {path}: {detail}") from None

    def test_three_consecutive_live_upgrades_zero_errors_and_sub_250ms_cutover(self):
        first = self._bootstrap(self.resources)
        edge_url = first["url"]
        _, config = self._http_json(edge_url, "GET", "api/browser/config")
        stable_token = config["token"]

        _, created = self._http_json(edge_url, "POST", "api/browser/sessions", stable_token,
                                     {"cwd": str(self.root), "client": "codex", "name": "Triple Upgrade Stream"})
        thread_id = created["session"]["thread_id"]
        self._http_json(edge_url, "POST", f"api/browser/sessions/{thread_id}/messages", stable_token, {"message": "start"})

        deadline = time.monotonic() + 10
        active_turn = None
        while time.monotonic() < deadline:
            _, detail = self._http_json(edge_url, "GET", f"api/browser/sessions/{thread_id}", stable_token)
            if detail.get("activeTurn"):
                active_turn = detail["activeTurn"]
                break
            time.sleep(0.05)
        self.assertIsNotNone(active_turn)
        provider_pid = int((self.root / "provider.pid").read_text())

        stop_traffic = threading.Event()
        samples: list[dict] = []
        samples_lock = threading.Lock()
        sse_events: list[int] = []
        sse_resets = 0

        def sse_worker():
            nonlocal sse_resets
            last_id = None
            batches = 0
            while not stop_traffic.is_set():
                headers = {"X-Agent-Coord-Token": stable_token, "Accept": "text/event-stream"}
                if last_id:
                    headers["Last-Event-ID"] = last_id
                try:
                    req = urllib.request.Request(edge_url + "api/browser/events", headers=headers)
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        event_id = None
                        while not stop_traffic.is_set():
                            line = resp.readline()
                            if not line:
                                break
                            decoded = line.decode("utf-8").rstrip("\n")
                            if decoded.startswith("id: "):
                                event_id = decoded[4:]
                            elif decoded.startswith("data: "):
                                batch = json.loads(decoded[6:])
                                if batches > 0 and batch.get("reset"):
                                    sse_resets += 1
                                batches += 1
                                for ev in batch.get("events", []):
                                    if isinstance(ev.get("seq"), int):
                                        sse_events.append(ev["seq"])
                                if event_id:
                                    last_id = event_id
                except OSError:
                    stop_traffic.wait(0.02)

        def send_worker():
            seq = 0
            while not stop_traffic.is_set():
                seq += 1
                started = time.perf_counter()
                try:
                    code, _ = self._http_json(
                        edge_url, "POST", f"api/browser/sessions/{thread_id}/messages", stable_token,
                        {"message": f"steer-{seq}", "requestId": f"bench-send-{seq}", "expectedTurnId": active_turn},
                    )
                    ok = code == 200
                except Exception as exc:
                    ok = False
                    err = str(exc)
                else:
                    err = None
                ended = time.perf_counter()
                with samples_lock:
                    samples.append({"kind": "send", "ok": ok, "start": started, "end": ended,
                                    "ms": (ended - started) * 1000.0, "error": err})
                stop_traffic.wait(0.06)  # ~15 msg/s

        def open_and_patch_worker():
            seq = 0
            while not stop_traffic.is_set():
                seq += 1
                started = time.perf_counter()
                try:
                    if seq % 5 == 0:
                        code, _ = self._http_json(edge_url, "GET", "api/browser/integrations/stoic", stable_token)
                    else:
                        code, detail = self._http_json(edge_url, "GET", f"api/browser/sessions/{thread_id}", stable_token)
                        code = 200 if code == 200 and detail.get("thread", {}).get("id") == thread_id else 500
                    ok = code == 200
                except Exception as exc:
                    ok = False
                    err = str(exc)
                else:
                    err = None
                ended = time.perf_counter()
                with samples_lock:
                    samples.append({"kind": "open", "ok": ok, "start": started, "end": ended,
                                    "ms": (ended - started) * 1000.0, "error": err})
                stop_traffic.wait(0.12)  # ~8 opens/s

        workers = [
            threading.Thread(target=sse_worker, daemon=True),
            threading.Thread(target=send_worker, daemon=True),
            threading.Thread(target=open_and_patch_worker, daemon=True),
        ]
        for worker in workers:
            worker.start()

        seen_pids = [first["pid"]]
        cutover_wall_ms: list[float] = []
        cutover_windows: list[tuple[float, float]] = []
        edge_route_path = runtime.runtime_directory(self.database) / "edge-route.json"

        for gen in (2, 3, 4):
            cand_dir = self.root / f"Gen{gen}/Contents/Resources"
            shutil.copytree(self.resources, cand_dir)
            meta = json.loads((cand_dir / "backend.json").read_text())
            meta["runtime_version"] = f"1.0.{gen}+{gen}"
            (cand_dir / "backend.json").write_text(json.dumps(meta))
            (cand_dir / f"gen-{gen}.txt").write_text(str(gen))

            staged = self._bootstrap(cand_dir, confirm=True)
            self.assertEqual(staged["url"], edge_url)
            record = runtime.discover(self.database)
            runtime.control(record, "activate-update", request_id=f"bench-upgrade-{gen}", payload=staged["pending"])

            cutover_start = None
            commit_done = None
            wait_end = time.monotonic() + 25
            measured_route_cutover_ms = None
            while time.monotonic() < wait_end:
                try:
                    route_doc = json.loads(edge_route_path.read_bytes())
                    if route_doc.get("state") == "cutover" and cutover_start is None:
                        cutover_start = time.perf_counter()
                    elif (route_doc.get("state") == "serving"
                          and route_doc.get("health", {}).get("pid") not in seen_pids
                          and route_doc.get("health", {}).get("pid") is not None):
                        commit_done = time.perf_counter()
                        measured_route_cutover_ms = route_doc.get("last_cutover_ms")
                        break
                except (OSError, ValueError):
                    pass
                time.sleep(0.002)

            self.assertIsNotNone(commit_done, f"Upgrade {gen} did not switch edge route to new instance")
            current_status = None
            status_end = time.monotonic() + 10
            while time.monotonic() < status_end:
                try:
                    disc = runtime.discover(self.database, timeout=1)
                    if disc and disc["pid"] not in seen_pids:
                        st = runtime.control(disc, "status", timeout=2)
                        if st.get("status") == "serving" and (st.get("update") or {}).get("phase") == "committed":
                            current_status = st
                            break
                except (OSError, ValueError, RuntimeError):
                    pass
                time.sleep(0.01)

            self.assertIsNotNone(current_status, f"Upgrade {gen} did not commit")
            self.assertEqual(current_status["url"], edge_url)
            self.assertNotIn(current_status["pid"], seen_pids)
            seen_pids.append(current_status["pid"])
            drain_s = float(current_status["update"].get("admission_drain_seconds") or 0.0)
            window_start = cutover_start if cutover_start is not None else (commit_done - max(drain_s, 0.05))
            cutover_windows.append((window_start - 0.02, commit_done + 0.02))
            if isinstance(measured_route_cutover_ms, (int, float)):
                measured_ms = float(measured_route_cutover_ms)
            else:
                measured_ms = ((commit_done - cutover_start) * 1000.0) if cutover_start else (drain_s * 1000.0)
            cutover_wall_ms.append(measured_ms)
            time.sleep(0.25)

        stop_traffic.set()
        for worker in workers:
            worker.join(timeout=5)

        failures = [s for s in samples if not s["ok"]]
        self.assertEqual(failures, [], f"Expected 0 failed HTTP requests across 3 upgrades, got: {failures[:5]}")
        self.assertEqual(len(seen_pids), 4)
        self.assertEqual(int((self.root / "provider.pid").read_text()), provider_pid)
        self.assertEqual(sse_resets, 0)
        self.assertGreater(len(sse_events), 5)
        self.assertEqual(sse_events, sorted(set(sse_events)))

        def overlaps_cutover(sample: dict) -> bool:
            return any(sample["end"] >= lo and sample["start"] <= hi for lo, hi in cutover_windows)

        cutover_sends = sorted(s["ms"] for s in samples if s["kind"] == "send" and overlaps_cutover(s))
        all_sends = sorted(s["ms"] for s in samples if s["kind"] == "send")
        target_sends = cutover_sends or all_sends
        send_p99 = target_sends[math.ceil(len(target_sends) * 0.99) - 1]
        if os.environ.get("RIBBON_BENCH_REPORT"):
            Path(os.environ["RIBBON_BENCH_REPORT"]).write_text(json.dumps({
                "total_http_requests": len(samples),
                "failed_http_requests": len(failures),
                "send_p50_ms": round(all_sends[len(all_sends) // 2], 2),
                "send_p99_during_cutover_ms": round(send_p99, 2),
                "cutover_wall_ms": [round(x, 2) for x in cutover_wall_ms],
                "sse_resets": sse_resets,
                "sse_events": len(sse_events),
                "coordinator_pids": seen_pids,
            }, indent=2) + "\n")
        self.assertLess(send_p99, 150.0, f"Cutover send p99 {send_p99:.2f} ms exceeded 150 ms")
        for idx, wall_ms in enumerate(cutover_wall_ms, 1):
            self.assertLess(wall_ms, 250.0, f"Upgrade {idx} cutover wall time {wall_ms:.2f} ms exceeded 250 ms")


if __name__ == "__main__":
    unittest.main()
