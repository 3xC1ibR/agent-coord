from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from .cli import find_repository_root
from .context import command_prefix, coordination_bootstrap, persist_claude_environment
from .store import (
    CoordinationError,
    CoordinationStore,
    normalize_target_path,
)
from .usage import capture_delegation_usage
from .write_policy import check_write
from .zellij_wake import WAKE_PROMPT, enable_from_environment

WRITE_TOOLS = {"apply_patch", "Edit", "Write"}
PATCH_PATH = re.compile(
    r"^\*\*\* (?:(?:Add|Update|Delete) File|Move to): (.+)$", re.MULTILINE
)


def _client() -> str:
    configured = os.environ.get("AGENT_COORD_CLIENT")
    if configured in {"claude", "codex", "kimi", "antigravity", "muse"}:
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
    lines = [
        "Delivered actionable agent-coordination messages (full requests below). "
        "These requests are already included in your context; handle them directly "
        "without fetching the inbox again. If needed request text is missing, "
        "recover it with agent-coord inbox --unread (plain inbox only returns "
        "undelivered messages)."
    ]
    for message in messages:
        sender = message["sender_name"] or message["sender_session_id"]
        bead = f" for {message['sender_bead_id']}" if message["sender_bead_id"] else ""
        lines.append(
            f"- Message #{message['id']} (thread {message['thread_id']}, "
            f"reply_required={str(message['reply_required']).lower()}) from "
            f"{sender}{bead}: {message['body']}"
        )
        if message.get("context_lifecycle") is not None:
            lines.append("  Context lifecycle (result metadata): " + json.dumps(message["context_lifecycle"]))
    lines.append(
        "For handled messages you do not reply to, acknowledge silently with agent-coord ack --message-id <id>. "
        "Answer the user directly when requested; keep receipt bookkeeping out of "
        "the answer unless asked. Reply to the sender only where reply_required=true, "
        "using agent-coord reply --message-id <id> '<response>'; this delivers the "
        "reply without requesting another and automatically acknowledges the original message. "
        "For new messages, send requires --reply-required or --no-reply-required."
    )
    return "\n".join(lines)


def _actionable_context(store: CoordinationStore, session_id: str, event: str = "UserPromptSubmit") -> str:
    # Kimi consumes appended context only at prompt submission. Its tool hooks
    # enforce denials or send notifications, but ignore appended messages.
    if _client() == "kimi" and event != "UserPromptSubmit":
        return ""
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


def _tool_label(payload: dict[str, Any], cwd: str, tool_name: str) -> str:
    """A short, path-relative description of a tool call for the overview."""
    tool_input = payload.get("tool_input")
    if tool_name in WRITE_TOOLS or tool_name == "NotebookEdit":
        paths, _ = _extract_paths(payload, cwd)
        if paths:
            return paths[0]
    if not isinstance(tool_input, dict):
        return ""
    for key in ("command", "file_path", "notebook_path", "path", "pattern", "query", "url"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            first = value.strip().splitlines()[0]
            return " ".join(first.split())
    return ""


def _record_tool(
    store: CoordinationStore, session_id: str, cwd: str, tool_name: str, payload: dict[str, Any]
) -> None:
    # Activity is a free signal for the overview; it must never block a tool call.
    try:
        store.record_tool(session_id, tool_name, _tool_label(payload, cwd, tool_name))
    except Exception:  # noqa: BLE001 -- best effort by design.
        return


def _ensure_registered(
    store: CoordinationStore, payload: dict[str, Any]
) -> dict[str, Any]:
    session_id = (os.environ.get("AGENT_COORD_SESSION_ID") if _client() in {"kimi", "muse"} else None) or payload.get("session_id")
    cwd = payload.get("cwd") or os.getcwd()
    if not isinstance(session_id, str) or not session_id:
        raise CoordinationError("Hook input does not contain a session_id.")
    return store.register(
        session_id=session_id,
        client=_client(),
        cwd=find_repository_root(str(cwd)),
        project_cwd=str(cwd),
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


def _capture_thread_usage(store, payload, session_id):
    """Ordinary terminal sessions also get durable usage; preserve delegation capture."""
    try:
        if store.threads.is_browser_session(session_id):
            return  # App adapters own their native identities and telemetry.
        if store.get_session(session_id)["client"] not in {"codex", "claude"}:
            return
        path = payload.get("transcript_path")
        if path is not None and (not isinstance(path, str) or not path):
            return
        store.usage.backfill(session_id, transcript_path=path)
    except Exception:  # Telemetry must never block Stop or SessionEnd.
        return


def handle(
    payload: dict[str, Any], store: CoordinationStore | None = None
) -> dict[str, Any]:
    coordination = store or CoordinationStore()
    if _client() == "muse":
        payload = dict(payload)
        payload["tool_name"] = {"write_file": "Write", "edit_file": "Edit"}.get(payload.get("tool_name"), payload.get("tool_name"))
        tool_input = payload.get("tool_input")
        if payload.get("tool_name") == "apply_patch":
            if isinstance(tool_input, str):
                payload["tool_input"] = {"command": tool_input}
            elif isinstance(tool_input, dict):
                payload["tool_input"] = {**tool_input, "command": tool_input.get("patch", tool_input.get("input", tool_input.get("command")))}
    event = payload.get("hook_event_name")
    if not isinstance(event, str):
        raise CoordinationError("Hook input does not contain hook_event_name.")
    owner = os.environ.get("AGENT_COORD_SESSION_ID")
    if _client() == "claude" and owner and payload.get("session_id") != owner:
        try:
            app_session = coordination.get_session(owner)
            app_owner = app_session["client"] == "claude" and coordination.threads.is_browser_session(owner)
        except CoordinationError:
            app_owner = False
        if app_owner:
            try:
                with coordination._connection() as db:
                    native = db.execute("SELECT native_id FROM claude_sessions WHERE thread_id=?", (owner,)).fetchone()
                native_id = native["native_id"] if native else None
            except Exception:
                native_id = None
            if native_id and native_id == payload.get("session_id"):
                # Claude may resume a native ID distinct from the stable app
                # identity. Keep its hooks/inbox attached to the app thread.
                payload = {**payload, "session_id": owner}
            else:
                # Native children (and as-yet-unverified native IDs) must not
                # create phantom threads or drain/mutate the app's lifecycle.
                if event == "PreToolUse" and payload.get("tool_name") in WRITE_TOOLS:
                    paths, outside = _extract_paths(payload, app_session["cwd"])
                    reason = check_write(coordination, owner, paths, has_outside=outside)
                    if reason:
                        return _deny(event, reason)
                return {}
    if _client() == "muse" and owner and payload.get("session_id") != owner:
        # Native reminder/subagent hooks inherit the parent's configuration.
        # They share its write scope, but must never drain its inbox or change
        # its registered workspace or turn state.
        if event == "PreToolUse" and payload.get("tool_name") in WRITE_TOOLS:
            session = coordination.get_session(owner)
            paths, has_outside = _extract_paths(payload, session["cwd"])
            reason = check_write(coordination, owner, paths, has_outside=has_outside)
            if reason:
                return _deny(event, reason)
        return {}
    session = _ensure_registered(coordination, payload)
    session_id = session["session_id"]

    if event == "SessionEnd":
        client_label = "Claude Code" if session["client"] == "claude" else "Codex"
        coordination.fail_active_delegations_for_child(
            session_id,
            f"The delegated {client_label} session ended before it reported a result.",
        )
        _capture_terminal_delegation_usage(coordination, payload, session_id)
        _capture_thread_usage(coordination, payload, session_id)
        coordination.disable_wake(session_id)
        coordination.end_session(session_id)
        return {}

    if event == "Stop":
        # Browser turns can still be interrupted or fail after a hook runs;
        # app-server supplies their authoritative completion and outcome.
        if not coordination.threads.is_browser_session(session_id):
            coordination.threads.finish_turn(session_id, turn_id=payload.get("turn_id"))
        unfinished = (
            bool(session["write_scope"])
            or session["scope_required"]
            or session["activity"]
            in {"planning", "implementing", "validating", "waiting"}
        )
        activity = "waiting" if unfinished else "idle"
        coordination.touch(session_id, activity, turn_active=False)
        _capture_terminal_delegation_usage(coordination, payload, session_id)
        _capture_thread_usage(coordination, payload, session_id)
        return {}

    if event == "UserPromptSubmit":
        if coordination.threads.is_browser_session(session_id):
            prompt = payload.get("prompt")
            if isinstance(prompt, str):
                coordination.threads.capture_request(session_id, prompt)
        else:
            coordination.threads.start_turn(session_id, prompt=payload.get("prompt"), turn_id=payload.get("turn_id"))
            # Browser sends own this transition; their hook may arrive after the
            # user parks an already-running turn. Automatic inbox wakes are not
            # a user returning to a terminal conversation.
            prompt = payload.get("prompt")
            if not isinstance(prompt, str) or prompt.strip() != WAKE_PROMPT:
                coordination.threads.user_message(session_id)
        if session["bead_id"] is None and session["activity"] == "idle":
            coordination.touch(session_id, "discussing", turn_active=True)
        else:
            coordination.touch(session_id, turn_active=True)
        # Codex injects CODEX_THREAD_ID into tool shells, not necessarily hooks.
        text = coordination.threads.instructions(
            session_id, caller_context=session["client"] == "codex",
            command=command_prefix(coordination.database_path),
        )
        messages = _actionable_context(coordination, session_id, event)
        if messages:
            text += "\n\n" + messages
        return _context(event, text) if text else {}

    if event == "SessionStart":
        shell_context_ready = session["client"] == "codex"
        cli_path = command_prefix(coordination.database_path)
        if session["client"] == "claude":
            shell_context_ready = persist_claude_environment(coordination.database_path, session_id)
            if shell_context_ready:
                cli_path = "agent-coord"
        coordination.touch(session_id, turn_active=False)
        text = ""
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
                bead_context = (
                    f" for Bead {delegation['bead_id']}"
                    if delegation["bead_id"] is not None else ""
                )
                text += (
                    f" This session is attached to delegation {delegation_id}{bead_context}. Authorized scopes: "
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
        # Attach inherited scopes first so startup reports the effective state.
        text = coordination_bootstrap(
            coordination, session_id, caller_context=shell_context_ready, command=cli_path
        ) + text
        formatted = _actionable_context(coordination, session_id, event)
        if formatted:
            text += "\n\n" + formatted
        return _context(event, text)

    coordination.touch(session_id, turn_active=True)
    tool_name = payload.get("tool_name")
    if event in {"PreToolUse", "PostToolUse"} and isinstance(tool_name, str) and tool_name.strip():
        _record_tool(coordination, session_id, session["cwd"], tool_name, payload)

    if event == "PreToolUse" and tool_name in WRITE_TOOLS:
        paths, has_outside = _extract_paths(payload, session["cwd"])
        reason = check_write(coordination, session_id, paths, has_outside=has_outside)
        message_context = _actionable_context(coordination, session_id, event)
        if reason:
            return _deny(event, reason, message_context or None)
        return _context(event, message_context) if message_context else {}

    message_context = _actionable_context(coordination, session_id, event)
    if message_context:
        return _context(event, message_context)
    return {}


ANTIGRAVITY_WRITE_TOOLS = {
    "write_to_file", "replace_file_content", "multi_replace_file_content",
    "create_file", "edit_file", "multi_edit_file",
}


def handle_antigravity(payload, store=None):
    """Translate Google's vetoable PreToolUse hook, never ACP notifications."""
    coordination = store or CoordinationStore()
    session_id = os.environ.get("AGENT_COORD_SESSION_ID")
    if not session_id:
        raise CoordinationError("Antigravity hook has no Ribbon Field session identity.")
    session = coordination.get_session(session_id)
    if session["client"] != "antigravity":
        raise CoordinationError("Antigravity hook identity belongs to another provider.")
    call = payload.get("toolCall")
    if not isinstance(call, dict) or call.get("name") not in ANTIGRAVITY_WRITE_TOOLS:
        raise CoordinationError("Antigravity write hook received an unknown tool.")
    args = call.get("args")
    target = args.get("TargetFile") if isinstance(args, dict) else None
    # Validate before the sole-writer fast path; malformed native inputs must fail closed.
    if not isinstance(target, str) or not target.strip():
        raise CoordinationError("Antigravity write hook has no TargetFile.")
    paths, has_outside = _extract_paths({"tool_input": {"path": target}}, session["cwd"])
    reason = check_write(coordination, session_id, paths, has_outside=has_outside)
    _record_tool(coordination, session_id, session["cwd"], call["name"], {"tool_input": {"path": target}})
    # Native PreToolUse does not inject inbox context. The adapter owns message delivery.
    return {"decision": "deny", "reason": reason} if reason else {"decision": "allow"}


def main() -> int:
    payload: Any = None
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise CoordinationError("Hook input must be a JSON object.")
        result = handle_antigravity(payload) if _client() == "antigravity" else handle(payload)
    except Exception as exc:  # noqa: BLE001 -- Hooks must return JSON, not a traceback.
        event = "PreToolUse"
        tool_name = None
        if isinstance(payload, dict):
            event = str(payload.get("hook_event_name") or event)
            tool_name = payload.get("tool_name")
        if _client() == "antigravity":
            result = {"decision": "deny", "reason": f"Agent coordination check failed closed: {exc}"}
        elif event == "PreToolUse" and (tool_name in WRITE_TOOLS or _client() == "muse" and tool_name in {"write_file", "edit_file"}):
            result = _deny(event, f"Agent coordination check failed closed: {exc}")
        else:
            result = {"systemMessage": f"Agent coordination hook warning: {exc}"}
    if _client() == "kimi":
        # Kimi consumes `message`, while Claude/Codex consume additionalContext.
        context = result.get("hookSpecificOutput", {}).get("additionalContext")
        if context:
            result["message"] = context
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
