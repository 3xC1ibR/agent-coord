#!/usr/bin/env python3
"""Snapshot the real work threads into shared/real-threads.js for the mockups.

Reads the coordination database read-only (the same file the app and CLI use),
joins threads with their latest checkpoint, checkpoint history, lineage
(app-agent creations and terminal delegations), live session state, and
completed-turn counts, and writes a script that sets
window.AGENT_COORD_SNAPSHOT so a mockup opened from file:// can load it.

    python3 docs/agent-display-mockups/export-threads.py            # Now and Later threads
    python3 docs/agent-display-mockups/export-threads.py --archived # include closed threads
    python3 docs/agent-display-mockups/export-threads.py --message-days 30

Agent-to-agent messages from the last --message-days days (default 14) are
included when either side is an exported thread; bodies are truncated.

Nothing here writes to the database.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "agent-coord" / "scripts"))

try:
    from agent_coord.store import default_database_path  # type: ignore
    from agent_coord.organization import app_agent_creators  # type: ignore
except Exception:  # pragma: no cover - the exporter still works without the package
    default_database_path = None
    app_agent_creators = None


def creators_fallback(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name = 'app_control_requests'").fetchone():
        return {}
    rows = db.execute("""SELECT r.thread_id, r.sender_session_id, r.payload_json FROM app_control_requests r
        WHERE r.operation = 'create' AND r.thread_id IS NOT NULL AND r.status IN ('running', 'completed', 'uncertain')
        ORDER BY r.created_at, r.request_id""").fetchall()
    out = {}
    for row in rows:
        if row["thread_id"] == row["sender_session_id"] or row["thread_id"] in out:
            continue
        try:
            mode = (json.loads(row["payload_json"] or "{}").get("mode"))
        except Exception:
            mode = None
        out[row["thread_id"]] = {"thread_id": row["sender_session_id"], "mode": mode}
    return out


def load_messages(db, ids: list, days: float) -> tuple[list, dict]:
    """Messages touching the exported threads, plus names for counterparts outside the snapshot."""
    known = set(ids)
    since = time.time() - days * 86400
    created = set()
    if db.execute("SELECT 1 FROM sqlite_master WHERE name = 'app_control_requests'").fetchone():
        for r in db.execute("SELECT result_json FROM app_control_requests WHERE operation = 'create' AND status = 'completed'"):
            try:
                mid = json.loads(r["result_json"] or "{}").get("message_id")
            except Exception:
                mid = None
            if type(mid) is int:
                created.add(mid)
    closed = {}
    if db.execute("SELECT 1 FROM sqlite_master WHERE name = 'message_threads'").fetchone():
        cols = [c[1] for c in db.execute("PRAGMA table_info(message_threads)")]
        if "closed_at" in cols:
            closed = {r["thread_id"]: r["closed_at"] for r in db.execute("SELECT thread_id, closed_at FROM message_threads")}
    out, others = [], set()
    for r in db.execute("""SELECT id, sender_session_id, recipient_session_id, body, created_at, delivered_at, acknowledged_at, classification,
            thread_id, reply_required, in_reply_to, wake_turn_id, suppressed_at FROM messages WHERE created_at >= ? ORDER BY id""", (since,)):
        a, b = r["sender_session_id"], r["recipient_session_id"]
        if a not in known and b not in known:
            continue
        others.update(x for x in (a, b) if x not in known)
        body = r["body"] or ""
        out.append({"id": r["id"], "from": a, "to": b, "body": body[:240] + ("…" if len(body) > 240 else ""), "at": r["created_at"],
                    "delivered_at": r["delivered_at"], "acked_at": r["acknowledged_at"], "classification": r["classification"],
                    "exchange": r["thread_id"], "reply_required": bool(r["reply_required"]), "in_reply_to": r["in_reply_to"],
                    "creation": r["id"] in created, "woke_turn": bool(r["wake_turn_id"]), "suppressed": bool(r["suppressed_at"]),
                    "exchange_closed": bool(closed.get(r["thread_id"]))})
    parties = {}
    if others:
        marks = ",".join("?" * len(others))
        for r in db.execute(f"""SELECT s.session_id, s.client, s.name, s.cwd, t.title FROM sessions s LEFT JOIN work_threads t ON t.thread_id = s.session_id
                WHERE s.session_id IN ({marks})""", list(others)):
            parties[r["session_id"]] = {"title": r["title"] or r["name"] or r["session_id"][:8], "client": r["client"], "cwd": r["cwd"]}
    return out, parties


def load(path: Path, archived: bool, message_days: float = 14) -> dict:
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    placement = "" if archived else "WHERE attention != 'archived'"
    threads = [dict(r) for r in db.execute(f"SELECT * FROM work_threads {placement} ORDER BY created_at").fetchall()]
    ids = [t["thread_id"] for t in threads]
    marks = ",".join("?" * len(ids)) or "''"
    history: dict[str, list] = {}
    for r in db.execute(f"SELECT thread_id, phase, summary, next_action, next_actor, author, created_at, turn_id FROM thread_checkpoints WHERE thread_id IN ({marks}) ORDER BY id", ids):
        history.setdefault(r["thread_id"], []).append(dict(r))
    completions: dict[str, list] = {}
    for r in db.execute(f"SELECT thread_id, status, completed_at FROM turn_completions WHERE thread_id IN ({marks}) ORDER BY id", ids):
        completions.setdefault(r["thread_id"], []).append(dict(r))
    links: dict[str, list] = {}
    for r in db.execute(f"SELECT thread_id, kind, label, target FROM thread_links WHERE thread_id IN ({marks}) ORDER BY id", ids):
        links.setdefault(r["thread_id"], []).append({"kind": r["kind"], "label": r["label"], "target": r["target"]})
    sessions = {r["session_id"]: dict(r) for r in db.execute(f"SELECT session_id, client, cwd, name, activity, write_scope_json, started_at, last_seen_at, ended_at, turn_active, last_tool_json FROM sessions WHERE session_id IN ({marks})", ids)}
    browser = {r["thread_id"]: dict(r) for r in db.execute(f"SELECT thread_id, client, name, cwd, archived FROM browser_sessions WHERE thread_id IN ({marks})", ids)}
    creators = (app_agent_creators(db) if app_agent_creators else creators_fallback(db)) or {}
    delegations = {r["child_session_id"]: {"thread_id": r["parent_session_id"], "mode": "delegate", "kind": r["runtime_kind"] or "delegate"}
                   for r in db.execute(f"SELECT parent_session_id, child_session_id, runtime_kind FROM delegations WHERE child_session_id IN ({marks})", ids)}
    dispatchers = set()
    if db.execute("SELECT 1 FROM sqlite_master WHERE name = 'dispatch_settings'").fetchone():
        cols = [c[1] for c in db.execute("PRAGMA table_info(dispatch_settings)")]
        key = "thread_id" if "thread_id" in cols else cols[0]
        flag = "enabled" if "enabled" in cols else None
        for r in db.execute(f"SELECT * FROM dispatch_settings"):
            if flag is None or r[flag]:
                dispatchers.add(r[key])
    classifications = {}
    if db.execute("SELECT 1 FROM sqlite_master WHERE name = 'response_classifications'").fetchone():
        cols = [c[1] for c in db.execute("PRAGMA table_info(response_classifications)")]
        if "thread_id" in cols and "choice" in cols:
            for r in db.execute(f"SELECT thread_id, choice FROM response_classifications WHERE thread_id IN ({marks}) ORDER BY id", ids):
                classifications[r["thread_id"]] = r["choice"]
    out = []
    for t in threads:
        tid = t["thread_id"]
        hist = history.get(tid, [])
        latest = hist[-1] if hist else None
        comps = completions.get(tid, [])
        s = sessions.get(tid) or {}
        b = browser.get(tid) or {}
        creator = creators.get(tid)
        parent = {"thread_id": creator["thread_id"], "mode": creator.get("mode"), "kind": "app"} if creator else delegations.get(tid)
        try:
            scope = json.loads(s.get("write_scope_json") or "[]")
        except Exception:
            scope = []
        try:
            last_tool = json.loads(s.get("last_tool_json") or "null")
        except Exception:
            last_tool = None
        out.append({
            "id": tid, "title": t["title"], "title_source": t["title_source"],
            "domain": t.get("domain"), "domain_source": t.get("domain_source"), "scope_cluster": t.get("scope_cluster"),
            "cwd": s.get("cwd") or b.get("cwd") or t.get("project_cwd"), "project_cwd": t.get("project_cwd"),
            "attention": t["attention"], "pinned": bool(t.get("pinned")),
            "client": b.get("client") or s.get("client") or "codex", "app": bool(b), "dispatcher": tid in dispatchers,
            "created_at": t["created_at"], "updated_at": t["updated_at"], "turn_started_at": t.get("turn_started_at"), "turn_id": t.get("turn_id"),
            "original_request": (t.get("original_request") or "")[:400],
            "checkpoint": ({k: latest[k] for k in ("phase", "summary", "next_action", "next_actor", "author", "created_at", "turn_id")} if latest else None),
            "history": [{"at": h["created_at"], "phase": h["phase"], "summary": h["summary"][:240], "author": h["author"]} for h in hist[-8:]],
            "checkpoints": len(hist),
            "completions": len(comps), "last_completion": comps[-1]["status"] if comps else None,
            "parent": parent,
            "links": links.get(tid, [])[:6],
            "classification": classifications.get(tid),
            "session": ({"activity": s.get("activity"), "write_scope": scope, "last_seen_at": s.get("last_seen_at"), "ended_at": s.get("ended_at"),
                         "turn_active": bool(s.get("turn_active")), "last_tool": last_tool, "name": s.get("name")} if s else None),
        })
    messages, parties = load_messages(db, ids, message_days)
    workspaces: dict[str, int] = {}
    for t in out:
        workspaces[t["cwd"] or "unknown"] = workspaces.get(t["cwd"] or "unknown", 0) + 1
    db.close()
    return {"exported_at": time.time(), "source": str(path), "archived_included": archived,
            "workspaces": [{"cwd": k, "count": v} for k, v in sorted(workspaces.items(), key=lambda kv: -kv[1])], "threads": out,
            "message_days": message_days, "messages": messages, "parties": parties}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--db", help="Database path (defaults to the agent-coord state database).")
    parser.add_argument("--archived", action="store_true", help="Include closed threads.")
    parser.add_argument("--message-days", type=float, default=14, help="Include agent messages from the last N days (default 14).")
    parser.add_argument("--out", default=str(HERE / "shared" / "real-threads.js"))
    args = parser.parse_args()
    path = Path(args.db).expanduser() if args.db else (default_database_path() if default_database_path else Path.home() / ".local/state/agent-coord/state.sqlite3")
    if not path.exists():
        print(f"database not found: {path}", file=sys.stderr)
        return 1
    snapshot = load(path, args.archived, args.message_days)
    body = "/* Generated by export-threads.py. A read-only snapshot of real work threads for the mockups. */\nwindow.AGENT_COORD_SNAPSHOT = " + json.dumps(snapshot, ensure_ascii=False) + ";\n"
    Path(args.out).write_text(body, encoding="utf-8")
    print(f"wrote {args.out}: {len(snapshot['threads'])} threads across {len(snapshot['workspaces'])} workspaces, {len(snapshot['messages'])} messages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
