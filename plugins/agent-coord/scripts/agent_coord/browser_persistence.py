"""Ordered, isolated retries for bookkeeping; never replay provider operations."""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections import deque
from collections.abc import Iterator
from contextlib import nullcontext
from dataclasses import dataclass


ALL_KEYS = object()


def database_busy(error):
    code = getattr(error, "sqlite_errorcode", None)
    if code is not None:
        return code & 255 in {5, 6}
    return str(error).lower() in {"database is locked", "database table is locked", "database is busy"}


@dataclass
class _Write:
    callback: object
    key: object
    sequence: int
    coalesce: object
    required: bool
    created_at: float
    due_at: float = 0
    attempts: int = 0
    started: bool = False
    steps: Iterator | None = None


class BrowserPersistence:
    """Run bookkeeping off readers, in order per conversation.

    Callbacks must be idempotent persistence only, never provider operations.
    A busy key retains its place in its own sequence but yields to other keys.
    A callback may return an iterator of finite steps. One step runs per key
    rotation, with a separate context each time. Steps must yield outside every
    lock, database connection and thread-local context. A busy step restarts its
    idempotent callback factory; it never resumes an iterator after an exception.
    Required permanent failures fail that key's barriers and stop its sequence.
    Optional telemetry failures are logged and do not prevent later lifecycle work.
    """

    def __init__(self, *, retry_delay=0.25, context=nullcontext, max_pending_per_key=1024):
        self.condition = threading.Condition()
        self.queues = {}
        self.order = deque()
        self.failures = {}
        self.sequence = 0
        self.in_flight = None
        self.stopped = False
        self.worker = None
        self.retry_delay = retry_delay
        self.context = context
        self.max_pending_per_key = max_pending_per_key
        self.retries = 0
        self.optional_failures = 0

    @property
    def pending(self):
        with self.condition:
            return [write for queue in self.queues.values() for write in queue]

    @property
    def draining(self):
        with self.condition:
            return bool(self.queues)

    def submit(self, callback, *, key=None, coalesce=None, required=True):
        with self.condition:
            if self.stopped:
                raise RuntimeError("Browser persistence is closed.")
            self.sequence += 1
            sequence = self.sequence
            if key in self.failures:
                return sequence  # Retain the failure; never claim later work was saved.
            queue = self.queues.get(key)
            if queue is None:
                queue = self.queues[key] = deque()
                self.order.append(key)
            if coalesce is not None:
                for write in reversed(queue):
                    if write.required and write.coalesce is None:
                        # A replacement snapshot belongs after the lifecycle
                        # transition that preceded its receipt, never before it.
                        break
                    if not write.started and write.coalesce == coalesce:
                        write.callback = callback
                        write.required |= required
                        self.condition.notify_all()
                        return sequence
            if len(queue) >= self.max_pending_per_key:
                self.failures[key] = (sequence, "Persistence backlog exceeded its bounded capacity.")
                logging.getLogger(__name__).error("Browser bookkeeping backlog exceeded capacity for %s", key)
                self.condition.notify_all()
                return sequence
            queue.append(_Write(callback, key, sequence, coalesce, required, time.monotonic()))
            if self.worker is None:
                self.worker = threading.Thread(target=self._run, name="browser-persistence", daemon=True)
                self.worker.start()
            self.condition.notify_all()
            return sequence

    def _next(self):
        now = time.monotonic()
        delay = None
        for _ in range(len(self.order)):
            key = self.order.popleft()
            self.order.append(key)
            if key in self.failures:
                continue
            write = self.queues[key][0]
            if write.due_at <= now:
                return write, 0
            remaining = write.due_at - now
            delay = remaining if delay is None else min(delay, remaining)
        return None, delay

    @staticmethod
    def _step(write):
        if write.steps is None:
            result = write.callback()
            if not isinstance(result, Iterator):
                return True
            write.steps = result
        try:
            next(write.steps)
        except StopIteration:
            return True
        return False

    @staticmethod
    def _close_steps(write):
        steps, write.steps = write.steps, None
        close = getattr(steps, "close", None)
        if close is not None:
            close()

    def _run(self):
        try:
            self._run_steps()
        finally:
            # The worker owns these iterators. Finalizers run outside the
            # condition so they can release resources or report measurements.
            with self.condition:
                pending = [write for queue in self.queues.values() for write in queue if write.steps is not None]
            for write in pending:
                try:
                    self._close_steps(write)
                except Exception:
                    logging.getLogger(__name__).exception("Browser bookkeeping cleanup failed for %s", write.key)

    def _run_steps(self):
        while True:
            with self.condition:
                while not self.stopped:
                    write, delay = self._next()
                    if write is not None:
                        write.started = True
                        self.in_flight = write
                        break
                    self.condition.wait(timeout=delay)
                if self.stopped:
                    return
            error = None
            complete = False
            try:
                with self.context():
                    complete = self._step(write)
            except Exception as exc:
                error = exc
            if complete or error is not None:
                try:
                    self._close_steps(write)
                except Exception as exc:
                    error = exc
            if error is not None:
                if not isinstance(error, sqlite3.OperationalError) or not database_busy(error):
                    logging.getLogger(__name__).error("Browser bookkeeping failed for %s", write.key,
                        exc_info=(type(error), error, error.__traceback__))
            with self.condition:
                self.in_flight = None
                if isinstance(error, sqlite3.OperationalError) and database_busy(error):
                    if not write.attempts:
                        logging.getLogger(__name__).warning("Database busy; deferring browser bookkeeping for %s", write.key)
                    write.attempts += 1
                    self.retries += 1
                    # Most zero-timeout collisions end with a short foreground
                    # transaction. Retry those promptly, then back off to the
                    # configured ceiling during sustained contention.
                    delay = min(self.retry_delay, .01 * 2 ** min(write.attempts - 1, 30))
                    write.due_at = time.monotonic() + delay
                elif complete or error is not None:
                    queue = self.queues[write.key]
                    queue.popleft()
                    if error is not None and write.required:
                        self.failures[write.key] = (write.sequence, type(error).__name__ + ": " + str(error))
                    elif error is not None:
                        self.optional_failures += 1
                    if not queue:
                        del self.queues[write.key]
                        self.order.remove(write.key)
                self.condition.notify_all()

    def failure(self, key=None):
        with self.condition:
            failure = self.failures.get(key)
            return failure[1] if failure else None

    def flush(self, timeout=5, *, key=ALL_KEYS):
        """Wait for the sequence accepted before this call, not future events."""
        with self.condition:
            barrier = self.sequence

            def failed():
                return any(sequence <= barrier for identity, (sequence, _) in self.failures.items()
                           if key is ALL_KEYS or identity == key)

            def pending():
                return any(write.sequence <= barrier for identity, queue in self.queues.items()
                           if key is ALL_KEYS or identity == key for write in queue)

            self.condition.wait_for(lambda: failed() or not pending() or self.stopped, timeout=timeout)
            return not failed() and not pending()

    def status(self, *, key=ALL_KEYS):
        with self.condition:
            pending = [write for identity, queue in self.queues.items()
                       if key is ALL_KEYS or identity == key for write in queue]
            failures = {str(identity): error for identity, (_, error) in self.failures.items()
                        if key is ALL_KEYS or identity == key}
            return {"pending": len(pending), "failures": failures, "retries": self.retries,
                    "optional_failure_count": self.optional_failures,
                    "oldest_age_seconds": max((time.monotonic() - write.created_at for write in pending), default=0),
                    "draining": bool(pending), "stopped": self.stopped}

    def close(self):
        if not self.flush():
            logging.getLogger(__name__).warning("Closing with unsaved browser bookkeeping; provider history remains authoritative")
        with self.condition:
            self.stopped = True
            self.condition.notify_all()
        if self.worker is not None:
            self.worker.join(timeout=6)
