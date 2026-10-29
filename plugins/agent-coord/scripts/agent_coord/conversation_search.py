"""Incremental, local search projection of saved conversation prose.

This never resumes providers, imports transcripts, or searches tool output.
History triggers invalidate the projection even when an older runtime saves it.
"""
from __future__ import annotations

import json
import sqlite3

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


def routing_history(history, records=None):
    """The lossless, bounded prose view used by routing (including duplicate IDs)."""
    records = list(messages(history)) if records is None else records
    return {"history_status": "partial" if history.get("historyUnavailable") else "saved",
            "messages": [{"turn_id": r[0], "item_id": r[1], "role": r[2], "timestamp": r[3],
                          "text": r[4][:6000], "truncated": len(r[4]) > 6000} for r in records[-200:]],
            "history_truncated": len(records) > 200}


# The complete projection schema: four tables and six history triggers.
_SCHEMA = ("conversation_search_messages", "conversation_search_dirty", "conversation_search_histories",
           "conversation_search_routing", *(f"conversation_{kind}_{event}" for kind in ("search", "routing")
                                            for event in ("insert", "update", "delete")))
# Read-only: whether any history awaits indexing. This avoids the writer lock
# on searches while agents stream.
_CURRENT = f"""SELECT (SELECT COUNT(*) FROM sqlite_master WHERE name IN ({", ".join("?" * len(_SCHEMA))})) = ?
    AND NOT EXISTS (SELECT 1 FROM conversation_search_dirty)
    AND NOT EXISTS (SELECT 1 FROM browser_history WHERE thread_id NOT IN
        (SELECT thread_id FROM conversation_search_histories)
        OR thread_id NOT IN (SELECT thread_id FROM conversation_search_routing))"""
# Native parameters keep REAL timestamps exact; 100 rows stay within 999 variables.
_INSERT_ROWS = 100


def _current(db):
    try:
        return bool(db.execute(_CURRENT, (*_SCHEMA, len(_SCHEMA))).fetchone()[0])
    except sqlite3.OperationalError:
        # A table is missing; the full path below creates it.
        return False


def sync_messages(db, *, cwd=None):
    """Backfill once, then rebuild only changed histories in this transaction."""
    if _current(db):
        return
    db.execute("""CREATE TABLE IF NOT EXISTS conversation_search_messages (
        thread_id TEXT NOT NULL, turn_id TEXT NOT NULL, item_id TEXT NOT NULL,
        role TEXT NOT NULL, timestamp REAL, text TEXT NOT NULL, folded TEXT NOT NULL,
        position INTEGER NOT NULL,
        PRIMARY KEY (thread_id, turn_id, item_id))""")
    db.execute("CREATE TABLE IF NOT EXISTS conversation_search_dirty (thread_id TEXT PRIMARY KEY)")
    db.execute("""CREATE TABLE IF NOT EXISTS conversation_search_histories (
        thread_id TEXT PRIMARY KEY, status TEXT NOT NULL)""")
    # Separate table keeps older search runtimes compatible. Independent
    # invalidation remains effective even if an older runtime drains dirty.
    db.execute("""CREATE TABLE IF NOT EXISTS conversation_search_routing (
        thread_id TEXT PRIMARY KEY, evidence_json TEXT NOT NULL)""")
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='browser_history'").fetchone():
        return
    for event in ("INSERT", "UPDATE", "DELETE"):
        ref = "OLD" if event == "DELETE" else "NEW"
        db.execute(f"""CREATE TRIGGER IF NOT EXISTS conversation_search_{event.lower()}
            AFTER {event} ON browser_history BEGIN
            INSERT OR REPLACE INTO conversation_search_dirty VALUES ({ref}.thread_id);
            END""")
        db.execute(f"""CREATE TRIGGER IF NOT EXISTS conversation_routing_{event.lower()}
            AFTER {event} ON browser_history BEGIN
            DELETE FROM conversation_search_routing WHERE thread_id={ref}.thread_id;
            END""")
    db.execute("""INSERT OR IGNORE INTO conversation_search_dirty
        SELECT thread_id FROM browser_history WHERE thread_id NOT IN
        (SELECT thread_id FROM conversation_search_histories)
        OR thread_id NOT IN (SELECT thread_id FROM conversation_search_routing)""")
    # Acquire the write lock before reading the dirty queue; a concurrent history
    # update must not have its invalidation removed by this rebuild.
    scope, params = "", ()
    if cwd is not None:
        columns = {r[1] for r in db.execute("PRAGMA table_info(browser_sessions)")}
        source = "b.cwd" if "cwd" in columns else "s.cwd"
        scope = f""" WHERE d.thread_id IN (SELECT b.thread_id FROM browser_sessions b
            JOIN sessions s ON s.session_id=b.thread_id WHERE {source}=?)"""
        params = (cwd,)
    rows = db.execute("""SELECT d.thread_id, h.history_json FROM conversation_search_dirty d
                         LEFT JOIN browser_history h ON h.thread_id=d.thread_id""" + scope, params).fetchall()
    if not rows:
        return
    # A fixed number of statements per rebuild: each statement step can wait
    # for the GIL behind other request threads.
    records, histories = [], []
    for row in rows:
        identity = row["thread_id"]
        if row["history_json"] is not None:
            routing = {"messages": [], "history_status": "unavailable"}
            try:
                history = json.loads(row["history_json"])
                found = list(messages(history))
                routing = routing_history(history, found)
                if not isinstance(history, dict) or not isinstance(history.get("turns", []), list):
                    raise ValueError("Invalid conversation history")
                status = "partial" if history.get("historyUnavailable") else "saved"
            except (ValueError, TypeError, AttributeError):
                found, status = [], "unavailable"
            records.extend((identity, *record, position) for position, record in enumerate(found))
            histories.append([identity, status, json.dumps(routing)])
    identities = json.dumps([row["thread_id"] for row in rows])
    for table in ("conversation_search_messages", "conversation_search_histories", "conversation_search_routing"):
        db.execute(f"DELETE FROM {table} WHERE thread_id IN (SELECT value FROM json_each(?))", (identities,))
    for start in range(0, len(records), _INSERT_ROWS):
        chunk = records[start:start + _INSERT_ROWS]
        # Later duplicate IDs replace earlier ones, as with sequential inserts.
        db.execute("INSERT OR REPLACE INTO conversation_search_messages VALUES "
                   + ", ".join(["(?, ?, ?, ?, ?, ?, ?, ?)"] * len(chunk)), [value for record in chunk for value in record])
    if histories:
        histories = json.dumps(histories)
        db.execute("""INSERT INTO conversation_search_histories SELECT json_extract(value, '$[0]'),
                      json_extract(value, '$[1]') FROM json_each(?)""", (histories,))
        db.execute("""INSERT INTO conversation_search_routing SELECT json_extract(value, '$[0]'),
                      json_extract(value, '$[2]') FROM json_each(?)""", (histories,))
    db.execute("DELETE FROM conversation_search_dirty WHERE thread_id IN (SELECT value FROM json_each(?))", (identities,))


def message_condition(terms, role):
    predicates = [f"instr(m.folded, :q{i}) > 0" for i in range(len(terms))]
    if role == "user":
        predicates.append("m.role = 'user'")
    return " AND ".join(predicates)
