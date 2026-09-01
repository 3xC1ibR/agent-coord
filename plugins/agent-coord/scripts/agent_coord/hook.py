from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from .cli import find_repository_root
from .store import (
    CoordinationError,
    CoordinationStore,
    normalize_target_path,
    path_is_in_scope,
)
from .usage import capture_delegation_usage
from .zellij_wake import enable_from_environment

WRITE_TOOLS = {"apply_patch", "Edit", "Write"}
PATCH_PATH = re.compile(
    r"^\*\*\* (?:Add|Update|Delete|Move to) File: (.+)$", re.MULTILINE
)


def _client() -> str:
    configured = os.environ.get("AGENT_COORD_CLIENT")
    if configured in {"claude", "codex"}:
        return configured
    return "codex" if os.environ.get("PLUGIN_ROOT") else "claude"


def _context(event: str, text: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": text,
        }
    }


def _deny(event: str, reason: str, context: str | None = None) -> dict[str, Any]:
    output: dict[str, Any] = {
        "hookEventName": event,
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }
    if context:
        output["additionalContext"] = context
    return {"hookSpecificOutput": output}


def _format_messages(messages: list[dict[str, Any]]) -> str:
    if not messages:
        return ""
    lines = ["Unread actionable agent-coordination messages:"]
    for message in messages:
        sender = message["sender_name"] or message["sender_session_id"]
        bead = f" for {message['sender_bead_id']}" if message["sender_bead_id"] else ""
        lines.append(
            f"- Message #{message['id']} (thread {message['thread_id']}, "
            f"reply_required={str(message['reply_required']).lower()}) from "
            f"{sender}{bead}: {message['body']}"
        )
    lines.append(
        "Reply conversationally only where reply_required=true. Transport "
        "acknowledgements are silent."
    )
    return "\n".join(lines)


def _actionable_context(store: CoordinationStore, session_id: str) -> str:
    return _format_messages(
        store.inbox(session_id, classifications={"action_required"})
    )


def _extract_paths(payload: dict[str, Any], cwd: str) -> tuple[list[str], bool]:
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return [], False
    values: list[str] = []
    for key in ("file_path", "path"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            values.append(value)
    command = tool_input.get("command")
    if isinstance(command, str):
        values.extend(PATCH_PATH.findall(command))
    in_repo: set[str] = set()
    has_outside = False
    for value in values:
        try:
            in_repo.add(normalize_target_path(value, cwd))
        except CoordinationError:
            # Paths outside the registered repository are outside Agent Coord
            # ownership; they are reported so the gate can pass them through.
            has_outside = True
    return sorted(in_repo), has_outside


def _ensure_registered(
    store: CoordinationStore, payload: dict[str, Any]
) -> dict[str, Any]:
    session_id = payload.get("session_id")
    cwd = payload.get("cwd") or os.getcwd()
    if not isinstance(session_id, str) or not session_id:
        raise CoordinationError("Hook input does not contain a session_id.")
    return store.register(
        session_id=session_id,
        client=_client(),
        cwd=find_repository_root(str(cwd)),
    )


def _capture_terminal_delegation_usage(
    store: CoordinationStore, payload: dict[str, Any], session_id: str
) -> None:
    inherited_id = os.environ.get("AGENT_COORD_DELEGATION_ID")
    try:
        delegations = [
            item
            for item in store.list_delegations()
            if item["child_session_id"] == session_id
            and item["status"] in {"completed", "failed"}
            and item["token_usage"] is None
            and (inherited_id is None or item["delegation_id"] == inherited_id)
        ]
    except Exception:  # noqa: BLE001 -- Telemetry cannot break lifecycle hooks.
        return
    if not delegations:
        return
    capture_event = str(payload["hook_event_name"])
    transcript_path = payload.get("transcript_path")
    model = payload.get("model")
    active_model = model if isinstance(model, str) and model else None
    for delegation in delegations:
        if not isinstance(transcript_path, str) or not transcript_path:
            try:
                store.record_delegation_token_usage(
                    delegation["delegation_id"],
                    child_session_id=session_id,
                    usage=None,
                    capture_event=capture_event,
                    error="Hook input did not include a transcript_path.",
                )
            except Exception:  # noqa: BLE001
                pass
            continue
        try:
            capture_delegation_usage(
                store,
                delegation=delegation,
                session_id=session_id,
                transcript_path=transcript_path,
                capture_event=capture_event,
                model=active_model,
            )
        except Exception as exc:  # noqa: BLE001
            # Usage telemetry must never break the coordination lifecycle.
            try:
                store.record_delegation_token_usage(
                    delegation["delegation_id"],
                    child_session_id=session_id,
                    usage=None,
                    capture_event=capture_event,
                    error=f"Token usage capture failed: {exc}",
                )
            except Exception:  # noqa: BLE001
                pass


def handle(
    payload: dict[str, Any], store: CoordinationStore | None = None
) -> dict[str, Any]:
    coordination = store or CoordinationStore()
    event = payload.get("hook_event_name")
    if not isinstance(event, str):
        raise CoordinationError("Hook input does not contain hook_event_name.")
    session = _ensure_registered(coordination, payload)
    session_id = session["session_id"]

    if event == "SessionEnd":
        client_label = "Claude Code" if session["client"] == "claude" else "Codex"
        coordination.fail_active_delegations_for_child(
            session_id,
            f"The delegated {client_label} session ended before it reported a result.",
        )
        _capture_terminal_delegation_usage(coordination, payload, session_id)
        coordination.disable_wake(session_id)
        coordination.end_session(session_id)
        return {}

    if event == "Stop":
        unfinished = (
            bool(session["write_scope"])
            or session["scope_required"]
            or session["activity"]
            in {"planning", "implementing", "validating", "waiting"}
        )
        activity = "waiting" if unfinished else "idle"
        coordination.touch(session_id, activity, turn_active=False)
        _capture_terminal_delegation_usage(coordination, payload, session_id)
        return {}

    if event == "UserPromptSubmit":
        if session["bead_id"] is None and session["activity"] == "idle":
            coordination.touch(session_id, "discussing", turn_active=True)
        else:
            coordination.touch(session_id, turn_active=True)
        text = _actionable_context(coordination, session_id)
        return _context(event, text) if text else {}

    if event == "SessionStart":
        coordination.touch(session_id, turn_active=False)
        cli_path = Path(__file__).resolve().parents[1] / "agent-coord"
        text = (
            f"This session is registered with agent-coord as {session_id}. "
            f"The bundled CLI is {cli_path}. Use the agent-coordination skill "
            "before implementation. A sole active session may write without a "
            "Beads issue or scope. When another session is active, declare the "
            "smallest write scope; a Beads issue is optional for direct work."
        )
        delegation_warning = None
        delegation_id = os.environ.get("AGENT_COORD_DELEGATION_ID")
        if delegation_id:
            try:
                delegation = coordination.attach_delegation(
                    delegation_id, session_id
                )
            except CoordinationError as exc:
                delegation_warning = str(exc)
            else:
                scopes = ", ".join(delegation["write_scope"])
                text += (
                    f" This session is attached to delegation {delegation_id} "
                    f"for Bead {delegation['bead_id']}. Authorized scopes: "
                    f"{scopes}."
                )
                if delegation["runtime_kind"] == "managed-pty":
                    text += (
                        " Its persistent runtime and inbox wake are owned by "
                        "Agent Coord."
                    )
        if delegation_warning:
            text += f" Delegation attachment failed: {delegation_warning}"
        wake_status = None
        wake_warning = None
        try:
            wake_status = enable_from_environment(coordination, session_id)
        except (CoordinationError, OSError) as exc:
            wake_warning = str(exc)
        if wake_status:
            text += (
                " Zellij wake is enabled for "
                f"{wake_status['zellij_session']}/{wake_status['pane_id']}."
            )
        elif wake_warning:
            text += f" Zellij wake could not start: {wake_warning}"
        formatted = _actionable_context(coordination, session_id)
        if formatted:
            text += "\n\n" + formatted
        return _context(event, text)

    coordination.touch(session_id, turn_active=True)
    tool_name = payload.get("tool_name")

    if event == "PreToolUse" and tool_name in WRITE_TOOLS:
        session = coordination.get_session(session_id)
        paths, has_outside = _extract_paths(payload, session["cwd"])
        if not paths and has_outside:
            # Every target is outside the registered repository, which Agent
            # Coord does not own; the write passes through uncoordinated.
            message_context = _actionable_context(coordination, session_id)
            if message_context:
                return _context(event, message_context)
            return {}

        if not session["write_scope"]:
            blockers = coordination.scope_blocking_peers(session_id)
            if not blockers:
                if session["scope_required"]:
                    coordination.clear_scope_requirement(session_id)
                coordination.touch(session_id, "implementing", turn_active=True)
                message_context = _actionable_context(coordination, session_id)
                return _context(event, message_context) if message_context else {}

            unscoped_blockers = [peer for peer in blockers if not peer["write_scope"]]
            if not session["scope_required"]:
                coordination.request_scope_declarations(
                    session_id,
                    [peer["session_id"] for peer in unscoped_blockers],
                )
            coordination.touch(session_id, "waiting", turn_active=True)
            owners = ", ".join(
                peer["name"] or peer["session_id"] for peer in blockers
            )
            message_context = _actionable_context(coordination, session_id)
            next_action = (
                "Wait for the unscoped incumbent(s) to declare, then run "
                if unscoped_blockers
                else "Run "
            )
            return _deny(
                event,
                "Another active session requires write-scope coordination: "
                f"{owners}. {next_action}agent-coord begin-work --session-id "
                f"{session_id} "
                "--scope '<path-or-glob>' before editing. --bead is optional. "
                "Unscoped blocking peers have been asked to declare their scopes.",
                message_context or None,
            )
        message_context = _actionable_context(coordination, session_id)
        if session["lease_mode"] == "validation":
            return _deny(
                event,
                "This declaration is a validation lease; explicit write tools "
                "are disabled until a write lease is acquired.",
                message_context or None,
            )
        if session["activity"] not in {"implementing", "validating"}:
            return _deny(
                event,
                f"Session activity is {session['activity']}; set it to implementing or validating before editing.",
                message_context or None,
            )
        if not paths:
            return _deny(
                event,
                "The write target could not be determined from the tool input.",
                message_context or None,
            )
        out_of_scope = [
            path
            for path in paths
            if not any(
                path_is_in_scope(path, scope) for scope in session["write_scope"]
            )
        ]
        if out_of_scope:
            return _deny(
                event,
                "Write target is outside the declared scope: "
                + ", ".join(out_of_scope),
                message_context or None,
            )
        conflicts = coordination.check_conflicts(session_id)
        if conflicts:
            owners = ", ".join(
                conflict["name"] or conflict["session_id"] for conflict in conflicts
            )
            return _deny(
                event,
                f"Declared work conflicts with live session(s): {owners}.",
                message_context or None,
            )

    message_context = _actionable_context(coordination, session_id)
    if message_context:
        return _context(event, message_context)
    return {}


def main() -> int:
    payload: Any = None
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise CoordinationError("Hook input must be a JSON object.")
        result = handle(payload)
    except Exception as exc:  # noqa: BLE001 -- Hooks must return JSON, not a traceback.
        event = "PreToolUse"
        tool_name = None
        if isinstance(payload, dict):
            event = str(payload.get("hook_event_name") or event)
            tool_name = payload.get("tool_name")
        if event == "PreToolUse" and tool_name in WRITE_TOOLS:
            result = _deny(event, f"Agent coordination check failed closed: {exc}")
        else:
            result = {"systemMessage": f"Agent coordination hook warning: {exc}"}
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
