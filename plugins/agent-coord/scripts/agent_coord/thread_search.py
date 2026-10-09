"""Bounded discovery of durable conversations, independent of UI placement.

Search saved routing context, not entire provider transcripts. Pages are live
keyset queries: repeat the search when conversations change during discovery.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math

from .navigation import NavigationStore
from .organization import UNSET
from .store import CoordinationError
from .threads import project_root


def _cursor(value, fingerprint):
    try:
        if not isinstance(value, str) or len(value) > 2048:
            raise ValueError()
        data = json.loads(base64.b64decode(value, altchars=b"-_", validate=True))
        if (data["v"] != 1 or data["query"] != fingerprint
                or type(data["score"]) is not int or data["score"] < 1
                or type(data["time"]) not in (int, float) or not math.isfinite(data["time"])
                or not isinstance(data["id"], str) or not 0 < len(data["id"]) <= 200):
            raise ValueError()
        return data
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise CoordinationError("Invalid search cursor or changed query/filters. Start a new search.") from None


def _snippet(value, terms, limit):
    value = " ".join((value or "").split())
    positions = [value.lower().find(term) for term in terms if term in value.lower()]
    start = max(0, min(positions, default=0) - limit // 4)
    return ("…" if start else "") + value[start:start + limit] + ("…" if len(value) > start + limit else "")


def search_threads(store, query, *, limit=10, cursor=None, cwd=None,
                   repository_id=UNSET, project_id=UNSET, app_only=False):
    if not isinstance(query, str) or not query.strip() or len(query) > 500:
        raise CoordinationError("Search query must contain 1–500 characters.")
    terms = list(dict.fromkeys(query.lower().split()))
    if len(terms) > 10:
        raise CoordinationError("Search with at most 10 keywords.")
    if type(limit) is not int or not 1 <= limit <= 50:
        raise CoordinationError("Search limit must be between 1 and 50.")
    workspace = project_root(cwd) if cwd is not None else None
    filters = {"terms": terms, "cwd": workspace, "app_only": bool(app_only)}
    filters.update({key: value for key, value in (("repository", repository_id), ("project", project_id))
                    if value is not UNSET})
    fingerprint = hashlib.sha256(json.dumps(filters, sort_keys=True).encode()).hexdigest()
    after = _cursor(cursor, fingerprint) if cursor is not None else None
    threads = store.threads
    threads.organization.backfill()
    params = {f"q{i}": term for i, term in enumerate(terms)}
    params["limit"] = limit + 1
    where = []
    if workspace is not None:
        where.append("w.root = :cwd")
        params["cwd"] = workspace
    for key, value in (("repository_id", repository_id), ("project_id", project_id)):
        if value is not UNSET:
            where.append(f"o.{key} IS :{key}")
            params[key] = value
    # Each keyword must match some routing context. Title matches rank above
    # checkpoint, original request and artifact matches; recent work breaks ties.
    fields = {"title": "t.title", "summary": "COALESCE(c.summary, '') || ' ' || COALESCE(c.next_action, '')",
              "request": "t.original_request"}
    scores = []
    for i in range(len(terms)):
        matches = {name: f"instr(lower({expr}), :q{i}) > 0" for name, expr in fields.items()}
        matches["artifact"] = ("EXISTS (SELECT 1 FROM thread_links l WHERE l.thread_id = t.thread_id "
                               f"AND instr(lower(l.label || ' ' || l.target), :q{i}) > 0)")
        where.append("(" + " OR ".join(matches.values()) + ")")
        scores.extend(f"({matches[name]}) * {weight}" for name, weight in
                      (("title", 8), ("summary", 4), ("request", 2), ("artifact", 1)))
    with store._connection() as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        app = "EXISTS (SELECT 1 FROM browser_sessions b WHERE b.thread_id = t.thread_id)" if "browser_sessions" in tables else "0"
        if app_only:
            where.append(app)
        rows = db.execute(f"""WITH ranked AS (
            SELECT t.thread_id, t.title, t.attention, t.original_request, s.client, s.cwd,
                   s.turn_active, o.repository_id, r.name AS repository_name,
                   o.project_id, p.name AS project_name, c.phase, c.summary, c.next_action,
                   c.next_actor, c.created_at AS checkpoint_at, {app} AS app_conversation,
                   done.status AS last_turn_status,
                   MAX(t.created_at, COALESCE(t.turn_started_at, 0),
                       COALESCE(done.completed_at, 0), COALESCE(c.created_at, 0)) AS last_work_at,
                   {' + '.join(scores)} AS score
            FROM work_threads t JOIN sessions s ON s.session_id = t.thread_id
            JOIN work_projects w ON w.id = t.project_id
            JOIN thread_organization o ON o.thread_id = t.thread_id
            LEFT JOIN work_repositories r ON r.id = o.repository_id
            LEFT JOIN named_projects p ON p.id = o.project_id
            LEFT JOIN thread_checkpoints c ON c.id =
                (SELECT MAX(id) FROM thread_checkpoints WHERE thread_id = t.thread_id)
            LEFT JOIN turn_completions done ON done.id =
                (SELECT MAX(id) FROM turn_completions WHERE thread_id = t.thread_id)
            WHERE {' AND '.join(where)}
        ) SELECT * FROM ranked
        {"WHERE (score, last_work_at, thread_id) < (:after_score, :after_time, :after_id)" if after else ""}
        ORDER BY score DESC, last_work_at DESC, thread_id DESC LIMIT :limit""",
            {**params, **({"after_score": after["score"], "after_time": after["time"], "after_id": after["id"]} if after else {})}).fetchall()
        items = []
        for row in rows[:limit]:
            item = dict(row)
            item["original_request"] = _snippet(item["original_request"], terms, 240)
            item["summary"] = _snippet(item["summary"], terms, 320)
            item["next_action"] = _snippet(item["next_action"], terms, 160)
            item["app_conversation"] = bool(item["app_conversation"])
            item["turn_active"] = bool(item["turn_active"])
            item["wake_paused"] = None
            item["wake_pause_reason"] = None
            if item["app_conversation"] and "browser_inbox_wake" in tables:
                wake = db.execute("SELECT paused, error FROM browser_inbox_wake WHERE thread_id = ?", (item["thread_id"],)).fetchone()
                item["wake_paused"] = bool(wake and wake["paused"])
                item["wake_pause_reason"] = _snippet(wake["error"], [], 160) if wake and wake["paused"] else None
            items.append(item)
    navigation = NavigationStore(store)
    for item in items:
        item["url"] = navigation.link(thread=item["thread_id"])["url"]
    next_cursor = None
    if len(rows) > limit:
        last = rows[limit - 1]
        next_cursor = base64.urlsafe_b64encode(json.dumps({"v": 1, "query": fingerprint,
            "score": last["score"], "time": last["last_work_at"], "id": last["thread_id"]}, separators=(",", ":")).encode()).decode()
    return {"items": items, "next_cursor": next_cursor}
