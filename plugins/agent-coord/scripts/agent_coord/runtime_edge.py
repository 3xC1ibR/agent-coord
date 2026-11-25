"""Permanent loopback HTTP and Server-Sent Events edge proxy for desktop sessions."""
from __future__ import annotations
import argparse, fcntl, http.client, json, os, secrets, signal, threading, time
from contextlib import closing
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit
from .local_http import LocalHTTPServer

MAX_PROXY_BODY_BYTES, CUTOVER_HOLD_SECONDS = 32 * 1024 * 1024, 2.0
HEALTH_PROBE_INTERVAL_SECONDS, HEALTH_PROBE_TIMEOUT_SECONDS = 0.5, 0.35
SKIP_HEADERS = frozenset({"server", "date", "content-length", "transfer-encoding", "connection"})
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _write_json(path: Path, payload: dict) -> None:
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(6)}")
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as stream:
        json.dump(payload, stream); stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, path)


class EdgeProxy:
    def __init__(self, runtime_dir: Path, database: str, preferred_port: int = 0, csrf_token: str | None = None):
        self.runtime_dir, self.database, self.directory = Path(runtime_dir), database, Path(runtime_dir) / "runtime-edge"
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.instance, self.control_token, self.csrf_token = secrets.token_hex(16), secrets.token_urlsafe(32), csrf_token or secrets.token_urlsafe(32)
        self.stopping, self._lock, self._tokens, self._cache = threading.Event(), threading.Lock(), {}, {}
        self._route_path, self._update_path, self._cancel_path = self.runtime_dir / "edge-route.json", self.runtime_dir / "update.json", self.runtime_dir / "update-cancellation.json"
        self._failed_instance, self._last_ok, self.server = None, 0.0, self._bind(preferred_port)
        self.port, self.url = self.server.server_port, f"http://127.0.0.1:{self.server.server_port}/"

    def _read_cached(self, path: Path) -> dict | None:
        try: status_record = os.stat(path)
        except FileNotFoundError:
            with self._lock: self._cache.pop(path, None)
            return None
        signature = (status_record.st_mtime_ns, status_record.st_ino, status_record.st_size)
        with self._lock:
            if (cached := self._cache.get(path)) and cached[0] == signature: return cached[1]
        try: parsed = json.loads(path.read_bytes())
        except (OSError, ValueError): return cached[1] if cached else None
        value = parsed if isinstance(parsed, dict) else None
        with self._lock: self._cache[path] = (signature, value)
        return value

    def _bind(self, preferred_port: int) -> LocalHTTPServer:
        proxy = self
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            disable_nagle_algorithm = True
            do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = lambda self: proxy.handle_http(self)
            log_message = lambda self, *_args: None
        for port in ([preferred_port, 0] if preferred_port > 0 else [0]):
            try:
                server = LocalHTTPServer(("127.0.0.1", port), Handler)
                server.daemon_threads = True
                return server
            except OSError: continue
        raise RuntimeError("Unable to bind loopback edge listener.")

    def _probe_upstream(self, instance: str, port: int) -> bool:
        try:
            with closing(http.client.HTTPConnection("127.0.0.1", port, timeout=HEALTH_PROBE_TIMEOUT_SECONDS)) as connection:
                connection.request("GET", "/api/browser/config", headers={"Host": f"127.0.0.1:{port}"})
                response = connection.getresponse()
                raw = response.read(65536)
                if response.status == 200:
                    if isinstance(upstream_token := json.loads(raw).get("token"), str):
                        with self._lock: self._tokens[instance] = upstream_token
                    self._last_ok, self._failed_instance = time.monotonic(), None
                    return True
        except (OSError, http.client.HTTPException, ValueError): pass
        if (current := self._read_cached(self._route_path) or {}).get("state") == "serving" and current.get("instance") == instance:
            self._failed_instance = instance
        return False

    def _monitor_loop(self) -> None:
        while not self.stopping.wait(HEALTH_PROBE_INTERVAL_SECONDS):
            if (route := self._read_cached(self._route_path) or {}).get("state") == "serving" and route.get("upstream_port") and route.get("instance"):
                if self._failed_instance == route["instance"] or time.monotonic() - self._last_ok >= HEALTH_PROBE_INTERVAL_SECONDS:
                    self._probe_upstream(str(route["instance"]), int(route["upstream_port"]))

    def _upstream_csrf(self, route: dict) -> str:
        if isinstance(route.get("csrf_token"), str) and route["csrf_token"]: return route["csrf_token"]
        instance = str(route.get("instance", ""))
        with self._lock: upstream_token = self._tokens.get(instance)
        if not upstream_token and self._probe_upstream(instance, int(route["upstream_port"])):
            with self._lock: upstream_token = self._tokens.get(instance, "")
        return upstream_token or ""

    @staticmethod
    def _reply_json(handler: BaseHTTPRequestHandler, code: int, payload: dict, drain: int = 0, keep_alive: bool = False) -> None:
        raw = json.dumps(payload).encode("utf-8")
        reuse = keep_alive and drain == 0 and handler.headers.get("Connection", "").lower() != "close"
        try:
            handler.send_response(code)
            for name, value in (("Content-Type", "application/json; charset=utf-8"), ("Content-Length", str(len(raw))), ("Cache-Control", "no-store"), ("Connection", "keep-alive" if reuse else "close")):
                handler.send_header(name, value)
            handler.end_headers()
            if handler.command != "HEAD": handler.wfile.write(raw)
            if reuse: handler.wfile.flush()
            if drain > 0: handler.wfile.flush(); handler.connection.settimeout(0.5); handler.rfile.read(drain)
        except OSError:
            reuse = False
        if not reuse:
            handler.close_connection = True

    def handle_http(self, handler: BaseHTTPRequestHandler) -> None:
        parsed = urlsplit(handler.path)
        if parsed.path in {"/_runtime/edge", "/_runtime/stop"}:
            if handler.headers.get("Origin") or not secrets.compare_digest(handler.headers.get("Authorization", ""), "Bearer " + self.control_token):
                return self._reply_json(handler, 403, {"error": "Forbidden."})
            if parsed.path == "/_runtime/stop" and handler.command == "POST":
                self._reply_json(handler, 200, {"stopped": True, "instance": self.instance})
                self.stopping.set(); threading.Thread(target=self.server.shutdown, daemon=True).start(); return
            return self._reply_json(handler, 200, {"instance": self.instance, "url": self.url, "pid": os.getpid()})
        host = handler.headers.get("Host", "")
        if host not in {f"127.0.0.1:{self.port}", f"localhost:{self.port}"} or any(name.lower().startswith(("forwarded", "x-forwarded-", "tailscale-")) for name in handler.headers):
            return self._reply_json(handler, 403, {"error": "Invalid host."})
        client_host = "localhost" if host.startswith("localhost:") else "127.0.0.1"
        if parsed.path == "/_runtime/health" and handler.command in {"GET", "HEAD"}:
            if handler.headers.get("Origin") not in (None, f"http://{client_host}:{self.port}"):
                return self._reply_json(handler, 403, {"error": "Forbidden."})
            route, update = self._read_cached(self._route_path) or {}, self._read_cached(self._update_path)
            if update and (self._read_cached(self._cancel_path) or {}).get("id") == update.get("id"):
                update = {**update, "cancelled": True}
            if (state := route.get("state")) in {"cutover", "updating", "preparing"}:
                return self._reply_json(handler, 200, {**(route.get("health") or {}), "status": "preparing" if state == "preparing" else "updating", "url": self.url, "edge_pid": os.getpid(), "update": update}, keep_alive=True)
            if state != "serving" or self._failed_instance == route.get("instance"):
                return self._reply_json(handler, 503, {"status": "unavailable", "url": self.url, "update": update}, keep_alive=True)
            return self._reply_json(handler, 200, {**(route.get("health") or {}), "status": "serving", "url": self.url, "edge_pid": os.getpid(), "update": update}, keep_alive=True)
        if handler.headers.get("Transfer-Encoding"): return self._reply_json(handler, 415, {"error": "Chunked request bodies are unsupported."})
        try: length = int(handler.headers.get("Content-Length", "0"))
        except ValueError: return self._reply_json(handler, 400, {"error": "Invalid Content-Length."})
        if not 0 <= length <= MAX_PROXY_BODY_BYTES: return self._reply_json(handler, 413, {"error": "Request body is too large."})
        external_work = parsed.path == "/api/external-work" or parsed.path.startswith("/api/external-work/")
        token = handler.headers.get("X-Agent-Coord-Token", "")
        if handler.command in MUTATING_METHODS and not external_work and (not token or not secrets.compare_digest(token, self.csrf_token)):
            return self._reply_json(handler, 403, {"error": "Reload Ribbon Field before making changes."}, drain=length)
        body, deadline, attempt = (handler.rfile.read(length) if length > 0 else None), time.monotonic() + CUTOVER_HOLD_SECONDS, 0
        while True:
            route = self._read_cached(self._route_path) or {}
            if route.get("state") == "stopped": return self._reply_json(handler, 503, {"error": "Ribbon Field runtime is stopped."})
            if route.get("state") != "serving" or not route.get("upstream_port"):
                if time.monotonic() >= deadline: return self._reply_json(handler, 503, {"error": "Ribbon Field is updating. Try again shortly."})
                time.sleep(min(0.005, 0.001 + attempt * 0.0005)); attempt += 1; continue
            upstream_port, instance = int(route["upstream_port"]), str(route.get("instance", ""))
            upstream_csrf = self._upstream_csrf(route) if token else ""
            headers = {k: v for k, v in handler.headers.items() if k.lower() not in {"host", "connection"}}
            headers["Host"], headers["Connection"] = f"{client_host}:{upstream_port}", "close"
            if headers.get("Origin") == f"http://{client_host}:{self.port}": headers["Origin"] = f"http://{client_host}:{upstream_port}"
            if token and secrets.compare_digest(token, self.csrf_token) and upstream_csrf: headers["X-Agent-Coord-Token"] = upstream_csrf
            with closing(http.client.HTTPConnection("127.0.0.1", upstream_port, timeout=30)) as connection:
                try:
                    connection.request(handler.command, handler.path, body=body, headers=headers)
                    response = connection.getresponse()
                except (OSError, http.client.HTTPException):
                    if time.monotonic() < deadline: time.sleep(min(0.005, 0.001 + attempt * 0.0005)); attempt += 1; continue
                    return self._reply_json(handler, 503, {"error": "Coordinator unavailable."})
                if response.status in {400, 409, 503} and not route.get("paused"):
                    error_body = response.read()
                    if b"Runtime maintenance is pausing new work" in error_body and time.monotonic() < deadline:
                        time.sleep(min(0.005, 0.001 + attempt * 0.0005)); attempt += 1; continue
                    return self._send_upstream(handler, response.status, response.getheaders(), error_body, client_host, upstream_port)
                self._last_ok, self._failed_instance = time.monotonic(), None
                if response.getheader("Content-Type", "").startswith("text/event-stream"):
                    return self._send_upstream(handler, response.status, response.getheaders(), None, client_host, upstream_port, response)
                data = response.read()
                if parsed.path == "/api/browser/config" and response.status == 200 and data:
                    if isinstance(upstream_token := (configuration := json.loads(data)).get("token"), str):
                        with self._lock: self._tokens[instance] = upstream_token
                    data = json.dumps({**configuration, "token": self.csrf_token}).encode("utf-8")
                return self._send_upstream(handler, response.status, response.getheaders(), data, client_host, upstream_port)

    def _send_upstream(self, handler: BaseHTTPRequestHandler, code: int, headers: list[tuple[str, str]],
                       body: bytes | None, client_host: str, upstream_port: int, stream: http.client.HTTPResponse | None = None) -> None:
        edge_origin = f"http://{client_host}:{self.port}"
        try:
            handler.send_response(code)
            for name, value in headers:
                if name.lower() not in SKIP_HEADERS:
                    if name.lower() in {"content-security-policy", "location"}:
                        value = value.replace(f"http://{client_host}:{upstream_port}", edge_origin).replace(f"http://127.0.0.1:{upstream_port}", edge_origin)
                    handler.send_header(name, value)
            if stream is None: handler.send_header("Content-Length", str(len(body or b"")))
            handler.send_header("Connection", "close"); handler.end_headers()
            if handler.command != "HEAD":
                if stream is not None:
                    while chunk := (stream.fp.read1(16384) if hasattr(stream.fp, "read1") else stream.readline()):
                        handler.wfile.write(chunk); handler.wfile.flush()
                elif body: handler.wfile.write(body)
        except (OSError, ValueError): pass
        handler.close_connection = True

    def serve(self, ready_fd: int | None = None) -> int:
        _write_json(self.directory / "owner.json", {"instance": self.instance, "database": self.database, "url": self.url, "port": self.port, "pid": os.getpid(), "control_token": self.control_token, "csrf_token": self.csrf_token})
        threading.Thread(target=self._monitor_loop, name="edge-health-monitor", daemon=True).start()
        if threading.current_thread() is threading.main_thread():
            for signal_number in (signal.SIGTERM, signal.SIGINT):
                signal.signal(signal_number, lambda *_: (self.stopping.set(), threading.Thread(target=self.server.shutdown, daemon=True).start()))
        if ready_fd is not None:
            try: os.write(ready_fd, b"ready\n")
            finally: os.close(ready_fd)
        try: self.server.serve_forever(poll_interval=0.1)
        finally: self.stopping.set(); self.server.server_close()
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for flag, kind in (("--directory", Path), ("--database", str), ("--inherit-owner-fd", int)):
        parser.add_argument(flag, type=kind, required=True)
    parser.add_argument("--ready-fd", type=int)
    parser.add_argument("--preferred-port", type=int, default=0)
    args, csrf_token = parser.parse_args(argv), os.environ.pop("RIBBON_EDGE_CSRF_TOKEN", None)
    try:
        fcntl.flock(args.inherit_owner_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return EdgeProxy(args.directory, args.database, args.preferred_port, csrf_token).serve(args.ready_fd)
    finally:
        os.close(args.inherit_owner_fd)


if __name__ == "__main__":
    raise SystemExit(main())
