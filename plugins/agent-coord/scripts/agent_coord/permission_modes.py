"""Provider permission identities and compatibility with the legacy YOLO bit.

Mode IDs are provider-specific. In particular, approval policy alone does not
describe a provider's filesystem sandbox, reviewer, or plan mode.
"""
from __future__ import annotations

from .store import CoordinationError


LEGACY_MODES = {
    "codex": ("workspace-write", "full-access"),
    "claude": ("default", "bypassPermissions"),
    "antigravity": ("default", "yolo"),
    "kimi": ("default", "yolo"),
    "muse": (":ask-me", ":unrestricted"),
}

CLAUDE_MODES = [
    {"id": "default", "label": "Manual", "description": "Claude asks before actions that require approval."},
    {"id": "acceptEdits", "label": "Accept edits", "description": "Approve file edits automatically; other actions may ask."},
    {"id": "plan", "label": "Plan", "description": "Explore and plan; edits require approval."},
    {"id": "auto", "label": "Auto", "description": "Claude's native classifier reviews actions. Requires an eligible model and account."},
    {"id": "dontAsk", "label": "Don't ask", "description": "Deny actions that would require approval instead of asking."},
    {"id": "bypassPermissions", "label": "Dangerously skip permissions", "description": "Skip ordinary approval checks. Native safety confirmations, rules, hooks and user questions still apply."},
]

# Codex's sandbox, approval policy and reviewer are independent protocol axes.
# Each application preset names its exact combination; named native profiles
# remain distinct rather than being approximated by one of these presets.
CODEX_PRESETS = {
    "read-only": ("read-only", "on-request", "user", "Read only", "Read files; ask before actions outside the read-only sandbox."),
    "workspace-write": ("workspace-write", "on-request", "user", "Auto", "Edit the workspace; ask before sandbox escapes or restricted network access."),
    "full-access": ("danger-full-access", "never", "user", "Full access", "Disable Codex sandbox restrictions and ordinary approval prompts."),
    "untrusted": ("workspace-write", "untrusted", "user", "Ask for untrusted commands", "Use the workspace sandbox and ask before commands Codex does not trust."),
    "auto-review": ("workspace-write", "on-request", "auto_review", "Auto review", "Use the workspace sandbox; Codex's native reviewer decides approval requests."),
    "workspace-no-prompts": ("workspace-write", "never", "user", "Workspace without prompts", "Use the workspace sandbox; deny actions that require escalation instead of asking."),
}


def codex_modes(rpc, cwd):
    catalog = [{"id": key, "label": value[3], "description": value[4]} for key, value in CODEX_PRESETS.items()]
    cursor = None
    while True:
        try:
            result = rpc.request("permissionProfile/list", {"cwd": cwd, **({"cursor": cursor} if cursor else {})}) or {}
        except CoordinationError as exc:
            if any(text in str(exc).lower() for text in ("method not found", "unknown method", "not supported")):
                break  # Older servers can still apply the explicit legacy axes.
            raise
        for entry in result.get("data", []):
            if isinstance(entry.get("id"), str) and entry["id"]:
                allowed = entry.get("allowed") is not False
                catalog.append({"id": "profile:" + entry["id"], "label": entry["id"],
                                "description": (entry.get("description") or "Native filesystem and network profile.") + " Approvals are requested from you.",
                                "available": allowed,
                                **({"reason": "Your managed Codex policy excludes this profile."} if not allowed else {})})
        next_cursor = result.get("nextCursor")
        if not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor
    return catalog


def codex_options(mode, roots, *, turn=False):
    if mode.startswith("profile:") and mode[8:]:
        return {"permissions": mode[8:], "approvalPolicy": "on-request", "approvalsReviewer": "user"}
    if mode not in CODEX_PRESETS:
        raise CoordinationError(f"Unknown Codex permission mode '{mode}'.")
    sandbox, approval, reviewer, _, _ = CODEX_PRESETS[mode]
    result = {"approvalPolicy": approval, "approvalsReviewer": reviewer}
    if not turn:
        result["sandbox"] = sandbox
    elif sandbox == "workspace-write":
        result["sandboxPolicy"] = {"type": "workspaceWrite", "writableRoots": list(dict.fromkeys(roots)), "networkAccess": False}
    else:
        result["sandboxPolicy"] = {"type": "readOnly" if sandbox == "read-only" else "dangerFullAccess"}
    return result


def codex_effective_mode(result, requested):
    """Report the native response, including requirements that constrain it."""
    if not isinstance(result.get("sandbox"), dict) or "approvalPolicy" not in result:
        return None
    approval = result["approvalPolicy"]
    reviewer = result.get("approvalsReviewer", "user")
    if reviewer == "guardian_subagent":
        reviewer = "auto_review"
    profile = result.get("activePermissionProfile") or {}
    if (requested.startswith("profile:") and profile.get("id") == requested[8:]
            and approval == "on-request" and reviewer == "user"):
        return requested
    sandbox = {"readOnly": "read-only", "workspaceWrite": "workspace-write",
               "dangerFullAccess": "danger-full-access"}.get(result["sandbox"].get("type"))
    if sandbox == "workspace-write" and result["sandbox"].get("networkAccess", False):
        return None  # These presets explicitly request restricted networking.
    for mode, axes in CODEX_PRESETS.items():
        if axes[:3] == (sandbox, approval, reviewer):
            return mode
    return None


def selection(mode, catalog):
    rows = [dict(entry) for entry in catalog]
    if mode and not any(entry["id"] == mode for entry in rows):
        rows.append({"id": mode, "label": mode, "description": "Saved permission mode.",
                     "available": False, "reason": "The current provider does not offer this saved mode."})
    return {"permission_mode": mode, "permission_modes": rows}


def legacy_mode(client, yolo=False):
    try:
        return LEGACY_MODES[client][bool(yolo)]
    except KeyError as exc:
        raise CoordinationError("Unknown permission provider.") from exc


def selected_mode(record):
    return record.get("permission_mode") or legacy_mode(record.get("client", "codex"), record.get("yolo", False))


def is_legacy_yolo(client, mode):
    return mode == legacy_mode(client, True)


def requested_mode(client, body, record=None):
    """Do not reset an explicit native mode on an unrelated settings update."""
    if "yolo" in body and not isinstance(body["yolo"], bool):
        raise CoordinationError("YOLO must be true or false.")
    if "permission_mode" in body:
        mode = body["permission_mode"]
        if not isinstance(mode, str) or not mode or mode != mode.strip() or len(mode) > 200:
            raise CoordinationError("Choose an available permission mode.")
        if "yolo" in body and body["yolo"] != is_legacy_yolo(client, mode):
            raise CoordinationError("permission_mode and yolo select different permissions.")
        return mode
    if "yolo" in body:
        return legacy_mode(client, body["yolo"])
    return selected_mode(record or {"client": client})


def validate_mode(mode, catalog):
    item = next((entry for entry in catalog if entry.get("id") == mode), None)
    if item is None:
        raise CoordinationError(f"Permission mode '{mode}' is not offered by this provider.")
    if item.get("available") is False:
        raise CoordinationError(item.get("reason") or f"Permission mode '{mode}' is unavailable.")
    return mode


def _select_entries(entries):
    for entry in entries:
        if isinstance(entry, dict) and "options" in entry:
            yield from _select_entries(entry["options"])
        elif isinstance(entry, dict):
            yield entry


def acp_modes(session):
    """Prefer ACP configuration options; retain native IDs and descriptions."""
    option = next((entry for entry in session.get("configOptions", [])
                   if entry.get("category") == "mode" or entry.get("id") == "mode"), {})
    entries = list(_select_entries(option.get("options", [])))
    if not entries:
        entries = session.get("modes", {}).get("availableModes", [])
    result = []
    for entry in entries:
        value = entry.get("value", entry.get("id"))
        if isinstance(value, str) and value:
            result.append({"id": value, "label": entry.get("name") or value,
                           "description": entry.get("description") or "",
                           **({"available": entry["available"]} if "available" in entry else {})})
    return result


def acp_current_mode(session):
    option = next((entry for entry in session.get("configOptions", [])
                   if entry.get("category") == "mode" or entry.get("id") == "mode"), {})
    return option.get("currentValue") or session.get("modes", {}).get("currentModeId")
