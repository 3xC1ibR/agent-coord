"""Caller context and executable discovery shared by the CLI and launchers."""
from __future__ import annotations

import os
from pathlib import Path
import shlex
import shutil
import tempfile


def cli_path() -> Path:
    return Path(__file__).resolve().parents[1] / "agent-coord"


def caller_session() -> str | None:
    return os.environ.get("AGENT_COORD_SESSION_ID") or os.environ.get("CODEX_THREAD_ID") or None


def client_environment(environment, database_path, client, *, session_id=None):
    env = dict(environment)
    # A child must never inherit the parent's identity or Claude shell exports.
    for key in ("AGENT_COORD_SESSION_ID", "CODEX_THREAD_ID", "CLAUDE_ENV_FILE"):
        env.pop(key, None)
    env.update(AGENT_COORD_DB=str(database_path), AGENT_COORD_CLIENT=client)
    env["PATH"] = str(cli_path().parent) + os.pathsep + env.get("PATH", os.defpath)
    if session_id:
        env["AGENT_COORD_SESSION_ID"] = session_id
    return env


def command_prefix(database_path) -> str:
    from .store import default_database_path

    command = "agent-coord" if shutil.which("agent-coord") else shlex.quote(str(cli_path()))
    if Path(database_path).resolve() != default_database_path().resolve():
        command += " --db " + shlex.quote(str(database_path))
    return command


def persist_claude_environment(database_path, session_id) -> bool:
    """SessionStart exports used by Claude's subsequent Bash commands."""
    destination = os.environ.get("CLAUDE_ENV_FILE")
    if not destination:
        return False
    with open(destination, "a", encoding="utf-8") as output:
        output.write("\nexport AGENT_COORD_SESSION_ID=" + shlex.quote(session_id) + "\n")
        output.write("export AGENT_COORD_DB=" + shlex.quote(str(database_path)) + "\n")
        output.write("export PATH=" + shlex.quote(str(cli_path().parent)) + ':"$PATH"\n')
    return True


def install_cli(bin_dir: str | None = None) -> dict:
    """Install a stable executable; rerunning after plugin refresh retargets it."""
    from .store import CoordinationError

    directory = Path(bin_dir).expanduser() if bin_dir else Path.home() / ".local/bin"
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "agent-coord"
    marker = "# Agent Coord managed launcher\n"
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not target.is_file() or marker not in target.read_text(encoding="utf-8"):
            raise CoordinationError(f"Refusing to replace an unmanaged executable: {target}")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory, delete=False) as output:
            temporary = Path(output.name)
            output.write("#!/bin/sh\n" + marker + "exec " + shlex.quote(str(cli_path())) + ' "$@"\n')
        temporary.chmod(0o755)
        os.replace(temporary, target)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
    return {"status": "installed", "path": str(target), "target": str(cli_path()),
            "on_path": str(directory) in os.environ.get("PATH", "").split(os.pathsep)}
