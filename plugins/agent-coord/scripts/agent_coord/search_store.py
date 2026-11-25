"""Isolated FTS5 trigram search store in search.sqlite3.

Search indexing runs on an independent single-writer queue against
``<db_dir>/search.sqlite3`` (WAL + synchronous=NORMAL), never acquiring
``state.sqlite3``'s writer lock. Only two tables exist in ``search.sqlite3``:
  - ``search_turns`` (compact <= 320-char excerpt per turn)
  - ``search_fts`` (FTS5 trigram virtual table with detail=full and bm25())
"""
from __future__ import annotations

from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import queue
import sqlite3
import threading
import weakref
from typing import Any

from .history_window import messages, routing_history, turn_messages

MAX_EXCERPT_CHARACTERS = 320

SEARCH_SCHEMA = """
CREATE TABLE IF NOT EXISTS search_turns (
    thread_id TEXT NOT NULL,
    turn_seq INTEGER NOT NULL,
    turn_id TEXT,
    role TEXT,
    created_at REAL,
    excerpt TEXT,
    PRIMARY KEY(thread_id, turn_seq)
);
CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
    thread_id UNINDEXED,
    turn_seq UNINDEXED,
    title,
    domain,
    body,
    tokenize='trigram',
    detail=full
);
"""


def _normalize(value: str | None) -> str:
    return " ".join((value or "").casefold().split())


def _compact_excerpt(value: str | None) -> str:
    collapsed = " ".join((value or "").split())
    return collapsed[:MAX_EXCERPT_CHARACTERS]


def search_database_path(state_database_path: str | Path) -> Path:
    return Path(state_database_path).expanduser().resolve().parent / "search.sqlite3"


def _fts_phrase_query(terms: list[str], *, body_only: bool = False) -> str | None:
    trigram_terms = [term for term in terms if len(term) >= 3]
    if not trigram_terms:
        return None
    escaped = ['"' + term.replace('"', '""') + '"' for term in trigram_terms]
    expr = " AND ".join(escaped)
    return f"body : ({expr})" if body_only else expr


class _ReadConnectionHolder:
    """Reusable read-only SQLite connection bound to one thread."""

    def __init__(self, connection: sqlite3.Connection, file_identity: tuple[int, int]) -> None:
        self.connection = connection
        self.file_identity = file_identity
        self.closer = weakref.finalize(self, connection.close)

    def discard(self) -> None:
        if self.closer is not None:
            self.closer()
            self.closer = None
        self.connection = None


def _close_readers(holders: list[_ReadConnectionHolder], lock: threading.Lock) -> None:
    with lock:
        snapshot = list(holders)
        holders.clear()
    for holder in snapshot:
        holder.discard()


def _shutdown_worker(
    stop_event: threading.Event,
    work_queue: queue.SimpleQueue,
    worker_ref: list,
    reader_holders: list[_ReadConnectionHolder],
    reader_lock: threading.Lock,
) -> None:
    stop_event.set()
    work_queue.put(None)
    worker = worker_ref[0]
    if worker is not None and worker.is_alive() and worker is not threading.current_thread():
        worker.join(timeout=5)
    _close_readers(reader_holders, reader_lock)


class SearchStore:
    """Dedicated single-writer FTS5 search index isolated from state.sqlite3."""

    def __init__(self, state_database_path: str | Path) -> None:
        self.state_database_path = Path(state_database_path).expanduser().resolve()
        self.path = search_database_path(self.state_database_path)
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        self._stop = threading.Event()
        self._write_lock = threading.Lock()
        self._worker_lock = threading.Lock()
        self._worker_holder: list[threading.Thread | None] = [None]
        self._read_local = threading.local()
        self._reader_lock = threading.Lock()
        self._reader_holders: list[_ReadConnectionHolder] = []
        self._indexed_turns = 0
        self._indexed_metadata = 0
        self._pending_tasks = 0
        self._idle_condition = threading.Condition()
        self._initialized = False
        self._finalizer = weakref.finalize(
            self,
            _shutdown_worker,
            self._stop,
            self._queue,
            self._worker_holder,
            self._reader_holders,
            self._reader_lock,
        )

    def _ensure_initialized(self) -> None:
        if self._initialized and self.path.exists():
            return
        with self._write_lock:
            if self._initialized and self.path.exists():
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.path, timeout=5.0)) as db:
                db.execute("PRAGMA journal_mode = WAL")
                db.execute("PRAGMA synchronous = NORMAL")
                db.execute("PRAGMA busy_timeout = 5000")
                tables = {
                    row[0]
                    for row in db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                    )
                }
                if "search_control" in tables:
                    for name in list(tables):
                        if not name.startswith("search_fts_"):
                            db.execute(f'DROP TABLE IF EXISTS "{name}"')
                with db:
                    db.executescript(SEARCH_SCHEMA)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
            self._initialized = True

    def _ensure_worker(self) -> None:
        if self._stop.is_set():
            return
        with self._worker_lock:
            worker = self._worker_holder[0]
            if worker is None or not worker.is_alive():
                worker = threading.Thread(target=self._run_writer, name="search-store-writer", daemon=True)
                self._worker_holder[0] = worker
                worker.start()

    def _open_writer_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def _run_writer(self) -> None:
        writer_conn: sqlite3.Connection | None = None
        writer_identity: tuple[int, int] | None = None
        try:
            while not self._stop.is_set():
                task = self._queue.get()
                if task is None:
                    break
                batch = [task]
                while len(batch) < 64:
                    try:
                        nxt = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    if nxt is None:
                        self._stop.set()
                        break
                    batch.append(nxt)
                try:
                    if not self._stop.is_set() and self.state_database_path.exists():
                        self._ensure_initialized()
                        with self._write_lock:
                            st = self.path.stat()
                            cur_identity = (st.st_dev, st.st_ino)
                            if writer_conn is None or writer_identity != cur_identity:
                                if writer_conn is not None:
                                    writer_conn.close()
                                writer_conn = self._open_writer_connection()
                                writer_identity = cur_identity
                            with writer_conn:
                                for item in batch:
                                    self._apply_task(writer_conn, item)
                except (sqlite3.Error, OSError):
                    if writer_conn is not None:
                        try:
                            writer_conn.close()
                        except sqlite3.Error:
                            pass
                        writer_conn = None
                        writer_identity = None
                finally:
                    with self._idle_condition:
                        self._pending_tasks = max(0, self._pending_tasks - len(batch))
                        if self._pending_tasks == 0:
                            self._idle_condition.notify_all()
        finally:
            if writer_conn is not None:
                try:
                    writer_conn.close()
                except sqlite3.Error:
                    pass

    def _apply_task(self, db: sqlite3.Connection, task: tuple) -> None:
        kind = task[0]
        if kind == "turns":
            _, thread_id, turn_pairs = task
            self._upsert_turns_on_connection(db, thread_id, turn_pairs)
        elif kind == "metadata":
            _, thread_id, title, domain, body = task
            self._upsert_metadata_on_connection(db, thread_id, title, domain, body)
        elif kind == "delete_thread":
            _, thread_id = task
            self._delete_thread_on_connection(db, thread_id)

    def _upsert_metadata_on_connection(
        self, db: sqlite3.Connection, thread_id: str, title: str, domain: str, body: str
    ) -> None:
        existing = db.execute(
            "SELECT rowid FROM search_turns WHERE thread_id = ? AND turn_seq = -1",
            (thread_id,),
        ).fetchone()
        excerpt = _compact_excerpt(title or body)
        if existing is not None:
            base_rowid = existing[0]
            db.execute("DELETE FROM search_fts WHERE rowid IN (?, ?)", (base_rowid * 2, base_rowid * 2 + 1))
            db.execute(
                "UPDATE search_turns SET role = 'context', excerpt = ? WHERE rowid = ?",
                (excerpt, base_rowid),
            )
        else:
            cursor = db.execute(
                "INSERT INTO search_turns(thread_id, turn_seq, turn_id, role, created_at, excerpt) VALUES (?, -1, NULL, 'context', NULL, ?)",
                (thread_id, excerpt),
            )
            base_rowid = cursor.lastrowid
        norm_title = _normalize(title)
        norm_domain = _normalize(domain)
        norm_body = _normalize(body)
        if norm_title or norm_domain or norm_body:
            db.execute(
                "INSERT INTO search_fts(rowid, thread_id, turn_seq, title, domain, body) VALUES (?, ?, -1, ?, ?, ?)",
                (base_rowid * 2, thread_id, norm_title, norm_domain, norm_body),
            )
        self._indexed_metadata += 1

    def _upsert_turns_on_connection(
        self, db: sqlite3.Connection, thread_id: str, turn_pairs: list[tuple[int, dict]]
    ) -> None:
        for turn_seq, turn in turn_pairs:
            if not isinstance(turn, dict) or turn.get("status") == "inProgress":
                continue
            extracted = list(turn_messages(turn, turn_seq))
            turn_id = str(turn.get("id") or f"search-turn-{turn_seq}")
            existing = db.execute(
                "SELECT rowid FROM search_turns WHERE thread_id = ? AND turn_seq = ?",
                (thread_id, turn_seq),
            ).fetchone()
            if not extracted:
                if existing is not None:
                    base_rowid = existing[0]
                    db.execute("DELETE FROM search_fts WHERE rowid IN (?, ?)", (base_rowid * 2, base_rowid * 2 + 1))
                    db.execute("DELETE FROM search_turns WHERE rowid = ?", (base_rowid,))
                continue

            user_texts = [entry[4] for entry in extracted if entry[2] == "user"]
            assistant_texts = [entry[4] for entry in extracted if entry[2] == "assistant"]
            roles = {entry[2] for entry in extracted}
            role = "user" if roles == {"user"} else "assistant" if roles == {"assistant"} else "both"
            created_at = next((entry[3] for entry in reversed(extracted) if entry[3] is not None), None)
            combined_raw = "\n".join(entry[4] for entry in extracted)
            excerpt = _compact_excerpt(combined_raw)

            if existing is not None:
                base_rowid = existing[0]
                db.execute("DELETE FROM search_fts WHERE rowid IN (?, ?)", (base_rowid * 2, base_rowid * 2 + 1))
                db.execute(
                    "UPDATE search_turns SET turn_id = ?, role = ?, created_at = ?, excerpt = ? WHERE rowid = ?",
                    (turn_id, role, created_at, excerpt, base_rowid),
                )
            else:
                cursor = db.execute(
                    "INSERT INTO search_turns(thread_id, turn_seq, turn_id, role, created_at, excerpt) VALUES (?, ?, ?, ?, ?, ?)",
                    (thread_id, turn_seq, turn_id, role, created_at, excerpt),
                )
                base_rowid = cursor.lastrowid

            if user_texts:
                db.execute(
                    "INSERT INTO search_fts(rowid, thread_id, turn_seq, title, domain, body) VALUES (?, ?, ?, '', '', ?)",
                    (base_rowid * 2, thread_id, turn_seq, _normalize("\n".join(user_texts))),
                )
            if assistant_texts:
                db.execute(
                    "INSERT INTO search_fts(rowid, thread_id, turn_seq, title, domain, body) VALUES (?, ?, ?, '', '', ?)",
                    (base_rowid * 2 + 1, thread_id, turn_seq, _normalize("\n".join(assistant_texts))),
                )
            self._indexed_turns += 1

    def _delete_thread_on_connection(self, db: sqlite3.Connection, thread_id: str) -> None:
        rowids = [
            row[0]
            for row in db.execute("SELECT rowid FROM search_turns WHERE thread_id = ?", (thread_id,))
        ]
        if rowids:
            fts_ids = [val for rid in rowids for val in (rid * 2, rid * 2 + 1)]
            for idx in range(0, len(fts_ids), 256):
                chunk = fts_ids[idx : idx + 256]
                placeholders = ",".join("?" for _ in chunk)
                db.execute(f"DELETE FROM search_fts WHERE rowid IN ({placeholders})", chunk)
            db.execute("DELETE FROM search_turns WHERE thread_id = ?", (thread_id,))

    # ------------------------------------------------------------------
    # Public non-blocking enqueue & synchronous batch APIs
    # ------------------------------------------------------------------

    def enqueue_turns(self, thread_id: str, turn_pairs: list[tuple[int, dict]]) -> None:
        finalized = [
            (seq, turn)
            for seq, turn in turn_pairs
            if isinstance(turn, dict) and turn.get("status") != "inProgress"
        ]
        if not finalized or self._stop.is_set():
            return
        with self._idle_condition:
            self._pending_tasks += 1
        self._ensure_worker()
        self._queue.put(("turns", thread_id, finalized))

    def enqueue_metadata(self, thread_id: str, title: str = "", domain: str = "", body: str = "") -> None:
        if self._stop.is_set():
            return
        with self._idle_condition:
            self._pending_tasks += 1
        self._ensure_worker()
        self._queue.put(("metadata", thread_id, title or "", domain or "", body or ""))

    def delete_thread(self, thread_id: str) -> None:
        if self._stop.is_set():
            return
        with self._idle_condition:
            self._pending_tasks += 1
        self._ensure_worker()
        self._queue.put(("delete_thread", thread_id))

    def index_turns_sync(self, thread_id: str, turn_pairs: list[tuple[int, dict]]) -> None:
        self._ensure_initialized()
        with self._write_lock, closing(self._open_writer_connection()) as db:
            with db:
                self._upsert_turns_on_connection(db, thread_id, turn_pairs)

    def index_metadata_sync(self, thread_id: str, title: str = "", domain: str = "", body: str = "") -> None:
        self._ensure_initialized()
        with self._write_lock, closing(self._open_writer_connection()) as db:
            with db:
                self._upsert_metadata_on_connection(db, thread_id, title, domain, body)

    def delete_thread_sync(self, thread_id: str) -> None:
        self._ensure_initialized()
        with self._write_lock, closing(self._open_writer_connection()) as db:
            with db:
                self._delete_thread_on_connection(db, thread_id)

    def flush(self, timeout: float = 5.0) -> bool:
        with self._idle_condition:
            return self._idle_condition.wait_for(lambda: self._pending_tasks == 0, timeout=timeout)

    def close(self) -> None:
        self.flush(timeout=2.0)
        _shutdown_worker(
            self._stop,
            self._queue,
            self._worker_holder,
            self._reader_holders,
            self._reader_lock,
        )
        with self._worker_lock:
            self._worker_holder[0] = None
            self._stop.clear()

    @contextmanager
    def _readonly_connection(self):
        self._ensure_initialized()
        st = self.path.stat()
        identity = (st.st_dev, st.st_ino)
        holder: _ReadConnectionHolder | None = getattr(self._read_local, "holder", None)
        if holder is not None and (holder.connection is None or holder.file_identity != identity):
            holder.discard()
            holder = None
            self._read_local.holder = None
        if holder is None:
            conn = sqlite3.connect(
                self.path.as_uri() + "?mode=ro",
                uri=True,
                timeout=2.0,
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = ON")
            conn.execute("PRAGMA busy_timeout = 2000")
            holder = _ReadConnectionHolder(conn, identity)
            with self._reader_lock:
                self._reader_holders.append(holder)
            self._read_local.holder = holder
        try:
            yield holder.connection
        finally:
            if holder.connection is not None and holder.connection.in_transaction:
                holder.connection.rollback()

    # ------------------------------------------------------------------
    # Read-only FTS5 query API
    # ------------------------------------------------------------------

    def search_messages(
        self,
        terms: list[str],
        *,
        user_only: bool = False,
        thread_ids: set[str] | list[str] | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """Query search_fts + search_turns using native FTS5 trigram and bm25()."""
        self._ensure_initialized()
        if not terms:
            return {}
        fts_expr = _fts_phrase_query(terms, body_only=True)
        short_terms = [term for term in terms if len(term) < 3]
        params: list[Any] = []
        conditions = ["f.turn_seq >= 0"]
        if user_only:
            conditions.append("(f.rowid & 1) = 0")
        if fts_expr is not None:
            conditions.append("search_fts MATCH ?")
            params.append(fts_expr)
        for short in short_terms:
            conditions.append("instr(f.body, ?) > 0")
            params.append(short)
        allowed_filter: set[str] | None = None
        if thread_ids is not None:
            allowed_filter = set(thread_ids)
            if not allowed_filter:
                return {}
            if len(allowed_filter) <= 64:
                conditions.append("f.thread_id IN (SELECT value FROM json_each(?))")
                params.append(json.dumps(sorted(allowed_filter)))

        rank_expr = "-bm25(search_fts, 0.0, 0.0, 1.0)" if fts_expr is not None else "1.0"
        sql = f"""
            SELECT f.thread_id, f.turn_seq, (f.rowid & 1) AS is_assistant,
                   {rank_expr} AS rank_score,
                   t.turn_id, t.role, t.created_at, t.excerpt, f.body
            FROM search_fts f
            JOIN search_turns t ON t.rowid = (f.rowid >> 1)
            WHERE {' AND '.join(conditions)}
        """
        grouped: dict[str, list[dict[str, Any]]] = {}
        with self._readonly_connection() as db:
            for row in db.execute(sql, params):
                tid = row["thread_id"]
                if allowed_filter is not None and tid not in allowed_filter:
                    continue
                role = "assistant" if row["is_assistant"] else "user"
                grouped.setdefault(tid, []).append(
                    {
                        "turn_seq": row["turn_seq"],
                        "turn_id": row["turn_id"] or f"search-turn-{row['turn_seq']}",
                        "role": role,
                        "timestamp": row["created_at"],
                        "excerpt": row["excerpt"] or "",
                        "folded_body": row["body"] or "",
                        "bm25_score": float(row["rank_score"] or 0.0),
                    }
                )
        return grouped

    def indexed_thread_ids(self) -> set[str]:
        if not self.path.exists():
            return set()
        with self._readonly_connection() as db:
            return {
                row[0]
                for row in db.execute("SELECT DISTINCT thread_id FROM search_turns WHERE turn_seq >= 0")
            }

    def status(self) -> dict[str, Any]:
        size_bytes = self.path.stat().st_size if self.path.exists() else 0
        return {
            "database_path": str(self.path),
            "size_bytes": size_bytes,
            "pending_tasks": self._pending_tasks,
            "indexed_turns": self._indexed_turns,
            "indexed_metadata": self._indexed_metadata,
            "substring_index": {
                "supported": True,
                "state": "complete" if self._pending_tasks == 0 else "indexing",
                "remaining_sources": self._pending_tasks,
            },
        }


def search_metrics(store: Any, *, refresh_coverage: bool = True) -> dict[str, Any]:
    del refresh_coverage
    search = getattr(store, "search_store", None)
    if search is None:
        search = SearchStore(store.database_path)
    return search.status()


__all__ = [
    "MAX_EXCERPT_CHARACTERS",
    "SEARCH_SCHEMA",
    "SearchStore",
    "messages",
    "routing_history",
    "search_database_path",
    "search_metrics",
]
