from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

from .cli import find_repository_root
from .context import (command_prefix, coordination_bootstrap, persist_claude_environment,
                      caller_environment, caller_directory)
from .store import (
    CoordinationError,
    CoordinationStore,
    normalize_target_path,
    default_database_path,
)
from .usage import capture_delegation_usage
from .write_policy import check_write
from .zellij_wake import WAKE_PROMPT, enable_from_environment

WRITE_TOOLS = {"apply_patch", "Edit", "Write"}
PATCH_PATH = re.compile(
    r"^\*\*\* (?:(?:Add|Update|Delete) File|Move to): (.+)$", re.MULTILINE
)


def _client() -> str:
    configured = caller_environment().get("AGENT_COORD_CLIENT")
    if configured in {"claude", "codex", "kimi", "antigravity", "muse"}:
        return configured
    return "codex" if caller_environment().get("PLUGIN_ROOT") else "claude"


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


def _actionable_context(store: CoordinationStore, session_id: str, event: str = "UserPromptSubmit", payload=None) -> str:
    if _client() == "kimi" and event != "UserPromptSubmit":
        return ""
    from .message_delivery import MessageDelivery
    with store.reading(), store._connection() as db:
        row = db.execute("SELECT COALESCE(turn_id, turn_key) AS turn_id FROM work_threads WHERE thread_id=?", (session_id,)).fetchone()
    turn_id = (payload or {}).get('turn_id') or (row['turn_id'] if row else None)
    if event == 'SessionStart':
        return ''  # Wait for a prompt with an identifiable receiving turn.
    return MessageDelivery(store).hook_context(session_id, turn_id,
        boundary_id=(payload or {}).get('tool_use_id') or (payload or {}).get('tool_call_id'))


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
            # The hook's cwd locates relative tool targets; the registered cwd
            # remains the base of a live declaration even inside a nested repo.
            target = Path(value)
            if not target.is_absolute():
                target = Path(payload.get("cwd") or cwd) / target
            in_repo.add(normalize_target_path(str(target), cwd))
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
    session_id = payload.get("session_id")
    cwd = payload.get("cwd") or caller_directory()
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
    inherited_id = caller_environment().get("AGENT_COORD_DELEGATION_ID")
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


def _hook_identity(store, payload):
    """Resolve provider-native hooks to their coordinating session before any mutation."""
    owner = caller_environment().get("AGENT_COORD_SESSION_ID")
    native = payload.get("session_id") or payload.get("sessionId")
    parent = payload.get("parent_session_id") or payload.get("parent_thread_id")
    transcript = Path(str(payload.get("agent_transcript_path") or payload.get("transcript_path") or ""))
    subagent_transcript = transcript.parent.name == "subagents"
    # A top-level Claude --agent selection also supplies agent_type.
    agent = payload.get("agent_id") or payload.get("agentId")
    if parent or subagent_transcript or payload.get("parent_tool_use_id") or (agent and agent != "main"):
        transcript_owner = transcript.parent.parent.name if subagent_transcript else None
        return owner or parent or transcript_owner or native, True
    if not owner or native == owner:
        return native, False
    if native is None and _client() in {"antigravity", "kimi", "muse"}:
        return owner, False

    # Some app providers allocate a different native ID for the root itself.
    # Only a recorded binding establishes that it may consume the app inbox.
    table = {"claude": "claude_sessions", "kimi": "kimi_sessions",
             "muse": "muse_sessions", "antigravity": "antigravity_sessions"}.get(_client())
    if table and native:
        with store.reading(), store._connection() as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                bound = db.execute(f"SELECT 1 FROM {table} WHERE thread_id=? AND native_id=?", (owner, native)).fetchone()
                if bound:
                    return owner, False
    return owner, True


def _normalize_tool(payload):
    if _client() == "muse":
        payload = dict(payload)
        payload["tool_name"] = {"write_file": "Write", "edit_file": "Edit"}.get(payload.get("tool_name"), payload.get("tool_name"))
        tool_input = payload.get("tool_input")
        if payload.get("tool_name") == "apply_patch":
            if isinstance(tool_input, str):
                payload["tool_input"] = {"command": tool_input}
            elif isinstance(tool_input, dict):
                payload["tool_input"] = {**tool_input, "command": tool_input.get("patch", tool_input.get("input", tool_input.get("command")))}
    return payload


def handle(
    payload: dict[str, Any], store: CoordinationStore | None = None
) -> dict[str, Any]:
    coordination = store or CoordinationStore()
    payload = _normalize_tool(payload)
    event = payload.get("hook_event_name")
    if not isinstance(event, str):
        raise CoordinationError("Hook input does not contain hook_event_name.")
    owner, child = _hook_identity(coordination, payload)
    if child:
        if event == "PreToolUse" and payload.get("tool_name") in WRITE_TOOLS:
            from .hook_reads import find_session, unregistered_write_reason
            with coordination.reading():
                session = find_session(coordination, owner)
                cwd = session["cwd"] if session else find_repository_root(str(payload.get("cwd") or caller_directory()))
                paths, outside = _extract_paths(payload, cwd)
                if session:
                    reason = check_write(coordination, owner, paths, has_outside=outside, record=False)
                else:
                    reason = unregistered_write_reason(coordination, cwd) if paths or not outside else None
            return _deny(event, reason) if reason else {}
        return {}
    native_id = payload.get("session_id") or owner
    payload = {**payload, "session_id": owner}
    session = _ensure_registered(coordination, payload)
    session_id = session["session_id"]
    # Provider processes may fire SessionStart before thread/start responds
    # with the ID needed to insert the browser_sessions row.
    app_prompt = session["client"] in {"codex", "claude"} and caller_environment().get("AGENT_COORD_APP_PROMPT") == "1"
    browser = app_prompt or coordination.threads.is_browser_session(session_id)
    from .message_delivery import MessageDelivery
    delivery = MessageDelivery(coordination)
    with coordination.reading(), coordination._connection() as db:
        current = db.execute("SELECT COALESCE(turn_id, turn_key) AS turn_id FROM work_threads WHERE thread_id=?", (session_id,)).fetchone()
    provider_turn = payload.get('turn_id') or (current['turn_id'] if current else None)
    # Parallel tool calls can produce distinct hook IDs without another model
    # step. Only a successful turn boundary confirms hook delivery.
    if event == 'Stop' and not browser:
        delivery.finish_turn(session_id, provider_turn, completed=True)
    elif event == 'SessionEnd' and not browser:
        delivery.finish_turn(session_id, provider_turn, completed=False)

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
        if not browser:
            coordination.threads.finish_turn(session_id, turn_id=payload.get("turn_id"))
        unfinished = (
            bool(session["write_scope"])
            or session["scope_required"]
            or session["activity"]
            in {"planning", "implementing", "validating", "waiting"}
        )
        activity = "waiting" if unfinished else "idle"
        coordination.touch(session_id, activity, turn_active=None if browser else False)
        _capture_terminal_delegation_usage(coordination, payload, session_id)
        _capture_thread_usage(coordination, payload, session_id)
        return {}

    if event == "UserPromptSubmit":
        if browser:
            prompt = payload.get("prompt")
            if (isinstance(prompt, str) and prompt.strip() != WAKE_PROMPT
                    and not prompt.lstrip().startswith(("[Automatic Agent Coord inbox wake — not a human message]",
                                                       "[Automatic Agent Coord message delivery — not a human message]"))):
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
            coordination.touch(session_id, "discussing", turn_active=None if browser else True)
        else:
            coordination.touch(session_id, turn_active=None if browser else True)
        messages = _actionable_context(coordination, session_id, event, payload)
        changed = coordination.threads.observe_context(session_id)
        metadata = "Updated coordination context: " + json.dumps(changed, sort_keys=True) if changed else ""
        text = "\n\n".join(filter(None, (messages, metadata)))
        return _context(event, text) if text else {}

    if event == "SessionStart":
        shell_context_ready = session["client"] == "codex"
        cli_path = command_prefix(coordination.database_path)
        if session["client"] == "claude":
            shell_context_ready = persist_claude_environment(coordination.database_path, session_id)
            if shell_context_ready:
                cli_path = "agent-coord"
        # App-server turn events can precede this hook. Only terminal startup
        # owns the transition to idle; browser liveness belongs to the runtime.
        coordination.touch(session_id, turn_active=None if browser else False)
        text = ""
        delegation_warning = None
        delegation_id = caller_environment().get("AGENT_COORD_DELEGATION_ID")
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
            wake_status = enable_from_environment(coordination, session_id, environ=caller_environment())
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
        if browser and session["client"] in {"antigravity", "kimi", "muse"}:
            return _context(event, text.strip()) if text.strip() else {}
        startup = coordination.threads.startup_context(session_id, native_id, lambda: coordination_bootstrap(
            coordination, session_id, caller_context=shell_context_ready, command=cli_path,
            include_thread_instructions=not (browser and session["client"] in {"codex", "claude"}),
        ))
        text = startup + text
        return _context(event, text.strip()) if text.strip() else {}

    # Browser runtime events own turn liveness. Late tool/prompt hooks from an
    # interrupted turn must not resurrect it (nor may a Stop hook end a new one).
    coordination.touch(session_id, turn_active=None if browser else True)
    tool_name = payload.get("tool_name")
    if event in {"PreToolUse", "PostToolUse"} and isinstance(tool_name, str) and tool_name.strip():
        _record_tool(coordination, session_id, session["cwd"], tool_name, payload)

    if event == "PreToolUse" and tool_name in WRITE_TOOLS:
        paths, has_outside = _extract_paths(payload, session["cwd"])
        reason = check_write(coordination, session_id, paths, has_outside=has_outside)
        if reason:
            # An inbox read or reservation cannot invalidate a confirmed denial.
            try:
                message_context = _actionable_context(coordination, session_id, event, payload)
            except sqlite3.OperationalError:
                message_context = ""
            return _deny(event, reason, message_context or None)
        message_context = _actionable_context(coordination, session_id, event, payload)
        return _context(event, message_context) if message_context else {}

    message_context = _actionable_context(coordination, session_id, event, payload)
    if message_context:
        return _context(event, message_context)
    return {}


ANTIGRAVITY_WRITE_TOOLS = {
    "write_to_file", "replace_file_content", "multi_replace_file_content",
    "create_file", "edit_file", "multi_edit_file",
}


def handle_antigravity(payload, store=None, *, record=True):
    """Translate Google's vetoable PreToolUse hook, never ACP notifications."""
    from .hook_reads import find_session, unregistered_write_reason

    coordination = store or CoordinationStore()
    session_id = caller_environment().get("AGENT_COORD_SESSION_ID")
    if not session_id:
        raise CoordinationError("Antigravity hook has no Ribbon Field session identity.")
    call = payload.get("toolCall")
    if not isinstance(call, dict) or call.get("name") not in ANTIGRAVITY_WRITE_TOOLS:
        raise CoordinationError("Antigravity write hook received an unknown tool.")
    args = call.get("args")
    target = args.get("TargetFile") if isinstance(args, dict) else None
    # Validate before the sole-writer fast path; malformed native inputs must fail closed.
    if not isinstance(target, str) or not target.strip():
        raise CoordinationError("Antigravity write hook has no TargetFile.")
    _, child = _hook_identity(coordination, payload)
    record = record and not child
    session = find_session(coordination, session_id) if not record else coordination.get_session(session_id)
    if session is None:
        cwd = find_repository_root(str(payload.get("cwd") or caller_directory()))
        paths, has_outside = _extract_paths({"tool_input": {"path": target}}, cwd)
        reason = unregistered_write_reason(coordination, cwd) if paths or not has_outside else None
        return {"decision": "deny", "reason": reason} if reason else {"decision": "allow"}
    if session["client"] != "antigravity":
        raise CoordinationError("Antigravity hook identity belongs to another provider.")
    paths, has_outside = _extract_paths({"tool_input": {"path": target}}, session["cwd"])
    reason = check_write(coordination, session_id, paths, has_outside=has_outside, record=record)
    if record:
        _record_tool(coordination, session_id, session["cwd"], call["name"], {"tool_input": {"path": target}})
    # Native PreToolUse does not inject inbox context. The adapter owns message delivery.
    return {"decision": "deny", "reason": reason} if reason else {"decision": "allow"}


def _validate_antigravity_input(payload):
    session_id = caller_environment().get("AGENT_COORD_SESSION_ID")
    if not session_id:
        raise CoordinationError("Antigravity hook has no Ribbon Field session identity.")
    call = payload.get("toolCall")
    if not isinstance(call, dict) or call.get("name") not in ANTIGRAVITY_WRITE_TOOLS:
        raise CoordinationError("Antigravity write hook received an unknown tool.")
    args = call.get("args")
    target = args.get("TargetFile") if isinstance(args, dict) else None
    if not isinstance(target, str) or not target.strip():
        raise CoordinationError("Antigravity write hook has no TargetFile.")


def _read_only_pre_tool(payload, database):
    from .browser_persistence import database_busy
    from .hook_reads import hook_snapshot

    if _client() == "antigravity":
        _validate_antigravity_input(payload)
    if not database.exists():
        return {"decision": "allow"} if _client() == "antigravity" else {}

    try:
        with hook_snapshot(database) as store:
            if _client() == "antigravity":
                return handle_antigravity(payload, store, record=False)
            payload = _normalize_tool(payload)
            owner, child = _hook_identity(store, payload)
            if payload.get("tool_name") in WRITE_TOOLS:
                if not isinstance(owner, str) or not owner:
                    raise CoordinationError("Hook input does not contain a session_id.")
                session = store.find_session(owner)
                cwd = session["cwd"] if session else find_repository_root(payload.get("cwd") or caller_directory())
                paths, outside = _extract_paths(payload, cwd)
                if session:
                    reason = check_write(store, owner, paths, has_outside=outside, record=False)
                else:
                    reason = store.unregistered_write_reason(cwd) if paths or not outside else None
                if reason:
                    return _deny("PreToolUse", reason)
            if not child and _client() != "kimi":
                message_id = store.pending_message(owner)
                if message_id:
                    return _context("PreToolUse", f"Coordination message {message_id} is pending. Read agent-coord inbox --unread.")
    except sqlite3.OperationalError as error:
        if not database_busy(error):
            raise
        print(f"Agent Coord PreToolUse: lease database busy; allowing this tool call ({error}).", file=sys.stderr)
    return {"decision": "allow"} if _client() == "antigravity" else {}


def _run_hook(payload, *, pre_tool_fast_path=False):
    from .entrypoint import close_http_errors, is_transient_runtime_error
    from .runtime_requests import run_local_or_forward

    native = _client() == "antigravity"
    pre_tool = native or payload.get("hook_event_name") == "PreToolUse"
    database = default_database_path()

    if pre_tool:
        # Evaluate PreToolUse directly against state.sqlite3?mode=ro without
        # coordinator HTTP roundtrips or database writes by default.
        return _read_only_pre_tool(payload, database)

    def local():
        store = CoordinationStore(database)
        return handle(payload, store)

    try:
        return run_local_or_forward("hook", {"input": payload}, local, database, timeout=45)
    except Exception as error:
        close_http_errors(error)
        if is_transient_runtime_error(error):
            event_name = str(payload.get("hook_event_name") or "Hook")
            print(
                f"Agent Coord {event_name}: coordinator unavailable during restart; continuing ({error}).",
                file=sys.stderr,
            )
            return {}
        raise


def main(*, unavailable=None, payload=None, payload_error=None, pre_tool_fast_path=False) -> int:
    try:
        if payload_error is not None:
            raise payload_error
        if payload is None:
            payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise CoordinationError("Hook input must be a JSON object.")
        if unavailable is not None:
            if _client() == "antigravity" or payload.get("hook_event_name") == "PreToolUse":
                result = _read_only_pre_tool(payload, default_database_path())
            else:
                event_name = str(payload.get("hook_event_name") or "Hook")
                print(
                    f"Agent Coord {event_name}: coordinator unavailable during restart; continuing ({unavailable}).",
                    file=sys.stderr,
                )
                result = {}
        else:
            result = _run_hook(payload, pre_tool_fast_path=pre_tool_fast_path)
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
