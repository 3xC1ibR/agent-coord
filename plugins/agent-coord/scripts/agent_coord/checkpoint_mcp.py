"""Small dependency-free stdio MCP surface for thread controls."""
from __future__ import annotations

import json
import math
import sqlite3
import sys
import uuid

from .context import bind_caller, request_session
from .store import CoordinationError, CoordinationStore
from .threads import LINK_KINDS, PHASES
from .context_lifecycle import CHECKPOINT_SCHEMA


ACTIVITY_TOOL = {
    "name": "query_activity",
    "description": "Read Ribbon Field activity totals and paginated raw interaction events for retrospective analysis. "
                   "Use when the user asks to review their work or attention. Does not wake workers. "
                   "Counts describe observed human interactions, not mental attention or productivity. "
                   "Project means a directory; group means the user's named thread group. Follow next_cursor with the same filters.",
    "inputSchema": {"type": "object", "additionalProperties": False, "properties": {
        "start": {"type": "number", "description": "Inclusive Unix seconds; defaults to 24 hours ago."},
        "end": {"type": "number", "description": "Exclusive Unix seconds; defaults to now. Preserve this cutoff when paging."},
        "project": {"type": "string"}, "domain": {"type": "string"}, "group": {"type": "string"},
        "thread_id": {"type": "string"},
        "workspace": {"type": "string", "description": "Limit evidence to this workspace and its linked worktrees."},
        "group_by": {"type": "string", "enum": ["thread", "project", "domain", "group"]},
        "limit": {"type": "integer", "minimum": 1, "maximum": 200}, "cursor": {"type": "string"},
    }},
    "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
}

ACTIVITY_HISTORY_TOOL = {
    "name": "read_activity_history",
    "description": "Read saved Ribbon Field conversation prose, checkpoints and artifact links without waking a provider. "
                   "Use to investigate activity findings. Follow next_cursor to retrieve all messages and long-message chunks. "
                   "Provider user roles do not establish human origin; use activity events for counts. Unknown timestamps are included as context.",
    "inputSchema": {"type": "object", "additionalProperties": False, "required": ["thread_id"], "properties": {
        "thread_id": {"type": "string", "minLength": 1},
        "workspace": {"type": "string"},
        "start": {"type": "number"}, "end": {"type": "number"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 200}, "cursor": {"type": "string"},
    }},
    "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
}


def query_activity(arguments, *, store=None):
    from .activity import ActivityStore
    from .activity_api import query_arguments
    if not isinstance(arguments, dict):
        raise CoordinationError("Choose activity query fields.")
    fields = dict(arguments)
    workspace = fields.pop("workspace", None)
    return ActivityStore(store or CoordinationStore()).query(**query_arguments(fields), allowed_cwd=workspace)


def read_activity_history(arguments, *, store=None):
    from .activity_api import read_activity_history as read
    if (not isinstance(arguments, dict) or "thread_id" not in arguments or
            set(arguments) - ACTIVITY_HISTORY_TOOL["inputSchema"]["properties"].keys()):
        raise CoordinationError("Choose a thread ID, optional time range, limit, and cursor.")
    fields = dict(arguments)
    workspace = fields.pop("workspace", None)
    return read(store or CoordinationStore(), **fields, allowed_cwd=workspace)


TOOL = {
    "name": "update_checkpoint",
    "description": (
        "Create or update your work-thread checkpoint. Send only changed fields; omissions preserve saved values. "
        "The first checkpoint requires phase and summary. Set next_step to null to clear it. "
        "Returns only status and checkpoint_id. Caller identity must be bound by the host; "
        "use the agent-coord CLI from your current tool shell if it is unavailable."
    ),
    "inputSchema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "session_id": {"type": "string", "minLength": 1, "description": "Optional assertion of the bound caller ID; a different ID is rejected."},
            "phase": {"type": "string", "enum": sorted(PHASES)},
            "summary": {"type": "string", "minLength": 1, "maxLength": 2000},
            "next_step": {
                "description": "Required next action and its owner, or null when no next step remains.",
                "anyOf": [{"type": "null"}, {
                    "type": "object", "additionalProperties": False, "required": ["actor", "action"],
                    "properties": {"actor": {"type": "string", "enum": ["user", "agent", "external"]},
                                   "action": {"type": "string", "minLength": 1, "maxLength": 1000}},
                }],
            },
            "title": {"type": "string", "minLength": 1, "maxLength": 160},
            "domain": {"type": "string", "minLength": 1, "maxLength": 64},
            "turn_id": {"type": "string", "description": "Optional current turn ID to reject an outdated update."},
            "context_lifecycle": CHECKPOINT_SCHEMA,
            "links": {
                "type": "array", "maxItems": 100, "description": "Add or update links; existing links persist.",
                "items": {
                    "type": "object", "additionalProperties": False, "required": ["kind", "target"],
                    "properties": {"kind": {"type": "string", "enum": sorted(LINK_KINDS)},
                                   "target": {"type": "string", "minLength": 1, "maxLength": 4000},
                                   "label": {"type": "string", "maxLength": 4000},
                                   "workspace_id": {"type": "string"}},
                },
            },
        },
    },
    "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
}

MOVE_TOOL = {
    "name": "move_thread",
    "description": (
        "Move a Ribbon Field app thread to a project and change its working directory together. "
        "Use only at the user's request. Defaults to your own thread; release its work scope first. "
        "Returns a queued request that the owning app applies when idle, including after your current turn. "
        "Queued is not confirmation. Inspect with agent-coord thread request-status --request-id <id>. "
        "Codex and Claude app conversations are supported."
    ),
    "inputSchema": {
        "type": "object", "additionalProperties": False, "required": ["cwd"],
        "properties": {
            "cwd": {"type": "string", "minLength": 1, "description": "Absolute destination project directory."},
            "session_id": {"type": "string", "minLength": 1, "description": "Target thread; defaults to your caller identity."},
            "from_session": {"type": "string", "minLength": 1, "description": "Optional assertion of the bound caller ID; a different ID is rejected."},
            "request_id": {"type": "string", "minLength": 1, "maxLength": 160,
                           "description": "Stable idempotency key; reuse when retrying."},
        },
    },
    "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
}


ASK_TOOL = {
    "name": "ask_human",
    "description": (
        "Ask the user a decision only they can make. The ask appears in Ribbon Field's Decisions list; "
        "the answer arrives later as an ordinary inbox message. Choose blocking only when you cannot make "
        "useful progress without the answer, then end your turn (or set wait_seconds). A non-blocking ask "
        "must state in context the default you will proceed with. Returns the ask id and status."
    ),
    "inputSchema": {
        "type": "object", "additionalProperties": False, "required": ["question", "blocking"],
        "properties": {
            "session_id": {"type": "string", "minLength": 1, "description": "Optional assertion of the bound caller ID; a different ID is rejected."},
            "question": {"type": "string", "minLength": 1, "maxLength": 2000},
            "options": {"type": "array", "maxItems": 8, "description": "Optional answer choices; free text is always allowed.",
                        "items": {"type": "string", "minLength": 1, "maxLength": 200}},
            "blocking": {"type": "boolean", "description": "True only when no useful progress is possible without the answer."},
            "context": {"type": "string", "maxLength": 4000, "description": "Why you ask and, for non-blocking asks, the default you will use."},
            "supersedes": {"type": "integer", "minimum": 1, "description": "Withdraw your earlier open ask with this ID."},
            "wait_seconds": {"type": "number", "minimum": 1, "maximum": 300,
                             "description": "Blocking only: wait up to this long for the answer before returning."},
        },
    },
    "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
}

SCHEDULE_TOOL = {
    "name": "schedule_prompt",
    "description": (
        "Schedule a one-time prompt through the running Ribbon Field app. Use only when the user authorizes scheduling. "
        "Choose self for your own app conversation, or new for a new agent with explicit provider settings. "
        "Self uses the conversation's settings at delivery. A queued receipt is not a saved schedule; inspect "
        "agent-coord thread request-status --request-id <id>. Reuse request_id on retry. "
        "The Mac must be awake and Ribbon Field running at the scheduled time; missed runs need review. "
        "Terminal sessions cannot schedule a prompt to themselves. Only set yolo with explicit authorization."
    ),
    "inputSchema": {
        "type": "object", "additionalProperties": False, "required": ["mode", "message", "run_at", "timezone"],
        "properties": {
            "session_id": {"type": "string", "minLength": 1, "description": "Optional assertion of the bound caller ID; a different ID is rejected."},
            "mode": {"type": "string", "enum": ["self", "new"]},
            "message": {"type": "string", "minLength": 1, "maxLength": 100000},
            "run_at": {"type": "number", "description": "Future execution time as Unix epoch seconds (UTC)."},
            "timezone": {"type": "string", "minLength": 1, "description": "IANA time zone, e.g. America/Chicago."},
            "settings": {"type": "object", "additionalProperties": False,
                         "required": ["cwd", "client", "model"], "description": "Required for new; forbidden for self.",
                         "properties": {"cwd": {"type": "string", "minLength": 1},
                                        "client": {"type": "string", "enum": ["codex", "claude", "antigravity", "kimi", "muse"]},
                                        "model": {"type": "string", "minLength": 1},
                                        "effort": {"type": "string"}, "yolo": {"type": "boolean"},
                                        "project_id": {"type": "string"}, "repository_id": {"type": "string"}}},
            "request_id": {"type": "string", "minLength": 1, "maxLength": 160, "description": "Stable idempotency key; reuse on retry."},
            "wait_seconds": {"type": "number", "minimum": 0, "maximum": 30, "description": "Wait for the app to save the schedule; defaults to 5."},
        },
    },
    "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
}


def schedule_prompt(arguments, *, store=None):
    if not isinstance(arguments, dict) or set(arguments) - SCHEDULE_TOOL["inputSchema"]["properties"].keys():
        raise CoordinationError("Use the schedule_prompt input fields.")
    sender = bind_caller(arguments.get("session_id"))
    if not isinstance(sender, str) or not sender.strip():
        raise CoordinationError("No caller session. Pass session_id from your hook context.")
    mode = arguments.get("mode")
    message = arguments.get("message")
    run_at = arguments.get("run_at")
    timezone = arguments.get("timezone")
    wait = arguments.get("wait_seconds", 5)
    if not isinstance(mode, str) or mode not in {"self", "new"} or not isinstance(message, str) or not message.strip() or len(message) > 100000:
        raise CoordinationError("Choose self or new and supply a prompt of 1–100000 characters.")
    if (type(run_at) not in (int, float) or not 0 < run_at < 253402300800 or not math.isfinite(run_at)
            or not isinstance(timezone, str) or not timezone.strip()):
        raise CoordinationError("Provide a future Unix time and an IANA time zone.")
    if type(wait) not in (int, float) or not 0 <= wait <= 30 or not math.isfinite(wait):
        raise CoordinationError("wait_seconds must be between 0 and 30.")
    if "request_id" in arguments and (not isinstance(arguments["request_id"], str)
                                      or not arguments["request_id"].strip() or len(arguments["request_id"]) > 160):
        raise CoordinationError("request_id must contain 1–160 characters.")
    payload = {"mode": mode, "message": message, "run_at": run_at, "timezone": timezone}
    if mode == "new":
        settings = arguments.get("settings")
        allowed = {"cwd", "client", "model", "effort", "yolo", "project_id", "repository_id"}
        if (not isinstance(settings, dict) or set(settings) - allowed or
                any(not isinstance(settings.get(key), str) or not settings[key].strip() for key in ("cwd", "client", "model"))):
            raise CoordinationError("New-agent schedules require workspace, client, and model settings.")
        payload["settings"] = settings
    elif "settings" in arguments:
        raise CoordinationError("Self schedules use the conversation's current settings.")
    from .app_control import AppControl
    control = AppControl(store or CoordinationStore())
    receipt = control.request("schedule", sender, payload, request_id=arguments.get("request_id"))
    result = control.status(receipt["request_id"], wait=wait)
    result["schedule_id"] = str(uuid.uuid5(uuid.NAMESPACE_URL, "agent-coord:schedule:" + receipt["request_id"]))
    return result


def ask_human(arguments, *, store=None):
    if not isinstance(arguments, dict) or set(arguments) - ASK_TOOL["inputSchema"]["properties"].keys():
        raise CoordinationError("Use the ask_human input fields: question, blocking, options, context, supersedes, wait_seconds.")
    session_id = bind_caller(arguments.get("session_id"))
    if not isinstance(session_id, str) or not session_id.strip():
        raise CoordinationError("No caller session. Pass session_id from your hook context, or set AGENT_COORD_SESSION_ID or CODEX_THREAD_ID.")
    wait = arguments.get("wait_seconds")
    if wait is not None and (isinstance(wait, bool) or not isinstance(wait, (int, float)) or not 1 <= wait <= 300):
        raise CoordinationError("wait_seconds must be a number from 1 to 300.")
    if wait is not None and arguments.get("blocking") is not True:
        raise CoordinationError("wait_seconds applies to blocking asks only.")
    asks = (store or CoordinationStore()).asks
    ask = asks.create(session_id, arguments.get("question"), options=arguments.get("options"),
                      blocking=arguments.get("blocking"), context=arguments.get("context"),
                      supersedes=arguments.get("supersedes"))
    if wait is not None:
        ask = asks.wait(ask["id"], session_id, timeout_seconds=float(wait))
    result = {"status": "ok", "ask_id": ask["id"], "state": ask["status"]}
    if ask["status"] == "answered":
        result["answer"] = ask["answer"]
    elif wait is not None:
        result["note"] = "Not answered yet. End your turn; the answer will arrive as an inbox message."
    return result


def move_thread(arguments, *, store=None):
    if not isinstance(arguments, dict) or set(arguments) - MOVE_TOOL["inputSchema"]["properties"].keys():
        raise CoordinationError("Use the move_thread input fields.")
    sender = bind_caller(arguments.get("from_session")) or arguments.get("session_id")
    target = arguments.get("session_id") or sender
    if not isinstance(sender, str) or not sender.strip() or not isinstance(target, str) or not target.strip():
        raise CoordinationError("No caller session. Pass from_session or session_id from your hook context.")
    from .app_control import AppControl
    return AppControl(store or CoordinationStore()).request(
        "move", sender, {"cwd": arguments.get("cwd")}, thread_id=target, request_id=arguments.get("request_id"))


def update_checkpoint(arguments, *, store=None):
    if not isinstance(arguments, dict) or set(arguments) - TOOL["inputSchema"]["properties"].keys():
        raise CoordinationError("Use the update_checkpoint input fields; send only fields that changed.")
    session_id = bind_caller(arguments.get("session_id"))
    if not isinstance(session_id, str) or not session_id.strip():
        raise CoordinationError("No caller session. Pass session_id from your hook context, or set AGENT_COORD_SESSION_ID or CODEX_THREAD_ID.")
    payload = {key: value for key, value in arguments.items() if key != "session_id"}
    result = (store or CoordinationStore()).threads.checkpoint(session_id, payload)
    return {"status": "ok", "checkpoint_id": result["checkpoint"]["id"]}


def dispatch(message):
    """Handle the MCP methods needed for a fixed tool catalog, without SDK dependencies."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid JSON-RPC request."}}
    if "id" not in message:
        return None
    response = {"jsonrpc": "2.0", "id": message["id"]}
    method, params = message["method"], message.get("params", {})
    if not isinstance(params, dict):
        return {**response, "error": {"code": -32602, "message": "Parameters must be an object."}}
    if method == "initialize":
        versions = {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"}
        version = params.get("protocolVersion")
        result = {"protocolVersion": version if isinstance(version, str) and version in versions else "2025-06-18",
                  "capabilities": {"tools": {}}, "serverInfo": {"name": "agent-coord", "version": "0.1.0"}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [TOOL, MOVE_TOOL, ASK_TOOL, SCHEDULE_TOOL, ACTIVITY_TOOL, ACTIVITY_HISTORY_TOOL]}
    elif method == "tools/call":
        handler = {TOOL["name"]: update_checkpoint, MOVE_TOOL["name"]: move_thread,
                   ASK_TOOL["name"]: ask_human, SCHEDULE_TOOL["name"]: schedule_prompt,
                   ACTIVITY_TOOL["name"]: query_activity,
                   ACTIVITY_HISTORY_TOOL["name"]: read_activity_history}.get(params.get("name"))
        if handler is None:
            return {**response, "error": {"code": -32602, "message": "Unknown tool."}}
        try:
            metadata = params.get("_meta", {})
            session_id = metadata.get("threadId") if isinstance(metadata, dict) else None
            if session_id is not None and (not isinstance(session_id, str) or not session_id.strip()):
                raise CoordinationError("MCP threadId must be a nonempty session ID.")
            token = request_session.set(session_id)
            try:
                arguments = params.get("arguments", {})
                if handler in {update_checkpoint, ask_human, schedule_prompt, move_thread}:
                    if not isinstance(arguments, dict):
                        raise CoordinationError("Tool arguments must be an object.")
                    # move_thread's session_id is a target; from_session is the
                    # actor. Other mutations own the caller's session directly.
                    field = "from_session" if handler is move_thread else "session_id"
                    identity = bind_caller(arguments.get(field), require_context=True)
                    arguments = {**arguments, field: identity}
                receipt = handler(arguments)
            finally:
                request_session.reset(token)
            result = {"content": [{"type": "text", "text": json.dumps(receipt, separators=(",", ":"))}]}
        except (CoordinationError, OSError, sqlite3.Error) as exc:
            result = {"isError": True, "content": [{"type": "text", "text": str(exc)}]}
    else:
        return {**response, "error": {"code": -32601, "message": "Method not found."}}
    return {**response, "result": result}


def main():
    for line in sys.stdin:
        try:
            response = dispatch(json.loads(line))
        except json.JSONDecodeError:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid JSON."}}
        if response is not None:
            print(json.dumps(response, separators=(",", ":")), flush=True)
