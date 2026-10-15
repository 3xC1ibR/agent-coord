"""Caller context and executable discovery shared by the CLI and launchers."""
from __future__ import annotations

import json
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


def coordination_bootstrap(store, session_id, *, caller_context=True, command=None):
    """The same actionable coordination contract for every provider and resume."""
    session = store.get_session(session_id)
    command = command or command_prefix(store.database_path)
    identity = ""
    if not caller_context and caller_session() != session_id:
        identity = " --session-id " + shlex.quote(session_id)
    skill = cli_path().parent.parent / "skills/agent-coordination/SKILL.md"
    text = (
        f"This session is registered with agent-coord as {session_id}. "
        f"Use {command}; caller identity is inferred from AGENT_COORD_SESSION_ID or CODEX_THREAD_ID. "
        "Explicit --session-id and --from-session override it. "
        f"Read workspace AGENTS.md and the agent-coordination skill at {skill} before implementation. "
        "A sole active session may write without a Beads issue or scope. When another session is active, "
        "declare the smallest write scope; Beads is optional for all coordination workflows. "
        "To review or organize the user's threads across workspaces, use the manage-threads skill. "
        "Use thread list without --cwd for discovery; use a known session ID directly for a specific thread. "
        "To close your own thread after an explicit user request, save the checkpoint, then run "
        "thread close --session-id <your-session-id> --after-turn. "
        "A queued receipt is not confirmation that the thread has closed."
    )
    text += "\nCurrent coordination state: " + json.dumps({
        key: session[key] for key in ("cwd", "write_scope", "lease_mode", "activity", "scope_required")
    }, sort_keys=True)
    text += (
        "\nCLI examples (replace path placeholders; repeat --scope for each path, never comma-join):"
        f"\n{command} status"
        f"\n{command} begin-work{identity} --scope 'src/example.py' --scope 'tests/test_example.py'"
        f"\n{command} inbox{identity} --unread"
        f"\n{command} ack{identity} --message-id <id>"
        f"\n{command} end-work{identity}"
        "\nAcknowledge handled messages silently with ack when not replying. "
        "Reply only when reply_required=true, using "
        f"{command} reply --message-id <id> '<response>'; a successful reply automatically "
        "acknowledges the original message."
    )
    return text + "\n\n" + store.threads.instructions(session_id, caller_context=caller_context, command=command)


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
