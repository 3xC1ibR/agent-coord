"""Anonymous launch milestones. No network I/O runs on a coordination action.

Only a distributed native app initializes this state. CLI/hooks sharing its DB
may enqueue milestones; the native backend alone delivers them with retries.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from pathlib import Path
from urllib.request import Request, urlopen

ENDPOINT = "https://api.ribbonfield.com/v1/launch-events"
DAY = 86400


def initialize(store, *, enabled=True):
    with store._connection() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS launch_install (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                install_id TEXT NOT NULL, launched_at INTEGER NOT NULL,
                enabled INTEGER NOT NULL, download_token TEXT,
                existing_install INTEGER NOT NULL, activated_at INTEGER,
                returned_at INTEGER
            );
            CREATE TABLE IF NOT EXISTS launch_outbox (
                event_id TEXT PRIMARY KEY, payload TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
        """)
        existing = bool(db.execute("SELECT 1 FROM sessions LIMIT 1").fetchone())
        db.execute("INSERT OR IGNORE INTO launch_install VALUES (1, ?, ?, ?, NULL, ?, NULL, NULL)",
                   (str(uuid.uuid4()), int(store.clock()), int(enabled), int(existing)))
        row = db.execute("SELECT * FROM launch_install").fetchone()
        if row["enabled"]:
            _enqueue(db, row, "app_first_launch", row["launched_at"])


def _enqueue(db, install, event, timestamp, **extra):
    event_id = str(uuid.uuid5(uuid.UUID(install["install_id"]), event))
    payload = dict(event_id=event_id, event=event, install_id=install["install_id"],
                   occurred_at=int(timestamp), launched_at=install["launched_at"],
                   existing_install=bool(install["existing_install"]), **extra)
    if install["download_token"]:
        payload["download_token"] = install["download_token"]
    db.execute("INSERT OR IGNORE INTO launch_outbox VALUES (?, ?, ?)",
               (event_id, json.dumps(payload), int(timestamp)))


def settings(store, enabled=None):
    try:
        with store._connection() as db:
            if enabled is not None:
                if not isinstance(enabled, bool):
                    raise ValueError("enabled must be a boolean")
                db.execute("UPDATE launch_install SET enabled = ?", (int(enabled),))
                if not enabled:
                    db.execute("DELETE FROM launch_outbox")
            row = db.execute("SELECT enabled FROM launch_install").fetchone()
            return {"available": row is not None, "enabled": bool(row and row[0])}
    except sqlite3.OperationalError:
        return {"available": False, "enabled": False}


def attribute(store, token):
    if not isinstance(token, str) or str(uuid.UUID(token)) != token:
        raise ValueError("Invalid download token")
    with store._connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM launch_install").fetchone()
        if row is None or not row["enabled"] or row["download_token"]:
            return
        db.execute("UPDATE launch_install SET download_token = ?", (token,))
        row = db.execute("SELECT * FROM launch_install").fetchone()
        _enqueue(db, row, "install_attributed", int(store.clock()))


def record_action(store, criterion):
    """Fail open: measurement must never fail an agent action or a checkpoint."""
    try:
        with store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM launch_install").fetchone()
            if not row or not row["enabled"]:
                return
            now = int(store.clock())
            age = now - row["launched_at"]
            if 0 <= age <= DAY and row["activated_at"] is None:
                db.execute("UPDATE launch_install SET activated_at = ?", (now,))
                _enqueue(db, row, "first_coordinated_action", now, criterion=criterion)
            if (row["activated_at"] is not None and row["returned_at"] is None
                    and 3 * DAY <= age < 5 * DAY
                    and now // DAY > row["activated_at"] // DAY):
                db.execute("UPDATE launch_install SET returned_at = ?", (now,))
                _enqueue(db, row, "d3_active_session", now, criterion=criterion)
    except Exception:
        pass


def registered(store):
    try:
        with store._connection() as db:
            count = db.execute("""SELECT count(*) FROM sessions
                WHERE client IN ('codex', 'claude') AND ended_at IS NULL
                AND last_seen_at >= ?""", (store.clock() - store.stale_after_seconds,)).fetchone()[0]
        if count >= 2:
            record_action(store, "two_sessions")
    except Exception:
        pass


def flush(store, *, send=None):
    def deliver(payload):
        request = Request(ENDPOINT, json.dumps(payload).encode(),
                          {"Content-Type": "application/json"}, method="POST")
        with urlopen(request, timeout=4) as response:
            if response.status not in (200, 202, 204):
                raise OSError("Event was not accepted")
    send = send or deliver
    # Keep the preference check and send serialized against opt-out. This lock
    # is confined to the metrics worker/settings, never coordination actions.
    with _delivery_lock:
        if not settings(store)["enabled"]:
            return
        with store._connection() as db:
            db.execute("DELETE FROM launch_outbox WHERE created_at < ?", (int(store.clock()) - 7 * DAY,))
            rows = db.execute("SELECT * FROM launch_outbox ORDER BY created_at, rowid LIMIT 10").fetchall()
        for row in rows:
            try:
                send(json.loads(row["payload"]))
            except Exception:
                return
            with store._connection() as db:
                db.execute("DELETE FROM launch_outbox WHERE event_id = ?", (row["event_id"],))


_delivery_lock = threading.RLock()


def configure(store, enabled):
    with _delivery_lock:
        return settings(store, enabled)


def start(store, stopping, resources):
    """Exclude development builds, mounted DMGs, and distribution smoke tests."""
    resources = Path(resources).resolve()
    app = resources.parent.parent
    try:
        config = json.loads((resources / "backend.json").read_text())
        if (config.get("database") is not None or app.suffix != ".app"
                or app.parent not in (Path("/Applications"), Path.home() / "Applications")):
            return
        initialize(store)
    except Exception:
        return

    def worker():
        while not stopping.is_set():
            try:
                flush(store)
            except Exception:
                pass
            stopping.wait(60)
    threading.Thread(target=worker, name="launch-metrics", daemon=True).start()
