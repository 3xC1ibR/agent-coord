"""Durable follow-ups dispatched after a browser turn, never as steering."""
from __future__ import annotations

import json
import threading
import time
import uuid

from .store import CoordinationError
from .image_inputs import message_images
from .navigation import window_id


class BrowserMessageQueue:
    def __init__(self, sessions):
        self.sessions = sessions
        self.owner = str(uuid.uuid4())
        self.ready = threading.Event()
        self.lock = threading.Lock()
        self.worker = None
        self.stopped = False
        with sessions.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS browser_message_queue (
                id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, message TEXT NOT NULL,
                state TEXT NOT NULL, error TEXT, owner TEXT NOT NULL, created_at REAL NOT NULL
            )""")
            columns = {row[1] for row in db.execute("PRAGMA table_info(browser_message_queue)")}
            if "images_json" not in columns:
                db.execute("ALTER TABLE browser_message_queue ADD COLUMN images_json TEXT NOT NULL DEFAULT '[]'")
            if "window_id" not in columns:
                db.execute("ALTER TABLE browser_message_queue ADD COLUMN window_id TEXT")

    def list(self, thread_id):
        self.sessions._record(thread_id)
        with self.sessions.store._connection() as db:
            rows = db.execute("SELECT * FROM browser_message_queue WHERE thread_id = ? ORDER BY created_at, rowid", (thread_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["images"] = json.loads(item.pop("images_json"))
            if item.pop("owner") != self.owner:
                item.update(state="paused", error="The server restarted. Review this message before resuming the queue.")
            result.append(item)
        return result

    def _open(self, thread_id):
        record = self.sessions._record(thread_id)
        if record["archived"] or self.sessions.store.threads.get(thread_id)["attention"] == "archived":
            raise CoordinationError("Reopen this thread before changing its queued messages.")
        if self.sessions.closed:
            raise CoordinationError("Browser sessions are closed.")

    def enqueue(self, thread_id, body):
        message, images = message_images(body)
        source_window = window_id(body.get("windowId"))
        with self.sessions._thread_lock(thread_id):
            self._open(thread_id)
            self.sessions.read(thread_id)
            item_id = str(uuid.uuid4())
            with self.sessions.store._connection() as db:
                db.execute("""INSERT INTO browser_message_queue
                           (id, thread_id, message, state, error, owner, created_at, images_json, window_id)
                           VALUES (?, ?, ?, 'queued', NULL, ?, ?, ?, ?)""",
                           (item_id, thread_id, message, self.owner, time.time(), json.dumps(images), source_window))
            self.sessions._publish("browser/changed", {"threadId": thread_id})
            self.wake()
            return {"queued": True, "id": item_id}

    def change(self, thread_id, body):
        with self.sessions._thread_lock(thread_id):
            self._open(thread_id)
            if body.get("action") == "cancel":
                item_id = self.sessions._text(body.get("id"), "Queued message ID", 200)
                with self.sessions.store._connection() as db:
                    db.execute("DELETE FROM browser_message_queue WHERE thread_id = ? AND id = ?", (thread_id, item_id))
            elif body.get("action") == "resume":
                with self.sessions.store._connection() as db:
                    db.execute("UPDATE browser_message_queue SET state = 'queued', error = NULL, owner = ? WHERE thread_id = ?",
                               (self.owner, thread_id))
            else:
                raise CoordinationError("Choose cancel or resume for queued messages.")
            self.sessions._publish("browser/changed", {"threadId": thread_id})
            self.wake()
            return {"queuedMessages": self.list(thread_id)}

    def pause(self, thread_id, reason):
        with self.sessions.store._connection() as db:
            db.execute("UPDATE browser_message_queue SET state = 'paused', error = ? WHERE thread_id = ? AND state = 'queued'",
                       (reason, thread_id))

    def wake(self):
        # RPC must run off the app-server reader thread, which resolves replies.
        with self.lock:
            if self.stopped:
                return
            self.ready.set()
            if self.worker is None:
                self.worker = threading.Thread(target=self._run, name="browser-message-queue", daemon=True)
                self.worker.start()

    def _run(self):
        while True:
            self.ready.wait()
            self.ready.clear()
            if self.stopped or self.sessions.closed:
                return
            with self.sessions.store._connection() as db:
                threads = db.execute("SELECT DISTINCT thread_id FROM browser_message_queue WHERE owner = ? AND state = 'queued'", (self.owner,)).fetchall()
            for row in threads:
                if self.stopped or self.sessions.closed:
                    return
                self._dispatch(row[0])

    def _dispatch(self, thread_id):
        sessions = self.sessions
        with sessions._thread_lock(thread_id):
            items = self.list(thread_id)
            if not items or items[0]["state"] != "queued":
                return
            item = items[0]
            try:
                self._open(thread_id)
                # Re-read before delivery: another browser may have started work.
                if sessions.read(thread_id)["running"]:
                    return
                with sessions.store._connection() as db:
                    claimed = db.execute("UPDATE browser_message_queue SET state = 'sending' WHERE id = ? AND state = 'queued' AND owner = ?",
                                         (item["id"], self.owner)).rowcount
                if not claimed:
                    return  # Stop/disconnect may have paused it during the read.
                sessions.send(thread_id, {"message": item["message"], "images": item["images"],
                                          "windowId": item["window_id"]}, start_only=True)
            except Exception as exc:
                # Keep the message and images for review after transport failures.
                with sessions.store._connection() as db:
                    db.execute("UPDATE browser_message_queue SET state = 'paused', error = ? WHERE id = ?", (str(exc), item["id"]))
                self.pause(thread_id, "An earlier queued message needs review. Resume when ready.")
            else:
                with sessions.store._connection() as db:
                    db.execute("DELETE FROM browser_message_queue WHERE id = ?", (item["id"],))
                # Handles commands and turns that complete before turn/start replies.
                self.ready.set()
            sessions._publish("browser/changed", {"threadId": thread_id})

    def close(self):
        with self.lock:
            self.stopped = True
            self.ready.set()
            worker = self.worker
        if worker is not None:
            worker.join(timeout=5)
