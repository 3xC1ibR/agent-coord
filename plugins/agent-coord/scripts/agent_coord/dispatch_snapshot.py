"""Read a consistent routing inventory without resuming or mutating conversations.

This adapter deliberately does not instantiate CoordinationStore: its migrations,
search-index refreshes and provider reads are unnecessary for shadow evaluation.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from .conversation_search import messages


def read_snapshot(database_path, *, now=None):
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
        rows = db.execute(f"""SELECT t.thread_id, t.title, t.original_request, t.attention,
            t.created_at, {cwd_source} AS cwd, s.client, s.turn_active, s.write_scope_json,
            c.summary, c.phase, c.created_at AS checkpoint_at
            FROM work_threads t JOIN browser_sessions b ON b.thread_id=t.thread_id
            JOIN sessions s ON s.session_id=t.thread_id
            LEFT JOIN thread_checkpoints c ON c.id=(SELECT MAX(id) FROM thread_checkpoints
                                                   WHERE thread_id=t.thread_id)
            ORDER BY t.thread_id""").fetchall()
        inventory = []
        for row in rows:
            item = dict(row)
            tid = item["thread_id"]
            item["dispatcher"] = "dispatch_settings" in tables and bool(db.execute(
                "SELECT 1 FROM dispatch_settings WHERE thread_id=? AND enabled=1", (tid,)).fetchone())
            item["scopes"] = json.loads(item.pop("write_scope_json"))
            item["running"] = bool(item.pop("turn_active"))
            item["messages"] = []
            item["history_status"] = "missing"
            if "browser_history" in tables:
                history = db.execute("SELECT history_json FROM browser_history WHERE thread_id=?", (tid,)).fetchone()
                if history:
                    try:
                        data = json.loads(history[0])
                        records = list(messages(data))
                        item["history_status"] = "partial" if data.get("historyUnavailable") else "saved"
                        # Keep prose only. Tool output and provider setup are not routing evidence.
                        item["messages"] = [{"turn_id": r[0], "item_id": r[1], "role": r[2],
                                             "timestamp": r[3], "text": r[4][:6000],
                                             "truncated": len(r[4]) > 6000} for r in records[-200:]]
                        item["history_truncated"] = len(records) > 200
                    except (ValueError, TypeError, AttributeError):
                        item["history_status"] = "unavailable"
            item["last_work_at"] = max(item["created_at"], item["checkpoint_at"] or 0)
            if "turn_completions" in tables:
                last = db.execute("SELECT status, completed_at FROM turn_completions WHERE thread_id=? ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
                if last:
                    item["last_turn_status"] = last[0]
                    item["last_work_at"] = max(item["last_work_at"], last[1])
            item["wake_paused"] = False
            if "browser_inbox_wake" in tables:
                wake = db.execute("SELECT paused FROM browser_inbox_wake WHERE thread_id=?", (tid,)).fetchone()
                item["wake_paused"] = bool(wake and wake[0])
            item["pending_close"] = "thread_close_requests" in tables and bool(db.execute(
                "SELECT 1 FROM thread_close_requests WHERE thread_id=? AND status IN ('queued','closing')", (tid,)).fetchone())
            item["user_queue"] = (db.execute("SELECT COUNT(*) FROM browser_message_queue WHERE thread_id=?", (tid,)).fetchone()[0]
                                  if "browser_message_queue" in tables else 0)
            item["artifacts"] = ([dict(r) for r in db.execute(
                "SELECT kind,label,target FROM thread_links WHERE thread_id=? ORDER BY id DESC LIMIT 12", (tid,))]
                if "thread_links" in tables else [])
            item["assignments"] = []
            if "messages" in tables:
                for assignment in db.execute("""SELECT id,body,created_at,delivered_at FROM messages
                    WHERE recipient_session_id=? AND classification='action_required'
                    AND in_reply_to IS NULL AND suppressed_at IS NULL ORDER BY id DESC LIMIT 3""", (tid,)):
                    body = assignment[1].split("User request:\n", 1)[-1]
                    item["assignments"].append({"id": assignment[0], "text": body[:4000],
                                                "timestamp": assignment[2], "pending": assignment[3] is None})
            # Large original setup records are not useful in full to a router.
            item["original_request"] = (item["original_request"] or "")[:6000]
            inventory.append(item)
        return {"version": 1, "captured_at": time.time() if now is None else now,
                "threads": inventory, "read_only": True}
