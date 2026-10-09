"""Incremental, local search projection of saved conversation prose.

This never resumes providers, imports transcripts, or searches tool output.
History triggers invalidate the projection even when an older runtime saves it.
"""
from __future__ import annotations

import json

from .timeline_clock import seconds
from .zellij_wake import WAKE_PROMPT


def messages(history):
    if not isinstance(history, dict):
        return
    for ti, turn in enumerate(history.get("turns") or []):
        if not isinstance(turn, dict):
            continue
        for ii, item in enumerate(turn.get("items") or []):
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            if kind == "userMessage":
                text = "\n".join(c["text"] for c in (item.get("content") or [])
                                 if isinstance(c, dict) and c.get("type") in {"text", "input_text"}
                                 and isinstance(c.get("text"), str))
                role = "user"
                # Provider context and inbox wake prompts aren't user conversation.
                if text.strip() == WAKE_PROMPT or text.lstrip().startswith((
                        "# AGENTS.md", "<environment_context>", "<INSTRUCTIONS>", "<agent-coord", "[agent-coord")):
                    continue
            elif kind == "agentMessage" and isinstance(item.get("text"), str):
                text, role = item["text"], "assistant"
            else:
                continue
            if text.strip():
                yield (str(turn.get("id") or f"search-turn-{ti}"),
                       str(item.get("id") or f"search-item-{ii}"), role,
                       seconds(item.get("timelineAt")) or seconds(turn.get("startedAt")),
                       text, text.casefold())


def sync_messages(db):
    """Backfill once, then rebuild only changed histories in this transaction."""
    db.execute("""CREATE TABLE IF NOT EXISTS conversation_search_messages (
        thread_id TEXT NOT NULL, turn_id TEXT NOT NULL, item_id TEXT NOT NULL,
        role TEXT NOT NULL, timestamp REAL, text TEXT NOT NULL, folded TEXT NOT NULL,
        position INTEGER NOT NULL,
        PRIMARY KEY (thread_id, turn_id, item_id))""")
    db.execute("CREATE TABLE IF NOT EXISTS conversation_search_dirty (thread_id TEXT PRIMARY KEY)")
    db.execute("""CREATE TABLE IF NOT EXISTS conversation_search_histories (
        thread_id TEXT PRIMARY KEY, status TEXT NOT NULL)""")
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='browser_history'").fetchone():
        return
    for event in ("INSERT", "UPDATE", "DELETE"):
        ref = "OLD" if event == "DELETE" else "NEW"
        db.execute(f"""CREATE TRIGGER IF NOT EXISTS conversation_search_{event.lower()}
            AFTER {event} ON browser_history BEGIN
            INSERT OR REPLACE INTO conversation_search_dirty VALUES ({ref}.thread_id);
            END""")
    db.execute("""INSERT OR IGNORE INTO conversation_search_dirty
        SELECT thread_id FROM browser_history WHERE thread_id NOT IN
        (SELECT thread_id FROM conversation_search_histories)""")
    # Acquire the write lock before reading the dirty queue; a concurrent history
    # update must not have its invalidation removed by this rebuild.
    for row in db.execute("""SELECT d.thread_id, h.history_json FROM conversation_search_dirty d
                             LEFT JOIN browser_history h ON h.thread_id=d.thread_id""").fetchall():
        identity = row["thread_id"]
        db.execute("DELETE FROM conversation_search_messages WHERE thread_id=?", (identity,))
        db.execute("DELETE FROM conversation_search_histories WHERE thread_id=?", (identity,))
        if row["history_json"] is not None:
            try:
                history = json.loads(row["history_json"])
                if not isinstance(history, dict) or not isinstance(history.get("turns", []), list):
                    raise ValueError("Invalid conversation history")
                records = list(messages(history))
                status = "partial" if history.get("historyUnavailable") else "saved"
            except (ValueError, TypeError):
                records, status = [], "unavailable"
            db.executemany("INSERT OR REPLACE INTO conversation_search_messages VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                           [(identity, *record, position) for position, record in enumerate(records)])
            db.execute("INSERT INTO conversation_search_histories VALUES (?, ?)", (identity, status))
        db.execute("DELETE FROM conversation_search_dirty WHERE thread_id=?", (identity,))


def message_condition(terms, role):
    predicates = [f"instr(m.folded, :q{i}) > 0" for i in range(len(terms))]
    if role == "user":
        predicates.append("m.role = 'user'")
    return " AND ".join(predicates)
