"""Caller context and executable discovery shared by the CLI and launchers."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import tempfile
from contextvars import ContextVar
from contextlib import contextmanager
import sys


# A shared app-server can issue MCP calls for several sessions in one process.
# Never publish request identity through process-wide environment variables.
request_session: ContextVar[str | None] = ContextVar("agent_coord_request_session", default=None)


def cli_path() -> Path:
    return Path(__file__).resolve().parents[1] / "agent-coord"


request_environment: ContextVar[dict | None] = ContextVar("agent_coord_request_environment", default=None)
request_directory: ContextVar[str | None] = ContextVar("agent_coord_request_directory", default=None)
request_input: ContextVar[str | None] = ContextVar("agent_coord_request_input", default=None)
request_ownership: ContextVar[frozenset[tuple[int, str]]] = ContextVar("agent_coord_request_ownership", default=frozenset())


def caller_environment():
    selected = request_environment.get()
    return os.environ if selected is None else selected


def caller_directory():
    return request_directory.get() or os.getcwd()


def read_input(limit=-1):
    value = request_input.get()
    if value is None:
        return sys.stdin.read(limit)
    return value if limit < 0 else value[:limit]


@contextmanager
def client_context(*, environment, directory, session=None, input_text=None):
    tokens = [(request_environment, request_environment.set(environment)),
              (request_directory, request_directory.set(directory)),
              (request_session, request_session.set(session)),
              (request_input, request_input.set(input_text))]
    try:
        yield
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)


def caller_session() -> str | None:
    environment = caller_environment()
    return request_session.get() or environment.get("AGENT_COORD_SESSION_ID") or environment.get("CODEX_THREAD_ID") or None


def bind_caller(explicit=None, *, require_context=False):
    """Bind a client action's actor; target/recipient IDs are not actors.

    CLI administration without a provider context may name an actor explicitly.
    Shared MCP servers must have request metadata or a bound environment: an
    argument copied from conversation history cannot establish their caller.
    """
    from .store import CoordinationError

    identity = caller_session()
    if explicit is not None and (not isinstance(explicit, str) or not explicit.strip()):
        raise CoordinationError("Caller session ID must be a nonempty string.")
    if not identity and require_context:
        raise CoordinationError(
            "No caller session is bound to this MCP request. Use the agent-coord CLI "
            "from your current tool shell, which inherits the executing thread identity; "
            "do not supply an ID copied from conversation history."
        )
    if identity and explicit is not None and explicit != identity:
        raise CoordinationError(
            f"Caller identity mismatch: this action belongs to executing thread {identity}, "
            f"not {explicit}. Omit the caller ID or use {identity}. "
            "A fork must not reuse its parent's identity."
        )
    return identity or explicit


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
        "Caller-owned actions reject an explicit --session-id or --from-session that differs from it. "
        "A fork uses its own current identity, never an ID copied from inherited history. "
        f"Read workspace AGENTS.md and the agent-coordination skill at {skill} before implementation. "
        "A sole active session may write without a Beads issue or scope. When another session is active, "
        "declare the smallest write scope; Beads is optional for all coordination workflows. "
        "To review or organize the user's threads across workspaces, use the manage-threads skill. "
        "Use thread list without --cwd for discovery; use a known session ID directly for a specific thread. "
        "To close your own thread after an explicit user request, save the checkpoint, then run "
        "thread close --session-id <your-session-id> --after-turn. "
        "A queued receipt is not confirmation that the thread has closed."
    )
    text += (
        "\nRibbon Field renders complete fenced `openui` or `openui-lang` blocks in assistant messages "
        "as interactive UI using the bundled OpenUI chat library. For charts, tables, cards, or forms, "
        "read the authoring guide at "
        f"{skill.parent / 'references/openui.md'}. "
        "Button/form actions arrive as ordinary user messages prefixed 'OpenUI response'. "
        "Use this for useful interactive answers; normal prose and code stay Markdown."
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
    destination = caller_environment().get("CLAUDE_ENV_FILE")
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
