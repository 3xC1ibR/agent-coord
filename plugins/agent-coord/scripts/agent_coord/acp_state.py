"""Explicit, version-compatible ACP adapter state for coordinator replacement."""
from __future__ import annotations

import copy
from pathlib import Path

COMMON_FIELDS = ("options", "native", "turns", "completed", "requests", "update_stopping", "native_replays")
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
            "answered_requests": copy.deepcopy(connection.answered_requests),
            "native_decisions": copy.deepcopy(connection.native_decisions),
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
                connection.answered_requests = copy.deepcopy(state["connections"][channel].get("answered_requests", {}))
                connection.native_decisions = copy.deepcopy(state["connections"][channel].get("native_decisions", {}))
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
    if provider != "muse":
        import json
        for key, pending in list(adapter.requests.items()):
            native_request = pending["id"] if isinstance(pending, dict) else pending[1]
            owner = pending["thread"] if isinstance(pending, dict) else pending[0]
            if owner == identity and json.dumps(native_request) in connection.answered_requests:
                adapter.requests.pop(key, None)
                for browser_key, request in list(projection.sessions.requests.items()):
                    if request.get("id") == key:
                        projection.sessions.requests.pop(browser_key, None)
    return connection


def begin_replay(adapter, identity):
    if not hasattr(adapter, "native_replays"):
        adapter.native_replays = {}
    adapter.native_replays[identity] = []


def replay_update(adapter, identity, update):
    """ACP 1.x identifies replay boundaries by user-message chunks."""
    import uuid
    turns = getattr(adapter, "native_replays", {}).get(identity)
    if turns is None:
        return False
    kind = update.get("sessionUpdate")
    if kind == "user_message_chunk" or not turns:
        native_id = update.get("turnId")
        if kind == "user_message_chunk" and turns and turns[-1]["items"] and all(
                item["type"] == "userMessage" for item in turns[-1]["items"]):
            pass
        else:
            turns.append({"id": native_id or str(uuid.uuid5(uuid.NAMESPACE_URL,
                f"acp:{adapter.native[identity]}:{len(turns)}")), "status": "completed", "items": []})
    turn = turns[-1]
    items = turn["items"]
    item_id = update.get("itemId") or update.get("toolCallId")
    if kind in {"user_message_chunk", "agent_message_chunk", "agent_thought_chunk"}:
        content = update.get("content", {})
        if content.get("type") != "text":
            return True
        item_type = {"user_message_chunk": "userMessage", "agent_message_chunk": "agentMessage",
                     "agent_thought_chunk": "reasoning"}[kind]
        item = items[-1] if items else {}
        if item.get("type") != item_type or item_id and item.get("id") != item_id:
            item = {"id": item_id or str(uuid.uuid5(uuid.NAMESPACE_URL, turn["id"] + ":" + str(len(items)))), "type": item_type}
            items.append(item)
        text = content.get("text", "")
        if item_type == "userMessage":
            content = item.setdefault("content", [{"type": "text", "text": ""}])
            content[-1]["text"] += text
        elif item_type == "reasoning":
            item.setdefault("summary", [""])[0] += text
        else:
            item["text"] = item.get("text", "") + text
    elif kind in {"tool_call", "tool_call_update"} and item_id:
        item = next((item for item in items if item["id"] == item_id), None)
        if item is None:
            item = {"id": item_id, "type": "mcpToolCall", "tool": update.get("title", "Tool"), "arguments": {}}
            items.append(item)
        if "rawInput" in update:
            item["arguments"] = update["rawInput"]
        if "rawOutput" in update or "content" in update:
            item["result"] = update.get("rawOutput", update.get("content"))
        item["status"] = {"pending": "inProgress", "in_progress": "inProgress"}.get(update.get("status"), "completed")
    return True


def _user_text(turn):
    return "\n".join(part.get("text", "") for item in turn.get("items", []) if item.get("type") == "userMessage"
                     for part in item.get("content", []) if part.get("type") == "text")


def merge_replay(adapter, identity, turns):
    """Merge archived items without publishing a second live completion."""
    from .history_persistence import read_history, HistoryPersistence
    sessions = getattr(adapter.on_event, "__self__", None)
    if sessions is not None and hasattr(sessions, "_history"):
        history = sessions._history(identity)
    else:
        history = read_history(adapter.store, identity) or {"id": identity, "turns": []}
    saved = history.setdefault("turns", [])
    matched = set()
    changed = False
    for turn in turns:
        existing = next((entry for entry in saved if entry["id"] == turn["id"]), None)
        if existing is None:
            text = _user_text(turn)
            # Older adapters used local turn IDs. Match each original user turn
            # once, in native order; retain its local ID and initial bootstrap.
            existing = next((entry for entry in saved if entry["id"] not in matched and text
                             and _user_text(entry) and text.endswith(_user_text(entry))), None)
        if existing is None:
            saved.append(copy.deepcopy(turn))
            changed = True
            continue
        matched.add(existing["id"])
        if existing.get("status") == "inProgress":
            continue  # A snapshot must not complete an independently active turn.
        for item in turn.get("items", []):
            old = next((entry for entry in existing.get("items", []) if entry["id"] == item["id"]), None)
            if old is None:
                comparable = {k: v for k, v in item.items() if k != "id"}
                if item.get("type") == "userMessage" or any(
                        {k: v for k, v in entry.items() if k != "id"} == comparable for entry in existing.get("items", [])):
                    continue
                existing.setdefault("items", []).append(copy.deepcopy(item))
                changed = True
            elif old != item:
                old.update(copy.deepcopy(item))
                changed = True
    if changed:
        if sessions is not None and hasattr(sessions, "_save_history"):
            sessions._save_history(identity)
        else:
            HistoryPersistence(adapter.store).save(identity, history)
    return history


def finish_replay(adapter, identity):
    turns = getattr(adapter, "native_replays", {}).pop(identity, None)
    if turns is not None:
        merge_replay(adapter, identity, [turn for turn in turns if turn["items"]])


def reconcile_muse(adapter, identity, response):
    session = response.get("session", {})
    if session.get("sessionId") != adapter.native.get(identity):
        raise ValueError("Muse history response belongs to a different session")
    turns = []
    for native in session.get("turns", []):
        turn_id = native.get("turnId")
        if not turn_id:
            continue
        status = {"completed": "completed", "cancelled": "interrupted", "failed": "failed"}.get(native.get("terminal"))
        items = []
        for item in native.get("items", []):
            if item.get("kind") == "userMessage":
                items.append({"id": item["itemId"], "type": "userMessage", "content": item.get("content") or
                              [{"type": "text", "text": item.get("text", "")}]})
            else:
                items.append(adapter._item(item))
            if (adapter.turns.get(identity, {}).get("id") == turn_id
                    or turn_id in adapter.background_turns.get(identity, {})) and item.get("kind") != "userMessage":
                adapter._update_item(identity, "item/completed", {"turnId": turn_id, "item": item})
            adapter.items.setdefault(identity, {})[item["itemId"]] = copy.deepcopy(item)
        if adapter.turns.get(identity, {}).get("id") == turn_id:
            if status is not None:
                adapter._finish(identity, status)
            continue
        if status is None:
            continue
        turn = {"id": turn_id, "status": status, "items": items}
        turns.append(turn)
        if any(item.get("nativeKind") == "workflow" and item.get("status") == "inProgress" for item in items):
            adapter.background_turns.setdefault(identity, {})[turn_id] = copy.deepcopy(turn)
    return merge_replay(adapter, identity, turns)


def settle_muse_decision(projection, thread_id, decision, receipt):
    adapter = projection.sessions.muse
    identity = (thread_id, decision["native_id"])
    if decision["question"] or receipt.get("terminal"):
        adapter.settled.add(identity)
    else:
        adapter.answered.add((*identity, (decision.get("requirement") or {}).get("sourceIndex")))
    for key, pending in list(adapter.requests.items()):
        if pending["thread"] == thread_id and pending["native_id"] == decision["native_id"]:
            adapter.requests.pop(key, None)
            for browser_key, request in list(projection.sessions.requests.items()):
                if request.get("id") == key:
                    projection.sessions.requests.pop(browser_key, None)
