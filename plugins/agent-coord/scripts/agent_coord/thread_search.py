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

from .database_writer import sqlite_commit_signature
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


@lru_cache(maxsize=16384)
def _relevance_folded_cached(folded: str, terms_key: tuple[str, ...], phrase: str) -> int:
    span = _match_span(folded, list(terms_key), phrase)
    if span is None:
        return 0
    gap = max(0, span[1] - span[0] - sum(map(len, terms_key)))
    return 1 + max(0, 24 - gap // 8) + 32 * (phrase in folded) + 16 * (folded == phrase)


def _relevance_folded(folded: str, terms: list[str], phrase: str) -> int:
    return _relevance_folded_cached(folded, tuple(terms), phrase)


def _relevance(value: str | None, terms: list[str], phrase: str) -> int:
    return _relevance_folded_cached(_normalize(value), tuple(terms), phrase)


@lru_cache(maxsize=16384)
def _score_context_fields(
    title_val: str,
    domain_val: str,
    summary_val: str,
    request_val: str,
    artifacts_val: str,
    terms_key: tuple[str, ...],
    phrase: str,
) -> tuple[bool, int]:
    folded_fields = _folded_context(title_val, domain_val, summary_val, request_val, artifacts_val)
    if not all(any(term in val for val in folded_fields.values()) for term in terms_key):
        return False, 0
    context_score = 0
    for term in terms_key:
        context_score += (
            8 * (term in folded_fields["title"])
            + 6 * (term in folded_fields["domain"])
            + 4 * (term in folded_fields["summary"])
            + 2 * (term in folded_fields["request"])
            + 1 * (term in folded_fields["artifacts"])
        )
    context_score += (
        3 * _relevance_folded_cached(folded_fields["title"], terms_key, phrase)
        + _relevance_folded_cached(folded_fields["summary"], terms_key, phrase)
        + _relevance_folded_cached(folded_fields["request"], terms_key, phrase)
    )
    return True, context_score


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
    from .desktop_runtime import _OWNED_DATABASES
    owned = store._database_identity in _OWNED_DATABASES
    while True:
        with store.reading(), store._connection() as db:
            cached_tables = getattr(store, "_search_owned_tables", None) if owned else None
            if cached_tables is not None:
                yield db, cached_tables
                return
            if getattr(store, "_search_org_verified", False):
                names, count_mismatch = db.execute(
                    """SELECT
                        (SELECT json_group_array(name) FROM sqlite_master WHERE type='table' AND name IN
                         ('browser_sessions', 'browser_history', 'browser_inbox_wake', 'thread_turns')),
                        (SELECT COUNT(*) FROM work_threads) != (SELECT COUNT(*) FROM thread_organization)"""
                ).fetchone()
                organization_missing = bool(count_mismatch) and bool(
                    db.execute(
                        """SELECT EXISTS (SELECT 1 FROM work_threads t
                           LEFT JOIN thread_organization o ON o.thread_id=t.thread_id
                           WHERE o.thread_id IS NULL)"""
                    ).fetchone()[0]
                )
            else:
                names, organization_missing = db.execute(
                    """SELECT
                        (SELECT json_group_array(name) FROM sqlite_master WHERE type='table' AND name IN
                         ('browser_sessions', 'browser_history', 'browser_inbox_wake', 'thread_turns')),
                        EXISTS (SELECT 1 FROM work_threads t LEFT JOIN thread_organization o ON o.thread_id=t.thread_id
                                WHERE o.thread_id IS NULL)"""
                ).fetchone()
                if not organization_missing:
                    store._search_org_verified = True
            tables = set(json.loads(names))
            if not organization_missing or store.read_only:
                if owned and not organization_missing and len(tables) == 4:
                    store._search_owned_tables = tables
                yield db, tables
                return
        store.threads.organization.backfill()


def _query_context_entries(db, tables: set[str], thread_ids: list[str] | None = None) -> dict[str, tuple]:
    app_expr = (
        "EXISTS (SELECT 1 FROM browser_sessions b WHERE b.thread_id = t.thread_id)"
        if "browser_sessions" in tables
        else "0"
    )
    if thread_ids is not None:
        if not thread_ids:
            return {}
        rows = db.execute(
            f"""WITH _ids(tid) AS (SELECT value FROM json_each(?))
                SELECT t.thread_id, t.title, t.domain, t.original_request,
                       c.summary, c.next_action,
                       (SELECT group_concat(artifact, char(10)) FROM (
                           SELECT l.label || ' ' || l.target AS artifact
                           FROM thread_links l WHERE l.thread_id = t.thread_id ORDER BY l.id
                       )),
                       MAX(t.created_at, COALESCE(t.turn_started_at, 0), COALESCE(done.completed_at, 0), COALESCE(c.created_at, 0)),
                       w.root, o.repository_id, o.project_id, {app_expr}
                FROM work_threads t
                JOIN _ids ids ON ids.tid = t.thread_id
                JOIN sessions s ON s.session_id = t.thread_id
                JOIN work_projects w ON w.id = t.project_id
                JOIN thread_organization o ON o.thread_id = t.thread_id
                LEFT JOIN thread_checkpoints c ON c.id =
                    (SELECT MAX(id) FROM thread_checkpoints WHERE thread_id = t.thread_id)
                LEFT JOIN turn_completions done ON done.id =
                    (SELECT MAX(id) FROM turn_completions WHERE thread_id = t.thread_id)""",
            (json.dumps(sorted(thread_ids)),),
        ).fetchall()
    else:
        rows = db.execute(
            f"""WITH _cp_max AS (SELECT thread_id, MAX(id) AS max_id FROM thread_checkpoints GROUP BY thread_id),
                     _done_max AS (SELECT thread_id, MAX(id) AS max_id FROM turn_completions GROUP BY thread_id),
                     _tl AS (
                         SELECT thread_id, group_concat(artifact, char(10)) AS artifacts
                         FROM (SELECT thread_id, label || ' ' || target AS artifact FROM thread_links ORDER BY thread_id, id)
                         GROUP BY thread_id
                     )
                SELECT t.thread_id, t.title, t.domain, t.original_request,
                       c0.summary, c0.next_action, tl.artifacts,
                       MAX(t.created_at, COALESCE(t.turn_started_at, 0), COALESCE(d0.completed_at, 0), COALESCE(c0.created_at, 0)),
                       w.root, o.repository_id, o.project_id, {app_expr}
                FROM work_threads t
                JOIN sessions s ON s.session_id = t.thread_id
                JOIN work_projects w ON w.id = t.project_id
                JOIN thread_organization o ON o.thread_id = t.thread_id
                LEFT JOIN _cp_max cm ON cm.thread_id = t.thread_id
                LEFT JOIN thread_checkpoints c0 ON c0.id = cm.max_id
                LEFT JOIN _done_max dm ON dm.thread_id = t.thread_id
                LEFT JOIN turn_completions d0 ON d0.id = dm.max_id
                LEFT JOIN _tl tl ON tl.thread_id = t.thread_id"""
        ).fetchall()

    entries: dict[str, tuple] = {}
    for row in rows:
        tid, title, domain, req, summary, next_act, artifacts, last_work, w_root, repo_id, proj_id, is_app = row
        raw_ctx = " ".join(
            part for part in (title, domain, req, summary, next_act, artifacts) if part
        )
        entries[tid] = (
            tid,
            title,
            domain,
            req,
            summary,
            next_act,
            artifacts,
            last_work,
            w_root,
            repo_id,
            proj_id,
            bool(is_app),
            raw_ctx.lower(),
            not raw_ctx.isascii(),
        )
    return entries


def _connection_data_version(db) -> int:
    prev = getattr(db, "_applying_internal_pragma", False)
    try:
        db._applying_internal_pragma = True
    except AttributeError:
        pass
    try:
        return int(db.execute("PRAGMA data_version").fetchone()[0])
    finally:
        try:
            db._applying_internal_pragma = prev
        except AttributeError:
            pass


def _load_context_snapshot(
    store,
    db,
    tables: set[str],
    *,
    owned: bool,
    sig_before: tuple[int, int, int, int] | None,
    rev_before: int,
    unsafe_before: int,
    dirty_seq_before: int,
    pre_build_token: object | None,
) -> dict[str, tuple]:
    writer = store._writer
    if owned:
        with writer.search_cache_lock:
            cache = writer.search_cache
            dirty_map = (
                dict(writer.search_dirty_threads)
                if cache is not None
                and writer.search_dirty_threads is not None
                and cache.get("unsafe_epoch") == writer.search_unsafe_epoch
                else None
            )
            cached_rows = dict(cache["rows"]) if dirty_map is not None else None
        if cached_rows is not None and dirty_map is not None:
            if pre_build_token is not None:
                with writer.search_cache_lock:
                    if writer._search_build_token is pre_build_token:
                        writer._search_build_token = None
                        writer._search_build_dirty = None
                        writer.search_build_cond.notify_all()
            if dirty_map:
                refreshed = _query_context_entries(db, tables, list(dirty_map))
                for tid in dirty_map:
                    if tid in refreshed:
                        cached_rows[tid] = refreshed[tid]
                    else:
                        cached_rows.pop(tid, None)
                with writer.search_cache_lock:
                    if (
                        writer.search_unsafe_epoch == unsafe_before
                        and writer.search_cache is not None
                        and writer.search_dirty_threads is not None
                        and writer.search_cache.get("unsafe_epoch") == unsafe_before
                    ):
                        for tid, seq in dirty_map.items():
                            if seq <= dirty_seq_before and writer.search_dirty_threads.get(tid) == seq:
                                if tid in refreshed:
                                    writer.search_cache["rows"][tid] = refreshed[tid]
                                else:
                                    writer.search_cache["rows"].pop(tid, None)
                                writer.search_dirty_threads.pop(tid, None)
            return cached_rows

        try:
            built = _query_context_entries(db, tables)
        except BaseException:
            if pre_build_token is not None:
                with writer.search_cache_lock:
                    if writer._search_build_token is pre_build_token:
                        writer._search_build_token = None
                        writer._search_build_dirty = None
                        writer.search_build_cond.notify_all()
            raise
        if pre_build_token is not None:
            with writer.search_cache_lock:
                if (
                    writer._search_build_token is pre_build_token
                    and writer.search_unsafe_epoch == unsafe_before
                    and writer._search_build_dirty is not None
                ):
                    writer.search_cache = {
                        "sig": None,
                        "unsafe_epoch": unsafe_before,
                        "rows": dict(built),
                    }
                    writer.search_dirty_threads = dict(writer._search_build_dirty)
                if writer._search_build_token is pre_build_token:
                    writer._search_build_token = None
                    writer._search_build_dirty = None
                    writer.search_build_cond.notify_all()
        return built

    cur_dv = _connection_data_version(db)
    last_dv = getattr(db, "_search_last_dv", None)
    last_rev = getattr(db, "_search_last_rev", None)
    external_dv_jump = last_dv is not None and last_rev == writer.commit_revision and cur_dv != last_dv
    sig_after = sqlite_commit_signature(store.database_path)
    build_token = None
    with writer.search_cache_lock:
        if external_dv_jump:
            writer.search_dirty_threads = None
            writer.search_known_sigs.clear()
        cache = writer.search_cache
        dirty_map = None if writer.search_dirty_threads is None else dict(writer.search_dirty_threads)
        pinned_clean = sig_before == sig_after and writer.commit_revision == rev_before and not external_dv_jump
        sig_trusted = (
            not external_dv_jump
            and cache is not None
            and dirty_map is not None
            and cache.get("sig") is not None
            and (cache["sig"] == sig_after or sig_after in writer.search_known_sigs)
        )
        cached_rows = dict(cache["rows"]) if sig_trusted else None
        if cached_rows is None and pinned_clean and writer._search_build_token is None:
            build_token = object()
            writer._search_build_token = build_token
            writer._search_build_sig = sig_after
            writer._search_build_dirty = {}
            writer.search_known_sigs.append(sig_after)

    def remember_conn_state() -> None:
        try:
            db._search_last_dv = cur_dv
            db._search_last_rev = writer.commit_revision
        except AttributeError:
            pass

    if cached_rows is not None and dirty_map is not None:
        if dirty_map:
            refreshed = _query_context_entries(db, tables, list(dirty_map))
            for tid in dirty_map:
                if tid in refreshed:
                    cached_rows[tid] = refreshed[tid]
                else:
                    cached_rows.pop(tid, None)
            if pinned_clean:
                with writer.search_cache_lock:
                    if (
                        writer.commit_revision == rev_before
                        and writer.search_cache is not None
                        and writer.search_cache.get("sig") == sig_after
                        and writer.search_dirty_threads is not None
                        and sqlite_commit_signature(store.database_path) == sig_after
                    ):
                        writer.search_cache["rows"] = dict(cached_rows)
                        for tid, seq in dirty_map.items():
                            if writer.search_dirty_threads.get(tid) == seq:
                                writer.search_dirty_threads.pop(tid, None)
        remember_conn_state()
        return cached_rows

    try:
        built = _query_context_entries(db, tables)
    except BaseException:
        if build_token is not None:
            with writer.search_cache_lock:
                if writer._search_build_token is build_token:
                    writer._search_build_token = None
                    writer._search_build_dirty = None
                    writer.search_build_cond.notify_all()
        raise

    if build_token is not None:
        with writer.search_cache_lock:
            if (
                writer._search_build_token is build_token
                and writer._search_build_sig is not None
                and writer._search_build_dirty is not None
                and sqlite_commit_signature(store.database_path) == writer._search_build_sig
            ):
                writer.search_cache = {
                    "sig": writer._search_build_sig,
                    "unsafe_epoch": writer.search_unsafe_epoch,
                    "rows": dict(built),
                }
                writer.search_dirty_threads = dict(writer._search_build_dirty)
            if writer._search_build_token is build_token:
                writer._search_build_token = None
                writer._search_build_dirty = None
                writer.search_build_cond.notify_all()
    remember_conn_state()
    return built


def _sync_unindexed_histories(store, db, tables: set[str], scope_ids: list[str]) -> tuple[set[str], set[str], dict[str, list[tuple]]]:
    """Ensure finalized turns in scope are indexed in search.sqlite3; return coverage & live in-progress hits."""
    search = store.search_store
    tstore = store.transcripts
    if search._pending_tasks > 0 and tstore._saved_threads is None:
        search.flush(timeout=0.02)
    saved_ids: set[str] = set()
    partial_ids: set[str] = set()
    live_prose: dict[str, list[tuple]] = {}
    if not scope_ids:
        return saved_ids, partial_ids, live_prose

    all_saved, all_partial = tstore.coverage_sets(db, tables, search)
    scope_set = set(scope_ids)
    saved_ids = all_saved & scope_set
    partial_ids = all_partial & scope_set

    # Legacy fallback for unmigrated browser_history rows in standalone tests.
    from .desktop_runtime import _OWNED_DATABASES
    owned = store._database_identity in _OWNED_DATABASES
    if "browser_history" in tables and not (owned and getattr(store, "_search_legacy_history_empty", False)):
        has_legacy = db.execute("SELECT 1 FROM browser_history LIMIT 1").fetchone() is not None
        if not has_legacy and owned:
            store._search_legacy_history_empty = True
    else:
        has_legacy = False
    if has_legacy:
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

    has_scope_filter = bool(
        thread_ids is not None
        or workspace is not None
        or repository_id is not UNSET
        or project_id is not UNSET
        or app_only
    )

    with store._writer.measure_request("search.query"), _readonly_state_connection(store) as (db, tables):
        app_expr = (
            "EXISTS (SELECT 1 FROM browser_sessions b WHERE b.thread_id = t.thread_id)"
            if "browser_sessions" in tables
            else "0"
        )
        from .desktop_runtime import _OWNED_DATABASES
        owned = store._database_identity in _OWNED_DATABASES
        pre_build_token = None
        with store._writer.search_cache_lock:
            if owned and store._writer.search_cache is None and store._writer._search_build_token is not None:
                store._writer.search_build_cond.wait(timeout=0.15)
            rev_before = store._writer.commit_revision
            unsafe_before = store._writer.search_unsafe_epoch
            dirty_seq_before = store._writer.search_dirty_seq
            if (
                owned
                and (
                    store._writer.search_cache is None
                    or store._writer.search_dirty_threads is None
                    or store._writer.search_cache.get("unsafe_epoch") != unsafe_before
                )
                and store._writer._search_build_token is None
            ):
                pre_build_token = object()
                store._writer._search_build_token = pre_build_token
                store._writer._search_build_epoch = unsafe_before
                store._writer._search_build_dirty = {}
        sig_before = None if owned else sqlite_commit_signature(store.database_path)
        db.execute("BEGIN")
        try:
            db.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
            all_entries = _load_context_snapshot(
                store,
                db,
                tables,
                owned=owned,
                sig_before=sig_before,
                rev_before=rev_before,
                unsafe_before=unsafe_before,
                dirty_seq_before=dirty_seq_before,
                pre_build_token=pre_build_token,
            )
            if not has_scope_filter:
                in_scope_entries = list(all_entries.values())
                scope_ids = list(all_entries)
                scope_set = set(all_entries)
            else:
                allowed_tids = set(thread_ids) if thread_ids is not None else None
                in_scope_entries = [
                    entry
                    for entry in all_entries.values()
                    if (allowed_tids is None or entry[0] in allowed_tids)
                    and (workspace is None or entry[8] == workspace)
                    and (repository_id is UNSET or entry[9] == repository_id)
                    and (project_id is UNSET or entry[10] == project_id)
                    and (not app_only or entry[11])
                ]
                scope_ids = [entry[0] for entry in in_scope_entries]
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

            terms_key = tuple(terms)
            ascii_terms = all(term.isascii() for term in terms_key)
            if not scope_ids:
                scoring_rows = []
            elif not use_context:
                scoring_rows = [all_entries[tid] for tid in message_hits if tid in scope_set]
            elif ascii_terms:
                scoring_rows = [
                    entry
                    for entry in in_scope_entries
                    if entry[0] in message_hits or entry[13] or all(term in entry[12] for term in terms_key)
                ]
            else:
                scoring_rows = in_scope_entries

            candidates = []
            for srow in scoring_rows:
                tid = srow[0]
                context_matched = False
                context_score = 0
                if use_context:
                    title_val = srow[1] or ""
                    domain_val = srow[2] or ""
                    summary_val = ((srow[4] or "") + " " + (srow[5] or "")).strip()
                    request_val = srow[3] or ""
                    artifacts_val = srow[6] or ""
                    context_matched, context_score = _score_context_fields(
                        title_val, domain_val, summary_val, request_val, artifacts_val, terms_key, phrase
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
                        rel = _relevance_folded_cached(hit["folded_body"], terms_key, phrase)
                        bm25_bonus = min(16, max(0, int(round(hit["bm25_score"]))))
                        hit_score = max(1, rel + bm25_bonus)
                        msg_score = max(msg_score, hit_score)
                        ranked_matches.append((hit_score, hit["turn_seq"], hit))

                total_score = max(1, context_score + msg_score)
                last_work_at = _decode_number(srow[7])
                key = (total_score, last_work_at, tid)
                if after is not None and key >= after:
                    continue
                candidates.append((key, tid, context_matched, ranked_matches))

            candidates.sort(key=lambda entry: entry[0], reverse=True)
            selected = candidates[: limit + 1]
            page = selected[:limit]

            if page:
                page_ids_json = json.dumps([tid for _, tid, _, _ in page])
                packed_rows = db.execute(
                    f"""WITH _page_ids(tid) AS (SELECT value FROM json_each(?))
                        SELECT COALESCE(json_group_array(json_array(
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
                               NULL,
                               {"bw.thread_id IS NOT NULL, bw.paused, bw.error" if "browser_inbox_wake" in tables else "0, NULL, NULL"}
                           )), '[]')
                        FROM work_threads t
                        JOIN _page_ids pids ON pids.tid = t.thread_id
                        JOIN sessions s ON s.session_id = t.thread_id
                        JOIN work_projects w ON w.id = t.project_id
                        JOIN thread_organization o ON o.thread_id = t.thread_id
                        LEFT JOIN work_repositories r ON r.id = o.repository_id
                        LEFT JOIN named_projects p ON p.id = o.project_id
                        LEFT JOIN thread_checkpoints c ON c.id =
                            (SELECT MAX(id) FROM thread_checkpoints WHERE thread_id = t.thread_id)
                        LEFT JOIN turn_completions done ON done.id =
                            (SELECT MAX(id) FROM turn_completions WHERE thread_id = t.thread_id)
                        {"LEFT JOIN browser_inbox_wake bw ON bw.thread_id = t.thread_id" if "browser_inbox_wake" in tables else ""}""",
                    (page_ids_json,),
                ).fetchone()[0]
                decoded_rows = json.loads(packed_rows)
            else:
                decoded_rows = []
        finally:
            if pre_build_token is not None and store._writer._search_build_token is pre_build_token:
                with store._writer.search_cache_lock:
                    if store._writer._search_build_token is pre_build_token:
                        store._writer._search_build_token = None
                        store._writer._search_build_dirty = None
                        store._writer.search_build_cond.notify_all()
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

    database = hashlib.sha256(str(store.database_path.resolve()).encode()).hexdigest()[:24]
    items = []
    for (score, last_work_at, _), tid, context_matched, ranked_matches in page:
        row = scope_by_id[tid]
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
