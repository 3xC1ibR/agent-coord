"""Opt-in Jev setup. Credentials never enter SQLite, URLs, or process arguments."""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys
import threading

from .store import CoordinationError

SERVICE = b"agent-coord.typesafe"
_LOCK = threading.Lock()


def config_path(database_path):
    return Path(os.environ.get("AGENT_COORD_JEV_CONFIG", Path(database_path).parent / "jev.json")).expanduser()


def validate_key(key):
    if not isinstance(key, str):
        raise ValueError("Invalid key")
    key = key.strip()
    if not key or len(key) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in key):
        raise ValueError("Invalid key")
    return key


class Keychain:
    """System Keychain APIs via stdlib ctypes; no credential-bearing CLI calls."""
    def __init__(self):
        if sys.platform != "darwin":
            raise OSError("Keychain unavailable")
        self.api = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
        self.cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        pointer, uint = ctypes.c_void_p, ctypes.c_uint32
        signatures = {
            "SecKeychainFindGenericPassword": [pointer, uint, pointer, uint, pointer,
                                               ctypes.POINTER(uint), ctypes.POINTER(pointer), ctypes.POINTER(pointer)],
            "SecKeychainAddGenericPassword": [pointer, uint, pointer, uint, pointer, uint, pointer, ctypes.POINTER(pointer)],
            "SecKeychainItemModifyAttributesAndData": [pointer, pointer, uint, pointer],
            "SecKeychainItemDelete": [pointer],
            "SecKeychainItemFreeContent": [pointer, pointer],
        }
        for name, arguments in signatures.items():
            function = getattr(self.api, name)
            function.argtypes, function.restype = arguments, ctypes.c_int32
        self.cf.CFRelease.argtypes, self.cf.CFRelease.restype = [pointer], None

    def _find(self, account):
        account = account.encode("utf-8")
        length, data, item = ctypes.c_uint32(), ctypes.c_void_p(), ctypes.c_void_p()
        status = self.api.SecKeychainFindGenericPassword(None, len(SERVICE), SERVICE, len(account), account,
                                                       ctypes.byref(length), ctypes.byref(data), ctypes.byref(item))
        if status == -25300:  # errSecItemNotFound
            return None, None
        if status:
            raise OSError("Keychain unavailable")
        try:
            return ctypes.string_at(data, length.value).decode("ascii"), item
        except BaseException:
            self.cf.CFRelease(item)
            raise
        finally:
            self.api.SecKeychainItemFreeContent(None, data)

    def read(self, account):
        key, item = self._find(account)
        if item:
            self.cf.CFRelease(item)
        if key is None:
            raise OSError("Keychain credential missing")
        return key

    def write(self, account, key):
        _, item = self._find(account)
        encoded = key.encode("ascii")
        try:
            if item:
                status = self.api.SecKeychainItemModifyAttributesAndData(item, None, len(encoded), encoded)
            else:
                account_bytes = account.encode("utf-8")
                status = self.api.SecKeychainAddGenericPassword(None, len(SERVICE), SERVICE,
                    len(account_bytes), account_bytes, len(encoded), encoded, None)
            if status:
                raise OSError("Could not save Keychain credential")
        finally:
            if item:
                self.cf.CFRelease(item)

    def delete(self, account):
        _, item = self._find(account)
        if item:
            try:
                if self.api.SecKeychainItemDelete(item):
                    raise OSError("Could not remove Keychain credential")
            finally:
                self.cf.CFRelease(item)


def read_key(config):
    if not isinstance(config, dict):
        raise TypeError("Missing credential reference")
    account = config.get("keychain_account")
    if account:
        if not isinstance(account, str):
            raise ValueError("Invalid credential reference")
        return validate_key(Keychain().read(account))
    return validate_key(Path(config["api_key_file"]).expanduser().read_text())


def _atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + secrets.token_hex(8))
    try:
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as file:
            file.write(text)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class JevSettings:
    def __init__(self, database_path):
        self.path = config_path(database_path)
        self.key_path = self.path.with_name(self.path.name + ".key")
        self.account = hashlib.sha256(str(self.path.resolve()).encode()).hexdigest()

    def _read(self):
        try:
            value = json.loads(self.path.read_text())
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, UnicodeError):
            raise CoordinationError("Could not read Jev settings. Check the local Jev configuration file.") from None

    def _save(self, value):
        _atomic_write(self.path, json.dumps(value) + "\n")

    def settings(self):
        with _LOCK:
            return self._settings(self._read())

    def _settings(self, value):
        configured = any(isinstance(value.get(name), str) and bool(value[name].strip())
                         for name in ("keychain_account", "api_key_file"))
        return {"enabled": value.get("enabled") is True and configured, "configured": configured,
                "verified": value.get("verified") is True,
                "onboarding_dismissed": value.get("onboarding_dismissed") is True or configured,
                "storage": "keychain" if value.get("keychain_account") else "file" if configured else None}

    def configure(self, body):
        from .attention import ClassificationError, classify
        action = body.get("action")
        if not isinstance(action, str) or action not in {"test_enable", "disable", "remove", "dismiss"} or set(body) - {"action", "api_key"}:
            raise CoordinationError("Choose a Jev settings action.")
        if "api_key" in body and action != "test_enable":
            raise CoordinationError("Only connection setup accepts an API key.")
        with _LOCK:
            value = self._read()
            try:
                if action == "test_enable":
                    try:
                        key = validate_key(body["api_key"]) if "api_key" in body else read_key(value)
                    except (OSError, TypeError, ValueError, KeyError, UnicodeError):
                        raise CoordinationError("Enter a valid TypeSafe API key, or unlock your Keychain and try again.") from None
                    try:
                        # The test contains no conversation history or user content.
                        classify({"original_request": "Say hello.", "recent_messages": [
                            {"role": "user", "text": "Say hello."},
                            {"role": "assistant", "text": "Hello!"}]}, {}, api_key=key)
                    except ClassificationError as exc:
                        message = {"http_401": "TypeSafe rejected this key. Create a new API key and try again.",
                                   "http_403": "This TypeSafe key does not have access to Jev.",
                                   "http_402": "Check billing in your TypeSafe account, then try again.",
                                   "http_429": "TypeSafe is rate limiting requests. Wait a moment and try again."}.get(str(exc),
                                   "Could not connect to TypeSafe. Check your connection and account, then try again.")
                        raise CoordinationError(message) from None
                    if sys.platform == "darwin":
                        Keychain().write(self.account, key)
                        credential = {"keychain_account": self.account}
                    else:
                        _atomic_write(self.key_path, key)
                        credential = {"api_key_file": str(self.key_path.resolve())}
                    value = {"enabled": True, "verified": True, "onboarding_dismissed": True, **credential}
                elif action == "disable":
                    value["enabled"] = False
                elif action == "remove":
                    # Persist opt-out before deleting; no new requests if deletion fails.
                    value["enabled"] = False
                    self._save(value)
                    if value.get("keychain_account"):
                        Keychain().delete(value["keychain_account"])
                    if value.get("api_key_file") == str(self.key_path.resolve()):
                        self.key_path.unlink(missing_ok=True)
                    # User-managed legacy key files are never deleted by the app.
                    value = {"enabled": False, "onboarding_dismissed": True}
                else:
                    value["onboarding_dismissed"] = True
                self._save(value)
            except (OSError, UnicodeError):
                raise CoordinationError("Could not save Jev settings. Check local storage and Keychain access, then try again.") from None
            return self._settings(value)
