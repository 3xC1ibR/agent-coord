"""Optional Web Push delivery; the coordination CLI remains dependency-free.

Subscriptions and an outbox live beside the coordination DB. Only the backend
that owns remote access sends. No browser connection is needed to discover work.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from urllib.parse import quote, urlsplit

from .store import CoordinationError


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def validate_subscription(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) - {"endpoint", "keys", "expirationTime"}:
        raise CoordinationError("Invalid push subscription.")
    endpoint = value.get("endpoint")
    if not isinstance(endpoint, str) or len(endpoint) > 4096:
        raise CoordinationError("Invalid push endpoint.")
    try:
        url = urlsplit(endpoint)
        host = url.hostname or ""
        allowed = (host.endswith(".push.apple.com") or host == "fcm.googleapis.com"
                   or host == "updates.push.services.mozilla.com")
        if (not allowed or url.scheme != "https" or url.port not in (None, 443)
                or url.username or url.password or url.fragment or not url.path
                or any(c.isspace() or ord(c) < 32 for c in endpoint)):
            raise ValueError()
    except ValueError as exc:
        raise CoordinationError("Use an Apple, Chrome, or Firefox HTTPS push endpoint.") from exc
    keys = value.get("keys")
    if not isinstance(keys, dict) or set(keys) != {"auth", "p256dh"}:
        raise CoordinationError("Invalid push encryption keys.")
    for name, length in (("auth", 16), ("p256dh", 65)):
        key = keys[name]
        try:
            if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}={0,2}", key):
                raise ValueError()
            decoded = base64.b64decode(key + "=" * (-len(key) % 4), altchars=b"-_", validate=True)
            if len(decoded) != length or (name == "p256dh" and decoded[0] != 4):
                raise ValueError()
        except ValueError as exc:
            raise CoordinationError("Invalid push encryption keys.") from exc
    return {"endpoint": endpoint, "keys": dict(keys)}


class PushTransport:
    """Encryption/signing belong to the maintained pywebpush library."""

    def __init__(self):
        try:
            from pywebpush import webpush
            from cryptography.hazmat.primitives.asymmetric import ec
            self.available = True
        except ImportError:
            self.available = False

    def generate_keys(self) -> tuple[str, str]:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        key = ec.generate_private_key(ec.SECP256R1())
        return (_b64(key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption())),
                _b64(key.public_key().public_bytes(serialization.Encoding.X962,
                                                   serialization.PublicFormat.UncompressedPoint)))

    def validate_key(self, subscription):
        from cryptography.hazmat.primitives.asymmetric import ec
        try:
            key = subscription["keys"]["p256dh"]
            ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), base64.urlsafe_b64decode(key + "=" * (-len(key) % 4)))
        except ValueError as exc:
            raise CoordinationError("Invalid push encryption key.") from exc

    def send(self, subscription, payload, private_key, origin, ttl):
        from pywebpush import webpush, WebPushException
        import requests

        class NoRedirectSession(requests.Session):
            def post(self, url, **kwargs):
                kwargs["allow_redirects"] = False
                return super().post(url, **kwargs)

        # Never follow a push endpoint redirect onto a private service, and do
        # not inherit proxy/netrc credentials from the laptop environment.
        with NoRedirectSession() as session:
            session.trust_env = False
            try:
                response = webpush(subscription, data=json.dumps(payload), vapid_private_key=private_key,
                                   vapid_claims={"sub": "https://" + urlsplit(origin).hostname}, ttl=ttl, timeout=10,
                                   headers={"Urgency": "normal"}, requests_session=session)
                return response.status_code, response.headers.get("Retry-After")
            except WebPushException as exc:
                response = exc.response
                return (response.status_code, response.headers.get("Retry-After")) if response is not None else (503, None)
            except requests.RequestException:
                return 503, None


class WebPush:
    def __init__(self, store, sessions, remote, *, transport=None, clock=time.time):
        self.store, self.sessions, self.remote = store, sessions, remote
        self.transport = transport or PushTransport()
        self.clock = clock
        self.path = Path(str(store.database_path) + ".push.sqlite3")
        self.lock = threading.RLock()
        self.stopping = threading.Event()
        self.worker = None
        self.error = ""
        # Create privately before SQLite opens it (including the VAPID key).
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS subscriptions (
                    device TEXT PRIMARY KEY, origin TEXT NOT NULL, endpoint TEXT NOT NULL UNIQUE,
                    subscription TEXT NOT NULL, since INTEGER NOT NULL, created REAL NOT NULL,
                    error TEXT NOT NULL DEFAULT '', last_sent REAL);
                CREATE TABLE IF NOT EXISTS outbox (
                    device TEXT NOT NULL REFERENCES subscriptions(device) ON DELETE CASCADE,
                    event TEXT NOT NULL, payload TEXT NOT NULL, expires REAL NOT NULL,
                    next_attempt REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    done INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (device, event));
            """)
            db.execute("INSERT OR IGNORE INTO settings VALUES ('cursor', ?)", (str(store.threads.completion_cursor()),))

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def _keys(self, db):
        row = db.execute("SELECT value FROM settings WHERE name='keys'").fetchone()
        if row:
            return json.loads(row[0])
        keys = self.transport.generate_keys()
        db.execute("INSERT INTO settings VALUES ('keys', ?)", (json.dumps(keys),))
        return keys

    def status(self, device):
        with self.lock, self.connection() as db:
            row = db.execute("SELECT error, last_sent FROM subscriptions WHERE device=? AND origin=?",
                             (device, self.remote.origin)).fetchone()
            public = self._keys(db)[1] if self.transport.available else None
            return {"available": self.transport.available, "enabled": row is not None, "publicKey": public,
                    "error": (row["error"] if row else "") or self.error or
                             ("" if self.transport.available else "This server needs the Web Push package. Use the updated Mac app."),
                    "lastSent": row["last_sent"] if row else None}

    def subscribe(self, device, value):
        if not self.transport.available:
            raise CoordinationError("Web Push is unavailable in this server. Use the updated Mac app.")
        subscription = validate_subscription(value)
        self.transport.validate_key(subscription)
        with self.remote.lock, self.lock, self.connection() as db:
            if not self.remote.origin or device not in self.remote.active_device_ids():
                raise CoordinationError("Pair this device before enabling notifications.")
            existing = db.execute("SELECT device FROM subscriptions WHERE endpoint=?", (subscription["endpoint"],)).fetchone()
            if existing and existing[0] != device:
                if existing[0] in self.remote.active_device_ids():
                    raise CoordinationError("This notification subscription belongs to another paired device.")
                db.execute("DELETE FROM subscriptions WHERE device=?", (existing[0],))
            current = db.execute("SELECT subscription, origin FROM subscriptions WHERE device=?", (device,)).fetchone()
            encoded = json.dumps(subscription, sort_keys=True)
            # Reconnecting must not reset the cursor or replay a pending approval.
            if not current or current[0] != encoded or current[1] != self.remote.origin:
                db.execute("DELETE FROM subscriptions WHERE device=?", (device,))
                db.execute("INSERT INTO subscriptions (device,origin,endpoint,subscription,since,created) VALUES (?,?,?,?,?,?)",
                           (device, self.remote.origin, subscription["endpoint"], encoded,
                            self.store.threads.completion_cursor(), self.clock()))
            self._keys(db)
        return self.status(device)

    def unsubscribe(self, device):
        with self.lock, self.connection() as db:
            db.execute("DELETE FROM subscriptions WHERE device=?", (device,))
        return self.status(device)

    def test(self, device):
        with self.lock, self.connection() as db:
            row = db.execute("SELECT 1 FROM subscriptions WHERE device=?", (device,)).fetchone()
            if not row:
                raise CoordinationError("Enable notifications on this device first.")
            # A single pending test per device also bounds repeated button clicks.
            db.execute("DELETE FROM outbox WHERE device=? AND event='test' AND done=1", (device,))
            self._enqueue(db, device, "test", "test", "", self.clock() + 300)
        return {"queued": True}

    def _enqueue(self, db, device, event, kind, thread, expires):
        label = {"completed": "Turn finished", "failed": "Turn failed", "approval": "Approval needed",
                 "test": "Phone notifications are working"}[kind]
        payload = {"title": "Ribbon Field", "body": label,
                   "tag": "agent-coord-" + hashlib.sha256(event.encode()).hexdigest()[:24],
                   "url": "/#" + quote(thread, safe="") if thread else "/"}
        db.execute("INSERT OR IGNORE INTO outbox (device,event,payload,expires,next_attempt) VALUES (?,?,?,?,?)",
                   (device, event, json.dumps(payload), expires, self.clock()))

    def tick(self):
        # RemoteAccess owns a cross-process lease. Only that server can have an
        # active origin. Keep this lock order identical to subscribe and send.
        with self.remote.lock:
            if not self.remote.origin:
                return
            devices, origin = self.remote.active_device_ids(), self.remote.origin
        if not self.transport.available:
            return
        approvals = self.sessions.approval_notifications(include_claimed=True)
        active_approvals = {"approval:" + item["request_key"] for item in approvals}
        now = self.clock()
        with self.lock, self.connection() as db:
            for row in db.execute("SELECT device,origin FROM subscriptions").fetchall():
                if row["device"] not in devices or row["origin"] != origin:
                    db.execute("DELETE FROM subscriptions WHERE device=?", (row["device"],))
            cursor = int(db.execute("SELECT value FROM settings WHERE name='cursor'").fetchone()[0])
            batch = self.sessions.completions_after(cursor)
            subscribers = db.execute("SELECT device,since FROM subscriptions").fetchall()
            for event in batch["events"]:
                if event["status"] not in {"completed", "failed"} or event["completed_at"] + 3600 <= now:
                    continue
                for sub in subscribers:
                    if event["id"] > sub["since"]:
                        self._enqueue(db, sub["device"], "turn:" + str(event["id"]), event["status"],
                                      event["thread_id"], event["completed_at"] + 3600)
            db.execute("UPDATE settings SET value=? WHERE name='cursor'", (str(batch["seq"]),))
            for event in approvals:
                for sub in subscribers:
                    self._enqueue(db, sub["device"], "approval:" + event["request_key"], "approval",
                                  event["thread_id"], now + 900)
            for row in db.execute("SELECT DISTINCT event FROM outbox WHERE event LIKE 'approval:%'").fetchall():
                if row[0] not in active_approvals:
                    db.execute("DELETE FROM outbox WHERE event=?", (row[0],))
            # Keep approval tombstones while the request exists; never replay an
            # unanswered approval every time its delivery TTL expires.
            db.execute("DELETE FROM outbox WHERE expires < ? AND event NOT LIKE 'approval:%'", (now - 86400,))
            due = db.execute("SELECT device,event FROM outbox WHERE done=0 AND next_attempt<=? AND expires>? ORDER BY next_attempt LIMIT 20",
                             (now, now)).fetchall()
        for row in due:
            if self.stopping.is_set():
                break
            self._send(row["device"], row["event"])

    def _send(self, device, event):
        # A completion may already be queued for retry when the user snoozes.
        # Recheck at delivery as well as filtering newly discovered events.
        if event.startswith("turn:") and not self.sessions.completion_notification_allowed(int(event[5:])):
            with self.lock, self.connection() as db:
                db.execute("DELETE FROM outbox WHERE device=? AND event=?", (device, event))
            return
        if event.startswith("approval:") and not any("approval:" + item["request_key"] == event
                                                     for item in self.sessions.approval_notifications(include_claimed=True)):
            with self.lock, self.connection() as db:
                db.execute("DELETE FROM outbox WHERE device=? AND event=?", (device, event))
            return
        # Revocation and unsubscribe wait for an already-started send, and no
        # send can start after either operation returns. Network time is bounded.
        with self.remote.lock, self.lock, self.connection() as db:
            if not self.remote.origin or device not in self.remote.active_device_ids():
                db.execute("DELETE FROM subscriptions WHERE device=?", (device,))
                return
            row = db.execute("SELECT o.*,s.subscription,s.origin FROM outbox o JOIN subscriptions s USING(device) WHERE o.device=? AND o.event=?",
                             (device, event)).fetchone()
            if not row or row["done"] or row["expires"] <= self.clock() or row["origin"] != self.remote.origin:
                return
            private = self._keys(db)[0]
            try:
                subscription = validate_subscription(json.loads(row["subscription"]))
                status, retry_after = self.transport.send(subscription, json.loads(row["payload"]), private,
                                                          row["origin"], max(1, int(row["expires"] - self.clock())))
            except Exception:
                # Endpoint URLs and key material must never reach UI or logs.
                status, retry_after = 503, None
            if status in (404, 410):
                db.execute("DELETE FROM subscriptions WHERE device=?", (device,))
                return
            success = 200 <= status < 300
            attempts = row["attempts"] + 1
            transient = status == 429 or status >= 500
            delay = min(900, 5 * 2 ** min(attempts, 8))
            try:
                delay = max(delay, min(3600, int(retry_after)))
            except (TypeError, ValueError):
                pass
            db.execute("UPDATE outbox SET done=?,attempts=?,next_attempt=? WHERE device=? AND event=?",
                       (int(success or not transient or attempts >= 8), attempts, self.clock() + delay, device, event))
            error = "" if success else ("Delivery delayed; retrying." if transient and attempts < 8 else
                                          "Push delivery failed. Turn phone notifications off and on to retry.")
            db.execute("UPDATE subscriptions SET error=?,last_sent=CASE WHEN ? THEN ? ELSE last_sent END WHERE device=?",
                       (error, success, self.clock(), device))

    def start(self):
        if self.worker is not None or not self.transport.available:
            return
        def run():
            while not self.stopping.wait(2):
                try:
                    self.tick()
                    self.error = ""
                except Exception:
                    self.error = "Phone notification delivery is temporarily unavailable."
        self.worker = threading.Thread(target=run, name="web-push", daemon=True)
        self.worker.start()

    def close(self):
        self.stopping.set()
        if self.worker is not None:
            self.worker.join(timeout=15)
