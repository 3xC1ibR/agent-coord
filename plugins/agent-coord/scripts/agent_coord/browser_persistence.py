"""Retry browser bookkeeping without replaying provider events or prompts."""
from __future__ import annotations

import logging
import sqlite3
import threading
from collections import deque


def database_busy(error):
    code = getattr(error, "sqlite_errorcode", None)
    if code is not None:
        return code & 255 in {5, 6}  # SQLITE_BUSY / SQLITE_LOCKED, including extended codes.
    # Python 3.10 does not expose sqlite_errorcode.
    return str(error).lower() in {"database is locked", "database table is locked", "database is busy"}


class BrowserPersistence:
    """Keep successful writes synchronous; retry contention in order off-reader.

    Callbacks must only persist bookkeeping, never submit provider work. A busy
    callback remains at the head until it succeeds. Later writes cannot overtake
    it, including when a new turn starts before the previous one is persisted.
    """

    def __init__(self, *, retry_delay=0.25):
        self.condition = threading.Condition()
        self.pending = deque()
        self.draining = False
        self.retry = False
        self.stopped = False
        self.worker = None
        self.retry_delay = retry_delay

    def submit(self, callback):
        with self.condition:
            if self.stopped:
                raise RuntimeError("Browser persistence is closed.")
            self.pending.append(callback)
            if self.draining:
                return
            self.draining = True
        self._drain()

    def _drain(self):
        while True:
            with self.condition:
                if self.stopped:
                    return
                if not self.pending:
                    self.draining = self.retry = False
                    self.condition.notify_all()
                    return
                callback = self.pending[0]
            try:
                callback()
            except sqlite3.OperationalError as error:
                if database_busy(error):
                    with self.condition:
                        if not self.retry:
                            logging.getLogger(__name__).warning("Database busy; deferring browser bookkeeping")
                        self.retry = True
                        if self.worker is None:
                            self.worker = threading.Thread(target=self._run, name="browser-persistence", daemon=True)
                            self.worker.start()
                        self.condition.notify_all()
                    return
                logging.getLogger(__name__).exception("Browser bookkeeping failed; not retrying a non-lock database error")
            except Exception:
                logging.getLogger(__name__).exception("Browser bookkeeping failed")
            with self.condition:
                self.pending.popleft()

    def _run(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.stopped or self.retry)
                if self.stopped:
                    return
                self.condition.wait_for(lambda: self.stopped, timeout=self.retry_delay)
                if self.stopped:
                    return
            self._drain()

    def flush(self, timeout=5):
        with self.condition:
            return self.condition.wait_for(lambda: not self.draining, timeout=timeout)

    def close(self):
        if not self.flush():
            logging.getLogger(__name__).warning("Closing with browser bookkeeping still pending; provider history remains authoritative")
        with self.condition:
            self.stopped = True
            self.condition.notify_all()
        if self.worker is not None:
            self.worker.join(timeout=6)
