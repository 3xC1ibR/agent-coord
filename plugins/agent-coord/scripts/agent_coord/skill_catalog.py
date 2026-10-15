"""Read the native skill inventory without creating or resuming a conversation."""
from __future__ import annotations

import os

from .store import CoordinationError
from .workspaces import resolve_workspace


def skill_catalog(sessions, body: dict) -> list[dict]:
    if set(body) - {"cwd", "client"}:
        raise CoordinationError("Unknown skill catalog fields.")
    cwd = resolve_workspace(body.get("cwd", sessions.cwd or os.getcwd()), workspace=sessions.cwd)
    client = body.get("client", "codex")
    if not isinstance(client, str) or client not in {"codex", "claude", "antigravity", "kimi", "muse"}:
        raise CoordinationError("Choose a supported client.")
    # Dollar-sign invocation and skills/list belong to the native Codex protocol.
    if client != "codex":
        return []
    result = sessions.rpc.request("skills/list", {"cwds": [cwd], "forceReload": True})
    skills = {}
    for entry in result.get("data", []):
        if entry.get("cwd") != cwd:
            continue
        for skill in entry.get("skills", []):
            name = skill.get("name")
            if not skill.get("enabled") or not isinstance(name, str) or not name:
                continue
            interface = skill.get("interface") or {}
            skills.setdefault(name, {"name": name, "description": interface.get("shortDescription") or
                skill.get("shortDescription") or skill.get("description") or "",
                "scope": skill.get("scope", "")})
    return sorted(skills.values(), key=lambda skill: skill["name"].casefold())
