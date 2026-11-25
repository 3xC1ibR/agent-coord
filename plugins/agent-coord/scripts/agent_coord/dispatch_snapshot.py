"""Read a consistent routing inventory without resuming or mutating conversations.

Reads saved prose from ``TranscriptStore`` (``turns.compact.jsonl`` indexed by
``thread_turns``) with read-only fallback to legacy ``browser_history``.
"""
from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import time

from .history_persistence import decode_history
from .history_window import routing_history
from .transcript_store import _pread_exact, thread_directory


def _read_transcript_routing(database_path: Path, db: sqlite3.Connection, thread_id: str) -> dict | None:
    directory = thread_directory(database_path, thread_id)
    meta = None
    meta_path = directory / "meta.json"
    try:
        meta = json.loads(meta_path.read_bytes().decode("utf-8"))
    except (OSError, ValueError, UnicodeError):
        meta = None
    if isinstance(meta, dict) and "__raw_history__" in meta:
        return routing_history(meta["__raw_history__"])

    rows = db.execute(
        """SELECT compact_offset, compact_length FROM thread_turns
           WHERE thread_id = ? ORDER BY turn_seq ASC""",
        (thread_id,),
    ).fetchall()
    if not rows and meta is None:
        return None
    turns = []
    if rows:
        compact_path = directory / "turns.compact.jsonl"
        try:
            min_offset = min(r["compact_offset"] for r in rows)
            max_end = max(r["compact_offset"] + r["compact_length"] for r in rows)
            fd = os.open(compact_path, os.O_RDONLY)
            try:
                buf = _pread_exact(fd, max_end - min_offset, min_offset)
            finally:
                os.close(fd)
            for row in rows:
                rel = row["compact_offset"] - min_offset
                turns.append(json.loads(buf[rel : rel + row["compact_length"]].decode("utf-8", "surrogatepass")))
        except (OSError, ValueError, UnicodeError):
            return {"messages": [], "history_status": "unavailable"}
    base = dict(meta) if isinstance(meta, dict) else {"id": thread_id}
    base["turns"] = turns
    return routing_history(base)


def read_snapshot(database_path, *, now=None, cwd=None):
    path = Path(database_path).expanduser().resolve()
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")}
        required = {"browser_sessions", "sessions", "work_threads", "thread_checkpoints"}
        if not required <= tables:
            raise ValueError("Database does not contain an app conversation inventory")
        browser_columns = {r[1] for r in db.execute("PRAGMA table_info(browser_sessions)")}
        checkpoint_columns = {r[1] for r in db.execute("PRAGMA table_info(thread_checkpoints)")}
        thread_columns = {r[1] for r in db.execute("PRAGMA table_info(work_threads)")}
        cwd_source = "b.cwd" if "cwd" in browser_columns else "s.cwd"
        lifecycle_select = (
            ", c.id AS lifecycle_checkpoint_id, c.context_lifecycle_json, "
            + (
                "c.context_lifecycle_turn_key IS NOT t.turn_key"
                if "turn_key" in thread_columns and "context_lifecycle_turn_key" in checkpoint_columns
                else "0"
            )
            + " AS lifecycle_stale"
            if "context_lifecycle_json" in checkpoint_columns
            else ""
        )
        source = """FROM work_threads t JOIN browser_sessions b ON b.thread_id=t.thread_id
            JOIN sessions s ON s.session_id=t.thread_id"""
        where, params = (f" WHERE {cwd_source}=?", (cwd,)) if cwd is not None else ("", ())
        eligible = "WITH eligible AS (SELECT t.thread_id " + source + where + ") "
        rows = db.execute(
            f"""SELECT t.thread_id, t.title, t.original_request, t.attention,
            t.created_at, {cwd_source} AS cwd, s.client, s.turn_active, s.write_scope_json,
            c.summary, c.phase, c.created_at AS checkpoint_at{lifecycle_select}
            {source}
            LEFT JOIN thread_checkpoints c ON c.id=(SELECT MAX(id) FROM thread_checkpoints
                                                   WHERE thread_id=t.thread_id)
            {where} ORDER BY t.thread_id""",
            params,
        ).fetchall()
        inventory = {}
        for row in rows:
            item = dict(row)
            raw_lifecycle = item.pop("context_lifecycle_json", None)
            lifecycle_id = item.pop("lifecycle_checkpoint_id", None)
            lifecycle_stale = item.pop("lifecycle_stale", False)
            if raw_lifecycle is not None:
                try:
                    report = json.loads(raw_lifecycle)
                    item["context_lifecycle"] = {
                        "checkpoint_id": lifecycle_id,
                        "stale": bool(lifecycle_stale),
                        **{k: report[k] for k in ("recommendation", "reason", "reuse_for", "fresh_for") if k in report},
                    }
                except (ValueError, TypeError, KeyError):
                    pass
            item.update(
                dispatcher=False,
                running=bool(item.pop("turn_active")),
                scopes=json.loads(item.pop("write_scope_json")),
                messages=[],
                history_status="missing",
                wake_paused=False,
                pending_close=False,
                user_queue=0,
                artifacts=[],
                assignments=[],
            )
            item["last_work_at"] = max(item["created_at"], item["checkpoint_at"] or 0)
            item["original_request"] = (item["original_request"] or "")[:6000]
            inventory[item["thread_id"]] = item

        def batch(table, columns="x.*", suffix=""):
            if table not in tables:
                return []
            return db.execute(
                eligible + f"SELECT {columns} FROM {table} x JOIN eligible e ON e.thread_id=x.thread_id " + suffix,
                params,
            )

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
            return db.execute(
                eligible
                + f""", ranked AS (
                SELECT x.{key} AS thread_id,{fields},
                    ROW_NUMBER() OVER (PARTITION BY x.{key} ORDER BY x.id DESC) AS position
                FROM {table} x JOIN eligible e ON e.thread_id=x.{key} WHERE {condition})
                SELECT * FROM ranked WHERE position<=? ORDER BY thread_id,position""",
                (*params, limit),
            )

        for row in latest("turn_completions", "x.status,x.completed_at", 1):
            item = inventory[row["thread_id"]]
            item["last_turn_status"] = row["status"]
            item["last_work_at"] = max(item["last_work_at"], row["completed_at"])
        for row in latest("thread_links", "x.kind,x.label,x.target", 12):
            inventory[row["thread_id"]]["artifacts"].append({k: row[k] for k in ("kind", "label", "target")})
        for row in latest(
            "messages",
            "x.id,x.body,x.created_at,x.delivered_at",
            3,
            key="recipient_session_id",
            condition="x.classification='action_required' AND x.in_reply_to IS NULL AND x.suppressed_at IS NULL",
        ):
            inventory[row["thread_id"]]["assignments"].append(
                {
                    "id": row["id"],
                    "text": row["body"].split("User request:\n", 1)[-1][:4000],
                    "timestamp": row["created_at"],
                    "pending": row["delivered_at"] is None,
                }
            )

        resolved_threads: set[str] = set()
        if "thread_turns" in tables:
            for tid in inventory:
                evidence = _read_transcript_routing(path, db, tid)
                if evidence is not None:
                    inventory[tid].update(evidence)
                    resolved_threads.add(tid)
        else:
            for tid in inventory:
                if (thread_directory(path, tid) / "meta.json").exists():
                    evidence = _read_transcript_routing(path, db, tid)
                    if evidence is not None:
                        inventory[tid].update(evidence)
                        resolved_threads.add(tid)

        if "browser_history" in tables:
            for row in batch("browser_history", "x.thread_id,x.history_json"):
                tid = row["thread_id"]
                if tid in resolved_threads:
                    continue
                try:
                    evidence = routing_history(decode_history(row["history_json"], db=db))
                except (ValueError, TypeError, AttributeError):
                    evidence = {"messages": [], "history_status": "unavailable"}
                inventory[tid].update(evidence)

        return {
            "version": 1,
            "captured_at": time.time() if now is None else now,
            "threads": list(inventory.values()),
            "read_only": True,
        }
