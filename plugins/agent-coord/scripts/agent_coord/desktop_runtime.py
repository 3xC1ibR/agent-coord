"""Private, versioned desktop runtime with permanent loopback edge and blue/green cutover.

The canonical database owns the lock/discovery directory. Payloads are retained
copies, never executable paths in a replaceable app bundle. Control uses a
separate random credential and loopback port, unavailable to web clients.
"""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
import fcntl
import functools
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler
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
import urllib.parse
import urllib.request
import uuid

from .local_http import LocalHTTPServer

PROTOCOL_VERSION = 1
SCHEMA_VERSION = 2
UPDATE_ADMISSION_SECONDS = 30
HTTP_RESPONSE_DRAIN_SECONDS = 0.15
REVIEW_NOTICE = ("The backend update finished, but work stays paused for review because the previous "
                 "backend could not confirm a clean shutdown. Waiting will not resume it. Inspect running "
                 "tools, then run the runtime maintenance resume command.")
_OWNED_DATABASES = set()


class RuntimeOwnerUnavailableError(RuntimeError):
    """An ownership lock remains held while its discovered endpoint is unavailable."""


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_OWNED_DATABASES.clear)


def compatibility_metadata(version):
    return {"runtime_version": str(version), "runtime_protocol": PROTOCOL_VERSION,
            "ui_protocol_min": PROTOCOL_VERSION, "ui_protocol_max": PROTOCOL_VERSION,
            "runtime_schema_min": 1, "runtime_schema_max": SCHEMA_VERSION,
            "provider_transport_protocol": 1, "coordinator_projection_version": 1}


def compatible(target):
    keys = ("runtime_protocol", "ui_protocol_min", "ui_protocol_max", "runtime_schema_min", "runtime_schema_max")
    if not isinstance(target, dict) or any(type(target.get(key)) is not int or target[key] < 1 for key in keys):
        return False
    return (target.get("runtime_protocol") == PROTOCOL_VERSION
            and target.get("ui_protocol_min", 0) <= PROTOCOL_VERSION <= target.get("ui_protocol_max", 0)
            and target.get("runtime_schema_min", 0) <= SCHEMA_VERSION <= target.get("runtime_schema_max", 0))


def version_key(metadata):
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
        parent = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path):
    return json.loads(Path(path).read_text())


def _write_ephemeral_json(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


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


def _edge_control(port: int, token: str, method: str, route: str, timeout: float = 1.5) -> dict | None:
    try:
        with closing(http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)) as connection:
            connection.request(method, route, headers={"Authorization": "Bearer " + token})
            response = connection.getresponse()
            raw = response.read(65536)
            return json.loads(raw) if response.status == 200 else None
    except (OSError, http.client.HTTPException, ValueError):
        return None


def ensure_edge(directory: Path, database: str | Path, payload: Path) -> dict:
    edge_dir = directory / "runtime-edge"
    edge_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    canonical_db = str(Path(database).expanduser().resolve())
    record_path = edge_dir / "owner.json"
    launch_fd = lock_file(edge_dir / "launch.lock", blocking=True)
    try:
        existing = read_json(record_path) if record_path.exists() else {}
        if not owner_available(edge_dir):
            if existing.get("database") == canonical_db and existing.get("port") and existing.get("control_token"):
                verified = _edge_control(int(existing["port"]), existing["control_token"], "GET", "/_runtime/edge")
                if verified and verified.get("instance") == existing.get("instance"):
                    return existing
            raise RuntimeError("Loopback edge owner is locked without an authenticated control endpoint.")
        preferred_port = int(existing["port"]) if existing.get("database") == canonical_db and existing.get("port") else 0
        environment = payload_environment(payload)
        if existing.get("database") == canonical_db and isinstance(existing.get("csrf_token"), str):
            environment["RIBBON_EDGE_CSRF_TOKEN"] = existing["csrf_token"]
        owner_fd = lock_file(edge_dir / "owner.lock")
        ready_read, ready_write = os.pipe()
        try:
            python_bin = payload_command(payload)[0]
            command = [python_bin, "-B", "-m", "agent_coord.runtime_edge", "--directory", str(directory),
                       "--database", canonical_db, "--inherit-owner-fd", str(owner_fd),
                       "--ready-fd", str(ready_write), "--preferred-port", str(preferred_port)]
            with (edge_dir / "owner.log").open("ab", buffering=0) as log:
                child = subprocess.Popen(command, pass_fds=(owner_fd, ready_write), env=environment,
                                         cwd=str(Path.home()), stdin=subprocess.DEVNULL,
                                         stdout=log, stderr=log, start_new_session=True)
            os.close(ready_write)
            ready_write = None
            os.close(owner_fd)
            owner_fd = None
            readable, _, _ = select.select([ready_read], [], [], 10.0)
            if not readable or os.read(ready_read, 32) != b"ready\n":
                if child.poll() is None:
                    child.terminate()
                    child.wait(timeout=3)
                raise RuntimeError("Loopback edge proxy failed to confirm readiness.")
            threading.Thread(target=child.wait, daemon=True).start()
            return read_json(record_path)
        finally:
            for descriptor in (owner_fd, ready_read, ready_write):
                if descriptor is not None:
                    os.close(descriptor)
    finally:
        os.close(launch_fd)


def stop_edge(directory: Path) -> None:
    edge_dir = directory / "runtime-edge"
    atomic_json(directory / "edge-route.json", {"state": "stopped", "updated_at": time.monotonic()})
    record_path = edge_dir / "owner.json"
    if not record_path.exists():
        return
    try:
        record = read_json(record_path)
    except (OSError, ValueError):
        return
    if not owner_available(edge_dir) and record.get("port") and record.get("control_token"):
        _edge_control(int(record["port"]), record["control_token"], "POST", "/_runtime/stop", timeout=2.0)
    deadline = time.monotonic() + 5.0
    while not owner_available(edge_dir) and time.monotonic() < deadline:
        time.sleep(0.02)


def confirm_stopped(directory, database, instance):
    """Bind Stop proof to an observed exclusive ownership boundary."""
    try:
        descriptor = lock_file(directory / "owner.lock")
    except BlockingIOError:
        return False
    transport_descriptor = None
    edge_descriptor = None
    try:
        transport = directory / "provider-transport"
        if (transport / "owner.json").exists():
            return False
        if (transport / "owner.lock").exists():
            try:
                transport_descriptor = lock_file(transport / "owner.lock")
            except BlockingIOError:
                return False
        edge_dir = directory / "runtime-edge"
        if (edge_dir / "owner.lock").exists():
            try:
                edge_descriptor = lock_file(edge_dir / "owner.lock")
            except BlockingIOError:
                return False
        atomic_json(directory / "stop-proof.json", {"instance": instance,
            "database": str(Path(database).resolve()), "schema": SCHEMA_VERSION, "protocol": PROTOCOL_VERSION})
    finally:
        if edge_descriptor is not None:
            os.close(edge_descriptor)
        if transport_descriptor is not None:
            os.close(transport_descriptor)
        os.close(descriptor)
    return True


def stop_failure_path(directory, instance):
    return directory / "stop-failures" / (str(uuid.UUID(instance)) + ".json")


def record_stop_failure(directory, database, instance, transport_instance, error):
    path = stop_failure_path(directory, instance)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_json(path, {"runtime_instance": instance, "database": str(Path(database).resolve()),
                       "transport_instance": transport_instance, "error": str(error)})


def retained_stop_failure(directory, database, instance):
    """Read a matching failed Stop only after both owners release their locks."""
    path = stop_failure_path(directory, instance)
    if not path.exists():
        return None
    descriptors = []
    try:
        descriptors.append(lock_file(directory / "owner.lock"))
        descriptors.append(lock_file(directory / "provider-transport/owner.lock"))
        failure = read_json(path)
        transport = read_json(directory / "provider-transport/owner.json")
        if (isinstance(failure, dict) and isinstance(transport, dict)
                and failure.get("runtime_instance") == instance
                and failure.get("database") == str(Path(database).resolve())
                and isinstance(failure.get("transport_instance"), str) and failure["transport_instance"]
                and failure.get("transport_instance") == transport.get("instance")
                and isinstance(failure.get("error"), str) and failure["error"]):
            return ("Provider transport owner exited, but surviving providers cannot be verified by this Stop request. "
                    "Retained evidence was preserved. Shutdown error: " + failure["error"])
    except (OSError, ValueError):
        return None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
    return None


@functools.lru_cache(maxsize=1)
def schema_fingerprint():
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).parent.glob("*.py")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def schema_initialized(database, fingerprint, *, check_cookie=True):
    path = Path(database).resolve()
    if not path.exists():
        return False
    try:
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=.1)
        try:
            row = connection.execute("SELECT fingerprint, schema_cookie FROM coordination_schema WHERE id=1").fetchone()
            cookie = connection.execute("PRAGMA schema_version").fetchone()[0]
            return bool(row and row[0] == fingerprint and (not check_cookie or row[1] == cookie))
        finally:
            connection.close()
    except sqlite3.OperationalError:
        return False


@contextmanager
def schema_access(database, *, read_only=False):
    """Run migrations once; current-schema readers never take the schema lock."""
    directory = runtime_directory(database)
    record = directory / "schema.json"
    fingerprint = schema_fingerprint()
    from .runtime_requests import owner_operation, owns_database

    def verify():
        previous = read_json(record) if record.exists() else None
        owner = owns_database(database)
        update_active = False
        update_record = directory / "update.json"
        if update_record.exists():
            try:
                update_active = read_json(update_record).get("phase") not in {"committed", "rolled-back", "failed"}
            except (OSError, ValueError):
                update_active = False
        if previous and previous["fingerprint"] != fingerprint and not owner_available(directory) and not owner and not update_active:
            raise RuntimeError("A different Ribbon Field runtime owns this database. Wait for its idle upgrade before using the new CLI or hooks.")
        if previous and previous["schema"] > SCHEMA_VERSION:
            raise RuntimeError("This database requires a newer Ribbon Field runtime; downgrade is unsafe.")
        return previous

    previous = verify()
    live_owner = str(Path(database).resolve()) in _OWNED_DATABASES
    if not live_owner and not owns_database(database) and not owner_available(directory):
        try:
            live_owner = discover(database, timeout=.1) is not None
        except (OSError, RuntimeError, ValueError):
            live_owner = False
    if previous and previous["fingerprint"] == fingerprint and schema_initialized(database, fingerprint, check_cookie=not (live_owner or read_only)):
        yield False
        return
    if read_only:
        raise RuntimeError("Database initialization is required. Start the matching Ribbon Field runtime before querying it.")
    with owner_operation(database) as owner:
        if not owner:
            raise RuntimeError("Database initialization is owned by the running Ribbon Field runtime.")
        descriptor = lock_file(directory / "schema.lock", blocking=True)
        try:
            previous = verify()
            initialize = not schema_initialized(database, fingerprint)
            yield initialize
            if previous is None or previous["fingerprint"] != fingerprint:
                atomic_json(record, {"schema": SCHEMA_VERSION, "fingerprint": fingerprint})
        finally:
            os.close(descriptor)


class AdmissionGate:
    """Close admission before inspecting idle; count complete routing operations."""

    def __init__(self):
        self.lock = threading.RLock()
        self.local = threading.local()
        self.paused = False
        self.inflight = 0
        self.notice = None

    def message(self, default):
        return self.notice or default

    @contextmanager
    def enter(self, *, maintenance=False):
        with self.lock:
            depth = getattr(self.local, "depth", 0)
            allowed = bool(depth or not self.paused or maintenance)
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
                    raise BrowserBusyError(self.message("Runtime maintenance is pausing new work. Try again shortly."))
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


def uninterrupted_readiness(sessions):
    """Provider work transfers online across all brokered providers."""
    if not hasattr(sessions, "projection"):
        return ["provider transport ownership is not transferable"]
    reasons = []
    if sessions.admission.inflight:
        reasons.append("admitted operations")
    owns_provider = getattr(sessions.projection, "owns_provider", None)
    channels = getattr(sessions.projection, "channels", None)
    with sessions.lock:
        for identity in sessions.active:
            client = sessions.loaded_clients.get(identity, "codex")
            if callable(owns_provider):
                brokered = owns_provider(client, identity)
            elif isinstance(channels, dict):
                brokered = client in {"codex", "claude"} or any(
                    entry.get("provider") == client and entry.get("thread_id") in (None, identity) and not entry.get("closed")
                    for entry in channels.values()
                )
            else:
                brokered = client in {"codex", "claude"}
            if not brokered:
                reasons.append("active unbrokered provider: " + client)
    if sessions.projection.error:
        reasons.append("provider projection requires recovery")
    return reasons


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
    request = urllib.request.Request(
        url, method="POST",
        data=json.dumps({"action": action, "instance": record["instance"], **values}).encode(),
        headers={"Authorization": "Bearer " + record["control_token"], "Content-Type": "application/json"},
    )
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
            if str(exc) != "Unknown runtime control action.":
                raise
            status = control(record, "status", timeout=timeout)
    except (OSError, ValueError, RuntimeError):
        if not owner_available(directory):
            raise RuntimeOwnerUnavailableError("The runtime owner is unavailable. Refusing a second owner.")
        return None
    if status.get("protocol") != PROTOCOL_VERSION:
        raise RuntimeError("The running runtime is incompatible with this desktop.")
    return {**record, "payload": status.get("payload"), "client_requests": status.get("client_requests")}


def bootstrap(database, resources, *, start_paused=False, require_confirmation=False):
    database = str(Path(database).expanduser().resolve())
    directory = runtime_directory(database)
    launcher = lock_file(directory / "launch.lock", blocking=True)
    try:
        payload = stage_payload(Path(resources), directory)
        selected_payload = payload
        deadline = time.monotonic() + 20
        while True:
            try:
                record = discover(database)
            except RuntimeOwnerUnavailableError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.05)
                continue
            if record:
                if start_paused and not control(record, "status")["paused"]:
                    raise RuntimeError("A runtime is already admitting work. Pause it explicitly before maintenance.")
                return control(record, "attach", payload=str(payload), require_confirmation=require_confirmation)
            try:
                owner_fd = lock_file(directory / "owner.lock")
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Runtime ownership remained busy; no runtime was started.") from None
                time.sleep(.05)
        try:
            from . import runtime_update
            payload = runtime_update.recover(database, directory, payload)
            update = runtime_update.read(directory)
            start_paused = start_paused or bool(update and update.get("requires_review"))
            ensure_edge(directory, database, payload)
            with (directory / "runtime.log").open("ab", buffering=0) as log:
                command = payload_command(payload) + ["--runtime-supervise", "--db", database,
                                                       "--inherit-owner-fd", str(owner_fd)]
                if start_paused:
                    command.append("--runtime-start-paused")
                child = subprocess.Popen(command, pass_fds=(owner_fd,),
                                         env=payload_environment(payload), cwd=str(Path.home()), stdin=subprocess.DEVNULL,
                                         stdout=log, stderr=log, start_new_session=True)
        finally:
            os.close(owner_fd)
        for _ in range(200):
            if child.poll() is not None:
                raise RuntimeError("The detached runtime failed to start; see " + str(directory / "runtime.log"))
            try:
                record = discover(database)
            except RuntimeError:
                record = None
            if record:
                threading.Thread(target=child.wait, daemon=True).start()
                return control(record, "attach", payload=str(selected_payload), require_confirmation=require_confirmation)
            time.sleep(.05)
        raise RuntimeError("Runtime startup timed out; ownership remains reserved until it exits.")
    finally:
        os.close(launcher)


class Runtime:
    def __init__(self, database, payload, owner_fd, ready_fd=None, *, start_paused=False, commit_fd=None,
                 commit_ack_fd=None, supervisor_req_fd=None, supervisor_ack_fd=None):
        self.database = str(Path(database).resolve())
        self.directory = runtime_directory(database)
        self.payload = Path(payload).resolve()
        self.owner_fd = owner_fd
        self.ready_fd = ready_fd
        self.commit_fd = commit_fd
        self.commit_ack_fd = commit_ack_fd
        self.supervisor_req_fd = supervisor_req_fd
        self.supervisor_ack_fd = supervisor_ack_fd
        self.supervisor_lock = threading.Lock()
        self.trial = commit_fd is not None
        self.update_requested = None
        self.update_drain_started = None
        self.instance = uuid.uuid4().hex
        self.token = secrets.token_urlsafe(32)
        self.pending = None
        self.pending_confirmation = False
        self.selected = self.payload
        self.error = None
        self.mutex = threading.RLock()
        self.stopping = threading.Event()
        self.wake_event = threading.Event()
        self.upgrading = False
        self.handover_stopping = False
        self.maintenance = start_paused
        self.explicit_stop = False
        self.server = None
        self.edge_url = None
        self.edge_pid = None
        self.upstream_csrf_token = ""
        self._active_http_reads = 0
        self._http_reads_lock = threading.Lock()
        self.recovery_started = None
        self.recovery_running = False
        self.supervised = os.environ.pop("RIBBON_RUNTIME_SUPERVISED", "") == "1"
        self.supervisor_exit_fd = None

    def _wake(self):
        self.wake_event.set()

    def ensure_supervisor(self):
        """Adopt a legacy direct launch only after its durable commit."""
        if self.supervised:
            return
        exit_read, exit_write = os.pipe()
        ready_read, ready_write = os.pipe()
        req_read, req_write = os.pipe()
        ack_read, ack_write = os.pipe()
        child = None
        try:
            command = payload_command(self.payload) + [
                "--runtime-supervise", "--db", self.database,
                "--inherit-owner-fd", str(self.owner_fd), "--exit-fd", str(exit_read),
                "--source-instance", self.instance, "--ready-fd", str(ready_write),
                "--adopted-req-fd", str(req_read), "--adopted-ack-fd", str(ack_write),
            ]
            with (self.directory / "runtime.log").open("ab", buffering=0) as log:
                child = subprocess.Popen(
                    command, pass_fds=(self.owner_fd, exit_read, ready_write, req_read, ack_write),
                    env=payload_environment(self.payload), cwd=str(Path.home()), stdin=subprocess.DEVNULL,
                    stdout=log, stderr=log, start_new_session=True,
                )
            for name in ("exit_read", "ready_write", "req_read", "ack_write"):
                os.close(locals()[name])
            exit_read = ready_write = req_read = ack_write = None
            readable, _, _ = select.select([ready_read], [], [], 10)
            if not readable or os.read(ready_read, 32) != b"ready\n":
                raise RuntimeError("The adopted runtime supervisor did not confirm ownership.")
            self.supervisor_exit_fd = exit_write
            self.supervisor_req_fd = req_write
            self.supervisor_ack_fd = ack_read
            exit_write = req_write = ack_read = None
            self.supervised = True
            threading.Thread(target=child.wait, daemon=True).start()
        except BaseException:
            if child is not None and child.poll() is None:
                child.terminate()
                child.wait(timeout=5)
            raise
        finally:
            for descriptor in (exit_read, exit_write, ready_read, ready_write, req_read, req_write, ack_read, ack_write):
                if descriptor is not None:
                    os.close(descriptor)

    def _supervisor_call(self, message: dict, timeout: float = 35.0) -> dict:
        with self.supervisor_lock:
            if self.supervisor_req_fd is None or self.supervisor_ack_fd is None:
                raise RuntimeError("Supervisor communication channel is unavailable.")
            os.write(self.supervisor_req_fd, (json.dumps(message) + "\n").encode("utf-8"))
            buffer = bytearray()
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                readable, _, _ = select.select([self.supervisor_ack_fd], [], [], 0.1)
                if not readable:
                    continue
                chunk = os.read(self.supervisor_ack_fd, 4096)
                if not chunk:
                    raise RuntimeError("Supervisor closed acknowledgement channel.")
                buffer.extend(chunk)
                if b"\n" in buffer:
                    line, _ = buffer.split(b"\n", 1)
                    return json.loads(line.decode("utf-8"))
            raise RuntimeError("Supervisor operation timed out.")

    def identity(self):
        """Immutable owner identity; independent of readiness and session locks."""
        return {"database": self.database, "instance": self.instance,
                "protocol": PROTOCOL_VERSION, "schema": SCHEMA_VERSION, "pid": os.getpid(),
                "payload": str(self.payload), "client_requests": 1, "runtime_updates": 1,
                "runtime_update_prompts": 1, "uninterrupted_updates": 1,
                "interrupted_provider_continuation": 0, "transfer_admission_probe": 1}

    def update_status(self):
        """Read-only update transaction, blockers and per-conversation recovery."""
        from . import runtime_update
        from .runtime_recovery import status
        update = runtime_update.read(self.directory)
        if update:
            update["recovery"] = status(self.server.browser_sessions.store, update["id"])
            if update["phase"] not in runtime_update.TERMINAL:
                update["blocked"] = uninterrupted_readiness(self.server.browser_sessions)
                if self.update_drain_started is not None:
                    update["admission_drain_seconds"] = time.monotonic() - self.update_drain_started
        return update

    def public_url(self) -> str:
        if self.edge_url:
            return self.edge_url
        if self.server is not None:
            return f"http://127.0.0.1:{self.server.server_port}/"
        return ""

    def publish_edge_route(self, *, state: str = "serving") -> None:
        if self.server is None or not self.edge_url:
            return
        now = time.monotonic()
        route_path = self.directory / "edge-route.json"
        last_cutover_ms = None
        if state == "serving" and route_path.exists():
            try:
                previous = read_json(route_path)
                if previous.get("state") == "cutover" and isinstance(previous.get("cutover_started_monotonic"), (int, float)):
                    last_cutover_ms = round((now - float(previous["cutover_started_monotonic"])) * 1000.0, 2)
                elif isinstance(previous.get("last_cutover_ms"), (int, float)):
                    last_cutover_ms = previous["last_cutover_ms"]
            except (OSError, ValueError):
                pass
        admission_paused = bool(self.server.browser_sessions.admission.paused)
        explicit_pause = bool(self.maintenance)
        payload = {
            "state": state,
            "instance": self.instance,
            "upstream_port": self.server.server_port,
            "csrf_token": self.upstream_csrf_token,
            "paused": explicit_pause,
            "last_cutover_ms": last_cutover_ms,
            "health": {
                **self.identity(),
                "upstream_url": f"http://127.0.0.1:{self.server.server_port}/",
                "pending": str(self.pending) if self.pending else None,
                "pending_confirmation": bool(self.pending and self.pending_confirmation),
                "upgrade_error": self.error,
                "paused": admission_paused,
                "last_cutover_ms": last_cutover_ms,
            },
            "updated_at": now,
        }
        if state == "cutover":
            payload["cutover_started_monotonic"] = now
        _write_ephemeral_json(route_path, payload)

    def status(self):
        from .database_writer import performance_snapshot
        update = self.update_status()
        projection = getattr(self.server.browser_sessions, "projection", None)
        transport = None
        if projection is not None:
            transport = {"pid": projection.client.record["pid"], "instance": projection.client.record["instance"],
                         "epoch": projection.client.epoch, "cursor": projection.cursor,
                         "error": projection.error or projection.delivery_error,
                         "providers": [{"channel": name, "pid": entry["process"].pid,
                                        "pgid": entry["process"].pid, "thread_id": entry.get("thread_id")}
                                       for name, entry in projection.channels.items()]}
        upstream_url = f"http://127.0.0.1:{self.server.server_port}/"
        state_name = "preparing" if self.trial else ("updating" if self.handover_stopping else "serving")
        return {**self.identity(), "status": state_name,
                "url": self.public_url() or upstream_url,
                "upstream_url": upstream_url,
                "edge_pid": self.edge_pid,
                "pending": str(self.pending) if self.pending else None,
                "pending_confirmation": bool(self.pending and self.pending_confirmation),
                "upgrade_error": self.error,
                "paused": self.server.browser_sessions.admission.paused,
                "busy": readiness(self.server.browser_sessions),
                "update": update,
                "transport": transport,
                "database_metrics": performance_snapshot(self.server.browser_sessions, refresh_coverage=False)}

    def request_update(self, request_id, payload):
        from . import runtime_update
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,100}", request_id):
            raise RuntimeError("An update request ID is required.")
        previous = runtime_update.read(self.directory)
        if previous and previous["id"] == request_id:
            if previous["target"] != payload:
                raise RuntimeError("Update request ID was already used for another payload.")
            return previous
        if previous and previous["phase"] not in runtime_update.TERMINAL:
            raise RuntimeError("Another runtime update is already in progress.")
        if self.upgrading or self.maintenance:
            raise RuntimeError("Runtime maintenance is already in progress.")
        if not self.pending or str(self.pending) != payload:
            raise RuntimeError("The staged backend changed. Refresh before updating.")
        self.update_requested = runtime_update.write(self.directory, {
            "id": request_id, "phase": "accepted", "previous": str(self.payload),
            "target": payload, "source_instance": self.instance, "database": self.database,
            "created_at": time.time(), "error": None})
        self.update_drain_started = None
        self.publish_edge_route()
        self._wake()
        return self.update_requested

    def prepare_handover(self):
        """Drain admitted operations cleanly for handover when all providers are brokered."""
        from . import runtime_update
        sessions = self.server.browser_sessions
        blocked = uninterrupted_readiness(sessions)
        if blocked and (not self.update_requested or blocked != ["admitted operations"]):
            if self.update_drain_started is not None:
                self.update_requested = runtime_update.write(
                    self.directory, self.update_requested, phase="accepted",
                    admission_drain_seconds=time.monotonic() - self.update_drain_started)
                self.update_drain_started = None
                sessions.admission.resume()
                sessions.queue.wake()
                sessions.dispatch.wake()
                self.publish_edge_route()
            return False
        sessions.admission.pause()
        if self.update_requested and self.update_drain_started is None:
            self.update_drain_started = time.monotonic()
            self.update_requested = runtime_update.write(
                self.directory, self.update_requested, phase="draining", admission_drain_started_at=time.time())
        blocked = uninterrupted_readiness(sessions)
        if self.update_requested:
            elapsed = time.monotonic() - self.update_drain_started
            if blocked and elapsed >= UPDATE_ADMISSION_SECONDS:
                self.error = "Accepted operations did not drain; the current backend remains active."
                runtime_update.write(self.directory, self.update_requested, phase="failed",
                                     error=self.error, blocked=blocked, admission_drain_seconds=elapsed)
                self.update_requested = None
                self.update_drain_started = None
                self.pending = None
            elif not blocked:
                self.update_requested = runtime_update.write(
                    self.directory, self.update_requested, blocked=[], admission_drain_seconds=elapsed)
                self.update_drain_started = None
        if blocked:
            if not self.update_requested:
                sessions.admission.resume()
                sessions.queue.wake()
                sessions.dispatch.wake()
                self.publish_edge_route()
            return False
        return True

    def validate_candidate(self, candidate, *, capacity=False):
        try:
            entry = Path(candidate) / "backend.py"
            compile(entry.read_bytes(), str(entry), "exec")
            for path in (Path(candidate) / "backend").rglob("*.py"):
                compile(path.read_bytes(), str(path), "exec")
            return None
        except (OSError, RuntimeError, SyntaxError, ValueError) as exc:
            return str(exc)

    def abandon_unstarted_update(self, update):
        from . import runtime_update
        if not update or update.get("source_instance") == self.instance:
            return
        stopped_before_transfer = (update["phase"] == "stopping" and update.get("uninterrupted")
                                   and update.get("previous") == str(self.payload))
        if not stopped_before_transfer and (update["phase"] not in {"accepted", "draining"}
                                            or update.get("uninterrupted")):
            return
        runtime_update.write(self.directory, update, phase="failed", notice=None,
                             error="The backend restarted before this update started; the current backend remains active.")

    def reopen_work(self, sessions):
        if self.stopping.is_set() or self.maintenance:
            return
        if not self.start_recovery():
            sessions.admission.resume()
            sessions.queue.wake()
            sessions.dispatch.wake()
        self.publish_edge_route()

    def start_recovery(self):
        """Run saved continuation decisions once per legacy unbrokered update."""
        from . import runtime_update
        sessions = self.server.browser_sessions
        if self.recovery_running:
            return True
        update = runtime_update.read(self.directory)
        if (not update or update["id"] == self.recovery_started or update.get("cancelled")
                or update.get("recovery_uncertain")):
            return False
        eligible = not update.get("uninterrupted") and update["phase"] in {"committed", "rolled-back"}
        if not eligible:
            return False
        self.recovery_started = update["id"]
        self.recovery_running = True
        sessions.admission.pause()

        def recover_work():
            from .runtime_recovery import RuntimeRecovery

            def permitted():
                current = runtime_update.read(self.directory)
                return not self.stopping.is_set() and not self.maintenance and not current.get("cancelled")

            try:
                with sessions.admission.enter(maintenance=True):
                    RuntimeRecovery(sessions, permitted=permitted).recover(update["id"])
            except Exception as exc:
                self.error = "Conversation recovery requires review: " + str(exc)
            finally:
                with self.mutex:
                    self.recovery_running = False
                    if not self.stopping.is_set() and not self.maintenance:
                        sessions.admission.resume()
                        sessions.queue.wake()
                        sessions.dispatch.wake()
                    self.publish_edge_route()

        threading.Thread(target=recover_work, daemon=True, name="runtime-recovery").start()
        return True

    def resume_work(self):
        sessions = self.server.browser_sessions
        if self.stopping.is_set() or self.maintenance or self.recovery_running:
            return
        self.ensure_supervisor()
        projection = getattr(sessions, "projection", None)
        if projection is not None and projection.worker is None:
            projection.start()
        if not self.start_recovery():
            sessions.admission.resume()
            sessions.queue.wake()
            sessions.dispatch.wake()
        self.publish_edge_route()

    def activate_committed(self):
        from . import runtime_update
        with self.mutex:
            update = runtime_update.read(self.directory)
            if (not update or update["phase"] != "committed" or update["target"] != str(self.payload)
                    or update.get("cancelled") or self.stopping.is_set()):
                raise RuntimeError("Candidate activation was not committed.")
            self.trial = False
            self.maintenance = bool(update.get("requires_review"))
            if self.maintenance:
                self.hold_for_review()
            else:
                self.resume_work()
            schema_path = self.directory / "schema.json"
            desired_schema = {"schema": SCHEMA_VERSION, "fingerprint": schema_fingerprint()}
            if not schema_path.exists() or read_json(schema_path) != desired_schema:
                atomic_json(schema_path, desired_schema)

    def hold_for_review(self):
        self.error = "Backend updated; work remains paused because shutdown was incomplete. Inspect running tools before resuming."
        self.server.browser_sessions.admission.notice = REVIEW_NOTICE
        self.publish_edge_route()

    def dispatch(self, body):
        action = body.get("action")
        if self.trial and action not in {"identity", "status", "update-status", "stop"}:
            raise RuntimeError("Candidate activation has not been committed.")
        if action == "update-status":
            return {**self.identity(), "update": self.update_status()}
        if action == "identity":
            return self.identity()
        if action == "transfer-admission":
            sessions = self.server.browser_sessions
            admission = sessions.admission
            target = {}
            if "thread_id" in body or "expected_turn_id" in body:
                thread, turn = body.get("thread_id"), body.get("expected_turn_id")
                if not all(isinstance(value, str) and 0 < len(value) <= 200 for value in (thread, turn)):
                    raise RuntimeError("A thread and turn identity are required for steering.")
                with sessions.lock:
                    target["matches_active_turn"] = sessions.active.get(thread) == turn
            with admission.lock:
                return {**self.identity(), **target, "status": "preparing" if self.trial else "serving",
                        "paused": admission.paused, "url": self.public_url()}
        if action == "client-request":
            from .runtime_requests import execute_request
            from .runtime_metadata import install_operations
            install_operations()
            sessions = self.server.browser_sessions
            with sessions.admission.enter() as allowed:
                if not allowed:
                    return {**self.identity(), "failure": {
                        "type": "RuntimeRequestUnavailable" if sessions.admission.notice else "CoordinationError",
                        "message": sessions.admission.message("Runtime maintenance is pausing new coordination requests.")}}
                return {**self.identity(), **execute_request(sessions.store, body)}
        with self.mutex:
            sessions = self.server.browser_sessions
            if action == "cancel-update-authorization":
                (self.directory / "update-authorization.json").unlink(missing_ok=True)
                return self.identity()
            if action == "activate-update":
                update = self.request_update(body.get("request_id"), body.get("payload"))
                return {**self.identity(), "update": update}
            if action == "attach":
                candidate = Path(body["payload"]).resolve()
                if candidate.parent != self.directory / "payloads" or not candidate.is_dir():
                    raise RuntimeError("Only a retained runtime payload can be attached.")
                metadata = read_json(candidate / "backend.json")
                if not compatible(metadata):
                    raise RuntimeError("Desktop and running runtime are incompatible; activation deferred.")
                if (candidate != self.payload and hasattr(sessions, "projection")
                        and (metadata.get("provider_transport_protocol") != 1
                             or metadata.get("coordinator_projection_version") != 1)):
                    raise RuntimeError("Candidate cannot restore the surviving provider transport; activation deferred.")
                if self.upgrading:
                    raise RuntimeError("Runtime handover is in progress. Reconnect shortly.")
                if self.update_requested and candidate != self.pending:
                    raise RuntimeError("A backend update is already in progress.")
                self.selected = candidate
                previous_pending = self.pending
                current_version = version_key(read_json(self.payload / "backend.json"))
                candidate_version = version_key(metadata)
                self.pending = (candidate if candidate != self.payload and current_version is not None
                                and candidate_version is not None and candidate_version >= current_version else None)
                if not self.pending:
                    self.pending_confirmation = False
                elif self.pending != previous_pending:
                    self.pending_confirmation = body.get("require_confirmation") is True
                elif body.get("require_confirmation") is True:
                    self.pending_confirmation = True
                if not sessions.admission.notice:
                    self.error = None
                from . import ui
                ui._WEB_ROOT = candidate / "backend/agent_coord/web"
                atomic_json(self.directory / "selected.json", {"payload": str(candidate)})
                authorization = self.directory / "update-authorization.json"
                if self.pending and authorization.exists():
                    authorized = read_json(authorization)
                    if (authorized.get("runtime_version") == metadata.get("runtime_version")
                            and authorized.get("source_instance") == self.instance):
                        self.request_update(authorized["id"], str(candidate))
                        authorization.unlink(missing_ok=True)
                self.publish_edge_route()
                if self.pending and not self.pending_confirmation:
                    self._wake()
            elif action == "decision":
                if not compatible(body.get("target", {})):
                    raise RuntimeError("The update is incompatible with the running runtime.")
                if not self.payload.is_dir() or self.upgrading:
                    raise RuntimeError("Runtime lifetime isolation is unavailable; update deferred.")
                if body.get("authorize_restart") is True:
                    atomic_json(self.directory / "update-authorization.json", {
                        "id": str(uuid.uuid4()), "source_instance": self.instance,
                        "runtime_version": body["target"]["runtime_version"]})
            elif action == "pause":
                self.maintenance = True
                sessions.admission.pause()
                self.publish_edge_route()
            elif action == "resume":
                if self.upgrading:
                    raise RuntimeError("Runtime handover is in progress.")
                from . import runtime_update
                update = runtime_update.read(self.directory)
                if update and update["phase"] in runtime_update.TERMINAL and update.get("requires_review"):
                    runtime_update.write(self.directory, update, requires_review=False, reviewed_at=time.time())
                    self.error = None
                sessions.admission.notice = None
                self.maintenance = False
                self.resume_work()
                self._wake()
            elif action == "stop":
                from . import runtime_update
                guard = lock_file(self.directory / "update-control.lock", blocking=True)
                try:
                    update = runtime_update.read(self.directory)
                    if update:
                        runtime_update.write(self.directory, update, cancelled=True)
                finally:
                    os.close(guard)
                if self.update_requested and not self.upgrading:
                    runtime_update.write(self.directory, self.update_requested, phase="failed", cancelled=True,
                                         error="Update cancelled by Stop Work and Quit.")
                    self.update_requested = None
                self.maintenance = True
                self.explicit_stop = True
                sessions.admission.pause()
                self.stopping.set()
                self._wake()
            elif action != "status":
                raise RuntimeError("Unknown runtime control action.")
            return self.status()

    def _fetch_local_csrf_token(self) -> str:
        with closing(http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5.0)) as connection:
            connection.request("GET", "/api/browser/config", headers={"Host": f"127.0.0.1:{self.server.server_port}"})
            response = connection.getresponse()
            raw = response.read(65536)
            if response.status != 200:
                raise RuntimeError("Local coordinator HTTP server failed startup readiness.")
            return str(json.loads(raw).get("token", ""))

    def _install_http_read_tracker(self) -> None:
        handler_class = self.server.RequestHandlerClass
        original_get = handler_class.do_GET
        runtime_self = self

        def tracked_get(handler):
            is_sse = handler.path.startswith("/api/browser/events")
            if not is_sse:
                with runtime_self._http_reads_lock:
                    runtime_self._active_http_reads += 1
            try:
                return original_get(handler)
            finally:
                if not is_sse:
                    with runtime_self._http_reads_lock:
                        runtime_self._active_http_reads -= 1

        handler_class.do_GET = tracked_get

    def _execute_blue_green_handover(self, candidate: Path) -> tuple[str, bool]:
        """Run warm standby launch + < 200 ms cutover. Returns (outcome, projection_frozen)."""
        from . import runtime_update
        sessions = self.server.browser_sessions
        with self.mutex:
            non_drain = [r for r in uninterrupted_readiness(sessions) if r != "admitted operations"]
            if non_drain:
                if self.update_drain_started is not None:
                    self.update_requested = runtime_update.write(
                        self.directory, self.update_requested, phase="accepted",
                        admission_drain_seconds=time.monotonic() - self.update_drain_started)
                    self.update_drain_started = None
                    sessions.admission.resume()
                    sessions.queue.wake()
                    sessions.dispatch.wake()
                    self.publish_edge_route()
                return "deferred", False
            self.upgrading = True

        failure = self.validate_candidate(candidate)
        if failure is not None:
            with self.mutex:
                if self.update_requested:
                    runtime_update.write(self.directory, self.update_requested, phase="failed",
                                         error="Candidate validation failed: " + failure)
                    self.update_requested = None
                self.error = "Candidate validation failed: " + failure
                self.pending = None
                self.upgrading = False
                self.reopen_work(sessions)
            return "failed", False

        with self.mutex:
            if self.stopping.is_set():
                self.upgrading = False
                return "stop", False
            update = self.update_requested or {
                "id": str(uuid.uuid4()), "phase": "accepted", "target": str(candidate),
                "previous": str(self.payload), "source_instance": self.instance,
                "database": self.database, "created_at": time.time(), "error": None,
            }
            last_update = runtime_update.read(self.directory)
            if not self.update_requested and last_update:
                runtime_update.write(self.directory, last_update, superseded_by=str(candidate))
            update = runtime_update.snapshot(self.database, self.directory, update)
            if self.update_requested:
                self.update_requested = update

        try:
            prepared = self._supervisor_call(
                {"op": "prepare_candidate", "target": str(candidate), "update_id": update["id"]},
                timeout=45.0,
            )
        except Exception as exc:
            with self.mutex:
                self.error = str(exc)
                if self.update_requested:
                    runtime_update.write(self.directory, update, phase="failed", error=str(exc))
                    self.update_requested = None
                self.pending = None
                self.upgrading = False
                self.publish_edge_route()
            return "failed", False

        if not prepared.get("ok"):
            if prepared.get("rollback_required"):
                return "rollback", False
            with self.mutex:
                self.error = prepared.get("error") or "Candidate failed to prepare."
                if self.update_requested:
                    runtime_update.write(self.directory, update, phase="failed", error=self.error)
                    self.update_requested = None
                self.pending = None
                self.upgrading = False
                self.publish_edge_route()
            return "failed", False

        if self.stopping.is_set():
            self._supervisor_call({"op": "abort_candidate"})
            return "rollback", False

        # Candidate is already warm in background. Drain admitted operations, then run < 200 ms atomic cutover!
        sessions.admission.pause()
        drain_started = time.monotonic()
        blocked = uninterrupted_readiness(sessions)
        if blocked:
            with self.mutex:
                self.update_drain_started = drain_started
                update = runtime_update.write(
                    self.directory, update, phase="draining", admission_drain_started_at=time.time())
                if self.update_requested:
                    self.update_requested = update
                self.publish_edge_route(state="serving")

        while blocked:
            if self.stopping.is_set():
                self._supervisor_call({"op": "abort_candidate"})
                return "rollback", False
            non_drain = [r for r in blocked if r != "admitted operations"]
            elapsed = time.monotonic() - drain_started
            if non_drain:
                self._supervisor_call({"op": "abort_candidate"})
                with self.mutex:
                    self.upgrading = False
                    self.update_drain_started = None
                    if self.update_requested:
                        self.update_requested = runtime_update.write(
                            self.directory, update, phase="accepted", admission_drain_seconds=elapsed)
                    sessions.admission.resume()
                    sessions.queue.wake()
                    sessions.dispatch.wake()
                    self.publish_edge_route()
                return "deferred", False
            if elapsed >= UPDATE_ADMISSION_SECONDS:
                self._supervisor_call({"op": "abort_candidate"})
                with self.mutex:
                    self.error = "Accepted operations did not drain; the current backend remains active."
                    runtime_update.write(self.directory, update, phase="failed",
                                         error=self.error, blocked=blocked, admission_drain_seconds=elapsed)
                    self.update_requested = None
                    self.update_drain_started = None
                    self.pending = None
                    self.upgrading = False
                    sessions.admission.resume()
                    sessions.queue.wake()
                    sessions.dispatch.wake()
                    self.publish_edge_route()
                return "failed", False
            time.sleep(0.002)
            blocked = uninterrupted_readiness(sessions)

        with self.mutex:
            self.update_drain_started = None
            if self.stopping.is_set():
                self._supervisor_call({"op": "abort_candidate"})
                return "rollback", False
            self.publish_edge_route(state="cutover")
            try:
                if not sessions.persistence.flush() or not sessions.claude.persistence.flush():
                    raise RuntimeError("Provider bookkeeping did not reach a saved boundary.")
                sessions.projection.freeze()
                schema_path = self.directory / "schema.json"
                if not schema_path.exists() or read_json(schema_path).get("schema", SCHEMA_VERSION) <= SCHEMA_VERSION:
                    runtime_update.clone_database_files_direct(
                        Path(self.database),
                        self.directory / "update-backup.sqlite3",
                        self.directory / "update-backup.sqlite3-wal",
                    )
                drain_seconds = time.monotonic() - drain_started
                sessions.transport_handover = True
                self.handover_stopping = True
            except Exception as exc:
                self._supervisor_call({"op": "abort_candidate"})
                self.error = str(exc)
                runtime_update.write(self.directory, update, phase="failed", error=str(exc))
                self.update_requested = None
                self.pending = None
                self.upgrading = False
                if not sessions.projection.stopped.is_set():
                    sessions.admission.resume()
                elif sessions.projection.worker is None or not sessions.projection.worker.is_alive():
                    sessions.projection.start()
                    sessions.admission.resume()
                else:
                    self.maintenance = True
                self.publish_edge_route()
                return "failed", False

        committed = self._supervisor_call({
            "op": "commit_candidate",
            "queue_owner": sessions.queue.owner,
            "schedule_start": sessions.schedules.started_at,
            "admission_drain_seconds": drain_seconds,
        })
        if not committed.get("ok"):
            return "rollback", True
        return "committed", True

    def serve(self):
        from .store import CoordinationStore
        from .ui import make_ui_server
        from . import launch_metrics, runtime_update

        edge = ensure_edge(self.directory, self.database, self.payload)
        self.edge_url = edge["url"]
        self.edge_pid = edge["pid"]

        store = CoordinationStore(self.database)
        if not self.trial:
            from .provider_transport import Client
            store.provider_transport = Client.connect(self.directory / "provider-transport", self.instance)

        self.server = make_ui_server(store, host="127.0.0.1", port=0, admission_paused=True)
        self._install_http_read_tracker()
        self.server.transfer_receiver = (self.instance, str(self.payload), self.token)
        sessions = self.server.browser_sessions
        assert getattr(self.server, "runtime_admission_ready", False), "UI workers require admission integration"

        if not self.trial:
            owner = os.environ.pop("RIBBON_RUNTIME_QUEUE_OWNER", None)
            schedule_start = os.environ.pop("RIBBON_RUNTIME_SCHEDULE_START", None)
            if schedule_start is not None:
                sessions.schedules.started_at = float(schedule_start)
            if owner:
                with store._connection() as db:
                    db.execute("UPDATE browser_message_queue SET owner=? WHERE owner=? AND state='queued'",
                               (sessions.queue.owner, owner))
                sessions.queue.wake()

        launch_metrics.start(store, self.stopping, self.payload)
        runtime = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                if self.headers.get("Origin") or not secrets.compare_digest(
                        self.headers.get("Authorization", ""), "Bearer " + runtime.token):
                    self.send_error(403)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 8 * 1024 * 1024:
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

        controller = LocalHTTPServer(("127.0.0.1", 0), Handler)
        controller.daemon_threads = True
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, lambda *_: (self.stopping.set(), self._wake()))
        control_thread = threading.Thread(target=controller.serve_forever, daemon=True)
        ui_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        control_thread.start()
        ui_thread.start()
        self.upstream_csrf_token = self._fetch_local_csrf_token()

        def write_discovery():
            record = {**self.status(), "control_url": f"http://127.0.0.1:{controller.server_port}/",
                      "control_token": self.token}
            atomic_json(self.directory / "discovery.json", record)

        if self.commit_fd is not None:
            # Legacy updaters discover candidate via discovery.json only when commit_ack_fd is absent.
            if self.commit_ack_fd is None:
                write_discovery()
            os.write(self.ready_fd, b"prepared\n")
            os.close(self.ready_fd)
            self.ready_fd = None
            try:
                raw_commit = os.read(self.commit_fd, 4096)
            finally:
                os.close(self.commit_fd)
                self.commit_fd = None
            if not raw_commit:
                sessions.transport_handover = True
                sessions.runtime_handover = True
                sessions.closed = True
                with sessions.events_changed:
                    sessions.events_changed.notify_all()
                controller.shutdown()
                self.server.shutdown()
                control_thread.join()
                ui_thread.join()
                controller.server_close()
                self.server.server_close()
                if self.commit_ack_fd is not None:
                    os.close(self.commit_ack_fd)
                    self.commit_ack_fd = None
                return 0
            try:
                commit_info = json.loads(raw_commit.decode("utf-8")) if raw_commit.lstrip().startswith(b"{") else {}
                queue_owner = commit_info.get("queue_owner") or os.environ.pop("RIBBON_RUNTIME_QUEUE_OWNER", None)
                schedule_start = commit_info.get("schedule_start")
                if schedule_start is None:
                    env_sched = os.environ.pop("RIBBON_RUNTIME_SCHEDULE_START", None)
                    schedule_start = float(env_sched) if env_sched is not None else None
                from .provider_transport import Client
                from .coordinator_projection import CoordinatorProjection
                store.provider_transport = Client.connect(self.directory / "provider-transport", self.instance)
                sessions.projection = store.provider_projection = CoordinatorProjection(sessions, store.provider_transport)
                sessions.projection.load()
                if schedule_start is not None and sessions.projection.cursor == 0:
                    sessions.schedules.started_at = float(schedule_start)
                if queue_owner and sessions.projection.cursor == 0:
                    with store._connection() as db:
                        db.execute("UPDATE browser_message_queue SET owner=? WHERE owner=? AND state='queued'",
                                   (sessions.queue.owner, queue_owner))
                    sessions.queue.wake()
                self.activate_committed()
                write_discovery()
                if self.commit_ack_fd is not None:
                    os.write(self.commit_ack_fd, b"committed\n")
                    os.close(self.commit_ack_fd)
                    self.commit_ack_fd = None
            except BaseException:
                if self.commit_ack_fd is not None:
                    os.close(self.commit_ack_fd)
                    self.commit_ack_fd = None
                sessions.transport_handover = True
                sessions.runtime_handover = True
                sessions.closed = True
                with sessions.events_changed:
                    sessions.events_changed.notify_all()
                controller.shutdown()
                self.server.shutdown()
                control_thread.join()
                ui_thread.join()
                controller.server_close()
                self.server.server_close()
                record_path = self.directory / "discovery.json"
                if record_path.exists() and read_json(record_path).get("instance") == self.instance:
                    record_path.unlink()
                raise
        else:
            write_discovery()
            update = runtime_update.read(self.directory)
            if update and update["phase"] in runtime_update.TERMINAL:
                expected = update["target"] if update["phase"] == "committed" else update["previous"]
                if update.get("superseded_by") or str(self.payload) != expected:
                    runtime_update.write(self.directory, update, superseded_by=str(self.payload))
            else:
                self.abandon_unstarted_update(update)

        with self.mutex:
            self.ensure_supervisor()
            if not self.maintenance:
                self.resume_work()
            elif not self.trial:
                update = runtime_update.read(self.directory)
                if update and update["phase"] in runtime_update.TERMINAL and update.get("requires_review"):
                    self.hold_for_review()
                else:
                    self.publish_edge_route(state="serving")

        if self.ready_fd is not None:
            os.write(self.ready_fd, b"ready\n")
            os.close(self.ready_fd)
            self.ready_fd = None

        handover_outcome = None
        projection_frozen = False
        try:
            while not self.stopping.is_set():
                self.wake_event.wait(0.2)
                self.wake_event.clear()
                if self.stopping.is_set():
                    break
                with self.mutex:
                    if not self.pending or self.maintenance:
                        continue
                    if self.pending_confirmation and not self.update_requested:
                        continue
                    last_update = runtime_update.read(self.directory)
                    if (not self.update_requested and last_update and last_update["target"] == str(self.pending)
                            and last_update["phase"] in {"failed", "rolled-back"}):
                        continue
                    candidate = self.pending
                outcome, frozen = self._execute_blue_green_handover(candidate)
                if frozen:
                    projection_frozen = True
                if outcome in {"committed", "rollback", "stop"}:
                    handover_outcome = outcome
                    break
        finally:
            if handover_outcome == "committed":
                drain_end = time.monotonic() + HTTP_RESPONSE_DRAIN_SECONDS
                while time.monotonic() < drain_end:
                    with self._http_reads_lock:
                        if self._active_http_reads <= 0:
                            break
                    time.sleep(0.002)
            sessions.closed = True
            with sessions.events_changed:
                sessions.events_changed.notify_all()
            controller.shutdown()
            self.server.shutdown()
            control_thread.join()
            ui_thread.join()
            controller.server_close()
            sessions.runtime_handover = not self.explicit_stop
            if hasattr(sessions, "projection") and not self.explicit_stop:
                sessions.transport_handover = True
                if not projection_frozen:
                    try:
                        sessions.projection.freeze()
                    except Exception as exc:
                        self.error = str(exc)
            try:
                self.server.server_close()
            except (RuntimeError, OSError) as error:
                if self.explicit_stop:
                    record_stop_failure(self.directory, self.database, self.instance,
                                        store.provider_transport.record["instance"], error)
                raise
            for attr in ("supervisor_req_fd", "supervisor_ack_fd"):
                fd_val = getattr(self, attr, None)
                if fd_val is not None:
                    os.close(fd_val)
                    setattr(self, attr, None)

        if handover_outcome in {"committed", "rollback"}:
            return 0
        if self.explicit_stop:
            try:
                stop_retained_transport(self.directory, self.database)
            except (RuntimeError, OSError) as error:
                record_stop_failure(self.directory, self.database, self.instance,
                                    store.provider_transport.record["instance"], error)
                raise
            clear_stopped_projection(self.database)
            atomic_json(self.directory / "stop-proof.json", {
                "instance": self.instance, "database": self.database,
                "schema": SCHEMA_VERSION, "protocol": PROTOCOL_VERSION})
        record_path = self.directory / "discovery.json"
        if record_path.exists() and read_json(record_path).get("instance") == self.instance:
            record_path.unlink()
        return 0


def stop_retained_transport(target, database=None):
    """Honor explicit Stop for both loopback edge and provider transport."""
    from .provider_transport import Client, TransportError, retire_dead_owner
    resolved = Path(target).expanduser().resolve()
    if database is None and (resolved.suffix == ".sqlite3" or not resolved.name.endswith(".runtime")):
        database = resolved
        directory = runtime_directory(database)
    else:
        directory = resolved
    stop_edge(directory)
    record_path = directory / "provider-transport/owner.json"
    if not record_path.exists():
        return
    client = Client(read_json(record_path), "stop:" + uuid.uuid4().hex, 0)
    try:
        state = client.call("status")
    except TransportError:
        if database is None:
            raise RuntimeError("Dead transport recovery requires its coordination database; evidence was retained.")
        retire_dead_owner(record_path.parent, client.record,
                          lambda archive, reason: interrupt_stopped_projection(database, archive, reason))
        return
    client.epoch = client.call("claim", expected_epoch=state["epoch"])["epoch"]
    client.call("stop")
    deadline = time.monotonic() + 15
    while record_path.exists() and time.monotonic() < deadline:
        time.sleep(.05)
    if record_path.exists():
        raise RuntimeError("Provider transport has not confirmed Stop.")


def interrupt_stopped_projection(database, archive, reason):
    """Retain lost-owner evidence and pause work before explicit maintenance."""
    import zlib
    from .coordinator_projection import unpack
    from .store import CoordinationStore
    store = CoordinationStore(database)
    names = ("coordinator_projection", "coordinator_commands", "coordinator_events", "coordinator_channels")
    saved = archive / "coordinator.sqlite3"
    if not saved.exists():
        temporary = archive / ("coordinator-" + uuid.uuid4().hex + ".sqlite3")
        try:
            with closing(sqlite3.connect(temporary)) as evidence:
                os.chmod(temporary, 0o600)
                with store.reading(), store._connection() as db, evidence:
                    for name in names:
                        schema = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
                        if schema is None:
                            continue
                        evidence.execute(schema[0])
                        rows = db.execute("SELECT * FROM " + name)
                        evidence.executemany("INSERT INTO " + name + " VALUES (" + ",".join("?" for _ in rows.description) + ")", rows)
            os.replace(temporary, saved)
        finally:
            temporary.unlink(missing_ok=True)
    with closing(sqlite3.connect(saved)) as evidence:
        exists = evidence.execute("SELECT 1 FROM sqlite_master WHERE name='coordinator_projection'").fetchone()
        row = evidence.execute("SELECT state FROM coordinator_projection WHERE singleton=1").fetchone() if exists else None
    state = unpack(json.loads(zlib.decompress(row[0]))) if row else {}
    active = state.get("browser", {}).get("active", {})
    with store.reading(), store._connection() as db:
        active = {identity: turn for identity, turn in active.items() if db.execute(
            "SELECT 1 FROM work_threads t JOIN sessions s ON s.session_id=t.thread_id WHERE t.thread_id=? AND t.turn_id IS ?",
            (identity, turn)).fetchone()}
    for identity, turn in active.items():
        store.threads.finish_turn(identity, turn_id=turn, status="interrupted")
        store.end_session(identity)
    with store.database_operation("coordinator.interrupted"), store._connection() as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        identities = set(active)
        if "browser_message_queue" in tables and state.get("queue_owner"):
            identities.update(row[0] for row in db.execute(
                "SELECT thread_id FROM browser_message_queue WHERE owner=? AND state IN ('queued','sending')",
                (state["queue_owner"],)))
            db.execute("UPDATE browser_message_queue SET state='paused',error=? WHERE owner=? AND state IN ('queued','sending')",
                       (reason, state["queue_owner"]))
        if "browser_inbox_wake" in tables:
            for identity in identities:
                db.execute("""INSERT INTO browser_inbox_wake (thread_id,paused,error,pause_kind,paused_at)
                    VALUES (?,1,?,'stop',?) ON CONFLICT(thread_id) DO UPDATE SET
                    paused=1,error=excluded.error,pause_kind='stop',paused_at=excluded.paused_at,retry_at=NULL""",
                           (identity, reason, store.clock()))
    clear_stopped_projection(database)


def clear_stopped_projection(database):
    """Discard only transport-bound recovery state after confirmed Stop."""
    from .store import CoordinationStore
    store = CoordinationStore(database)
    with store.database_operation("coordinator.stop"), store._connection() as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ("coordinator_projection", "coordinator_commands", "coordinator_events", "coordinator_channels"):
            if table in tables:
                db.execute("DELETE FROM " + table)


def _terminate_child(child: subprocess.Popen) -> None:
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)


def supervise(database, payload, owner_fd, *, start_paused=False, source_exit_fd=None,
              source_instance=None, ready_fd=None, adopted_req_fd=None, adopted_ack_fd=None):
    """Retain ownership across coordinator restarts and orchestrate blue/green standbys."""
    from . import runtime_update
    directory = runtime_directory(database)
    current = Path(payload)
    failures = 0
    child = None
    req_read = adopted_req_fd
    ack_write = adopted_ack_fd

    if source_exit_fd is not None:
        atomic_json(directory / "supervisor.json", {
            "pid": os.getpid(), "source_instance": source_instance, "payload": str(current)})
        if ready_fd is not None:
            os.write(ready_fd, b"ready\n")
            os.close(ready_fd)
            ready_fd = None

    def spawn_coordinator(target: Path, *, paused: bool, standby: bool = False):
        r_read, r_write = os.pipe()
        s_req_read, s_req_write = os.pipe()
        s_ack_read, s_ack_write = os.pipe()
        c_read = c_write = c_ack_read = c_ack_write = None
        command = payload_command(target) + [
            "--runtime-serve", "--db", str(database),
            "--inherit-owner-fd", str(owner_fd), "--ready-fd", str(r_write),
            "--supervisor-req-fd", str(s_req_write), "--supervisor-ack-fd", str(s_ack_read),
        ]
        descriptors = [owner_fd, r_write, s_req_write, s_ack_read]
        if standby:
            c_read, c_write = os.pipe()
            c_ack_read, c_ack_write = os.pipe()
            descriptors.extend([c_read, c_ack_write])
            command += ["--commit-fd", str(c_read), "--commit-ack-fd", str(c_ack_write), "--runtime-start-paused"]
        elif paused:
            command.append("--runtime-start-paused")
        environment = payload_environment(target)
        environment["RIBBON_RUNTIME_SUPERVISED"] = "1"
        try:
            with (directory / "runtime.log").open("ab", buffering=0) as log:
                proc = subprocess.Popen(
                    command, env=environment, pass_fds=tuple(descriptors),
                    cwd=str(Path.home()), stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
                )
        except BaseException:
            for fd_item in (r_read, r_write, s_req_read, s_req_write, s_ack_read, s_ack_write,
                            c_read, c_write, c_ack_read, c_ack_write):
                if fd_item is not None:
                    os.close(fd_item)
            raise
        for fd_item in (r_write, s_req_write, s_ack_read, c_read, c_ack_write):
            if fd_item is not None:
                os.close(fd_item)
        return proc, r_read, s_req_read, s_ack_write, c_write, c_ack_read

    def cleanup_standby(state):
        if state is None:
            return
        proc, _target, c_write, c_ack_read, n_req_read, n_ack_write = state
        for fd_item in (c_write, c_ack_read, n_req_read, n_ack_write):
            if fd_item is not None:
                try:
                    os.close(fd_item)
                except OSError:
                    pass
        _terminate_child(proc)

    def perform_rollback_after_source_exit(source_proc, s_req, s_ack, exit_pipe):
        for fd_item in (s_req, s_ack):
            if fd_item is not None:
                try:
                    os.close(fd_item)
                except OSError:
                    pass
        if source_proc is not None:
            try:
                source_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                _terminate_child(source_proc)
        elif exit_pipe is not None:
            try:
                while os.read(exit_pipe, 64):
                    pass
            finally:
                os.close(exit_pipe)
        update = runtime_update.read(directory)
        if not update:
            return False, current
        if update.get("cancelled"):
            stop_retained_transport(directory, database)
            if update.get("backup"):
                runtime_update.restore(str(database), directory, update)
            else:
                runtime_update.write(directory, update, phase="rolled-back")
            clear_stopped_projection(database)
            record_path = directory / "discovery.json"
            record_path.unlink(missing_ok=True)
            atomic_json(directory / "stop-proof.json", {
                "instance": update["source_instance"], "database": str(Path(database).resolve()),
                "schema": SCHEMA_VERSION, "protocol": PROTOCOL_VERSION})
            return True, current
        update = runtime_update.write(directory, update,
                                      error="Candidate exited before readiness; previous coordinator restored.")
        if update.get("backup"):
            runtime_update.restore(str(database), directory, update)
        else:
            runtime_update.write(directory, update, phase="rolled-back")
        return False, Path(update["previous"])

    try:
        while True:
            if child is None and source_exit_fd is None:
                proc, r_read, req_read, ack_write, _, _ = spawn_coordinator(current, paused=start_paused)
                ready = False
                try:
                    deadline = time.monotonic() + 30
                    while proc.poll() is None and time.monotonic() < deadline:
                        readable, _, _ = select.select([r_read], [], [], 0.1)
                        if readable:
                            ready = os.read(r_read, 32) == b"ready\n"
                            break
                finally:
                    os.close(r_read)
                if not ready:
                    os.close(req_read)
                    os.close(ack_write)
                    req_read = ack_write = None
                    _terminate_child(proc)
                    failures += 1
                    if failures >= 3:
                        raise RuntimeError("Coordinator repeatedly failed to restore; provider transports remain retained.")
                    time.sleep(0.2)
                    continue
                failures = 0
                child = proc
                atomic_json(directory / "supervisor.json", {
                    "pid": os.getpid(), "coordinator_pid": child.pid, "payload": str(current)})

            standby_state = None
            buffer = bytearray()
            rollback_triggered = False
            while True:
                readable, _, _ = select.select([req_read], [], [], 0.2)
                if not readable:
                    if child is not None and child.poll() is not None:
                        break
                    continue
                chunk = os.read(req_read, 4096)
                if not chunk:
                    break
                buffer.extend(chunk)
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    request = json.loads(line.decode("utf-8"))
                    op = request.get("op")
                    if op == "prepare_candidate":
                        cleanup_standby(standby_state)
                        standby_state = None
                        target_path = Path(request["target"])
                        update = runtime_update.read(directory)
                        if update:
                            update = runtime_update.write(directory, update, phase="trial")
                        try:
                            c_proc, c_ready_read, n_req_read, n_ack_write, c_write, c_ack_read = spawn_coordinator(
                                target_path, paused=True, standby=True)
                        except Exception as exc:
                            os.write(ack_write, (json.dumps({
                                "ok": False, "rollback_required": True, "error": str(exc)}) + "\n").encode("utf-8"))
                            rollback_triggered = True
                            break
                        prepared_ok = False
                        cancelled_during_trial = False
                        try:
                            deadline = time.monotonic() + 30
                            while c_proc.poll() is None and time.monotonic() < deadline:
                                latest = runtime_update.read(directory)
                                if latest and latest.get("cancelled"):
                                    cancelled_during_trial = True
                                ready_fds, _, _ = select.select([c_ready_read], [], [], 0.05)
                                if ready_fds:
                                    prepared_ok = os.read(c_ready_read, 32) == b"prepared\n"
                                    break
                        finally:
                            os.close(c_ready_read)
                        latest = runtime_update.read(directory)
                        if latest and latest.get("cancelled"):
                            cancelled_during_trial = True
                        if prepared_ok and not cancelled_during_trial:
                            standby_state = (c_proc, target_path, c_write, c_ack_read, n_req_read, n_ack_write)
                            os.write(ack_write, (json.dumps({"ok": True, "pid": c_proc.pid}) + "\n").encode("utf-8"))
                        else:
                            cleanup_standby((c_proc, target_path, c_write, c_ack_read, n_req_read, n_ack_write))
                            os.write(ack_write, (json.dumps({
                                "ok": False, "rollback_required": True, "cancelled": cancelled_during_trial,
                                "error": "Candidate exited before readiness; previous coordinator restored.",
                            }) + "\n").encode("utf-8"))
                            rollback_triggered = True
                            break
                    elif op == "abort_candidate":
                        cleanup_standby(standby_state)
                        standby_state = None
                        os.write(ack_write, b'{"ok":true}\n')
                    elif op == "commit_candidate":
                        if standby_state is None:
                            os.write(ack_write, b'{"ok":false,"error":"No prepared candidate."}\n')
                            rollback_triggered = True
                            break
                        c_proc, target_path, c_write, c_ack_read, n_req_read, n_ack_write = standby_state
                        standby_state = None
                        guard = lock_file(directory / "update-control.lock", blocking=True)
                        try:
                            latest = runtime_update.read(directory)
                            cancelled = bool(latest and latest.get("cancelled"))
                            if not cancelled:
                                runtime_update.write(
                                    directory, latest, phase="committed", uninterrupted=True, blocked=[],
                                    admission_drain_seconds=request.get("admission_drain_seconds", 0.0),
                                    queue_owner=request.get("queue_owner"),
                                    schedule_start=request.get("schedule_start"),
                                )
                        finally:
                            os.close(guard)
                        if cancelled:
                            cleanup_standby((c_proc, target_path, c_write, c_ack_read, n_req_read, n_ack_write))
                            os.write(ack_write, b'{"ok":false,"cancelled":true}\n')
                            rollback_triggered = True
                            break
                        commit_payload = json.dumps({
                            "commit": True,
                            "queue_owner": request.get("queue_owner"),
                            "schedule_start": request.get("schedule_start"),
                        }).encode("utf-8") + b"\n"
                        committed_ok = False
                        try:
                            os.write(c_write, commit_payload)
                            os.close(c_write)
                            c_write = None
                            deadline = time.monotonic() + 20
                            while c_proc.poll() is None and time.monotonic() < deadline:
                                ready_fds, _, _ = select.select([c_ack_read], [], [], 0.05)
                                if ready_fds:
                                    committed_ok = os.read(c_ack_read, 32) == b"committed\n"
                                    break
                        except OSError:
                            committed_ok = False
                        finally:
                            if c_write is not None:
                                os.close(c_write)
                            os.close(c_ack_read)
                        if not committed_ok:
                            cleanup_standby((c_proc, target_path, None, None, n_req_read, n_ack_write))
                            os.write(ack_write, b'{"ok":false,"error":"Candidate failed commit."}\n')
                            rollback_triggered = True
                            break
                        os.write(ack_write, b'{"ok":true}\n')
                        old_child, old_exit = child, source_exit_fd
                        os.close(req_read)
                        os.close(ack_write)
                        if old_child is not None:
                            threading.Thread(target=old_child.wait, daemon=True).start()
                        if old_exit is not None:
                            os.close(old_exit)
                            source_exit_fd = None
                        child = c_proc
                        current = target_path
                        req_read, ack_write = n_req_read, n_ack_write
                        buffer.clear()
                        atomic_json(directory / "supervisor.json", {
                            "pid": os.getpid(), "coordinator_pid": child.pid, "payload": str(current)})
                if rollback_triggered:
                    break

            cleanup_standby(standby_state)
            standby_state = None

            if rollback_triggered:
                should_exit, next_payload = perform_rollback_after_source_exit(child, req_read, ack_write, source_exit_fd)
                child = None
                source_exit_fd = None
                req_read = ack_write = None
                if should_exit:
                    return 0
                current = next_payload
                update = runtime_update.read(directory)
                start_paused = bool(update and update.get("requires_review"))
                continue

            for fd_item in (req_read, ack_write):
                if fd_item is not None:
                    os.close(fd_item)
            req_read = ack_write = None
            if child is not None:
                result = child.wait()
                child = None
            else:
                result = 0
                if source_exit_fd is not None:
                    try:
                        while os.read(source_exit_fd, 64):
                            pass
                    finally:
                        os.close(source_exit_fd)
                        source_exit_fd = None
            proof = directory / "stop-proof.json"
            if result == 0 and proof.exists():
                return 0
            update = runtime_update.read(directory)
            start_paused = bool(update and update.get("requires_review"))
    finally:
        for fd_item in (ready_fd, source_exit_fd, req_read, ack_write):
            if fd_item is not None:
                try:
                    os.close(fd_item)
                except OSError:
                    pass


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db")
    parser.add_argument("--resources")
    parser.add_argument("--runtime-bootstrap", action="store_true")
    parser.add_argument("--runtime-serve", action="store_true")
    parser.add_argument("--runtime-supervise", action="store_true")
    parser.add_argument("--runtime-probe", action="store_true")
    parser.add_argument("--runtime-start-paused", action="store_true")
    parser.add_argument("--runtime-confirm-updates", action="store_true")
    parser.add_argument("--runtime-command", choices=(
        "status", "update-status", "pause", "resume", "stop", "decision",
        "activate-update", "cancel-update-authorization"))
    parser.add_argument("--inherit-owner-fd", type=int)
    parser.add_argument("--ready-fd", type=int)
    parser.add_argument("--commit-fd", type=int)
    parser.add_argument("--commit-ack-fd", type=int)
    parser.add_argument("--supervisor-req-fd", type=int)
    parser.add_argument("--supervisor-ack-fd", type=int)
    parser.add_argument("--adopted-req-fd", type=int)
    parser.add_argument("--adopted-ack-fd", type=int)
    parser.add_argument("--exit-fd", type=int)
    parser.add_argument("--source-instance")
    args = parser.parse_args(argv)
    if args.runtime_probe:
        from . import ui
        for path in Path(__file__).parent.rglob("*.py"):
            compile(path.read_bytes(), str(path), "exec")
        return 0
    if not args.db:
        parser.error("--db is required")
    if args.runtime_bootstrap:
        print(json.dumps(bootstrap(
            args.db, args.resources, start_paused=args.runtime_start_paused,
            require_confirmation=args.runtime_confirm_updates)), flush=True)
        return 0
    if args.runtime_command:
        from . import runtime_update
        directory = runtime_directory(args.db)
        values = json.load(sys.stdin) if args.runtime_command in {"decision", "activate-update"} else {}
        try:
            record = discover(args.db, timeout=2)
        except (RuntimeError, OSError):
            update = runtime_update.read(directory)
            if not update or update["phase"] in runtime_update.TERMINAL:
                raise
            if args.runtime_command in {"status", "update-status"}:
                print(json.dumps({"status": "updating", "update": update}), flush=True)
                return 0
            if args.runtime_command == "stop":
                guard = lock_file(directory / "update-control.lock", blocking=True)
                try:
                    runtime_update.write(directory, update, cancelled=True)
                finally:
                    os.close(guard)
                deadline, stopped_instances = time.monotonic() + 30, set()
                while time.monotonic() < deadline:
                    if confirm_stopped(directory, args.db, update["source_instance"]):
                        print(json.dumps({"status": "stopped", "instance": update["source_instance"]}), flush=True)
                        return 0
                    try:
                        current = discover(args.db, timeout=.5)
                        if current and current["instance"] not in stopped_instances:
                            control(current, "stop", timeout=2)
                            stopped_instances.add(current["instance"])
                    except (RuntimeError, OSError):
                        pass
                    time.sleep(.05)
                raise RuntimeError("Update cancellation is still stopping the runtime.")
            raise
        if not record:
            update = runtime_update.read(directory)
            if args.runtime_command == "update-status":
                print(json.dumps({"status": "unavailable", "update": update}), flush=True)
                return 0
            if (args.runtime_command == "status" and update and not update.get("cancelled")
                    and not (directory / "stop-proof.json").exists() and owner_available(directory)):
                selected = args.resources or update["target"]
                print(json.dumps(bootstrap(
                    args.db, selected, start_paused=bool(update.get("requires_review")))), flush=True)
                return 0
            proof_path = directory / "stop-proof.json"
            proof = read_json(proof_path) if proof_path.exists() else {}
            if args.runtime_command == "stop" and owner_available(directory):
                maintenance_owner = lock_file(directory / "owner.lock")
                try:
                    stop_edge(directory)
                    if (directory / "provider-transport/owner.json").exists():
                        stop_retained_transport(directory, args.db)
                        clear_stopped_projection(args.db)
                finally:
                    os.close(maintenance_owner)
                if confirm_stopped(directory, args.db, proof.get("instance")):
                    print(json.dumps({"status": "stopped", "instance": proof.get("instance")}))
                    return 0
                raise RuntimeError("Provider transport has not confirmed exit; work may still be stopping.")
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
                if confirm_stopped(directory, args.db, record["instance"]):
                    result = {"status": "stopped", "instance": record["instance"]}
                    break
                failure = retained_stop_failure(directory, args.db, record["instance"])
                if failure:
                    raise RuntimeError("Stop is unconfirmed: " + failure)
                time.sleep(.05)
            else:
                raise RuntimeError("Runtime has not confirmed exit; work may still be stopping.")
        print(json.dumps(result), flush=True)
        return 0
    if args.runtime_serve or args.runtime_supervise:
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
            if args.runtime_supervise:
                return supervise(
                    args.db, Path(sys.argv[0]).resolve().parent, fd,
                    start_paused=args.runtime_start_paused, source_exit_fd=args.exit_fd,
                    source_instance=args.source_instance, ready_fd=args.ready_fd,
                    adopted_req_fd=args.adopted_req_fd, adopted_ack_fd=args.adopted_ack_fd)
            return Runtime(
                args.db, Path(sys.argv[0]).resolve().parent, fd, args.ready_fd,
                start_paused=args.runtime_start_paused, commit_fd=args.commit_fd,
                commit_ack_fd=args.commit_ack_fd, supervisor_req_fd=args.supervisor_req_fd,
                supervisor_ack_fd=args.supervisor_ack_fd).serve()
        finally:
            os.close(fd)
    parser.error("Choose a runtime operation")
