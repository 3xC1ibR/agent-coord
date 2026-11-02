"""Lossless history snapshots with bounded, content-addressed SQLite writes.

Small histories retain the legacy inline representation. Larger histories use
immutable item blocks, turn manifests, and a root manifest. Stage those blocks
in finite transactions before publishing the root in browser_history; a crash
can leave unused blocks but cannot expose a partially saved conversation.

Blocks are deliberately retained. Collection requires a contract covering
readers that captured an older root and writers still staging a new root.
SQLite backups include both the published roots and all referenced blocks.
"""
from __future__ import annotations

import hashlib
import copy
import json
import sqlite3
import threading
import time
from contextlib import closing
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from .store import CoordinationError


INLINE_BYTES = 256 * 1024
CHUNK_BYTES = 64 * 1024
STAGE_BYTES = 256 * 1024
STAGE_CHUNKS = 32
QUERY_CHUNKS = 128
MARKER = "__agent_coord_history__"
PREPARATION_CACHE_BYTES = 64 * 1024 * 1024
PREPARATION_CACHE_THREADS = 16
CONFIRMED_CHUNKS = 16384


class HistoryUnavailableError(ValueError, CoordinationError):
    """A referenced snapshot is missing or corrupt; never substitute emptiness."""


@dataclass
class PreparedHistory:
    serialized: str
    chunks: dict[str, bytes]
    source_bytes: int
    documents: dict = field(default_factory=dict)


@dataclass
class _Document:
    value: object
    references: list[str]
    chunks: dict[str, bytes]
    source_bytes: int


def _json(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def ensure_schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS browser_history_chunks (
        digest TEXT PRIMARY KEY, payload BLOB NOT NULL
    ) WITHOUT ROWID""")


def prepare_history(history, *, previous=None):
    """Serialize and hash outside every database write transaction."""
    chunks, documents = {}, {}
    previous_documents = previous.documents if previous else {}

    def reference(value, key):
        existing = previous_documents.get(key)
        if existing is not None and existing.value == value:
            documents[key] = existing
            chunks.update(existing.chunks)
            return existing.references, existing.source_bytes
        content = _json(value).encode("utf-8")
        result, blocks = [], {}
        for offset in range(0, len(content), CHUNK_BYTES):
            payload = content[offset:offset + CHUNK_BYTES]
            digest = hashlib.sha256(payload).hexdigest()
            blocks.setdefault(digest, payload)
            result.append(digest)
        chunks.update(blocks)
        documents[key] = _Document(copy.deepcopy(value), result, blocks, len(content))
        return result, len(content)

    def array_size(sizes):
        return 2 + max(0, len(sizes) - 1) + sum(sizes)

    def object_size(metadata, fields):
        # Exact canonical JSON byte length without serializing unchanged items
        # again. ensure_ascii makes character and UTF-8 byte lengths identical.
        return (len(_json(metadata)) + sum(len(_json(key)) + 1 + size for key, size in fields.items())
                + (len(fields) if metadata else max(0, len(fields) - 1)))

    # Separate item boundaries keep changes in one item from shifting all
    # subsequent history blocks. Splitting large items also reuses their prefix.
    turns = history.get("turns") if isinstance(history, dict) else None
    structured = isinstance(turns, list) and all(
        isinstance(turn, dict) and isinstance(turn.get("items", []), list) for turn in turns)
    if structured:
        encoded_turns, turn_sizes = [], []
        for turn_index, turn in enumerate(turns):
            encoded = {"metadata": {key: value for key, value in turn.items() if key != "items"}}
            sizes = {}
            if "items" in turn:
                items = [reference(item, (turn_index, item_index)) for item_index, item in enumerate(turn["items"])]
                encoded["items"] = [item[0] for item in items]
                sizes["items"] = array_size([item[1] for item in items])
            turn_sizes.append(object_size(encoded["metadata"], sizes))
            encoded_turns.append(reference(encoded, (turn_index,))[0])
        metadata = {key: value for key, value in history.items() if key != "turns"}
        source_bytes = object_size(metadata, {"turns": array_size(turn_sizes)})
        root = reference({"metadata": metadata, "turns": encoded_turns}, ())[0]
    else:
        root, source_bytes = reference(history, ())
    if source_bytes <= INLINE_BYTES:
        return PreparedHistory(_json(history), {}, source_bytes, documents)
    return PreparedHistory(_json({MARKER: 1, "layout": "turns" if structured else "raw", "root": root}),
                           chunks, source_bytes, documents)


def _documents(db, references):
    digests = set()
    for reference in references:
        if not isinstance(reference, list) or not reference or any(
                not isinstance(value, str) or len(value) != 64 or
                any(character not in "0123456789abcdef" for character in value) for value in reference):
            raise HistoryUnavailableError("Saved conversation has an invalid block reference.")
        digests.update(reference)
    digests = sorted(digests)
    blocks = {}
    for offset in range(0, len(digests), QUERY_CHUNKS):
        batch = digests[offset:offset + QUERY_CHUNKS]
        try:
            rows = db.execute("SELECT digest, payload FROM browser_history_chunks WHERE digest IN ("
                              + ",".join("?" for _ in batch) + ")", batch).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc):
                raise
            raise HistoryUnavailableError("Saved conversation blocks are unavailable.") from exc
        for digest, payload in rows:
            if not isinstance(payload, bytes) or hashlib.sha256(payload).hexdigest() != digest:
                raise HistoryUnavailableError("Saved conversation block failed its integrity check.")
            blocks[digest] = payload
    if len(blocks) != len(digests):
        raise HistoryUnavailableError("Saved conversation is missing one or more blocks.")
    try:
        return [json.loads(b"".join(blocks[digest] for digest in reference)) for reference in references]
    except (ValueError, UnicodeError) as exc:
        raise HistoryUnavailableError("Saved conversation contains an invalid block document.") from exc


def _decode(db, manifest):
    own_snapshot = not db.in_transaction
    if own_snapshot:
        db.execute("BEGIN")
    try:
        if manifest.get(MARKER) != 1 or manifest.get("layout") not in {"raw", "turns"}:
            raise HistoryUnavailableError("Saved conversation requires an unsupported history format.")
        root = _documents(db, [manifest.get("root")])[0]
        if manifest["layout"] == "raw":
            return root
        history = root["metadata"]
        turns = _documents(db, root["turns"])
        references = [item for turn in turns for item in turn.get("items", [])]
        items = iter(_documents(db, references))
        decoded = []
        for turn in turns:
            value = turn["metadata"]
            if "items" in turn:
                value["items"] = [next(items) for _ in turn["items"]]
            decoded.append(value)
        history["turns"] = decoded
        return history
    except (KeyError, TypeError, StopIteration) as exc:
        raise HistoryUnavailableError("Saved conversation has an invalid manifest.") from exc
    finally:
        if own_snapshot:
            db.rollback()


def decode_history(serialized, *, database_path=None, db=None):
    """Decode legacy JSON or a manifest, using one read snapshot for its blocks.

    ``db`` supports callers already holding a coherent snapshot, including
    in-memory databases. Otherwise the codec opens one query-only connection.
    No provider is contacted and no schema or history is mutated.
    """
    value = json.loads(serialized)
    if not isinstance(value, dict) or MARKER not in value:
        return value
    if db is not None:
        return _decode(db, value)
    if database_path is None:
        raise HistoryUnavailableError("A database is required to read saved conversation blocks.")
    path = Path(database_path).expanduser().resolve()
    def read(mode):
        with closing(sqlite3.connect(path.as_uri() + "?mode=" + mode, uri=True)) as connection:
            connection.execute("PRAGMA query_only=ON")
            return _decode(connection, value)
    try:
        return read("ro")
    except sqlite3.OperationalError as error:
        code = getattr(error, "sqlite_errorcode", None)
        if code != sqlite3.SQLITE_CANTOPEN and not (code is None and str(error) == "unable to open database file"):
            raise
    # A restored SQLite backup can retain WAL mode without WAL coordination
    # files. Allow SQLite to initialize those files, but prevent every logical
    # write before reading. mode=rw still refuses to create a missing database.
    return read("rw")


def read_history(store, thread_id):
    """Read the root and its complete history in one snapshot, without a writer."""
    with store.reading(), store._connection() as db:
        db.execute("BEGIN")
        row = db.execute("SELECT history_json FROM browser_history WHERE thread_id=?", (thread_id,)).fetchone()
        return decode_history(row[0], db=db) if row else None


def stage_history(store, prepared, *, on_commit=None, confirmed=()):
    """Stage only absent blocks, with a byte bound on every write transaction."""
    present = prepared.chunks.keys() & set(confirmed)
    digests = [digest for digest in prepared.chunks if digest not in present]
    if digests:
        with store.reading(), store._connection() as db:
            for offset in range(0, len(digests), QUERY_CHUNKS):
                batch = digests[offset:offset + QUERY_CHUNKS]
                present.update(row[0] for row in db.execute(
                    "SELECT digest FROM browser_history_chunks WHERE digest IN ("
                    + ",".join("?" for _ in batch) + ")", batch))
    missing = [(digest, prepared.chunks[digest]) for digest in digests if digest not in present]
    batches, batch, size = [], [], 0
    for entry in missing:
        if batch and (size + len(entry[1]) > STAGE_BYTES or len(batch) >= STAGE_CHUNKS):
            batches.append(batch)
            batch, size = [], 0
        batch.append(entry)
        size += len(entry[1])
    if batch:
        batches.append(batch)
    written_bytes = written_chunks = 0
    for batch in batches:
        batch_bytes = batch_chunks = 0
        with store.database_operation("history.stage", priority="background"), store._connection() as db:
            for digest, payload in batch:
                changed = db.execute("INSERT OR IGNORE INTO browser_history_chunks VALUES (?, ?)",
                                     (digest, payload)).rowcount
                batch_chunks += changed
                batch_bytes += len(payload) * changed
        written_chunks += batch_chunks
        written_bytes += batch_bytes
        if on_commit is not None:
            on_commit({"staged_chunks": batch_chunks, "staged_bytes": batch_bytes, "staging_transactions": 1})
    return {"staged_chunks": written_chunks, "staged_bytes": written_bytes,
            "reused_chunks": len(prepared.chunks) - written_chunks,
            "reused_bytes": sum(map(len, prepared.chunks.values())) - written_bytes,
            "staging_transactions": len(batches)}


def publish_history(db, thread_id, prepared):
    """Publish after staging; an unchanged root does not invalidate search."""
    # Keep the outer statement free of an ON CONFLICT policy: SQLite can
    # otherwise override the conflict policy of older invalidation triggers.
    changed = db.execute("""UPDATE browser_history SET history_json=?
        WHERE thread_id=? AND history_json != ?""",
        (prepared.serialized, thread_id, prepared.serialized)).rowcount
    if changed:
        return True
    if db.execute("SELECT 1 FROM browser_history WHERE thread_id=?", (thread_id,)).fetchone():
        return False
    db.execute("INSERT INTO browser_history (thread_id, history_json) VALUES (?, ?)",
               (thread_id, prepared.serialized))
    return True


class HistoryPersistence:
    def __init__(self, store):
        self.store = store
        self.lock = threading.Lock()
        self.prepared = OrderedDict()
        self.confirmed = OrderedDict()
        self.prepared_bytes = 0
        self.metrics = {"snapshots": 0, "unchanged_snapshots": 0, "source_bytes": 0,
                        "staged_chunks": 0, "staged_bytes": 0, "reused_chunks": 0, "reused_bytes": 0,
                        "staging_transactions": 0, "published_bytes": 0, "elapsed_seconds": 0.0, "failures": 0}

    def _record(self, metrics):
        with self.lock:
            for key, value in metrics.items():
                self.metrics[key] += value

    def save(self, thread_id, history):
        started = time.monotonic()
        try:
            with self.lock:
                previous = self.prepared.get(thread_id)
            prepared = prepare_history(history, previous=previous)
            with self.lock:
                previous = self.prepared.pop(thread_id, None)
                self.prepared_bytes -= previous.source_bytes if previous else 0
                if prepared.source_bytes <= PREPARATION_CACHE_BYTES:
                    self.prepared[thread_id] = prepared
                    self.prepared_bytes += prepared.source_bytes
                while (self.prepared_bytes > PREPARATION_CACHE_BYTES
                       or len(self.prepared) > PREPARATION_CACHE_THREADS):
                    _, expired = self.prepared.popitem(last=False)
                    self.prepared_bytes -= expired.source_bytes
                confirmed = set(self.confirmed)
            metrics = stage_history(self.store, prepared, on_commit=self._record, confirmed=confirmed)
            # Only a complete staging pass confirms presence. Failed batches
            # never enter this cache; failed root publication is safe because
            # the immutable blocks have already committed independently.
            with self.lock:
                for digest in prepared.chunks:
                    self.confirmed.pop(digest, None)
                    self.confirmed[digest] = None
                while len(self.confirmed) > CONFIRMED_CHUNKS:
                    self.confirmed.popitem(last=False)
            self._record({"reused_chunks": metrics["reused_chunks"], "reused_bytes": metrics["reused_bytes"]})
            with self.store.database_operation("history.publish", priority="background"), self.store._connection() as db:
                changed = publish_history(db, thread_id, prepared)
            published = {"snapshots": 1, "unchanged_snapshots": int(not changed),
                         "source_bytes": prepared.source_bytes,
                         "published_bytes": len(prepared.serialized.encode("utf-8")) if changed else 0}
            self._record(published)
            return {**metrics, **published, "elapsed_seconds": time.monotonic() - started}
        except Exception:
            self._record({"failures": 1})
            raise
        finally:
            self._record({"elapsed_seconds": time.monotonic() - started})

    def status(self):
        with self.lock:
            return {**self.metrics, "preparation_cache_bytes": self.prepared_bytes,
                    "preparation_cache_threads": len(self.prepared), "confirmed_chunks": len(self.confirmed)}

    def forget(self, thread_id):
        with self.lock:
            prepared = self.prepared.pop(thread_id, None)
            self.prepared_bytes -= prepared.source_bytes if prepared else 0
