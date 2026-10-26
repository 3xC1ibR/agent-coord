"""Read account quotas without starting model turns or retaining credentials.

Quota snapshots are account-wide and deliberately separate from thread_usage.
Private endpoint shapes were researched with Headroom (see docs/provider-usage.md).
Only normalized display fields leave this module; provider errors and tokens do not.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from .antigravity_setup import provider_directory
from .store import CoordinationError

PROVIDERS = {"codex": "Codex", "claude": "Claude", "antigravity": "Antigravity",
             "kimi": "Kimi", "muse": "Muse"}
REFRESH_SECONDS = 120
MAX_BYTES = 2 * 1024 * 1024


class QuotaError(Exception):
    def __init__(self, message, *, state="error", retry_after=60):
        super().__init__(message)
        self.state, self.retry_after = state, retry_after


def _number(value, maximum=None):
    if type(value) not in (float, int) or not math.isfinite(value) or value < 0:
        return None
    return value if maximum is None or value <= maximum else None


def _text(value, fallback=""):
    return value[:160] if isinstance(value, str) and value.strip() else fallback


def _timestamp(value):
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.timestamp() if parsed.tzinfo else None
        except (ValueError, OverflowError):
            return None
    return _number(value, 253402300799)


def _window(key, label, used, resets):
    return {"id": key, "label": label, "used_percent": _number(used),
            "resets_at": _timestamp(resets)}


def _duration(minutes, fallback):
    minutes = _number(minutes)
    if not minutes:
        return fallback
    if minutes == 10080:
        return "Weekly"
    if minutes % 1440 == 0:
        return f"{minutes / 1440:g}-day"
    if minutes % 60 == 0:
        return f"{minutes / 60:g}-hour"
    return f"{minutes:g}-minute"


def codex_windows(payload):
    if not isinstance(payload, dict):
        raise QuotaError("Codex returned an unrecognized quota response.")
    buckets = payload.get("rateLimitsByLimitId")
    if not isinstance(buckets, dict) or not buckets:
        single = payload.get("rateLimits")
        buckets = {"codex": single} if isinstance(single, dict) else {}
    windows = []
    for key, bucket in list(buckets.items())[:30]:
        if not isinstance(bucket, dict):
            continue
        name = _text(bucket.get("limitName"), _text(key, "Codex"))
        for kind in ("primary", "secondary"):
            value = bucket.get(kind)
            if isinstance(value, dict):
                label = _duration(value.get("windowDurationMins"), kind.title())
                if len(buckets) > 1 or key != "codex":
                    label = f"{name} · {label}"
                windows.append(_window(f"{key}:{kind}", label,
                                       value.get("usedPercent"), value.get("resetsAt")))
    return windows


def claude_windows(payload):
    if not isinstance(payload, dict):
        raise QuotaError("Claude returned an unrecognized quota response.")
    windows = []
    for key, label in (("five_hour", "5-hour"), ("seven_day", "Weekly")):
        value = payload.get(key)
        if isinstance(value, dict):
            windows.append(_window(key, label, value.get("utilization"), value.get("resets_at")))
    # Structured scoped limits include provider-supplied model names. Do not
    # infer models from experimental bucket keys or duplicate legacy buckets.
    scoped = payload.get("limits")
    if isinstance(scoped, list):
        for item in scoped[:30]:
            if not isinstance(item, dict) or item.get("kind") != "weekly_scoped":
                continue
            scope = item.get("scope")
            model = scope.get("model") if isinstance(scope, dict) else None
            name = _text(model.get("display_name")) if isinstance(model, dict) else ""
            if name:
                windows.append(_window("weekly:" + name, f"{name} · Weekly",
                                       item.get("percent"), item.get("resets_at")))
    if not any(w["id"].startswith("weekly:") for w in windows):
        for key, name in (("seven_day_sonnet", "Sonnet"), ("seven_day_opus", "Opus")):
            item = payload.get(key)
            if isinstance(item, dict):
                windows.append(_window(key, f"{name} · Weekly", item.get("utilization"), item.get("resets_at")))
    return windows


def antigravity_windows(payload):
    if not isinstance(payload, dict):
        raise QuotaError("Antigravity returned an unrecognized quota response.")
    response = payload.get("response")
    groups = payload.get("groups", response.get("groups") if isinstance(response, dict) else None)
    if not isinstance(groups, list):
        raise QuotaError("Antigravity returned an unrecognized quota response.")
    labels = {"gemini-5h": "Gemini · 5-hour", "gemini-weekly": "Gemini · Weekly",
              "3p-5h": "Claude and GPT · 5-hour", "3p-weekly": "Claude and GPT · Weekly"}
    windows, seen = [], set()
    for group in groups[:30]:
        if not isinstance(group, dict) or not isinstance(group.get("buckets"), list):
            continue
        for bucket in group["buckets"][:30]:
            if not isinstance(bucket, dict):
                continue
            key = _text(bucket.get("bucketId"))
            if not key or key in seen:
                continue
            seen.add(key)
            remaining = _number(bucket.get("remainingFraction"), 1)
            label = labels.get(key, _text(bucket.get("displayName"), key))
            windows.append(_window(key, label, None if remaining is None else round((1 - remaining) * 100, 4),
                                   bucket.get("resetTime")))
    return windows


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward provider credentials to another destination.


def _http_json(url, *, headers=None, body=None, token_request=False):
    request = urllib.request.Request(url, headers={"Accept": "application/json", **(headers or {})},
        data=json.dumps(body).encode() if body is not None else None)
    if body is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=12) as response:
            raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("oversized")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("not an object")
        return value
    except urllib.error.HTTPError as exc:
        code = exc.code
        retry = exc.headers.get("Retry-After", "") if exc.headers else ""
        exc.close()
        if code == 401 or (token_request and code == 400):
            raise QuotaError("Sign-in needs refreshing. Open the provider and sign in, then refresh usage.",
                             state="sign_in_required") from None
        if code == 403:
            raise QuotaError("This sign-in cannot read account limits.", state="unavailable") from None
        if code == 429:
            delay = min(3600, max(60, int(retry))) if retry.isdigit() else 120
            raise QuotaError("The provider asked us to wait before refreshing.", retry_after=delay) from None
        raise QuotaError("The provider could not return usage. Try again shortly.") from None
    except (OSError, ValueError, urllib.error.URLError):
        raise QuotaError("Could not reach the provider or read its usage response.") from None


def _read_json(path):
    try:
        with path.open("rb") as source:
            raw = source.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("oversized")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("not an object")
        return value
    except (OSError, ValueError):
        raise QuotaError("Provider sign-in could not be read. Open the provider and sign in again.",
                         state="sign_in_required") from None


def _claude_credential():
    # ClaudeConnection removes inherited API keys to use the user's CLI login.
    # Match that behavior, while preserving explicit alternate provider routing.
    if os.environ.get("ANTHROPIC_BASE_URL") or any(os.environ.get(key) == "1" for key in
            ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")):
        raise QuotaError("Subscription quotas are unavailable for the configured API connection.", state="unsupported")
    supplied = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if supplied:
        return {"accessToken": supplied}
    config = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))).expanduser()
    path = config / ".credentials.json"
    if path.is_file():
        data = _read_json(path)
    elif sys.platform == "darwin" and not os.environ.get("CLAUDE_CONFIG_DIR"):
        try:
            result = subprocess.run(["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                                    capture_output=True, text=True, timeout=8, check=False)
            data = json.loads(result.stdout) if result.returncode == 0 else {}
        except (OSError, ValueError, subprocess.TimeoutExpired):
            data = {}
    else:
        data = {}
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if not isinstance(oauth, dict) or not isinstance(oauth.get("accessToken"), str) or not oauth["accessToken"]:
        raise QuotaError("Sign in to Claude Code on this Mac to see its limits.", state="sign_in_required")
    return oauth


class ProviderLimits:
    """Per-runtime cache: no background polling, no credentials in stored snapshots.

    Independent provider locks coalesce concurrent tabs. Refresh has a minimum
    interval even for manual requests; failures retain the last good snapshot.
    """
    def __init__(self, sessions, *, clock=time.time, collectors=None):
        self.sessions, self.clock = sessions, clock
        self.lock = threading.RLock()
        self.locks = {key: threading.Lock() for key in PROVIDERS}
        self.identities = {}
        self.google_access = None
        self.cache = {key: {"provider": key, "name": name, "state": "not_loaded", "windows": [],
                           "plan": None, "account": None, "observed_at": None, "checked_at": None,
                           "next_refresh_at": 0, "message": "Refresh to load account limits."}
                      for key, name in PROVIDERS.items()}
        self.collectors = collectors if collectors is not None else {
            "codex": self._codex, "claude": self._claude, "antigravity": self._antigravity}
        for key in PROVIDERS.keys() - self.collectors.keys():
            self.cache[key].update(state="unsupported", message="Account limits are not available from this integration yet.")

    def snapshot(self):
        with self.lock:
            rows = copy.deepcopy(list(self.cache.values()))
        now = self.clock()
        for row in rows:
            row["stale"] = bool(row["observed_at"] is not None and
                                (row["state"] != "ready" or now - row["observed_at"] >= REFRESH_SECONDS))
        return {"providers": rows, "refresh_seconds": REFRESH_SECONDS}

    def _identity(self, provider, value):
        digest = hashlib.sha256(value.encode()).hexdigest()
        with self.lock:
            if self.identities.get(provider) != digest:
                self.cache[provider].update(windows=[], observed_at=None, plan=None, account=None)
                self.identities[provider] = digest
        return digest

    def refresh(self, provider):
        if not isinstance(provider, str) or provider not in PROVIDERS:
            raise CoordinationError("Unknown usage provider.")
        with self.locks[provider]:
            with self.lock:
                current = self.cache[provider]
                if provider not in self.collectors or self.clock() < current["next_refresh_at"]:
                    return self.snapshot()
            try:
                result = self.collectors[provider]()
                windows = result.get("windows", [])
                if not windows:
                    raise QuotaError("This account did not report any quota windows.", state="unavailable")
                now = self.clock()
                with self.lock:
                    current.update(result, state="ready", message="", observed_at=now,
                                   checked_at=now, next_refresh_at=now + REFRESH_SECONDS)
            except Exception as exc:
                # Native transports and credential helpers may include secrets
                # in errors. Only our fixed messages may reach the UI or logs.
                error = exc if isinstance(exc, QuotaError) else QuotaError("Could not read provider limits. Try again shortly.")
                now = self.clock()
                with self.lock:
                    if error.state in {"sign_in_required", "unsupported", "unavailable"}:
                        current.update(windows=[], observed_at=None, plan=None, account=None)
                    current.update(state=error.state, message=str(error), checked_at=now,
                                   next_refresh_at=now + error.retry_after)
        return self.snapshot()

    def _codex(self):
        account = self.sessions.rpc.request("account/read", {"refreshToken": False}).get("account")
        if not isinstance(account, dict):
            raise QuotaError("Sign in to Codex on this Mac to see its limits.", state="sign_in_required")
        self._identity("codex", json.dumps(account, sort_keys=True))
        if account.get("type") == "apiKey":
            raise QuotaError("Subscription quotas are unavailable for an API-key connection.", state="unsupported")
        data = self.sessions.rpc.request("account/rateLimits/read")
        return {"windows": codex_windows(data), "plan": _text(account.get("planType")) or None,
                "account": _text(account.get("email")) or None, "source": "Codex"}

    def _claude(self):
        oauth = _claude_credential()
        self._identity("claude", oauth.get("refreshToken") or oauth["accessToken"])
        expires = _number(oauth.get("expiresAt"))
        if expires and expires / 1000 <= self.clock():
            raise QuotaError("Open Claude Code to refresh its sign-in, then refresh usage.", state="sign_in_required")
        data = _http_json("https://api.anthropic.com/api/oauth/usage", headers={
            "Authorization": "Bearer " + oauth["accessToken"], "anthropic-beta": "oauth-2025-04-20"})
        return {"windows": claude_windows(data), "plan": _text(oauth.get("subscriptionType")) or None,
                "account": "Claude Code sign-in", "source": "Claude"}

    def _antigravity(self):
        path = provider_directory(self.sessions.store) / "profile/antigravity-acp/acp_token.json"
        if not path.is_file():
            raise QuotaError("Sign in to Antigravity in Ribbon Field to see its limits.", state="sign_in_required")
        credential = _read_json(path)
        if any(not isinstance(credential.get(k), str) or not credential[k] for k in
               ("client_id", "client_secret", "refresh_token")):
            raise QuotaError("Antigravity sign-in needs refreshing.", state="sign_in_required")
        identity = self._identity("antigravity", credential["refresh_token"])
        cached = self.google_access
        if not cached or cached[0] != identity or cached[2] <= self.clock() + 60:
            data = _http_json("https://oauth2.googleapis.com/token", token_request=True, body={
                **{key: credential[key] for key in ("client_id", "client_secret", "refresh_token")},
                "grant_type": "refresh_token"})
            access = data.get("access_token")
            if not isinstance(access, str) or not access:
                raise QuotaError("Antigravity could not refresh access to its limits.")
            self.google_access = (identity, access, self.clock() + (_number(data.get("expires_in"), 86400) or 300))
        access = self.google_access[1]
        headers = {"Authorization": "Bearer " + access, "User-Agent": "antigravity"}
        project = credential.get("project_id")
        try:
            if not isinstance(project, str) or not project:
                data = _http_json("https://daily-cloudcode-pa.googleapis.com/v1internal:loadCodeAssist",
                    headers=headers, body={"metadata": {"ideType": "ANTIGRAVITY", "platform": "PLATFORM_UNSPECIFIED", "pluginType": "GEMINI"}})
                project = data.get("cloudaicompanionProject")
                if isinstance(project, dict):
                    project = project.get("id")
                if not isinstance(project, str) or not project:
                    raise QuotaError("Antigravity did not report an account quota project.", state="unavailable")
            data = _http_json("https://daily-cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary",
                              headers=headers, body={"project": project})
        except QuotaError:
            self.google_access = None
            raise
        return {"windows": antigravity_windows(data), "account": "Ribbon Field sign-in", "source": "Antigravity"}
