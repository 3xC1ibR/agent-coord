"""Incremental, local search projection of saved conversation prose.

This never resumes providers, imports transcripts, or searches tool output.
History triggers invalidate the projection even when an older runtime saves it.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
import weakref
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass

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
_TEXT_CHARACTERS = 16 * 1024
_STAGE_LEASE_SECONDS = 300
_CLEANUP_BATCHES = 8
_METRICS_LOCK = threading.Lock()
_METRICS = weakref.WeakKeyDictionary()
_GENERATION_SCHEMA = {
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


def search_metrics(store):
    """Process-local counters and bounded latency samples; never write SQLite."""
    with _METRICS_LOCK:
        result = {}
        for operation, values in _METRICS.get(store, {}).items():
            samples = sorted(values["samples_ms"])
            result[operation] = {key: value for key, value in values.items() if key != "samples_ms"}
            result[operation]["p50_ms"] = samples[(len(samples) - 1) // 2]
            result[operation]["p95_ms"] = samples[min(len(samples) - 1, int(len(samples) * .95))]
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
            if version is None or version[0] != 2:
                db.execute(f"DROP VIEW IF EXISTS {name}")
            db.execute(statement)
    db.execute("INSERT OR REPLACE INTO conversation_search_projection_version VALUES (1, 2)")
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
            AND (SELECT version=2 FROM conversation_search_projection_version WHERE id=1),
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


def prepare_messages(store, thread_id):
    """Read one source revision, then decode it outside all store scopes."""
    with _measure(store, "prepare") as measurement:
        result = _prepare_messages(store, thread_id)
        measurement["messages"] = len(result.records)
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
        history = decode_history(row["history_json"], database_path=store.database_path)
        found = list(messages(history))
        routing = routing_history(history, found)
        if not isinstance(history, dict) or not isinstance(history.get("turns", []), list):
            raise ValueError("Invalid conversation history")
        status = "partial" if history.get("historyUnavailable") else "saved"
    except (ValueError, TypeError, AttributeError):
        found, status = [], "unavailable"
    # Search keeps the last duplicate ID; routing intentionally keeps them all.
    records = {(record[0], record[1]): (position, *record) for position, record in enumerate(found)}
    return PreparedMessages(thread_id, row["revision"], status, tuple(records.values()), json.dumps(routing))


def _stage_rows(prepared, generation):
    for position, turn_id, item_id, role, timestamp, text, folded in prepared.records:
        yield "conversation_search_entries", (generation, position, turn_id, item_id, role, timestamp)
        for start in range(0, max(len(text), len(folded)), _TEXT_CHARACTERS):
            yield "conversation_search_text_chunks", (generation, position, start // _TEXT_CHARACTERS,
                text[start:start + _TEXT_CHARACTERS], folded[start:start + _TEXT_CHARACTERS])
    for start in range(0, len(prepared.routing), _TEXT_CHARACTERS):
        yield "conversation_search_routing_chunks", (generation, start // _TEXT_CHARACTERS,
                                                     prepared.routing[start:start + _TEXT_CHARACTERS])


def _stage_batches(rows):
    batch, size = [], 0
    for table, row in rows:
        length = sum(len(value.encode("utf-8")) if isinstance(value, str) else 8 for value in row)
        if batch and (len(batch) >= _STAGE_ROWS or size + length > _STAGE_BYTES):
            yield batch
            batch, size = [], 0
        batch.append((table, row))
        size += length
    if batch:
        yield batch


def _stage_batch(store, generation, batch):
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
                WHERE generation=? AND state='staging'""", (time.time() + _STAGE_LEASE_SECONDS, generation))
            if renewed.rowcount != 1:
                return False
            for statement, parameters in statements:
                db.execute(statement, parameters)
        measurement.update(rows=len(batch), bytes=size, max_rows=len(batch), max_bytes=size)
    return True


def publish_messages(store, prepared):
    """Stage bounded transactions and expose a complete, unchanged revision."""
    with _measure(store, "projection") as measurement:
        published = _publish_messages(store, prepared)
        measurement["published" if published else "discarded"] = 1
        return published


def _publish_messages(store, prepared):
    generation = uuid.uuid4().hex if prepared.status is not None else None
    if generation is not None:
        with _operation(store, "search.stage"), store._connection() as db:
            db.execute("INSERT INTO conversation_search_generations VALUES (?, ?, 'staging', ?)",
                       (generation, prepared.thread_id, time.time() + _STAGE_LEASE_SECONDS))
        for batch in _stage_batches(_stage_rows(prepared, generation)):
            if not _stage_batch(store, generation, batch):
                return False
    with _measure(store, "publish") as measurement:
        result = _publish_head(store, prepared, generation)
        measurement["published" if result else "discarded"] = 1
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
    tables = ("conversation_search_text_chunks", "conversation_search_entries", "conversation_search_routing_chunks")
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


def refresh_messages(store, *, cwd=None):
    """Refresh the initial pending inventory without holding a writer to parse."""
    with _measure(store, "refresh") as measurement:
        _refresh_messages(store, cwd, measurement)


def _refresh_messages(store, cwd, measurement):
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
                return
        with store.reading(), store._connection() as db:
            _, identities, garbage = _pending_messages(db, cwd)
    measurement["pending_histories"] = len(identities)
    for thread_id in identities:
        publish_messages(store, prepare_messages(store, thread_id))
    if identities or garbage:
        from .browser_persistence import database_busy
        try:
            with store.connection_timeout(100):
                _cleanup_messages(store)
        except sqlite3.OperationalError as error:
            if not database_busy(error):
                raise


def message_condition(terms, role):
    predicates = [f"instr(m.folded, :q{i}) > 0" for i in range(len(terms))]
    if role == "user":
        predicates.append("m.role = 'user'")
    return " AND ".join(predicates)
