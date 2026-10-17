"""Small dependency-free stdio MCP surface for thread controls."""
from __future__ import annotations

import json
import sqlite3
import sys

from .context import caller_session
from .store import CoordinationError, CoordinationStore
from .threads import LINK_KINDS, PHASES
from .context_lifecycle import CHECKPOINT_SCHEMA


TOOL = {
    "name": "update_checkpoint",
    "description": (
        "Create or update your work-thread checkpoint. Send only changed fields; omissions preserve saved values. "
        "The first checkpoint requires phase and summary. Set next_step to null to clear it. "
        "Returns only status and checkpoint_id. Use your session_id from hook context if caller identity is unavailable."
    ),
    "inputSchema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "session_id": {"type": "string", "minLength": 1, "description": "Your registered session ID; omit when inherited from the environment."},
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
            "from_session": {"type": "string", "minLength": 1, "description": "Requester identity from hook context if not inherited."},
            "request_id": {"type": "string", "minLength": 1, "maxLength": 160,
                           "description": "Stable idempotency key; reuse when retrying."},
        },
    },
    "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
}


def move_thread(arguments, *, store=None):
    if not isinstance(arguments, dict) or set(arguments) - MOVE_TOOL["inputSchema"]["properties"].keys():
        raise CoordinationError("Use the move_thread input fields.")
    sender = arguments.get("from_session") or caller_session() or arguments.get("session_id")
    target = arguments.get("session_id") or sender
    if not isinstance(sender, str) or not sender.strip() or not isinstance(target, str) or not target.strip():
        raise CoordinationError("No caller session. Pass from_session or session_id from your hook context.")
    from .app_control import AppControl
    return AppControl(store or CoordinationStore()).request(
        "move", sender, {"cwd": arguments.get("cwd")}, thread_id=target, request_id=arguments.get("request_id"))


def update_checkpoint(arguments, *, store=None):
    if not isinstance(arguments, dict) or set(arguments) - TOOL["inputSchema"]["properties"].keys():
        raise CoordinationError("Use the update_checkpoint input fields; send only fields that changed.")
    session_id = arguments.get("session_id", caller_session())
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
        result = {"tools": [TOOL, MOVE_TOOL]}
    elif method == "tools/call":
        handler = {TOOL["name"]: update_checkpoint, MOVE_TOOL["name"]: move_thread}.get(params.get("name"))
        if handler is None:
            return {**response, "error": {"code": -32602, "message": "Unknown tool; use update_checkpoint or move_thread."}}
        try:
            receipt = handler(params.get("arguments", {}))
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
