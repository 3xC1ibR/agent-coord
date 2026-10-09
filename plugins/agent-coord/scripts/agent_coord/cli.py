from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .context import caller_session, install_cli
from .delegate import delegate_work
from .managed_pty import read_delegation_output, supervise_managed_pty
from .navigation import NavigationStore
from .store import (
    ACTIVITIES,
    AmbiguousTargetError,
    ConflictError,
    CoordinationError,
    CoordinationStore,
    InboxTimeoutError,
)
from .ui import serve_ui
from .threads import ATTENTION_STATES, PHASES
from .thread_control import ThreadControl
from .app_control import AppControl
from .zellij_wake import enable_zellij_wake, watch_zellij


def find_repository_root(cwd: str) -> str:
    candidate = str(Path(cwd).resolve())
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=candidate,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0 and result.stdout.strip():
        return str(Path(result.stdout.strip()).resolve())
    return candidate


def validate_claimed_bead(bead_id: str, cwd: str) -> dict[str, Any]:
    if shutil.which("bd") is None:
        raise CoordinationError("bd is required before beginning implementation work.")
    result = subprocess.run(
        ["bd", "show", bead_id, "--json"],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown bd error"
        raise CoordinationError(f"Cannot read bead {bead_id}: {detail}")
    try:
        payload = json.loads(result.stdout)
        issue = payload[0]
    except (json.JSONDecodeError, IndexError, TypeError) as exc:
        raise CoordinationError(
            f"bd returned invalid data for bead {bead_id}."
        ) from exc
    if issue.get("status") != "in_progress" or not issue.get("assignee"):
        raise CoordinationError(
            f"Bead {bead_id} must be claimed and in_progress before work begins."
        )
    return issue


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-coord",
        description="Coordinate local Claude Code and Codex sessions.",
    )
    parser.add_argument("--db", help="Override the shared SQLite database path.")
    subcommands = parser.add_subparsers(dest="command", required=True)
    installer = subcommands.add_parser("install-cli", help="Install or refresh the stable agent-coord launcher.")
    installer.add_argument("--bin-dir", help="Executable directory; defaults to ~/.local/bin.")

    register = subcommands.add_parser("register", help="Register or refresh a session.")
    register.add_argument("--session-id", help="Caller session; defaults to AGENT_COORD_SESSION_ID or CODEX_THREAD_ID.")
    register.add_argument("--client", required=True, choices=["claude", "codex"])
    register.add_argument("--cwd", default=os.getcwd())
    register.add_argument("--name")

    status = subcommands.add_parser("status", help="Show one session.")
    status.add_argument("--session-id", help="Caller session; defaults to AGENT_COORD_SESSION_ID or CODEX_THREAD_ID.")

    activity = subcommands.add_parser("set-activity", help="Set semantic activity.")
    activity.add_argument("--session-id", help="Caller session; defaults to AGENT_COORD_SESSION_ID or CODEX_THREAD_ID.")
    activity.add_argument("--activity", required=True, choices=sorted(ACTIVITIES))

    checkpoint = subcommands.add_parser("checkpoint", help="Save a factual work-thread checkpoint and optional links.")
    checkpoint.add_argument("--session-id", help="Caller session; defaults to AGENT_COORD_SESSION_ID or CODEX_THREAD_ID.")
    checkpoint.add_argument("--json", dest="checkpoint_json", required=True,
                           help="Checkpoint JSON object, or - to read stdin. Phases: " + ", ".join(sorted(PHASES)) + ".")

    thread = subcommands.add_parser("thread", help="Create app agents; find and manage durable work threads.")
    thread_commands = thread.add_subparsers(dest="thread_command", required=True)
    thread_list = thread_commands.add_parser("list")
    thread_list.add_argument("--cwd")
    thread_list.add_argument("--archived", action="store_true")
    thread_search = thread_commands.add_parser("search", help="Search saved messages and context across open and closed threads.")
    thread_search.add_argument("query", help="Up to 10 literal keywords in saved context or a conversation message.")
    thread_search.add_argument("--source", choices=("all", "context", "messages"), default="all",
                               help="Search saved context and messages (default), or either source separately.")
    thread_search.add_argument("--my-messages", action="store_true", help="Match only user messages, excluding saved context and assistant replies.")
    thread_search.add_argument("--cwd")
    thread_search.add_argument("--limit", type=int, default=10)
    thread_search.add_argument("--cursor", help="Continue with the same query and filters using next_cursor.")
    thread_search.add_argument("--app-only", action="store_true", help="Return only app conversations that support automatic inbox wake.")
    for command in (thread_list, thread_search):
        for field in ("repository", "project"):
            options = command.add_mutually_exclusive_group()
            options.add_argument("--" + field, dest=field + "_id", default=argparse.SUPPRESS, metavar="ID")
            options.add_argument("--no-" + field, dest=field + "_id", action="store_const", const=None, default=argparse.SUPPRESS)
    thread_show = thread_commands.add_parser("show")
    thread_show.add_argument("--session-id", help="Caller session; defaults to AGENT_COORD_SESSION_ID or CODEX_THREAD_ID.")
    create_agent = thread_commands.add_parser("create", help="Create an independent app agent and route its first request.")
    create_agent.add_argument("--from-session", help="Dispatcher; defaults to the current caller.")
    create_agent.add_argument("--cwd", default=os.getcwd())
    create_agent.add_argument("--name", required=True)
    create_agent.add_argument("--client", choices=["codex", "claude"], default="codex")
    create_agent.add_argument("--model")
    create_agent.add_argument("--effort")
    create_agent.add_argument("--yolo", action="store_true", help="Explicitly authorize full machine access without approval prompts.")
    create_agent.add_argument("--reply-required", action=argparse.BooleanOptionalAction, default=False,
                              help="Request an initial result back to the creating agent (default: answer the user directly).")
    create_agent.add_argument("prompt", help="Exact initial user request; use - to read stdin.")
    settings = thread_commands.add_parser("settings", help="Inspect app settings or queue an idle settings change.")
    settings.add_argument("--session-id", required=True, help="Target app conversation.")
    settings.add_argument("--from-session", help="Requester; defaults to the current caller.")
    settings.add_argument("--model")
    settings.add_argument("--effort")
    permissions = settings.add_mutually_exclusive_group()
    permissions.add_argument("--yolo", dest="yolo", action="store_const", const=True, default=None)
    permissions.add_argument("--default-permissions", dest="yolo", action="store_const", const=False)
    models = thread_commands.add_parser("models", help="Query the running app for supported provider models and effort levels.")
    models.add_argument("--from-session", help="Requester; defaults to the current caller.")
    models.add_argument("--cwd", default=os.getcwd())
    models.add_argument("--client", choices=["codex", "claude"], default="codex")
    for control in (create_agent, settings, models):
        control.add_argument("--request-id", help="Stable idempotency key; reuse the same key when retrying this request.")
        control.add_argument("--wait", type=float, default=5, help="Wait up to 0–30 seconds for confirmation (default 5).")
    request_status = thread_commands.add_parser("request-status", help="Inspect an app request without repeating it.")
    request_status.add_argument("--request-id", required=True)
    request_status.add_argument("--wait", type=float, default=0)
    cancel_request = thread_commands.add_parser("cancel-request", help="Cancel an app request that has not started.")
    cancel_request.add_argument("--request-id", required=True)
    thread_close = thread_commands.add_parser("close", help="Request lifecycle closure through the Ribbon Field runtime.")
    thread_close.add_argument("--session-id", help="Caller session; defaults to AGENT_COORD_SESSION_ID or CODEX_THREAD_ID.")
    thread_close.add_argument("--after-turn", action="store_true", help="Wait for this turn to end; new input cancels a queued close. Use for your own session.")
    for name, help_text in (("close-status", "Inspect a close request without repeating it."),
                            ("cancel-close", "Cancel a queued close before execution starts.")):
        control = thread_commands.add_parser(name, help=help_text)
        control.add_argument("--session-id", help="Caller session; defaults to AGENT_COORD_SESSION_ID or CODEX_THREAD_ID.")
    thread_update = thread_commands.add_parser("update")
    thread_update.add_argument("--session-id", help="Caller session; defaults to AGENT_COORD_SESSION_ID or CODEX_THREAD_ID.")
    thread_update.add_argument("--title")
    thread_update.add_argument("--attention", choices=sorted(ATTENTION_STATES))
    for field in ("repository", "project"):
        options = thread_update.add_mutually_exclusive_group()
        options.add_argument("--" + field, dest=field + "_id", default=argparse.SUPPRESS, metavar="ID")
        options.add_argument("--no-" + field, dest=field + "_id", action="store_const", const=None, default=argparse.SUPPRESS)

    project = subcommands.add_parser("project", help="Create or list named projects independent of repositories.")
    project_commands = project.add_subparsers(dest="project_command", required=True)
    project_commands.add_parser("list")
    project_create = project_commands.add_parser("create")
    project_create.add_argument("--name", required=True)
    repository = subcommands.add_parser("repository", help="List or register Git repositories without changing a workspace.")
    repository_commands = repository.add_subparsers(dest="repository_command", required=True)
    repository_commands.add_parser("list")
    repository_add = repository_commands.add_parser("add")
    repository_add.add_argument("--path", required=True)

    begin = subcommands.add_parser(
        "begin-work", help="Declare an intended write scope and optional Beads work."
    )
    begin.add_argument("--session-id", help="Caller session; defaults to AGENT_COORD_SESSION_ID or CODEX_THREAD_ID.")
    begin.add_argument(
        "--bead",
        help="Optional claimed in-progress Beads issue for durable task identity.",
    )
    begin.add_argument("--scope", action="append", required=True)
    begin.add_argument(
        "--activity",
        choices=["planning", "implementing", "validating"],
        default="implementing",
    )
    begin.add_argument(
        "--lease-mode",
        choices=["write", "validation"],
        default="write",
        help="Reserve the declaration for editing or validation-only stability.",
    )

    end_work = subcommands.add_parser("end-work", help="Release a work declaration.")
    end_work.add_argument("--session-id", help="Caller session; defaults to AGENT_COORD_SESSION_ID or CODEX_THREAD_ID.")

    unregister = subcommands.add_parser("unregister", help="Mark a session offline.")
    unregister.add_argument("--session-id", help="Caller session; defaults to AGENT_COORD_SESSION_ID or CODEX_THREAD_ID.")

    list_parser = subcommands.add_parser("list", help="List known sessions.")
    list_parser.add_argument("--cwd", default=os.getcwd())
    list_parser.add_argument("--relevant", action="store_true")
    list_parser.add_argument("--include-offline", action="store_true")

    conflicts = subcommands.add_parser(
        "conflicts", help="Check a session's work scope."
    )
    conflicts.add_argument("--session-id", help="Caller session; defaults to AGENT_COORD_SESSION_ID or CODEX_THREAD_ID.")

    send = subcommands.add_parser("send", help="Send a durable message.")
    send.add_argument("--from-session", help="Sender; defaults to the current caller session.")
    target = send.add_mutually_exclusive_group(required=True)
    target.add_argument("--session")
    target.add_argument("--bead")
    send.add_argument(
        "--classification",
        choices=["action_required", "informational", "closure"],
        default="action_required",
    )
    reply_choice = send.add_mutually_exclusive_group(required=True)
    reply_choice.add_argument(
        "--reply-required",
        action="store_true",
        help=(
            "Choose --reply-required or --no-reply-required explicitly. "
            "To answer an existing message, use reply --message-id <id> instead."
        ),
    )
    reply_choice.add_argument(
        "--no-reply-required", dest="reply_required", action="store_false",
        help="Deliver the message without requesting a conversational reply.",
    )
    send.add_argument("--thread-id")
    send.add_argument("message")

    reply = subcommands.add_parser("reply", help="Answer a message without requesting another reply.")
    reply.add_argument("--from-session", help="Sender; defaults to the current caller session.")
    reply.add_argument("--message-id", type=int, required=True, help="Message being answered; recipient and thread are derived from it.")
    reply.add_argument("message")

    handoff = subcommands.add_parser(
        "handoff", help="Atomically transfer a whole work declaration."
    )
    handoff.add_argument("--from-session", help="Sender; defaults to the current caller session.")
    handoff.add_argument(
        "--session", "--to-session", dest="recipient_session_id", required=True
    )
    handoff.add_argument("--bead", "--target-bead", dest="target_bead_id")
    handoff.add_argument(
        "--scope",
        action="append",
        help="Optional exact repetition of every current scope; subsets are rejected.",
    )
    handoff.add_argument("--patch-label", required=True)
    handoff.add_argument("--validation-boundary", required=True)
    handoff.add_argument("--validation-responsibility", required=True)
    handoff.add_argument("--mode", required=True, choices=["write", "validation"])
    handoff.add_argument("--thread-id")

    inbox = subcommands.add_parser("inbox", help="Read durable messages.")
    inbox.add_argument("--session-id", help="Caller session; defaults to AGENT_COORD_SESSION_ID or CODEX_THREAD_ID.")
    inbox_mode = inbox.add_mutually_exclusive_group()
    inbox_mode.add_argument("--all", action="store_true", dest="include_delivered")
    inbox_mode.add_argument(
        "--unread",
        action="store_true",
        help="Show compact unacknowledged messages, including delivered messages.",
    )
    inbox.add_argument("--peek", action="store_true")
    inbox.add_argument(
        "--wait",
        action="store_true",
        help=(
            "Block until a message arrives, waiting indefinitely unless "
            "--timeout is given. Incompatible with --all."
        ),
    )
    inbox.add_argument(
        "--timeout",
        type=float,
        metavar="SECONDS",
        help="Only valid with --wait. Positive number of seconds to wait.",
    )

    acknowledge = subcommands.add_parser("ack", help="Acknowledge a message.")
    acknowledge.add_argument("--session-id", help="Caller session; defaults to AGENT_COORD_SESSION_ID or CODEX_THREAD_ID.")
    acknowledge_target = acknowledge.add_mutually_exclusive_group(required=True)
    acknowledge_target.add_argument("--message-id", type=int)
    acknowledge_target.add_argument("--all-unread", action="store_true")

    delegate = subcommands.add_parser(
        "delegate", help="Launch scoped Beads work in an Agent Coord-managed terminal worker."
    )
    delegate.add_argument("--from-session", help="Sender; defaults to the current caller session.")
    delegate.add_argument("--cwd", default=os.getcwd())
    delegate.add_argument("--bead", required=True)
    delegate.add_argument("--scope", action="append", required=True)
    delegate.add_argument(
        "--client",
        choices=["claude", "codex"],
        default="codex",
        help="Select the child client. Defaults to codex.",
    )
    delegate.add_argument(
        "--runtime",
        choices=["managed-pty", "zellij"],
        help=(
            "Host the persistent child in an Agent Coord-owned PTY (default) "
            "or a compatibility Zellij pane."
        ),
    )
    delegate.add_argument(
        "--zellij-session", help="Zellij compatibility runtime session name."
    )
    delegate.add_argument(
        "--name",
        dest="pane_name",
        metavar="WORKER_NAME",
        help="Durable worker name (and Zellij pane name when applicable).",
    )
    delegate.add_argument(
        "--floating",
        action="store_true",
        help="Use a floating pane with the Zellij compatibility runtime.",
    )
    delegate.add_argument("--width", default="90%")
    delegate.add_argument("--height", default="85%")
    delegate.add_argument(
        "--lease-mode",
        choices=["write", "validation"],
        default="write",
        help="Launch an editing worker or a validation-only worker.",
    )
    delegate.add_argument(
        "--model",
        help="Select the model for the child agent process.",
    )
    delegate.add_argument(
        "--effort",
        "--reasoning-effort",
        dest="reasoning_effort",
        metavar="LEVEL",
        help="Select child effort (model_reasoning_effort for Codex).",
    )
    delegate.add_argument(
        "--yolo",
        action="store_true",
        help="Explicitly bypass the selected client's permission safeguards.",
    )
    delegate.add_argument(
        "--bypass-hook-trust",
        action="store_true",
        help=(
            "Codex only: run enabled hooks without persisted trust after "
            "reviewing every hook in the target repository."
        ),
    )
    delegate.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and print the launch plan without mutation.",
    )
    delegate.add_argument("prompt", help="Specific instructions for the child agent.")

    delegation = subcommands.add_parser(
        "delegation", help="Inspect or finish durable delegation state."
    )
    delegation_commands = delegation.add_subparsers(
        dest="delegation_command", required=True
    )
    delegation_status = delegation_commands.add_parser(
        "status", help="Show one delegation."
    )
    delegation_status.add_argument("--delegation-id", required=True)
    delegation_list = delegation_commands.add_parser("list", help="List delegations.")
    delegation_list.add_argument("--parent-session")
    delegation_list.add_argument("--active", action="store_true")
    delegation_finish = delegation_commands.add_parser(
        "finish", help="Record a child result and notify its parent."
    )
    delegation_finish.add_argument("--delegation-id", required=True)
    delegation_finish.add_argument("--session-id", help="Caller session; defaults to AGENT_COORD_SESSION_ID or CODEX_THREAD_ID.")
    delegation_finish.add_argument(
        "--outcome", required=True, choices=["completed", "failed"]
    )
    delegation_finish.add_argument("--message", required=True)
    delegation_cancel = delegation_commands.add_parser(
        "cancel", help="Mark an active delegation as failed from its parent."
    )
    delegation_cancel.add_argument("--delegation-id", required=True)
    delegation_cancel.add_argument("--from-session", help="Sender; defaults to the current caller session.")
    delegation_cancel.add_argument("--message", required=True)
    delegation_supervise = delegation_commands.add_parser(
        "supervise", help="Run a managed PTY delegation supervisor."
    )
    delegation_supervise.add_argument("--delegation-id", required=True)
    delegation_supervise.add_argument("--poll-interval", type=float, default=0.25)
    delegation_logs = delegation_commands.add_parser(
        "logs", help="Read recent persisted delegation output."
    )
    delegation_logs.add_argument("--delegation-id", required=True)
    delegation_logs.add_argument("--tail-bytes", type=int, default=64 * 1024)

    wake_zellij = subcommands.add_parser(
        "wake-zellij", help="Manage opt-in wake-up for a Zellij agent pane."
    )
    wake_commands = wake_zellij.add_subparsers(dest="wake_command", required=True)
    wake_enable = wake_commands.add_parser(
        "enable", help="Register this pane and start its detached wake watcher."
    )
    wake_enable.add_argument("--session-id", help="Caller session; defaults to AGENT_COORD_SESSION_ID or CODEX_THREAD_ID.")
    wake_enable.add_argument("--zellij-session")
    wake_enable.add_argument("--pane-id")
    wake_status = wake_commands.add_parser("status", help="Show wake registration.")
    wake_status.add_argument("--session-id", help="Caller session; defaults to AGENT_COORD_SESSION_ID or CODEX_THREAD_ID.")
    wake_disable = wake_commands.add_parser(
        "disable", help="Disable wake-up for a session."
    )
    wake_disable.add_argument("--session-id", help="Caller session; defaults to AGENT_COORD_SESSION_ID or CODEX_THREAD_ID.")
    wake_watch = wake_commands.add_parser(
        "watch", help="Run the receiver-side watcher in the foreground."
    )
    wake_watch.add_argument("--session-id", help="Caller session; defaults to AGENT_COORD_SESSION_ID or CODEX_THREAD_ID.")
    wake_watch.add_argument("--once", action="store_true")
    wake_watch.add_argument("--poll-interval", type=float, default=0.5)

    ui = subcommands.add_parser(
        "ui", help="Create and manage Codex browser sessions and monitor coordination."
    )
    ui.add_argument("--host", default="127.0.0.1", help="Loopback bind address.")
    ui.add_argument("--port", type=int, default=8765, help="Local HTTP port.")
    ui.add_argument("--tailscale", "--tailscale-serve", action="store_true", help="Enable private Tailscale HTTPS; pair devices from the local UI.")
    ui.add_argument("--tailscale-port", type=int, default=443, help="Tailscale HTTPS port (default: 443).")
    ui.add_argument(
        "--parent-session", help="Only show this parent and its delegations."
    )
    ui.add_argument(
        "--cwd",
        "--repo",
        dest="ui_cwd",
        metavar="PATH",
        help="Show workspaces in this directory and its descendants, including repository worktrees.",
    )
    ui.add_argument(
        "--no-browser", action="store_true", help="Do not open the local URL."
    )
    navigation = ui.add_subparsers(dest="ui_command")
    for name in ("link", "open"):
        target = navigation.add_parser(name, help="Get an app link." if name == "link" else "Launch or focus the macOS app at a view.")
        for key in ("project", "repository"):
            choices = target.add_mutually_exclusive_group()
            choices.add_argument("--" + key, help="Exact name or ID" + (", or repository root path." if key == "repository" else "."))
            choices.add_argument("--no-" + key, dest=key, action="store_const", const="__none__")
        target.add_argument("--view", help="Saved view name or ID (or All work).")
        target.add_argument("--thread", help="Thread/session ID.")
        if name == "open":
            target.add_argument("--from-session", help="Target the window that sent this session's latest message.")
            target.add_argument("--wait", type=float, default=5, metavar="SECONDS", help="Wait up to 0–30 seconds for display acknowledgement (default 5).")
    for mutation in (register, activity, checkpoint, thread_update, begin, end_work, unregister, send, reply, acknowledge):
        mutation.add_argument("--full", action="store_true", help="Return the complete result instead of a compact receipt.")
    return parser


def run(arguments: argparse.Namespace) -> Any:
    command = arguments.command
    if command == "install-cli":
        return install_cli(arguments.bin_dir)
    for field in ("session_id", "from_session"):
        if hasattr(arguments, field) and not getattr(arguments, field):
            # ui open's origin is an optional window hint, not a required caller.
            identity = caller_session()
            settings_read = (command == "thread" and arguments.thread_command == "settings"
                             and all(getattr(arguments, key) is None for key in ("model", "effort", "yolo")))
            if not identity and command != "ui" and not settings_read:
                flag = "--" + field.replace("_", "-")
                raise CoordinationError(f"No caller session. Pass {flag}, or set AGENT_COORD_SESSION_ID (Codex also supports CODEX_THREAD_ID).")
            setattr(arguments, field, identity)
    store = CoordinationStore(arguments.db)
    if command == "register":
        return store.register(
            session_id=arguments.session_id,
            client=arguments.client,
            cwd=find_repository_root(arguments.cwd),
            name=arguments.name,
        )
    if command == "status":
        return store.get_session(arguments.session_id)
    if command == "set-activity":
        return store.touch(arguments.session_id, arguments.activity)
    if command == "checkpoint":
        raw = sys.stdin.read(100001) if arguments.checkpoint_json == "-" else arguments.checkpoint_json
        if len(raw) > 100000:
            raise CoordinationError("Checkpoint JSON is too large.")
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise CoordinationError("Checkpoint must be valid JSON.") from exc
        return store.threads.checkpoint(arguments.session_id, payload)
    if command == "thread":
        if arguments.thread_command in {"create", "settings", "models", "request-status", "cancel-request"}:
            control = AppControl(store)
            operation = arguments.thread_command
            if operation == "cancel-request":
                return control.cancel(arguments.request_id)
            if not 0 <= arguments.wait <= 30:
                raise CoordinationError("Wait must be between 0 and 30 seconds.")
            if operation == "request-status":
                return control.status(arguments.request_id, wait=arguments.wait)
            if operation == "settings":
                payload = {key: getattr(arguments, key) for key in ("model", "effort", "yolo") if getattr(arguments, key) is not None}
                if not payload:
                    return control.settings(arguments.session_id)
            else:
                payload = {"cwd": arguments.cwd, "client": arguments.client}
                if operation == "create":
                    prompt = sys.stdin.read(100001) if arguments.prompt == "-" else arguments.prompt
                    payload.update(name=arguments.name, prompt=prompt, yolo=arguments.yolo,
                                   reply_required=arguments.reply_required)
                    payload.update({key: getattr(arguments, key) for key in ("model", "effort") if getattr(arguments, key) is not None})
            receipt = control.request(operation, arguments.from_session, payload,
                                      thread_id=getattr(arguments, "session_id", None), request_id=arguments.request_id)
            return control.status(receipt["request_id"], wait=arguments.wait)
        associations = {key: getattr(arguments, key) for key in ("repository_id", "project_id") if hasattr(arguments, key)}
        if arguments.thread_command == "list":
            return store.threads.list(archived=arguments.archived, cwd=arguments.cwd, **associations)
        if arguments.thread_command == "search":
            from .thread_search import search_threads
            return search_threads(store, arguments.query, limit=arguments.limit, cursor=arguments.cursor,
                                  cwd=arguments.cwd, app_only=arguments.app_only, source=arguments.source,
                                  my_messages=arguments.my_messages, **associations)
        store.threads.ensure(arguments.session_id)
        if arguments.thread_command == "show":
            return store.threads.get(arguments.session_id, history=True)
        if arguments.thread_command in {"close", "close-status", "cancel-close"}:
            control = ThreadControl(store)
            if arguments.thread_command == "close":
                return control.request_close(arguments.session_id, after_turn=arguments.after_turn)
            if arguments.thread_command == "cancel-close":
                return control.cancel(arguments.session_id)
            return control.status(arguments.session_id)
        return store.threads.update(arguments.session_id, title=arguments.title, attention=arguments.attention, **associations)
    if command == "project":
        if arguments.project_command == "create":
            return store.threads.organization.create_project(arguments.name)
        return store.threads.organization.list()["projects"]
    if command == "repository":
        if arguments.repository_command == "add":
            return store.threads.organization.add_repository(arguments.path)
        return store.threads.organization.list()["repositories"]
    if command == "begin-work":
        session = store.get_session(arguments.session_id)
        if arguments.bead is not None:
            validate_claimed_bead(arguments.bead, session["cwd"])
        return store.begin_work(
            session_id=arguments.session_id,
            scopes=arguments.scope,
            bead_id=arguments.bead,
            activity=arguments.activity,
            lease_mode=arguments.lease_mode,
        )
    if command == "end-work":
        return store.end_work(arguments.session_id)
    if command == "unregister":
        return store.end_session(arguments.session_id)
    if command == "list":
        return store.list_sessions(
            cwd=find_repository_root(arguments.cwd),
            relevant_only=arguments.relevant,
            include_offline=arguments.include_offline,
        )
    if command == "conflicts":
        return {"conflicts": store.check_conflicts(arguments.session_id)}
    if command == "send":
        return store.send_message(
            sender_session_id=arguments.from_session,
            recipient_session_id=arguments.session,
            recipient_bead_id=arguments.bead,
            body=arguments.message,
            classification=arguments.classification,
            thread_id=arguments.thread_id,
            reply_required=arguments.reply_required,
        )
    if command == "reply":
        return store.reply_message(
            sender_session_id=arguments.from_session,
            message_id=arguments.message_id,
            body=arguments.message,
        )
    if command == "handoff":
        sender = store.get_session(arguments.from_session)
        target_bead_id = arguments.target_bead_id or sender["bead_id"]
        if target_bead_id is None:
            raise CoordinationError(
                f"Session {arguments.from_session} has no work declaration to hand off."
            )
        if target_bead_id != sender["bead_id"]:
            validate_claimed_bead(target_bead_id, sender["cwd"])
        return store.handoff_work(
            sender_session_id=arguments.from_session,
            recipient_session_id=arguments.recipient_session_id,
            target_bead_id=target_bead_id,
            scopes=arguments.scope,
            patch_label=arguments.patch_label,
            validation_boundary=arguments.validation_boundary,
            validation_responsibility=arguments.validation_responsibility,
            mode=arguments.mode,
            thread_id=arguments.thread_id,
        )
    if command == "inbox":
        if arguments.timeout is not None and not arguments.wait:
            raise CoordinationError("--timeout requires --wait.")
        if arguments.wait and arguments.unread:
            raise CoordinationError("--wait cannot be combined with --unread.")
        if arguments.wait:
            return store.inbox_wait(
                arguments.session_id,
                timeout_seconds=arguments.timeout,
                include_delivered=arguments.include_delivered,
                mark_delivered=not arguments.peek,
            )
        return store.inbox(
            arguments.session_id,
            include_delivered=arguments.include_delivered,
            mark_delivered=not arguments.peek,
            unread_only=arguments.unread,
        )
    if command == "ack":
        if arguments.all_unread:
            return store.acknowledge_all_unread(arguments.session_id)
        assert arguments.message_id is not None
        return store.acknowledge(arguments.session_id, arguments.message_id)
    if command == "delegate":
        return delegate_work(
            store,
            parent_session_id=arguments.from_session,
            cwd=arguments.cwd,
            bead_id=arguments.bead,
            scopes=arguments.scope,
            instructions=arguments.prompt,
            zellij_session=arguments.zellij_session,
            pane_name=arguments.pane_name,
            floating=arguments.floating,
            width=arguments.width,
            height=arguments.height,
            client=arguments.client,
            yolo=arguments.yolo,
            bypass_hook_trust=arguments.bypass_hook_trust,
            model=arguments.model,
            reasoning_effort=arguments.reasoning_effort,
            lease_mode=arguments.lease_mode,
            runtime=arguments.runtime,
            dry_run=arguments.dry_run,
        )
    if command == "delegation":
        if arguments.delegation_command == "status":
            return store.get_delegation(arguments.delegation_id)
        if arguments.delegation_command == "list":
            return store.list_delegations(
                parent_session_id=arguments.parent_session,
                include_terminal=not arguments.active,
            )
        if arguments.delegation_command == "finish":
            return store.finish_delegation(
                arguments.delegation_id,
                child_session_id=arguments.session_id,
                outcome=arguments.outcome,
                message=arguments.message,
            )
        if arguments.delegation_command == "cancel":
            return store.cancel_delegation(
                arguments.delegation_id,
                parent_session_id=arguments.from_session,
                reason=arguments.message,
            )
        if arguments.delegation_command == "supervise":
            return supervise_managed_pty(
                store,
                arguments.delegation_id,
                poll_interval_seconds=arguments.poll_interval,
            )
        if arguments.delegation_command == "logs":
            return {
                "delegation_id": arguments.delegation_id,
                "output": read_delegation_output(
                    store,
                    arguments.delegation_id,
                    max_bytes=arguments.tail_bytes,
                ),
            }
        raise AssertionError(
            f"Unhandled delegation command: {arguments.delegation_command}"
        )
    if command == "wake-zellij":
        if arguments.wake_command == "enable":
            return enable_zellij_wake(
                store,
                arguments.session_id,
                zellij_session=arguments.zellij_session,
                pane_id=arguments.pane_id,
            )
        if arguments.wake_command == "status":
            return store.get_zellij_wake(arguments.session_id)
        if arguments.wake_command == "disable":
            disabled = store.disable_zellij_wake(arguments.session_id)
            return disabled or {
                "session_id": arguments.session_id,
                "enabled": False,
            }
        if arguments.wake_command == "watch":
            return watch_zellij(
                store,
                arguments.session_id,
                once=arguments.once,
                poll_interval_seconds=arguments.poll_interval,
            )
        raise AssertionError(f"Unhandled wake command: {arguments.wake_command}")
    if command == "ui":
        if arguments.ui_command:
            navigation = NavigationStore(store)
            target = navigation.link(project=arguments.project, repository=arguments.repository,
                                     view=arguments.view, thread=arguments.thread)
            if arguments.ui_command == "link":
                return {**target, "status": "link"}
            return navigation.open(target, from_session=arguments.from_session, wait=arguments.wait)
        return serve_ui(
            store,
            host=arguments.host,
            port=arguments.port,
            parent_session_id=arguments.parent_session,
            cwd=(str(Path(arguments.ui_cwd).expanduser().resolve()) if arguments.ui_cwd else None),
            open_browser=not arguments.no_browser,
            tailscale=arguments.tailscale,
            tailscale_port=arguments.tailscale_port,
        )
    raise AssertionError(f"Unhandled command: {command}")


def mutation_receipt(arguments, result):
    """Keep reads and rich workflow results intact; summarize routine writes."""
    if not hasattr(arguments, "full") or arguments.full:
        return result
    receipt = {"status": "ok", "command": arguments.command}
    if isinstance(result, dict):
        fields = ("session_id", "thread_id", "id", "activity", "bead_id", "write_scope", "lease_mode",
                  "title", "attention", "project_id", "repository_id", "recipient_session_id",
                  "classification", "reply_required", "in_reply_to", "acknowledged_at", "acknowledged", "message_ids", "sender_session_id")
        receipt.update({key: result[key] for key in fields if key in result})
        if arguments.command == "checkpoint":
            checkpoint = result["checkpoint"]
            receipt.update(checkpoint_id=checkpoint["id"], phase=checkpoint["phase"])
    elif isinstance(result, list):
        receipt["count"] = len(result)
    return receipt


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        result = run(arguments)
    except ConflictError as exc:
        print(
            json.dumps({"error": str(exc), "conflicts": exc.conflicts}), file=sys.stderr
        )
        return 3
    except AmbiguousTargetError as exc:
        print(
            json.dumps(
                {"error": str(exc), "bead_id": exc.bead_id, "sessions": exc.sessions}
            ),
            file=sys.stderr,
        )
        return 4
    except InboxTimeoutError as exc:
        print(
            json.dumps(
                {
                    "error": str(exc),
                    "session_id": exc.session_id,
                    "timeout_seconds": exc.timeout_seconds,
                }
            ),
            file=sys.stderr,
        )
        return 5
    except CoordinationError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(mutation_receipt(arguments, result), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
