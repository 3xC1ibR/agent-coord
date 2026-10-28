"""Resolve the settings a new prompt will inherit, without starting a turn."""
from __future__ import annotations

import os

from .store import CoordinationError
from .workspaces import resolve_workspace


def session_defaults(sessions, body: dict) -> dict:
    if set(body) - {"cwd", "client", "model"}:
        raise CoordinationError("Unknown session defaults fields.")
    client, model = body.get("client", "codex"), body.get("model")
    if not isinstance(client, str) or client not in {"codex", "claude", "antigravity", "kimi", "muse"}:
        raise CoordinationError("Choose a supported client.")
    if model is not None and not isinstance(model, str):
        raise CoordinationError("Model must be a string.")
    model = model or None
    cwd = resolve_workspace(body.get("cwd", sessions.cwd or os.getcwd()), workspace=sessions.cwd)
    config = {}
    if client in {"codex", "claude"}:
        params = {"cwd": cwd, "includeLayers": False} if client == "codex" else {"cwd": cwd, "model": model}
        config = sessions._provider(client).request("config/read", params).get("config", {})
    models = sessions.models(client)
    effective = config.get("model") if client == "claude" else model or config.get("model")
    selected = next((item for item in models if effective and
                     (item["model"] == effective or item.get("resolvedModel") == effective)), None)
    if effective is None:
        selected = next((item for item in models if item.get("isDefault")), None)
        effective = selected["model"] if selected else None
    selected = selected or {}
    # Prefer a concrete display name to an alias called "Default".
    display = next((item.get("displayName") for item in models if effective and
                    item.get("resolvedModel") == effective and item["model"] != "default"), None)
    effort = config.get("model_reasoning_effort")
    if effort is None:
        effort = selected.get("defaultReasoningEffort")
    return {"model": effective, "modelLabel": display or selected.get("displayName") or effective,
            "effort": effort, "supportsEffort": bool(selected.get("supportedReasoningEfforts"))}
