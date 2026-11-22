"""Process-wide admission for finite SQLite write scopes and bounded telemetry.

Readers explicitly use CoordinationStore.reading(). No SQL, parameters or
conversation content is retained in diagnostics. This does not serialize whole
requests: provider/network work must remain outside database scopes.
"""
from __future__ import annotations

from collections import deque
from contextlib import contextmanager
import heapq
import math
import os
from pathlib import Path
import sqlite3
import sys
import threading
import time
import weakref

from .sqlite_compatibility import SQLITE_BUSY

try:
    import resource
except ImportError:  # Windows has no resource module.
    resource = None

_registry = weakref.WeakValueDictionary()
_registry_lock = threading.Lock()
SLOW_DATABASE_THRESHOLD_MS = 50.0
SLOW_DATABASE_PHASES = frozenset({
    "connection.open",
    "statement.execute", "statement.executemany", "statement.executescript",
    "statement.fetchall", "statement.fetchmany", "transaction.commit", "transaction.rollback", "scope",
})


def _after_fork():
    global _registry, _registry_lock
    _registry, _registry_lock = weakref.WeakValueDictionary(), threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


def writer_for(database):
    identity = str(Path(database).resolve())
    with _registry_lock:
        writer = _registry.get(identity)
        if writer is None:
            writer = _registry[identity] = DatabaseWriter()
        return writer


def _distribution(values):
    values = sorted(values)
    def percentile(fraction):
        return round(values[max(0, math.ceil(len(values) * fraction) - 1)], 3) if values else 0
    return {"samples": len(values), "p50_ms": percentile(.5), "p95_ms": percentile(.95),
            "p99_ms": percentile(.99), "max_ms": round(max(values, default=0), 3)}


def _thread_cpu_time():
    try:
        return time.thread_time()
    except (AttributeError, OSError):
        return None


def system_performance_snapshot():
    """Fixed-size process/host measurements without subprocesses or stored data."""
    try:
        load = dict(zip(("one_minute", "five_minutes", "fifteen_minutes"),
                        (round(value, 3) for value in os.getloadavg())))
    except (AttributeError, OSError):
        load = None
    try:
        cpu_count = os.cpu_count()
    except OSError:
        cpu_count = None
    try:
        cpu_seconds = round(time.process_time(), 6)
    except (AttributeError, OSError):
        cpu_seconds = None
    peak_resident_bytes = None
    if resource is not None and (sys.platform == "darwin" or sys.platform.startswith("linux")):
        try:
            # Darwin reports bytes; Linux getrusage(2) reports KiB. Do not
            # guess units for other platforms merely exposing this field.
            maximum = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            peak_resident_bytes = int(maximum) * (1 if sys.platform == "darwin" else 1024)
        except (AttributeError, OSError, ValueError, OverflowError):
            pass
    return {"host": {"logical_cpu_count": cpu_count, "load_average": load},
            "process": {"cpu_seconds": cpu_seconds, "peak_resident_memory_bytes": peak_resident_bytes}}


class DatabaseWriter:
    def __init__(self):
        self.condition = threading.Condition()
        self.owner = None
        self.connections = []
        self.queue = []
        self.foreground_streak = 0
        self.active_operation = None
        self.started = None
        self.started_thread_cpu = None
        self.maximum_queue = 0
        self.completed = self.failures = self.timeouts = 0
        self.waits = deque(maxlen=2048)
        self.holds = deque(maxlen=2048)
        self.operations = {}
        self.named_operations = set()
        self.inferred_operations = set()
        self.requests = {}
        self.slow_operations = deque(maxlen=32)
        self.slow_operation_count = 0
        self.scope_sequence = 0
        self.active_scope_id = None
        self.active_slow_phase_durations = None
        self.slow_writer_scopes = []
        self.slow_writer_scope_capacity = 16
        self.admission_timeout_records = deque(maxlen=16)

    def _next(self):
        foreground = [entry for entry in self.queue if entry[1] == "foreground"]
        background = [entry for entry in self.queue if entry[1] == "background"]
        return (background[0] if background and (not foreground or self.foreground_streak >= 8)
                else foreground[0] if foreground else self.queue[0] if self.queue else None)

    @contextmanager
    def reserve(self, connection, *, timeout=5, operation="database", priority="foreground", named=True):
        identity = threading.get_ident()
        entered = time.monotonic()
        nested = False
        with self.condition:
            if self.owner == identity:
                nested = True
            else:
                entry = (object(), priority)
                self.queue.append(entry)
                self.maximum_queue = max(self.maximum_queue, len(self.queue))
                deadline = entered + timeout
                try:
                    while self.owner is not None or self._next() is not entry:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            self.timeouts += 1
                            observed = time.monotonic()
                            self.admission_timeout_records.append({
                                "operation": operation, "priority": priority,
                                "observed_monotonic": observed,
                                "wait_ms": round((observed - entered) * 1000, 3),
                                "queue_depth": len(self.queue),
                                "active_operation": self.active_operation,
                                "active_scope_id": self.active_scope_id,
                                "active_duration_ms": round((observed - self.started) * 1000, 3)
                                                      if self.started is not None else 0})
                            error = sqlite3.OperationalError("database is busy: writer admission deadline exceeded")
                            error.sqlite_errorcode, error.sqlite_errorname = SQLITE_BUSY, "SQLITE_BUSY"
                            raise error
                        self.condition.wait(remaining)
                except BaseException:
                    self.queue.remove(entry)
                    self.condition.notify_all()
                    raise
                self.queue.remove(entry)
                self.owner = identity
                self.active_operation = operation
                self.scope_sequence += 1
                self.active_scope_id = self.scope_sequence
                self.active_slow_phase_durations = None
                self.started = time.monotonic()
                self.started_thread_cpu = _thread_cpu_time()
                self.foreground_streak = self.foreground_streak + 1 if priority == "foreground" else 0
                self.waits.append((self.started - entered) * 1000)
            self.connections.append(connection)
        failed = False
        try:
            yield nested
        except BaseException:
            failed = True
            raise
        finally:
            with self.condition:
                self.connections.remove(connection)
                if not nested:
                    duration = (time.monotonic() - self.started) * 1000
                    ended_thread_cpu = _thread_cpu_time()
                    thread_cpu_ms = (max(0.0, (ended_thread_cpu - self.started_thread_cpu) * 1000)
                                     if ended_thread_cpu is not None and self.started_thread_cpu is not None else None)
                    self.holds.append(duration)
                    self.completed += 1
                    self.failures += int(failed)
                    # Startup contains many inferred constructor names. Keep
                    # separate bounded capacity for deliberately named work so
                    # history/search telemetry cannot disappear into overflow.
                    names = self.named_operations if named else self.inferred_operations
                    if operation in self.operations:
                        key = operation
                    elif len(names) < 64:
                        names.add(operation)
                        key = operation
                    else:
                        key = "other"
                    sample = self.operations.setdefault(key, {"count": 0, "errors": 0, "duration": deque(maxlen=256)})
                    sample["count"] += 1
                    sample["errors"] += int(failed)
                    sample["duration"].append(duration)
                    if duration >= SLOW_DATABASE_THRESHOLD_MS:
                        self.record_slow_operation("scope", operation, duration, failed=int(failed),
                                                   scope_id=self.active_scope_id, thread_cpu_ms=thread_cpu_ms)
                    self.owner = self.active_operation = self.started = None
                    self.started_thread_cpu = None
                    self.active_scope_id = None
                    self.active_slow_phase_durations = None
                    self.condition.notify_all()

    def record_slow_operation(self, phase, operation, duration_ms, *, read_only=0,
                              failed=0, error_code=0, transaction_open=0,
                              connection_id=0, scope_id=0, total_changes=0, thread_cpu_ms=None):
        # Most statements must not contend on the metrics lock. Do not accept
        # arbitrary statement text, parameters, error strings or metadata.
        if duration_ms < SLOW_DATABASE_THRESHOLD_MS:
            return
        if phase not in SLOW_DATABASE_PHASES:
            raise ValueError("Unknown database diagnostic phase")
        with self.condition:
            self.slow_operation_count += 1
            record = {
                "sequence": self.slow_operation_count, "phase": phase,
                "operation": operation[:100], "duration_ms": round(duration_ms, 3),
                "observed_monotonic": time.monotonic(), "read_only": int(read_only),
                "failed": int(failed), "error_code": int(error_code),
                "transaction_open": int(transaction_open), "connection_id": int(connection_id),
                "scope_id": int(scope_id), "total_changes": int(total_changes),
            }
            if phase == "scope" and not read_only:
                # Off-CPU wall time includes I/O waits, GIL contention and OS
                # scheduling. This difference does not attribute their causes.
                record["thread_cpu_ms"] = round(thread_cpu_ms, 3) if thread_cpu_ms is not None else None
                record["off_cpu_ms"] = round(max(0.0, duration_ms - thread_cpu_ms), 3) if thread_cpu_ms is not None else None
            self.slow_operations.append(record)
            if scope_id and scope_id == self.active_scope_id and phase != "scope":
                if self.active_slow_phase_durations is None:
                    self.active_slow_phase_durations = {}
                self.active_slow_phase_durations[phase] = self.active_slow_phase_durations.get(phase, 0) + duration_ms
            if phase == "scope" and not read_only:
                # Read traffic may evict recent events, but it must never hide
                # the writer scopes that caused admission deadlines to expire.
                retained = {**record, "slow_phase_durations_ms": {
                    name: round(value, 3) for name, value in (self.active_slow_phase_durations or {}).items()
                } if scope_id == self.active_scope_id else {}}
                entry = (duration_ms, self.slow_operation_count, retained)
                if len(self.slow_writer_scopes) < self.slow_writer_scope_capacity:
                    heapq.heappush(self.slow_writer_scopes, entry)
                elif entry[:2] > self.slow_writer_scopes[0][:2]:
                    heapq.heapreplace(self.slow_writer_scopes, entry)

    def record_request(self, operation, duration_ms, status):
        with self.condition:
            key = operation if operation in self.requests or len(self.requests) < 64 else "other"
            sample = self.requests.setdefault(key, {"count": 0, "errors": 0, "duration": deque(maxlen=2048)})
            sample["count"] += 1
            sample["errors"] += int(status >= 400)
            sample["duration"].append(duration_ms)

    @contextmanager
    def measure_request(self, operation):
        """Measure a fixed request phase, including unsuccessful attempts."""
        started, status = time.monotonic(), 200
        try:
            yield
        except BaseException:
            status = 500
            raise
        finally:
            self.record_request(operation, (time.monotonic() - started) * 1000, status)

    def snapshot(self):
        with self.condition:
            def report(samples):
                return {name: {"count": item["count"], "errors": item["errors"],
                               **_distribution(item["duration"])} for name, item in samples.items()}
            return {"schema_version": 1, "sample_capacity": 2048, "operation_sample_capacity": 256,
                    "thread_switch_interval_ms": sys.getswitchinterval() * 1000,
                    "named_operation_capacity": 64, "inferred_operation_capacity": 64,
                    "request_latency_kind": "server_processing_before_response_body",
                    "completed_scopes": self.completed, "failed_scopes": self.failures,
                    "admission_timeouts": self.timeouts, "queue_depth": len(self.queue),
                    "admission_timeout_records": [dict(record) for record in self.admission_timeout_records],
                    "maximum_queue_depth": self.maximum_queue,
                    "active_operation": self.active_operation,
                    "active_scope_id": self.active_scope_id,
                    "active_duration_ms": round((time.monotonic() - self.started) * 1000, 3) if self.started else 0,
                    "writer_wait": _distribution(self.waits), "writer_hold": _distribution(self.holds),
                    "operations": report(self.operations), "requests": report(self.requests),
                    "slow_operation_threshold_ms": SLOW_DATABASE_THRESHOLD_MS,
                    "slow_operation_capacity": self.slow_operations.maxlen,
                    "slow_operation_count": self.slow_operation_count,
                    "slow_operations": [dict(record) for record in self.slow_operations],
                    "slow_writer_scope_capacity": self.slow_writer_scope_capacity,
                    "scope_off_cpu_definition": "Wall time minus thread CPU; includes I/O, GIL contention and scheduling, without attribution",
                    "slow_writer_scopes": [{**record, "slow_phase_durations_ms": dict(record["slow_phase_durations_ms"])}
                                           for _, _, record in sorted(self.slow_writer_scopes, reverse=True)]}


def performance_snapshot(sessions):
    from .conversation_search import search_metrics
    persistence = sessions.persistence.status()
    failures = persistence.pop("failures", {})
    checkpoint = getattr(sessions, "database_checkpoint", None)
    return {**sessions.store.database_metrics(), **system_performance_snapshot(), "history": sessions.history_persistence.status(),
            "search": search_metrics(sessions.store),
            "checkpoint": checkpoint.snapshot() if checkpoint is not None else None,
            "persistence": {**persistence, "failure_count": len(failures)}}
