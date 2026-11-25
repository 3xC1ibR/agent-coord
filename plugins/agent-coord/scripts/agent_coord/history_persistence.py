"""Transcript persistence compatibility facade over TranscriptStore.

 Stage 3 stores finalized conversation turns in sharded offset-indexed JSONL
files (``turns.compact.jsonl`` and ``turns.full.jsonl``) indexed by
``thread_turns`` in ``state.sqlite3``. Legacy ``browser_history`` and
``browser_history_chunks`` decoding remains available strictly as a read-only
fallback for unmigrated databases and ``scripts/migrate_transcript_storage.py``.
"""
from __future__ import annotations

from collections import OrderedDict
from contextlib import closing
from dataclasses import dataclass, field
import copy
import hashlib
import json
import logging
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any

from .history_window import RECENT_TURNS, recent_view
from .sqlite_compatibility import SQLITE_CANTOPEN
from .store import CoordinationError
from .transcript_store import TranscriptStore, ensure_schema as ensure_transcript_schema

INLINE_BYTES = 256 * 1024
CHUNK_BYTES = 64 * 1024
STAGE_BYTES = 256 * 1024
READ_CHUNKS = 32
MARKER = "__agent_coord_history__"


class HistoryUnavailableError(ValueError, CoordinationError):
    """A referenced snapshot is missing or corrupt; never substitute emptiness."""


@dataclass
class PreparedHistory:
    serialized: str
    chunks: dict[str, bytes] = field(default_factory=dict)
    source_bytes: int = 0
    documents: dict = field(default_factory=dict)
    durable_chunks: dict[str, int] = field(default_factory=dict)
    cache_bytes: int = 0
    encoded_document_bytes: int = 0
    history: Any = None
    plan: dict[str, Any] | None = None


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def ensure_schema(db: sqlite3.Connection) -> None:
    ensure_transcript_schema(db)


def prepare_history(history: Any, *, previous: PreparedHistory | None = None) -> PreparedHistory:
    del previous
    snapshot = copy.deepcopy(history)
    return PreparedHistory(serialized="{}", history=snapshot)


def stage_history(
    store: Any,
    prepared: PreparedHistory,
    *,
    on_commit=None,
    confirmed=(),
) -> dict[str, int]:
    del on_commit, confirmed
    metrics = {
        "staged_chunks": 0,
        "staged_bytes": 0,
        "reused_chunks": 0,
        "reused_bytes": 0,
        "staging_transactions": 0,
    }
    return metrics


def publish_history(db: sqlite3.Connection, thread_id: str, prepared: PreparedHistory) -> bool:
    """Publish a prepared history into TranscriptStore within db's write scope."""
    path_row = db.execute("PRAGMA database_list").fetchone()
    db_path = path_row[2] if path_row and path_row[2] else None
    if not db_path:
        raise HistoryUnavailableError("Transcript storage requires a file-backed coordination database.")
    dummy_store = _ConnectionBoundStore(Path(db_path), db)
    transcripts = TranscriptStore(dummy_store)
    try:
        plan = transcripts.prepare_turns_write(
            thread_id,
            prepared.history if prepared.history is not None else json.loads(prepared.serialized),
        )
        changed = transcripts.commit_turns_index(db, plan)
        transcripts.finalize_turns_commit(plan)
        return changed
    finally:
        transcripts.close()


class _ConnectionBoundStore:
    """Minimal store adapter for publish_history callers already inside a transaction."""

    def __init__(self, database_path: Path, connection: sqlite3.Connection) -> None:
        self.database_path = database_path
        self._conn = connection

    def reading(self):
        from contextlib import nullcontext
        return nullcontext()

    def _connection(self):
        from contextlib import nullcontext
        return nullcontext(self._conn)


# ----------------------------------------------------------------------
# Legacy read-only manifest decoder (used for fallback & CLI migration)
# ----------------------------------------------------------------------


def _documents(db: sqlite3.Connection, references: list) -> list:
    digests, multiblock = set(), set()
    for reference in references:
        if (
            not isinstance(reference, list)
            or not reference
            or any(
                not isinstance(value, str)
                or len(value) != 64
                or any(ch not in "0123456789abcdef" for ch in value)
                for value in reference
            )
        ):
            raise HistoryUnavailableError("Saved conversation has an invalid block reference.")
        digests.update(reference)
        if len(reference) > 1:
            multiblock.update(reference)
    blocks: dict[str, bytes] = {}
    try:
        for binary, group in ((True, multiblock), (False, digests - multiblock)):
            ordered = sorted(group)
            for offset in range(0, len(ordered), READ_CHUNKS):
                batch = ordered[offset : offset + READ_CHUNKS]
                source = " FROM browser_history_chunks WHERE digest IN (" + ",".join("?" for _ in batch) + ")"
                if binary:
                    rows = db.execute(
                        "SELECT digest, CASE typeof(payload) WHEN 'blob' THEN payload END" + source, batch
                    )
                else:
                    packed = db.execute(
                        "SELECT json_group_array(json_array(digest, typeof(payload), hex(payload)))" + source,
                        batch,
                    ).fetchone()[0]
                    rows = (
                        (digest, bytes.fromhex(encoded) if kind == "blob" else None)
                        for digest, kind, encoded in json.loads(packed)
                    )
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


def _materialize_manifest(manifest: dict, read_documents, *, recent: bool = False, live_turn: str | None = None):
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
            if window["earlierTurns"] > 0:
                window["beforeSeq"] = window["earlierTurns"]
            return window
        return history
    except (KeyError, TypeError, StopIteration) as exc:
        raise HistoryUnavailableError("Saved conversation has an invalid manifest.") from exc


def _decode(db: sqlite3.Connection, manifest: dict, *, recent: bool = False, live_turn: str | None = None):
    own_snapshot = not db.in_transaction
    if own_snapshot:
        db.execute("BEGIN")
    try:
        return _materialize_manifest(
            manifest,
            lambda refs: _documents(db, refs),
            recent=recent,
            live_turn=live_turn,
        )
    finally:
        if own_snapshot:
            db.rollback()


def decode_history(serialized: str | bytes, *, database_path: str | Path | None = None, db: sqlite3.Connection | None = None):
    """Decode legacy inline JSON or a chunked manifest in read-only mode."""
    value = json.loads(serialized)
    if not isinstance(value, dict) or MARKER not in value:
        return value
    if db is not None:
        return _decode(db, value)
    if database_path is None:
        raise HistoryUnavailableError("A database is required to read saved conversation blocks.")
    path = Path(database_path).expanduser().resolve()

    def read(mode: str):
        with closing(sqlite3.connect(path.as_uri() + "?mode=" + mode, uri=True)) as connection:
            connection.execute("PRAGMA query_only=ON")
            return _decode(connection, value)

    try:
        return read("ro")
    except sqlite3.OperationalError as error:
        code = getattr(error, "sqlite_errorcode", None)
        if code != SQLITE_CANTOPEN and not (code is None and str(error) == "unable to open database file"):
            raise
    return read("rw")


# ----------------------------------------------------------------------
# Primary TranscriptStore read/save facade with legacy read fallback
# ----------------------------------------------------------------------


def _legacy_table_exists(db: sqlite3.Connection) -> bool:
    return bool(
        db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='browser_history'").fetchone()
    )


def saved_turn_status(store: Any, thread_id: str, turn_id: str) -> str | None:
    status = store.transcripts.turn_status(thread_id, turn_id)
    if status is not None:
        return status
    with store.reading(), store._connection() as db:
        if not _legacy_table_exists(db):
            return None
        db.execute("BEGIN")
        row = db.execute("SELECT history_json FROM browser_history WHERE thread_id=?", (thread_id,)).fetchone()
        if not row:
            return None
        decoded = decode_history(row[0], db=db)
        turns = decoded.get("turns", []) if isinstance(decoded, dict) else []
        return next((turn.get("status") for turn in reversed(turns) if turn.get("id") == turn_id), None)


class SavedHistoryWindows:
    """Bounded window cache for legacy fallback histories."""

    def __init__(self) -> None:
        self.windows: OrderedDict = OrderedDict()
        self.lock = threading.Lock()

    def read(
        self,
        raw: bytes,
        db: sqlite3.Connection,
        live_turn: str | None,
        *,
        thread_id: str | None = None,
        before_seq: int | None = None,
        limit: int = RECENT_TURNS,
    ) -> dict:
        del thread_id
        key = (
            (hashlib.sha256(raw).digest(), live_turn, before_seq, limit)
            if len(raw) > INLINE_BYTES
            else None
        )
        if key:
            with self.lock:
                cached = self.windows.get(key)
                if cached is not None:
                    self.windows.move_to_end(key)
            if cached is not None:
                return json.loads(cached)
        thread = json.loads(raw)
        if isinstance(thread, dict) and MARKER in thread:
            full = _decode(db, thread)
            return recent_view(full, live_turn=live_turn, before_seq=before_seq, limit=limit)
        window = recent_view(thread, live_turn=live_turn, before_seq=before_seq, limit=limit)
        if key:
            encoded = json.dumps(window, separators=(",", ":")).encode()
            if len(encoded) <= 4 * 1024 * 1024:
                with self.lock:
                    self.windows[key] = encoded
                    self.windows.move_to_end(key)
                    if len(self.windows) > 4:
                        self.windows.popitem(last=False)
        return window


def read_history(
    store: Any,
    thread_id: str,
    *,
    windows: SavedHistoryWindows | None = None,
    live_turn: str | None = None,
    before_seq: int | None = None,
    limit: int = RECENT_TURNS,
) -> dict | None:
    """Read history or a recent window from TranscriptStore, with legacy read fallback."""
    if windows is not None or before_seq is not None:
        window = store.transcripts.read_window(
            thread_id,
            before_seq=before_seq,
            limit=limit,
            active_turn_id=live_turn,
        )
        if window is not None:
            return window
    else:
        full = store.transcripts.read_full_history(thread_id)
        if full is not None:
            return full

    with store.reading(), store._connection() as db:
        if not _legacy_table_exists(db):
            return None
        db.execute("BEGIN")
        row = db.execute(
            "SELECT CAST(history_json AS BLOB) FROM browser_history WHERE thread_id=?",
            (thread_id,),
        ).fetchone()
        if not row:
            return None
        if windows is not None or before_seq is not None:
            cache = windows or SavedHistoryWindows()
            return cache.read(
                row[0],
                db,
                live_turn,
                thread_id=thread_id,
                before_seq=before_seq,
                limit=limit,
            )
        return decode_history(row[0], db=db)


def save_history(store: Any, thread_id: str, history: dict) -> None:
    """Persist history incrementally to TranscriptStore and update summary."""
    from .attention import completed_summary, save_summary

    summary = completed_summary(history) if isinstance(history, dict) else None

    def _on_commit(db: sqlite3.Connection, _changed: bool) -> None:
        if summary:
            save_summary(db, thread_id, *summary)

    store.transcripts.save_turns(
        thread_id,
        history,
        on_commit_db=_on_commit if summary else None,
    )


class HistoryPersistence:
    """Incremental O(1) turn persistence facade backed by TranscriptStore."""

    def __init__(self, store: Any, *, on_saved=None) -> None:
        self.store = store
        self.on_saved = on_saved
        self.lock = threading.Lock()
        self.metrics: dict[str, Any] = {
            "snapshots": 0,
            "unchanged_snapshots": 0,
            "source_bytes": 0,
            "staged_chunks": 0,
            "staged_bytes": 0,
            "reused_chunks": 0,
            "reused_bytes": 0,
            "staging_transactions": 0,
            "published_bytes": 0,
            "encoded_document_bytes": 0,
            "elapsed_seconds": 0.0,
            "failures": 0,
            "search_seed_failures": 0,
        }

    def _record(self, values: dict[str, Any]) -> None:
        with self.lock:
            for key, value in values.items():
                self.metrics[key] = self.metrics.get(key, 0) + value

    def save(self, thread_id: str, history: dict) -> dict[str, Any]:
        gen = self.save_steps(thread_id, history)
        while True:
            try:
                next(gen)
            except StopIteration as finished:
                return finished.value

    def save_steps(self, thread_id: str, history: dict):
        started = time.monotonic()
        if False:  # Keep generator protocol for BrowserPersistence step runner.
            yield
        try:
            from .attention import completed_summary, save_summary

            summary = completed_summary(history) if isinstance(history, dict) else None

            def _on_commit(db: sqlite3.Connection, _changed: bool) -> None:
                if summary:
                    save_summary(db, thread_id, *summary)

            result = self.store.transcripts.save_turns(
                thread_id,
                history,
                on_commit_db=_on_commit if summary else None,
            )
            changed = bool(result["changed"])
            written_bytes = int(result["written_bytes"])
            published = {
                "snapshots": 1,
                "unchanged_snapshots": int(not changed),
                "source_bytes": written_bytes,
                "published_bytes": written_bytes if changed else 0,
                "encoded_document_bytes": written_bytes,
            }
            self._record(published)
            if self.on_saved is not None:
                try:
                    self.on_saved(thread_id, history)
                except Exception:
                    self._record({"search_seed_failures": 1})
                    logging.getLogger(__name__).exception("Could not schedule saved-history search indexing")
            return {**published, "elapsed_seconds": time.monotonic() - started}
        except Exception:
            self._record({"failures": 1})
            raise
        finally:
            self._record({"elapsed_seconds": time.monotonic() - started})

    def status(self) -> dict[str, Any]:
        with self.lock:
            return {
                **self.metrics,
                "preparation_cache_bytes": 0,
                "preparation_cache_threads": 0,
                "confirmed_chunks": 0,
            }

    def forget(self, thread_id: str) -> None:
        self.store.transcripts.forget(thread_id)
