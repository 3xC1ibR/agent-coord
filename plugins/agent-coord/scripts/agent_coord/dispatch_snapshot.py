"""Read a consistent routing inventory without resuming or mutating conversations.

The incremental prose projection is optional: stale or absent entries fall back
only to their saved histories. Standalone evaluation never refreshes the index.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from .conversation_search import routing_history


def read_snapshot(database_path, *, now=None, cwd=None):
    path = Path(database_path).expanduser().resolve()
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {"browser_sessions", "sessions", "work_threads", "thread_checkpoints"}
        if not required <= tables:
            raise ValueError("Database does not contain an app conversation inventory")
        browser_columns = {r[1] for r in db.execute("PRAGMA table_info(browser_sessions)")}
        cwd_source = "b.cwd" if "cwd" in browser_columns else "s.cwd"
        source = """FROM work_threads t JOIN browser_sessions b ON b.thread_id=t.thread_id
            JOIN sessions s ON s.session_id=t.thread_id"""
        where, params = (f" WHERE {cwd_source}=?", (cwd,)) if cwd is not None else ("", ())
        eligible = "WITH eligible AS (SELECT t.thread_id " + source + where + ") "
        rows = db.execute(f"""SELECT t.thread_id, t.title, t.original_request, t.attention,
            t.created_at, {cwd_source} AS cwd, s.client, s.turn_active, s.write_scope_json,
            c.summary, c.phase, c.created_at AS checkpoint_at
            {source}
            LEFT JOIN thread_checkpoints c ON c.id=(SELECT MAX(id) FROM thread_checkpoints
                                                   WHERE thread_id=t.thread_id)
            {where} ORDER BY t.thread_id""", params).fetchall()
        inventory = {}
        for row in rows:
            item = dict(row)
            item.update(dispatcher=False, running=bool(item.pop("turn_active")),
                        scopes=json.loads(item.pop("write_scope_json")),
                        messages=[], history_status="missing", wake_paused=False,
                        pending_close=False, user_queue=0, artifacts=[], assignments=[])
            item["last_work_at"] = max(item["created_at"], item["checkpoint_at"] or 0)
            item["original_request"] = (item["original_request"] or "")[:6000]
            inventory[item["thread_id"]] = item

        def batch(table, columns="x.*", suffix=""):
            if table not in tables:
                return []
            return db.execute(eligible + f"SELECT {columns} FROM {table} x JOIN eligible e "
                              "ON e.thread_id=x.thread_id " + suffix, params)

        for row in batch("dispatch_settings", "x.thread_id", "WHERE x.enabled=1"):
            inventory[row[0]]["dispatcher"] = True
        for row in batch("browser_inbox_wake", "x.thread_id,x.paused"):
            inventory[row[0]]["wake_paused"] = bool(row[1])
        for row in batch("thread_close_requests", "x.thread_id", "WHERE x.status IN ('queued','closing')"):
            inventory[row[0]]["pending_close"] = True
        for row in batch("browser_message_queue", "x.thread_id,COUNT(*)", "GROUP BY x.thread_id"):
            inventory[row[0]]["user_queue"] = row[1]

        def latest(table, fields, limit, *, key="thread_id", condition="1"):
            if table not in tables:
                return []
            return db.execute(eligible + f""", ranked AS (
                SELECT x.{key} AS thread_id,{fields},
                    ROW_NUMBER() OVER (PARTITION BY x.{key} ORDER BY x.id DESC) AS position
                FROM {table} x JOIN eligible e ON e.thread_id=x.{key} WHERE {condition})
                SELECT * FROM ranked WHERE position<=? ORDER BY thread_id,position""", (*params, limit))

        for row in latest("turn_completions", "x.status,x.completed_at", 1):
            item = inventory[row["thread_id"]]
            item["last_turn_status"] = row["status"]
            item["last_work_at"] = max(item["last_work_at"], row["completed_at"])
        for row in latest("thread_links", "x.kind,x.label,x.target", 12):
            inventory[row["thread_id"]]["artifacts"].append({k: row[k] for k in ("kind", "label", "target")})
        for row in latest("messages", "x.id,x.body,x.created_at,x.delivered_at", 3,
                          key="recipient_session_id", condition="x.classification='action_required' AND x.in_reply_to IS NULL AND x.suppressed_at IS NULL"):
            inventory[row["thread_id"]]["assignments"].append({
                "id": row["id"], "text": row["body"].split("User request:\n", 1)[-1][:4000],
                "timestamp": row["created_at"], "pending": row["delivered_at"] is None})

        if "browser_history" in tables:
            triggers = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
            cached = "conversation_search_routing" in tables and all(
                "conversation_routing_" + event in triggers for event in ("insert", "update", "delete"))
            if cached:
                histories = db.execute(eligible + """SELECT h.thread_id,r.evidence_json,
                    CASE WHEN r.thread_id IS NULL THEN h.history_json END AS history_json
                    FROM browser_history h JOIN eligible e ON e.thread_id=h.thread_id
                    LEFT JOIN conversation_search_routing r ON r.thread_id=h.thread_id""", params)
            else:
                histories = batch("browser_history", "x.thread_id,NULL AS evidence_json,x.history_json")
            for row in histories:
                try:
                    evidence = (json.loads(row["evidence_json"]) if row["evidence_json"] is not None
                                else routing_history(json.loads(row["history_json"])))
                except (ValueError, TypeError, AttributeError):
                    evidence = {"messages": [], "history_status": "unavailable"}
                inventory[row["thread_id"]].update(evidence)
        return {"version": 1, "captured_at": time.time() if now is None else now,
                "threads": list(inventory.values()), "read_only": True}
