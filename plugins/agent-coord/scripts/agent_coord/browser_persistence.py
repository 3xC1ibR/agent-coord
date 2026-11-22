"""Ordered, isolated retries for bookkeeping; never replay provider operations."""
from __future__ import annotations

import errno
import json
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


def storage_full(error):
    if isinstance(error, sqlite3.OperationalError):
        code = getattr(error, "sqlite_errorcode", None)
        if code is not None:
            return code & 255 == 13  # SQLITE_FULL, including extended codes.
        return str(error).lower() == "database or disk is full"
    return isinstance(error, OSError) and error.errno in {errno.ENOSPC, errno.EDQUOT}


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
    Storage exhaustion retains the idempotent write and retries with a longer
    backoff, so restoring capacity recovers without resubmitting provider work.

    Close drains every required write, including writes accepted while it
    drains, such as completion events from stopping providers. Optional work
    keeps its place in rotation but does not extend the drain; unsaved optional
    work (derived search seeding and backfill) does not make the stop unclean.
    """

    def __init__(self, *, retry_delay=0.25, storage_retry_delay=30, context=nullcontext, max_pending_per_key=1024):
        self.condition = threading.Condition()
        self.queues = {}
        self.order = deque()
        self.failures = {}
        self.sequence = 0
        self.in_flight = None
        self.stopped = False
        self.closing = False
        self.drain_report = None
        self.worker = None
        self.retry_delay = retry_delay
        self.storage_retry_delay = storage_retry_delay
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
            busy = isinstance(error, sqlite3.OperationalError) and database_busy(error)
            full = storage_full(error)
            if error is not None:
                if not busy and not full:
                    logging.getLogger(__name__).error("Browser bookkeeping failed for %s", write.key,
                        exc_info=(type(error), error, error.__traceback__))
            with self.condition:
                self.in_flight = None
                if busy or full:
                    if not write.attempts:
                        if full:
                            logging.getLogger(__name__).warning("Storage full; retaining browser bookkeeping for %s until capacity is restored", write.key)
                        else:
                            logging.getLogger(__name__).warning("Database busy; deferring browser bookkeeping for %s", write.key)
                    write.attempts += 1
                    self.retries += 1
                    # Most zero-timeout collisions end with a short foreground
                    # transaction. Retry those promptly, then back off to the
                    # configured ceiling during sustained contention.
                    if full:
                        delay = min(self.storage_retry_delay, 2 ** min(write.attempts - 1, 30))
                    else:
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

    def _required_pending(self):
        """Keys whose sequence can still save required work; failed keys cannot."""
        return [key for key, queue in self.queues.items()
                if key not in self.failures and any(write.required for write in queue)]

    def close(self, timeout=5, *, deadline=None, diagnostics=None, join_timeout=6):
        """Drain required bookkeeping and return a content-free shutdown report.

        The drain has no sequence barrier: work accepted while closing is part
        of the same shutdown. ``deadline`` is a monotonic time shared with the
        caller's own shutdown budget. ``diagnostics`` runs after the drain and
        before stopping, so contention evidence describes the moment of stop.
        """
        started = time.monotonic()
        deadline = started + timeout if deadline is None else deadline
        with self.condition:
            if self.stopped:
                return self.drain_report
            self.closing = True
            retries, sequence = self.retries, self.sequence
            initial = sum(len(queue) for queue in self.queues.values())
            self.condition.notify_all()
            self.condition.wait_for(lambda: not self._required_pending(),
                                    timeout=max(0, deadline - time.monotonic()))
        observed = None
        if diagnostics is not None:
            try:
                observed = diagnostics()
            except Exception:  # noqa: BLE001 -- evidence must not prevent shutdown.
                logging.getLogger(__name__).exception("Bookkeeping drain diagnostics failed")
        with self.condition:
            drained = time.monotonic()
            self.stopped = True
            self.condition.notify_all()
        if self.worker is not None:
            self.worker.join(timeout=join_timeout)
        worker_stopped = self.worker is None or not self.worker.is_alive()
        with self.condition:
            # The worker has exited, so this is the final saved state.
            now = time.monotonic()
            required = self._required_pending()
            pending = [write for queue in self.queues.values() for write in queue]
            in_flight = self.in_flight
            report = {
                "clean": worker_stopped and not required and not self.failures,
                "drain_seconds": round(drained - started, 3),
                "stop_seconds": round(now - drained, 3),
                "deadline_remaining_seconds": round(deadline - drained, 3),
                "initial_writes": initial,
                "submitted_while_closing": self.sequence - sequence,
                "required_writes": sum(write.required for write in pending
                                       if write.key not in self.failures),
                "required_keys": [str(key) for key in required[:10]],
                "optional_writes_unsaved": sum(not write.required for write in pending),
                "failed_keys": [str(key) for key in list(self.failures)[:10]],
                "failure_count": len(self.failures),
                "oldest_required_age_seconds": round(max((now - write.created_at for write in pending
                    if write.required and write.key not in self.failures), default=0), 3),
                "in_flight_key": str(in_flight.key) if in_flight is not None else None,
                "retries_while_closing": self.retries - retries,
                "worker_stopped": worker_stopped,
            }
            if observed is not None:
                report["writer"] = observed
            self.drain_report = report
        if not report["clean"]:
            logging.getLogger(__name__).warning(
                "Closing with unsaved browser bookkeeping; provider history remains authoritative: %s",
                json.dumps(report, sort_keys=True, default=str))
        return report
