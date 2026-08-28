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

from .delegate import delegate_work
from .store import (
    ACTIVITIES,
    AmbiguousTargetError,
    ConflictError,
    CoordinationError,
    CoordinationStore,
    InboxTimeoutError,
)
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

    register = subcommands.add_parser("register", help="Register or refresh a session.")
    register.add_argument("--session-id", required=True)
    register.add_argument("--client", required=True, choices=["claude", "codex"])
    register.add_argument("--cwd", default=os.getcwd())
    register.add_argument("--name")

    status = subcommands.add_parser("status", help="Show one session.")
    status.add_argument("--session-id", required=True)

    activity = subcommands.add_parser("set-activity", help="Set semantic activity.")
    activity.add_argument("--session-id", required=True)
    activity.add_argument("--activity", required=True, choices=sorted(ACTIVITIES))

    begin = subcommands.add_parser(
        "begin-work", help="Declare an intended write scope and optional Beads work."
    )
    begin.add_argument("--session-id", required=True)
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
    end_work.add_argument("--session-id", required=True)

    unregister = subcommands.add_parser("unregister", help="Mark a session offline.")
    unregister.add_argument("--session-id", required=True)

    list_parser = subcommands.add_parser("list", help="List known sessions.")
    list_parser.add_argument("--cwd", default=os.getcwd())
    list_parser.add_argument("--relevant", action="store_true")
    list_parser.add_argument("--include-offline", action="store_true")

    conflicts = subcommands.add_parser(
        "conflicts", help="Check a session's work scope."
    )
    conflicts.add_argument("--session-id", required=True)

    send = subcommands.add_parser("send", help="Send a durable message.")
    send.add_argument("--from-session", required=True)
    target = send.add_mutually_exclusive_group(required=True)
    target.add_argument("--session")
    target.add_argument("--bead")
    send.add_argument(
        "--classification",
        choices=["action_required", "informational", "closure"],
        default="action_required",
    )
    send.add_argument(
        "--reply-required",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Require a conversational reply. Defaults to true for action-required "
            "messages and false for informational or closure messages."
        ),
    )
    send.add_argument("--thread-id")
    send.add_argument("message")

    handoff = subcommands.add_parser(
        "handoff", help="Atomically transfer a whole work declaration."
    )
    handoff.add_argument("--from-session", required=True)
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
    inbox.add_argument("--session-id", required=True)
    inbox_mode = inbox.add_mutually_exclusive_group()
    inbox_mode.add_argument(
        "--all", action="store_true", dest="include_delivered"
    )
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
    acknowledge.add_argument("--session-id", required=True)
    acknowledge_target = acknowledge.add_mutually_exclusive_group(required=True)
    acknowledge_target.add_argument("--message-id", type=int)
    acknowledge_target.add_argument("--all-unread", action="store_true")

    delegate = subcommands.add_parser(
        "delegate", help="Launch ready Beads work in a new agent Zellij pane."
    )
    delegate.add_argument("--from-session", required=True)
    delegate.add_argument("--cwd", default=os.getcwd())
    delegate.add_argument("--bead", required=True)
    delegate.add_argument("--scope", action="append", required=True)
    delegate.add_argument(
        "--client",
        choices=["claude", "codex"],
        default="codex",
        help="Select the child client. Defaults to codex.",
    )
    delegate.add_argument("--zellij-session")
    delegate.add_argument("--name", dest="pane_name")
    delegate.add_argument("--floating", action="store_true")
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
    delegation_list = delegation_commands.add_parser(
        "list", help="List delegations."
    )
    delegation_list.add_argument("--parent-session")
    delegation_list.add_argument("--active", action="store_true")
    delegation_finish = delegation_commands.add_parser(
        "finish", help="Record a child result and notify its parent."
    )
    delegation_finish.add_argument("--delegation-id", required=True)
    delegation_finish.add_argument("--session-id", required=True)
    delegation_finish.add_argument(
        "--outcome", required=True, choices=["completed", "failed"]
    )
    delegation_finish.add_argument("--message", required=True)
    delegation_cancel = delegation_commands.add_parser(
        "cancel", help="Mark an active delegation as failed from its parent."
    )
    delegation_cancel.add_argument("--delegation-id", required=True)
    delegation_cancel.add_argument("--from-session", required=True)
    delegation_cancel.add_argument("--message", required=True)

    wake_zellij = subcommands.add_parser(
        "wake-zellij", help="Manage opt-in wake-up for a Zellij agent pane."
    )
    wake_commands = wake_zellij.add_subparsers(dest="wake_command", required=True)
    wake_enable = wake_commands.add_parser(
        "enable", help="Register this pane and start its detached wake watcher."
    )
    wake_enable.add_argument("--session-id", required=True)
    wake_enable.add_argument("--zellij-session")
    wake_enable.add_argument("--pane-id")
    wake_status = wake_commands.add_parser("status", help="Show wake registration.")
    wake_status.add_argument("--session-id", required=True)
    wake_disable = wake_commands.add_parser(
        "disable", help="Disable wake-up for a session."
    )
    wake_disable.add_argument("--session-id", required=True)
    wake_watch = wake_commands.add_parser(
        "watch", help="Run the receiver-side watcher in the foreground."
    )
    wake_watch.add_argument("--session-id", required=True)
    wake_watch.add_argument("--once", action="store_true")
    wake_watch.add_argument("--poll-interval", type=float, default=0.5)
    return parser


def run(arguments: argparse.Namespace) -> Any:
    store = CoordinationStore(arguments.db)
    command = arguments.command
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
    raise AssertionError(f"Unhandled command: {command}")


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
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
