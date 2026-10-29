"""Bounded discovery of durable conversations, independent of UI placement.

Search saved routing context and indexed conversation prose. Pages are live
keyset queries: repeat the search when conversations change during discovery.

Matching and ranking run inside SQLite over casefolded text. A request uses a
small, fixed number of statements and aggregates results into single rows:
under load every Python callback and returned row reacquires the GIL behind
other request threads. Python measures keyword proximity only for candidates
whose rank SQLite cannot settle.
"""
from __future__ import annotations

import base64
import hashlib
import heapq
import json
import math
import sqlite3
from pathlib import Path
from urllib.parse import quote, urlencode

from .organization import UNSET
from .store import CoordinationError
from .threads import project_root
from .conversation_search import message_condition, sync_messages

# Separators of str.split(); none is above U+3000.
_SPACE = "".join(chr(code) for code in range(0x3001) if chr(code).isspace())
_SCALE = 4503599627370496  # 2**52
_MEASURE_BATCH = 24
_CONTEXT_WEIGHTS = {"title": 3, "summary": 1, "request": 1}

# Casefolded, whitespace-collapsed routing context, as relevance compares it.
# Triggers delete a thread's row when its source changes, whichever runtime
# writes; a search recomputes missing rows from its own read snapshot.
_CONTEXT_SCHEMA = {
    "conversation_search_context": """CREATE TABLE IF NOT EXISTS conversation_search_context (
        thread_id TEXT PRIMARY KEY, title TEXT NOT NULL, request TEXT NOT NULL,
        summary TEXT NOT NULL, artifacts TEXT NOT NULL)""",
    "conversation_context_thread": """CREATE TRIGGER IF NOT EXISTS conversation_context_thread
        AFTER UPDATE OF thread_id, title, original_request ON work_threads BEGIN
        DELETE FROM conversation_search_context WHERE thread_id IN (OLD.thread_id, NEW.thread_id); END""",
    **{f"conversation_context_{table}_{event}": f"""CREATE TRIGGER IF NOT EXISTS conversation_context_{table}_{event}
        AFTER {event.upper()}{columns if event == "update" else ""} ON thread_{table}s BEGIN
        DELETE FROM conversation_search_context WHERE thread_id IN ({rows}); END"""
       for table, columns in (("checkpoint", " OF id, thread_id, summary, next_action"),
                              ("link", " OF id, thread_id, label, target"))
       for event, rows in (("insert", "NEW.thread_id"), ("update", "OLD.thread_id, NEW.thread_id"),
                           ("delete", "OLD.thread_id"))},
}
# Context sources of thread t: title, original request, latest checkpoint c, artifacts.
_SUMMARY = "COALESCE(c.summary, '') || ' ' || COALESCE(c.next_action, '')"
_ARTIFACTS = """(SELECT group_concat(artifact, char(10)) FROM (SELECT l.label || ' ' || l.target AS artifact
        FROM thread_links l WHERE l.thread_id = t.thread_id ORDER BY l.id))"""
_LATEST_CHECKPOINT = """LEFT JOIN thread_checkpoints c ON c.id =
        (SELECT MAX(id) FROM thread_checkpoints WHERE thread_id = t.thread_id)"""
_MISSING_CONTEXT = f"""SELECT json_group_array(json_array(t.thread_id, t.title, t.original_request,
        {_SUMMARY}, {_ARTIFACTS}))
    FROM work_threads t {_LATEST_CHECKPOINT}
    WHERE NOT EXISTS (SELECT 1 FROM conversation_search_context x WHERE x.thread_id = t.thread_id)"""
# Store a recomputed row only while its sources still match the snapshot it
# was computed from; a later change has already invalidated it. CROSS JOIN
# keeps the JSON rows outermost, so each is parsed once.
_SAVE_CONTEXT = f"""INSERT OR REPLACE INTO conversation_search_context
    SELECT t.thread_id, json_extract(r.value, '$[4]'), json_extract(r.value, '$[5]'),
           json_extract(r.value, '$[6]'), json_extract(r.value, '$[7]')
    FROM json_each(:rows) r CROSS JOIN work_threads t ON t.thread_id = r.key {_LATEST_CHECKPOINT}
    WHERE t.title IS json_extract(r.value, '$[0]') AND t.original_request IS json_extract(r.value, '$[1]')
      AND {_SUMMARY} IS json_extract(r.value, '$[2]') AND {_ARTIFACTS} IS json_extract(r.value, '$[3]')"""
# Rows recomputed in this snapshot, keyed by thread. LIMIT keeps SQLite from
# flattening it into the join, which would rescan the JSON for every thread.
_OVERLAY = """SELECT key AS thread_id, json_extract(value, '$[4]') AS title,
        json_extract(value, '$[5]') AS request, json_extract(value, '$[6]') AS summary,
        json_extract(value, '$[7]') AS artifacts FROM json_each(:overlay) LIMIT -1"""


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


def _match_span(folded, terms, phrase):
    """Find the phrase or tightest span containing every literal keyword."""
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


def _relevance(value, terms, phrase):
    folded = _normalize(value)
    span = _match_span(folded, terms, phrase)
    if span is None:
        return 0
    gap = max(0, span[1] - span[0] - sum(map(len, terms)))
    return 1 + max(0, 24 - gap // 8) + 32 * (phrase in folded) + 16 * (folded == phrase)


def _normalize(value):
    """Casefolded text with collapsed whitespace, as relevance compares it."""
    return " ".join((value or "").casefold().split())


def _snippet(value, terms, limit, phrase=None):
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
    return ("…" if start else "") + value[start:start + limit] + ("…" if len(value) > start + limit else "")


def _number(column):
    """Encode a SQLite number for exact transport through JSON.

    SQLite before 3.53 renders REAL values with 15 significant digits, so
    timestamps would not round-trip. The integer part and two 52-bit binary
    fractions are exact for every value from 2**-52 to 2**63.
    """
    whole = f"CAST({column} AS INTEGER)"
    fraction = f"(({column} - {whole}) * 4503599627370496.0)"
    return (f"CASE typeof({column}) WHEN 'real' THEN json_array({whole}, CAST({fraction} AS INTEGER), "
            f"CAST(({fraction} - CAST({fraction} AS INTEGER)) * 4503599627370496.0 AS INTEGER), "
            f"{column}) ELSE {column} END")


def _decode_number(value):
    if not isinstance(value, list):
        return value
    whole, fraction, remainder, rendered = value
    exact = whole + (fraction + remainder / _SCALE) / _SCALE
    # Outside the exact range, keep SQLite's own rendering.
    return exact if math.isclose(exact, rendered, rel_tol=1e-9) else rendered


def _relevance_sql(column, count, single, closeness):
    """Relevance of a casefolded, collapsed column; -1 when only proximity is unknown."""
    every = " AND ".join(f"instr({column}, :q{i}) > 0" for i in range(count))
    found = f"1 + {closeness} + 32 + 16 * ({column} = :phrase)"
    if single:
        return f"CASE WHEN {every} THEN {found} ELSE 0 END"
    return f"CASE WHEN instr({column}, :phrase) > 0 THEN {found} WHEN {every} THEN -1 ELSE 0 END"


def _message_relevance_sql(single, closeness):
    """Relevance of a message containing every keyword; -1 when SQLite cannot settle it.

    Messages keep casefolded text with its original whitespace. A literal
    phrase proves the collapsed phrase; other spacing needs Python.
    """
    found = f"1 + {closeness} + 32 + 16 * (trim(m.folded, :space) = :phrase)"
    if single:
        return found
    return f"CASE WHEN instr(m.folded, :phrase) > 0 THEN {found} ELSE -1 END"


class _Candidate:
    __slots__ = ("thread_id", "time", "fixed", "unknown", "certain", "uncertain",
                 "context_match", "low", "high", "score", "measured")

    def key(self, score):
        return score, self.time, self.thread_id


def _select(candidates, after, count, measure):
    """Return the best `count` candidates below `after` in score, time, ID order.

    Candidates are visited by their score ceiling; an unsettled score is
    measured only while it could still change the selection.
    """
    ordered = sorted(candidates, key=lambda c: c.key(c.high), reverse=True)
    best = []
    for index, candidate in enumerate(ordered):
        if len(best) == count and best[0][0] >= candidate.key(candidate.high):
            break
        if after is not None and candidate.key(candidate.low) >= after:
            continue
        if candidate.score is None:
            measure([c for c in ordered[index:index + _MEASURE_BATCH]
                     if c.score is None and (after is None or c.key(c.low) < after)])
        key = candidate.key(candidate.score)
        if after is not None and key >= after:
            continue
        if len(best) < count:
            heapq.heappush(best, (key, index))
        elif key > best[0][0]:
            heapq.heapreplace(best, (key, index))
    return [ordered[index] for _, index in sorted(best, reverse=True)]


def _thread_url(database, thread_id):
    # The NavigationStore.link(thread=...) route, without reloading the thread.
    return "agentcoord://thread/" + quote(thread_id, safe="") + "?" + urlencode({"database": database})


def _save_context(db, overlay):
    """Persist recomputed context rows when the writer is available."""
    if not overlay:
        return
    timeout = db.execute("PRAGMA busy_timeout").fetchone()[0]
    db.execute("PRAGMA busy_timeout = 250")
    try:
        db.execute(_SAVE_CONTEXT, {"rows": overlay})
        db.execute("DELETE FROM conversation_search_context WHERE thread_id NOT IN (SELECT thread_id FROM work_threads)")
        db.commit()
    except sqlite3.OperationalError as error:
        db.rollback()
        # A busy writer only defers this cache; the next search recomputes it.
        if "locked" not in str(error) and "busy" not in str(error):
            raise
    finally:
        db.execute(f"PRAGMA busy_timeout = {int(timeout)}")


def search_threads(store, query, *, limit=10, cursor=None, cwd=None,
                   repository_id=UNSET, project_id=UNSET, app_only=False,
                   source="all", my_messages=False, thread_ids=None):
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
    filters = {"ranking": 2, "terms": terms, "phrase": phrase, "cwd": workspace, "app_only": bool(app_only),
               "source": source, "my_messages": bool(my_messages),
               "thread_ids": sorted(thread_ids) if thread_ids is not None else None}
    filters.update({key: value for key, value in (("repository", repository_id), ("project", project_id))
                    if value is not UNSET})
    fingerprint = hashlib.sha256(json.dumps(filters, sort_keys=True).encode()).hexdigest()
    after = _cursor(cursor, fingerprint) if cursor is not None else None
    after = (after["score"], after["time"], after["id"]) if after else None
    store.threads.organization.backfill()
    # A literal phrase spans its keywords with a fixed gap; one keyword is its own phrase.
    closeness = max(0, 24 - max(0, len(phrase) - sum(map(len, terms))) // 8)
    single = " " not in phrase
    message_ceiling = 1 + closeness + 32 + 16
    params = {f"q{i}": term for i, term in enumerate(terms)}
    params.update(phrase=phrase, space=_SPACE)
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
    use_context, use_messages = source != "messages", source != "context"
    message_where = message_condition(terms, "user" if my_messages else None)
    message_relevance = _message_relevance_sql(single, closeness)
    with store._connection() as db:
        sync_messages(db)
        db.commit()
        tables = set(json.loads(db.execute(f"""SELECT json_group_array(name) FROM sqlite_master
            WHERE name IN ('browser_sessions', 'browser_inbox_wake', {", ".join("?" * len(_CONTEXT_SCHEMA))})""",
            tuple(_CONTEXT_SCHEMA)).fetchone()[0]))
        for name, statement in _CONTEXT_SCHEMA.items():
            if name not in tables:
                db.execute(statement)
        app = "EXISTS (SELECT 1 FROM browser_sessions b WHERE b.thread_id = t.thread_id)" if "browser_sessions" in tables else "0"
        if app_only:
            where.append(app)
        overlay = {}
        # One read snapshot keeps candidates, measurements and details consistent.
        db.execute("BEGIN")
        try:
            if use_context:
                # Sources first, then the casefolded text that the search compares.
                overlay = {row[0]: [*row[1:], *map(_normalize, row[1:])]
                           for row in json.loads(db.execute(_MISSING_CONTEXT).fetchone()[0])}
            params["overlay"] = json.dumps(overlay)
            fields = {name: f"CASE WHEN y.thread_id IS NULL THEN IFNULL(x.{name}, '') ELSE y.{name} END"
                      for name in ("title", "summary", "request", "artifacts")}
            # Each keyword must match some routing context. Title matches rank above
            # checkpoint, original request and artifact matches; recent work breaks ties.
            if use_context:
                weights = " + ".join(f"(instr({fields[name]}, :q{i}) > 0) * {weight}" for i in range(len(terms))
                                     for name, weight in (("title", 8), ("summary", 4), ("request", 2), ("artifacts", 1)))
                context_match = " AND ".join("(" + " OR ".join(f"instr({fields[name]}, :q{i}) > 0" for name in fields) + ")"
                                             for i in range(len(terms)))
                relevance = {name: _relevance_sql(fields[name], len(terms), single, closeness) for name in _CONTEXT_WEIGHTS}
            else:
                weights = context_match = "0"
                relevance = dict.fromkeys(_CONTEXT_WEIGHTS, "0")
            evidence = ("SELECT NULL AS thread_id, NULL AS certain, 0 AS uncertain WHERE 0" if not use_messages else
                        f"""SELECT thread_id, MAX(NULLIF(relevance, -1)) AS certain, MAX(relevance = -1) AS uncertain
                            FROM (SELECT m.thread_id, {message_relevance} AS relevance FROM conversation_search_messages m
                                  WHERE m.thread_id IN (SELECT thread_id FROM scope) AND {message_where})
                            GROUP BY thread_id""")
            match = {"context": "context_match", "messages": "message_match"}.get(source, "(context_match OR message_match)")
            # Score bounds: a field or message whose proximity is unknown adds 1–25,
            # or up to the phrase ceiling for messages, until Python measures it.
            fixed = ("weights + 3 * MAX(title, 0) + MAX(summary, 0) + MAX(request, 0)" if use_context else "0")
            spread = "3 * (title = -1) + (summary = -1) + (request = -1)" if use_context else "0"
            low, high = (("CASE WHEN uncertain THEN MAX(certain, 1) ELSE certain END",
                          f"CASE WHEN uncertain THEN MAX(certain, {message_ceiling}) ELSE certain END")
                         if use_messages else ("0", "0"))
            below = "(%s, last_work_at, thread_id) < (:after_score, :after_time, :after_id)"
            if after:
                params.update(after_score=after[0], after_time=after[1], after_id=after[2])
            params["floor"] = limit
            candidates, coverage = db.execute(f"""WITH scope AS (
                    SELECT t.thread_id, t.created_at, t.turn_started_at FROM work_threads t
                    JOIN sessions s ON s.session_id = t.thread_id JOIN work_projects w ON w.id = t.project_id
                    JOIN thread_organization o ON o.thread_id = t.thread_id WHERE {' AND '.join(where) or '1'}),
                evidence AS ({evidence}),
                bounded AS MATERIALIZED (SELECT *, {fixed} + {spread} + {low} AS low,
                        {fixed} + 25 * ({spread}) + {high} AS high
                    FROM (SELECT s.thread_id, {weights} AS weights, {relevance['title']} AS title,
                                 {relevance['summary']} AS summary, {relevance['request']} AS request,
                                 {context_match} AS context_match, IFNULL(e.certain, 0) AS certain,
                                 IFNULL(e.uncertain, 0) AS uncertain, e.thread_id IS NOT NULL AS message_match,
                                 MAX(s.created_at, COALESCE(s.turn_started_at, 0), COALESCE(done.completed_at, 0),
                                     COALESCE(c.created_at, 0)) AS last_work_at
                          FROM scope s LEFT JOIN conversation_search_context x ON x.thread_id = s.thread_id
                          LEFT JOIN ({_OVERLAY}) y ON y.thread_id = s.thread_id
                          LEFT JOIN evidence e ON e.thread_id = s.thread_id
                          LEFT JOIN thread_checkpoints c ON c.id =
                              (SELECT MAX(id) FROM thread_checkpoints WHERE thread_id = s.thread_id)
                          LEFT JOIN turn_completions done ON done.id =
                              (SELECT MAX(id) FROM turn_completions WHERE thread_id = s.thread_id))
                    WHERE {match}),
                -- A key below the cursor that `limit + 1` candidates certainly reach.
                -- A candidate whose ceiling is under it cannot be on this page.
                floor AS (SELECT low, last_work_at, thread_id FROM bounded
                    {f"WHERE {below % 'low'} AND {below % 'high'}" if after else ""}
                    ORDER BY low DESC, last_work_at DESC, thread_id DESC LIMIT 1 OFFSET :floor)
                SELECT (SELECT json_group_array(json_array(thread_id, weights, title, summary, request,
                            context_match, certain, uncertain, low, high, {_number('last_work_at')}))
                        FROM bounded WHERE {below % 'low' if after else '1'} AND (NOT EXISTS (SELECT 1 FROM floor)
                            OR (high, last_work_at, thread_id) >= (SELECT low, last_work_at, thread_id FROM floor))),
                       -- Coverage describes the searched inventory, before the keyword filter.
                       (SELECT json_array(COUNT(*), SUM(h.status = 'saved'), SUM(h.status = 'partial'))
                        FROM scope s LEFT JOIN conversation_search_histories h ON h.thread_id = s.thread_id)""",
                params).fetchone()
            threads, saved, partial = json.loads(coverage)
            coverage = {"threads": threads, "saved_histories": saved or 0, "partial_histories": partial or 0}
            coverage["missing_histories"] = coverage["threads"] - coverage["saved_histories"] - coverage["partial_histories"]
            coverage["scope"] = "Registered threads and saved message history; external provider transcripts are not searched."
            pool = []
            for (thread_id, weight, title, summary, request, context, certain, uncertain,
                 low, high, time) in json.loads(candidates):
                candidate = _Candidate()
                candidate.thread_id, candidate.time = thread_id, _decode_number(time)
                candidate.context_match, candidate.measured = bool(context), None
                ranks = {"title": title, "summary": summary, "request": request}
                candidate.fixed = (weight + sum(_CONTEXT_WEIGHTS[name] * max(rank, 0) for name, rank in ranks.items())
                                   if use_context else 0)
                candidate.unknown = tuple(name for name, rank in ranks.items() if rank == -1) if use_context else ()
                candidate.certain, candidate.uncertain = certain, bool(uncertain) and use_messages
                candidate.low, candidate.high = low, high
                candidate.score = low if low == high else None
                pool.append(candidate)

            def measured_messages(rows):
                found = {}
                for thread_id, position, turn_id, item_id, role, timestamp, text in json.loads(rows):
                    found.setdefault(thread_id, []).append((_relevance(text, terms, phrase), position, {
                        "turn_id": turn_id, "item_id": item_id, "role": role,
                        "timestamp": _decode_number(timestamp), "text": text}))
                return found

            uncertain_messages = f"""SELECT json_group_array(json_array(m.thread_id, m.position, m.turn_id, m.item_id,
                    m.role, {_number('m.timestamp')}, m.text)) FROM conversation_search_messages m
                WHERE m.thread_id IN (SELECT value FROM json_each(:measure)) AND {message_where}
                  AND instr(m.folded, :phrase) = 0"""

            def measure(batch):
                """Settle scores whose keyword proximity only Python can measure."""
                stored = [c.thread_id for c in batch if c.unknown and c.thread_id not in overlay]
                texts, rows = db.execute(f"""SELECT (SELECT json_group_array(json_array(thread_id, title, request, summary))
                        FROM conversation_search_context WHERE thread_id IN (SELECT value FROM json_each(:context))),
                    ({uncertain_messages})""", {**params, "context": json.dumps(stored),
                    "measure": json.dumps([c.thread_id for c in batch if c.uncertain])}).fetchone()
                context = {thread_id: dict(zip(("title", "request", "summary"), values))
                           for thread_id, *values in json.loads(texts)}
                context.update({thread_id: dict(zip(("title", "request", "summary"), values[4:]))
                                for thread_id, values in overlay.items()})
                messages = measured_messages(rows)
                for candidate in batch:
                    score = candidate.fixed + sum(_CONTEXT_WEIGHTS[name] * _relevance(context[candidate.thread_id][name], terms, phrase)
                                                  for name in candidate.unknown)
                    candidate.measured = messages.get(candidate.thread_id, [])
                    if use_messages:
                        score += max([candidate.certain, *(found[0] for found in candidate.measured)])
                    candidate.score = score

            selected = _select(pool, after, limit + 1, measure)
            page = selected[:limit]
            details, counts, ranked, unmeasured = db.execute(f"""SELECT
                (SELECT json_group_array(json_array(t.thread_id, t.title, t.attention, t.original_request, s.client,
                        s.cwd, s.turn_active, o.repository_id, r.name, o.project_id, p.name, c.phase, c.summary,
                        c.next_action, c.next_actor, {_number('c.created_at')}, {app}, c.id, c.context_lifecycle_json,
                        c.context_lifecycle_turn_key IS NOT t.turn_key, done.status,
                        {"bw.thread_id IS NOT NULL, bw.paused, bw.error" if "browser_inbox_wake" in tables else "0, NULL, NULL"}))
                    FROM work_threads t JOIN sessions s ON s.session_id = t.thread_id
                    JOIN work_projects w ON w.id = t.project_id
                    JOIN thread_organization o ON o.thread_id = t.thread_id
                    LEFT JOIN work_repositories r ON r.id = o.repository_id
                    LEFT JOIN named_projects p ON p.id = o.project_id
                    LEFT JOIN thread_checkpoints c ON c.id =
                        (SELECT MAX(id) FROM thread_checkpoints WHERE thread_id = t.thread_id)
                    LEFT JOIN turn_completions done ON done.id =
                        (SELECT MAX(id) FROM turn_completions WHERE thread_id = t.thread_id)
                    {"LEFT JOIN browser_inbox_wake bw ON bw.thread_id = t.thread_id" if "browser_inbox_wake" in tables else ""}
                    WHERE t.thread_id IN (SELECT value FROM json_each(:page))),
                (SELECT json_group_array(json_array(thread_id, total)) FROM (SELECT m.thread_id, COUNT(*) AS total
                    FROM conversation_search_messages m WHERE :messages AND m.thread_id IN (SELECT value FROM json_each(:page))
                    AND {message_where} GROUP BY m.thread_id)),
                (SELECT json_group_array(json_array(thread_id, relevance, position, turn_id, item_id, role,
                        {_number('timestamp')}, text)) FROM (
                    SELECT *, ROW_NUMBER() OVER (PARTITION BY thread_id ORDER BY relevance DESC, position DESC) AS place
                    FROM (SELECT m.thread_id, m.position, m.turn_id, m.item_id, m.role, m.timestamp, m.text,
                                 {message_relevance} AS relevance FROM conversation_search_messages m
                          WHERE :messages AND m.thread_id IN (SELECT value FROM json_each(:page)) AND {message_where}))
                    WHERE relevance != -1 AND place <= 3),
                ({uncertain_messages})""", {**params, "messages": use_messages,
                    "page": json.dumps([c.thread_id for c in page]),
                    "measure": json.dumps([c.thread_id for c in page if c.uncertain and c.measured is None])}).fetchone()
        finally:
            db.commit()
        _save_context(db, params["overlay"] if overlay else None)
    totals = dict(json.loads(counts))
    excerpts = {}
    for thread_id, relevance, position, turn_id, item_id, role, timestamp, text in json.loads(ranked):
        excerpts.setdefault(thread_id, []).append((relevance, position, {
            "turn_id": turn_id, "item_id": item_id, "role": role, "timestamp": _decode_number(timestamp), "text": text}))
    for thread_id, found in measured_messages(unmeasured).items():
        excerpts.setdefault(thread_id, []).extend(found)
    for candidate in page:
        excerpts.setdefault(candidate.thread_id, []).extend(candidate.measured or ())
    rows = {row[0]: row for row in json.loads(details)}
    database = hashlib.sha256(str(store.database_path.resolve()).encode()).hexdigest()[:24]
    items = []
    for candidate in page:
        (thread_id, title, attention, original_request, client, directory, turn_active, repository, repository_name,
         project, project_name, phase, summary, next_action, next_actor, checkpoint_at, app_conversation,
         lifecycle_id, raw_lifecycle, lifecycle_stale, last_turn_status, wake, paused, error) = rows[candidate.thread_id]
        item = {"thread_id": thread_id, "title": title, "attention": attention,
                "original_request": _snippet(original_request, terms, 240), "client": client, "cwd": directory,
                "turn_active": bool(turn_active), "repository_id": repository, "repository_name": repository_name,
                "project_id": project, "project_name": project_name, "phase": phase,
                "summary": _snippet(summary, terms, 320), "next_action": _snippet(next_action, terms, 160),
                "next_actor": next_actor, "checkpoint_at": _decode_number(checkpoint_at),
                "app_conversation": bool(app_conversation),
                "context_match": candidate.context_match and source != "messages",
                "last_turn_status": last_turn_status, "last_work_at": candidate.time, "score": candidate.score}
        if raw_lifecycle is not None:
            report = json.loads(raw_lifecycle)
            item["context_lifecycle"] = {"checkpoint_id": lifecycle_id, "stale": bool(lifecycle_stale),
                **{key: report[key] for key in ("recommendation", "reason", "reuse_for", "fresh_for")}}
        item["url"] = _thread_url(database, thread_id)
        item["matches"] = []
        for _, _, match in sorted(excerpts.get(thread_id, ()), key=lambda found: found[:2], reverse=True)[:3]:
            result = {key: match[key] for key in ("turn_id", "item_id", "role", "timestamp")}
            result["excerpt"] = _snippet(match["text"], terms, 320, phrase)
            result["url"] = item["url"] + "&" + urlencode({"turn": match["turn_id"], "item": match["item_id"]})
            item["matches"].append(result)
        item["message_count"] = totals.get(thread_id, 0)
        item["wake_paused"] = item["wake_pause_reason"] = None
        if item["app_conversation"] and "browser_inbox_wake" in tables:
            item["wake_paused"] = bool(wake and paused)
            item["wake_pause_reason"] = _snippet(error, [], 160) if wake and paused else None
        item["url"] = item.pop("url")
        items.append(item)
    next_cursor = None
    if len(selected) > limit:
        last = page[-1]
        next_cursor = base64.urlsafe_b64encode(json.dumps({"v": 1, "query": fingerprint,
            "score": last.score, "time": last.time, "id": last.thread_id}, separators=(",", ":")).encode()).decode()
    return {"items": items, "next_cursor": next_cursor, "coverage": coverage}


def search_browser_threads(store, sessions, query):
    """Search one app request's scope within the overview inventory boundary.

    The boundary comes from a single query; the live thread projection is built
    only when the current view filters on derived attention or stage.
    """
    from .thread_inventory import listed_threads
    scope = query.get("scope", ["all"])[0]
    if scope not in {"all", "all_projects", "current"}:
        raise CoordinationError("Search scope must be all, all_projects, or current.")
    show = query.get("show", ["active"])[0]
    phase = query.get("phase", [""])[0]
    placement = "archived" if show == "archived" else "later" if show == "later" else "now"
    if scope == "current" and (show in {"attention", "completed"} or phase):
        # No detail reads, provider resumes, or seen marks.
        threads = [t for t in sessions.list_work_threads(archived=show == "archived")
                   if t["attention"] == placement
                   and (show != "attention" or t["needs_attention"])
                   and (show != "completed" or t["work_phase"] == "finished")]
        if phase:
            threads = [t for t in threads if (t["work_phase"] or "new") == phase]
    else:
        with store._connection() as db:
            threads = listed_threads(store.threads, db, sessions.cwd)
        if scope == "current":
            threads = [t for t in threads if t["attention"] == placement]
    if scope == "current":
        for field in ("repository", "project"):
            selected = query.get(field, [""])[0]
            if selected:
                threads = [t for t in threads if t[field + "_id"] == (None if selected == "__none__" else selected)]
    if scope != "all_projects" and "project_cwd" in query:
        directory = query["project_cwd"][0]
        directory = str(Path(directory).expanduser().resolve()) if directory else ""
        threads = [t for t in threads if t["project_cwd"] == directory]
    return search_threads(store, query.get("q", [""])[0], limit=20, cursor=query.get("cursor", [None])[0],
                          my_messages=query.get("my_messages") == ["true"],
                          thread_ids=[t["thread_id"] for t in threads])
