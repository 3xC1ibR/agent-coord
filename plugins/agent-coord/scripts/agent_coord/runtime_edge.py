"""Retain the desktop browser origin while its loopback coordinator changes."""
from __future__ import annotations

from contextlib import closing, contextmanager
import fcntl
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

from .image_inputs import MAX_MESSAGE_BODY_BYTES
from .local_http import LocalHTTPServer

WAIT_SECONDS = 2.0
CONNECT_SECONDS = .2
REQUEST_SECONDS = 30
HEALTH = "/_runtime/health"
READY = "/_runtime/ready"
OBSERVER_HEADER = "X-Ribbon-Runtime-Observer"
FENCED_HEADER = "X-Ribbon-Admission-Fenced"
TOKEN_HEADER = "X-Agent-Coord-Token"
HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
}
SSE = "/api/browser/events"
CONFIG = "/api/browser/config"
PUBLIC_FRAME_ASSETS = {"/openui-frame.js", "/vendor/openui/openui-bundle.min.js",
                       "/vendor/openui/openui-styles.css"}
EVENT_ID = re.compile(r"([0-9]{1,19}):([0-9]{1,19})")
GENERATION = re.compile(r"[0-9a-f]{32}")


def authority(url):
    parsed = urlsplit(url)
    port = parsed.port
    if not port or url != f"http://127.0.0.1:{port}/":
        raise ValueError("Invalid loopback runtime address.")
    return parsed.netloc


def private_json(path):
    try:
        with path.open() as stream:
            stat = os.fstat(stream.fileno())
            if stat.st_uid != os.getuid() or stat.st_mode & 0o077:
                raise RuntimeError("Unsafe runtime edge record permissions.")
            return json.load(stream)
    except FileNotFoundError:
        return None


def read_address(directory):
    edge = private_json(directory / "edge.json")
    if edge is None:
        return None
    authority(edge["url"])
    return edge


def probe(edge):
    with closing(HTTPConnection(authority(edge["url"]), timeout=CONNECT_SECONDS)) as connection:
        connection.request("GET", HEALTH, headers={OBSERVER_HEADER: edge["observer"]})
        result = connection.getresponse()
        return json.load(result) if result.status == 200 else None


def ensure(database, payload):
    """Start the detached listener from a retained payload under its own lock."""
    from .desktop_runtime import lock_file, payload_command, payload_environment, runtime_directory
    directory = runtime_directory(database)
    with closing(os.fdopen(lock_file(directory / "edge-launch.lock", blocking=True), "rb")):
        edge = read_address(directory)
        if edge:
            try:
                if (probe(edge) or {}).get("edge_instance") == edge["instance"]:
                    return edge
            except (OSError, HTTPException, ValueError):
                pass
        owner = lock_file(directory / "edge-owner.lock")
        try:
            command = [payload_command(payload)[0], "-B", "-u", "-m", "agent_coord.runtime_edge",
                       str(directory), str(owner)]
            with (directory / "runtime.log").open("ab", buffering=0) as log:
                child = subprocess.Popen(command, pass_fds=(owner,), env=payload_environment(payload),
                    cwd=str(Path.home()), stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        finally:
            os.close(owner)
        deadline = time.monotonic() + 5
        while child.poll() is None and time.monotonic() < deadline:
            edge = read_address(directory)
            if edge and edge.get("pid") == child.pid:
                threading.Thread(target=child.wait, daemon=True).start()
                return edge
            time.sleep(.02)
        raise RuntimeError("The stable local runtime listener did not start.")


def status_for_desktop(directory, status):
    edge = read_address(directory)
    if not edge:
        raise RuntimeError("The stable local runtime listener has no discovery record.")
    return {**adoption_status(directory, status), "edge_url": edge["url"], "edge_observer": edge["observer"],
            "unbrokered_provider_wait": 1}


def adoption_status(directory, status):
    adoption = private_json(directory / "edge-adoption.json")
    if not adoption:
        return status
    update = status.get("update") or {}
    if (update.get("source_instance") == adoption["source_instance"]
            and update.get("target") == adoption["target"]):
        return {**status, "update": {**adoption, **update, "coordinator_update_id": update["id"], "id": adoption["id"]}}
    if status.get("instance") == adoption["source_instance"] and status.get("pending") == adoption["target"]:
        return {**status, "update": adoption}
    return status


def publish(directory, status, browser_token, *, ready, predecessor=False):
    """Publish a route only after the coordinator owns the serving generation."""
    from .desktop_runtime import lock_file
    with closing(os.fdopen(lock_file(directory / "edge-route.lock", blocking=True), "rb")):
        _publish_route(directory, status, browser_token, ready, predecessor)


def _publish_route(directory, status, browser_token, ready, predecessor):
    from .desktop_runtime import atomic_json
    current = private_json(directory / "edge-route.json")
    generation = status["instance"]
    if not GENERATION.fullmatch(generation):
        raise ValueError("Invalid coordinator generation.")
    authority(status["url"])
    # The regular runtime owner serializes publications. A predecessor may
    # close its own route but cannot overwrite a replacement's route.
    if not ready and current and current["status"]["instance"] != generation:
        return
    if predecessor and current and not current.get("predecessor") and current["status"]["instance"] != generation:
        return
    public_status = {key: status.get(key) for key in (
        "status", "url", "pid", "instance", "payload", "pending", "pending_confirmation",
        "runtime_updates", "runtime_update_prompts", "uninterrupted_updates",
        "interrupted_provider_continuation", "unbrokered_provider_wait", "paused", "upgrade_error",
    )}
    route = {"status": public_status, "browser_token": browser_token, "ready": ready, "predecessor": predecessor}
    if current and not current["ready"]:
        route["fenced_since"] = current.get("fenced_since")
    if not ready and not route.get("fenced_since"):
        route["fenced_since"] = time.monotonic()
    if ready and route.get("fenced_since"):
        route["handover_ms"] = round(1000 * (time.monotonic() - route.pop("fenced_since")), 2)
    elif ready and current and current["status"]["instance"] == generation and "handover_ms" in current:
        route["handover_ms"] = current["handover_ms"]
    if current != route:
        # Routing is a live observation. Boot and rollback recover from the
        # durable update transaction and publish a fresh serving generation.
        atomic_json(directory / "edge-route.json", route, durable=False)


def publish_predecessor(directory, status):
    """Bind a signed runtime that predates explicit edge publication."""
    with closing(HTTPConnection(authority(status["url"]), timeout=1)) as connection:
        connection.request("GET", CONFIG)
        with connection.getresponse() as response:
            if response.status != 200:
                raise RuntimeError("The preceding coordinator did not provide browser authentication.")
            token = json.load(response)["token"]
    publish(directory, {**status, "unbrokered_provider_wait": 1}, token, ready=True, predecessor=True)


def _cursor(target, generation, event_id):
    """Strip the edge generation while preserving the durable completion cursor."""
    if event_id:
        prefix, _, cursor = event_id.partition("/")
        match = EVENT_ID.fullmatch(cursor)
        if match:
            return f"{match[1] if prefix == generation else '0'}:{match[2]}"
        if EVENT_ID.fullmatch(event_id):
            return "0:" + event_id.split(":")[1]
        return None
    # The initial EventSource URL includes the generation in its sequence
    # value. An old URL can reconnect before its first event arrived.
    after = re.search(r"(?:[?&])after=([0-9a-f]{32})/([0-9]{1,19})(?:&|$)", target)
    completion = re.search(r"(?:[?&])completion_after=([0-9]{1,19})(?:&|$)", target)
    if after and completion:
        return f"{after[2] if after[1] == generation else '0'}:{completion[1]}"
    return None


def _safe_headers(headers):
    nominated = {value.strip().lower() for field in headers.get_all("Connection", [])
                 for value in field.split(",")}
    return HOP_HEADERS | nominated


@contextmanager
def inflight_descriptor(directory):
    fd = os.open(directory / "edge-inflight.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        yield fd
    finally:
        os.close(fd)


@contextmanager
def exclusive_admission(directory):
    with inflight_descriptor(directory) as fd:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            yield True


def drained(directory):
    with exclusive_admission(directory) as acquired:
        return acquired


class EdgeServer(LocalHTTPServer):
    daemon_threads = True
    request_queue_size = 128

    def __init__(self, directory, previous, port=0):
        super().__init__(("127.0.0.1", port), EdgeHandler)
        self.directory = Path(directory)
        self.host = f"127.0.0.1:{self.server_port}"
        self.origin = "http://" + self.host
        self.instance = secrets.token_hex(16)
        self.browser_token = previous.get("browser_token") or secrets.token_urlsafe(32)
        self.observer = previous.get("observer") or secrets.token_urlsafe(32)

    def route(self):
        route = private_json(self.directory / "edge-route.json")
        if route and route["ready"] and (pid := route["status"].get("pid")) is not None:
            try:
                os.kill(pid, 0)
            except OSError:
                return {**route, "ready": False}
        return route

    def health(self):
        from .runtime_update import TERMINAL, read
        route = self.route()
        update = read(self.directory)
        status = dict(route["status"]) if route else {}
        predecessor_switching = (route and route.get("predecessor") and update
            and update.get("source_instance") == status.get("instance")
            and update["phase"] in {"stopping", "prepared", "trial", "committed"})
        if predecessor_switching:
            status["status"] = "updating"
        elif not route or not route["ready"]:
            status["status"] = "updating" if update and update["phase"] not in TERMINAL else "unavailable"
        return {**adoption_status(self.directory, {**status, "update": update}),
                "edge_url": self.origin + "/", "edge_instance": self.instance,
                "edge_handover_ms": route.get("handover_ms") if route else None}


class EdgeHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def setup(self):
        self.request.settimeout(REQUEST_SECONDS)
        super().setup()

    def do_GET(self):
        self.relay()

    def do_HEAD(self):
        self.relay()

    def do_POST(self):
        self.relay()

    def respond(self, code, value):
        data = json.dumps(value).encode()
        self.send_response(code)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def relay(self):
        self.close_connection = True
        origins = self.headers.get_all("Origin", [])
        try:
            valid_target = (self.path.startswith("/") and not self.path.startswith("//")
                            and "\\" not in self.path and "#" not in self.path)
            path = urlsplit(self.path).path
            frame_asset = self.command == "GET" and path in PUBLIC_FRAME_ASSETS and origins in ([], ["null"])
            forbidden_headers = ("forwarded", "x-forwarded-", "tailscale-")
            if (self.headers.get_all("Host", []) != [self.server.host]
                    or (not frame_asset and (origins not in ([], [self.server.origin])
                                             or self.headers.get("Sec-Fetch-Site") == "cross-site"))
                    or any(name.lower().startswith(forbidden_headers) for name in self.headers)
                    or not valid_target):
                self.respond(403, {"error": "Invalid local runtime origin."})
                return
            if path in {HEALTH, READY}:
                provided = self.headers.get_all(OBSERVER_HEADER, [])
                if (self.command != "GET" or origins or len(provided) != 1
                        or not secrets.compare_digest(provided[0].encode("latin-1"), self.server.observer.encode())):
                    self.respond(403, {"error": "Runtime observation requires authorization."})
                    return
                status = self.server.health()
                ready = status["status"] == "serving" and not status.get("paused")
                self.respond(503 if path == READY and not ready else 200, status)
                return
            if path.startswith("/_runtime"):
                self.respond(404, {"error": "Unknown runtime route."})
                return
            lengths = self.headers.get_all("Content-Length", [])
            if (self.headers.get_all("Transfer-Encoding") or len(lengths) > 1
                    or (lengths and not re.fullmatch(r"[0-9]{1,8}", lengths[0]))):
                self.respond(400, {"error": "Invalid request framing."})
                return
            length = int(lengths[0]) if lengths else 0
            if length > MAX_MESSAGE_BODY_BYTES or (length and self.command != "POST"):
                self.respond(413, {"error": "Invalid request size."})
                return
            token = self.headers.get_all(TOKEN_HEADER, [])
            if token and (len(token) != 1 or not secrets.compare_digest(
                    token[0].encode("latin-1"), self.server.browser_token.encode())):
                self.respond(403, {"error": "Invalid local runtime request token."})
                return
            body = self.rfile.read(length) if length else None
            if body is not None and len(body) != length:
                self.respond(400, {"error": "Incomplete request."})
                return
            end = time.monotonic() + WAIT_SECONDS
            while time.monotonic() < end:
                route = self.server.route()
                if route and route["ready"]:
                    if path == SSE:
                        if self.forward(route, path, body):
                            return
                    else:
                        with inflight_descriptor(self.server.directory) as fd:
                            try:
                                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                            except BlockingIOError:
                                pass
                            else:
                                # A request holding the lease after this read
                                # is counted before the old process can exit.
                                route = self.server.route()
                                if route and route["ready"] and self.forward(route, path, body):
                                    return
                time.sleep(.005)
            self.respond(503, {"error": "The coordinator is switching. The request was not admitted."})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return

    def forward(self, route, path, body):
        status = route["status"]
        upstream = authority(status["url"])
        generation = status["instance"]
        omitted = _safe_headers(self.headers) | {"host", "content-length", "origin",
            OBSERVER_HEADER.lower(), FENCED_HEADER.lower(), TOKEN_HEADER.lower(), "last-event-id"}
        with closing(HTTPConnection(upstream, timeout=CONNECT_SECONDS)) as connection:
            # Only a connect failure establishes that zero request bytes were sent.
            try:
                connection.connect()
            except OSError:
                return False
            connection.sock.settimeout(REQUEST_SECONDS)
            try:
                connection.putrequest(self.command, self.path, skip_host=True, skip_accept_encoding=True)
                for key, value in self.headers.items():
                    if key.lower() not in omitted:
                        connection.putheader(key, value)
                connection.putheader("Host", upstream)
                if self.headers.get("Origin") == self.server.origin:
                    connection.putheader("Origin", "http://" + upstream)
                if self.headers.get(TOKEN_HEADER) is not None:
                    connection.putheader(TOKEN_HEADER, route["browser_token"])
                if self.command == "POST":
                    connection.putheader("Content-Length", str(len(body) if body else 0))
                if path == SSE:
                    event_id = _cursor(self.path, generation, self.headers.get("Last-Event-ID"))
                    if event_id:
                        connection.putheader("Last-Event-ID", event_id)
                connection.endheaders(body)
                response = connection.getresponse()
            except (OSError, HTTPException):
                # The receiving handler may already have committed. Let its
                # durable request receipt resolve retries from the caller.
                if self.command in {"GET", "HEAD"}:
                    return False
                self.respond(502, {"error": "The coordinator response was lost. Check the request outcome."})
                return True
            if response.status == 503 and response.getheader(FENCED_HEADER) == generation:
                return False  # Upstream proved the request never entered admission.
            try:
                self.deliver(response, upstream, generation, path)
            except (OSError, HTTPException):
                pass  # A response may be in flight. Never append a second one.
            return True

    def deliver(self, response, upstream, generation, path):
        stream = (response.getheader("Content-Type") or "").startswith("text/event-stream")
        data = None
        if self.command == "GET" and path == CONFIG and response.status == 200:
            config = json.load(response)
            config["token"] = self.server.browser_token
            config["eventCursor"] = f"{generation}/{config['eventCursor']}"
            data = json.dumps(config).encode()
        omitted = _safe_headers(response.headers) | {FENCED_HEADER.lower()}
        if data is not None:
            omitted.add("content-length")
        self.send_response(response.status)
        for key, value in response.headers.items():
            if key.lower() not in omitted:
                if key.lower() in {"content-security-policy", "location"}:
                    value = value.replace("http://" + upstream, self.server.origin)
                self.send_header(key, value)
        if data is not None:
            self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command == "HEAD":
            return
        if data is not None:
            self.wfile.write(data)
        elif stream:
            while line := response.readline():
                if line.startswith(b"id: ") and EVENT_ID.fullmatch(line[4:].strip().decode("ascii")):
                    line = b"id: " + generation.encode() + b"/" + line[4:]
                self.wfile.write(line)
                if line == b"\n":
                    self.wfile.flush()
                    current = self.server.route()
                    if not current or not current["ready"] or current["status"]["instance"] != generation:
                        break
        else:
            while chunk := response.read(64 * 1024):
                self.wfile.write(chunk)


def serve(directory):
    from .desktop_runtime import atomic_json
    previous = read_address(directory) or {}
    port = urlsplit(previous["url"]).port if previous else 0
    with EdgeServer(directory, previous, port) as server:
        atomic_json(directory / "edge.json", {
            "url": server.origin + "/", "instance": server.instance, "pid": os.getpid(),
            "observer": server.observer, "browser_token": server.browser_token,
        })
        server.timeout = .5
        while directory.is_dir():
            server.handle_request()


if __name__ == "__main__":
    # Hold the inherited edge owner descriptor for the listener lifetime.
    directory = Path(sys.argv[1])
    owner = int(sys.argv[2])
    descriptor, lock = os.fstat(owner), (directory / "edge-owner.lock").stat()
    if (descriptor.st_dev, descriptor.st_ino) != (lock.st_dev, lock.st_ino):
        raise RuntimeError("Invalid inherited runtime edge ownership.")
    fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
    serve(directory)
