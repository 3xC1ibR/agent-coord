"""Opt-in completed-turn sync using Stoic's existing conversation import API.

The provider stages local state; finite background uploads never hold provider
or database locks. No service, polling loop, or transcript backfill is needed.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import threading
from urllib import request
from urllib.parse import urlsplit
import uuid


# Keep the consent grammar identical to Stoic's Codex/Claude Code hooks.
ENABLE_PROMPT = re.compile(
    r"^\s*(?:(?:<command-name>)?[/$]stoic\b|(?:please\s+)?use\s+stoic\b)", re.I)
DISABLE_PROMPT = re.compile(
    r"^\s*(?:(?:<command-name>)?[/$]stoic(?:</command-name>)?\s+off\b|stop\s+using\s+stoic\b)", re.I)
DEFERRED = "Stoic conversation sync is deferred; check its local sync state."


def _now():
    return datetime.now(timezone.utc).isoformat()


def _token():
    token = os.environ.get("STOIC_API_TOKEN", "").strip()
    if token:
        return token
    path = Path(os.environ.get("STOIC_API_TOKEN_FILE") or "~/.config/stoic/codex-token").expanduser()
    if not path.is_file() or path.stat().st_mode & 0o077:
        return ""
    return path.read_text(encoding="utf-8").strip()


def _account(token):
    body = token.split(".")[1]
    subject = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))["sub"]
    if not isinstance(subject, str) or not subject:
        raise ValueError("Missing account")
    return hashlib.sha256(subject.encode()).hexdigest()[:20]


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _api(path, token, payload=None):
    origin = os.environ.get("STOIC_API_URL", "https://tryblume.ai").rstrip("/")
    parsed = urlsplit(origin)
    if (parsed.scheme != "https" and not (
            parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"})) or (
            parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("Invalid Stoic origin")
    req = request.Request(origin + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    with request.build_opener(_NoRedirect).open(req, timeout=4) as response:
        if response.status != 200:
            raise ValueError("Unexpected response")
        return json.load(response)


class ConversationSync:
    def __init__(self, root=None):
        self.root = Path(root or os.environ.get("STOIC_ANTIGRAVITY_SYNC_STATE_DIR") or
                         "~/.local/state/stoic-antigravity-sync").expanduser()

    def _directory(self, session_id):
        return self.root / hashlib.sha256(session_id.encode()).hexdigest()

    @contextmanager
    def _locked(self, directory, name="state.lock", *, blocking=True):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        fd = os.open(directory / name, os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, "w") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            yield

    @staticmethod
    def _read(directory):
        path = directory / "state.json"
        return json.loads(path.read_text()) if path.exists() else {}

    @staticmethod
    def _save(directory, state):
        fd, temporary = tempfile.mkstemp(prefix=".state-", dir=directory)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(state, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, directory / "state.json")
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def begin(self, session_id, turn_id, prompt, *, allow_consent=True):
        """Observe only the submitted user text, never injected instructions."""
        try:
            directory = self._directory(session_id)
            enable = allow_consent and ENABLE_PROMPT.match(prompt)
            disable = allow_consent and DISABLE_PROMPT.match(prompt)
            if not directory.exists() and not (enable or disable):
                return None
            with self._locked(directory):
                state = self._read(directory)
                if disable:
                    self._save(directory, {"enabled": False, "pending": {}})
                    return "Stoic conversation sync is off for this session."
                notice = None
                if enable:
                    token = _token()
                    if not token:
                        return "Stoic conversation sync could not be enabled: the agent token is missing or its file is not private."
                    account = _account(token)
                    if state.get("account") != account or not state.get("enabled"):
                        state = {"account": account, "generation": uuid.uuid4().hex, "pending": {}}
                    state["enabled"] = True
                    notice = "Stoic conversation sync is on for this session."
                if state.get("enabled") and prompt.strip():
                    state["pending"].setdefault(turn_id, {"session_id": session_id,
                        "turn_id": turn_id, "prompt": prompt, "prompt_at": _now()})
                    self._save(directory, state)
                return notice
        except Exception:
            # Exceptions may contain credentials, URLs or conversation content.
            logging.getLogger(__name__).warning(DEFERRED)
            return DEFERRED

    def finish(self, session_id, turn, *, background=True):
        """Persist completion before returning to the provider event handler."""
        try:
            directory = self._directory(session_id)
            if not directory.exists():
                return
            with self._locked(directory):
                state = self._read(directory)
                pending = state.get("pending", {})
                data = pending.get(turn["id"])
                reply = next((item.get("text", "") for item in reversed(turn.get("items", []))
                              if item.get("type") == "agentMessage"), "")
                if data:
                    if turn.get("status") == "completed" and reply.strip():
                        data.setdefault("reply", reply)
                        data.setdefault("reply_at", _now())
                    else:
                        pending.pop(turn["id"])
                    self._save(directory, state)
                ready = state.get("enabled") and any(p.get("reply") for p in pending.values())
            if ready and background:
                threading.Thread(target=self.flush, args=(session_id,),
                                 name="stoic-conversation-upload", daemon=True).start()
        except Exception:
            logging.getLogger(__name__).warning(DEFERRED)

    def flush(self, session_id):
        """Retry ready turns in order. Server IDs make lost acknowledgments safe."""
        directory = self._directory(session_id)
        if not directory.exists():
            return
        try:
            # Separate from the state lock: an off command never waits on HTTP.
            with self._locked(directory, "upload.lock", blocking=False):
                token = _token()
                with self._locked(directory):
                    state = self._read(directory)
                    if not state.get("enabled") or not state.get("pending"):
                        return
                    if not token or _account(token) != state.get("account"):
                        state["last_error"] = "credential_missing_or_account_changed"
                        self._save(directory, state)
                        return
                    generation = state["generation"]
                if _api("/api/auth/web/me", token).get("actor") != "agent":
                    raise ValueError("Agent credential required")
                while True:
                    with self._locked(directory):
                        state = self._read(directory)
                        if not state.get("enabled") or state.get("generation") != generation:
                            return
                        pending = sorted((p for p in state["pending"].values() if p.get("reply")),
                                         key=lambda p: p["prompt_at"])
                        if not pending:
                            return
                        data = pending[0]
                    result = _api("/api/codex/turns", token, data)
                    if not result.get("conversation_id") or result.get("added") not in {0, 1, 2}:
                        raise ValueError("Invalid import receipt")
                    with self._locked(directory):
                        state = self._read(directory)
                        if state.get("generation") != generation:
                            return
                        state["pending"].pop(data["turn_id"], None)
                        state.update(last_synced_at=_now(), last_turn_id=data["turn_id"],
                                     conversation_id=result["conversation_id"], last_error=None)
                        self._save(directory, state)
        except BlockingIOError:
            return  # Another finite uploader owns this session.
        except Exception:
            logging.getLogger(__name__).warning(DEFERRED)
            try:
                with self._locked(directory):
                    state = self._read(directory)
                    if state.get("enabled"):
                        state["last_error"] = "upload_deferred"
                        self._save(directory, state)
            except Exception:
                pass
