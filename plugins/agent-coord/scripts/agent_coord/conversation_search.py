"""Incremental, local search projection of saved conversation prose.

This never resumes providers, imports transcripts, or searches tool output.
History triggers invalidate the projection even when an older runtime saves it.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
import weakref
from collections import OrderedDict
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from functools import partial
from itertools import islice

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
                        "[Automatic Agent Coord inbox wake",
                        "[Automatic Agent Coord message delivery",
                        "Check and handle your unread agent-coord messages.",
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


def _ensure_schema(db):
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
        return False
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
    return True


def sync_messages(db, *, cwd=None):
    """Legacy transaction API; application callers must use refresh_messages."""
    if _current(db):
        return
    if not _ensure_schema(db):
        return
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
                from .history_persistence import decode_history
                history = decode_history(row["history_json"], db=db)
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


# Modern publication uses a separate generation schema. The original tables
# retain their shapes for installed older runtimes. Their dirty markers remain
# until a legacy reader rebuilds them; modern readers use the views below.
_STAGE_ROWS = 64
_STAGE_BYTES = 256 * 1024
_DOCUMENT_STAGE_ROWS = 512
# Smaller prose transactions reduce cold page/commit time. Preserve immutable
# part numbering: one existing 16K-character part may exceed this target, but
# still fits the original stage bound. Compact negative headers keep 512 rows.
_DOCUMENT_STAGE_BYTES = 64 * 1024
_STATEMENT_PARAMETERS = 999
_TEXT_CHARACTERS = 16 * 1024
_STAGE_LEASE_SECONDS = 300
_CLEANUP_BATCHES = 8
_METRICS_LOCK = threading.Lock()
_METRICS = weakref.WeakKeyDictionary()
_TURN_CACHE_LOCK = threading.Lock()
_TURN_CACHES = weakref.WeakKeyDictionary()
_TURN_CACHE_BYTES = 8 * 1024 * 1024
_TURN_CACHE_ENTRIES = 4096
_DOCUMENT_SCHEMA = {
    "conversation_search_documents": """CREATE TABLE IF NOT EXISTS conversation_search_documents (
        document TEXT PRIMARY KEY, item_id TEXT, role TEXT, timestamp REAL,
        excerpt TEXT NOT NULL, text_length INTEGER NOT NULL)""",
    "conversation_search_document_headers": """CREATE INDEX IF NOT EXISTS conversation_search_document_headers
        ON conversation_search_documents(document,item_id,role,timestamp,text_length)""",
    "conversation_search_document_text": """CREATE TABLE IF NOT EXISTS conversation_search_document_text (
        document TEXT NOT NULL, part INTEGER NOT NULL, text TEXT NOT NULL, folded TEXT NOT NULL,
        PRIMARY KEY (document, part))""",
}
_GENERATION_SCHEMA = {
    **_DOCUMENT_SCHEMA,
    "conversation_search_document_entries": """CREATE TABLE IF NOT EXISTS conversation_search_document_entries (
        generation TEXT NOT NULL, position INTEGER NOT NULL, turn_id TEXT NOT NULL, item_id TEXT NOT NULL,
        role TEXT NOT NULL, timestamp REAL, document TEXT NOT NULL, PRIMARY KEY (generation, position))""",
    "conversation_search_projection_version": """CREATE TABLE IF NOT EXISTS conversation_search_projection_version (
        id INTEGER PRIMARY KEY CHECK (id=1), version INTEGER NOT NULL)""",
    "conversation_search_revisions": """CREATE TABLE IF NOT EXISTS conversation_search_revisions (
        thread_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, indexed_revision INTEGER)""",
    "conversation_search_generations": """CREATE TABLE IF NOT EXISTS conversation_search_generations (
        generation TEXT PRIMARY KEY, thread_id TEXT NOT NULL, state TEXT NOT NULL, expires_at REAL NOT NULL)""",
    "conversation_search_heads": """CREATE TABLE IF NOT EXISTS conversation_search_heads (
        thread_id TEXT PRIMARY KEY, generation TEXT NOT NULL, revision INTEGER, status TEXT NOT NULL)""",
    "conversation_search_heads_generation": """CREATE INDEX IF NOT EXISTS conversation_search_heads_generation
        ON conversation_search_heads(generation)""",
    "conversation_search_entries": """CREATE TABLE IF NOT EXISTS conversation_search_entries (
        generation TEXT NOT NULL, position INTEGER NOT NULL, turn_id TEXT NOT NULL, item_id TEXT NOT NULL,
        role TEXT NOT NULL, timestamp REAL, PRIMARY KEY (generation, position))""",
    "conversation_search_text_chunks": """CREATE TABLE IF NOT EXISTS conversation_search_text_chunks (
        generation TEXT NOT NULL, position INTEGER NOT NULL, part INTEGER NOT NULL,
        text TEXT NOT NULL, folded TEXT NOT NULL, PRIMARY KEY (generation, position, part))""",
    "conversation_search_routing_chunks": """CREATE TABLE IF NOT EXISTS conversation_search_routing_chunks (
        generation TEXT NOT NULL, part INTEGER NOT NULL, text TEXT NOT NULL, PRIMARY KEY (generation, part))""",
    "conversation_search_message_index": """CREATE VIEW IF NOT EXISTS conversation_search_message_index AS
        SELECT h.thread_id, e.turn_id, e.item_id, e.role, e.timestamp,
            (SELECT group_concat(text, '') FROM (SELECT text FROM conversation_search_document_text c
             WHERE c.document=e.document ORDER BY c.part)) AS text,
            (SELECT group_concat(folded, '') FROM (SELECT folded FROM conversation_search_document_text c
             WHERE c.document=e.document ORDER BY c.part)) AS folded,
            e.position FROM conversation_search_heads h
        JOIN conversation_search_document_entries e ON e.generation=h.generation
        UNION ALL
        SELECT h.thread_id, e.turn_id, e.item_id, e.role, e.timestamp,
            (SELECT group_concat(text, '') FROM (SELECT text FROM conversation_search_text_chunks c
             WHERE c.generation=e.generation AND c.position=e.position ORDER BY c.part)) AS text,
            (SELECT group_concat(folded, '') FROM (SELECT folded FROM conversation_search_text_chunks c
             WHERE c.generation=e.generation AND c.position=e.position ORDER BY c.part)) AS folded,
            e.position FROM conversation_search_heads h
        JOIN conversation_search_entries e ON e.generation=h.generation
        UNION ALL SELECT m.* FROM conversation_search_messages m
        WHERE NOT EXISTS (SELECT 1 FROM conversation_search_heads h WHERE h.thread_id=m.thread_id)
          AND NOT EXISTS (SELECT 1 FROM conversation_search_dirty d WHERE d.thread_id=m.thread_id)
          AND EXISTS (SELECT 1 FROM conversation_search_routing r WHERE r.thread_id=m.thread_id)""",
    "conversation_search_history_index": """CREATE VIEW IF NOT EXISTS conversation_search_history_index AS
        SELECT thread_id, status FROM conversation_search_heads
        UNION ALL SELECT l.* FROM conversation_search_histories l
        WHERE NOT EXISTS (SELECT 1 FROM conversation_search_heads h WHERE h.thread_id=l.thread_id)
          AND NOT EXISTS (SELECT 1 FROM conversation_search_dirty d WHERE d.thread_id=l.thread_id)
          AND EXISTS (SELECT 1 FROM conversation_search_routing r WHERE r.thread_id=l.thread_id)""",
    "conversation_search_routing_index": """CREATE VIEW IF NOT EXISTS conversation_search_routing_index AS
        SELECT h.thread_id,
            (SELECT group_concat(text, '') FROM (SELECT text FROM conversation_search_routing_chunks c
             WHERE c.generation=h.generation ORDER BY c.part)) AS evidence_json
        FROM conversation_search_heads h LEFT JOIN conversation_search_revisions r ON r.thread_id=h.thread_id
        WHERE h.revision IS r.revision
        UNION ALL SELECT l.* FROM conversation_search_routing l
        WHERE NOT EXISTS (SELECT 1 FROM conversation_search_heads h WHERE h.thread_id=l.thread_id)
          AND NOT EXISTS (SELECT 1 FROM conversation_search_dirty d WHERE d.thread_id=l.thread_id)""",
}
_REVISION_TRIGGERS = tuple(f"conversation_search_revision_{event}" for event in ("insert", "update", "delete"))
_GENERATION_NAMES = (*_GENERATION_SCHEMA, *_REVISION_TRIGGERS)
_PENDING = """SELECT h.thread_id FROM browser_history h
    LEFT JOIN conversation_search_heads p ON p.thread_id=h.thread_id WHERE p.thread_id IS NULL
      AND (NOT EXISTS (SELECT 1 FROM conversation_search_histories l WHERE l.thread_id=h.thread_id)
           OR NOT EXISTS (SELECT 1 FROM conversation_search_routing r WHERE r.thread_id=h.thread_id)
           OR EXISTS (SELECT 1 FROM conversation_search_dirty d WHERE d.thread_id=h.thread_id))
    UNION SELECT thread_id FROM conversation_search_revisions WHERE indexed_revision IS NOT revision
    UNION SELECT d.thread_id FROM conversation_search_dirty d
          WHERE NOT EXISTS (SELECT 1 FROM conversation_search_revisions r
                            WHERE r.thread_id=d.thread_id AND r.indexed_revision IS r.revision)"""


def _operation(store, name):
    operation = getattr(store, "database_operation", None)
    return operation(name, priority="background") if operation else nullcontext()


@contextmanager
def _measure(store, operation):
    started = time.perf_counter()
    values = {}
    try:
        yield values
    except BaseException:
        values["failures"] = 1
        raise
    finally:
        elapsed = (time.perf_counter() - started) * 1000
        with _METRICS_LOCK:
            entry = _METRICS.setdefault(store, {}).setdefault(operation, {"count": 0, "total_ms": 0, "max_ms": 0, "samples_ms": []})
            entry["count"] += 1
            entry["total_ms"] += elapsed
            entry["max_ms"] = max(entry["max_ms"], elapsed)
            entry["samples_ms"] = (entry["samples_ms"] + [elapsed])[-256:]
            for key, value in values.items():
                entry[key] = max(entry.get(key, 0), value) if key.startswith("max_") else entry.get(key, 0) + value


def search_metrics(store, *, refresh_coverage=True):
    """Process-local counters and bounded latency samples; never write SQLite."""
    with _METRICS_LOCK:
        result = {}
        for operation, values in _METRICS.get(store, {}).items():
            samples = sorted(values["samples_ms"])
            result[operation] = {key: value for key, value in values.items() if key != "samples_ms"}
            result[operation]["p50_ms"] = samples[(len(samples) - 1) // 2]
            result[operation]["p95_ms"] = samples[min(len(samples) - 1, int(len(samples) * .95))]
    from .conversation_substrings import metrics as substring_metrics
    result["substring_index"] = substring_metrics(store, refresh_coverage=refresh_coverage)
    return result


def _ensure_generation_schema(db):
    db.execute("BEGIN IMMEDIATE")
    available = _ensure_schema(db)
    for statement in _GENERATION_SCHEMA.values():
        if not statement.startswith("CREATE VIEW"):
            db.execute(statement)
    version = db.execute("SELECT version FROM conversation_search_projection_version WHERE id=1").fetchone()
    for name, statement in _GENERATION_SCHEMA.items():
        if statement.startswith("CREATE VIEW"):
            if version is None or version[0] != 3:
                db.execute(f"DROP VIEW IF EXISTS {name}")
            db.execute(statement)
    db.execute("INSERT OR REPLACE INTO conversation_search_projection_version VALUES (1, 3)")
    if available:
        # An outer history UPSERT overrides OR REPLACE inside an older trigger.
        # Explicit UPSERT has its own conflict policy and keeps legacy writes safe.
        for event in ("insert", "update", "delete"):
            ref = "OLD" if event == "delete" else "NEW"
            db.execute(f"DROP TRIGGER IF EXISTS conversation_search_{event}")
            db.execute(f"""CREATE TRIGGER conversation_search_{event} AFTER {event.upper()} ON browser_history BEGIN
                INSERT INTO conversation_search_dirty VALUES ({ref}.thread_id)
                ON CONFLICT(thread_id) DO NOTHING; END""")
        for event, references in (("insert", ("NEW",)), ("update", ("OLD", "NEW")), ("delete", ("OLD",))):
            changes = " ".join(f"""INSERT INTO conversation_search_revisions (thread_id, revision)
                VALUES ({ref}.thread_id, 1) ON CONFLICT(thread_id) DO UPDATE SET revision=revision+1;"""
                for ref in references)
            db.execute(f"CREATE TRIGGER IF NOT EXISTS conversation_search_revision_{event} "
                       f"AFTER {event.upper()} ON browser_history BEGIN {changes} END")
    return available


def _pending_messages(db, cwd):
    scope, parameters = "", ()
    if cwd is not None:
        columns = {r[1] for r in db.execute("PRAGMA table_info(browser_sessions)")}
        source = "b.cwd" if "cwd" in columns else "s.cwd"
        scope = f""" WHERE thread_id IN (SELECT b.thread_id FROM browser_sessions b
            JOIN sessions s ON s.session_id=b.thread_id WHERE {source}=?)"""
        parameters = (cwd,)
    # Capture only identities, once. A streaming conversation cannot keep this
    # refresh chasing its dirty queue, and no backlog-sized payload is decoded.
    ready, identities, garbage = db.execute(f"""SELECT
        (SELECT COUNT(*) FROM sqlite_master WHERE name IN ({','.join('?' * len(_GENERATION_NAMES))})) = ?
            AND (SELECT version=3 FROM conversation_search_projection_version WHERE id=1),
        (SELECT json_group_array(thread_id) FROM ({_PENDING}){scope}),
        EXISTS (SELECT 1 FROM conversation_search_generations
                WHERE state='garbage' OR state='staging' AND expires_at<?)""",
        (*_GENERATION_NAMES, len(_GENERATION_NAMES), *parameters, time.time())).fetchone()
    return bool(ready), json.loads(identities), bool(garbage)


@dataclass(frozen=True)
class PreparedMessages:
    thread_id: str
    revision: int | None
    status: str | None
    records: tuple
    routing: str | None
    document_records: tuple = ()


def _document_key(references):
    # Include the extraction policy version: a later policy must not reuse
    # negative results (tool output or excluded provider context) from this one.
    return hashlib.sha256(("prose-v1:" + json.dumps(references, separators=(",", ":"))).encode()).hexdigest()


def _ensure_document_cache(store):
    with store.reading(), store._connection() as db:
        count = db.execute("SELECT COUNT(*) FROM sqlite_master WHERE name IN ("
                           + ",".join("?" * len(_DOCUMENT_SCHEMA)) + ")",
                           tuple(_DOCUMENT_SCHEMA)).fetchone()[0]
    if count != len(_DOCUMENT_SCHEMA):
        with _operation(store, "search.schema"), store._connection() as db:
            for statement in _DOCUMENT_SCHEMA.values():
                db.execute(statement)


def _cached_documents(db, identities, *, metadata=True):
    result = {}
    identities = list(identities)
    batch_rows = _DOCUMENT_STAGE_ROWS
    for offset in range(0, len(identities), batch_rows):
        batch = identities[offset:offset + batch_rows]
        # Preserve the header shape, but load payload only after selecting the
        # last 200 routing occurrences. The covering index excludes excerpts.
        fields = ("document,item_id,role,CASE WHEN timestamp IS NULL THEN NULL "
                  "ELSE printf('%!.17g',timestamp) END,NULL,text_length") if metadata else "document"
        rows = db.execute("SELECT json_group_array(json_array(" + fields + "))"
            " FROM conversation_search_documents INDEXED BY conversation_search_document_headers"
            " WHERE document IN (" + ",".join("?" * len(batch)) + ")",
            batch).fetchone()[0]
        for row in json.loads(rows):
            if metadata and row[3] is not None:
                row[3] = float(row[3])
            result[row[0]] = tuple(row)
    return result


def _cached_document_excerpts(db, identities):
    result = {}
    identities = list(identities)
    for offset in range(0, len(identities), _STAGE_ROWS):
        batch = identities[offset:offset + _STAGE_ROWS]
        rows = db.execute("SELECT json_group_array(json_array(document,excerpt))"
            " FROM conversation_search_documents WHERE document IN (" + ",".join("?" * len(batch)) + ")",
            batch).fetchone()[0]
        result.update(json.loads(rows))
    return result


def _document_rows(identity, item):
    found = next(messages({"turns": [{"items": [item]}]}), None)
    if found is None:
        header = (identity, None, None, None, "", 0)
        return header, ()
    _, _, role, timestamp, text, folded = found
    header = (identity, str(item["id"]) if item.get("id") else None,
              role, timestamp, text[:6000], len(text))
    return header, _prose_chunks(identity, text, folded)


def _prose_chunks(identity, text, folded):
    return tuple((identity, start // _TEXT_CHARACTERS, text[start:start + _TEXT_CHARACTERS],
                  folded[start:start + _TEXT_CHARACTERS])
                 for start in range(0, max(len(text), len(folded)), _TEXT_CHARACTERS))



def _inline_document_records(store, records):
    # Inline histories lack immutable item references. Reuse the same prose
    # cache and substring sources as manifest histories. Occurrence identity,
    # timestamp and position stay on the thread, even for identical prose.
    occurrences, prose = [], {}
    for position, turn, item, role, timestamp, text, folded in records:
        key = "inline-v1:" + role + ":" + hashlib.sha256(text.encode("utf-8")).hexdigest()
        occurrences.append((position, turn, item, role, timestamp, key))
        prose[key] = (role, text, folded)
    with store.reading(), store._connection() as db:
        cached = _cached_documents(db, prose, metadata=False)

    def missing():
        for key, (role, text, folded) in prose.items():
            if key not in cached:
                header = (key, None, role, None, text[:6000], len(text))
                yield header, _prose_chunks(key, text, folded)

    for _ in _stage_document_steps(store, missing()):
        pass
    return tuple(occurrences)


def _document_stage_rows(documents):
    for header, chunks in documents:
        for row in chunks:
            yield "conversation_search_document_text", row
        # This row is the completion marker. It is committed only after all
        # text chunks; interrupted staging is invisible and safely resumable.
        yield "conversation_search_documents", header


def _stage_document_steps(store, documents):
    for batch in _stage_batches(_document_stage_rows(documents), max_rows=_DOCUMENT_STAGE_ROWS,
                                max_bytes=_DOCUMENT_STAGE_BYTES):
        grouped = {}
        for table, row in batch:
            grouped.setdefault(table, []).append(row)
        statements = []
        for table, rows in grouped.items():
            # Several statements share one bounded transaction. Keep support
            # for SQLite builds with the original 999-variable statement limit.
            statement_rows = _STATEMENT_PARAMETERS // len(rows[0])
            for offset in range(0, len(rows), statement_rows):
                selected = rows[offset:offset + statement_rows]
                statements.append((f"INSERT OR IGNORE INTO {table} VALUES " + ",".join(
                    "(" + ",".join("?" * len(rows[0])) + ")" for _ in selected),
                    tuple(value for row in selected for value in row)))
        size = sum(sum(len(v.encode("utf-8")) if isinstance(v, str) else 8 for v in row) for _, row in batch)
        with _measure(store, "documents") as measurement:
            with _operation(store, "search.documents"), store._connection() as db:
                for statement, parameters in statements:
                    db.execute(statement, parameters)
            measurement.update(rows=len(batch), bytes=size, max_rows=len(batch), max_bytes=size)
        yield


def seed_prepared_history_steps(store, prepared):
    """Seed immutable prose during saving, before publishing the history root.

    Only item documents with a materialized value need consideration. Reused
    compact documents were seeded by their earlier save; search independently
    handles cache misses from older runtimes. Every yield is outside DB scopes.
    """
    _ensure_document_cache(store)
    yield
    documents = (document for position, document in prepared.documents.items()
                 if len(position) == 2 and document.value is not None)
    while batch := list(islice(documents, _DOCUMENT_STAGE_ROWS)):
        available = {_document_key(document.references): document.value for document in batch}
        with store.reading(), store._connection() as db:
            cached = _cached_documents(db, available, metadata=False)
        yield
        yield from _stage_document_steps(store, (_document_rows(identity, item)
                                                for identity, item in available.items() if identity not in cached))
        from .conversation_substrings import index_document_steps
        yield from index_document_steps(store, available)


def _prepare_document_messages(store, thread_id, revision, manifest):
    from .history_persistence import _documents
    _ensure_document_cache(store)
    file_identity = store._database_file_identity()
    with _measure(store, "document_read") as measurement:
        with store.reading(), store._connection() as db:
            db.execute("BEGIN")
            root = _documents(db, [manifest["root"]])[0]
            metadata, turn_references = root["metadata"], root["turns"]
            if not isinstance(metadata, dict) or not isinstance(turn_references, list):
                raise ValueError("Invalid conversation manifest")
            keys = [tuple(reference) for reference in turn_references]
            with _TURN_CACHE_LOCK:
                identity, cache, _ = _TURN_CACHES.get(store, (None, {}, 0))
                if identity != file_identity:
                    cache = {}
                retained = {key: cache[key][0] for key in keys if key in cache}
                for key in retained:
                    cache.move_to_end(key)
            absent = list(dict.fromkeys(key for key in keys if key not in retained))
            turns = _documents(db, [list(key) for key in absent])
            references = {}
            for turn in turns:
                if not isinstance(turn.get("metadata"), dict) or not isinstance(turn.get("items", []), list):
                    raise ValueError("Invalid conversation turn manifest")
                for reference in turn.get("items", []):
                    references[_document_key(reference)] = reference
            cached = _cached_documents(db, references)
            missing = [(identity, reference) for identity, reference in references.items() if identity not in cached]
            documents = []
            for offset in range(0, len(missing), _STAGE_ROWS):
                batch = missing[offset:offset + _STAGE_ROWS]
                items = _documents(db, [reference for _, reference in batch])
                for (identity, _), item in zip(batch, items):
                    document = _document_rows(identity, item)
                    documents.append(document)
                    cached[identity] = document[0]
            measurement.update(decoded_documents=len(missing), reused_documents=len(references) - len(missing))
        for _ in _stage_document_steps(store, documents):
            pass
        # Turn references are content addressed. Retain only their compact
        # occurrence metadata, after every referenced prose header is durable.
        # Positions, fallback IDs and routing are rebuilt from the current root;
        # no revision or publication decision is cached.
        added = {}
        for key, turn in zip(absent, turns):
            headers = tuple(cached[_document_key(reference)] for reference in turn.get("items", []))
            headers = tuple((*header[:4], None, header[5]) for header in headers)
            value = ({field: turn["metadata"][field] for field in ("id", "startedAt")
                      if field in turn["metadata"]}, headers)
            size = len(json.dumps((key, value)).encode("utf-8")) + 256 + 256 * len(headers)
            retained[key] = value
            if size <= _TURN_CACHE_BYTES:
                added[key] = (value, size)
        with _TURN_CACHE_LOCK:
            identity, cache, size = _TURN_CACHES.get(store, (None, OrderedDict(), 0))
            if identity != file_identity:
                cache, size = OrderedDict(), 0
            for key, entry in added.items():
                previous = cache.pop(key, None)
                size += entry[1] - (previous[1] if previous else 0)
                cache[key] = entry
            while size > _TURN_CACHE_BYTES or len(cache) > _TURN_CACHE_ENTRIES:
                _, (_, removed) = cache.popitem(last=False)
                size -= removed
            if file_identity == store._database_file_identity():
                _TURN_CACHES[store] = (file_identity, cache, size)
        measurement.update(decoded_turns=len(absent), reused_turns=len(keys) - len(absent))
    records, routing, count = {}, [], 0
    for turn_index, key in enumerate(keys):
        turn_metadata, headers = retained[key]
        turn_id = str(turn_metadata.get("id") or f"search-turn-{turn_index}")
        for item_index, header in enumerate(headers):
            document, item_id, role, timestamp, excerpt, text_length = header
            if role is None:
                continue
            item_id = item_id or f"search-item-{item_index}"
            timestamp = timestamp or seconds(turn_metadata.get("startedAt"))
            records[(turn_id, item_id)] = (count, turn_id, item_id, role, timestamp, document)
            routing.append((document, {"turn_id": turn_id, "item_id": item_id, "role": role,
                            "timestamp": timestamp, "text": excerpt, "truncated": text_length > 6000}))
            if len(routing) > 200:
                del routing[0]
            count += 1
    selected = {document for document, record in routing if record["text"] is None}
    with _measure(store, "routing_read") as measurement:
        excerpts = {}
        if selected:
            with store.reading(), store._connection() as db:
                excerpts = _cached_document_excerpts(db, selected)
        measurement.update(documents=len(excerpts), characters=sum(map(len, excerpts.values())))
    for document, record in routing:
        if record["text"] is None:
            record["text"] = excerpts[document]
    status = "partial" if metadata.get("historyUnavailable") else "saved"
    evidence = {"history_status": status, "messages": [record for _, record in routing], "history_truncated": count > 200}
    return PreparedMessages(thread_id, revision, status, (), json.dumps(evidence), tuple(records.values()))


def prepare_messages(store, thread_id):
    """Read one source revision, then decode it outside all store scopes."""
    with _measure(store, "prepare") as measurement:
        result = _prepare_messages(store, thread_id)
        measurement["messages"] = len(result.records) + len(result.document_records)
        return result


def _prepare_messages(store, thread_id):
    with store.reading(), store._connection() as db:
        row = db.execute("""SELECT h.history_json, r.revision FROM (SELECT ? AS thread_id) t
            LEFT JOIN browser_history h ON h.thread_id=t.thread_id
            LEFT JOIN conversation_search_revisions r ON r.thread_id=t.thread_id""", (thread_id,)).fetchone()
    if row["history_json"] is None:
        return PreparedMessages(thread_id, row["revision"], None, (), None)
    routing = {"messages": [], "history_status": "unavailable"}
    try:
        from .history_persistence import decode_history
        manifest = json.loads(row["history_json"])
        from .history_persistence import MARKER
        if isinstance(manifest, dict) and manifest.get(MARKER) == 1 and manifest.get("layout") == "turns":
            return _prepare_document_messages(store, thread_id, row["revision"], manifest)
        history = decode_history(row["history_json"], database_path=store.database_path)
        found = list(messages(history))
        routing = routing_history(history, found)
        if not isinstance(history, dict) or not isinstance(history.get("turns", []), list):
            raise ValueError("Invalid conversation history")
        status = "partial" if history.get("historyUnavailable") else "saved"
    except (ValueError, TypeError, AttributeError, KeyError):
        found, status = [], "unavailable"
    # Search keeps the last duplicate ID; routing intentionally keeps them all.
    records = {(record[0], record[1]): (position, *record) for position, record in enumerate(found)}
    # A generation would stage at least two rows per inline message.
    # Above one batch, give individual prose immutable identities so a change
    # to another occurrence never invalidates their substring sources.
    if len(records) >= _STAGE_ROWS // 2:
        occurrences = _inline_document_records(store, records.values())
        return PreparedMessages(thread_id, row["revision"], status, (), json.dumps(routing), occurrences)
    return PreparedMessages(thread_id, row["revision"], status, tuple(records.values()), json.dumps(routing))


def _stage_rows(prepared, generation):
    for record in prepared.document_records:
        yield "conversation_search_document_entries", (generation, *record)
    for position, turn_id, item_id, role, timestamp, text, folded in prepared.records:
        yield "conversation_search_entries", (generation, position, turn_id, item_id, role, timestamp)
        for start in range(0, max(len(text), len(folded)), _TEXT_CHARACTERS):
            yield "conversation_search_text_chunks", (generation, position, start // _TEXT_CHARACTERS,
                text[start:start + _TEXT_CHARACTERS], folded[start:start + _TEXT_CHARACTERS])
    for start in range(0, len(prepared.routing), _TEXT_CHARACTERS):
        yield "conversation_search_routing_chunks", (generation, start // _TEXT_CHARACTERS,
                                                     prepared.routing[start:start + _TEXT_CHARACTERS])


def _stage_batches(rows, *, max_rows=_STAGE_ROWS, max_bytes=_STAGE_BYTES):
    batch, size = [], 0
    for table, row in rows:
        length = sum(len(value.encode("utf-8")) if isinstance(value, str) else 8 for value in row)
        if batch and (len(batch) >= max_rows or size + length > max_bytes):
            yield batch
            batch, size = [], 0
        batch.append((table, row))
        size += length
    if batch:
        yield batch


def _stage_batch(store, generation, batch, revision):
    # Group SQL parameters before writer admission as well as history decoding.
    grouped = {}
    for table, row in batch:
        grouped.setdefault(table, []).append(row)
    statements = [(f"INSERT INTO {table} VALUES " + ",".join(
        "(" + ",".join("?" * len(rows[0])) + ")" for _ in rows),
        tuple(value for row in rows for value in row)) for table, rows in grouped.items()]
    size = sum(sum(len(v.encode("utf-8")) if isinstance(v, str) else 8 for v in row) for _, row in batch)
    with _measure(store, "stage") as measurement:
        with _operation(store, "search.stage"), store._connection() as db:
            renewed = db.execute("""UPDATE conversation_search_generations SET expires_at=?
                WHERE generation=? AND state='staging' AND (SELECT r.revision FROM conversation_search_revisions r
                    WHERE r.thread_id=conversation_search_generations.thread_id) IS ?""",
                (time.time() + _STAGE_LEASE_SECONDS, generation, revision))
            if renewed.rowcount != 1:
                db.execute("UPDATE conversation_search_generations SET state='garbage' "
                           "WHERE generation=? AND state='staging'", (generation,))
                return False
            for statement, parameters in statements:
                db.execute(statement, parameters)
        measurement.update(rows=len(batch), bytes=size, max_rows=len(batch), max_bytes=size)
    return True


def _document_delta(store, prepared):
    """Compare compact occurrences against one published snapshot outside the writer."""
    if not prepared.document_records:
        return None
    with store.reading(), store._connection() as db:
        db.execute("BEGIN")
        head = db.execute("""SELECT h.generation,h.revision FROM conversation_search_heads h
            WHERE h.thread_id=? AND NOT EXISTS (SELECT 1 FROM conversation_search_entries e
                WHERE e.generation=h.generation)""", (prepared.thread_id,)).fetchone()
        if head is None:
            return None
        generation, revision = head
        # A large truncation gets a fresh generation plus bounded cleanup.
        rows = db.execute("""SELECT position,turn_id,item_id,role,timestamp,document
            FROM conversation_search_document_entries WHERE generation=? LIMIT ?""",
            (generation, len(prepared.document_records) + _STAGE_ROWS + 1)).fetchall()
        if len(rows) > len(prepared.document_records) + _STAGE_ROWS:
            return None
        old_records = {row[0]: tuple(row) for row in rows}
        writes = [row for row in prepared.document_records if old_records.pop(row[0], None) != row]
        changes = len(writes) + len(old_records)
        if changes > _STAGE_ROWS:
            return None
        size = sum(sum(len(v.encode("utf-8")) if isinstance(v, str) else 8 for v in row) for row in writes)
        if size > _STAGE_BYTES:
            return None
        routing = {offset // _TEXT_CHARACTERS: prepared.routing[offset:offset + _TEXT_CHARACTERS]
                   for offset in range(0, len(prepared.routing), _TEXT_CHARACTERS)}
        parts, removed_parts = [], []
        # Read existing parts one at a time. Retain a large routing window
        # when its changed parts fit the normal transaction budget.
        for part, old_text in db.execute("""SELECT part,text FROM conversation_search_routing_chunks
                WHERE generation=? ORDER BY part""", (generation,)):
            new_text = routing.pop(part, None)
            if new_text == old_text:
                continue
            if new_text is None:
                removed_parts.append(part)
            else:
                parts.append((part, new_text))
            size += max(len(old_text), len(new_text or ""))
            if size > _STAGE_BYTES or changes + len(parts) + len(removed_parts) > _STAGE_ROWS:
                return None
    if (changes + len(parts) + len(removed_parts) + len(routing) > _STAGE_ROWS
            or size + sum(map(len, routing.values())) > _STAGE_BYTES):
        return None
    parts.extend(routing.items())
    return generation, revision, writes, tuple(old_records), parts, removed_parts


def _publish_document_delta(store, prepared, delta):
    # Published document keys are immutable. Only the thread occurrence map
    # and its matching routing/status change, in a single bounded transaction.
    generation, revision, writes, removed, parts, removed_parts = delta
    with _operation(store, "search.publish"), store._connection() as db:
        db.execute("BEGIN IMMEDIATE")
        head = db.execute("""UPDATE conversation_search_heads SET revision=?,status=?
            WHERE thread_id=? AND generation=? AND revision IS ?
              AND (SELECT revision FROM conversation_search_revisions WHERE thread_id=?) IS ?""",
            (prepared.revision, prepared.status, prepared.thread_id, generation, revision,
             prepared.thread_id, prepared.revision))
        if head.rowcount != 1:
            return False
        for table, column, values in (("conversation_search_document_entries", "position", removed),
                                      ("conversation_search_routing_chunks", "part", removed_parts)):
            if values:
                db.execute(f"DELETE FROM {table} WHERE generation=? AND {column} IN ("
                           + ",".join("?" * len(values)) + ")", (generation, *values))
        if writes:
            db.executemany("INSERT OR REPLACE INTO conversation_search_document_entries VALUES (?,?,?,?,?,?,?)",
                           [(generation, *row) for row in writes])
        if parts:
            db.executemany("INSERT OR REPLACE INTO conversation_search_routing_chunks VALUES (?,?,?)",
                           [(generation, part, text) for part, text in parts])
        db.execute("UPDATE conversation_search_revisions SET indexed_revision=revision WHERE thread_id=?",
                   (prepared.thread_id,))
    return True


def publish_messages(store, prepared, *, schedule_index=None):
    """Stage bounded transactions and expose a complete, unchanged revision."""
    with _measure(store, "projection") as measurement:
        published = _publish_messages(store, prepared, schedule_index)
        measurement["published" if published else "discarded"] = 1
        return published


def _publish_messages(store, prepared, schedule_index):
    from .conversation_substrings import index_document_steps, index_generation_steps

    delta = _document_delta(store, prepared)
    generation = None
    staged = True
    if delta is None and prepared.status is not None:
        generation = uuid.uuid4().hex
        with _operation(store, "search.stage"), store._connection() as db:
            inserted = db.execute("""INSERT INTO conversation_search_generations
                SELECT ?, ?, 'staging', ? WHERE
                (SELECT revision FROM conversation_search_revisions WHERE thread_id=?) IS ?""",
                (generation, prepared.thread_id, time.time() + _STAGE_LEASE_SECONDS,
                 prepared.thread_id, prepared.revision))
            staged = inserted.rowcount == 1
        if staged:
            staged = all(_stage_batch(store, generation, batch, prepared.revision)
                         for batch in _stage_batches(_stage_rows(prepared, generation)))
    with _measure(store, "publish") as measurement:
        result = (_publish_document_delta(store, prepared, delta) if delta is not None
                  else staged and _publish_head(store, prepared, generation))
        measurement["published" if result else "discarded"] = 1
    if result and prepared.status is not None:
        if prepared.document_records:
            documents = tuple(record[-1] for record in prepared.document_records)
            index_steps = partial(index_document_steps, store, documents)
        else:
            index_steps = partial(index_generation_steps, store, generation)
        # Queries scan published prose until its trigram index is ready. A
        # restart before this work completes is covered by startup backfill.
        if schedule_index is None:
            for _ in index_steps():
                pass
        else:
            schedule_index(index_steps)
    return result


def _publish_head(store, prepared, generation):
    with _operation(store, "search.publish"), store._connection() as db:
        db.execute("BEGIN IMMEDIATE")
        revision = db.execute("SELECT revision FROM conversation_search_revisions WHERE thread_id=?",
                              (prepared.thread_id,)).fetchone()
        if (revision[0] if revision else None) != prepared.revision:
            if generation is not None:
                db.execute("UPDATE conversation_search_generations SET state='garbage' WHERE generation=?", (generation,))
            return False
        if generation is not None and not db.execute("""SELECT 1 FROM conversation_search_generations
                WHERE generation=? AND state='staging'""", (generation,)).fetchone():
            return False
        # Preserve an indexed tombstone even for sources deleted before the
        # revision triggers were installed. Its legacy dirty marker must remain
        # for older runtimes, without causing endless modern re-publication.
        current_revision = prepared.revision if prepared.revision is not None else 0
        db.execute("INSERT INTO conversation_search_revisions VALUES (?, ?, ?) "
                   "ON CONFLICT(thread_id) DO NOTHING", (prepared.thread_id, current_revision, current_revision))
        db.execute("""UPDATE conversation_search_generations SET state='garbage' WHERE generation IN
            (SELECT generation FROM conversation_search_heads WHERE thread_id=?)""", (prepared.thread_id,))
        if generation is None:
            db.execute("DELETE FROM conversation_search_heads WHERE thread_id=?", (prepared.thread_id,))
        else:
            db.execute("INSERT OR REPLACE INTO conversation_search_heads VALUES (?, ?, ?, ?)",
                       (prepared.thread_id, generation, current_revision, prepared.status))
            db.execute("UPDATE conversation_search_generations SET state='published' WHERE generation=?", (generation,))
        db.execute("UPDATE conversation_search_revisions SET indexed_revision=revision WHERE thread_id=?",
                   (prepared.thread_id,))
    return True


def _cleanup_messages(store):
    """Bound garbage collection too; an expired stage can never resume midway."""
    tables = ("conversation_search_text_chunks", "conversation_search_entries", "conversation_search_routing_chunks",
              "conversation_search_document_entries")
    for _ in range(_CLEANUP_BATCHES):
        with store.reading(), store._connection() as db:
            row = db.execute("""SELECT generation FROM conversation_search_generations g
                WHERE (state='garbage' OR state='staging' AND expires_at<?)
                AND NOT EXISTS (SELECT 1 FROM conversation_search_heads h WHERE h.generation=g.generation)
                LIMIT 1""", (time.time(),)).fetchone()
        if row is None:
            return
        generation = row[0]
        with _operation(store, "search.cleanup"), store._connection() as db:
            db.execute("""UPDATE conversation_search_generations SET state='garbage' WHERE generation=?
                AND (state='garbage' OR state='staging' AND expires_at<?)
                AND NOT EXISTS (SELECT 1 FROM conversation_search_heads WHERE generation=?)""",
                (generation, time.time(), generation))
            if not db.execute("SELECT 1 FROM conversation_search_generations WHERE generation=? AND state='garbage'",
                              (generation,)).fetchone():
                continue
            for table in tables:
                removed = db.execute(f"DELETE FROM {table} WHERE rowid IN "
                    f"(SELECT rowid FROM {table} WHERE generation=? LIMIT ?)", (generation, _STAGE_ROWS))
                if removed.rowcount:
                    break
            else:
                db.execute("DELETE FROM conversation_search_generations WHERE generation=?", (generation,))


def refresh_messages(store, *, cwd=None, schedule_index=None):
    """Refresh the initial pending inventory without holding a writer to parse."""
    with _measure(store, "refresh") as measurement:
        _refresh_messages(store, cwd, measurement, schedule_index)


def ensure_message_schema(store, cwd=None):
    with store.reading(), store._connection() as db:
        try:
            ready, identities, garbage = _pending_messages(db, cwd)
        except sqlite3.OperationalError as error:
            if "no such table" not in str(error):
                raise
            ready, identities, garbage = False, [], False
            if str(error) == "no such table: browser_history":
                # Terminal-only stores have no history source. Once the empty
                # views exist, context searches still need no writer admission.
                count = db.execute(f"SELECT COUNT(*) FROM sqlite_master WHERE name IN "
                    f"({','.join('?' * len(_GENERATION_SCHEMA))})", tuple(_GENERATION_SCHEMA)).fetchone()[0]
                ready = count == len(_GENERATION_SCHEMA)
    if not ready:
        with _operation(store, "search.schema"), store._connection() as db:
            if not _ensure_generation_schema(db):
                return [], False
        with store.reading(), store._connection() as db:
            _, identities, garbage = _pending_messages(db, cwd)
    return identities, garbage


def _refresh_messages(store, cwd, measurement, schedule_index):
    identities, garbage = ensure_message_schema(store, cwd)
    measurement["pending_histories"] = len(identities)
    publish = publish_messages if schedule_index is None else partial(publish_messages, schedule_index=schedule_index)
    for thread_id in identities:
        for attempt in range(3):
            if publish(store, prepare_messages(store, thread_id)):
                break
            measurement["revision_conflicts"] = measurement.get("revision_conflicts", 0) + 1
        else:
            # Returning normally would let the query advertise an older head
            # as fresh. Keep the initial identity inventory bounded, but never
            # silently hide or substitute a source that changes during staging.
            from .store import CoordinationError
            raise CoordinationError("Conversation search changed repeatedly while indexing. Try again.")
    if identities or garbage:
        from .browser_persistence import database_busy
        try:
            with store.connection_timeout(100):
                _cleanup_messages(store)
        except sqlite3.OperationalError as error:
            if not database_busy(error):
                raise


def _pending_prose_history(db, manifest, measurement):
    """Read current occurrence order with immutable prose, in the caller's snapshot."""
    from .history_persistence import _documents

    root = _documents(db, [manifest["root"]])[0]
    if not isinstance(root["metadata"], dict) or not isinstance(root["turns"], list):
        raise ValueError("Invalid conversation manifest")
    turns = _documents(db, root["turns"])
    references = {}
    for turn in turns:
        if not isinstance(turn["metadata"], dict) or not isinstance(turn.get("items", []), list):
            raise ValueError("Invalid conversation turn manifest")
        for reference in turn.get("items", []):
            references[_document_key(reference)] = reference
    headers = _cached_documents(db, references)
    prose = {key for key, header in headers.items() if header[2] is not None}
    text = _cached_document_excerpts(db, {key for key in prose if headers[key][5] <= 6000})
    large = sorted(key for key in prose if headers[key][5] > 6000)
    for offset in range(0, len(large), _STAGE_ROWS):
        batch = large[offset:offset + _STAGE_ROWS]
        rows = db.execute("SELECT document,group_concat(text,'') FROM "
            "(SELECT document,text FROM conversation_search_document_text WHERE document IN ("
            + ",".join("?" * len(batch)) + ") ORDER BY document,part) GROUP BY document", batch)
        text.update(rows)
    if any(len(text.get(key, "")) != headers[key][5] for key in prose):
        raise ValueError("Incomplete immutable search prose")
    missing = [(key, reference) for key, reference in references.items() if key not in headers]
    for offset in range(0, len(missing), _STAGE_ROWS):
        batch = missing[offset:offset + _STAGE_ROWS]
        for (key, _), item in zip(batch, _documents(db, [reference for _, reference in batch])):
            header, chunks = _document_rows(key, item)
            headers[key] = header
            if header[2] is not None:
                text[key] = "".join(chunk[2] for chunk in chunks)
    measurement["decoded_documents"] = measurement.get("decoded_documents", 0) + len(missing)
    measurement["reused_documents"] = measurement.get("reused_documents", 0) + len(references) - len(missing)
    decoded = []
    for turn in turns:
        items = []
        for reference in turn.get("items", []):
            key = _document_key(reference)
            _, identity, role, stamp, _, _ = headers[key]
            # Keep placeholders: fallback item IDs use original positions.
            item = {}
            if role is not None:
                item = {"id": identity, "timelineAt": stamp}
                if role == "user":
                    item.update(type="userMessage", content=[{"type": "text", "text": text[key]}])
                else:
                    item.update(type="agentMessage", text=text[key])
            items.append(item)
        decoded.append({**turn["metadata"], "items": items})
    return {**root["metadata"], "turns": decoded}


def pending_search_evidence(db, terms, thread_ids, *, user_only, published, measurement):
    """Inspect current revisions in the same SQLite snapshot as the ranked query."""
    from .history_persistence import MARKER, decode_history, prepared_history_view

    filter_ids = "" if thread_ids is None else " WHERE p.thread_id IN (SELECT value FROM json_each(?))"
    parameters = () if thread_ids is None else (json.dumps(sorted(thread_ids)),)
    rows = db.execute(f"""SELECT p.thread_id, h.history_json, s.turn_active FROM ({_PENDING}) p
        LEFT JOIN browser_history h ON h.thread_id=p.thread_id
        LEFT JOIN sessions s ON s.session_id=p.thread_id{filter_ids}""", parameters)
    statuses, matches, settled = {}, [], []
    for identity, serialized, active in rows:
        statuses[identity] = None
        if serialized is None:
            settled.append(identity)
            continue
        try:
            prepared = published.get(identity)
            history = prepared_history_view(prepared, serialized)
            missing_documents = measurement.get("decoded_documents", 0)
            if history is None:
                manifest = json.loads(serialized)
                if isinstance(manifest, dict) and manifest.get(MARKER) == 1 and manifest.get("layout") == "turns":
                    history = _pending_prose_history(db, manifest, measurement)
                else:
                    history = decode_history(serialized, db=db)
                measurement["database_reads"] = measurement.get("database_reads", 0) + 1
            else:
                measurement["published_reads"] = measurement.get("published_reads", 0) + 1
                measurement["published_bytes"] = measurement.get("published_bytes", 0) + prepared.source_bytes
            if not isinstance(history, dict) or not isinstance(history.get("turns", []), list):
                raise ValueError("Invalid conversation history")
            found = {}
            for position, record in enumerate(messages(history)):
                turn, item, role, stamp, text, folded = record
                key = (turn, item)
                if (not user_only or role == "user") and all(term in folded for term in terms):
                    # Keep the original precision through SQLite JSON functions.
                    timestamp = None if stamp is None else "binary64:" + stamp.hex()
                    found[key] = (identity, turn, item, role, timestamp, text, folded, position)
                else:
                    found.pop(key, None)
            matches.extend(found.values())
            statuses[identity] = "partial" if history.get("historyUnavailable") else "saved"
            if (measurement.get("decoded_documents", 0) > missing_documents or
                    (not active and not any(turn.get("status") == "inProgress" for turn in history.get("turns", [])
                                           if isinstance(turn, dict)))):
                settled.append(identity)
        except (ValueError, TypeError, AttributeError, KeyError):
            statuses[identity] = "unavailable"
    return statuses, matches, settled


def index_search_snapshot(store, identities, schedule_index):
    """Seed immutable documents even for active threads; publish only idle revisions."""
    for identity in identities:
        with store.reading(), store._connection() as db:
            pending = db.execute(f"""SELECT COALESCE(s.turn_active, 0) FROM ({_PENDING}) p
                LEFT JOIN sessions s ON s.session_id=p.thread_id WHERE p.thread_id=?""", (identity,)).fetchone()
        if pending is None:
            continue
        prepared = prepare_messages(store, identity)
        if not pending[0]:
            publish_messages(store, prepared, schedule_index=schedule_index)
        yield
    _cleanup_messages(store)


def message_condition(terms, role):
    predicates = [f"instr(m.folded, :q{i}) > 0" for i in range(len(terms))]
    if role == "user":
        predicates.append("m.role = 'user'")
    return " AND ".join(predicates)
