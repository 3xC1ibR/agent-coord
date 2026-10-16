"""User attention is separate from agent message delivery and turn subscriptions."""
from __future__ import annotations

from .organization import app_agent_creators


def initialize(db):
    # A side table keeps older running hooks compatible with the new runtime.
    db.execute("""CREATE TABLE IF NOT EXISTS turn_notification_context (
        thread_id TEXT NOT NULL, turn_key TEXT NOT NULL,
        origin TEXT NOT NULL, owner_thread_id TEXT,
        checkpoint_id INTEGER,
        PRIMARY KEY (thread_id, turn_key)
    )""")


def record_context(db, thread_id, turn_id, context):
    db.execute("""INSERT INTO turn_notification_context (thread_id, turn_key, origin, owner_thread_id)
        VALUES (?, ?, ?, ?) ON CONFLICT(thread_id, turn_key) DO UPDATE SET
        origin = CASE WHEN origin = 'user' THEN origin ELSE excluded.origin END,
        owner_thread_id = CASE WHEN origin = 'user' THEN owner_thread_id ELSE excluded.owner_thread_id END""",
        (thread_id, turn_id, context["origin"], context.get("owner_thread_id")))


def snapshot_checkpoint(db, thread_id, turn_id):
    db.execute("""UPDATE turn_notification_context SET checkpoint_id = (
        SELECT cp.id FROM thread_checkpoints cp JOIN turn_completions c ON c.thread_id = cp.thread_id
        WHERE cp.thread_id = ? AND cp.turn_id IS ? AND c.turn_key = ? AND cp.created_at <= c.completed_at
        ORDER BY cp.id DESC LIMIT 1) WHERE thread_id = ? AND turn_key = ? AND checkpoint_id IS NULL""",
        (thread_id, turn_id, turn_id, thread_id, turn_id))


def root_owner(thread_id, creators):
    current, seen = thread_id, set()
    while current not in seen:
        seen.add(current)
        creator = creators.get(current)
        if not creator or creator["mode"] != "orchestration":
            return current
        current = creator["thread_id"]
    return thread_id  # Malformed historical cycles must not hide a request.


def automatic_context(store, thread_id):
    with store._connection() as db:
        creators = app_agent_creators(db)
        owner = root_owner(thread_id, creators)
        messages = db.execute("""SELECT m.* FROM messages m JOIN message_wake_attempts a
            ON a.message_id = m.id WHERE a.session_id = ? AND a.outcome = 'claimed'
            AND m.delivered_at IS NULL ORDER BY m.id""", (thread_id,)).fetchall()
        creator = creators.get(thread_id)
        # Dispatch creates an independently user-facing conversation. A reply
        # contract or a reply from a peer is coordination, not dispatch.
        if creator and creator["mode"] is None and messages and all(
                m["sender_session_id"] == creator["thread_id"] and not m["reply_required"]
                and m["in_reply_to"] is None and not m["thread_id"].startswith("event:")
                for m in messages):
            return {"origin": "dispatch", "owner_thread_id": thread_id}
        # Reusing a specialist need not change its permanent creation record.
        if owner == thread_id:
            for message in messages:
                sender = message["sender_session_id"]
                checkpoint = db.execute("SELECT phase FROM thread_checkpoints WHERE thread_id = ? ORDER BY id DESC LIMIT 1",
                                        (sender,)).fetchone()
                if message["reply_required"] and checkpoint and checkpoint["phase"] == "orchestrating":
                    owner = root_owner(sender, creators)
                    break
        is_owner = any(c["mode"] == "orchestration" and root_owner(child, creators) == thread_id
                       for child, c in creators.items())
        is_owner = is_owner or db.execute("SELECT 1 FROM turn_notification_context WHERE owner_thread_id = ? AND thread_id != ? LIMIT 1",
                                         (thread_id, thread_id)).fetchone() is not None
        return {"origin": "coordination", "owner_thread_id": owner if owner != thread_id or is_owner else None}


def available_owners(db, store):
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    paused = {row[0] for row in db.execute("SELECT thread_id FROM browser_inbox_wake WHERE paused = 1")} if "browser_inbox_wake" in tables else set()
    browsers = {row[0] for row in db.execute("SELECT thread_id FROM browser_sessions")} if "browser_sessions" in tables else set()
    result = set()
    for row in db.execute("""SELECT t.thread_id, t.attention, s.ended_at, s.turn_active, s.last_seen_at,
            c.status FROM work_threads t JOIN sessions s ON s.session_id = t.thread_id
            LEFT JOIN turn_completions c ON c.thread_id = t.thread_id AND c.turn_key = t.turn_key"""):
        tid = row["thread_id"]
        if (row["attention"] != "archived" and tid not in paused
                and (row["turn_active"] or row["status"] not in {"failed", "interrupted"})
                and (tid in browsers or row["ended_at"] is None
                     and row["last_seen_at"] >= store.clock() - store.stale_after_seconds)):
            result.add(tid)
    return result


def decide(thread, *, waiting=False, owners=()):
    """Return a presentation policy; never acknowledge messages or completions."""
    context = thread.get("notification_context") or {}
    origin = context.get("origin")
    owner = context.get("owner_thread_id")
    legacy_child = origin is None and thread.get("automatic_turn") and thread.get("creation_mode") == "orchestration"
    if legacy_child:
        owner = thread.get("created_by_thread_id")
    internal = origin == "coordination" or legacy_child
    checkpoint = thread.get("checkpoint") if not thread.get("checkpoint_stale") else None
    completion = thread.get("turn_completion")
    needs_user = bool(waiting or checkpoint and checkpoint["next_actor"] == "user" and checkpoint["next_action"])
    waiting_for_agents = bool(checkpoint and checkpoint["phase"] == "orchestrating"
                             and checkpoint["next_actor"] in {"agent", "external"})
    delivered = bool(owner == thread["thread_id"] and checkpoint
                     and checkpoint["phase"] != "orchestrating" and checkpoint["next_actor"] == "nobody")
    failed = bool(completion and completion["status"] == "failed")
    # A stopped/closed/failed owner cannot be relied on to recover a child.
    stranded = failed and (not owner or owner == thread["thread_id"] or owner not in owners)
    quiet = not needs_user and ((internal and not delivered and not stranded) or waiting_for_agents and not failed)
    return {"quiet": quiet, "notify": not quiet,
            "owner_thread_id": owner if internal and owner in owners else thread["thread_id"],
            "needs_user": needs_user}


def group_attention(threads):
    """Roll up live child needs without changing receipts or saved placement."""
    by_id = {thread["thread_id"]: thread for thread in threads}
    groups = {}
    for thread in threads:
        owner = by_id.get(thread.get("attention_owner_thread_id"))
        if (not thread["needs_attention"] or not owner or owner is thread
                or owner["attention"] == "archived" or owner["snoozed"]
                or owner["attention"] != thread["attention"]):
            continue
        groups.setdefault(owner["thread_id"], []).append(thread)
    for owner_id, children in groups.items():
        owner = by_id[owner_id]
        items = ([owner] if owner["needs_attention"] else []) + children
        owner["attention_items"] = [{"thread_id": item["thread_id"], "title": item["title"],
            "key": item["attention_key"], "reason": item["attention_reason"],
            "action": item.get("attention_action") or "Needs your input"} for item in items]
        owner["needs_attention"] = True
        owner["attention_reason"] = "blocked" if any(i["attention_reason"] == "blocked" for i in items) else "review"
        owner["attention_key"] = "group:" + "|".join(sorted(str(i["attention_key"]) for i in items))
        owner["attention_since"] = min(i["attention_since"] for i in items if i["attention_since"] is not None)
        owner["attention_target_thread_id"] = items[0]["thread_id"]
        owner["can_handle_response"] = False  # Each underlying need has its own receipt.
        for child in children:
            # Retain the underlying need for filters/search and direct reads.
            # Presentation folds it only while its owner is actually visible.
            child["attention_grouped_under"] = owner_id
    return threads
