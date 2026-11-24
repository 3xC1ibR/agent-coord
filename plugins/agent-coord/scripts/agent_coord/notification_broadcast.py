"""One coordination-notification reader per HTTP server, shared by its SSE clients."""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass

from .message_timeline import message_changes, message_cursor, message_sends


@dataclass(eq=False)
class Subscription:
    completion: int
    messages: int
    sends: int
    pending: tuple | Exception | None = None
    initialized: bool = False


class NotificationBroadcast:
    # Wall-clock snoozes and changes from other processes also need a wake-up.
    RECONCILE_SECONDS = 1.0
    COALESCE_SECONDS = .15

    def __init__(self, store, sessions):
        self.store = store
        self.sessions = sessions
        self.condition = store._writer.commit_condition
        self.clients = set()
        self.thread = None
        self.closed = False

    @contextmanager
    def subscribe(self, completion):
        client = Subscription(completion, message_cursor(self.store), message_sends(self.store)[0])
        with self.condition:
            if not self.closed:
                self.clients.add(client)
                if self.thread is None:
                    self.thread = threading.Thread(target=self._run, name="rf-notifications", daemon=True)
                    self.thread.start()
                self.condition.notify_all()
        try:
            yield client
        finally:
            with self.condition:
                self.clients.discard(client)
                self.condition.notify_all()

    def take(self, client):
        with self.condition:
            pending = client.pending
            if pending is None:
                return None
            client.pending = None
            self.condition.notify_all()
            if isinstance(pending, Exception):
                raise pending
            client.completion, client.messages, client.sends, result = pending
            return result

    def close(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()
            worker = self.thread
        if worker:
            worker.join()

    def _run(self):
        generation = -1
        last_read = 0.0
        while True:
            with self.condition:
                while True:
                    if self.closed or not self.clients:
                        self.thread = None
                        return
                    now = time.monotonic()
                    changed = generation != self.store._writer.commit_generation
                    interval = self.COALESCE_SECONDS if changed else self.RECONCILE_SECONDS
                    available = [c for c in self.clients if c.pending is None]
                    if available and (any(not c.initialized for c in available) or now >= last_read + interval):
                        generation = self.store._writer.commit_generation
                        last_read = now
                        break
                    self.condition.wait(max(.01, last_read + interval - now) if available else self.RECONCILE_SECONDS)
            try:
                self._refresh(available)
            except Exception as error:
                # Each affected handler applies its existing HTTP/SSE error policy.
                # Keep the durable cursors at their last successfully delivered range.
                with self.condition:
                    for client in available:
                        client.pending = error
                        client.initialized = True
                with self.sessions.events_changed:
                    self.sessions.events_changed.notify_all()

    def _refresh(self, clients):
        # All clients use the server's workspace. Group by each durable cursor so
        # a reconnecting client can replay its own range without rewinding others.
        completions = {seq: self.sessions.completions_after(seq) for seq in {c.completion for c in clients}}
        messages = {seq: message_changes(self.store, seq, self.sessions.cwd) for seq in {c.messages for c in clients}}
        sends = {seq: message_sends(self.store, seq, self.sessions.cwd) for seq in {c.sends for c in clients}}
        approvals = self.sessions.approval_notifications()
        snoozes = self.sessions.snooze_notifications()
        with self.condition:
            for client in clients:
                if client not in self.clients:
                    continue
                completion = completions[client.completion]
                message_seq, changes = messages[client.messages]
                send_seq, sent = sends[client.sends]
                client.pending = (completion["seq"], message_seq, send_seq, {
                    "events": changes + sent, "completions": completion["events"], "completionSeq": completion["seq"],
                    "approvals": approvals, "snoozes": snoozes,
                })
                client.initialized = True
        with self.sessions.events_changed:
            self.sessions.events_changed.notify_all()
