"""Disposable conversation index with an independent, serialized SQLite writer.

Activation is explicit: legacy readers remain usable until a migration verifies
coverage. Nothing in this module opens the coordination database for writing.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

from .conversation_search import messages

_SCHEMA = """
CREATE TABLE IF NOT EXISTS search_turns (
    id INTEGER PRIMARY KEY, thread_id TEXT NOT NULL, turn_seq INTEGER NOT NULL,
    turn_id TEXT NOT NULL, item_id TEXT NOT NULL, role TEXT NOT NULL,
    created_at REAL, excerpt TEXT NOT NULL, UNIQUE(thread_id, turn_seq));
CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
    title, domain, body, tokenize='trigram', detail=full);
CREATE TABLE IF NOT EXISTS search_threads (
    thread_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, digest TEXT NOT NULL,
    status TEXT NOT NULL, title TEXT NOT NULL, request TEXT NOT NULL,
    summary TEXT NOT NULL, artifacts TEXT NOT NULL, domain TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS search_control (
    key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""
_LOCK = threading.RLock()
_STORES = weakref.WeakKeyDictionary()
_PATH_STORES = weakref.WeakValueDictionary()


def _valid_text(value):
    try:
        value.encode('utf-8')
    except UnicodeEncodeError:
        raise ValueError('Search text contains an unpaired Unicode surrogate; source history is unchanged') from None
    return value


def _normalize(value):
    return ' '.join(_valid_text(value or '').casefold().split())


def _prepare(history):
    if history is None:
        return [], hashlib.sha256(b'[]').hexdigest(), 'unavailable'
    if not isinstance(history, dict) or not isinstance(history.get('turns', []), list):
        raise ValueError('Search history must contain a list of turns')
    # An explicit in-progress turn is never searchable. Legacy histories did
    # not carry statuses, and are finalized snapshots supplied by the caller.
    turns = [turn for turn in history.get('turns', []) if isinstance(turn, dict)
             and turn.get('status') not in {'inProgress', 'in_progress', 'running', 'streaming'}]
    rows = list(messages({'turns': turns}))
    for row in rows:
        for value in (row[0], row[1], row[4]):
            _valid_text(value)
    digest = hashlib.sha256(json.dumps(rows, ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()
    return rows, digest, 'partial' if history.get('historyUnavailable') else 'saved'


class SearchStore:
    def __init__(self, path, *, initialize=True):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='rf-search-index')
        self._closed = False
        self._futures = set()
        self._future_lock = threading.Lock()
        if initialize:
            with self.connection() as db:
                db.execute('PRAGMA journal_mode=WAL')
                db.executescript(_SCHEMA)

    @classmethod
    def for_store(cls, store, *, initialize=True):
        with _LOCK:
            index = _STORES.get(store)
            if index is None or index._closed:
                path = Path(store.database_path)
                # Preserve isolation for custom state names and test databases.
                name = 'search.sqlite3' if path.name == 'state.sqlite3' else path.stem + '.search.sqlite3'
                destination = path.with_name(name).resolve()
                index = _PATH_STORES.get(str(destination))
                if index is None or index._closed:
                    index = cls(destination, initialize=initialize)
                    _PATH_STORES[str(destination)] = index
                _STORES[store] = index
            return index

    @contextmanager
    def connection(self, *, readonly=False):
        target = self.path.as_uri() + '?mode=ro' if readonly else str(self.path)
        db = sqlite3.connect(target, timeout=5, uri=readonly)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON' if readonly else 'PRAGMA synchronous=NORMAL')
        try:
            with db:
                yield db
        finally:
            db.close()

    def _submit(self, callback, *args):
        with self._future_lock:
            if self._closed:
                raise RuntimeError('Search index is closed')
            future = self._executor.submit(callback, *args)
            self._futures.add(future)
        future.add_done_callback(self._finished)
        return future

    def _finished(self, future):
        # Successful jobs carry no durable state. Retain failures until flush
        # reports them, without growing an unbounded set during long sessions.
        if not future.cancelled() and future.exception() is None:
            with self._future_lock:
                self._futures.discard(future)

    def enqueue_history(self, thread_id, history, *, revision, metadata=None):
        # Freeze caller-owned data before publication; providers can immediately
        # reuse their mutable history after this returns.
        serialized = json.dumps(history, ensure_ascii=False)
        metadata = dict(metadata) if metadata is not None else None
        return self._submit(self._index_history, str(thread_id), serialized, int(revision), metadata)

    def _index_history(self, identity, serialized, revision, metadata):
        rows, digest, status = _prepare(json.loads(serialized))
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM search_threads WHERE thread_id=?', (identity,)).fetchone()
            if old is not None and revision < old['revision']:
                return False
            if old is not None and revision == old['revision'] and digest != old['digest']:
                raise ValueError('Search revision reused for different conversation prose')
            db.execute("DELETE FROM search_control WHERE key='verified'")
            if old is None or old['digest'] != digest:
                previous = {row['turn_seq']: row for row in db.execute(
                    'SELECT * FROM search_turns WHERE thread_id=?', (identity,))}
                # Retain finalized prefixes. Appending a turn writes only its new
                # prose; corrected provider snapshots replace affected positions.
                for position, (turn, item, role, timestamp, text, folded) in enumerate(rows):
                    prior = previous.pop(position, None)
                    if prior is not None and tuple(prior[key] for key in
                            ('turn_id', 'item_id', 'role', 'created_at', 'excerpt')) == (turn, item, role, timestamp, text):
                        continue
                    if prior is not None:
                        db.execute('DELETE FROM search_fts WHERE rowid=?', (prior['id'],))
                        db.execute('DELETE FROM search_turns WHERE id=?', (prior['id'],))
                    cursor = db.execute('''INSERT INTO search_turns
                        (thread_id,turn_seq,turn_id,item_id,role,created_at,excerpt) VALUES (?,?,?,?,?,?,?)''',
                        (identity, position, turn, item, role, timestamp, text))
                    db.execute('INSERT INTO search_fts(rowid,title,domain,body) VALUES (?,?,?,?)',
                               (cursor.lastrowid, '', '', folded))
                for prior in previous.values():
                    db.execute('DELETE FROM search_fts WHERE rowid=?', (prior['id'],))
                    db.execute('DELETE FROM search_turns WHERE id=?', (prior['id'],))
            fields = self._metadata(metadata, old)
            db.execute('''INSERT INTO search_threads VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(thread_id) DO UPDATE SET revision=excluded.revision,digest=excluded.digest,
                status=excluded.status,title=excluded.title,request=excluded.request,
                summary=excluded.summary,artifacts=excluded.artifacts,domain=excluded.domain''',
                       (identity, revision, digest, status, *fields))
            self._index_context(db, identity, fields)
        return True

    @staticmethod
    def _metadata(metadata, old):
        metadata = metadata or {}
        return tuple(_normalize(metadata.get(key, old[key] if old is not None else ''))
                     for key in ('title', 'request', 'summary', 'artifacts', 'domain'))

    def enqueue_metadata(self, thread_id, metadata):
        return self._submit(self._index_metadata, str(thread_id), dict(metadata))

    def _index_metadata(self, identity, metadata):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("DELETE FROM search_control WHERE key='verified'")
            old = db.execute('SELECT * FROM search_threads WHERE thread_id=?', (identity,)).fetchone()
            fields = self._metadata(metadata, old)
            db.execute('''INSERT INTO search_threads VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(thread_id) DO UPDATE SET revision=excluded.revision,digest=excluded.digest,
                status=excluded.status,title=excluded.title,request=excluded.request,
                summary=excluded.summary,artifacts=excluded.artifacts,domain=excluded.domain''',
                       (identity, old['revision'] if old else -1, old['digest'] if old else '',
                        old['status'] if old else 'unavailable', *fields))

            self._index_context(db, identity, fields)

    @staticmethod
    def _index_context(db, identity, fields):
        rowid = -db.execute('SELECT rowid FROM search_threads WHERE thread_id=?', (identity,)).fetchone()[0]
        # Metadata gets its own FTS row, including threads without saved prose.
        db.execute('DELETE FROM search_fts WHERE rowid=?', (rowid,))
        db.execute('INSERT INTO search_fts(rowid,title,domain,body) VALUES (?,?,?,?)',
                   (rowid, fields[0], fields[4], ' '.join(fields[1:4])))

    def delete(self, thread_id):
        return self._submit(self._delete, str(thread_id))

    def _delete(self, identity):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("DELETE FROM search_control WHERE key='verified'")
            db.execute('DELETE FROM search_fts WHERE rowid=-(SELECT rowid FROM search_threads WHERE thread_id=?)', (identity,))
            db.execute('DELETE FROM search_fts WHERE rowid IN (SELECT id FROM search_turns WHERE thread_id=?)', (identity,))
            db.execute('DELETE FROM search_turns WHERE thread_id=?', (identity,))
            db.execute('DELETE FROM search_threads WHERE thread_id=?', (identity,))

    def flush(self):
        # Keep failures until a waiter observes them; executor callbacks must
        # never silently discard a failed asynchronous index operation.
        with self._future_lock:
            futures = tuple(self._futures)
        for future in futures:
            try:
                future.result()
            finally:
                with self._future_lock:
                    self._futures.discard(future)

    def close(self):
        with self._future_lock:
            self._closed = True
        self._executor.shutdown(wait=True)
        self.flush()

    def rebuild_steps(self, records):
        """Resume at thread granularity; each yielded receipt is committed.

        records yields {thread_id, history, revision, metadata}. Revisions must
        be monotonic for each thread, including across backend restarts.
        """
        for record in records:
            accepted = self.enqueue_history(record['thread_id'], record['history'],
                revision=record['revision'], metadata=record.get('metadata')).result()
            yield {'thread_id': record['thread_id'], 'revision': record['revision'], 'accepted': accepted}

    def verify(self, records):
        self.flush()
        failures, count, identities = [], 0, set()
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            for record in records:
                count += 1
                expected, digest, status = _prepare(record['history'])
                identity = record['thread_id']
                if identity in identities:
                    failures.append(identity)
                identities.add(identity)
                head = db.execute('SELECT * FROM search_threads WHERE thread_id=?', (identity,)).fetchone()
                actual = db.execute('''SELECT t.turn_id,t.item_id,t.role,t.created_at,t.excerpt,f.body
                    FROM search_turns t LEFT JOIN search_fts f ON f.rowid=t.id
                    WHERE t.thread_id=? ORDER BY t.turn_seq''', (identity,)).fetchall()
                if (head is None or head['digest'] != digest or head['status'] != status
                        or head['revision'] != record['revision']
                        or [tuple(row) for row in actual] != [tuple(row) for row in expected]
                        or (record.get('metadata') is not None and self._metadata(record['metadata'], head)
                            != tuple(head[key] for key in ('title', 'request', 'summary', 'artifacts', 'domain')))):
                    failures.append(identity)
            extras = {row[0] for row in db.execute('SELECT thread_id FROM search_threads')} - identities
            failures.extend(sorted(extras))
            failures = sorted(set(failures))
            db.execute("INSERT OR REPLACE INTO search_control VALUES ('verified',?)",
                       (json.dumps({'threads': count, 'failures': failures}),))
        return {'threads': count, 'failures': failures}

    def activate(self):
        self.flush()
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            verified = db.execute("SELECT value FROM search_control WHERE key='verified'").fetchone()
            if verified is None or json.loads(verified[0])['failures']:
                raise ValueError('Search activation requires successful coverage verification')
            db.execute("INSERT OR REPLACE INTO search_control VALUES ('active','1')")

    @property
    def active(self):
        with self.connection(readonly=True) as db:
            return db.execute("SELECT 1 FROM search_control WHERE key='active' AND value='1'").fetchone() is not None

    def coverage(self, *, wait=True):
        if wait:
            self.flush()
        with self.connection(readonly=True) as db:
            return dict(db.execute('''SELECT COUNT(*) AS threads,
                COALESCE(SUM(status='saved'),0) AS saved_histories,
                COALESCE(SUM(status='partial'),0) AS partial_histories,
                (SELECT COUNT(*) FROM search_turns) AS messages FROM search_threads''').fetchone())

    @staticmethod
    def native_message_source():
        return '''SELECT t.thread_id,t.turn_id,t.item_id,t.role,t.created_at AS timestamp,
            t.excerpt AS text,f.body AS folded,t.turn_seq AS position,
            json_extract(k.value,'$[1]') AS native_relevance
            FROM json_each(:native_matches) k
            JOIN isolated_search.search_turns t ON t.id=json_extract(k.value,'$[0]')
            JOIN isolated_search.search_fts f ON f.rowid=t.id'''

    @staticmethod
    def message_source(terms, *, include_text=True, thread_filter=None, evidence_only=False):
        # Quoted trigram phrases are literal matches, including code punctuation.
        # Short/NUL terms still need exact instr verification by the caller.
        eligible = [term for term in terms if len(term) >= 3 and '\0' not in term]
        match = ' AND '.join('"' + term.replace('"', '""') + '"' for term in eligible)
        phrase = ' '.join(terms)
        if len(terms) > 1 and eligible and '\0' not in phrase:
            match = '(' + match + ') OR "' + phrase.replace('"', '""') + '"'
        predicate = ' AND search_fts MATCH :external_match' if match else ''
        score = 'CAST(-f.rank * 1000000000 AS INTEGER) + 1' if match else '1'
        text = 't.excerpt' if include_text else 'NULL'
        folded = 'f.body' if any(len(term) < 3 or '\0' in term for term in terms) else 'NULL'
        if thread_filter is None:
            source = 'isolated_search.search_fts f JOIN isolated_search.search_turns t ON t.id=f.rowid'
        else:
            source = 'isolated_search.search_turns t CROSS JOIN isolated_search.search_fts f ON f.rowid=t.id'
            predicate += f' AND t.thread_id IN (SELECT value FROM json_each({thread_filter}))'
        if evidence_only:
            columns = f't.id AS native_id,t.thread_id,t.role,{folded} AS folded,{score} AS native_relevance'
        else:
            columns = (f't.thread_id,t.turn_id,t.item_id,t.role,t.created_at AS timestamp,'
                       f'{text} AS text,{folded} AS folded,t.turn_seq AS position,{score} AS native_relevance')
        return (f'''SELECT {columns}

            FROM {source}
            WHERE 1''' + predicate), match


def search_path(database_path):
    path = Path(database_path).resolve()
    name = 'search.sqlite3' if path.name == 'state.sqlite3' else path.stem + '.search.sqlite3'
    return path.with_name(name)


def active_search(store):
    path = search_path(store.database_path)
    if not path.is_file():
        return None
    # Discovery is strictly read-only and must not initialize a writer or DDL
    # for an incomplete or unsupported index during online migration.
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)
    try:
        db.execute('PRAGMA query_only=ON')
        active = db.execute("SELECT 1 FROM search_control WHERE key='active' AND value='1'").fetchone()
    except sqlite3.OperationalError as error:
        if 'no such table' not in str(error):
            raise
        return None
    finally:
        db.close()
    return SearchStore.for_store(store, initialize=False) if active else None
