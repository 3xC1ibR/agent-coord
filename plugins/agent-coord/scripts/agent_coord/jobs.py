"""Runtime-independent managed command jobs.

A job is one argv command owned by a registered session. ``launch`` starts a
detached supervisor process that outlives the launching CLI, the agent turn,
and the app; the supervisor owns the live child handle, honours durable cancel
requests and timeouts, keeps a bounded output log, and records one terminal
state. Nothing here wakes a model: the subscription runtime observes terminal
rows in the ``jobs`` table.

Supervisor entry point::

    python -m agent_coord.jobs supervise --db <path> --job-id <id> --token <t>
"""
from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import re
import select
import signal
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .store import CoordinationError, CoordinationStore

JOB_STATES = ("queued", "starting", "running", "completed", "failed", "cancelled", "timed_out", "unknown")
TERMINAL_STATES = frozenset({"completed", "failed", "cancelled", "timed_out", "unknown"})
ACTIVE_STATES = frozenset({"starting", "running"})

DEFAULT_LOG_LIMIT_BYTES = 1024 * 1024
DEFAULT_LOG_RETAIN_BYTES = 768 * 1024
MAX_LOG_READ_BYTES = 1024 * 1024
DEFAULT_LOGS_LIMIT = 8000
TRUNCATION_MARKER = b"[older job output truncated]\n"

DEFAULT_POLL_SECONDS = 0.25
DEFAULT_HEARTBEAT_SECONDS = 2.0
DEFAULT_TERMINAL_GRACE_SECONDS = 3.0
STARTING_STALE_SECONDS = 60.0
HEARTBEAT_STALE_SECONDS = 300.0
SPAWN_TIMEOUT_SECONDS = 60.0

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_IDENTITY_KEYS = ("AGENT_COORD_SESSION_ID", "CODEX_THREAD_ID", "CLAUDE_ENV_FILE")


def _iso(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def _pid_is_running(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _kill_group(leader_pid: int, sig: signal.Signals) -> None:
    try:
        os.killpg(leader_pid, sig)
    except ProcessLookupError:
        pass
    except PermissionError:
        pass


class ChildExit:
    """Observe exit without reaping, retaining the process-group identity.

    Linux exposes waitid/WNOWAIT; macOS exposes kqueue process events. Keeping
    the leader unreaped lets cancellation safely kill its remaining children.
    """

    def __init__(self, child):
        self.child = child
        self.queue = None
        self.exited = False
        if not (hasattr(os, "waitid") and hasattr(os, "WNOWAIT")):
            if not hasattr(select, "kqueue"):
                raise CoordinationError("Managed jobs require waitid or kqueue process events.")
            self.queue = select.kqueue()
            event = select.kevent(child.pid, filter=select.KQ_FILTER_PROC,
                                  flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE,
                                  fflags=select.KQ_NOTE_EXIT)
            try:
                self.queue.control([event], 0, 0)
            except ProcessLookupError:
                self.exited = True

    def ready(self):
        if self.exited or self.child.returncode is not None:
            return True
        if self.queue is not None:
            self.exited = bool(self.queue.control([], 1, 0))
        else:
            try:
                self.exited = os.waitid(os.P_PID, self.child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
            except ChildProcessError:
                self.exited = True
        return self.exited

    def close(self):
        if self.queue is not None:
            self.queue.close()


def _trim_incomplete_utf8(data: bytes) -> bytes:
    """Drop a trailing partial multi-byte sequence so paged text stays clean."""
    for back in range(1, min(4, len(data)) + 1):
        byte = data[-back]
        if byte & 0xC0 != 0x80:
            if byte & 0x80 == 0:
                return data
            needed = 2 if byte & 0xE0 == 0xC0 else 3 if byte & 0xF0 == 0xE0 else 4
            return data[:-back] if back < needed else data
    return data


def jobs_root(store: CoordinationStore) -> Path:
    return store.database_path.resolve().parent / "jobs"


def job_directory(store: CoordinationStore, job_id: str) -> Path:
    if not _SAFE_ID.fullmatch(job_id):
        raise CoordinationError("Job ID is not safe for a runtime path.")
    root = jobs_root(store)
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink():
        raise CoordinationError("Refusing to use a jobs symlink.")
    directory = root / job_id
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.is_symlink():
        raise CoordinationError("Refusing to use a job runtime symlink.")
    return directory


def output_log_path(store: CoordinationStore, job_id: str) -> Path:
    return job_directory(store, job_id) / "output.log"


def supervisor_log_path(store: CoordinationStore, job_id: str) -> Path:
    return job_directory(store, job_id) / "supervisor.log"


def scripts_directory() -> Path:
    return Path(__file__).resolve().parents[1]


def supervisor_environment(environment: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if environment is None else environment)
    scripts = str(scripts_directory())
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = scripts if not existing else scripts + os.pathsep + existing
    return env


def command_environment(environment: dict[str, str] | None = None) -> dict[str, str]:
    """The job command must never inherit the owner's agent identity."""
    env = dict(os.environ if environment is None else environment)
    for key in _IDENTITY_KEYS:
        env.pop(key, None)
    return env


def build_supervisor_command(store: CoordinationStore, job_id: str, token: str, *, stage: str = "supervise") -> list[str]:
    return [sys.executable, "-m", "agent_coord.jobs", stage, "--db", str(store.database_path.resolve()),
            "--job-id", job_id, "--token", token]


class BoundedLog:
    """Append-only output file with a disk cap and stable logical offsets."""

    def __init__(self, path: Path, *, limit_bytes: int = DEFAULT_LOG_LIMIT_BYTES,
                 retain_bytes: int = DEFAULT_LOG_RETAIN_BYTES) -> None:
        if limit_bytes <= 0 or not 0 < retain_bytes <= limit_bytes:
            raise CoordinationError("Job log bounds are invalid.")
        self.path = path
        self.limit_bytes = limit_bytes
        self.retain_bytes = retain_bytes
        self.discarded = 0  # logical bytes no longer present in the file

    def append(self, data: bytes) -> bool:
        """Append and return whether older output was discarded."""
        if not data:
            return False
        if self.path.is_symlink():
            raise CoordinationError("Refusing to write job output through a symlink.")
        with self.path.open("ab") as log_file:
            log_file.write(data)
        size = self.path.stat().st_size
        if size <= self.limit_bytes:
            return False
        with self.path.open("rb") as log_file:
            log_file.seek(size - self.retain_bytes)
            tail = log_file.read()
        with self.path.open("wb") as log_file:
            log_file.write(TRUNCATION_MARKER)
            log_file.write(tail)
        # Logical size stays equal to every byte ever written.
        self.discarded += (size - self.retain_bytes) - len(TRUNCATION_MARKER)
        return True


class ManagedJobs:
    """Owner-scoped durable command jobs backed by the coordination store."""

    starting_stale_seconds = STARTING_STALE_SECONDS
    heartbeat_stale_seconds = HEARTBEAT_STALE_SECONDS

    def __init__(self, store: CoordinationStore, *, run=subprocess.run) -> None:
        self.store = store
        self._run = run
        with store._connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    owner_session_id TEXT NOT NULL REFERENCES sessions(session_id),
                    cwd TEXT NOT NULL,
                    command_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    exit_code INTEGER,
                    output_path TEXT,
                    error TEXT,
                    timeout REAL,
                    created_at REAL NOT NULL,
                    launched_at REAL,
                    started_at REAL,
                    finished_at REAL,
                    updated_at REAL NOT NULL,
                    supervisor_token TEXT,
                    supervisor_pid INTEGER,
                    child_pid INTEGER,
                    heartbeat_at REAL,
                    cancel_requested_at REAL,
                    output_discarded INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS jobs_owner_idx ON jobs(owner_session_id, created_at);
            """)

    # -- records -----------------------------------------------------------

    @staticmethod
    def _record(row) -> dict[str, Any]:
        row = dict(row)
        return {
            "id": row["id"],
            "owner_session_id": row["owner_session_id"],
            "cwd": row["cwd"],
            "command": json.loads(row["command_json"]),
            "state": row["state"],
            "terminal": row["state"] in TERMINAL_STATES,
            "exit_code": row["exit_code"],
            "output_path": row["output_path"],
            "error": row["error"],
            "timeout": row["timeout"],
            "created_at": _iso(row["created_at"]),
            "launched_at": _iso(row["launched_at"]),
            "started_at": _iso(row["started_at"]),
            "finished_at": _iso(row["finished_at"]),
            "heartbeat_at": _iso(row["heartbeat_at"]),
            "cancel_requested_at": _iso(row["cancel_requested_at"]),
            "supervisor_pid": row["supervisor_pid"],
            "child_pid": row["child_pid"],
        }

    def _row(self, db, job_id: str):
        return db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()

    def _owned_row(self, db, session_id: str, job_id: str):
        if not isinstance(job_id, str) or not job_id.strip():
            raise CoordinationError("Job ID must not be empty.")
        row = self._row(db, job_id)
        if row is None or row["owner_session_id"] != session_id:
            raise CoordinationError(f"Job {job_id} is not owned by session {session_id}.")
        return row

    def _require_session(self, session_id: str) -> dict[str, Any]:
        if not isinstance(session_id, str) or not session_id.strip():
            raise CoordinationError("Session ID must not be empty.")
        return self.store.get_session(session_id)

    # -- public API --------------------------------------------------------

    def create(self, session_id: str, command, *, cwd: str | None = None,
               timeout: float | None = None, job_id: str | None = None) -> dict[str, Any]:
        session = self._require_session(session_id)
        if isinstance(command, (str, bytes)) or not isinstance(command, (list, tuple)):
            raise CoordinationError("Job command must be an argv list.")
        argv = list(command)
        if not argv or not all(isinstance(part, str) and "\0" not in part for part in argv) or not argv[0]:
            raise CoordinationError("Job command must be an argv list with a nonempty executable and no NUL characters.")
        if timeout is not None:
            try:
                valid = type(timeout) in (int, float) and math.isfinite(timeout) and timeout > 0
            except OverflowError:
                valid = False
            if not valid:
                raise CoordinationError("Job timeout must be a finite positive number of seconds.")
            timeout = float(timeout)
        base = Path(session["cwd"])
        directory = base if cwd is None else (base / Path(cwd).expanduser())
        directory = directory.resolve()
        if not directory.is_dir():
            raise CoordinationError(f"Job working directory does not exist: {directory}")
        identifier = job_id if job_id is not None else str(uuid.uuid4())
        if not isinstance(identifier, str) or not _SAFE_ID.fullmatch(identifier):
            raise CoordinationError("Job ID must be 1-128 characters of letters, digits, '.', '_' or '-'.")
        command_json = json.dumps(argv)
        output_path = str(output_log_path(self.store, identifier))
        now = self.store.clock()
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = self._row(db, identifier)
            if existing is not None:
                same = (existing["owner_session_id"] == session_id and existing["command_json"] == command_json
                        and existing["cwd"] == str(directory) and existing["timeout"] == timeout)
                if not same:
                    raise CoordinationError(f"Job {identifier} already exists with a different owner or command.")
                return self._record(existing)
            db.execute("""INSERT INTO jobs (id, owner_session_id, cwd, command_json, state, output_path, timeout,
                          created_at, updated_at) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?)""",
                       (identifier, session_id, str(directory), command_json, output_path, timeout, now, now))
            return self._record(self._row(db, identifier))

    def launch(self, session_id: str, job_id: str) -> dict[str, Any]:
        self._require_session(session_id)
        token = uuid.uuid4().hex
        now = self.store.clock()
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._owned_row(db, session_id, job_id)
            claimed = db.execute("""UPDATE jobs SET state = 'starting', supervisor_token = ?, launched_at = ?, updated_at = ?
                                    WHERE id = ? AND state = 'queued'""", (token, now, now, job_id)).rowcount
            if not claimed:
                row = self._row(db, job_id)
        if not claimed:
            record = self.get(session_id, job_id)
            return {"job": record, "launched": False, "job_id": job_id, "state": record["state"]}
        command = build_supervisor_command(self.store, job_id, token, stage="spawn")
        try:
            completed = self._run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                  timeout=SPAWN_TIMEOUT_SECONDS, env=supervisor_environment(), close_fds=True)
        except (OSError, subprocess.SubprocessError) as exc:
            self._fail_launch(job_id, token, f"Cannot start job supervisor: {exc}")
            raise CoordinationError(f"Cannot start job supervisor: {exc}") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()[-2000:]
            error = f"Job supervisor failed to start (exit {completed.returncode}): {detail}"
            self._fail_launch(job_id, token, error)
            raise CoordinationError(error)
        try:
            supervisor_pid = int((completed.stdout or "").strip().splitlines()[-1])
        except (ValueError, IndexError):
            supervisor_pid = None
        if supervisor_pid is not None:
            with self.store._connection() as db:
                db.execute("""UPDATE jobs SET supervisor_pid = COALESCE(supervisor_pid, ?), updated_at = ?
                              WHERE id = ? AND supervisor_token = ?""",
                           (supervisor_pid, self.store.clock(), job_id, token))
        record = self.get(session_id, job_id)
        return {"job": record, "launched": True, "job_id": job_id, "state": record["state"],
                "supervisor_pid": supervisor_pid, "supervisor_log_path": str(supervisor_log_path(self.store, job_id))}

    def _fail_launch(self, job_id: str, token: str, error: str) -> None:
        now = self.store.clock()
        with self.store._connection() as db:
            db.execute("""UPDATE jobs SET state = 'failed', error = ?, finished_at = ?, updated_at = ?
                          WHERE id = ? AND supervisor_token = ? AND state = 'starting'""",
                       (error, now, now, job_id, token))

    def get(self, session_id: str, job_id: str) -> dict[str, Any]:
        self._require_session(session_id)
        with self.store._connection() as db:
            row = self._owned_row(db, session_id, job_id)
        return self._record(self._reconcile(row))

    def list(self, session_id: str) -> list[dict[str, Any]]:
        self._require_session(session_id)
        with self.store._connection() as db:
            rows = db.execute("SELECT * FROM jobs WHERE owner_session_id = ? ORDER BY created_at, id",
                              (session_id,)).fetchall()
        return [self._record(self._reconcile(row)) for row in rows]

    def cancel(self, session_id: str, job_id: str) -> dict[str, Any]:
        self._require_session(session_id)
        for _ in range(4):
            with self.store._connection() as db:
                row = self._owned_row(db, session_id, job_id)
            row = self._reconcile(row)
            state = row["state"]
            if state in TERMINAL_STATES:
                return self._record(row)
            now = self.store.clock()
            with self.store._connection() as db:
                if state in {"queued", "starting"}:
                    changed = db.execute("""UPDATE jobs SET state = 'cancelled', cancel_requested_at = ?, finished_at = ?,
                                            updated_at = ?, error = COALESCE(error, 'Cancelled before the command started.')
                                            WHERE id = ? AND state = ?""", (now, now, now, job_id, state)).rowcount
                else:
                    changed = db.execute("""UPDATE jobs SET cancel_requested_at = COALESCE(cancel_requested_at, ?),
                                            updated_at = ? WHERE id = ? AND state = 'running'""",
                                         (now, now, job_id)).rowcount
                if changed:
                    return self._record(self._row(db, job_id))
        raise CoordinationError(f"Job {job_id} changed state while cancelling; retry.")

    def logs(self, session_id: str, job_id: str, *, offset: int = 0, limit: int = DEFAULT_LOGS_LIMIT) -> dict[str, Any]:
        self._require_session(session_id)
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise CoordinationError("Log offset must be a non-negative integer.")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise CoordinationError("Log limit must be a positive integer.")
        limit = min(limit, MAX_LOG_READ_BYTES)
        with self.store._connection() as db:
            row = self._owned_row(db, session_id, job_id)
        row = self._reconcile(row)
        discarded = int(row["output_discarded"] or 0)
        path = Path(row["output_path"]) if row["output_path"] else None
        data = b""
        file_size = 0
        if path is not None and path.is_file() and not path.is_symlink():
            file_size = path.stat().st_size
            start = max(offset, discarded) - discarded
            if start < file_size:
                with path.open("rb") as log_file:
                    log_file.seek(start)
                    raw = log_file.read(limit + 3)
                    data = _trim_incomplete_utf8(raw[:limit])
                    # A byte limit smaller than one UTF-8 code point may need
                    # up to three extra bytes to make progress without loss.
                    if not data:
                        for width in range(limit + 1, len(raw) + 1):
                            data = _trim_incomplete_utf8(raw[:width])
                            if data:
                                break
                    if not data and row["state"] in TERMINAL_STATES:
                        data = raw[:limit]  # Malformed terminal output remains readable.
        size = discarded + file_size
        start_offset = max(offset, discarded)
        next_offset = start_offset + len(data)
        terminal = row["state"] in TERMINAL_STATES
        return {
            "job_id": job_id,
            "state": row["state"],
            "terminal": terminal,
            "offset": start_offset,
            "next_offset": next_offset,
            "size": size,
            "gap": offset < discarded,
            "eof": terminal and next_offset >= size,
            "text": data.decode("utf-8", errors="replace"),
        }

    # -- stale supervisor recovery ------------------------------------------

    def _reconcile(self, row):
        """Turn an abandoned starting/running row terminal; never rerun a command."""
        state = row["state"]
        if state not in ACTIVE_STATES:
            return row
        now = self.store.clock()
        pid = row["supervisor_pid"]
        new_state = None
        error = None
        if state == "starting":
            if pid and not _pid_is_running(pid):
                # The supervisor claims the row before it starts the command.
                new_state, error = "failed", f"Job supervisor {pid} exited before starting the command."
            elif row["launched_at"] is not None and now - row["launched_at"] > self.starting_stale_seconds:
                new_state, error = "unknown", "Job supervisor did not report starting the command in time."
        else:
            if not _pid_is_running(pid):
                new_state, error = "unknown", f"Job supervisor {pid} exited without recording a result."
            elif row["heartbeat_at"] is not None and now - row["heartbeat_at"] > self.heartbeat_stale_seconds:
                new_state, error = "unknown", "Job supervisor heartbeat is stale; the command result is unknown."
        if new_state is None:
            return row
        with self.store._connection() as db:
            db.execute("""UPDATE jobs SET state = ?, error = ?, finished_at = ?, updated_at = ?
                          WHERE id = ? AND state = ? AND supervisor_token IS ?
                          AND heartbeat_at IS ? AND supervisor_pid IS ? AND launched_at IS ?""",
                       (new_state, error, now, now, row["id"], state, row["supervisor_token"],
                        row["heartbeat_at"], row["supervisor_pid"], row["launched_at"]))
            return self._row(db, row["id"])


# -- supervisor --------------------------------------------------------------


@contextmanager
def _supervisor_lock(store: CoordinationStore, job_id: str):
    path = job_directory(store, job_id) / "supervisor.lock"
    with path.open("a+", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CoordinationError(f"A supervisor already owns job {job_id}.") from exc
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def spawn_supervisor(store: CoordinationStore, job_id: str, token: str) -> int:
    """Start the detached supervisor and return its PID; the caller exits at once."""
    command = build_supervisor_command(store, job_id, token, stage="supervise")
    log_path = supervisor_log_path(store, job_id)
    with log_path.open("ab") as log_file:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log_file, stderr=subprocess.STDOUT,
                                   start_new_session=True, close_fds=True, env=supervisor_environment())
    pid = process.pid
    # The intermediate exits without waiting; the supervisor is reparented to init.
    process.returncode = 0
    return pid


def supervise(store: CoordinationStore, job_id: str, token: str, *,
              poll_seconds: float = DEFAULT_POLL_SECONDS,
              heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
              grace_seconds: float = DEFAULT_TERMINAL_GRACE_SECONDS,
              log_limit_bytes: int = DEFAULT_LOG_LIMIT_BYTES,
              log_retain_bytes: int = DEFAULT_LOG_RETAIN_BYTES,
              popen=subprocess.Popen, monotonic=time.monotonic) -> dict[str, Any]:
    """Run one job to a terminal state. Safe to invoke twice: only the token holder runs."""
    if poll_seconds <= 0:
        raise CoordinationError("Job poll interval must be positive.")
    jobs = ManagedJobs(store)
    with _supervisor_lock(store, job_id):
        now = store.clock()
        with store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            claimed = db.execute("""UPDATE jobs SET state = 'running', started_at = ?, heartbeat_at = ?, updated_at = ?,
                                    supervisor_pid = ? WHERE id = ? AND supervisor_token = ? AND state = 'starting'
                                    AND cancel_requested_at IS NULL""",
                                 (now, now, now, os.getpid(), job_id, token)).rowcount
            row = jobs._row(db, job_id)
        if row is None:
            raise CoordinationError(f"Job {job_id} does not exist.")
        if not claimed:
            return ManagedJobs._record(row)  # Not ours, or already handled: never rerun.
        return _run_claimed(store, jobs, dict(row), token, poll_seconds=poll_seconds,
                            heartbeat_seconds=heartbeat_seconds, grace_seconds=grace_seconds,
                            log_limit_bytes=log_limit_bytes, log_retain_bytes=log_retain_bytes,
                            popen=popen, monotonic=monotonic)


def _finish(store: CoordinationStore, job_id: str, token: str, *, state: str, exit_code: int | None,
            error: str | None, discarded: int) -> dict[str, Any]:
    now = store.clock()
    with store._connection() as db:
        db.execute("""UPDATE jobs SET state = ?, exit_code = ?, error = ?, finished_at = ?, updated_at = ?,
                      heartbeat_at = ?, output_discarded = ?
                      WHERE id = ? AND supervisor_token = ? AND state = 'running'""",
                   (state, exit_code, error, now, now, now, discarded, job_id, token))
        return ManagedJobs._record(db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())


def _run_claimed(store, jobs, row, token, *, poll_seconds, heartbeat_seconds, grace_seconds,
                 log_limit_bytes, log_retain_bytes, popen, monotonic) -> dict[str, Any]:
    job_id = row["id"]
    argv = json.loads(row["command_json"])
    log = BoundedLog(Path(row["output_path"]), limit_bytes=log_limit_bytes, retain_bytes=log_retain_bytes)
    try:
        child = popen(argv, cwd=row["cwd"], env=command_environment(), stdin=subprocess.DEVNULL,
                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
    except OSError as exc:
        return _finish(store, job_id, token, state="failed", exit_code=None,
                       error=f"Cannot start command: {exc}", discarded=0)
    monitor = ChildExit(child)
    with store._connection() as db:
        db.execute("UPDATE jobs SET child_pid = ?, updated_at = ? WHERE id = ? AND supervisor_token = ?",
                   (child.pid, store.clock(), job_id, token))
    pipe = child.stdout
    fd = pipe.fileno()
    os.set_blocking(fd, False)
    timeout = row["timeout"]
    started = monotonic()
    deadline = started + float(timeout) if timeout else None
    outcome: str | None = None
    term_sent_at: float | None = None
    killed = False
    last_heartbeat = started
    last_cancel_check = started - poll_seconds
    cancel_requested = False
    eof = False
    exited_after = None
    try:
        while True:
            readable, _, _ = select.select([fd], [], [], poll_seconds) if not eof else ([], [], [])
            if readable:
                try:
                    data = os.read(fd, 64 * 1024)
                except BlockingIOError:
                    data = None
                if data:
                    if log.append(data):
                        with store._connection() as db:
                            db.execute("UPDATE jobs SET output_discarded = ? WHERE id = ? AND supervisor_token = ?",
                                       (log.discarded, job_id, token))
                if data == b"":
                    eof = True
            now = monotonic()
            exited = monitor.ready()
            if exited and eof:
                # With no remaining pipe writers, cleanup can finish now. The
                # unreaped leader still reserves the group for descendant kill.
                break
            if exited:
                # The command is done but a descendant still holds the pipe. Drain
                # for a grace period, then abandon the pipe rather than hang.
                if exited_after is None:
                    exited_after = now
                elif now - exited_after >= grace_seconds and outcome is None:
                    break
                if not readable:
                    time.sleep(min(poll_seconds, 0.05))
            elif eof:
                time.sleep(poll_seconds)
            if outcome is None:
                if now - last_cancel_check >= poll_seconds:
                    last_cancel_check = now
                    with store._connection() as db:
                        flag = db.execute("SELECT cancel_requested_at FROM jobs WHERE id = ?", (job_id,)).fetchone()
                    cancel_requested = bool(flag and flag["cancel_requested_at"] is not None)
                if cancel_requested:
                    outcome = "cancelled"
                elif deadline is not None and now >= deadline:
                    outcome = "timed_out"
                if outcome is not None:
                    _kill_group(child.pid, signal.SIGTERM)
                    term_sent_at = now
            elif not killed and term_sent_at is not None and now - term_sent_at >= grace_seconds:
                # The leader remains unreaped even if it already exited. Its
                # process-group ID cannot be reused while we kill descendants.
                _kill_group(child.pid, signal.SIGKILL)
                killed = True
            if outcome is not None and killed and exited:
                break
            if now - last_heartbeat >= heartbeat_seconds:
                last_heartbeat = now
                with store._connection() as db:
                    db.execute("UPDATE jobs SET heartbeat_at = ?, updated_at = ? WHERE id = ? AND supervisor_token = ?",
                               (store.clock(), store.clock(), job_id, token))
        if not eof:
            # Final non-blocking drain of whatever is already buffered.
            for _ in range(16):
                try:
                    data = os.read(fd, 64 * 1024)
                except (BlockingIOError, OSError):
                    break
                if not data:
                    break
                log.append(data)
    finally:
        if child.returncode is None:
            if not monitor.ready():
                _kill_group(child.pid, signal.SIGTERM)
                cleanup_deadline = monotonic() + grace_seconds
                while not monitor.ready() and monotonic() < cleanup_deadline:
                    time.sleep(min(poll_seconds, 0.05))
            # A completed job owns no live descendants. Signal before reaping
            # its leader so its group identity cannot have been reused.
            _kill_group(child.pid, signal.SIGKILL)
            child.wait()
        pipe.close()
        monitor.close()
    exit_code = child.returncode
    if outcome == "cancelled":
        state, error = "cancelled", "Cancelled by the owner."
    elif outcome == "timed_out":
        state, error = "timed_out", f"Command exceeded its timeout of {timeout:g} seconds."
    elif exit_code == 0:
        state, error = "completed", None
    else:
        state = "failed"
        error = (f"Command terminated by signal {-exit_code}." if exit_code is not None and exit_code < 0
                 else f"Command exited with status {exit_code}.")
    return _finish(store, job_id, token, state=state, exit_code=exit_code, error=error, discarded=log.discarded)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agent_coord.jobs", description="Managed job supervisor.")
    parser.add_argument("stage", choices=["spawn", "supervise"])
    parser.add_argument("--db", required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--token", required=True)
    arguments = parser.parse_args(argv)
    store = CoordinationStore(arguments.db)
    try:
        if arguments.stage == "spawn":
            pid = spawn_supervisor(store, arguments.job_id, arguments.token)
            sys.stdout.write(f"{pid}\n")
            sys.stdout.flush()
            return 0
        record = supervise(store, arguments.job_id, arguments.token)
    except CoordinationError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1
    sys.stdout.write(json.dumps({"job_id": record["id"], "state": record["state"], "exit_code": record["exit_code"]}) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
