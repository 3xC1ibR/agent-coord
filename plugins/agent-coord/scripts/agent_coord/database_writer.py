"""Process-wide admission for finite SQLite write scopes and bounded telemetry.

Readers explicitly use CoordinationStore.reading(). No SQL, parameters or
conversation content is retained in diagnostics. This does not serialize whole
requests: provider/network work must remain outside database scopes.
"""
from __future__ import annotations

from collections import deque
from contextlib import contextmanager
import math
import os
from pathlib import Path
import sqlite3
import sys
import threading
import time
import weakref

_registry = weakref.WeakValueDictionary()
_registry_lock = threading.Lock()


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


class DatabaseWriter:
    def __init__(self):
        self.condition = threading.Condition()
        self.owner = None
        self.connections = []
        self.queue = []
        self.foreground_streak = 0
        self.active_operation = None
        self.started = None
        self.maximum_queue = 0
        self.completed = self.failures = self.timeouts = 0
        self.waits = deque(maxlen=2048)
        self.holds = deque(maxlen=2048)
        self.operations = {}
        self.named_operations = set()
        self.inferred_operations = set()
        self.requests = {}

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
                            error = sqlite3.OperationalError("database is busy: writer admission deadline exceeded")
                            error.sqlite_errorcode, error.sqlite_errorname = sqlite3.SQLITE_BUSY, "SQLITE_BUSY"
                            raise error
                        self.condition.wait(remaining)
                except BaseException:
                    self.queue.remove(entry)
                    self.condition.notify_all()
                    raise
                self.queue.remove(entry)
                self.owner = identity
                self.active_operation = operation
                self.started = time.monotonic()
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
                    self.owner = self.active_operation = self.started = None
                    self.condition.notify_all()

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
                    "maximum_queue_depth": self.maximum_queue,
                    "active_operation": self.active_operation,
                    "active_duration_ms": round((time.monotonic() - self.started) * 1000, 3) if self.started else 0,
                    "writer_wait": _distribution(self.waits), "writer_hold": _distribution(self.holds),
                    "operations": report(self.operations), "requests": report(self.requests)}


def performance_snapshot(sessions):
    from .conversation_search import search_metrics
    persistence = sessions.persistence.status()
    failures = persistence.pop("failures", {})
    return {**sessions.store.database_metrics(), "history": sessions.history_persistence.status(),
            "search": search_metrics(sessions.store),
            "persistence": {**persistence, "failure_count": len(failures)}}
