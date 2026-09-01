from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .store import WAKEABLE_ACTIVITIES, CoordinationError, CoordinationStore

DEFAULT_WAKE_POLL_SECONDS = 0.5
DEFAULT_WAKE_VERIFY_ATTEMPTS = 5
DEFAULT_WAKE_VERIFY_INTERVAL_SECONDS = 0.05
WAKE_PROMPT = "Check and handle your unread agent-coord messages."
PROMPT_EMPTY = "empty"
PROMPT_SELF_WAKE = "self-wake"
PROMPT_OCCUPIED = "occupied"
_ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_CLAUDE_PROMPT = re.compile(r"^\s*❯\s*(.*)$")
_CODEX_PROMPT = re.compile(r"^\s*›\s*(.*)$")
_CODEX_PLACEHOLDERS = {
    "Ask Codex to do anything",
    "Describe a task",
}


class ZellijCommandError(CoordinationError):
    """A Zellij action could not inspect or wake the registered pane."""


def _normalize_pane_id(value: str) -> str:
    candidate = value.strip()
    if candidate.isdigit():
        return f"terminal_{candidate}"
    if re.fullmatch(r"terminal_\d+", candidate):
        return candidate
    raise CoordinationError(
        "Zellij pane ID must be a terminal pane such as terminal_12 or 12."
    )


def prompt_state(screen: str, client: str) -> str:
    """Classify the last recognizable client prompt without altering its input."""
    clean = _ANSI_ESCAPE.sub("", screen).replace("\u00a0", " ")
    matcher = _CLAUDE_PROMPT if client == "claude" else _CODEX_PROMPT
    matches = [matcher.match(line) for line in clean.splitlines()]
    prompts = [match.group(1).strip() for match in matches if match is not None]
    if not prompts:
        return PROMPT_OCCUPIED
    value = prompts[-1]
    if not value:
        return PROMPT_EMPTY
    if client == "codex" and value in _CODEX_PLACEHOLDERS:
        return PROMPT_EMPTY
    if value == WAKE_PROMPT:
        return PROMPT_SELF_WAKE
    return PROMPT_OCCUPIED


def prompt_is_empty(screen: str, client: str) -> bool:
    """Return true only when the last recognizable client prompt has no input."""
    return prompt_state(screen, client) == PROMPT_EMPTY


class ZellijClient:
    def __init__(
        self,
        *,
        session_name: str,
        pane_id: str,
        executable: str = "zellij",
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.session_name = session_name
        self.pane_id = _normalize_pane_id(pane_id)
        self.executable = executable
        self.run = run

    def _action(self, arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
        command = [
            self.executable,
            "--session",
            self.session_name,
            "action",
            *arguments,
        ]
        result = self.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
            raise ZellijCommandError(
                f"Zellij action failed for {self.pane_id}: {detail}"
            )
        return result

    def dump_screen(self) -> str:
        return self._action(["dump-screen", "--pane-id", self.pane_id]).stdout

    def wake(self, prompt: str | None = WAKE_PROMPT) -> None:
        if prompt is not None:
            self._action(["write-chars", "--pane-id", self.pane_id, prompt])
        self._action(["send-keys", "--pane-id", self.pane_id, "Enter"])


class ZellijWakeWatcher:
    def __init__(
        self,
        store: CoordinationStore,
        session_id: str,
        *,
        executable: str = "zellij",
        client_factory: Callable[..., ZellijClient] = ZellijClient,
        verify_attempts: int = DEFAULT_WAKE_VERIFY_ATTEMPTS,
        verify_interval_seconds: float = DEFAULT_WAKE_VERIFY_INTERVAL_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.store = store
        self.session_id = session_id
        self.executable = executable
        self.client_factory = client_factory
        self.verify_attempts = verify_attempts
        self.verify_interval_seconds = verify_interval_seconds
        self.sleep = sleep

    def _submission_observed(
        self, client: ZellijClient, client_name: str
    ) -> tuple[bool, str | None]:
        for attempt in range(self.verify_attempts):
            if self.store.get_session(self.session_id)["turn_active"]:
                return True, None
            try:
                screen = client.dump_screen()
            except ZellijCommandError as exc:
                return False, str(exc)
            if prompt_state(screen, client_name) != PROMPT_SELF_WAKE:
                return True, None
            if attempt + 1 < self.verify_attempts:
                self.sleep(self.verify_interval_seconds)
        return False, "Agent Coord wake prompt remained unsubmitted."

    def run_once(self) -> dict[str, Any]:
        try:
            target = self.store.get_zellij_wake(self.session_id)
        except CoordinationError:
            return {"status": "disabled", "session_id": self.session_id}
        if not target["enabled"]:
            return {"status": "disabled", "session_id": self.session_id}

        session = self.store.get_session(self.session_id)
        if session["presence"] == "offline":
            return {"status": "offline", "session_id": self.session_id}

        message_ids = self.store.pending_wake_message_ids(self.session_id)
        if not message_ids:
            return {"status": "waiting", "session_id": self.session_id}

        if session["turn_active"] or session["activity"] not in WAKEABLE_ACTIVITIES:
            self.store.record_zellij_check(self.session_id)
            return {
                "status": "busy",
                "session_id": self.session_id,
                "message_ids": message_ids,
            }

        client = self.client_factory(
            session_name=target["zellij_session"],
            pane_id=target["pane_id"],
            executable=self.executable,
        )
        try:
            screen = client.dump_screen()
        except ZellijCommandError as exc:
            self.store.record_zellij_check(self.session_id, error=str(exc))
            return {
                "status": "unavailable",
                "session_id": self.session_id,
                "error": str(exc),
            }
        state = prompt_state(screen, session["client"])
        if state == PROMPT_OCCUPIED:
            self.store.record_zellij_check(self.session_id)
            return {
                "status": "prompt-not-empty",
                "session_id": self.session_id,
                "message_ids": message_ids,
            }

        claimed = self.store.claim_wake_messages(self.session_id, message_ids)
        if not claimed:
            return {"status": "raced", "session_id": self.session_id}
        try:
            client.wake(None if state == PROMPT_SELF_WAKE else WAKE_PROMPT)
        except ZellijCommandError as exc:
            self.store.complete_wake_attempts(
                self.session_id,
                claimed,
                outcome="failed",
                detail=str(exc),
            )
            self.store.record_zellij_check(self.session_id, error=str(exc))
            return {
                "status": "failed",
                "session_id": self.session_id,
                "message_ids": claimed,
                "error": str(exc),
            }

        submitted, submission_error = self._submission_observed(
            client, session["client"]
        )
        if not submitted:
            detail = submission_error or "Wake submission could not be verified."
            self.store.complete_wake_attempts(
                self.session_id,
                claimed,
                outcome="failed",
                detail=detail,
            )
            self.store.record_zellij_check(self.session_id, error=detail)
            return {
                "status": "failed",
                "session_id": self.session_id,
                "message_ids": claimed,
                "error": detail,
            }

        self.store.complete_wake_attempts(
            self.session_id,
            claimed,
            outcome="sent",
            detail=WAKE_PROMPT,
        )
        self.store.record_zellij_check(self.session_id, woke=True)
        return {
            "status": "woke",
            "session_id": self.session_id,
            "message_ids": claimed,
        }


def _pid_is_running(pid: int | None) -> bool:
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    return True


def _lock_path(store: CoordinationStore, session_id: str) -> Path:
    digest = hashlib.sha256(session_id.encode()).hexdigest()[:16]
    directory = store.database_path.parent / "wake"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"zellij-{digest}.lock"


@contextmanager
def _watcher_lock(store: CoordinationStore, session_id: str):
    try:
        import fcntl
    except ImportError as exc:  # pragma: no cover - Zellij targets Unix systems.
        raise CoordinationError("Zellij wake requires Unix file locking.") from exc
    path = _lock_path(store, session_id)
    with path.open("a+", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CoordinationError(
                f"A Zellij wake watcher already owns session {session_id}."
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def watch_zellij(
    store: CoordinationStore,
    session_id: str,
    *,
    once: bool = False,
    poll_interval_seconds: float = DEFAULT_WAKE_POLL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    executable: str = "zellij",
) -> dict[str, Any]:
    if poll_interval_seconds <= 0:
        raise CoordinationError("Wake poll interval must be positive.")
    with _watcher_lock(store, session_id):
        watcher_pid = os.getpid()
        store.set_zellij_watcher_pid(session_id, watcher_pid)
        try:
            watcher = ZellijWakeWatcher(store, session_id, executable=executable)
            while True:
                result = watcher.run_once()
                if once or result["status"] in {"disabled", "offline"}:
                    return result
                sleep(poll_interval_seconds)
        finally:
            store.clear_zellij_watcher_pid(session_id, watcher_pid)


def enable_zellij_wake(
    store: CoordinationStore,
    session_id: str,
    *,
    zellij_session: str | None = None,
    pane_id: str | None = None,
    environ: dict[str, str] | None = None,
    popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
) -> dict[str, Any]:
    environment = os.environ if environ is None else environ
    target_session = zellij_session or environment.get("ZELLIJ_SESSION_NAME")
    target_pane = pane_id or environment.get("ZELLIJ_PANE_ID")
    if not target_session or not target_pane:
        raise CoordinationError(
            "Zellij wake needs --zellij-session and --pane-id, or the matching "
            "ZELLIJ_SESSION_NAME and ZELLIJ_PANE_ID environment variables."
        )
    executable = shutil.which("zellij")
    if executable is None:
        raise CoordinationError("zellij is not installed or is not on PATH.")
    normalized_pane = _normalize_pane_id(target_pane)

    existing: dict[str, Any] | None = None
    try:
        existing = store.get_zellij_wake(session_id)
    except CoordinationError:
        pass
    target = store.register_zellij_wake(
        session_id=session_id,
        zellij_session=target_session,
        pane_id=normalized_pane,
    )
    if existing and _pid_is_running(existing["watcher_pid"]):
        target["watcher_started"] = False
        target["watcher_already_running"] = True
        return target

    wrapper = Path(__file__).resolve().parents[1] / "agent-coord"
    log_directory = store.database_path.parent / "wake"
    log_directory.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(session_id.encode()).hexdigest()[:16]
    log_path = log_directory / f"zellij-{digest}.log"
    command = [
        sys.executable,
        str(wrapper),
        "--db",
        str(store.database_path),
        "wake-zellij",
        "watch",
        "--session-id",
        session_id,
    ]
    try:
        with log_path.open("ab") as log_file:
            process = popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
    except OSError as exc:
        store.disable_zellij_wake(session_id)
        raise CoordinationError(f"Cannot start Zellij wake watcher: {exc}") from exc
    try:
        target = store.set_zellij_watcher_pid(session_id, process.pid)
    except Exception:
        process.terminate()
        store.disable_zellij_wake(session_id)
        raise
    target["watcher_started"] = True
    target["watcher_already_running"] = False
    target["log_path"] = str(log_path)
    return target


def environment_requests_wake(environ: dict[str, str] | None = None) -> bool:
    environment = os.environ if environ is None else environ
    return environment.get("AGENT_COORD_ZELLIJ_WAKE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def enable_from_environment(
    store: CoordinationStore, session_id: str
) -> dict[str, Any] | None:
    if not environment_requests_wake():
        return None
    return enable_zellij_wake(store, session_id)
