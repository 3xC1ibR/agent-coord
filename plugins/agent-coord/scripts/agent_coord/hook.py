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
    lines = ["Unread agent-coordination messages:"]
    for message in messages:
        sender = message["sender_name"] or message["sender_session_id"]
        bead = f" for {message['sender_bead_id']}" if message["sender_bead_id"] else ""
        lines.append(
            f"- Message #{message['id']} from {sender}{bead}: {message['body']}"
        )
    lines.append(
        "Use the agent-coordination skill to reply and acknowledge these messages."
    )
    return "\n".join(lines)


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
        coordination.fail_active_delegations_for_child(
            session_id,
            "The delegated Codex session ended before it reported a result.",
        )
        coordination.disable_zellij_wake(session_id)
        coordination.end_session(session_id)
        return {}

    if event == "Stop":
        activity = "waiting" if session["bead_id"] else "idle"
        coordination.touch(session_id, activity, turn_active=False)
        return {}

    if event == "UserPromptSubmit":
        if session["bead_id"] is None and session["activity"] == "idle":
            coordination.touch(session_id, "discussing", turn_active=True)
        else:
            coordination.touch(session_id, turn_active=True)
        messages = coordination.inbox(session_id)
        text = _format_messages(messages)
        return _context(event, text) if text else {}

    if event == "SessionStart":
        coordination.touch(session_id, turn_active=False)
        messages = coordination.inbox(session_id)
        cli_path = Path(__file__).resolve().parents[1] / "agent-coord"
        text = (
            f"This session is registered with agent-coord as {session_id}. "
            f"The bundled CLI is {cli_path}. Before implementation, claim a "
            "Beads issue and use the agent-coordination skill to declare the "
            "intended write scope."
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
        formatted = _format_messages(messages)
        if formatted:
            text += "\n\n" + formatted
        return _context(event, text)

    coordination.touch(session_id, turn_active=True)
    messages = coordination.inbox(session_id)
    message_context = _format_messages(messages)
    tool_name = payload.get("tool_name")

    if event == "PreToolUse" and tool_name in WRITE_TOOLS:
        session = coordination.get_session(session_id)
        paths, has_outside = _extract_paths(payload, session["cwd"])
        if not paths and has_outside:
            # Every target is outside the registered repository, which Agent
            # Coord does not own; the write passes through uncoordinated.
            if message_context:
                return _context(event, message_context)
            return {}
        if session["bead_id"] is None:
            return _deny(
                event,
                f"No Beads work declaration is active for session {session_id}. "
                "Claim a bead and run agent-coord begin-work before editing.",
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
