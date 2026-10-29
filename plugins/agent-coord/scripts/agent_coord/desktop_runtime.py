"""Private, versioned desktop runtime; no dependency on the native app lifetime.

The canonical database owns the lock/discovery directory. Payloads are retained
copies, never executable paths in a replaceable app bundle. Control uses a
separate random credential and loopback port, unavailable to web clients.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import functools
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import select
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid

PROTOCOL_VERSION = 1
SCHEMA_VERSION = 1
_OWNED_DATABASES = set()


def compatibility_metadata(version):
    return {"runtime_version": str(version), "runtime_protocol": PROTOCOL_VERSION,
            "ui_protocol_min": PROTOCOL_VERSION, "ui_protocol_max": PROTOCOL_VERSION,
            "runtime_schema_min": SCHEMA_VERSION, "runtime_schema_max": SCHEMA_VERSION}


def compatible(target):
    keys = ("runtime_protocol", "ui_protocol_min", "ui_protocol_max", "runtime_schema_min", "runtime_schema_max")
    if not isinstance(target, dict) or any(type(target.get(key)) is not int or target[key] < 1 for key in keys):
        return False
    return (target.get("runtime_protocol") == PROTOCOL_VERSION
            and target.get("ui_protocol_min", 0) <= PROTOCOL_VERSION <= target.get("ui_protocol_max", 0)
            and target.get("runtime_schema_min", 0) <= SCHEMA_VERSION <= target.get("runtime_schema_max", 0))


def version_key(metadata):
    # Distributed build versions are numeric dotted versions plus a numeric
    # build stamp. Unknown formats cannot authorize runtime replacement.
    version = metadata.get("runtime_version", "")
    if not re.fullmatch(r"\d+(?:\.\d+)*(?:\+\d+)?", version):
        return None
    return tuple(int(part) for part in re.split(r"[.+]", version))


def runtime_directory(database):
    database = Path(database).expanduser().resolve()
    path = database.parent / ("." + database.name + ".runtime")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or path.stat().st_uid != os.getuid():
        raise RuntimeError("Runtime directory is not owned by this user.")
    path.chmod(0o700)
    return path


def atomic_json(path, value):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path):
    return json.loads(Path(path).read_text())


def lock_file(path, *, blocking=False):
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def owner_available(directory):
    try:
        descriptor = lock_file(directory / "owner.lock")
    except BlockingIOError:
        return False
    os.close(descriptor)
    return True


@functools.lru_cache(maxsize=1)
def schema_fingerprint():
    # Conservative compatibility: any Python change requires an idle migration
    # boundary. Old providers keep invoking their retained, matching CLI/hooks.
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).parent.glob("*.py")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


@contextmanager
def schema_access(database):
    """Serialize constructors and reject new CLI/hook schema under an old owner."""
    directory = runtime_directory(database)
    descriptor = lock_file(directory / "schema.lock", blocking=True)
    try:
        record = directory / "schema.json"
        fingerprint = schema_fingerprint()
        previous = read_json(record) if record.exists() else None
        owner = str(Path(database).resolve()) in _OWNED_DATABASES
        if previous and previous["fingerprint"] != fingerprint and not owner_available(directory) and not owner:
            raise RuntimeError("A different Ribbon Field runtime owns this database. Wait for its idle upgrade before using the new CLI or hooks.")
        if previous and previous["schema"] > SCHEMA_VERSION:
            raise RuntimeError("This database requires a newer Ribbon Field runtime; downgrade is unsafe.")
        yield
        if previous is None or previous["fingerprint"] != fingerprint:
            atomic_json(record, {"schema": SCHEMA_VERSION, "fingerprint": fingerprint})
    finally:
        os.close(descriptor)


class AdmissionGate:
    """Close admission before inspecting idle; count complete routing operations.

    Nested calls on an admitted thread finish normally. Other callers are either
    rejected before accepting work, or leave durable automatic work unclaimed.
    No gate lock is held across provider I/O or callbacks.
    """
    def __init__(self):
        self.lock = threading.RLock()
        self.local = threading.local()
        self.paused = False
        self.inflight = 0

    @contextmanager
    def enter(self):
        with self.lock:
            depth = getattr(self.local, "depth", 0)
            allowed = bool(depth or not self.paused)
            if allowed:
                self.local.depth = depth + 1
                if depth == 0:
                    self.inflight += 1
        try:
            yield allowed
        finally:
            if allowed:
                with self.lock:
                    self.local.depth -= 1
                    if self.local.depth == 0:
                        self.inflight -= 1

    def wrap(self, target, name, *, automatic=False):
        method = getattr(target, name)
        @functools.wraps(method)
        def admitted(*args, **kwargs):
            with self.enter() as allowed:
                if not allowed:
                    if automatic:
                        return False
                    from .codex_app_server import BrowserBusyError
                    raise BrowserBusyError("Runtime maintenance is pausing new work. Try again shortly.")
                return method(*args, **kwargs)
        setattr(target, name, admitted)

    def pause(self):
        with self.lock:
            self.paused = True
            return self.inflight

    def resume(self):
        with self.lock:
            self.paused = False


def install_session_admission(sessions):
    gate = sessions.admission = AdmissionGate()
    for name in ("create", "send", "submit", "read", "resume_work_thread", "fork_work_thread", "answer", "update"):
        gate.wrap(sessions, name)
    gate.wrap(sessions.queue, "enqueue")
    gate.wrap(sessions.capacity_retries, "process_once", automatic=True)
    gate.wrap(sessions.queue, "change")
    for worker, name in ((sessions.queue, "_dispatch"), (sessions.schedules, "process_once"),
                         (sessions.inbox_wake, "process_once"), (sessions.dispatch, "process_once"),
                         (sessions.event_subscriptions, "process_once"), (sessions.reviews, "process_once"),
                         (sessions.sidecars, "process_once"), (sessions.watchdog, "process_once")):
        gate.wrap(worker, name, automatic=True)
    gate.wrap(sessions.reviews, "submit")
    for provider in (sessions.rpc, sessions.claude, sessions.antigravity, sessions.kimi, sessions.muse):
        gate.wrap(provider, "request")
    return gate


def busy_reasons(sessions):
    reasons = []
    if sessions.admission.inflight:
        reasons.append("admitted operations")
    with sessions.lock:
        if sessions.active:
            reasons.append("active turns")
        if sessions.requests:
            reasons.append("pending approvals or questions")
    with sessions.persistence.condition:
        if sessions.persistence.pending or sessions.persistence.draining:
            reasons.append("pending provider bookkeeping")
    with sessions.store._connection() as db:
        for table, condition in (("review_runs", "state IN ('active','creating')"),
                                 ("dispatch_requests", "state IN ('routing','creating','sending')"),
                                 ("browser_message_queue", "state='sending'"),
                                 ("app_control_requests", "status='running'")):
            if db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
                if db.execute(f"SELECT 1 FROM {table} WHERE {condition} LIMIT 1").fetchone():
                    reasons.append(table)
    return reasons


def readiness(sessions):
    """An unavailable snapshot is busy, never permission to stop providers."""
    try:
        return busy_reasons(sessions)
    except (sqlite3.Error, OSError, RuntimeError):
        return ["runtime readiness unavailable"]


def payload_digest(resources):
    digest = hashlib.sha256()
    for path in sorted(resources.rglob("*")):
        if "__pycache__" in path.parts or path.name.endswith(".pyc"):
            continue
        if path.is_file():
            digest.update(str(path.relative_to(resources)).encode())
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
    return digest.hexdigest()


def stage_payload(resources, directory):
    resources = Path(resources).resolve()
    identity = payload_digest(resources)
    payload = directory / "payloads" / identity
    if not payload.exists():
        payload.parent.mkdir(exist_ok=True, mode=0o700)
        temporary = Path(tempfile.mkdtemp(prefix="stage-", dir=payload.parent))
        try:
            shutil.copytree(resources, temporary / "resources", symlinks=True,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            copied = temporary / "resources"
            if payload_digest(copied) != identity:
                raise RuntimeError("Application resources changed while staging the runtime.")
            # Relative internal runtime symlinks remain relocatable. Refuse any
            # link whose target would still depend on the replaceable bundle.
            for link in copied.rglob("*"):
                if link.is_symlink() and not link.resolve().is_relative_to(copied):
                    raise RuntimeError("Runtime payload contains an external symlink.")
            os.rename(copied, payload)
        except FileExistsError:
            pass
        finally:
            shutil.rmtree(temporary)
    return payload


def payload_command(payload):
    config = read_json(payload / "backend.json")
    python = Path(config["python"])
    if not python.is_absolute():
        python = payload / python
    return [str(python), "-u", str(payload / "backend.py")]


def payload_environment(payload):
    environment = dict(os.environ)
    for name in list(environment):
        if name.startswith(("AGENT_COORD_", "ZELLIJ", "RIBBON_RUNTIME_")) or name in {"CODEX_THREAD_ID", "PYTHONHOME"}:
            environment.pop(name)
    environment.update(PYTHONPATH=str(payload / "backend"), PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1")
    return environment


def control(record, action, *, timeout=25, **values):
    url = record["control_url"]
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.username or parsed.password or parsed.path != "/":
        raise RuntimeError("Invalid runtime discovery address.")
    request = urllib.request.Request(url, method="POST", data=json.dumps({"action": action, "instance": record["instance"], **values}).encode(),
        headers={"Authorization": "Bearer " + record["control_token"], "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        result = json.load(response)
    if result.get("instance") != record["instance"] or result.get("database") != record["database"]:
        raise RuntimeError("Runtime discovery identity does not match the live instance.")
    if result.get("error"):
        raise RuntimeError(result["error"])
    return result


def discover(database, *, timeout=25):
    directory = runtime_directory(database)
    path = directory / "discovery.json"
    if not path.exists():
        return None
    if path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise RuntimeError("Runtime discovery permissions are unsafe.")
    record = read_json(path)
    if record.get("database") != str(Path(database).resolve()):
        raise RuntimeError("Runtime discovery refers to another database.")
    try:
        try:
            status = control(record, "identity", timeout=timeout)
        except RuntimeError as exc:
            # Older retained runtimes predate the lightweight handshake. Only
            # their authenticated unsupported-action response permits fallback;
            # authentication, identity and transport failures still fail closed.
            if str(exc) != "Unknown runtime control action.":
                raise
            status = control(record, "status", timeout=timeout)
    except (OSError, ValueError, RuntimeError):
        if not owner_available(directory):
            raise RuntimeError("The runtime owner is unavailable. Refusing a second owner.")
        return None
    if status.get("protocol") != PROTOCOL_VERSION:
        raise RuntimeError("The running runtime is incompatible with this desktop.")
    # A stale discovery payload is never authority to load client code. Use
    # the payload authenticated by this instance, without another status scan.
    return {**record, "payload": status.get("payload")}


def bootstrap(database, resources, *, start_paused=False):
    database = str(Path(database).expanduser().resolve())
    directory = runtime_directory(database)
    launcher = lock_file(directory / "launch.lock", blocking=True)
    try:
        payload = stage_payload(Path(resources), directory)
        record = discover(database)
        if record:
            if start_paused and not control(record, "status")["paused"]:
                raise RuntimeError("A runtime is already admitting work. Pause it explicitly before maintenance.")
            return control(record, "attach", payload=str(payload))
        if not owner_available(directory):
            raise RuntimeError("Another runtime is starting. Try again shortly.")
        with (directory / "runtime.log").open("ab", buffering=0) as log:
            command = payload_command(payload) + ["--runtime-serve", "--db", database]
            if start_paused:
                command.append("--runtime-start-paused")
            child = subprocess.Popen(command,
                                     env=payload_environment(payload), cwd=str(Path.home()), stdin=subprocess.DEVNULL,
                                     stdout=log, stderr=log, start_new_session=True)
        for _ in range(200):
            if child.poll() is not None:
                raise RuntimeError("The detached runtime failed to start; see " + str(directory / "runtime.log"))
            try:
                record = discover(database)
            except RuntimeError:
                record = None
            if record:
                threading.Thread(target=child.wait, daemon=True).start()
                return control(record, "attach", payload=str(payload))
            time.sleep(.1)
        raise RuntimeError("Runtime startup timed out; ownership remains reserved until it exits.")
    finally:
        os.close(launcher)


class Runtime:
    def __init__(self, database, payload, owner_fd, ready_fd=None, *, start_paused=False):
        self.database = str(Path(database).resolve())
        self.directory = runtime_directory(database)
        self.payload = Path(payload).resolve()
        self.owner_fd = owner_fd
        self.ready_fd = ready_fd
        self.instance = uuid.uuid4().hex
        self.token = secrets.token_urlsafe(32)
        self.pending = None
        self.selected = self.payload
        self.error = None
        self.mutex = threading.RLock()
        self.stopping = threading.Event()
        self.upgrading = False
        self.maintenance = start_paused
        self.explicit_stop = False
        self.server = None

    def identity(self):
        """Immutable owner identity; independent of readiness and session locks."""
        return {"database": self.database, "instance": self.instance,
                "protocol": PROTOCOL_VERSION, "schema": SCHEMA_VERSION, "pid": os.getpid(),
                "payload": str(self.payload)}

    def status(self):
        return {**self.identity(), "status": "serving",
                "url": f"http://127.0.0.1:{self.server.server_port}/",
                "pending": str(self.pending) if self.pending else None, "upgrade_error": self.error,
                "paused": self.server.browser_sessions.admission.paused,
                "busy": readiness(self.server.browser_sessions)}

    def dispatch(self, body):
        action = body.get("action")
        if action == "identity":
            # The control handler has already authenticated token + instance.
            # Readiness is required for handover, not for routing a hook/CLI.
            return self.identity()
        with self.mutex:
            sessions = self.server.browser_sessions
            if action == "attach":
                candidate = Path(body["payload"]).resolve()
                if candidate.parent != self.directory / "payloads" or not candidate.is_dir():
                    raise RuntimeError("Only a retained runtime payload can be attached.")
                metadata = read_json(candidate / "backend.json")
                if not compatible(metadata):
                    raise RuntimeError("Desktop and running runtime are incompatible; activation deferred.")
                if self.upgrading:
                    raise RuntimeError("Runtime handover is in progress. Reconnect shortly.")
                self.selected = candidate
                current_version = version_key(read_json(self.payload / "backend.json"))
                candidate_version = version_key(metadata)
                self.pending = (candidate if candidate != self.payload and current_version is not None
                                and candidate_version is not None and candidate_version >= current_version else None)
                self.error = None
                # Each attach selects the installed desktop, so rollback to the
                # old desktop cancels a previously staged newer runtime.
                from . import ui
                ui._WEB_ROOT = candidate / "backend/agent_coord/web"
                atomic_json(self.directory / "selected.json", {"payload": str(candidate)})
            elif action == "decision":
                if not compatible(body.get("target", {})):
                    raise RuntimeError("The update is incompatible with the running runtime.")
                if not self.payload.is_dir() or self.upgrading:
                    raise RuntimeError("Runtime lifetime isolation is unavailable; update deferred.")
            elif action == "pause":
                self.maintenance = True
                sessions.admission.pause()
            elif action == "resume":
                if self.upgrading:
                    raise RuntimeError("Runtime handover is in progress.")
                self.maintenance = False
                sessions.admission.resume()
                if not self.pending:
                    sessions.queue.wake()
                sessions.dispatch.wake()
            elif action == "stop":
                self.maintenance = True
                self.explicit_stop = True
                sessions.admission.pause()
                self.stopping.set()
            elif action != "status":
                raise RuntimeError("Unknown runtime control action.")
            return self.status()

    def serve(self):
        from .store import CoordinationStore
        from .ui import make_ui_server
        from . import launch_metrics
        store = CoordinationStore(self.database)
        # Finish ownership adoption before any automatic worker can claim work.
        self.server = make_ui_server(store, host="127.0.0.1", port=0, admission_paused=True)
        sessions = self.server.browser_sessions
        # make_ui_server installs these before starting workers; wrapping again
        # is harmless but would not protect startup, so assert the contract.
        assert getattr(self.server, "runtime_admission_ready", False), "UI workers require admission integration"
        owner = os.environ.pop("RIBBON_RUNTIME_QUEUE_OWNER", None)
        schedule_start = os.environ.pop("RIBBON_RUNTIME_SCHEDULE_START", None)
        if schedule_start is not None:
            sessions.schedules.started_at = float(schedule_start)
        if owner:
            with store._connection() as db:
                db.execute("UPDATE browser_message_queue SET owner=? WHERE owner=? AND state='queued'", (sessions.queue.owner, owner))
            sessions.queue.wake()
        launch_metrics.start(store, self.stopping, self.payload)
        runtime = self
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                if self.headers.get("Origin") or not secrets.compare_digest(self.headers.get("Authorization", ""), "Bearer " + runtime.token):
                    self.send_error(403)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length < 65536:
                        raise ValueError("Invalid control request size.")
                    body = json.loads(self.rfile.read(length))
                    if body.get("instance") != runtime.instance:
                        raise ValueError("Wrong runtime instance.")
                    result = runtime.dispatch(body)
                except (ValueError, RuntimeError, OSError, sqlite3.Error) as exc:
                    result = {"instance": runtime.instance, "database": runtime.database, "error": str(exc)}
                data = json.dumps(result).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            def log_message(self, *_):
                pass
        controller = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        controller.daemon_threads = True
        record = {**self.status(), "control_url": f"http://127.0.0.1:{controller.server_port}/", "control_token": self.token}
        atomic_json(self.directory / "discovery.json", record)
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, lambda *_: self.stopping.set())
        control_thread = threading.Thread(target=controller.serve_forever, daemon=True)
        ui_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        control_thread.start()
        ui_thread.start()
        if not self.maintenance:
            sessions.admission.resume()
            sessions.queue.wake()
        if self.ready_fd is not None:
            os.write(self.ready_fd, b"ready\n")
            os.close(self.ready_fd)
            self.ready_fd = None
        candidate = None
        try:
            while not self.stopping.wait(.5):
                with self.mutex:
                    if not self.pending or self.maintenance:
                        continue
                    # Busy work must remain answerable while an update waits.
                    # Only close admission after an initial idle observation;
                    # the closed-gate check below still catches newly admitted work.
                    if readiness(sessions):
                        continue
                    # Close admissions atomically before checking all accepted
                    # operations and provider activity. A busy check reopens it.
                    sessions.admission.pause()
                    if readiness(sessions):
                        sessions.admission.resume()
                        sessions.queue.wake()
                        continue
                    self.upgrading = True
                    candidate = self.pending
                try:
                    probe = subprocess.run(payload_command(candidate) + ["--runtime-probe"],
                                           env=payload_environment(candidate), capture_output=True, timeout=30)
                    failure = probe.stderr.decode(errors="replace")[-1000:] if probe.returncode else None
                except (OSError, subprocess.TimeoutExpired) as exc:
                    failure = str(exc)
                if failure is not None:
                    with self.mutex:
                        self.error = "Candidate validation failed: " + failure
                        self.pending = None
                        self.upgrading = False
                        sessions.admission.resume()
                        sessions.queue.wake()
                    candidate = None
                    continue
                with self.mutex:
                    if self.stopping.is_set():
                        candidate = None
                        break
                    if self.maintenance or readiness(sessions):
                        self.upgrading = False
                        candidate = None
                        if not self.maintenance:
                            sessions.admission.resume()
                        continue
                break
        finally:
            controller.shutdown()
            self.server.shutdown()
            control_thread.join()
            ui_thread.join()
            controller.server_close()
            sessions.runtime_handover = candidate is not None
            self.server.server_close()
        if candidate:
            environment = payload_environment(candidate)
            environment["RIBBON_RUNTIME_QUEUE_OWNER"] = sessions.queue.owner
            environment["RIBBON_RUNTIME_SCHEDULE_START"] = str(sessions.schedules.started_at)
            ready_read, ready_write = os.pipe()
            with (self.directory / "runtime.log").open("ab", buffering=0) as log:
                child = subprocess.Popen(payload_command(candidate) + ["--runtime-serve", "--db", self.database,
                    "--inherit-owner-fd", str(self.owner_fd), "--ready-fd", str(ready_write)], env=environment, cwd=str(Path.home()),
                    pass_fds=(self.owner_fd, ready_write), stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
            os.close(ready_write)
            for _ in range(200):
                readable, _, _ = select.select([ready_read], [], [], .1)
                if readable and os.read(ready_read, 32) == b"ready\n":
                    os.close(ready_read)
                    threading.Thread(target=child.wait, daemon=True).start()
                    return 0
                if child.poll() is not None:
                    os.close(ready_read)
                    # Only idle work crossed this boundary. Rebuild the retained
                    # old runtime and adopt only known queued (never sending) work.
                    self.instance = uuid.uuid4().hex
                    self.pending = None
                    self.upgrading = False
                    self.error = "Candidate failed to start; retained runtime restored."
                    os.environ["RIBBON_RUNTIME_QUEUE_OWNER"] = sessions.queue.owner
                    os.environ["RIBBON_RUNTIME_SCHEDULE_START"] = str(sessions.schedules.started_at)
                    return self.serve()
            os.close(ready_read)
            # The candidate owns the inherited lock. Never kill or start another
            # process on an ambiguous timeout; leave recovery evidence in log.
            raise RuntimeError("Candidate startup is uncertain; retained payload remains available.")
        record_path = self.directory / "discovery.json"
        if self.explicit_stop:
            atomic_json(self.directory / "stop-proof.json", {"instance": self.instance, "database": self.database,
                                                           "schema": SCHEMA_VERSION, "protocol": PROTOCOL_VERSION})
        if record_path.exists() and read_json(record_path).get("instance") == self.instance:
            record_path.unlink()
        return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db")
    parser.add_argument("--resources")
    parser.add_argument("--runtime-bootstrap", action="store_true")
    parser.add_argument("--runtime-serve", action="store_true")
    parser.add_argument("--runtime-probe", action="store_true")
    parser.add_argument("--runtime-start-paused", action="store_true")
    parser.add_argument("--runtime-command", choices=("status", "pause", "resume", "stop", "decision"))
    parser.add_argument("--inherit-owner-fd", type=int)
    parser.add_argument("--ready-fd", type=int)
    args = parser.parse_args(argv)
    if args.runtime_probe:
        from . import ui
        for path in Path(__file__).parent.rglob("*.py"):
            compile(path.read_bytes(), str(path), "exec")
        return 0
    if not args.db:
        parser.error("--db is required")
    if args.runtime_bootstrap:
        print(json.dumps(bootstrap(args.db, args.resources, start_paused=args.runtime_start_paused)), flush=True)
        return 0
    if args.runtime_command:
        directory = runtime_directory(args.db)
        values = json.load(sys.stdin) if args.runtime_command == "decision" else {}
        record = discover(args.db)
        if not record:
            proof_path = directory / "stop-proof.json"
            proof = read_json(proof_path) if proof_path.exists() else {}
            if args.runtime_command == "stop" and owner_available(directory):
                print(json.dumps({"status": "stopped", "instance": proof.get("instance")}))
                return 0
            if (args.runtime_command == "decision" and values.get("stopped_instance")
                    and proof.get("instance") == values["stopped_instance"]
                    and proof.get("database") == str(Path(args.db).resolve())
                    and owner_available(directory) and compatible(values.get("target", {}))
                    and read_json(directory / "schema.json")["schema"] <= SCHEMA_VERSION):
                print(json.dumps({"status": "stopped", "compatible": True}))
                return 0
            raise RuntimeError("No authenticated runtime is available.")
        result = control(record, args.runtime_command, **values)
        if args.runtime_command == "stop":
            for _ in range(300):
                if owner_available(directory):
                    result = {"status": "stopped", "instance": record["instance"]}
                    break
                time.sleep(.1)
            else:
                raise RuntimeError("Runtime has not confirmed exit; work may still be stopping.")
        print(json.dumps(result), flush=True)
        return 0
    if args.runtime_serve:
        directory = runtime_directory(args.db)
        fd = args.inherit_owner_fd
        if fd is not None:
            actual, expected = os.fstat(fd), (directory / "owner.lock").stat()
            if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
                raise RuntimeError("Invalid inherited runtime ownership.")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            fd = lock_file(directory / "owner.lock")
        (directory / "stop-proof.json").unlink(missing_ok=True)
        _OWNED_DATABASES.add(str(Path(args.db).resolve()))
        try:
            return Runtime(args.db, Path(sys.argv[0]).resolve().parent, fd, args.ready_fd,
                           start_paused=args.runtime_start_paused).serve()
        finally:
            os.close(fd)
    parser.error("Choose a runtime operation")
