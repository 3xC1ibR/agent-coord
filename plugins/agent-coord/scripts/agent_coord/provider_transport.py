"""Versioned, provider-neutral ownership of subprocess pipes.

The owner executes no provider adapters and never opens the coordination database.
It retains raw frames and command outcomes while replaceable coordinators attach.
An interrupted write is uncertain, not an instruction to send the bytes again.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import errno
import fcntl
from functools import lru_cache
import hashlib
from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import secrets
import select
import signal
import errno
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid

from .local_http import LocalHTTPServer
from .store import CoordinationError

PROTOCOL = 1
# Native history replies can exceed 32 MiB. Limits apply to frames, encoded
# batches, and retained data independently; ordinary streaming stays small.
MAX_FRAME = 64 * 1024 * 1024
MAX_BATCH_BYTES = MAX_FRAME * 6 + 4096
MAX_JOURNAL_BYTES = 512 * 1024 * 1024
MAX_RECEIPTS = 1_000_000
MAX_CHANNELS = 512
MAX_SPECIFICATION_BYTES = 1024 * 1024
WRITE_TIMEOUT = 5.0


class TransportError(RuntimeError):
    pass


class ProviderLaunchRejected(TransportError, CoordinationError):
    """The process launch failed before any provider input could be sent."""


@lru_cache(maxsize=1)
def _darwin_child_observer():
    """Bind the 64-bit Darwin ABI when Python does not expose os.waitid."""
    import ctypes

    if sys.platform != "darwin" or ctypes.sizeof(ctypes.c_void_p) != 8:
        raise TransportError("Non-reaping child observation is unavailable on this platform.")

    # Darwin SDK sys/signal.h (__siginfo, sigval), sys/_types.h (pid_t,
    # uid_t, id_t), and sys/wait.h (idtype_t, waitid). Both 64-bit Darwin
    # architectures use the unsuffixed UNIX03 waitid symbol (sys/cdefs.h).
    # Verified with sizeof/offsetof against the macOS SDK; no SDK is needed
    # at runtime. Refuse another ABI rather than risk reaping or signaling
    # a child whose retained identity we cannot verify.
    class SignalValue(ctypes.Union):
        _fields_ = [("sival_int", ctypes.c_int), ("sival_ptr", ctypes.c_void_p)]

    class SignalInfo(ctypes.Structure):
        _fields_ = [
            ("si_signo", ctypes.c_int), ("si_errno", ctypes.c_int),
            ("si_code", ctypes.c_int), ("si_pid", ctypes.c_int32),
            ("si_uid", ctypes.c_uint32), ("si_status", ctypes.c_int),
            ("si_addr", ctypes.c_void_p), ("si_value", SignalValue),
            ("si_band", ctypes.c_long), ("__pad", ctypes.c_ulong * 7),
        ]

    expected_offsets = {"si_pid": 12, "si_status": 20, "si_addr": 24,
                        "si_value": 32, "si_band": 40, "__pad": 48}
    if (ctypes.sizeof(SignalInfo) != 104 or ctypes.alignment(SignalInfo) != 8
            or any(getattr(SignalInfo, name).offset != offset
                   for name, offset in expected_offsets.items())
            or (os.P_PID, os.WEXITED, os.WNOHANG, os.WNOWAIT) != (1, 4, 1, 32)):
        raise TransportError("Unsupported Darwin child observation ABI.")
    library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    waitid = library.waitid
    waitid.argtypes = [ctypes.c_int, ctypes.c_uint32,
                       ctypes.POINTER(SignalInfo), ctypes.c_int]
    waitid.restype = ctypes.c_int

    def observe(pid):
        information = SignalInfo()
        while waitid(os.P_PID, pid, ctypes.byref(information),
                     os.WEXITED | os.WNOHANG | os.WNOWAIT) == -1:
            code = ctypes.get_errno()
            if code != errno.EINTR:
                raise OSError(code, os.strerror(code))
        return information.si_pid or None

    return observe


def observe_child_exit(pid):
    """Return an exited child PID without reaping it, or None while it runs."""
    native_waitid = getattr(os, "waitid", None)
    if callable(native_waitid):
        result = native_waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        observed_pid = result.si_pid if result is not None else None
    else:
        observed_pid = _darwin_child_observer()(pid)
    if observed_pid is not None and observed_pid != pid:
        raise TransportError("Child observation returned an unexpected process identity.")
    return observed_pid


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def storage_exhausted(error):
    """Whether SQLite failed because the volume ran out of space."""
    if isinstance(error, OSError):
        return error.errno in {errno.ENOSPC, errno.EDQUOT, errno.EIO}
    name = getattr(error, "sqlite_errorname", None)  # Python 3.11+
    if name is not None:
        return name == "SQLITE_FULL" or name.startswith("SQLITE_IOERR")
    return "database or disk is full" in str(error) or "disk I/O error" in str(error)


def event_size(channel, stream, data):
    # Includes the largest sequence/size fields and batch separators. Counting
    # serialized bytes also covers JSON escaping of control characters.
    return len(encode({"channel": channel, "stream": stream, "data": data}).encode()) + 96


def atomic_record(path, value):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(encode(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def boot_identity():
    """Identify a macOS boot without interpreting a potentially recycled PID."""
    if sys.platform != "darwin":
        return None
    result = subprocess.run(["/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"],
                            check=True, capture_output=True, text=True, timeout=2)
    return str(uuid.UUID(result.stdout.strip()))


def retained_directory(directory, record):
    identity = str(uuid.UUID(record["instance"]))
    path = Path(directory) / "retired" / identity
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def retire_dead_owner(directory, record, before_retire):
    """Explicit maintenance only: preserve evidence, never signal stale PIDs."""
    directory = Path(directory)
    with (directory / "owner.lock").open("a") as guard:
        try:
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise TransportError("Provider transport is unavailable but still owns its lock; Stop is unconfirmed.") from error
        path = directory / "owner.json"
        if json.loads(path.read_text()) != record:
            raise TransportError("Provider ownership changed during maintenance; no state was retired.")
        current_boot = boot_identity()
        previous_boot = record.get("boot_id")
        previous_boot = str(uuid.UUID(previous_boot)) if previous_boot else None
        prior_boot = bool(current_boot and previous_boot and current_boot != previous_boot)
        unused = False
        if not prior_boot and not record.get("shutdown_error"):
            journal = directory / "transport.sqlite3"
            if record.get("journal_generation") == record["instance"]:
                journal = directory / "generations" / str(uuid.UUID(record["instance"])) / "transport.sqlite3"
            try:
                with closing(sqlite3.connect(journal.resolve().as_uri() + "?mode=ro", uri=True)) as db:
                    # An opening intent precedes spawn. Even a missing PID or a
                    # closed channel cannot establish absence of escaped tools.
                    unused = (db.execute("SELECT count(*) FROM channels").fetchone()[0] == 0
                              and db.execute("SELECT count(*) FROM commands WHERE operation='open'").fetchone()[0] == 0)
            except sqlite3.Error:
                pass  # Missing or incomplete evidence never authorizes recovery.
        if not prior_boot and not unused:
            recovery = ("Recovery requires a verified new boot." if previous_boot else
                        "This record has no verifiable boot identity; explicit maintenance review is required.")
            raise TransportError("Provider transport owner exited, but surviving providers cannot be verified in this boot. "
                                 "Retained evidence was preserved; no process was signaled. " + recovery)
        reason = ("The previous system boot ended; provider work was interrupted. Review tool outcomes before continuing."
                  if prior_boot else "The transport owner exited before opening any provider.")
        archive = retained_directory(directory, record)
        atomic_record(archive / "owner.json", {**record, "state": "interrupted", "reason": reason,
                                               "recovered_boot_id": current_boot})
        before_retire(archive, reason)
        # The active marker is removed last; failure retains the maintenance
        # boundary and any original shutdown_error, including across retries.
        path.unlink()


class Journal:
    """Single-owner WAL; command intent precedes every irreversible pipe write."""

    def __init__(self, directory, *, file_events=None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        self.path = self.directory / "transport.sqlite3"
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.stopping = False
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        os.chmod(self.path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        with self.db:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS ownership (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), epoch INTEGER NOT NULL,
                    coordinator TEXT NOT NULL);
                INSERT OR IGNORE INTO ownership VALUES (1, 0, '');
                CREATE TABLE IF NOT EXISTS commands (
                    command_id TEXT PRIMARY KEY, channel TEXT NOT NULL, operation TEXT NOT NULL,
                    payload TEXT NOT NULL, outcome TEXT NOT NULL, result TEXT);
                CREATE TABLE IF NOT EXISTS channels (
                    channel TEXT PRIMARY KEY, specification TEXT NOT NULL, pid INTEGER,
                    state TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS channels_state ON channels(state);
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT NOT NULL,
                    stream TEXT NOT NULL, data TEXT NOT NULL, size INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS journal_bounds (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    acknowledged INTEGER NOT NULL, retained_after INTEGER NOT NULL);
                INSERT OR IGNORE INTO journal_bounds VALUES (1, 0, 0);
            """)
            if "size" not in {r["name"] for r in self.db.execute("PRAGMA table_info(events)")}:
                self.db.execute("ALTER TABLE events ADD COLUMN size INTEGER NOT NULL DEFAULT 0")
            while True:
                rows = self.db.execute("SELECT * FROM events WHERE size=0 LIMIT 128").fetchall()
                if not rows:
                    break
                for row in rows:
                    self.db.execute("UPDATE events SET size=? WHERE sequence=?",
                        (event_size(row["channel"], row["stream"], row["data"]), row["sequence"]))
            # Pipes cannot be recovered after the low-level owner itself dies.
            self.db.execute("UPDATE commands SET outcome='uncertain' WHERE outcome='writing'")
            self.db.execute("UPDATE channels SET state='lost' WHERE state IN ('opening','running','write-uncertain')")
        self.event_bytes = self.db.execute("SELECT COALESCE(SUM(size),0) FROM events").fetchone()[0]
        self.receipt_count = self.db.execute("SELECT count(*) FROM commands").fetchone()[0]
        from .stream_journal import StreamJournal
        spool_directory = self.directory / "events"
        use_spool = file_events is True or spool_directory.exists()
        try:
            prior_sequence = self.db.execute("SELECT seq FROM sqlite_sequence WHERE name='events'").fetchone()
            if use_spool and prior_sequence and prior_sequence[0]:
                raise TransportError("File event spool requires a new transport generation.")
            self.spool = StreamJournal(spool_directory) if use_spool else None
        except BaseException:
            self.db.close()
            raise
        if self.spool is not None:
            self.event_bytes = self.spool.event_bytes

    def sequence(self):
        with self.lock:
            if self.spool is not None:
                return self.spool.sequence
            row = self.db.execute("SELECT seq FROM sqlite_sequence WHERE name='events'").fetchone()
            return row[0] if row else 0

    def bounds(self):
        with self.lock:
            bounds = ({"acknowledged": self.spool.acknowledged, "retained_after": self.spool.retained_after}
                      if self.spool is not None else
                      dict(self.db.execute("SELECT acknowledged,retained_after FROM journal_bounds").fetchone()))
            return {**bounds, "event_storage": "file" if self.spool is not None else "sqlite",
                    "event_bytes": self.event_bytes, "event_limit_bytes": MAX_JOURNAL_BYTES,
                    "receipt_count": self.receipt_count, "receipt_limit": MAX_RECEIPTS}

    def ownership(self):
        with self.lock:
            return dict(self.db.execute("SELECT epoch, coordinator FROM ownership").fetchone())

    def fence(self, epoch, coordinator):
        if self.ownership() != {"epoch": epoch, "coordinator": coordinator}:
            raise TransportError("Coordinator ownership is fenced.")

    def claim(self, expected, coordinator):
        if not isinstance(coordinator, str) or not coordinator:
            raise TransportError("A coordinator identity is required.")
        with self.lock, self.db:
            current = self.ownership()
            if current["coordinator"] == coordinator:
                return current
            if current["epoch"] != expected:
                raise TransportError("Transport ownership changed before claim.")
            self.db.execute("UPDATE ownership SET epoch=epoch+1, coordinator=?", (coordinator,))
            self.changed.notify_all()
            return self.ownership()

    def append(self, channel, stream, data):
        size = event_size(channel, stream, data)
        if size > MAX_BATCH_BYTES or size > MAX_JOURNAL_BYTES:
            raise TransportError("Provider event exceeds the retained transport capacity.")
        with self.changed:
            reported = False
            while True:
                # Backpressure preserves the next frame in this reader, bounded by
                # MAX_FRAME. No database transaction remains open while waiting.
                while self.event_bytes + size > MAX_JOURNAL_BYTES and not self.stopping:
                    self.changed.wait(.2)
                if self.stopping:
                    return None
                try:
                    if self.spool is not None:
                        sequence = self.spool.append(channel, stream, data, size)
                        break
                    with self.db:
                        cursor = self.db.execute("INSERT INTO events(channel,stream,data,size) VALUES (?,?,?,?)",
                                                 (channel, stream, data, size))
                    break
                except (sqlite3.OperationalError, OSError) as error:
                    # A full disk must stall this reader like journal capacity.
                    # Ending it delivers end-of-output, which retires a live provider.
                    if not storage_exhausted(error):
                        raise
                    if not reported:
                        print(f"Provider transport journal write deferred: {error}", file=sys.stderr, flush=True)
                        reported = True
                    # The wait releases the lock; capacity is checked again before retrying.
                    self.changed.wait(1)
            self.event_bytes += size
            self.changed.notify_all()
            return sequence if self.spool is not None else cursor.lastrowid

    def acknowledge_events(self, cursor, retain_after=0):
        """Advance only a caller-proven durable recovery floor.

        retain_after must include any checkpoint to which rollback can restore
        the coordinator. Acknowledging delivery alone does not authorize pruning.
        """
        if type(cursor) is not int or type(retain_after) is not int or not 0 <= retain_after <= cursor:
            raise TransportError("Invalid transport acknowledgement or recovery floor.")
        with self.changed:
            current = self.bounds()
            if cursor > self.sequence() or cursor < current["acknowledged"]:
                raise TransportError("Transport acknowledgement is outside the available sequence.")
            if retain_after < current["retained_after"]:
                raise TransportError("The requested recovery floor has already been retired.")
            if self.spool is not None:
                self.spool.acknowledge(cursor, retain_after)
                self.event_bytes = self.spool.event_bytes
                self.changed.notify_all()
                return self.bounds()
            # Bounded maintenance avoids monopolizing the owner behind a large
            # checkpoint. Repeating the same acknowledgement finishes pruning.
            rows = self.db.execute("SELECT sequence,size FROM events WHERE sequence<=? ORDER BY sequence LIMIT 256",
                                   (retain_after,)).fetchall()
            retired = rows[-1]["sequence"] if rows else current["retained_after"]
            with self.db:
                if rows:
                    self.db.execute("DELETE FROM events WHERE sequence<=?", (retired,))
                self.db.execute("UPDATE journal_bounds SET acknowledged=?,retained_after=?",
                                (cursor, retired))
            self.event_bytes -= sum(row["size"] for row in rows)
            self.changed.notify_all()
            return self.bounds()

    def stop_waits(self):
        with self.changed:
            self.stopping = True
            self.changed.notify_all()

    def events(self, after, limit=256):
        if type(after) is not int or after < 0 or type(limit) is not int or limit < 1:
            raise TransportError("Invalid transport event cursor or batch limit.")
        with self.lock:
            if after < self.bounds()["retained_after"]:
                raise TransportError("Requested transport cursor is no longer retained; recovery must stop.")
            if self.spool is not None:
                return self.spool.events(after, min(limit, 256), MAX_BATCH_BYTES)
            result, size = [], 0
            for row in self.db.execute("SELECT * FROM events WHERE sequence>? ORDER BY sequence LIMIT ?",
                                       (after, min(limit, 256))):
                count = row["size"]
                if result and size + count > MAX_BATCH_BYTES:
                    break
                size += count
                result.append({key: row[key] for key in ("sequence", "channel", "stream", "data")})
            return result

    def close(self):
        self.stop_waits()
        with self.lock:
            if self.spool is not None:
                self.spool.close()
            self.db.close()


class Owner:
    def __init__(self, directory):
        self.directory = Path(directory)
        if (self.directory / "owner.json").exists():
            raise TransportError("Retained transport ownership requires explicit Stop maintenance before restart.")
        self.instance = uuid.uuid4().hex
        self.boot_id = boot_identity()
        # Each pipe owner has a fresh sequence space. Retired raw events and
        # uncertain command receipts remain available under the old generation.
        self.journal = Journal(self.directory / "generations" / str(uuid.UUID(self.instance)), file_events=True)
        self.processes = {}
        self.channel_locks = {}
        self.readers = []
        self.channel_readers = {}
        self.active_commands = 0
        self.stopping = threading.Event()
        self.token = secrets.token_urlsafe(32)

    def _reap_finished(self):
        """Release completed local handles without discarding delivery records."""
        with self.journal.lock:
            for channel, process in list(self.processes.items()):
                if (channel in self.channel_locks
                        or any(reader.is_alive() for reader in self.channel_readers[channel])
                        or process.poll() is None):
                    continue
                for stream in (process.stdin, process.stdout, process.stderr):
                    stream.close()
                with self.journal.db:
                    self.journal.db.execute("UPDATE channels SET state='closed',specification='' WHERE channel=?", (channel,))
                del self.processes[channel]
                del self.channel_readers[channel]
            self.readers[:] = [reader for reader in self.readers if reader.is_alive()]

    def _read(self, channel, process, stream):
        source = getattr(process, stream)
        try:
            while True:
                frame = source.readline(MAX_FRAME + 1)
                if not frame:
                    break
                if len(frame) > MAX_FRAME:
                    self.journal.append(channel, "error", "Provider frame exceeded transport limit.")
                    # Preserve the subprocess but refuse to interpret truncated frames.
                    while frame and not frame.endswith(b"\n"):
                        frame = source.readline(MAX_FRAME + 1)
                    continue
                if self.journal.append(channel, stream, frame.decode("utf-8", errors="replace")) is None:
                    break
        except (OSError, ValueError):
            if not self.stopping.is_set():
                raise
        finally:
            if stream == "stdout":
                self.journal.append(channel, "eof", "")

    def dispatch(self, body):
        action = body.get("action")
        if action == "status":
            self._reap_finished()
            with self.journal.lock:
                channels = [dict(row) for row in self.journal.db.execute("SELECT channel,pid,state FROM channels")]
                for channel in channels:
                    process = self.processes.get(channel["channel"])
                    channel["pgid"] = None
                    if process is not None and process.returncode is None:
                        try:
                            channel["pgid"] = os.getpgid(process.pid)
                        except ProcessLookupError:
                            pass
                return {"protocol": PROTOCOL, "instance": self.instance, "pid": os.getpid(),
                        **self.journal.ownership(), "channels": channels,
                        "sequence": self.journal.sequence(), "retention": self.journal.bounds()}
        if action == "claim":
            return self.journal.claim(body["expected_epoch"], body["coordinator"])
        with self.journal.lock:
            self.journal.fence(body.get("epoch"), body.get("coordinator"))
            if action == "events":
                events = self.journal.events(body["after"])
                if body.get("wait") is not True:
                    return {"events": events}
                if not events and not self.journal.stopping:
                    # Release the journal lock while the provider writes. A
                    # replacement may have claimed the journal in the meantime.
                    self.journal.changed.wait(.25)
                    self.journal.fence(body.get("epoch"), body.get("coordinator"))
                    events = self.journal.events(body["after"])
                return {"events": events, "event_wait": True}
            if action == "acknowledge_events":
                return self.journal.acknowledge_events(body["cursor"], body.get("retain_after", 0))
            if action == "outcome":
                row = self.journal.db.execute("SELECT outcome,result FROM commands WHERE command_id=?",
                                              (body["command_id"],)).fetchone()
                return {"outcome": row["outcome"], "result": json.loads(row["result"]) if row["result"] else None} if row else {"outcome": "absent", "result": None}
            if action == "stop":
                self.stopping.set()
                self.journal.stop_waits()
                return {"stopping": True}
            if action != "command":
                raise TransportError("Unknown transport operation.")
            if self.stopping.is_set():
                raise TransportError("Provider transport is stopping; no command was accepted.")
            channel = body.get("channel")
            if not isinstance(channel, str) or not 1 <= len(channel) <= 256:
                raise TransportError("Invalid provider channel identity.")
            entry = self.channel_locks.setdefault(channel, [threading.Lock(), 0])
            entry[1] += 1
            self.active_commands += 1
            channel_lock = entry[0]
        # The process may produce output before consuming input. Never hold the
        # journal lock over pipe backpressure, process creation, or exit waits.
        acquired = False
        try:
            acquired = channel_lock.acquire(timeout=WRITE_TIMEOUT)
            if not acquired:
                raise TransportError("Provider channel is busy; no additional command was accepted.")
            return self.command(body)
        finally:
            if acquired:
                channel_lock.release()
            with self.journal.lock:
                entry[1] -= 1
                if not entry[1]:
                    del self.channel_locks[channel]
                self.active_commands -= 1
                self.journal.changed.notify_all()

    def command(self, body):
        identity, channel, operation, value = (body[k] for k in ("command_id", "channel", "operation", "value"))
        if not isinstance(identity, str) or not 1 <= len(identity) <= 256:
            raise TransportError("Invalid transport command identity.")
        encoded = "sha256:" + hashlib.sha256(encode(value).encode()).hexdigest()
        with self.journal.lock:
            self.journal.fence(body.get("epoch"), body.get("coordinator"))
            row = self.journal.db.execute("SELECT * FROM commands WHERE command_id=?", (identity,)).fetchone()
            if row is not None:
                expected = encoded if row["payload"].startswith("sha256:") else encode(value)
                if (row["channel"], row["operation"], row["payload"]) != (channel, operation, expected):
                    raise TransportError("Command identity was reused with different content.")
                return {"outcome": row["outcome"], "result": json.loads(row["result"]) if row["result"] else None}
            if operation not in {"open", "write", "close"}:
                raise TransportError("Unsupported transport command.")
            if self.journal.receipt_count >= MAX_RECEIPTS:
                raise TransportError("Transport receipt capacity reached; no command was accepted. Retained receipts require maintenance.")
            with self.journal.db:
                self.journal.db.execute("INSERT INTO commands VALUES (?,?,?,?,?,NULL)",
                                        (identity, channel, operation, encoded, "writing"))
            self.journal.receipt_count += 1
        try:
            result = self._command(channel, operation, value)
        except Exception as error:
            outcome = "rejected" if operation == "open" and isinstance(error, ProviderLaunchRejected) else "uncertain"
            result = {"error": str(error)}
            with self.journal.lock, self.journal.db:
                self.journal.db.execute("UPDATE commands SET outcome=?,result=? WHERE command_id=?",
                                        (outcome, encode(result), identity))
            return {"outcome": outcome, "result": result}
        with self.journal.lock, self.journal.db:
            self.journal.db.execute("UPDATE commands SET outcome='written',result=? WHERE command_id=?",
                                    (encode(result), identity))
        return {"outcome": "written", "result": result}

    def _command(self, channel, operation, value):
        if self.stopping.is_set():
            raise TransportError("Provider transport stopped before command delivery.")
        if operation == "open":
            specification = encode(value)
            if len(specification.encode()) > MAX_SPECIFICATION_BYTES:
                raise TransportError("Provider process specification exceeds its capacity.")
            self._reap_finished()
            with self.journal.lock:
                row = self.journal.db.execute("SELECT * FROM channels WHERE channel=?", (channel,)).fetchone()
                if not row and self.journal.db.execute("SELECT count(*) FROM channels WHERE state IN ('opening','running','write-uncertain')").fetchone()[0] >= MAX_CHANNELS:
                    raise TransportError("Provider channel capacity reached.")
                if row:
                    if row["specification"] != specification or row["state"] != "running":
                        raise TransportError("Existing transport cannot be opened with this specification or state.")
                    return {"pid": row["pid"], "attached": True}
                with self.journal.db:
                    self.journal.db.execute("INSERT INTO channels VALUES (?,?,NULL,'opening')", (channel, specification))
            try:
                process = subprocess.Popen(value["args"], cwd=value.get("cwd"), env=value["env"],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            except Exception as error:
                with self.journal.lock, self.journal.db:
                    self.journal.db.execute("UPDATE channels SET state='closed',specification='' WHERE channel=?", (channel,))
                if isinstance(error, OSError):
                    raise ProviderLaunchRejected(str(error)) from error
                raise
            os.set_blocking(process.stdin.fileno(), False)
            with self.journal.lock, self.journal.db:
                self.processes[channel] = process
                self.journal.db.execute("UPDATE channels SET pid=?,state='running' WHERE channel=?", (process.pid, channel))
                readers = [threading.Thread(target=self._read, args=(channel, process, stream), daemon=True)
                           for stream in ("stdout", "stderr")]
                self.channel_readers[channel] = readers
                self.readers.extend(readers)
                for reader in readers:
                    reader.start()
            return {"pid": process.pid, "attached": False}
        process = self.processes.get(channel)
        if process is None:
            if operation == "close":
                with self.journal.lock:
                    state = self.journal.db.execute("SELECT state FROM channels WHERE channel=?", (channel,)).fetchone()
                    if state is not None and state["state"] == "closed":
                        # Status may already have reaped a naturally exited
                        # provider. Its stored PID no longer establishes ownership.
                        return {"closed": True}
            raise TransportError("Provider transport is unavailable; delivery requires review.")
        if operation == "write":
            data = value["data"].encode("utf-8")
            if len(data) > MAX_FRAME or not data.endswith(b"\n"):
                raise TransportError("Invalid provider frame.")
            with self.journal.lock:
                state = self.journal.db.execute("SELECT state FROM channels WHERE channel=?", (channel,)).fetchone()
                if state is None or state["state"] != "running":
                    raise TransportError("Provider channel has unresolved delivery; further frames are refused.")
            descriptor, written = process.stdin.fileno(), 0
            deadline = time.monotonic() + WRITE_TIMEOUT
            try:
                while written < len(data):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or self.stopping.is_set():
                        raise TransportError("Provider pipe write did not finish; delivery is uncertain.")
                    if not select.select([], [descriptor], [], min(.1, remaining))[1]:
                        continue
                    try:
                        count = os.write(descriptor, memoryview(data)[written:])
                    except BlockingIOError:
                        continue
                    if count <= 0:
                        raise TransportError("Provider pipe closed during delivery.")
                    written += count
            except Exception:
                # Never concatenate a later command onto a partially delivered
                # frame. The provider stays alive; explicit recovery is required.
                with self.journal.lock, self.journal.db:
                    self.journal.db.execute("UPDATE channels SET state='write-uncertain' WHERE channel=?", (channel,))
                raise
            return {"bytes": len(data)}
        self._close_process(process)
        with self.journal.lock, self.journal.db:
            self.journal.db.execute("UPDATE channels SET state='closed',specification='' WHERE channel=?", (channel,))
        return {"closed": True}

    @staticmethod
    def _signal_process_group(process, number):
        if getattr(process, "_transport_closed", False):
            return
        # Every process was launched with start_new_session=True. Explicit
        # close owns that group, including tools retaining stdout/stderr after
        # the provider leader exits. A coordinator detach never calls here.
        group = process.pid
        if process.returncode is not None:
            raise TransportError("Provider group ownership is uncertain after its leader was reaped; no signal was sent.")
        try:
            # WNOWAIT preserves the owned child identity until every group
            # signal is sent, so its numeric PID cannot be recycled meanwhile.
            exited = observe_child_exit(process.pid)
        except ChildProcessError as error:
            raise TransportError("Provider group ownership is no longer verifiable; no signal was sent.") from error
        try:
            actual_group = os.getpgid(process.pid)
        except ProcessLookupError:
            if exited is None:
                raise TransportError("Provider process group is unavailable; no signal was sent.")
            actual_group = group
        if actual_group != group:
            raise TransportError("Provider process group no longer matches its retained ownership.")
        try:
            os.killpg(group, number)
        except ProcessLookupError:
            pass
        except PermissionError:
            # macOS returns EPERM for a group containing only zombies. Keep
            # the leader waitable while checking membership; a live member or
            # failed inspection leaves shutdown explicitly incomplete.
            if exited is None or Owner._group_has_live_members(group):
                raise

    @staticmethod
    def _group_has_live_members(group):
        result = subprocess.run(["ps", "-axo", "pgid=,stat="], check=True,
                                capture_output=True, text=True, timeout=2)
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) != 2:
                raise TransportError("Cannot verify provider process group membership.")
            if int(fields[0]) == group and not fields[1].startswith("Z"):
                return True
        return False

    @classmethod
    def _close_process(cls, process, *, deadline=None):
        if getattr(process, "_transport_closed", False):
            return
        group = process.pid
        if deadline is None:
            cls._signal_process_group(process, signal.SIGTERM)
            deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if observe_child_exit(process.pid) is not None:
                break
            time.sleep(.02)
        cls._signal_process_group(process, signal.SIGKILL)
        process.wait(timeout=3)
        process._transport_closed = True

    @classmethod
    def _close_processes(cls, processes):
        # Stop grants one shared grace period, not three seconds per provider.
        processes = list(processes)
        eligible, errors = [], []
        for process in processes:
            try:
                cls._signal_process_group(process, signal.SIGTERM)
                eligible.append(process)
            except Exception as error:
                errors.append(f"Process {process.pid}: {error}")
        deadline = time.monotonic() + 3
        for process in eligible:
            try:
                cls._close_process(process, deadline=deadline)
            except Exception as error:
                errors.append(f"Process {process.pid}: {error}")
        if errors:
            raise TransportError("Provider shutdown incomplete: " + "; ".join(errors))

    def _close_streams(self):
        deadline = time.monotonic() + 5
        for reader in self.readers:
            reader.join(timeout=max(0, deadline - time.monotonic()))
        incomplete = False
        for channel, process in self.processes.items():
            process.stdin.close()
            for stream, reader in zip((process.stdout, process.stderr), self.channel_readers[channel]):
                if reader.is_alive():
                    # An escaped descendant may retain a pipe. Closing the raw
                    # FileIO avoids BufferedReader.close waiting on its lock;
                    # the remaining daemon reader cannot hold owner shutdown.
                    stream.raw.close()
                    incomplete = True
                else:
                    stream.close()
        if incomplete:
            raise TransportError("Provider pipe shutdown incomplete; a process outside the owned groups may retain a descriptor.")

    def serve(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                if self.headers.get("Origin") or not secrets.compare_digest(
                        self.headers.get("Authorization", ""), "Bearer " + owner.token):
                    self.send_error(403)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= MAX_BATCH_BYTES:
                        raise TransportError("Invalid transport request size.")
                    body = json.loads(self.rfile.read(length))
                    if body.get("instance") != owner.instance:
                        raise TransportError("Wrong transport instance.")
                    value = owner.dispatch(body)
                except Exception as error:
                    value = {"error": str(error)}
                data = encode(value).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_):
                pass

        server = LocalHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        server.timeout = .2
        record = {"url": f"http://127.0.0.1:{server.server_port}/", "token": self.token,
                  "instance": self.instance, "protocol": PROTOCOL, "pid": os.getpid(),
                  "boot_id": self.boot_id, "journal_generation": self.instance, "event_storage": "file"}
        atomic_record(self.directory / "owner.json", record)
        try:
            while not self.stopping.is_set():
                server.handle_request()
        finally:
            server.server_close()
            self.journal.stop_waits()
            with self.journal.changed:
                while self.active_commands:
                    self.journal.changed.wait(.1)
            failures = []
            for cleanup in (lambda: self._close_processes(self.processes.values()), self._close_streams):
                try:
                    cleanup()
                except Exception as error:
                    failures.append(str(error))
            record_path = self.directory / "owner.json"
            self.journal.close()
            if failures:
                atomic_record(record_path, {**record, "shutdown_error": "; ".join(failures)})
            else:
                atomic_record(retained_directory(self.directory, record) / "owner.json", {**record, "state": "stopped"})
                record_path.unlink(missing_ok=True)
            if failures:
                raise TransportError("; ".join(failures))


class Client:
    def __init__(self, record, coordinator, epoch):
        self.record, self.coordinator, self.epoch = record, coordinator, epoch

    def call(self, action, **values):
        body = {"instance": self.record["instance"], "coordinator": self.coordinator,
                "epoch": self.epoch, "action": action, **values}
        request = urllib.request.Request(self.record["url"], data=encode(body).encode(),
            headers={"Authorization": "Bearer " + self.record["token"], "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                result = json.load(response)
        except (OSError, ValueError) as error:
            raise TransportError("Provider transport outcome unavailable; do not retry delivery blindly.") from error
        if "error" in result:
            raise TransportError(result["error"])
        return result

    def command(self, channel, operation, value, *, command_id=None):
        result = self.call("command", channel=channel, operation=operation, value=value,
                           command_id=command_id or uuid.uuid4().hex)
        if operation == "open" and result["outcome"] == "rejected":
            raise ProviderLaunchRejected(result["result"]["error"])
        if result["outcome"] != "written":
            raise TransportError("Provider delivery is uncertain; inspect the retained command outcome.")
        return result["result"]

    @classmethod
    def connect(cls, directory, coordinator):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / "owner.json"
        if not path.exists():
            with (directory / "owner.log").open("ab", buffering=0) as log:
                process = subprocess.Popen([sys.executable, "-B", "-m", __name__, "--directory", str(directory)],
                    stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
            deadline = time.monotonic() + 10
            while not path.exists() and time.monotonic() < deadline and process.poll() is None:
                time.sleep(.02)
            if not path.exists():
                raise TransportError("Provider transport did not become ready.")
            threading.Thread(target=process.wait, daemon=True).start()
        record = json.loads(path.read_text())
        if record["protocol"] != PROTOCOL:
            raise TransportError("Provider transport protocol is incompatible.")
        client = cls(record, coordinator, 0)
        try:
            state = client.call("status")
        except TransportError as error:
            raise TransportError("Provider transport is unavailable; explicit Stop must verify retained ownership before restart. "
                                 "No command was replayed.") from error
        claim = client.call("claim", expected_epoch=state["epoch"])
        client.epoch = claim["epoch"]
        return client


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (args.directory / "owner.lock").open("a") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        Owner(args.directory).serve()


if __name__ == "__main__":
    main()
