"""User-confirmed closure of attached subagent threads."""
from __future__ import annotations

from .store import CoordinationError


def close_plan(sessions, thread_id):
    # Check the requested root against this runtime's workspace boundary first.
    sessions.work_thread(thread_id, history=False)
    edges = {}
    for child, creator in sessions._app_agent_creators().items():
        if creator["mode"] == "orchestration":
            edges.setdefault(creator["thread_id"], set()).add(child)
    with sessions.store._connection() as db:
        for row in db.execute("SELECT parent_session_id, child_session_id FROM delegations WHERE child_session_id IS NOT NULL"):
            edges.setdefault(row[0], set()).add(row[1])
    visited, ordered = {thread_id}, []

    def visit(parent):
        for child in sorted(edges.get(parent, ())):
            if child in visited:
                continue
            visited.add(child)
            visit(child)
            thread = sessions.store.threads.get(child)
            if thread["attention"] != "archived":
                session = sessions.store.get_session(child)
                ordered.append({"thread_id": child, "title": thread["title"],
                                "attention": thread["attention"], "snoozed_until": thread["snoozed_until"],
                                "running": bool(session["turn_active"] and session["presence"] != "offline")})

    visit(thread_id)
    return {"subagents": ordered}


def close_with_subagents(sessions, thread_id, body):
    if set(body) - {"subagent_thread_ids"}:
        raise CoordinationError("Close accepts subagent_thread_ids only.")
    if "subagent_thread_ids" not in body:
        return sessions.close_work_thread(thread_id)
    selected = body["subagent_thread_ids"]
    if (not isinstance(selected, list) or any(not isinstance(item, str) for item in selected)
            or len(selected) != len(set(selected))):
        raise CoordinationError("Choose a list of distinct subagent thread IDs.")
    plan = close_plan(sessions, thread_id)["subagents"]
    if set(selected) != {item["thread_id"] for item in plan}:
        raise CoordinationError("Subagent threads changed. Try Close again to review them.")
    # Validate every workspace boundary before stopping any work. Close leaves
    # history intact and waits for owned running turns to acknowledge Stop.
    for item in plan:
        sessions.work_thread(item["thread_id"], history=False)
    closed, errors, notices = [], [], []
    for item in plan:
        try:
            result = sessions.close_work_thread(item["thread_id"])
            closed.append(item["thread_id"])
            if result.get("close_notice"):
                notices.append(result["close_notice"])
        except CoordinationError as exc:
            errors.append({"thread_id": item["thread_id"], "error": str(exc)})
    # A parent can create children while shutdown is in progress. Never close
    # a newly attached thread that was absent from the user's confirmation.
    if not errors and close_plan(sessions, thread_id)["subagents"]:
        errors.append({"thread_id": thread_id, "error": "Subagent threads changed. Try Close again to review them."})
    if not errors:
        try:
            result = sessions.close_work_thread(thread_id)
            closed.append(thread_id)
        except CoordinationError as exc:
            errors.append({"thread_id": thread_id, "error": str(exc)})
    if errors:
        result = sessions.work_thread(thread_id, history=False)
    if notices:
        result["close_notice"] = " ".join(dict.fromkeys(notices + ([result["close_notice"]] if result.get("close_notice") else [])))
    return {**result, "closed_thread_ids": closed, "subagent_close_errors": errors}
