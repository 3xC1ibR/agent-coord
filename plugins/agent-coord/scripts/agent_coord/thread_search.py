"""Bounded discovery of durable conversations backed by isolated search.sqlite3.

Context metadata is read directly from ``state.sqlite3`` in query-only mode
(zero tables or triggers added to ``state.sqlite3``), while finalized turn prose
is queried from ``<db_dir>/search.sqlite3`` via FTS5 trigram + ``bm25()``.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from functools import lru_cache
import hashlib
import heapq
import json
import math
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from .history_persistence import decode_history
from .history_window import messages, turn_messages
from .organization import UNSET
from .store import CoordinationError
from .threads import project_root

_SPACE = "".join(chr(code) for code in range(sys.maxunicode + 1) if chr(code).isspace()) if False else ""
import sys as _sys  # noqa: E402
_SPACE = "".join(chr(code) for code in range(_sys.maxunicode + 1) if chr(code).isspace())
_SCALE = 4503599627370496  # 2**52
_MEASURE_BATCH = 24
_CONTEXT_WEIGHTS = {"title": 3, "summary": 1, "request": 1}


def _cursor(value, fingerprint: str) -> dict:
    try:
        if not isinstance(value, str) or len(value) > 2048:
            raise ValueError()
        data = json.loads(base64.b64decode(value, altchars=b"-_", validate=True))
        if (
            data["v"] != 1
            or data["query"] != fingerprint
            or type(data["score"]) is not int
            or data["score"] < 1
            or type(data["time"]) not in (int, float)
            or not math.isfinite(data["time"])
            or not isinstance(data["id"], str)
            or not 0 < len(data["id"]) <= 200
        ):
            raise ValueError()
        return data
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise CoordinationError("Invalid search cursor or changed query/filters. Start a new search.") from None


def _match_span(folded: str, terms: list[str], phrase: str):
    if phrase and (position := folded.find(phrase)) >= 0:
        return position, position + len(phrase)
    positions = [(folded.find(term), i) for i, term in enumerate(terms)]
    if not positions or any(position < 0 for position, _ in positions):
        return None
    heapq.heapify(positions)
    right = max(position + len(terms[i]) for position, i in positions)
    best = (positions[0][0], right)
    while positions:
        left, i = heapq.heappop(positions)
        if right - left < best[1] - best[0]:
            best = (left, right)
        following = folded.find(terms[i], left + 1)
        if following < 0:
            break
        heapq.heappush(positions, (following, i))
        right = max(right, following + len(terms[i]))
    return best


def _normalize(value: str | None) -> str:
    return " ".join((value or "").casefold().split())


@lru_cache(maxsize=4096)
def _folded_context(
    title_val: str,
    domain_val: str,
    summary_val: str,
    request_val: str,
    artifacts_val: str,
) -> dict[str, str]:
    return {
        "title": _normalize(title_val),
        "domain": _normalize(domain_val),
        "summary": _normalize(summary_val),
        "request": _normalize(request_val),
        "artifacts": _normalize(artifacts_val),
    }


def _relevance_folded(folded: str, terms: list[str], phrase: str) -> int:
    span = _match_span(folded, terms, phrase)
    if span is None:
        return 0
    gap = max(0, span[1] - span[0] - sum(map(len, terms)))
    return 1 + max(0, 24 - gap // 8) + 32 * (phrase in folded) + 16 * (folded == phrase)


def _relevance(value: str | None, terms: list[str], phrase: str) -> int:
    return _relevance_folded(_normalize(value), terms, phrase)


def _snippet(value: str | None, terms: list[str], limit: int, phrase: str | None = None) -> str:
    value = " ".join((value or "").split())
    folded = value.casefold()
    span = _match_span(folded, terms, phrase or " ".join(terms))
    position = min((folded.find(term) for term in terms if term in folded), default=0)
    position, end = span or (position, position)
    if len(folded) != len(value):
        offset = 0
        original_position = None
        for index, char in enumerate(value):
            if original_position is None and offset + len(char.casefold()) > position:
                original_position = index
            if offset >= end:
                end = index
                break
            offset += len(char.casefold())
        else:
            end = len(value)
        position = original_position if original_position is not None else position
    start = max(0, position - limit // 4)
    if end - position <= limit:
        start = max(start, end - limit)
    return ("…" if start else "") + value[start : start + limit] + ("…" if len(value) > start + limit else "")


def _number(column: str) -> str:
    whole = f"CAST({column} AS INTEGER)"
    fraction = f"(({column} - {whole}) * 4503599627370496.0)"
    return (
        f"CASE typeof({column}) WHEN 'real' THEN json_array({whole}, CAST({fraction} AS INTEGER), "
        f"CAST(({fraction} - CAST({fraction} AS INTEGER)) * 4503599627370496.0 AS INTEGER), "
        f"{column}) ELSE {column} END"
    )


def _decode_number(value):
    if isinstance(value, str) and value.startswith("binary64:"):
        return float.fromhex(value[9:])
    if not isinstance(value, list):
        return value
    whole, fraction, remainder, rendered = value
    exact = whole + (fraction + remainder / _SCALE) / _SCALE
    return exact if math.isclose(exact, rendered, rel_tol=1e-9) else rendered


def _thread_url(database: str, thread_id: str) -> str:
    return "agentcoord://thread/" + quote(thread_id, safe="") + "?" + urlencode({"database": database})


@contextmanager
def _readonly_state_connection(store):
    while True:
        with store.reading(), store._connection() as db:
            names, organization_missing = db.execute(
                """SELECT
                    (SELECT json_group_array(name) FROM sqlite_master WHERE type='table' AND name IN
                     ('browser_sessions', 'browser_history', 'browser_inbox_wake', 'thread_turns')),
                    EXISTS (SELECT 1 FROM work_threads t LEFT JOIN thread_organization o ON o.thread_id=t.thread_id
                            WHERE o.thread_id IS NULL)"""
            ).fetchone()
            tables = set(json.loads(names))
            if not organization_missing or store.read_only:
                yield db, tables
                return
        store.threads.organization.backfill()


def _sync_unindexed_histories(store, db, tables: set[str], scope_ids: list[str]) -> tuple[set[str], set[str], dict[str, list[tuple]]]:
    """Ensure finalized turns in scope are indexed in search.sqlite3; return coverage & live in-progress hits."""
    search = store.search_store
    if search._pending_tasks > 0:
        search.flush(timeout=0.05)
    saved_ids: set[str] = set()
    partial_ids: set[str] = set()
    live_prose: dict[str, list[tuple]] = {}
    if not scope_ids:
        return saved_ids, partial_ids, live_prose

    tstore = store.transcripts
    all_saved, all_partial = tstore.coverage_sets(db, tables, search)
    scope_set = set(scope_ids)
    saved_ids = all_saved & scope_set
    partial_ids = all_partial & scope_set

    # Legacy fallback for unmigrated browser_history rows in standalone tests.
    if "browser_history" in tables:
        remaining = [tid for tid in scope_ids if tid not in saved_ids and tid not in partial_ids]
        if remaining:
            indexed_ids = search.indexed_thread_ids()
            legacy_rows = db.execute(
                """SELECT thread_id, history_json FROM browser_history
                   WHERE thread_id IN (SELECT value FROM json_each(?))""",
                (json.dumps(remaining),),
            ).fetchall()
            for row in legacy_rows:
                tid = row["thread_id"]
                try:
                    history = decode_history(row["history_json"], db=db)
                except Exception:
                    partial_ids.add(tid)
                    continue
                if not isinstance(history, dict):
                    partial_ids.add(tid)
                    continue
                if history.get("historyUnavailable"):
                    partial_ids.add(tid)
                else:
                    saved_ids.add(tid)
                turns = history.get("turns") or []
                finalized_pairs = []
                for seq, turn in enumerate(turns):
                    if not isinstance(turn, dict):
                        continue
                    if turn.get("status") == "inProgress":
                        live_prose.setdefault(tid, []).extend(turn_messages(turn, seq))
                    elif tid not in indexed_ids:
                        finalized_pairs.append((seq, turn))
                if finalized_pairs:
                    search.index_turns_sync(tid, finalized_pairs)

    return saved_ids, partial_ids, live_prose


def search_threads(
    store,
    query: str,
    *,
    limit: int = 10,
    cursor: str | None = None,
    cwd: str | None = None,
    repository_id=UNSET,
    project_id=UNSET,
    app_only: bool = False,
    source: str = "all",
    my_messages: bool = False,
    thread_ids=None,
    schedule_index=None,
    snapshot_dirty: bool = False,
    resident_live_turns: dict[str, dict] | None = None,
) -> dict[str, Any]:
    del schedule_index, snapshot_dirty
    if not isinstance(query, str) or not query.strip() or len(query) > 500:
        raise CoordinationError("Search query must contain 1–500 characters.")
    terms = list(dict.fromkeys(query.casefold().split()))
    phrase = " ".join(query.casefold().split())
    if len(terms) > 10:
        raise CoordinationError("Search with at most 10 keywords.")
    if type(limit) is not int or not 1 <= limit <= 50:
        raise CoordinationError("Search limit must be between 1 and 50.")
    if source not in {"all", "context", "messages"}:
        raise CoordinationError("Search source must be all, context, or messages.")
    if my_messages:
        source = "messages"
    workspace = project_root(cwd) if cwd is not None else None
    filters: dict[str, Any] = {
        "ranking": 3,
        "terms": terms,
        "phrase": phrase,
        "cwd": workspace,
        "app_only": bool(app_only),
        "source": source,
        "my_messages": bool(my_messages),
        "thread_ids": sorted(thread_ids) if thread_ids is not None else None,
    }
    filters.update(
        {key: value for key, value in (("repository", repository_id), ("project", project_id)) if value is not UNSET}
    )
    fingerprint = hashlib.sha256(json.dumps(filters, sort_keys=True).encode()).hexdigest()
    after_data = _cursor(cursor, fingerprint) if cursor is not None else None
    after = (after_data["score"], after_data["time"], after_data["id"]) if after_data else None

    use_context = source != "messages"
    use_messages = source != "context"

    where = []
    params: dict[str, Any] = {}
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

    with store._writer.measure_request("search.query"), _readonly_state_connection(store) as (db, tables):
        app_expr = (
            "EXISTS (SELECT 1 FROM browser_sessions b WHERE b.thread_id = t.thread_id)"
            if "browser_sessions" in tables
            else "0"
        )
        if app_only:
            where.append(app_expr)
        has_scope_filter = bool(where)
        db.execute("BEGIN")
        try:
            scope_ids = [
                row[0]
                for row in db.execute(
                    f"""SELECT t.thread_id
                        FROM work_threads t
                        JOIN sessions s ON s.session_id = t.thread_id
                        JOIN work_projects w ON w.id = t.project_id
                        JOIN thread_organization o ON o.thread_id = t.thread_id
                        WHERE {' AND '.join(where) or '1'}""",
                    params,
                )
            ]
            scope_set = set(scope_ids)
            saved_ids, partial_ids, live_prose = _sync_unindexed_histories(store, db, tables, scope_ids)

            if resident_live_turns:
                for tid, turn in resident_live_turns.items():
                    if tid in scope_set and isinstance(turn, dict):
                        live_prose.setdefault(tid, []).extend(turn_messages(turn, 0))
                        if tid not in saved_ids:
                            saved_ids.add(tid)

            message_hits: dict[str, list[dict[str, Any]]] = {}
            if use_messages and scope_ids:
                raw_hits = store.search_store.search_messages(
                    terms,
                    user_only=my_messages,
                    thread_ids=scope_ids if has_scope_filter else None,
                )
                message_hits = {tid: hits for tid, hits in raw_hits.items() if tid in scope_set}
                for tid, items_list in live_prose.items():
                    for pos, (turn_id, item_id, role, stamp, text, folded) in enumerate(items_list):
                        if my_messages and role != "user":
                            continue
                        if all(term in folded for term in terms):
                            message_hits.setdefault(tid, []).append(
                                {
                                    "turn_seq": 1_000_000 + pos,
                                    "turn_id": turn_id,
                                    "item_id": item_id,
                                    "role": role,
                                    "timestamp": stamp,
                                    "excerpt": text[:320],
                                    "folded_body": _normalize(text),
                                    "bm25_score": 1.0,
                                }
                            )

            row_where = list(where)
            row_params = dict(params)
            skip_row_query = not scope_ids
            cte_sql = ""
            matched_join = ""
            if not skip_row_query and not use_context:
                hit_ids = sorted(message_hits)
                if not hit_ids:
                    skip_row_query = True
                else:
                    row_where = ["t.thread_id IN (SELECT value FROM json_each(:msg_hit_ids))"]
                    row_params = {"msg_hit_ids": json.dumps(hit_ids)}
            elif not skip_row_query and use_context and all(term.isascii() for term in terms):
                term_clauses = []
                for idx, term in enumerate(terms):
                    key_name = f"_ascii_term_{idx}"
                    row_params[key_name] = term
                    term_clauses.append(f"instr(_ctx.folded_ctx, :{key_name}) > 0")
                disjuncts = ["_ctx.has_unicode", "(" + " AND ".join(term_clauses) + ")"]
                if message_hits:
                    row_params["msg_hit_ids"] = json.dumps(sorted(message_hits))
                    disjuncts.insert(0, "_ctx.thread_id IN (SELECT value FROM json_each(:msg_hit_ids))")
                raw_ctx = (
                    "(COALESCE(t.title, '') || ' ' || COALESCE(t.domain, '') || ' ' || "
                    "COALESCE(t.original_request, '') || ' ' || COALESCE(c0.summary, '') || ' ' || "
                    "COALESCE(c0.next_action, '') || ' ' || COALESCE(tl.links_text, ''))"
                )
                cte_sql = f"""WITH _raw AS (
                    SELECT t.thread_id,
                           c0.id AS latest_checkpoint_id,
                           {raw_ctx} AS raw_ctx
                    FROM work_threads t
                    JOIN sessions s ON s.session_id = t.thread_id
                    JOIN work_projects w ON w.id = t.project_id
                    JOIN thread_organization o ON o.thread_id = t.thread_id
                    LEFT JOIN (SELECT thread_id, MAX(id) AS max_id FROM thread_checkpoints GROUP BY thread_id) cm
                        ON cm.thread_id = t.thread_id
                    LEFT JOIN thread_checkpoints c0 ON c0.id = cm.max_id
                    LEFT JOIN (SELECT thread_id, group_concat(label || ' ' || target, ' ') AS links_text FROM thread_links GROUP BY thread_id) tl
                        ON tl.thread_id = t.thread_id
                    WHERE {' AND '.join(where) or '1'}
                ), _ctx AS (
                    SELECT thread_id,
                           latest_checkpoint_id,
                           lower(raw_ctx) AS folded_ctx,
                           (length(CAST(raw_ctx AS BLOB)) != length(raw_ctx)) AS has_unicode
                    FROM _raw
                ), _matched AS (
                    SELECT thread_id, latest_checkpoint_id FROM _ctx
                    WHERE {' OR '.join(disjuncts)}
                ) """
                matched_join = "JOIN _matched m ON m.thread_id = t.thread_id"
                row_where = []

            if skip_row_query:
                decoded_rows = []
            else:
                checkpoint_join = (
                    "LEFT JOIN thread_checkpoints c ON c.id = m.latest_checkpoint_id"
                    if matched_join
                    else "LEFT JOIN thread_checkpoints c ON c.id = (SELECT MAX(id) FROM thread_checkpoints WHERE thread_id = t.thread_id)"
                )
                packed_rows = db.execute(
                    f"""{cte_sql}SELECT COALESCE(json_group_array(json_array(
                               t.thread_id, t.title, t.domain, t.original_request, t.attention,
                               s.client, s.cwd, s.turn_active,
                               o.repository_id, r.name,
                               o.project_id, p.name,
                               c.phase, c.summary, c.next_action, c.next_actor,
                               c.created_at,
                               {app_expr},
                               c.id, c.context_lifecycle_json,
                               c.context_lifecycle_turn_key IS NOT t.turn_key,
                               done.status,
                               MAX(t.created_at, COALESCE(t.turn_started_at, 0), COALESCE(done.completed_at, 0), COALESCE(c.created_at, 0)),
                               (SELECT group_concat(artifact, char(10)) FROM (
                                   SELECT l.label || ' ' || l.target AS artifact
                                   FROM thread_links l WHERE l.thread_id = t.thread_id ORDER BY l.id
                               )),
                               {"bw.thread_id IS NOT NULL, bw.paused, bw.error" if "browser_inbox_wake" in tables else "0, NULL, NULL"}
                           )), '[]')
                        FROM work_threads t
                        {matched_join}
                        JOIN sessions s ON s.session_id = t.thread_id
                        JOIN work_projects w ON w.id = t.project_id
                        JOIN thread_organization o ON o.thread_id = t.thread_id
                        LEFT JOIN work_repositories r ON r.id = o.repository_id
                        LEFT JOIN named_projects p ON p.id = o.project_id
                        {checkpoint_join}
                        LEFT JOIN turn_completions done ON done.id =
                            (SELECT MAX(id) FROM turn_completions WHERE thread_id = t.thread_id)
                        {"LEFT JOIN browser_inbox_wake bw ON bw.thread_id = t.thread_id" if "browser_inbox_wake" in tables else ""}
                        WHERE {' AND '.join(row_where) or '1'}""",
                    row_params,
                ).fetchone()[0]
                decoded_rows = json.loads(packed_rows)
        finally:
            db.rollback()

    _KEYS = (
        "thread_id", "title", "domain", "original_request", "attention",
        "client", "cwd", "turn_active",
        "repository_id", "repository_name",
        "project_id", "project_name",
        "phase", "summary", "next_action", "next_actor",
        "checkpoint_at",
        "app_conversation",
        "lifecycle_id", "context_lifecycle_json",
        "lifecycle_stale",
        "last_turn_status",
        "last_work_at",
        "artifacts",
        "wake_exists", "wake_paused", "wake_error",
    )
    scope_by_id = {vals[0]: dict(zip(_KEYS, vals)) for vals in decoded_rows}

    coverage = {
        "threads": len(scope_ids),
        "saved_histories": len(saved_ids),
        "partial_histories": len(partial_ids),
        "missing_histories": len(scope_ids) - len(saved_ids) - len(partial_ids),
        "scope": "Registered threads and saved message history; external provider transcripts are not searched.",
    }

    candidates = []
    for tid, row in scope_by_id.items():
        context_matched = False
        context_score = 0
        if use_context:
            title_val = row["title"] or ""
            domain_val = row["domain"] or ""
            summary_val = ((row["summary"] or "") + " " + (row["next_action"] or "")).strip()
            request_val = row["original_request"] or ""
            artifacts_val = row["artifacts"] or ""
            folded_fields = _folded_context(title_val, domain_val, summary_val, request_val, artifacts_val)
            if all(any(term in val for val in folded_fields.values()) for term in terms):
                context_matched = True
                for term in terms:
                    context_score += (
                        8 * (term in folded_fields["title"])
                        + 6 * (term in folded_fields["domain"])
                        + 4 * (term in folded_fields["summary"])
                        + 2 * (term in folded_fields["request"])
                        + 1 * (term in folded_fields["artifacts"])
                    )
                context_score += (
                    3 * _relevance_folded(folded_fields["title"], terms, phrase)
                    + _relevance_folded(folded_fields["summary"], terms, phrase)
                    + _relevance_folded(folded_fields["request"], terms, phrase)
                )

        thread_messages = message_hits.get(tid, []) if use_messages else []
        message_matched = bool(thread_messages)
        matched = (
            context_matched
            if source == "context"
            else message_matched
            if source == "messages"
            else (context_matched or message_matched)
        )
        if not matched:
            continue

        msg_score = 0
        ranked_matches = []
        if thread_messages:
            for hit in thread_messages:
                rel = _relevance(hit["folded_body"], terms, phrase)
                bm25_bonus = min(16, max(0, int(round(hit["bm25_score"]))))
                hit_score = max(1, rel + bm25_bonus)
                msg_score = max(msg_score, hit_score)
                ranked_matches.append((hit_score, hit["turn_seq"], hit))

        total_score = max(1, context_score + msg_score)
        last_work_at = _decode_number(row["last_work_at"])
        key = (total_score, last_work_at, tid)
        if after is not None and key >= after:
            continue
        candidates.append((key, tid, row, context_matched, ranked_matches))

    candidates.sort(key=lambda entry: entry[0], reverse=True)
    selected = candidates[: limit + 1]
    page = selected[:limit]

    database = hashlib.sha256(str(store.database_path.resolve()).encode()).hexdigest()[:24]
    items = []
    for (score, last_work_at, _), tid, row, context_matched, ranked_matches in page:
        item: dict[str, Any] = {
            "thread_id": tid,
            "title": row["title"],
            "attention": row["attention"],
            "original_request": _snippet(row["original_request"], terms, 240),
            "client": row["client"],
            "cwd": row["cwd"],
            "turn_active": bool(row["turn_active"]),
            "repository_id": row["repository_id"],
            "repository_name": row["repository_name"],
            "project_id": row["project_id"],
            "project_name": row["project_name"],
            "phase": row["phase"],
            "summary": _snippet(row["summary"], terms, 320),
            "next_action": _snippet(row["next_action"], terms, 160),
            "next_actor": row["next_actor"],
            "checkpoint_at": _decode_number(row["checkpoint_at"]),
            "app_conversation": bool(row["app_conversation"]),
            "context_match": bool(context_matched and source != "messages"),
            "last_turn_status": row["last_turn_status"],
            "last_work_at": last_work_at,
            "score": score,
        }
        if row["context_lifecycle_json"] is not None:
            report = json.loads(row["context_lifecycle_json"])
            item["context_lifecycle"] = {
                "checkpoint_id": row["lifecycle_id"],
                "stale": bool(row["lifecycle_stale"]),
                **{k: report[k] for k in ("recommendation", "reason", "reuse_for", "fresh_for")},
            }
        thread_url = _thread_url(database, tid)
        matches_out = []
        for _, _, match in sorted(ranked_matches, key=lambda entry: entry[:2], reverse=True)[:3]:
            turn_id = match["turn_id"]
            item_id = match.get("item_id") or f"turn-{match['turn_seq']}"
            matches_out.append(
                {
                    "turn_id": turn_id,
                    "item_id": item_id,
                    "role": match["role"],
                    "timestamp": match["timestamp"],
                    "excerpt": _snippet(match["excerpt"], terms, 320, phrase),
                    "url": thread_url + "&" + urlencode({"turn": turn_id, "item": item_id}),
                }
            )
        item["matches"] = matches_out
        item["message_count"] = len(ranked_matches)
        item["wake_paused"] = item["wake_pause_reason"] = None
        if item["app_conversation"] and "browser_inbox_wake" in tables:
            paused = bool(row["wake_exists"] and row["wake_paused"])
            item["wake_paused"] = paused
            item["wake_pause_reason"] = _snippet(row["wake_error"], [], 160) if paused else None
        item["url"] = thread_url
        items.append(item)

    next_cursor = None
    if len(selected) > limit:
        (last_score, last_time, last_id), *_ = page[-1]
        next_cursor = base64.urlsafe_b64encode(
            json.dumps(
                {"v": 1, "query": fingerprint, "score": last_score, "time": last_time, "id": last_id},
                separators=(",", ":"),
            ).encode()
        ).decode()
    return {"items": items, "next_cursor": next_cursor, "coverage": coverage}


def search_browser_threads(store, sessions, query):
    """Search one app request's scope within the overview inventory boundary."""
    from .thread_inventory import listed_threads

    scope = query.get("scope", ["all"])[0]
    if scope not in {"all", "all_projects", "current"}:
        raise CoordinationError("Search scope must be all, all_projects, or current.")
    show = query.get("show", ["active"])[0]
    phase = query.get("phase", [""])[0]
    placement = "archived" if show == "archived" else "later" if show == "later" else "now"
    if scope == "current" and (show in {"attention", "completed"} or phase):
        threads = [
            t
            for t in sessions.list_work_threads(archived=show == "archived")
            if t["attention"] == placement
            and (show != "attention" or t["needs_attention"])
            and (show != "completed" or t["work_phase"] == "finished")
        ]
        if phase:
            threads = [t for t in threads if (t["work_phase"] or "new") == phase]
    else:
        with store.reading(), store._connection() as db:
            threads = listed_threads(store.threads, db, sessions.cwd)
        if scope == "current":
            threads = [t for t in threads if t["attention"] == placement]
    if scope == "current":
        for field in ("repository", "project"):
            selected = query.get(field, [""])[0]
            if selected:
                threads = [
                    t
                    for t in threads
                    if t[field + "_id"] == (None if selected == "__none__" else selected)
                ]
    if scope != "all_projects" and "project_cwd" in query:
        directory = query["project_cwd"][0]
        directory = str(Path(directory).expanduser().resolve()) if directory else ""
        threads = [t for t in threads if t["project_cwd"] == directory]

    live_turns: dict[str, dict] = {}
    histories = getattr(sessions, "histories", None)
    lock = getattr(sessions, "lock", None)
    if isinstance(histories, dict) and lock is not None:
        with lock:
            for tid, hist in histories.items():
                turns = hist.get("turns") if isinstance(hist, dict) else None
                if turns and isinstance(turns[-1], dict) and turns[-1].get("status") == "inProgress":
                    live_turns[tid] = dict(turns[-1], items=list(turns[-1].get("items") or []))

    return search_threads(
        store,
        query.get("q", [""])[0],
        limit=20,
        cursor=query.get("cursor", [None])[0],
        my_messages=query.get("my_messages") == ["true"],
        thread_ids=[t["thread_id"] for t in threads],
        resident_live_turns=live_turns or None,
    )


__all__ = ["messages", "search_browser_threads", "search_threads"]
