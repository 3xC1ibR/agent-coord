"""Bounded substring candidates; exact prose matching remains authoritative.

Each immutable source is indexed in overlapping folded-text parts. Publication
of its completion marker follows every part. A missing marker, unsupported
tokenizer, or short query always uses the complete ordinary search projection.
Legacy rows have monotonic revisions so updates and rowid reuse cannot publish
stale candidates. These derived immutable index records are retained, like the
history blocks; collection needs a separate reader/staging retention contract.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import weakref


PART_CHARACTERS = 16 * 1024
# Tokenizing several full output parts in one transaction can hold the only
# writer beyond foreground admission deadlines. Keep the persisted part format
# and its two-character overlap, but yield between full parts.
BATCH_BYTES = 24 * 1024
BATCH_ROWS = 512
READ_ROWS = 32
MERGE_PAGES = 8
_LOCK = threading.Lock()
_STATUS = weakref.WeakKeyDictionary()
_CAPABILITY = weakref.WeakKeyDictionary()

_TABLES = {
    "conversation_substring_parts": """CREATE TABLE IF NOT EXISTS conversation_substring_parts (
        id INTEGER PRIMARY KEY, source TEXT NOT NULL, part INTEGER NOT NULL, indexed INTEGER NOT NULL DEFAULT 0,
        UNIQUE(source,part))""",
    "conversation_substring_ready": """CREATE TABLE IF NOT EXISTS conversation_substring_ready (
        source TEXT PRIMARY KEY, usable INTEGER NOT NULL)""",
    "conversation_substring_revisions": """CREATE TABLE IF NOT EXISTS conversation_substring_revisions (
        message_rowid INTEGER PRIMARY KEY, revision INTEGER NOT NULL)""",
}
_FTS = """CREATE VIRTUAL TABLE IF NOT EXISTS conversation_substring_terms USING fts5(
    folded, tokenize='trigram case_sensitive 1', content='', detail=none, columnsize=0)"""


def _status(store, **values):
    with _LOCK:
        state = _STATUS.setdefault(store, {"supported": None, "state": "uninitialized", "failures": 0,
                                          "backfill_running": False, "remaining_sources": None})
        state.update(values)


def metrics(store):
    with _LOCK:
        state = dict(_STATUS.get(store, {"supported": None, "state": "uninitialized", "failures": 0,
                                         "backfill_running": False, "remaining_sources": None}))
        observed = time.monotonic()
        refresh = (state.get("supported") and not state.get("coverage_refresh_running")
                   and observed - state.get("coverage_observed_monotonic", 0) >= 1)
        if refresh:
            # Reserve this observation before leaving the lock. Concurrent
            # performance requests must not repeat the same metadata scan.
            _STATUS[store]["coverage_observed_monotonic"] = observed
            _STATUS[store]["coverage_refresh_running"] = True
    if refresh:
        try:
            _coverage(store)
        except sqlite3.Error:
            with _LOCK:
                current = _STATUS[store]
                current["state"] = "failed" if current.get("failures") else "coverage_unavailable"
        finally:
            _status(store, coverage_refresh_running=False)
        with _LOCK:
            state = dict(_STATUS[store])
    return state


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _unsupported(error):
    message = str(error).lower()
    return any(value in message for value in ("no such module: fts5", "no such tokenizer", "error in tokenizer constructor",
                                              "unrecognized tokenizer", "no such function: octet_length"))


def ensure_schema(store):
    with _LOCK:
        known = _CAPABILITY.get(store)
    if known is not None:
        return known
    from .conversation_search import _ensure_generation_schema, _operation
    try:
        with _operation(store, "search.substring_schema"), store._connection() as db:
            _ensure_generation_schema(db)
            # octet_length reads record lengths without paging overflow prose.
            db.execute("SELECT octet_length('')").fetchone()
            for statement in _TABLES.values():
                db.execute(statement)
            db.execute(_FTS)
            db.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS conversation_substring_vocabulary
                USING fts5vocab(conversation_substring_terms, row)""")
            # Never let one insertion initiate an unbounded crisis merge.
            # Explicit page-budget merges below maintain the index instead.
            db.execute("INSERT INTO conversation_substring_terms(conversation_substring_terms,rank) VALUES('automerge',0)")
            db.execute("INSERT INTO conversation_substring_terms(conversation_substring_terms,rank) VALUES('crisismerge',65536)")
            for event, references in (("insert", ("NEW",)), ("update", ("OLD", "NEW")), ("delete", ("OLD",))):
                updates = " ".join(f"""INSERT INTO conversation_substring_revisions VALUES ({ref}.rowid,1)
                    ON CONFLICT(message_rowid) DO UPDATE SET revision=revision+1;""" for ref in references)
                db.execute(f"""CREATE TRIGGER IF NOT EXISTS conversation_substring_legacy_{event}
                    AFTER {event.upper()} ON conversation_search_messages BEGIN {updates} END""")
    except sqlite3.OperationalError as error:
        if not _unsupported(error):
            raise
        with _LOCK:
            _CAPABILITY[store] = False
        _status(store, supported=False, state="unsupported", unavailable_reason="sqlite_capability")
        return False
    with _LOCK:
        _CAPABILITY[store] = True
    _status(store, supported=True, state="partial", unavailable_reason=None)
    return True


def legacy_source(rowid, revision):
    return f"legacy:{rowid}:{revision}"


def _text_rows(source, folded):
    # NUL handling varies between SQLite tokenizers. Such sources remain on
    # the exact path; they can never become false-negative candidates.
    if not isinstance(folded, str) or "\0" in folded:
        yield "ready", (source, 0)
        return
    for offset in range(0, len(folded), PART_CHARACTERS):
        yield "part", (source, offset // PART_CHARACTERS, folded[max(0, offset - 2):offset + PART_CHARACTERS])
    yield "ready", (source, 1)


def _write_batch(store, parts, completed, size):
    from .conversation_search import _measure, _operation
    keys = _json([(source, part) for source, part, _ in parts])
    payload, markers = _json(parts), _json(completed)
    with _measure(store, "substring_stage") as measurement:
        with _operation(store, "search.substring_stage"), store._connection() as db:
            if parts:
                db.execute("""INSERT OR IGNORE INTO conversation_substring_parts(source,part)
                    SELECT json_extract(value,'$[0]'),json_extract(value,'$[1]') FROM json_each(?)""", (keys,))
                db.execute("""INSERT INTO conversation_substring_terms(rowid,folded)
                    SELECT p.id,json_extract(j.value,'$[2]') FROM json_each(?) j
                    JOIN conversation_substring_parts p ON p.source=json_extract(j.value,'$[0]')
                      AND p.part=json_extract(j.value,'$[1]') WHERE p.indexed=0""", (payload,))
                db.execute("""UPDATE conversation_substring_parts SET indexed=1
                    WHERE indexed=0 AND (source,part) IN (SELECT json_extract(value,'$[0]'),json_extract(value,'$[1]')
                        FROM json_each(?))""", (keys,))
            if completed:
                db.execute("""INSERT OR IGNORE INTO conversation_substring_ready
                    SELECT json_extract(value,'$[0]'),json_extract(value,'$[1]') FROM json_each(?)""", (markers,))
        measurement.update(parts=len(parts), sources=len(completed), bytes=size,
                           max_rows=len(parts) + len(completed), max_bytes=size)


def _merge_step(store):
    from .conversation_search import _measure, _operation
    with _measure(store, "substring_merge") as measurement:
        with _operation(store, "search.substring_merge"), store._connection() as db:
            before = db.total_changes
            db.execute("INSERT INTO conversation_substring_terms(conversation_substring_terms,rank) VALUES('merge',?)",
                       (MERGE_PAGES,))
            changed = db.total_changes - before
        measurement.update(page_budget=MERGE_PAGES, worked=int(changed >= 2))


def _stage_steps(store, rows):
    parts, completed, size, batches = [], [], 4, 0
    for event in rows:
        if event is None:
            # Read batches also yield without forcing an undersized commit.
            yield
            continue
        kind, row = event
        length = len(_json(row).encode("utf-8")) + 1
        if (parts or completed) and (size + length > BATCH_BYTES or len(parts) + len(completed) >= BATCH_ROWS):
            _write_batch(store, parts, completed, size)
            batches += 1
            parts, completed, size = [], [], 4
            yield
            if batches % 4 == 0:
                _merge_step(store)
                yield
        (parts if kind == "part" else completed).append(row)
        size += length
    if parts or completed:
        _write_batch(store, parts, completed, size)
        # Most incremental saves fit in this final batch. Count them too so
        # separate small calls cannot accumulate segments with automerge off.
        batches += 1
        yield
    if batches:
        _merge_step(store)
        yield


def _document_rows(store, documents):
    for document in documents:
        source, after, tail, usable = "document:" + document, -1, "", True
        while True:
            with store.reading(), store._connection() as db:
                packed = db.execute("""SELECT json_group_array(json_array(part,folded)) FROM (
                    SELECT part,folded FROM conversation_search_document_text WHERE document=? AND part>?
                    ORDER BY part LIMIT ?)""", (document, after, READ_ROWS)).fetchone()[0]
            batch = json.loads(packed)
            yield None
            if not batch:
                break
            for part, folded in batch:
                after = part
                if "\0" in folded:
                    usable = False
                yield "part", (source, part, tail + folded)
                tail = folded[-2:]
        yield "ready", (source, int(usable))


def index_document_steps(store, documents):
    if not ensure_schema(store):
        return
    documents = list(dict.fromkeys(documents))
    for offset in range(0, len(documents), BATCH_ROWS):
        payload = _json(documents[offset:offset + BATCH_ROWS])
        with store.reading(), store._connection() as db:
            packed = db.execute("""SELECT json_group_array(d.document) FROM conversation_search_documents d
                INDEXED BY conversation_search_document_headers
                WHERE d.document IN (SELECT value FROM json_each(?)) AND d.role IS NOT NULL
                  AND NOT EXISTS (SELECT 1 FROM conversation_substring_ready r WHERE r.source='document:'||d.document)""",
                (payload,)).fetchone()[0]
        missing = json.loads(packed)
        yield
        yield from _stage_steps(store, _document_rows(store, missing))


def _generation_rows(store, generation):
    after = -1
    while True:
        with store.reading(), store._connection() as db:
            packed = db.execute("""SELECT json_group_array(position) FROM (
                SELECT position FROM conversation_search_entries e WHERE generation=? AND position>?
                  AND NOT EXISTS (SELECT 1 FROM conversation_substring_ready r
                      WHERE r.source='generation:'||e.generation||':'||e.position)
                ORDER BY position LIMIT ?)""", (generation, after, READ_ROWS)).fetchone()[0]
        positions = json.loads(packed)
        yield None
        if not positions:
            return
        for position in positions:
            after = position
            source, part_after, tail, usable = f"generation:{generation}:{position}", -1, "", True
            while True:
                with store.reading(), store._connection() as db:
                    packed = db.execute("""SELECT json_group_array(json_array(part,folded)) FROM (
                        SELECT part,folded FROM conversation_search_text_chunks
                        WHERE generation=? AND position=? AND part>? ORDER BY part LIMIT ?)""",
                        (generation, position, part_after, READ_ROWS)).fetchone()[0]
                parts = json.loads(packed)
                yield None
                if not parts:
                    break
                for part, folded in parts:
                    part_after = part
                    if "\0" in folded:
                        usable = False
                    yield "part", (source, part, tail + folded)
                    tail = folded[-2:]
            yield "ready", (source, int(usable))


def index_generation_steps(store, generation):
    if ensure_schema(store):
        yield from _stage_steps(store, _generation_rows(store, generation))


def _legacy_rows(store):
    after = 0
    while True:
        with store.reading(), store._connection() as db:
            db.execute("BEGIN")
            packed = db.execute("""SELECT json_group_array(json_array(identity,revision,bytes)) FROM (
                SELECT m.rowid AS identity,COALESCE(v.revision,0) AS revision,octet_length(m.folded) AS bytes
                FROM conversation_search_messages m LEFT JOIN conversation_substring_revisions v ON v.message_rowid=m.rowid
                WHERE m.rowid>? AND NOT EXISTS (SELECT 1 FROM conversation_substring_ready r
                    WHERE r.source='legacy:'||m.rowid||':'||COALESCE(v.revision,0))
                ORDER BY m.rowid LIMIT ?)""", (after, READ_ROWS)).fetchone()[0]
            metadata = json.loads(packed)
            selected, size = [], 0
            for identity, revision, length in metadata:
                if selected and size + length > BATCH_BYTES:
                    break
                selected.append(identity)
                size += length
            packed = db.execute("""SELECT json_group_array(json_array(m.rowid,COALESCE(v.revision,0),m.folded))
                FROM conversation_search_messages m LEFT JOIN conversation_substring_revisions v ON v.message_rowid=m.rowid
                WHERE m.rowid IN (SELECT value FROM json_each(?)) ORDER BY m.rowid""", (_json(selected),)).fetchone()[0]
        batch = json.loads(packed)
        yield None
        if not metadata:
            return
        after = selected[-1]
        for identity, revision, folded in batch:
            yield from _text_rows(legacy_source(identity, revision), folded)


def _coverage(store):
    with store.reading(), store._connection() as db:
        row = db.execute("""SELECT
            (SELECT COUNT(*) FROM conversation_search_messages),
            (SELECT COUNT(*) FROM conversation_search_messages m
                LEFT JOIN conversation_substring_revisions v ON v.message_rowid=m.rowid
                WHERE EXISTS (SELECT 1 FROM conversation_substring_ready r
                    WHERE r.source='legacy:'||m.rowid||':'||COALESCE(v.revision,0))),
            (SELECT COUNT(*) FROM conversation_search_documents INDEXED BY conversation_search_document_headers WHERE role IS NOT NULL),
            (SELECT COUNT(*) FROM conversation_search_documents d INDEXED BY conversation_search_document_headers WHERE role IS NOT NULL
                AND EXISTS (SELECT 1 FROM conversation_substring_ready r WHERE r.source='document:'||d.document)),
            (SELECT COUNT(*) FROM conversation_search_heads h JOIN conversation_search_entries e ON e.generation=h.generation),
            (SELECT COUNT(*) FROM conversation_search_heads h JOIN conversation_search_entries e ON e.generation=h.generation
                WHERE EXISTS (SELECT 1 FROM conversation_substring_ready r WHERE r.source='generation:'||e.generation||':'||e.position)),
            (SELECT COUNT(*) FROM conversation_substring_ready WHERE usable=0)""").fetchone()
    remaining = row[0] - row[1] + row[2] - row[3] + row[4] - row[5]
    with _LOCK:
        status = _STATUS.setdefault(store, {})
        running, failed = status.get("backfill_running", False), status.get("failures", 0)
        status.update(legacy_sources=row[0], legacy_indexed=row[1], document_sources=row[2], document_indexed=row[3],
                      generation_sources=row[4], generation_indexed=row[5], exact_only_sources=row[6],
                      remaining_sources=remaining, coverage_observed_monotonic=time.monotonic(),
                      state="failed" if failed else "building" if running else "partial" if remaining else "complete")


def backfill_steps(store):
    """Optional background migration; each yield releases all database scopes."""
    try:
        if not ensure_schema(store):
            return
        _status(store, backfill_running=True, state="building")
        _coverage(store)
        yield
        count = 0
        for _ in _stage_steps(store, _legacy_rows(store)):
            count += 1
            if count % 64 == 0:
                _coverage(store)
            yield
        with store.reading(), store._connection() as db:
            packed = db.execute("SELECT json_group_array(document) FROM conversation_search_documents WHERE role IS NOT NULL").fetchone()[0]
        documents = json.loads(packed)
        yield
        yield from index_document_steps(store, documents)
        with store.reading(), store._connection() as db:
            packed = db.execute("SELECT json_group_array(generation) FROM conversation_search_heads").fetchone()[0]
        generations = json.loads(packed)
        yield
        for generation in generations:
            yield from index_generation_steps(store, generation)
        _status(store, backfill_completed=True, backfill_running=False)
        _coverage(store)
    except Exception:
        with _LOCK:
            state = _STATUS.setdefault(store, {})
            state["failures"] = state.get("failures", 0) + 1
            state["state"] = "failed"
        raise
    finally:
        _status(store, backfill_running=False)


def candidates(db, terms, measurement):
    """Return candidate keys, or None for a complete unsupported/short fallback."""
    term_grams = [{term[offset:offset + 3] for offset in range(len(term) - 2)}
                  for term in terms if len(term) >= 3 and "\0" not in term]
    if not term_grams:
        measurement["short_query_fallbacks"] = 1
        return None
    names = (*_TABLES, "conversation_substring_terms", "conversation_substring_vocabulary")
    ready = db.execute("SELECT COUNT(*) FROM sqlite_master WHERE name IN (" + ",".join("?" * len(names)) + ")", names).fetchone()[0]
    if ready != len(names):
        measurement["unavailable_query_fallbacks"] = 1
        return None
    # Common suffixes such as "ion" select most of an archive. Choose the
    # least frequent necessary trigram of each keyword in this read snapshot.
    # Frequency affects selectivity only: every exact match contains every
    # chosen gram, including matches spanning indexed parts. An absent gram
    # excludes indexed sources; incomplete sources still use the exact path.
    try:
        frequencies = dict(db.execute("""SELECT term, doc FROM conversation_substring_vocabulary
            WHERE term IN (SELECT value FROM json_each(?))""", (_json(sorted(set.union(*term_grams))),)))
        grams = sorted({min(options, key=lambda gram: (frequencies.get(gram, 0), gram))
                        for options in term_grams})
        queries = _json(['"' + gram.replace('"', '""') + '"' for gram in grams])
        packed, count = db.execute("""SELECT json_group_array(source),COUNT(*) FROM (
            SELECT source FROM (SELECT DISTINCT p.source,q.value FROM json_each(?) q
                CROSS JOIN conversation_substring_terms f JOIN conversation_substring_parts p ON p.id=f.rowid
                WHERE conversation_substring_terms MATCH q.value)
            GROUP BY source HAVING COUNT(*)=?)""", (queries, len(grams))).fetchone()
    except sqlite3.OperationalError as error:
        if not _unsupported(error):
            raise
        measurement["unavailable_query_fallbacks"] = 1
        return None
    measurement.update(indexed_queries=1, candidate_sources=count, grams=len(grams))
    return packed


def message_source(*, include_text, indexed):
    """Push candidates before payload reconstruction in each visibility branch."""
    def allowed(source):
        return (f"({source} IN (SELECT value FROM json_each(:substring_candidates)) OR NOT EXISTS "
                f"(SELECT 1 FROM conversation_substring_ready r WHERE r.source={source} AND r.usable=1))") if indexed else "1"
    text = ("(SELECT group_concat(text,'') FROM (SELECT text FROM conversation_search_document_text c "
            "WHERE c.document=e.document ORDER BY part))") if include_text else "NULL"
    old_text = ("(SELECT group_concat(text,'') FROM (SELECT text FROM conversation_search_text_chunks c "
                "WHERE c.generation=e.generation AND c.position=e.position ORDER BY part))") if include_text else "NULL"
    legacy_key = "'legacy:'||m.rowid||':'||COALESCE(v.revision,0)"
    return f"""SELECT h.thread_id,e.turn_id,e.item_id,e.role,e.timestamp,{text} AS text,
        (SELECT group_concat(folded,'') FROM (SELECT folded FROM conversation_search_document_text c
            WHERE c.document=e.document ORDER BY part)) AS folded,e.position
        FROM conversation_search_heads h JOIN conversation_search_document_entries e ON e.generation=h.generation
        WHERE {allowed("'document:'||e.document")}
        UNION ALL SELECT h.thread_id,e.turn_id,e.item_id,e.role,e.timestamp,{old_text},
        (SELECT group_concat(folded,'') FROM (SELECT folded FROM conversation_search_text_chunks c
            WHERE c.generation=e.generation AND c.position=e.position ORDER BY part)),e.position
        FROM conversation_search_heads h JOIN conversation_search_entries e ON e.generation=h.generation
        WHERE {allowed("'generation:'||e.generation||':'||e.position")}
        UNION ALL SELECT m.thread_id,m.turn_id,m.item_id,m.role,m.timestamp,{"m.text" if include_text else "NULL"},m.folded,m.position
        FROM conversation_search_messages m
        {"LEFT JOIN conversation_substring_revisions v ON v.message_rowid=m.rowid" if indexed else ""}
        WHERE NOT EXISTS (SELECT 1 FROM conversation_search_heads h WHERE h.thread_id=m.thread_id)
          AND NOT EXISTS (SELECT 1 FROM conversation_search_dirty d WHERE d.thread_id=m.thread_id)
          AND EXISTS (SELECT 1 FROM conversation_search_routing r WHERE r.thread_id=m.thread_id)
          AND {allowed(legacy_key)}"""
