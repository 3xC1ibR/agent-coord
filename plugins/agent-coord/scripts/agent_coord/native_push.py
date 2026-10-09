"""Native Apple push from the owning Mac; no relay or CLI dependency required.

Uses the desktop app's optional cryptography package and macOS system curl's
HTTP/2 transport. Provider keys stay on the Mac, never in the iOS app or DB.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time

from .store import CoordinationError
from .web_push import WebPush


def validate_registration(value):
    if not isinstance(value, dict) or set(value) != {"token", "environment"}:
        raise CoordinationError("Native notification registration requires a token and environment.")
    token, environment = value["token"], value["environment"]
    # APNs tokens are variable length. Never assume the currently common 32 bytes.
    if (not isinstance(token, str) or not re.fullmatch(r"[0-9a-fA-F]{32,512}", token)
            or len(token) % 2 or environment not in ("development", "production")):
        raise CoordinationError("Invalid native notification registration.")
    return {"token": token.lower(), "environment": environment}


def _b64(value):
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


class APNsTransport:
    def __init__(self, config_path, *, runner=subprocess.run, clock=time.time):
        self.runner, self.clock = runner, clock
        self.available = False
        self.error = "Set up Apple push notifications on your Mac first."
        self.jwt = None
        self.issued = 0
        try:
            from cryptography.exceptions import UnsupportedAlgorithm
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric import ec
            path = Path(config_path).expanduser()
            if path.stat().st_mode & 0o077:
                raise ValueError("Private configuration permissions required")
            config = json.loads(path.read_text())
            if set(config) != {"team_id", "key_id", "key_path", "topic"}:
                raise ValueError("Unexpected configuration fields")
            if any(not isinstance(config[k], str) or not re.fullmatch(r"[A-Z0-9]{10}", config[k])
                   for k in ("team_id", "key_id")) or config["topic"] != "com.agentcoord.ios":
                raise ValueError("Invalid signing identity")
            key_path = Path(config["key_path"]).expanduser()
            if not key_path.is_absolute() or key_path.stat().st_mode & 0o077:
                raise ValueError("Private absolute key path required")
            try:
                self.key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
            except UnsupportedAlgorithm as error:
                raise ValueError("Unsupported signing key") from error
            if not isinstance(self.key, ec.EllipticCurvePrivateKey) or not isinstance(self.key.curve, ec.SECP256R1):
                raise ValueError("APNs requires a P-256 signing key")
            version = runner(["/usr/bin/curl", "--disable", "--version"], capture_output=True,
                             text=True, timeout=5, check=False)
            if version.returncode or "HTTP2" not in version.stdout:
                raise ValueError("HTTP/2 curl required")
            self.config = config
            self.available, self.error = True, ""
        except (OSError, ValueError, TypeError, KeyError, ImportError, subprocess.SubprocessError):
            # Neither key material, filesystem paths nor provider responses reach UI/logs.
            pass

    def _authorization(self):
        now = int(self.clock())
        if self.jwt is not None and 0 <= now - self.issued < 3000:
            return self.jwt
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec, utils
        header = _b64(json.dumps({"alg": "ES256", "kid": self.config["key_id"]}, separators=(",", ":")).encode())
        claims = _b64(json.dumps({"iss": self.config["team_id"], "iat": now}, separators=(",", ":")).encode())
        signing = header + "." + claims
        der = self.key.sign(signing.encode(), ec.ECDSA(hashes.SHA256()))
        r, s = utils.decode_dss_signature(der)
        self.jwt = signing + "." + _b64(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
        self.issued = now
        return self.jwt

    def send(self, registration, payload, _private, origin, ttl):
        if not self.available:
            return 503, None
        registration = validate_registration(registration)
        host = "api.sandbox.push.apple.com" if registration["environment"] == "development" else "api.push.apple.com"
        body = {"aps": {"alert": {"title": payload["title"], "body": payload["body"]}, "sound": "default"},
                "agentcoord": {"origin": origin, "url": payload["url"]}}
        encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode()) > 4096:
            return 413, None
        headers = ["authorization: bearer " + self._authorization(), "apns-topic: " + self.config["topic"],
                   "apns-push-type: alert", "apns-priority: 10", "content-type: application/json",
                   "apns-expiration: " + str(int(self.clock()) + ttl), "apns-collapse-id: " + payload["tag"]]
        # curl config goes through stdin so device/bearer tokens never appear in process arguments.
        def quote(value):
            return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\r', '\\r') + '"'
        config = "url = " + quote("https://" + host + "/3/device/" + registration["token"]) + "\n"
        config += "\n".join("header = " + quote(header) for header in headers) + "\n"
        config += "data-binary = " + quote(encoded) + "\n"
        try:
            response = self.runner(["/usr/bin/curl", "--disable", "--http2", "--silent", "--show-error",
                                    "--proto", "=https", "--proxy", "", "--noproxy", "*", "--max-time", "10",
                                    "--dump-header", "-", "--write-out", "\n%{http_code}", "--config", "-"],
                                   input=config, capture_output=True, text=True, timeout=12, check=False)
        except (OSError, subprocess.SubprocessError):
            return 503, None
        if response.returncode:
            return 503, None
        try:
            output, code = response.stdout.rsplit("\n", 1)
            status = int(code)
            if not 100 <= status <= 599:
                return 503, None
            normalized = output.replace("\r\n", "\n")
            header_block, body_text = normalized.rsplit("\n\n", 1)
            retry = next((line.split(":", 1)[1].strip() for line in header_block.splitlines()
                          if line.lower().startswith("retry-after:")), None)
            reason = json.loads(body_text).get("reason") if body_text.strip() else None
        except (ValueError, AttributeError):
            return 503, None
        if status == 400 and reason in {"BadDeviceToken", "DeviceTokenNotForTopic"}:
            return 410, None
        if status == 403 and reason == "ExpiredProviderToken":
            self.jwt = None
            return 503, None
        return status, retry


class NativePush(WebPush):
    """Reuse event discovery, revocation checks, deduplication and bounded retries."""
    database_suffix = ".apns.sqlite3"
    validate_subscription = staticmethod(validate_registration)

    def __init__(self, store, sessions, remote, *, transport=None, clock=time.time):
        config = os.environ.get("AGENT_COORD_APNS_CONFIG") or str(store.database_path) + ".apns.json"
        super().__init__(store, sessions, remote, transport=transport or APNsTransport(config, clock=clock), clock=clock)

    def _keys(self, db):
        return None, None

    def status(self, device):
        with self.lock, self.connection() as db:
            row = db.execute("SELECT error,last_sent FROM subscriptions WHERE device=? AND origin=?",
                             (device, self.remote.origin)).fetchone()
            return {"available": self.transport.available, "enabled": row is not None,
                    "error": (row["error"] if row else "") or self.error or getattr(self.transport, "error", ""),
                    "lastSent": row["last_sent"] if row else None}

    def subscribe(self, device, value):
        if not self.transport.available:
            raise CoordinationError("Set up Apple push notifications on your Mac first.")
        registration = validate_registration(value)
        endpoint = registration["environment"] + ":" + hashlib.sha256(registration["token"].encode()).hexdigest()
        with self.remote.lock, self.lock, self.connection() as db:
            if not self.remote.origin or device not in self.remote.active_device_ids():
                raise CoordinationError("Pair this device before enabling notifications.")
            existing = db.execute("SELECT device FROM subscriptions WHERE endpoint=?", (endpoint,)).fetchone()
            if existing and existing[0] != device:
                if existing[0] in self.remote.active_device_ids():
                    raise CoordinationError("Disable this device’s previous notification registration before pairing again.")
                db.execute("DELETE FROM subscriptions WHERE device=?", (existing[0],))
            current = db.execute("SELECT subscription,origin FROM subscriptions WHERE device=?", (device,)).fetchone()
            encoded = json.dumps(registration, sort_keys=True)
            if not current or current[0] != encoded or current[1] != self.remote.origin:
                db.execute("DELETE FROM subscriptions WHERE device=?", (device,))
                db.execute("INSERT INTO subscriptions (device,origin,endpoint,subscription,since,created) VALUES (?,?,?,?,?,?)",
                           (device, self.remote.origin, endpoint, encoded,
                            self.store.threads.completion_cursor(), self.clock()))
        return self.status(device)

    def test(self, device):
        if not self.transport.available:
            raise CoordinationError("Set up Apple push notifications on your Mac first.")
        return super().test(device)
