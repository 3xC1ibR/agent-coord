"""Runtime-owned PASSIVE WAL maintenance outside database writer admission.

Only the runtime owning the initialized database should start this worker.
Writable store connections select automatic_checkpoint_pages() on every
checkout; read-only connections retain their existing configuration. The
worker must close after producers and persistence drain, outside writer scopes.
"""

from __future__ import annotations

from collections import deque
import math
import os
from pathlib import Path
import sqlite3
import threading
import time


AUTOMATIC_CHECKPOINT_PAGES = 1000
_owners = {}
_owners_lock = threading.Lock()


def supports_concurrent_checkpoint(version=None):
    """Require the WAL-reset repair, including its documented backports.

    https://sqlite.org/wal.html#walreset
    """
    version = tuple(sqlite3.sqlite_version_info if version is None else version)
    return (version >= (3, 51, 3)
            or version[:2] == (3, 50) and version >= (3, 50, 7)
            or version[:2] == (3, 44) and version >= (3, 44, 6))


def automatic_checkpoint_pages(canonical_database_path):
    """Return the writer setting; the caller supplies its canonical identity.

    Recheck on reused writable handles so stopping or failed maintenance cannot
    leave them indefinitely without automatic checkpoints. This function never
    starts a worker and never changes standalone or read-only connections.
    """
    with _owners_lock:
        owner = _owners.get(os.fspath(canonical_database_path))
    return 0 if owner is not None and owner.enabled else AUTOMATIC_CHECKPOINT_PAGES


def _after_fork():
    global _owners, _owners_lock
    _owners, _owners_lock = {}, threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


class DatabaseCheckpoint:
    """One maintenance connection and worker for a database's runtime lifetime.

    start() is idempotent and publishes readiness only after opening/configuring
    the worker connection. close() joins the worker, including checkpoint I/O;
    it must not run inside writer admission. No thread can safely cancel an
    operating-system fsync, so shutdown waits rather than leaking that worker.
    """

    def __init__(self, database_path, *, interval=0.25, sample_limit=256):
        if not isinstance(interval, (int, float)) or not math.isfinite(interval) or interval <= 0:
            raise ValueError("Checkpoint interval must be a positive finite number.")
        if type(sample_limit) is not int or not 1 <= sample_limit <= 2048:
            raise ValueError("Checkpoint sample limit must be between 1 and 2048.")
        self.database_path = Path(database_path).expanduser().resolve()
        self._identity = str(self.database_path)
        self._wal_path = Path(self._identity + "-wal")
        self._process_id = os.getpid()
        self._interval = interval
        self._lock = threading.Lock()
        self._lifecycle = threading.RLock()
        self._ready, self._stop = threading.Event(), threading.Event()
        self._thread = None
        self._state = "new"
        self._healthy = False
        self._configured = False
        self._retry_pending = False
        self._samples = deque(maxlen=sample_limit)
        self._attempts = self._busy = self._errors = 0
        self._page_size = 0
        self._log_frames = self._checkpointed_frames = self._backlog_frames = None
        self._wal_bytes = 0
        self._last_attempt = self._last_success = self._last_progress = None
        self._last_progress_clock = self._backlog_since = None
        self._last_error = None
        self._maximum_duration = 0.0
        self._last_signature = object()
        self._last_progress_signature = object()
        self.sqlite_version = sqlite3.sqlite_version

    def _check_process(self):
        if os.getpid() != self._process_id:
            raise RuntimeError("Create new database checkpoint maintenance after a process fork.")

    @property
    def enabled(self):
        self._check_process()
        with self._lock:
            return self._state == "running" and self._healthy and not self._stop.is_set()

    def start(self, *, timeout=5.0):
        self._check_process()
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout < 0:
            raise ValueError("Checkpoint startup timeout must be finite and nonnegative.")
        with self._lifecycle:
            if self._thread is not None:
                return self.enabled
            if self._stop.is_set():
                return False
            if not supports_concurrent_checkpoint():
                with self._lock:
                    self._state = "unsupported_sqlite"
                return False
            with _owners_lock:
                if self._identity in _owners:
                    with self._lock:
                        self._state = "already_owned"
                    return False
                _owners[self._identity] = self
            with self._lock:
                self._state = "starting"
            self._thread = threading.Thread(target=self._run, name="database-checkpoint", daemon=True)
            try:
                self._thread.start()
            except BaseException:
                self._release_owner()
                self._thread = None
                with self._lock:
                    self._state = "failed"
                raise
            if not self._ready.wait(timeout):
                self.close()
                return False
            with self._lock:
                configured, failed = self._configured, self._state == "failed"
            if not configured or failed:
                self._stop.set()
                self._thread.join()
                return False
            return True

    def close(self):
        self._check_process()
        with self._lifecycle:
            self._stop.set()
            with self._lock:
                if self._state in {"starting", "running"}:
                    self._state = "stopping"
            if self._thread is not None:
                if self._thread is threading.current_thread():
                    raise RuntimeError("Database checkpoint maintenance cannot join its own worker.")
                self._thread.join()
            self._release_owner()
            with self._lock:
                if self._state in {"new", "stopping"}:
                    self._state = "stopped"

    def _release_owner(self):
        with _owners_lock:
            if _owners.get(self._identity) is self:
                del _owners[self._identity]

    def _connect(self):
        # This connection deliberately bypasses CoordinationStore and its writer
        # gate. Opening mode=rw prevents maintenance from creating a database.
        connection = sqlite3.connect(self.database_path.as_uri() + "?mode=rw", uri=True,
                                     timeout=0, isolation_level=None)
        try:
            connection.execute("PRAGMA busy_timeout = 0").close()
            if connection.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
                raise RuntimeError("Database checkpoint maintenance requires WAL mode.")
            connection.execute("PRAGMA synchronous = FULL").close()
            connection.execute("PRAGMA wal_autocheckpoint = 0").close()
            if connection.execute("PRAGMA synchronous").fetchone()[0] != 2:
                raise RuntimeError("Database checkpoint maintenance requires FULL durability.")
            self._page_size = connection.execute("PRAGMA page_size").fetchone()[0]
            return connection
        except BaseException:
            connection.close()
            raise

    def _wal_signature(self):
        try:
            status = self._wal_path.stat()
            return status.st_mtime_ns, status.st_size
        except FileNotFoundError:
            return None

    def _run(self):
        connection = None
        try:
            connection = self._connect()
            with self._lock:
                self._state, self._healthy = "running", True
                self._configured = True
            self._ready.set()
            while not self._stop.is_set():
                signature = self._wal_signature()
                with self._lock:
                    retry = self._backlog_frames or self._retry_pending or not self._healthy
                if signature != self._last_signature or retry:
                    self._checkpoint(connection)
                    self._last_signature = signature
                self._stop.wait(self._interval)
        except Exception as exc:
            with self._lock:
                self._errors += 1
                self._last_error = type(exc).__name__
                self._state, self._healthy = "failed", False
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception as exc:
                    with self._lock:
                        self._errors += 1
                        self._last_error = type(exc).__name__
            with self._lock:
                if self._state != "failed":
                    self._state = "stopped"
                self._healthy = False
            self._release_owner()
            self._ready.set()

    @staticmethod
    def _execute_checkpoint(connection):
        cursor = connection.execute("PRAGMA main.wal_checkpoint(PASSIVE)")
        try:
            return cursor.fetchone()
        finally:
            cursor.close()

    def _checkpoint(self, connection):
        started = time.monotonic()
        result, error = None, None
        try:
            result = self._execute_checkpoint(connection)
        except sqlite3.Error as exc:
            error = type(exc).__name__
        duration = (time.monotonic() - started) * 1000
        signature = self._wal_signature()
        now, clock = time.time(), time.monotonic()
        with self._lock:
            self._attempts += 1
            self._samples.append(duration)
            self._maximum_duration = max(self._maximum_duration, duration)
            self._last_attempt = now
            self._wal_bytes = signature[1] if signature else 0
            if error is not None:
                self._errors += 1
                self._last_error, self._healthy = error, False
                return
            busy, log, checkpointed = result
            self._busy += int(bool(busy))
            self._healthy = True
            self._retry_pending = bool(busy or log < 0 or checkpointed < 0)
            if log < 0 or checkpointed < 0:
                return  # Another checkpointer can hold the checkpoint lock.
            backlog = max(0, log - checkpointed)
            progress = (checkpointed > (self._checkpointed_frames or 0)
                        or self._log_frames is not None and log < self._log_frames and checkpointed > 0
                        or self._backlog_frames is not None and backlog < self._backlog_frames
                        or backlog == 0 and checkpointed > 0 and signature != self._last_progress_signature)
            self._log_frames, self._checkpointed_frames, self._backlog_frames = log, checkpointed, backlog
            if not busy:
                self._last_success = now
            if progress:
                self._last_progress, self._last_progress_clock = now, clock
                self._last_progress_signature = signature
            if backlog == 0:
                self._backlog_since = None
            elif self._backlog_since is None:
                self._backlog_since = clock

    def snapshot(self):
        self._check_process()
        with self._lock:
            samples = sorted(self._samples)
            clock = time.monotonic()
            return {"mode": "PASSIVE", "sqlite_version": self.sqlite_version,
                    "state": self._state,
                    "enabled": self._state == "running" and self._healthy and not self._stop.is_set(),
                    "attempts": self._attempts, "busy": self._busy, "errors": self._errors,
                    "last_error": self._last_error, "last_attempt_at": self._last_attempt,
                    "last_success_at": self._last_success, "last_progress_at": self._last_progress,
                    "last_progress_age_seconds": None if self._last_progress_clock is None else clock - self._last_progress_clock,
                    "log_frames": self._log_frames, "checkpointed_frames": self._checkpointed_frames,
                    "backlog_frames": self._backlog_frames,
                    "backlog_bytes": None if self._backlog_frames is None else self._backlog_frames * self._page_size,
                    "backlog_age_seconds": 0 if self._backlog_since is None else clock - self._backlog_since,
                    "wal_file_bytes": self._wal_bytes, "sample_capacity": self._samples.maxlen,
                    "sample_count": len(samples),
                    "duration_ms": {"last": self._samples[-1] if samples else None,
                                    "p95": samples[math.ceil(len(samples) * .95) - 1] if samples else None,
                                    "maximum": self._maximum_duration}}
