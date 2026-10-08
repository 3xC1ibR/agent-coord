"""Private Tailscale HTTPS and browser pairing, with no third-party dependencies.

Design reference: pingdotgg/t3code's packages/tailscale and remote-access guide.
See docs/remote-access.md for the pinned reference and security/lifecycle model.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import threading
import time
from http.cookies import CookieError, SimpleCookie
from typing import Any

from .store import CoordinationError

COOKIE_NAME = "__Host-agent-coord-device"
PAIR_SECONDS = 300
DEVICE_SECONDS = 30 * 24 * 60 * 60


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _port(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise CoordinationError("Choose an HTTPS port between 1 and 65535.")
    return value


class Tailscale:
    def executable(self) -> str:
        candidates = [shutil.which("tailscale"), "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
                      str(Path.home() / "Applications/Tailscale.app/Contents/MacOS/Tailscale")]
        for candidate in candidates:
            if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        raise CoordinationError("Install Tailscale on this Mac, open it, and sign in before enabling remote access.")

    def run(self, *args: str, timeout: int = 5) -> str:
        # The macOS app binary otherwise guesses GUI versus CLI mode from
        # shell environment variables that Finder-launched apps do not have.
        environment = dict(os.environ, TAILSCALE_BE_CLI="1")
        try:
            result = subprocess.run([self.executable(), *args], capture_output=True, text=True,
                                    timeout=timeout, check=False, env=environment)
        except subprocess.TimeoutExpired as exc:
            raise CoordinationError("Tailscale timed out. Check that it is running and HTTPS is enabled in your tailnet.") from exc
        except OSError as exc:
            raise CoordinationError("Could not start Tailscale. Open the Tailscale app and retry.") from exc
        if result.returncode:
            # Never expose arbitrary CLI output: it may contain authentication credentials.
            output = (result.stderr + result.stdout).lower()
            if any(word in output for word in ("not logged in", "logged out", "needs login")):
                message = "Sign in to Tailscale on this Mac and retry."
            elif any(word in output for word in ("permission denied", "access denied", "must be root")):
                message = "Tailscale denied access. Check your local Tailscale permissions."
            else:
                message = "Tailscale could not complete the request. Check its connection and enable HTTPS/Serve in your tailnet."
            raise CoordinationError(message)
        return result.stdout

    def json(self, *args: str) -> dict[str, Any]:
        try:
            result = json.loads(self.run(*args))
            if not isinstance(result, dict):
                raise ValueError()
            return result
        except (ValueError, TypeError) as exc:
            raise CoordinationError("Tailscale returned an invalid status response.") from exc

    def hostname(self) -> str:
        status = self.json("status", "--json")
        if status.get("BackendState") != "Running":
            raise CoordinationError("Connect and sign in to Tailscale on this Mac, then retry.")
        node = status.get("Self") or {}
        name = str(node.get("DNSName", "")).rstrip(".").lower() if isinstance(node, dict) else ""
        if not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+ts\.net", name):
            raise CoordinationError("Tailscale needs a MagicDNS name ending in .ts.net for HTTPS access.")
        return name

    def config(self) -> dict[str, Any]:
        return self.json("serve", "status", "--json")

    def enable(self, port: int, target: str) -> None:
        self.run("serve", "--bg", f"--https={port}", target, timeout=15)

    def disable(self, port: int) -> None:
        self.run("serve", f"--https={port}", "off", timeout=15)


def _occupied(config: dict, port: int) -> bool:
    if any(value for key, value in (config.get("AllowFunnel") or {}).items() if str(key).endswith(f":{port}")):
        return True
    if str(port) in (config.get("TCP") or {}):
        return True
    if any(str(key).endswith(f":{port}") for key in (config.get("Web") or {})):
        return True
    return any(_occupied(child, port) for child in (config.get("Foreground") or {}).values())


def _owned(config: dict, authority: str, port: int, target: str) -> bool:
    """Only remove/replace an exact root proxy; never reset another service."""
    if any(value for key, value in (config.get("AllowFunnel") or {}).items()
           if str(key).endswith(f":{port}")):
        return False
    if any(_occupied(child, port) for child in (config.get("Foreground") or {}).values()):
        return False
    webs = {key: value for key, value in (config.get("Web") or {}).items() if key.endswith(f":{port}")}
    return ((config.get("TCP") or {}).get(str(port)) == {"HTTPS": True}
            and webs == {authority: {"Handlers": {"/": {"Proxy": target}}}})


class RemoteAccess:
    """One UI backend owns the route; persisted device grants survive restart."""

    def __init__(self, database_path: Path, *, tailscale: Tailscale | None = None, clock=time.time):
        self.path = Path(str(database_path) + ".remote.json")
        self.lock_path = Path(str(database_path) + ".remote.lock")
        self.tailscale = tailscale or Tailscale()
        self.clock = clock
        self.lock = threading.RLock()
        self.lease = None
        self.data: dict[str, Any] = {}
        self.pairs: dict[str, float] = {}
        self.origin = ""
        self.target = ""
        self.error = ""
        self.closed = False

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
            if not isinstance(data, dict):
                raise ValueError()
            return data
        except FileNotFoundError:
            return {}
        except (ValueError, OSError) as exc:
            raise CoordinationError("Cannot read remote access settings. Check the local settings file.") from exc

    def _save(self) -> None:
        temporary = self.path.with_name(self.path.name + "." + secrets.token_hex(8))
        try:
            with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as file:
                json.dump(self.data, file)
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def _acquire(self) -> None:
        if self.lease is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lease = os.fdopen(os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600), "a+")
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            lease.close()
            raise CoordinationError("Remote access is owned by another Ribbon Field window/server. Manage it there.") from exc
        self.lease = lease
        try:
            self.data = self._read()
        except Exception:
            self._release()
            raise

    def _release(self) -> None:
        if self.lease is not None:
            self.lease.close()
            self.lease = None

    def restore(self, target: str) -> None:
        self.target = target
        try:
            saved = self._read()
            self.data = saved
            if saved.get("enabled"):
                self.enable(saved.get("port", 443))
        except (CoordinationError, OSError) as exc:
            self.error = str(exc)

    def enable(self, port: int = 443) -> dict:
        with self.lock:
            if self.closed:
                raise CoordinationError("The UI server is stopping.")
            _port(port)
            if self.origin and self.data.get("port") != port:
                raise CoordinationError("Disable remote access before changing its HTTPS port.")
            self._acquire()
            attempted = False
            try:
                hostname = self.tailscale.hostname()
                authority = f"{hostname}:{port}"
                config = self.tailscale.config()
                previous = self.data
                replace_owned = (previous.get("hostname") == hostname and previous.get("port") == port
                                 and _owned(config, authority, port, previous.get("target", "")))
                if _occupied(config, port) and not replace_owned:
                    raise CoordinationError("That Tailscale port is already used by another service or Funnel. Choose another HTTPS port.")
                attempted = True
                self.tailscale.enable(port, self.target)
                self.data.update(enabled=True, hostname=hostname, port=port, target=self.target)
                self._save()
                if not _owned(self.tailscale.config(), authority, port, self.target):
                    raise CoordinationError("Tailscale did not create the expected private HTTPS route. Remote access remains locked.")
                self.origin = f"https://{hostname}" + (f":{port}" if port != 443 else "")
                self.error = ""
                return self.status()
            except Exception:
                self.origin = ""
                if attempted:
                    try:
                        if _owned(self.tailscale.config(), authority, port, self.target):
                            self.tailscale.disable(port)
                    except (CoordinationError, OSError):
                        pass
                self._release()
                raise

    def status(self, *, probe: bool = False) -> dict:
        with self.lock:
            result = {"enabled": bool(self.origin), "url": self.origin + "/" if self.origin else None,
                      "configured": bool(self.data.get("enabled")),
                      "port": self.data.get("port", 443), "error": self.error,
                      "devices": [{"id": item["id"], "name": item["name"], "created_at": item["created_at"],
                                   "expires_at": item["expires_at"]}
                                  for item in self.data.get("devices", []) if item["expires_at"] > self.clock()]}
            if probe:
                try:
                    result["hostname"] = self.tailscale.hostname()
                    result["available"] = True
                    if self.origin and not _owned(self.tailscale.config(), f'{self.data["hostname"]}:{self.data["port"]}',
                                                  self.data["port"], self.target):
                        result["error"] = "The Tailscale route has changed. Disable remote access and configure it again."
                except CoordinationError as exc:
                    result.update(available=False, error=str(exc))
            return result

    def pairing(self) -> dict:
        with self.lock:
            if not self.origin:
                raise CoordinationError("Enable Tailscale HTTPS before pairing a device.")
            token = secrets.token_urlsafe(32)
            # One outstanding invitation; never persist or put the secret in a URL query.
            self.pairs = {_digest(token): self.clock() + PAIR_SECONDS}
            return {"url": self.origin + "/pair#pair=" + token, "expires_in": PAIR_SECONDS}

    def redeem(self, token: str, name: str) -> str:
        with self.lock:
            if not isinstance(token, str) or len(token) > 100 or not self.origin:
                raise CoordinationError("Pairing link is invalid or expired. Create a new link on your Mac.")
            expires = self.pairs.pop(_digest(token), 0)
            if expires <= self.clock():
                raise CoordinationError("Pairing link is invalid or expired. Create a new link on your Mac.")
            if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
                raise CoordinationError("Give this device a name of 1–80 characters.")
            devices = [item for item in self.data.get("devices", []) if item["expires_at"] > self.clock()]
            if len(devices) >= 50:
                raise CoordinationError("Revoke an old device before pairing another.")
            credential = secrets.token_urlsafe(32)
            devices.append({"id": secrets.token_hex(12), "name": name.strip(), "hash": _digest(credential),
                            "created_at": self.clock(), "expires_at": self.clock() + DEVICE_SECONDS})
            self.data["devices"] = devices
            self._save()
            return f"{COOKIE_NAME}={credential}; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age={DEVICE_SECONDS}"

    def authenticated(self, cookie: str) -> bool:
        return self.device_id(cookie) is not None

    def device_id(self, cookie: str) -> str | None:
        """Return the grant identity without exposing its credential to callers."""
        with self.lock:
            if not self.origin:
                return None
            try:
                parsed = SimpleCookie(cookie)
                value = parsed[COOKIE_NAME].value if COOKIE_NAME in parsed else ""
            except CookieError:
                return None
            digest = _digest(value)
            return next((item["id"] for item in self.data.get("devices", [])
                         if item["expires_at"] > self.clock() and secrets.compare_digest(item["hash"], digest)), None)

    def active_device_ids(self) -> set[str]:
        with self.lock:
            return {item["id"] for item in self.data.get("devices", []) if item["expires_at"] > self.clock()}

    def revoke(self, device_id: str) -> dict:
        with self.lock:
            if not self.origin:
                raise CoordinationError("Manage devices from the server that owns remote access.")
            self.data["devices"] = [item for item in self.data.get("devices", []) if item["id"] != device_id]
            self._save()
            return self.status()

    def _remove_route(self) -> None:
        port = self.data.get("port", 443)
        config = self.tailscale.config()
        if not _occupied(config, port):
            return
        if not _owned(config, f'{self.data.get("hostname")}:{port}', port, self.data.get("target", "")):
            raise CoordinationError("The Tailscale route was changed by another service; Ribbon Field left it untouched.")
        self.tailscale.disable(port)

    def disable(self) -> dict:
        with self.lock:
            self._acquire()
            self.origin = ""
            self.pairs.clear()
            self.data.update(enabled=False, devices=[])
            self._save()
            try:
                if self.data.get("hostname"):
                    self._remove_route()
                self.error = ""
            except CoordinationError as exc:
                self.error = str(exc)
            finally:
                self._release()
            return self.status()

    def close(self) -> None:
        with self.lock:
            self.closed = True
            self.origin = ""
            self.pairs.clear()
            if self.lease is not None:
                try:
                    self._remove_route()
                except CoordinationError:
                    pass
                finally:
                    self._release()


PAIR_PAGE = b'''<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Pair with Ribbon Field</title>
<meta name="theme-color" content="#213a32">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Ribbon Field">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<link rel="manifest" href="/manifest.webmanifest">
<link rel="apple-touch-icon" sizes="180x180" href="/app-icons/apple-touch-icon.png">
<link rel="icon" type="image/png" sizes="192x192" href="/app-icons/icon-192.png">
<style>body{font:17px system-ui;background:#f5f6f3;color:#213a32;max-width:430px;margin:12vh auto;padding:24px}input,button{box-sizing:border-box;font:inherit;padding:14px;width:100%;margin:12px 0}button{background:#213a32;color:white;border:0;border-radius:8px}p{line-height:1.5}</style>
<h1>Continue on this device</h1><p>Open a fresh pairing link from Remote access on your Mac. Both devices must be connected to your Tailscale network.</p>
<form><div id="pair-link-field" hidden><label for="pair-link">Pairing link</label><input id="pair-link" type="url" placeholder="Paste the link from your Mac" autocomplete="off" autocapitalize="off" spellcheck="false"></div><label for="name">Device name</label><input id="name" value="My iPhone" maxlength="80" required autocomplete="off"><button>Pair this device</button></form>
<p id="status" role="status"></p><script>
const token=new URLSearchParams(location.hash.slice(1)).get('pair');history.replaceState(null,'','/');
const form=document.querySelector('form'),status=document.getElementById('status');
if(!token){document.getElementById('pair-link-field').hidden=false;document.getElementById('pair-link').required=true;status.textContent='Create a pairing link on your Mac, then paste it here to connect this app.'}
form.onsubmit=async event=>{event.preventDefault();const button=form.querySelector('button');button.disabled=true;status.textContent='';
try{let pairingToken=token;
if(!pairingToken){let link;try{link=new URL(document.getElementById('pair-link').value.trim())}catch{throw Error('Paste a complete pairing link from your Mac.')}
if(link.origin!==location.origin||link.pathname!=='/pair'||link.username||link.password)throw Error('Use a pairing link from this Mac and address.');
pairingToken=new URLSearchParams(link.hash.slice(1)).get('pair');if(!pairingToken)throw Error('This link has no pairing code. Create a fresh link on your Mac.')}
const response=await fetch('/api/remote/pair',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:pairingToken,name:document.getElementById('name').value})});
const data=await response.json();if(!response.ok)throw Error(data.error);location.replace('/');}
catch(error){status.textContent=error.message;button.disabled=false}};
</script></html>'''
