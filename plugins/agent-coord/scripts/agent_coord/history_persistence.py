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
import logging
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from .store import CoordinationError
from .sqlite_compatibility import SQLITE_CANTOPEN
from .history_window import RECENT_TURNS, recent_view


INLINE_BYTES = 256 * 1024
CHUNK_BYTES = 64 * 1024
STAGE_BYTES = 256 * 1024
STAGE_CHUNKS = 32
QUERY_CHUNKS = 128
READ_CHUNKS = 32
_READ_AHEAD_SLOT = threading.BoundedSemaphore(1)
_READ_AHEAD_WORKERS = 4
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
    durable_chunks: dict[str, int] = field(default_factory=dict)
    cache_bytes: int = 0
    encoded_document_bytes: int = 0


@dataclass
class _Document:
    value: object
    references: list[str]
    chunks: dict[str, bytes]
    source_bytes: int
    fingerprint: bytes | None
    committed: bool = False

    def content_digest(self):
        if self.fingerprint is not None:
            return self.fingerprint
        digest = hashlib.sha256()
        for reference in self.references:
            digest.update(self.chunks[reference])
        return digest.digest()


def _json(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _extend_item_text(value, previous):
    """Reuse canonical JSON blocks before an append to a single large text field."""
    if previous.committed or not isinstance(value, dict) or not isinstance(previous.value, dict):
        return None
    for field in ("text", "aggregatedOutput"):
        before, after = previous.value.get(field), value.get(field)
        if (not isinstance(before, str) or len(before) < CHUNK_BYTES or not isinstance(after, str)
                or len(after) <= len(before) or not after.startswith(before)):
            continue
        old_metadata = _json({k: v for k, v in previous.value.items() if k != field})
        metadata = _json({k: v for k, v in value.items() if k != field})
        if old_metadata != metadata:
            return None
        following = {k: v for k, v in value.items() if k > field}
        suffix = ('",' + _json(following)[1:] if following else '"}').encode()
        complete, partial = divmod(previous.source_bytes - len(suffix), CHUNK_BYTES)
        references = previous.references[:complete]
        blocks = {digest: previous.chunks[digest] for digest in references}
        tail = previous.chunks[previous.references[complete]][:partial] if partial else b""
        addition = _json(after[len(before):]).encode()[1:-1]
        remaining = tail + addition + suffix
        for offset in range(0, len(remaining), CHUNK_BYTES):
            block = remaining[offset:offset + CHUNK_BYTES]
            digest = hashlib.sha256(block).hexdigest()
            references.append(digest)
            blocks[digest] = block
        document = _Document(copy.deepcopy(value), references, blocks, previous.source_bytes + len(addition), None)
        return document, len(old_metadata) + len(metadata) + len(suffix) + len(addition)
    return None


def ensure_schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS browser_history_chunks (
        digest TEXT PRIMARY KEY, payload BLOB NOT NULL
    ) WITHOUT ROWID""")
    # The primary B-tree also contains payloads. A compact covering index keeps
    # existence checks from paging transcript contents into SQLite's cache.
    db.execute("CREATE INDEX IF NOT EXISTS browser_history_chunk_digests ON browser_history_chunks(digest)")


def _finish_steps(steps):
    while True:
        try:
            next(steps)
        except StopIteration as finished:
            return finished.value


def prepare_history(history, *, previous=None):
    """Serialize and hash outside every database write transaction."""
    return _finish_steps(prepare_history_steps(history, previous=previous))


def prepare_projected_history(history, *, previous=None):
    """An inline coordinator checkpoint has no chunk documents to stage or index."""
    if previous is None or previous.source_bytes <= INLINE_BYTES:
        # A provider can append megabytes to a formerly small history in one
        # event. Bound the probe before asking the JSON encoder for a complete
        # string. The final canonical length decides the storage format.
        pending, remaining = [history], INLINE_BYTES
        while pending and remaining >= 0:
            value = pending.pop()
            if isinstance(value, str):
                remaining -= len(value)
            elif isinstance(value, (dict, list, tuple)):
                remaining -= len(value)
                if remaining >= 0:
                    if isinstance(value, dict):
                        pending.extend(value.keys())
                        pending.extend(value.values())
                    else:
                        pending.extend(value)
            elif isinstance(value, int):
                remaining -= value.bit_length() // 4
        if remaining >= 0:
            serialized = _json(history)
            if len(serialized) <= INLINE_BYTES:
                return PreparedHistory(serialized, {}, len(serialized), encoded_document_bytes=len(serialized))
    return prepare_history(history, previous=previous)


def prepare_history_steps(history, *, previous=None):
    """Yield between documents so one archive does not monopolize saving."""
    chunks, documents, durable_chunks = {}, {}, {}
    previous_documents = previous.documents if previous else {}
    deadline = time.monotonic() + .002
    encoded_document_bytes = 0

    def reference(value, key):
        nonlocal encoded_document_bytes
        existing = previous_documents.get(key)
        content = None
        if existing is not None and existing.committed:
            content = _json(value).encode("utf-8")
            encoded_document_bytes += len(content)
            unchanged = hashlib.sha256(content).digest() == existing.fingerprint
        else:
            unchanged = existing is not None and existing.value == value
        if unchanged:
            documents[key] = existing
            chunks.update(existing.chunks)
            if existing.committed:
                for index, digest in enumerate(existing.references):
                    durable_chunks[digest] = min(CHUNK_BYTES, existing.source_bytes - index * CHUNK_BYTES)
            return existing.references, existing.source_bytes
        if existing is not None and len(key) == 2:
            extended = _extend_item_text(value, existing)
            if extended is not None:
                document, encoded_bytes = extended
                encoded_document_bytes += encoded_bytes
                documents[key] = document
                chunks.update(document.chunks)
                return document.references, document.source_bytes
        if content is None:
            content = _json(value).encode("utf-8")
            encoded_document_bytes += len(content)
        result, blocks = [], {}
        for index, offset in enumerate(range(0, len(content), CHUNK_BYTES)):
            # A streamed output usually changes only its final block. Reuse a
            # digest only after comparing the actual bytes at the same offset.
            payload = None
            if existing is not None and index < len(existing.references):
                digest = existing.references[index]
                cached = existing.chunks.get(digest)
                if (cached is not None and len(cached) == min(CHUNK_BYTES, len(content) - offset)
                        and content.startswith(cached, offset)):
                    payload = cached
            if payload is None:
                payload = content[offset:offset + CHUNK_BYTES]
                digest = hashlib.sha256(payload).hexdigest()
            blocks.setdefault(digest, payload)
            result.append(digest)
        chunks.update(blocks)
        documents[key] = _Document(copy.deepcopy(value), result, blocks, len(content), hashlib.sha256(content).digest())
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
                items = []
                for item_index, item in enumerate(turn["items"]):
                    items.append(reference(item, (turn_index, item_index)))
                    if time.monotonic() >= deadline:
                        yield
                        deadline = time.monotonic() + .002
                encoded["items"] = [item[0] for item in items]
                sizes["items"] = array_size([item[1] for item in items])
            turn_sizes.append(object_size(encoded["metadata"], sizes))
            encoded_turns.append(reference(encoded, (turn_index,))[0])
            if time.monotonic() >= deadline:
                yield
                deadline = time.monotonic() + .002
        metadata = {key: value for key, value in history.items() if key != "turns"}
        source_bytes = object_size(metadata, {"turns": array_size(turn_sizes)})
        root = reference({"metadata": metadata, "turns": encoded_turns}, ())[0]
    else:
        root, source_bytes = reference(history, ())
    if source_bytes <= INLINE_BYTES:
        return PreparedHistory(_json(history), {}, source_bytes, documents, durable_chunks,
                               encoded_document_bytes=encoded_document_bytes)
    return PreparedHistory(_json({MARKER: 1, "layout": "turns" if structured else "raw", "root": root}),
                           chunks, source_bytes, documents, durable_chunks,
                           encoded_document_bytes=encoded_document_bytes)


def compact_history(prepared):
    """Keep only fingerprints and references after every block has committed."""
    if prepared.source_bytes <= INLINE_BYTES:
        # Inline documents' blocks have not been staged. Retain their ordinary
        # preparation so a later transition to chunked history writes them.
        return PreparedHistory(prepared.serialized, {}, prepared.source_bytes, prepared.documents,
                               cache_bytes=2 * prepared.source_bytes + 1024 * len(prepared.documents))
    documents = {key: _Document(None, value.references, {}, value.source_bytes, value.content_digest(), True)
                 for key, value in prepared.documents.items()}
    # Charge conservative per-document/container and digest-reference overhead;
    # logical transcript bytes remain a separate workload metric.
    retained = len(prepared.serialized) + sum(640 + 128 * len(value.references) for value in documents.values())
    return PreparedHistory(prepared.serialized, {}, prepared.source_bytes, documents, cache_bytes=retained)


def _prefetch_blocks(uri, digests):
    """Fault selected payload pages into the OS cache. Never return data to the reader."""
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=.05)) as db:
            for offset in range(0, len(digests), QUERY_CHUNKS):
                batch = digests[offset:offset + QUERY_CHUNKS]
                placeholders = ",".join("?" for _ in batch)
                db.execute(f"SELECT sum(length(substr(payload, -1, 1))) FROM browser_history_chunks "
                           f"WHERE digest IN ({placeholders})", batch).fetchone()
    except sqlite3.Error:
        # Contention or a missing block is handled by the authoritative snapshot read below.
        pass


def _documents(db, references, *, read_ahead=None):
    digests, multiblock = set(), set()
    for reference in references:
        if not isinstance(reference, list) or not reference or any(
                not isinstance(value, str) or len(value) != 64 or
                any(character not in "0123456789abcdef" for character in value) for value in reference):
            raise HistoryUnavailableError("Saved conversation has an invalid block reference.")
        digests.update(reference)
        if len(reference) > 1:
            multiblock.update(reference)
    if read_ahead is not None and len(digests) >= READ_CHUNKS:
        read_ahead(sorted(digests)[:_READ_AHEAD_WORKERS * QUERY_CHUNKS])
    blocks = {}
    try:
        for binary, group in ((True, multiblock), (False, digests - multiblock)):
            ordered = sorted(group)
            for offset in range(0, len(ordered), READ_CHUNKS):
                batch = ordered[offset:offset + READ_CHUNKS]
                source = " FROM browser_history_chunks WHERE digest IN (" + ",".join("?" for _ in batch) + ")"
                if binary:
                    # Full blocks already occupy 64 KiB. Returning their bytes avoids
                    # doubling a streamed message into hexadecimal and JSON strings.
                    rows = db.execute("SELECT digest, CASE typeof(payload) WHEN 'blob' THEN payload END" + source, batch)
                else:
                    # A single native aggregate avoids a GIL handoff for every tiny item.
                    packed = db.execute("SELECT json_group_array(json_array(digest, typeof(payload), hex(payload)))"
                                        + source, batch).fetchone()[0]
                    rows = ((digest, bytes.fromhex(encoded) if kind == "blob" else None)
                            for digest, kind, encoded in json.loads(packed))
                for digest, payload in rows:
                    if payload is None or hashlib.sha256(payload).hexdigest() != digest:
                        raise HistoryUnavailableError("Saved conversation block failed its integrity check.")
                    blocks[digest] = payload
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        raise HistoryUnavailableError("Saved conversation blocks are unavailable.") from exc
    if len(blocks) != len(digests):
        raise HistoryUnavailableError("Saved conversation is missing one or more blocks.")
    try:
        return [json.loads(b"".join(blocks[digest] for digest in reference)) for reference in references]
    except (ValueError, UnicodeError) as exc:
        raise HistoryUnavailableError("Saved conversation contains an invalid block document.") from exc


def prepared_history_view(prepared, serialized):
    """Borrow immutable item values only for the exact published archive root."""
    if prepared is None or prepared.serialized != serialized:
        return None
    documents = prepared.documents
    root = documents.get(())
    if root is None or not isinstance(root.value, dict):
        return None
    if not isinstance(root.value.get("metadata"), dict) or not isinstance(root.value.get("turns"), list):
        return None
    turns = []
    for index in range(len(root.value["turns"])):
        document = documents.get((index,))
        if document is None or document.value is None:
            return None
        saved = document.value
        turn = dict(saved["metadata"])
        if "items" in saved:
            items = []
            for position in range(len(saved["items"])):
                item = documents.get((index, position))
                if item is None or item.value is None:
                    return None
                items.append(item.value)
            turn["items"] = items
        turns.append(turn)
    return {**root.value["metadata"], "turns": turns}


def _materialize_manifest(manifest, read_documents, *, recent=False, live_turn=None):
    try:
        if manifest.get(MARKER) != 1 or manifest.get("layout") not in {"raw", "turns"}:
            raise HistoryUnavailableError("Saved conversation requires an unsupported history format.")
        root = read_documents([manifest.get("root")])[0]
        if manifest["layout"] == "raw":
            return recent_view(root, live_turn=live_turn) if recent else root
        history = root["metadata"]
        turn_refs = root["turns"]
        selected = turn_refs[-RECENT_TURNS:] if recent else turn_refs
        turns = read_documents(selected)
        references = [item for turn in turns for item in turn.get("items", [])]
        items = iter(read_documents(references))
        decoded = []
        for turn in turns:
            value = turn["metadata"]
            if "items" in turn:
                value["items"] = [next(items) for _ in turn["items"]]
            decoded.append(value)
        history["turns"] = decoded
        if recent:
            window = recent_view(history, live_turn=live_turn)
            window["earlierTurns"] += len(turn_refs) - len(selected)
            return window
        return history
    except (KeyError, TypeError, StopIteration) as exc:
        raise HistoryUnavailableError("Saved conversation has an invalid manifest.") from exc


def restore_projected_history(prepared):
    """Recover the pre-event mirror from its immutable, complete in-memory checkpoint."""
    manifest = json.loads(prepared.serialized)
    if not prepared.chunks:
        return manifest

    def read_documents(references):
        return [json.loads(b"".join(prepared.chunks[digest] for digest in reference)) for reference in references]

    return _materialize_manifest(manifest, read_documents)


def _decode(db, manifest, *, recent=False, live_turn=None, read_ahead=None):
    own_snapshot = not db.in_transaction
    if own_snapshot:
        db.execute("BEGIN")
    try:
        return _materialize_manifest(manifest, lambda refs: _documents(db, refs, read_ahead=read_ahead),
                                     recent=recent, live_turn=live_turn)
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
        if code != SQLITE_CANTOPEN and not (code is None and str(error) == "unable to open database file"):
            raise
    # A restored SQLite backup can retain WAL mode without WAL coordination
    # files. Allow SQLite to initialize those files, but prevent every logical
    # write before reading. mode=rw still refuses to create a missing database.
    return read("rw")


def saved_turn_status(store, thread_id, turn_id):
    """Look up an outcome in one published root without loading archived item bodies."""
    with store.reading(), store._connection() as db:
        db.execute("BEGIN")
        row = db.execute("SELECT history_json FROM browser_history WHERE thread_id=?", (thread_id,)).fetchone()
        if not row:
            return None
        root = json.loads(row[0])
        if MARKER not in root:
            turns = root.get("turns", [])
        elif root.get(MARKER) == 1 and root.get("layout") == "turns":
            try:
                references = _documents(db, [root["root"]])[0]["turns"]
                for end in range(len(references), 0, -READ_CHUNKS):
                    batch = references[max(0, end - READ_CHUNKS):end]
                    for turn in reversed(_documents(db, batch)):
                        if turn["metadata"]["id"] == turn_id:
                            return turn["metadata"].get("status")
                return None
            except (KeyError, TypeError) as exc:
                raise HistoryUnavailableError("Saved conversation has an invalid turn manifest.") from exc
        else:
            turns = _decode(db, root).get("turns", [])
        return next((turn.get("status") for turn in reversed(turns) if turn.get("id") == turn_id), None)


def read_history(store, thread_id, *, windows=None, live_turn=None):
    """Read the published history or its recent view in a single query-only snapshot."""
    with store.reading(), store._connection() as db:
        db.execute("BEGIN")
        row = db.execute("SELECT CAST(history_json AS BLOB) FROM browser_history WHERE thread_id=?", (thread_id,)).fetchone()
        if not row:
            return None
        if windows is not None:
            return windows.read(row[0], db, live_turn, thread_id=thread_id)
        return decode_history(row[0], db=db)


class SavedHistoryWindows:
    """Read recent windows and reuse bounded views of large legacy inline histories."""

    def __init__(self):
        self.windows = OrderedDict()
        self.warmed_threads = OrderedDict()
        self.lock = threading.Lock()

    def _recent_manifest(self, thread, db, live_turn, thread_id):
        with self.lock:
            warm = thread_id in self.warmed_threads
            if warm:
                self.warmed_threads.move_to_end(thread_id)
        if warm or thread.get("layout") != "turns" or not _READ_AHEAD_SLOT.acquire(blocking=False):
            return _decode(db, thread, recent=True, live_turn=live_turn)
        try:
            path = db.execute("PRAGMA database_list").fetchone()[2]
            if not path:
                return _decode(db, thread, recent=True, live_turn=live_turn)
            uri = Path(path).as_uri() + "?mode=ro"
            with ThreadPoolExecutor(max_workers=_READ_AHEAD_WORKERS, thread_name_prefix="history-read") as pool:
                def read_ahead(digests):
                    futures = [pool.submit(_prefetch_blocks, uri, digests[i::_READ_AHEAD_WORKERS])
                               for i in range(_READ_AHEAD_WORKERS)]
                    for future in futures:
                        future.result()
                window = _decode(db, thread, recent=True, live_turn=live_turn, read_ahead=read_ahead)
            with self.lock:
                self.warmed_threads[thread_id] = None
                if len(self.warmed_threads) > 4:
                    self.warmed_threads.popitem(last=False)
            return window
        finally:
            _READ_AHEAD_SLOT.release()

    def read(self, raw, db, live_turn, *, thread_id=None):
        key = (hashlib.sha256(raw).digest(), live_turn) if len(raw) > INLINE_BYTES else None
        if key:
            with self.lock:
                cached = self.windows.get(key)
                if cached is not None:
                    self.windows.move_to_end(key)
            if cached is not None:
                return json.loads(cached)
        thread = json.loads(raw)
        if MARKER in thread:
            # The verified root supplies the full turn count. Read and
            # verify the displayed turn and item blocks in this same snapshot.
            return self._recent_manifest(thread, db, live_turn, thread_id)
        window = recent_view(thread, live_turn=live_turn)
        if key:
            encoded = json.dumps(window, separators=(",", ":")).encode()
            if len(encoded) <= 4 * 1024 * 1024:
                with self.lock:
                    self.windows[key] = encoded
                    self.windows.move_to_end(key)
                    if len(self.windows) > 4:
                        self.windows.popitem(last=False)
        return window


def stage_history(store, prepared, *, on_commit=None, confirmed=()):
    """Stage only absent blocks, with a byte bound on every write transaction."""
    return _finish_steps(stage_history_steps(store, prepared, on_commit=on_commit, confirmed=confirmed))


def stage_history_steps(store, prepared, *, on_commit=None, confirmed=()):
    """Yield after every bounded read or write, with no connection held."""
    present = prepared.chunks.keys() & (set(confirmed) | prepared.durable_chunks.keys())
    digests = [digest for digest in prepared.chunks if digest not in present]
    for offset in range(0, len(digests), QUERY_CHUNKS):
        batch = digests[offset:offset + QUERY_CHUNKS]
        with store.database_operation("history.present", priority="background"), store.reading(), store._connection() as db:
            packed = db.execute(
                "SELECT json_group_array(digest) FROM browser_history_chunks INDEXED BY browser_history_chunk_digests WHERE digest IN ("
                + ",".join("?" for _ in batch) + ")", batch).fetchone()[0]
        present.update(json.loads(packed))
        yield
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
        yield
    total_sizes = {**prepared.durable_chunks, **{key: len(value) for key, value in prepared.chunks.items()}}
    return {"staged_chunks": written_chunks, "staged_bytes": written_bytes,
            "reused_chunks": len(total_sizes) - written_chunks,
            "reused_bytes": sum(total_sizes.values()) - written_bytes,
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
    def __init__(self, store, *, on_saved=None):
        self.store = store
        self.on_saved = on_saved
        self.lock = threading.Lock()
        self.prepared = OrderedDict()
        self.confirmed = OrderedDict()
        self.prepared_bytes = 0
        self.metrics = {"snapshots": 0, "unchanged_snapshots": 0, "source_bytes": 0,
                        "staged_chunks": 0, "staged_bytes": 0, "reused_chunks": 0, "reused_bytes": 0,
                        "staging_transactions": 0, "published_bytes": 0, "encoded_document_bytes": 0,
                        "elapsed_seconds": 0.0, "failures": 0, "search_seed_failures": 0}

    def _record(self, metrics):
        with self.lock:
            for key, value in metrics.items():
                self.metrics[key] += value

    def save(self, thread_id, history):
        return _finish_steps(self.save_steps(thread_id, history))

    def save_steps(self, thread_id, history):
        started = time.monotonic()
        try:
            with self.lock:
                previous = self.prepared.get(thread_id)
            with self.store._writer.measure_request("history.prepare"):
                prepared = yield from prepare_history_steps(history, previous=previous)
            self._record({"encoded_document_bytes": prepared.encoded_document_bytes})
            with self.lock:
                confirmed = set(self.confirmed)
            with self.store._writer.measure_request("history.stage"):
                metrics = yield from stage_history_steps(self.store, prepared, on_commit=self._record, confirmed=confirmed)
            # Only a complete staging pass confirms presence. Failed batches
            # never enter this cache; failed root publication is safe because
            # the immutable blocks have already committed independently.
            with self.lock:
                # Never replace another save's newer generation, and never
                # expose references until the complete staging pass commits.
                if self.prepared.get(thread_id) is previous:
                    compact = compact_history(prepared)
                    self.prepared.pop(thread_id, None)
                    self.prepared_bytes -= previous.cache_bytes if previous else 0
                    if compact.cache_bytes <= PREPARATION_CACHE_BYTES:
                        self.prepared[thread_id] = compact
                        self.prepared_bytes += compact.cache_bytes
                    while (self.prepared_bytes > PREPARATION_CACHE_BYTES
                           or len(self.prepared) > PREPARATION_CACHE_THREADS):
                        _, expired = self.prepared.popitem(last=False)
                        self.prepared_bytes -= expired.cache_bytes
                for digest in prepared.chunks:
                    self.confirmed.pop(digest, None)
                    self.confirmed[digest] = None
                while len(self.confirmed) > CONFIRMED_CHUNKS:
                    self.confirmed.popitem(last=False)
            self._record({"reused_chunks": metrics["reused_chunks"], "reused_bytes": metrics["reused_bytes"]})
            from .attention import completed_summary, save_summary
            summary = completed_summary(history)
            with self.store.database_operation("history.publish", priority="background"), self.store._connection() as db:
                changed = publish_history(db, thread_id, prepared)
                if summary:
                    save_summary(db, thread_id, *summary)
            published = {"snapshots": 1, "unchanged_snapshots": int(not changed),
                         "source_bytes": prepared.source_bytes,
                         "published_bytes": len(prepared.serialized.encode("utf-8")) if changed else 0}
            self._record(published)
            if self.on_saved is not None:
                try:
                    self.on_saved(thread_id, prepared)
                except Exception:
                    # Search is a derived projection. Its fallback can rebuild
                    # from this committed root; it must not reject a saved turn.
                    self._record({"search_seed_failures": 1})
                    logging.getLogger(__name__).exception("Could not schedule saved-history search preparation")
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
            self.prepared_bytes -= prepared.cache_bytes if prepared else 0
