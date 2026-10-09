"""Bounded discovery of durable conversations, independent of UI placement.

Search saved routing context and indexed conversation prose. Pages are live
keyset queries: repeat the search when conversations change during discovery.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
from urllib.parse import urlencode

from .navigation import NavigationStore
from .organization import UNSET
from .store import CoordinationError
from .threads import project_root
from .conversation_search import message_condition, sync_messages


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
    folded = value.casefold()
    positions = [folded.find(term) for term in terms if term in folded]
    position = min(positions, default=0)
    if len(folded) != len(value):
        offset = 0
        for index, char in enumerate(value):
            if offset >= position:
                position = index
                break
            offset += len(char.casefold())
    start = max(0, position - limit // 4)
    return ("…" if start else "") + value[start:start + limit] + ("…" if len(value) > start + limit else "")


def search_threads(store, query, *, limit=10, cursor=None, cwd=None,
                   repository_id=UNSET, project_id=UNSET, app_only=False,
                   source="all", my_messages=False, thread_ids=None):
    if not isinstance(query, str) or not query.strip() or len(query) > 500:
        raise CoordinationError("Search query must contain 1–500 characters.")
    terms = list(dict.fromkeys(query.casefold().split()))
    if len(terms) > 10:
        raise CoordinationError("Search with at most 10 keywords.")
    if type(limit) is not int or not 1 <= limit <= 50:
        raise CoordinationError("Search limit must be between 1 and 50.")
    if source not in {"all", "context", "messages"}:
        raise CoordinationError("Search source must be all, context, or messages.")
    if my_messages:
        source = "messages"
    workspace = project_root(cwd) if cwd is not None else None
    filters = {"terms": terms, "cwd": workspace, "app_only": bool(app_only),
               "source": source, "my_messages": bool(my_messages),
               "thread_ids": sorted(thread_ids) if thread_ids is not None else None}
    filters.update({key: value for key, value in (("repository", repository_id), ("project", project_id))
                    if value is not UNSET})
    fingerprint = hashlib.sha256(json.dumps(filters, sort_keys=True).encode()).hexdigest()
    after = _cursor(cursor, fingerprint) if cursor is not None else None
    threads = store.threads
    threads.organization.backfill()
    params = {f"q{i}": term for i, term in enumerate(terms)}
    params["limit"] = limit + 1
    where = []
    if thread_ids is not None:
        where.append("t.thread_id IN (SELECT value FROM json_each(:thread_ids))")
        params["thread_ids"] = json.dumps(sorted(thread_ids))
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
    scores, context_matches = [], []
    for i in range(len(terms)):
        matches = {name: f"instr(search_fold({expr}), :q{i}) > 0" for name, expr in fields.items()}
        matches["artifact"] = ("EXISTS (SELECT 1 FROM thread_links l WHERE l.thread_id = t.thread_id "
                               f"AND instr(search_fold(l.label || ' ' || l.target), :q{i}) > 0)")
        context_matches.append("(" + " OR ".join(matches.values()) + ")")
        scores.extend(f"({matches[name]}) * {weight}" for name, weight in
                      (("title", 8), ("summary", 4), ("request", 2), ("artifact", 1)))
    with store._connection() as db:
        db.create_function("search_fold", 1, lambda value: (value or "").casefold(), deterministic=True)
        sync_messages(db)
        message_where = message_condition(terms, "user" if my_messages else None)
        message_match = ("EXISTS (SELECT 1 FROM conversation_search_messages m "
                         f"WHERE m.thread_id=t.thread_id AND {message_where})")
        context_match = "(" + " AND ".join(context_matches) + ")"
        scores.append(f"({message_match}) * 6" if source != "context" else "0")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        app = "EXISTS (SELECT 1 FROM browser_sessions b WHERE b.thread_id = t.thread_id)" if "browser_sessions" in tables else "0"
        if app_only:
            where.append(app)
        # Coverage describes the searched inventory, before the keyword filter.
        joins = """FROM work_threads t JOIN sessions s ON s.session_id=t.thread_id
            JOIN work_projects w ON w.id=t.project_id
            JOIN thread_organization o ON o.thread_id=t.thread_id"""
        coverage = dict(db.execute(f"""SELECT COUNT(*) AS threads,
            SUM(h.status='saved') AS saved_histories,
            SUM(h.status='partial') AS partial_histories
            {joins} LEFT JOIN conversation_search_histories h ON h.thread_id=t.thread_id
            WHERE {' AND '.join(where) or '1'}""", params).fetchone())
        coverage = {key: value or 0 for key, value in coverage.items()}
        coverage["missing_histories"] = coverage["threads"] - coverage["saved_histories"] - coverage["partial_histories"]
        coverage["scope"] = "Registered threads and saved message history; external provider transcripts are not searched."
        where.append(context_match if source == "context" else message_match if source == "messages"
                     else f"({context_match} OR {message_match})")
        rows = db.execute(f"""WITH ranked AS (
            SELECT t.thread_id, t.title, t.attention, t.original_request, s.client, s.cwd,
                   s.turn_active, o.repository_id, r.name AS repository_name,
                   o.project_id, p.name AS project_name, c.phase, c.summary, c.next_action,
                   c.next_actor, c.created_at AS checkpoint_at, {app} AS app_conversation,
                   {context_match} AS context_match,
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
            item["context_match"] = bool(item["context_match"]) and source != "messages"
            item["matches"] = []
            item["message_count"] = 0
            if source != "context":
                values = {**params, "thread": item["thread_id"]}
                item["message_count"] = db.execute(f"""SELECT COUNT(*) FROM conversation_search_messages m
                    WHERE m.thread_id=:thread AND {message_where}""", values).fetchone()[0]
                for match in db.execute(f"""SELECT turn_id, item_id, role, timestamp, text
                    FROM conversation_search_messages m WHERE m.thread_id=:thread AND {message_where}
                    ORDER BY position DESC LIMIT 3""", values):
                    result = dict(match)
                    result["excerpt"] = _snippet(result.pop("text"), terms, 320)
                    item["matches"].append(result)
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
        for match in item["matches"]:
            match["url"] = item["url"] + "&" + urlencode({"turn": match["turn_id"], "item": match["item_id"]})
    next_cursor = None
    if len(rows) > limit:
        last = rows[limit - 1]
        next_cursor = base64.urlsafe_b64encode(json.dumps({"v": 1, "query": fingerprint,
            "score": last["score"], "time": last["last_work_at"], "id": last["thread_id"]}, separators=(",", ":")).encode()).decode()
    return {"items": items, "next_cursor": next_cursor, "coverage": coverage}
