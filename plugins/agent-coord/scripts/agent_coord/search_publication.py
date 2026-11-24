"""Replay committed search-source changes into the independent search database."""

from __future__ import annotations

import json
import logging
import threading
import time

from .thread_search import _ARTIFACTS, _LATEST_CHECKPOINT, _SUMMARY


def install_change_capture(db):
    """Install before baseline export so concurrent source changes remain replayable."""
    db.execute("""CREATE TABLE IF NOT EXISTS search_publications (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, thread_id TEXT NOT NULL,
        kind TEXT NOT NULL, UNIQUE(thread_id, kind))""")
    sources = (
        ("work_threads", "metadata", ("title", "domain", "original_request")),
        ("thread_links", "metadata", ("id", "label", "target")),
        ("thread_checkpoints", "metadata", ("id", "summary", "next_action")),
        ("transcript_generations", "history", ("generation", "active")),
        ("browser_history", "history", ("history_json",)),
    )
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table, kind, columns in sources:
        if table not in tables:
            continue
        def record(row):
            return f"""INSERT INTO search_publications(thread_id,kind) VALUES ({row}.thread_id,'{kind}')
                ON CONFLICT(thread_id,kind) DO UPDATE SET sequence=excluded.sequence;"""
        for action, row in (("INSERT", "NEW"), ("DELETE", "OLD")):
            db.execute(f"""CREATE TRIGGER IF NOT EXISTS search_publication_{table}_{action}
                AFTER {action} ON {table} BEGIN {record(row)} END""")
        db.execute(f"""CREATE TRIGGER IF NOT EXISTS search_publication_{table}_update
            AFTER UPDATE OF {','.join(columns)},thread_id ON {table} BEGIN
            INSERT INTO search_publications(thread_id,kind)
                SELECT OLD.thread_id,'{kind}' WHERE OLD.thread_id IS NOT NEW.thread_id
                ON CONFLICT(thread_id,kind) DO UPDATE SET sequence=excluded.sequence;
            {record('NEW')} END""")


def read_metadata(db, thread_id):
    row = db.execute(f"""SELECT t.title,t.original_request,{_SUMMARY},{_ARTIFACTS},t.domain
        FROM work_threads t {_LATEST_CHECKPOINT} WHERE t.thread_id=?""", (thread_id,)).fetchone()
    return None if row is None else dict(zip(("title", "request", "summary", "artifacts", "domain"), row))


def _search_history(store, thread_id):
    """Read prose from compact turns. Unconverted generations use the existing decoder."""
    from .history_persistence import read_history
    from .transcript_store import TranscriptStore

    transcripts = TranscriptStore(store)
    page = transcripts.read_window(thread_id, limit=200)
    if page is None:
        return read_history(store, thread_id)
    pages = [page["turns"]]
    while page.get("beforeSeq"):
        page = transcripts.read_window(thread_id, before_seq=page["beforeSeq"], limit=200)
        if page is None:
            raise ValueError("Transcript generation changed during search publication")
        pages.append(page["turns"])
    return {**page, "turns": [turn for turns in reversed(pages) for turn in turns]}


def _source_versions(store, identity=None):
    sql = """SELECT t.thread_id, COALESCE(h.sequence,0), COALESCE(m.sequence,0)
        FROM work_threads t
        LEFT JOIN search_publications h ON h.thread_id=t.thread_id AND h.kind='history'
        LEFT JOIN search_publications m ON m.thread_id=t.thread_id AND m.kind='metadata'"""
    args = ()
    if identity is not None:
        sql += " WHERE t.thread_id=?"
        args = (identity,)
    with store.reading(), store._connection() as db:
        return {key: (history, metadata) for key, history, metadata in db.execute(sql, args)}


def prepare_search_publication(store, *, stopped, owner_guard):
    """Verify a resumable baseline against a source revision before switching reads.

    The owning migration installs source change capture before copying any roots.
    Records newer than the verified snapshot remain in that persistent outbox.
    """
    from .search_store import SearchStore

    def permitted():
        return not store.read_only and not stopped.is_set() and owner_guard()

    if not permitted():
        return False
    index = SearchStore.for_store(store)
    if index.active:
        return True
    with index.connection() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS search_baseline_receipts (
            thread_id TEXT PRIMARY KEY, history_sequence INTEGER NOT NULL,
            metadata_sequence INTEGER NOT NULL) WITHOUT ROWID""")
        verified = {key: (history, metadata) for key, history, metadata in
                    db.execute("SELECT thread_id,history_sequence,metadata_sequence FROM search_baseline_receipts")}
    versions = _source_versions(store)
    for identity, version in versions.items():
        if not permitted():
            return False
        if verified.get(identity) == version:
            continue
        with store.reading(), store._connection() as db:
            metadata = read_metadata(db, identity)
        if metadata is None:
            continue
        history = _search_history(store, identity)
        if _source_versions(store, identity).get(identity) != version or not permitted():
            continue
        record = dict(thread_id=identity, history=history, metadata=metadata, revision=version[0])
        index.enqueue_history(identity, history, revision=version[0], metadata=metadata).result()
        with index.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if not index.matches_record(db, record):
                raise ValueError("Search baseline differs from the source history or metadata")
            db.execute("INSERT OR REPLACE INTO search_baseline_receipts VALUES (?,?,?)", (identity, *version))
        verified[identity] = version

    current = _source_versions(store)
    if any(verified.get(key) != value for key, value in current.items()) or not permitted():
        return False
    with index.connection(readonly=True) as db:
        extra = {row[0] for row in db.execute("SELECT thread_id FROM search_threads")} - current.keys()
    for identity in extra:
        if not permitted():
            return False
        index.delete(identity).result()
        with index.connection() as db:
            db.execute("DELETE FROM search_baseline_receipts WHERE thread_id=?", (identity,))
    # Any change after this source snapshot is durable and replayed by the
    # owning worker. Recording the verified revisions and activation together
    # avoids exposing an incomplete index across a coordinator restart.
    if not permitted():
        return False
    with index.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        if {row[0] for row in db.execute("SELECT thread_id FROM search_threads")} != current.keys():
            return False
        db.execute("""CREATE TABLE IF NOT EXISTS applied_publications (
            thread_id TEXT NOT NULL, kind TEXT NOT NULL, sequence INTEGER NOT NULL,
            PRIMARY KEY(thread_id,kind)) WITHOUT ROWID""")
        db.executemany("INSERT OR REPLACE INTO applied_publications VALUES (?,?,?)",
                       ((key, kind, seq) for key, version in current.items()
                        for kind, seq in zip(("history", "metadata"), version)))
        db.execute("INSERT OR REPLACE INTO search_control VALUES ('verified',?)",
                   (json.dumps({"threads": len(current), "failures": []}),))
        db.execute("INSERT OR REPLACE INTO search_control VALUES ('active','1')")
    return True


class SearchPublicationWorker:
    """Only the owning runtime consumes the outbox. All source reads are query-only."""

    def __init__(self, store, index):
        self.store = store
        self.index = index
        self.stopped = threading.Event()
        self.thread = None
        self.cursor = 0
        self.retry = {}
        with index.connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS applied_publications (
                thread_id TEXT NOT NULL, kind TEXT NOT NULL, sequence INTEGER NOT NULL,
                PRIMARY KEY(thread_id,kind)) WITHOUT ROWID""")

    @classmethod
    def start_if_active(cls, store):
        from .desktop_runtime import _OWNED_DATABASES
        if store.read_only or str(store.database_path) not in _OWNED_DATABASES:
            return None
        from .search_store import active_search
        index = active_search(store)
        if index is None:
            return None
        with store._connection() as db:
            install_change_capture(db)
        worker = cls(store, index)
        worker.thread = threading.Thread(target=worker._run, name="search-publications", daemon=True)
        worker.thread.start()
        return worker

    def _run(self):
        while not self.stopped.is_set():
            try:
                processed = self.process_batch()
            except Exception:
                logging.getLogger(__name__).exception("Reading search publication queue failed")
                self.stopped.wait(2)
                continue
            if not processed:
                with self.store._writer.commit_condition:
                    if not self.stopped.is_set():
                        self.store._writer.commit_condition.wait(1)

    def _deliver(self, identity, kind, sequence):
        with self.index.connection() as db:
            applied = db.execute("SELECT sequence FROM applied_publications WHERE thread_id=? AND kind=?",
                                 (identity, kind)).fetchone()
        if applied is not None and applied[0] >= sequence:
            return
        with self.store.reading(), self.store._connection() as db:
            metadata = read_metadata(db, identity)
        history = _search_history(self.store, identity) if metadata is not None and kind == "history" else None
        # A page walk can span several read snapshots. Publish it only while the
        # source still has the revision that initiated the walk.
        with self.store.reading(), self.store._connection() as db:
            current = db.execute("SELECT sequence FROM search_publications WHERE thread_id=? AND kind=?",
                                 (identity, kind)).fetchone()
        if current is None or current[0] != sequence:
            return
        if metadata is None:
            self.index.delete(identity).result()
        elif kind == "history":
            self.index.enqueue_history(identity, history, revision=sequence).result()
        else:
            self.index.enqueue_metadata(identity, metadata).result()
        # A crash between independent commits replays the same source revision.
        with self.index.connection() as db:
            db.execute("""INSERT INTO applied_publications VALUES (?,?,?)
                ON CONFLICT(thread_id,kind) DO UPDATE SET sequence=MAX(sequence,excluded.sequence)""",
                       (identity, kind, sequence))

    def process_batch(self):
        with self.store.reading(), self.store._connection() as db:
            rows = db.execute("""SELECT thread_id,kind,sequence FROM search_publications
                WHERE sequence>? ORDER BY sequence LIMIT 64""", (self.cursor,)).fetchall()
        work = dict((key, sequence) for key, (sequence, expiry) in self.retry.items()
                    if expiry <= time.monotonic())
        for identity, kind, sequence in rows:
            work[identity, kind] = sequence
        for (identity, kind), sequence in work.items():
            if self.stopped.is_set():
                return False
            try:
                self._deliver(identity, kind, sequence)
            except Exception:
                if (identity, kind) not in self.retry:
                    logging.getLogger(__name__).exception("Search publication failed at revision %d", sequence)
                self.retry[identity, kind] = (sequence, time.monotonic() + 5)
            else:
                self.retry.pop((identity, kind), None)
        if rows:
            self.cursor = rows[-1][2]
        return bool(rows)

    def close(self):
        self.stopped.set()
        with self.store._writer.commit_condition:
            self.store._writer.commit_condition.notify_all()
        if self.thread is not None:
            self.thread.join()
        self.index.close()
