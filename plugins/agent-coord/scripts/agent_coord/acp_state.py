"""Explicit, version-compatible ACP adapter state for coordinator replacement."""
from __future__ import annotations

import copy
from pathlib import Path

COMMON_FIELDS = ("options", "native", "turns", "completed", "requests", "update_stopping")
PROVIDER_FIELDS = {
    "antigravity": ("resolved_models", "catalog", "permission_catalogs", "effective_permissions", "delivery_turns", "last_activity"),
    "kimi": ("settings", "capabilities", "catalog", "permission_catalogs", "effective_permissions"),
    "muse": ("items", "settings", "seen", "ready", "terminal_turns", "background_turns", "request_history", "answered", "settled"),
}


def capture(sessions):
    result = {}
    for provider, fields in PROVIDER_FIELDS.items():
        adapter = getattr(sessions, provider)
        state = {name: copy.deepcopy(getattr(adapter, name)) for name in COMMON_FIELDS + fields if hasattr(adapter, name)}
        state["initial_prompt"] = copy.deepcopy(adapter.initial_prompt.pending)
        state["connections"] = {connection.channel: {
            "bindings": copy.deepcopy(connection.bindings),
            "errors": list(connection.errors),
            "resource_path": getattr(connection, "resource_path", None),
        } for connection in adapter.connections.values() if getattr(connection, "channel", None)}
        if provider == "kimi":
            state["journals"] = {identity: {**vars(journal), "path": str(journal.path) if journal.path else None,
                                            "anchor": journal.anchor.hex()}
                                 for identity, journal in adapter.journals.items()}
        result[provider] = state
    return result


def restore(sessions, saved):
    for provider, state in saved.items():
        adapter = getattr(sessions, provider)
        for name in COMMON_FIELDS + PROVIDER_FIELDS[provider]:
            if name in state:
                setattr(adapter, name, copy.deepcopy(state[name]))
        adapter.initial_prompt.pending = copy.deepcopy(state.get("initial_prompt", {}))
        if provider == "kimi":
            from .kimi_status import PromptJournal
            adapter.journals = {}
            for identity, values in state.get("journals", {}).items():
                journal = PromptJournal.__new__(PromptJournal)
                journal.__dict__.update(copy.deepcopy(values))
                journal.path = Path(journal.path) if journal.path else None
                journal.anchor = bytes.fromhex(journal.anchor)
                adapter.journals[identity] = journal
        for connection in adapter.connections.values():
            channel = getattr(connection, "channel", None)
            if channel in state.get("connections", {}):
                connection.bindings = copy.deepcopy(state["connections"][channel]["bindings"])
        # Native Muse items and their rendered turn items are separate objects;
        # revisions and IDs, rather than object identity, reconcile updates.


def attach(projection, provider, identity, channel, pid, saved):
    from .antigravity import AntigravityConnection
    from .kimi import KimiConnection
    from .muse_code import MuseConnection
    adapter = getattr(projection.sessions, provider)
    if provider == "antigravity":
        callback = lambda message: adapter._message(identity, message)
    else:
        generation = object()
        adapter.generations[identity] = generation
        callback = lambda message: adapter._message(identity, generation, message)
    cls = {"antigravity": AntigravityConnection, "kimi": KimiConnection, "muse": MuseConnection}[provider]
    state = saved.get(provider, {}).get("connections", {}).get(channel, {})
    connection = cls.attach(projection.store, callback, identity, channel, pid, provider=provider, state=state)
    connection.errors.extend(state.get("errors", []))
    connection.options = dict(adapter.options.get(identity, {}))
    adapter.connections[identity] = connection
    return connection
