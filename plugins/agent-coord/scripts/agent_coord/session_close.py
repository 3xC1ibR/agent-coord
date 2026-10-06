"""Stop a terminal Codex only when its open transcript identifies the process."""
from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import time
import uuid
from pathlib import Path

from .store import CoordinationError


def _command(arguments: list[str]) -> str:
    try:
        result = subprocess.run(arguments, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CoordinationError("Cannot verify the terminal session. Exit it in its terminal, then close this thread.") from exc
    if result.returncode not in {0, 1} or (result.returncode and result.stderr.strip()):
        raise CoordinationError("Cannot verify the terminal session. Exit it in its terminal, then close this thread.")
    return result.stdout


def _owners(transcript: Path) -> set[int]:
    return {int(line[1:]) for line in _command(["lsof", "-nP", "-Fp", str(transcript)]).splitlines()
            if line.startswith("p") and line[1:].isdigit()}


def _is_terminal_codex(pid: int) -> bool:
    try:
        command = shlex.split(_command(["ps", "-ww", "-p", str(pid), "-o", "args="]).strip())
    except ValueError:
        return False
    tty = _command(["ps", "-p", str(pid), "-o", "tty="]).strip()
    return bool(command and Path(command[0]).name == "codex" and tty and tty not in {"?", "??"}
                and not {"app-server", "exec", "exec-server", "review"}.intersection(command[1:]))


def stop_terminal_session(session: dict, *, timeout: float = 5) -> None:
    """Never signal a shared app-server, shell, or a remembered/reused pane ID."""
    if session["presence"] == "offline":
        return
    error = "Cannot safely stop this session from the UI. Exit it in its terminal, then close this thread."
    if session["client"] != "codex":
        raise CoordinationError(error)
    try:
        session_id = str(uuid.UUID(session["session_id"]))
    except (ValueError, TypeError) as exc:
        raise CoordinationError(error) from exc
    root = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    transcripts = list((root / "sessions").glob(f"**/*-{session_id}.jsonl"))
    if len(transcripts) != 1:
        raise CoordinationError(error)
    transcript = transcripts[0]
    try:
        with transcript.open(encoding="utf-8") as stream:
            metadata = json.loads(stream.readline())
        if (not isinstance(metadata, dict) or metadata.get("type") != "session_meta"
                or not isinstance(metadata.get("payload"), dict) or metadata["payload"].get("id") != session_id):
            raise CoordinationError(error)
    except (OSError, ValueError) as exc:
        raise CoordinationError(error) from exc
    owners = _owners(transcript)
    if len(owners) != 1:
        raise CoordinationError(error)
    pid = next(iter(owners))
    if pid == os.getpid() or not _is_terminal_codex(pid) or _owners(transcript) != {pid}:
        raise CoordinationError(error)
    try:
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            os.kill(pid, 0)
            time.sleep(0.05)
    except ProcessLookupError:
        return
    except OSError as exc:
        raise CoordinationError(f"Could not stop the terminal session: {exc}") from exc
    raise CoordinationError("The terminal session has not exited yet. Retry Close after it exits.")
