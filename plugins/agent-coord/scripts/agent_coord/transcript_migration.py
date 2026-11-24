"""Owner-only, resumable transcript migration for a compatible runtime rollout.

The server supplies its exact-instance guard after durable runtime commit.
Preparation runs outside browser/admission locks. Only short metadata writes
enter admission; closing admission or losing ownership suspends publication.
The first compatible rollout retains the live coordination database and legacy
history, so rollback sees accepted work in that same current database.
"""
from __future__ import annotations

from contextlib import contextmanager
import logging
import threading

from .transcript_store import TranscriptStore, ensure_schema


class MigrationSuspended(RuntimeError):
    pass


class TranscriptMigrationWorker:
    def __init__(self, sessions, *, owner_guard, search_handshake=None,
                 install_capture=None, interval=5.0, batch_size=16):
        self.sessions = sessions
        self.store = sessions.store
        self.instance_guard = owner_guard
        self.stopped = threading.Event()
        self.interval = interval
        self.batch_size = batch_size
        self.thread = None
        self.phase = "waiting"
        self.error = None
        self.initialized = False
        self.after_identity = None
        self.search_handshake = search_handshake or self._prepare_search
        self.install_capture = install_capture or self._install_capture
        self.transcripts = TranscriptStore(self.store, publication_guard=self.publication_guard,
                                           minimum_free_bytes=2 * 1024 ** 3)

    def owner_guard(self):
        """Cheap boolean guard; the supplied instance check must perform no I/O."""
        from .desktop_runtime import _OWNED_DATABASES
        return (not self.stopped.is_set() and not self.store.read_only
                and str(self.store.database_path) in _OWNED_DATABASES
                and bool(self.instance_guard()))

    @contextmanager
    def publication_guard(self):
        with self.sessions.admission.enter() as allowed:
            if not allowed or not self.owner_guard():
                raise MigrationSuspended("Transcript publication is no longer admitted by this owner")
            yield

    def _install_capture(self, db):
        from .search_publication import install_change_capture
        install_change_capture(db)

    def _prepare_search(self):
        from .search_publication import prepare_search_publication
        return prepare_search_publication(self.store, stopped=self.stopped, owner_guard=self.owner_guard)

    def _inventory(self, *, pending=False, inactive=False, limit=None, after=None):
        with self.store.reading(), self.store._connection() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='browser_history'").fetchone():
                return []
            query = """SELECT h.thread_id FROM browser_history h
                LEFT JOIN transcript_generations g ON g.thread_id=h.thread_id
                LEFT JOIN transcript_migration_verification v ON v.thread_id=h.thread_id
                LEFT JOIN transcript_legacy_revisions r ON r.thread_id=h.thread_id
                WHERE NOT (g.active IS 1 AND g.source_root IS NULL)"""
            if pending:
                query += """ AND (v.generation IS NOT g.generation OR v.thread_id IS NULL
                    OR g.source_root IS NOT 'legacy:' || COALESCE(r.revision,0)
                    OR v.source_revision IS NOT g.source_root)"""
            if inactive:
                query += " AND g.active IS NOT 1"
            parameters = []
            if after is not None:
                query += " AND h.thread_id>?"
                parameters.append(after)
            query += " ORDER BY h.thread_id"
            if limit is not None:
                query += " LIMIT ?"
                parameters.append(limit)
            return [row[0] for row in db.execute(query, parameters)]

    def run_once(self):
        """One bounded inventory batch; public for deterministic qualification."""
        if not self.owner_guard():
            raise MigrationSuspended("Only the current committed owner may migrate transcripts")
        if not self.initialized:
            with self.publication_guard(), self.store.database_operation("transcript.migration.initialize", priority="background"), self.store._connection() as db:
                ensure_schema(db)
                self.install_capture(db)  # Capture concurrent source changes before any baseline copy.
            self.initialized = True
        pending = self._inventory(pending=True, limit=self.batch_size, after=self.after_identity)
        if not pending and self.after_identity is not None:
            self.after_identity = None
            pending = self._inventory(pending=True, limit=self.batch_size)
        if pending:
            self.after_identity = pending[-1]
        self.phase = "copying" if pending else "search"
        for identity in pending:
            if not self.owner_guard():
                raise MigrationSuspended("Transcript migration ownership changed")
            try:
                self.transcripts.migrate(thread_ids=[identity])
            except ValueError as error:
                # A source update can overtake preparation. Its revision stays
                # pending; other independent conversations can still advance.
                self.error = str(error)
        if self._inventory(pending=True, limit=1):
            return False
        if not self.search_handshake():
            self.phase = "search"
            return False
        # The search hook owns activation and durable replay of its source log.
        # Transcript activation is independently fenced and per-thread atomic.
        inactive = self._inventory(inactive=True, limit=self.batch_size)
        if inactive:
            self.transcripts.migrate(thread_ids=inactive, activate=True)
        self.phase = "activating" if self._inventory(inactive=True, limit=1) else "active"
        self.error = None
        return self.phase == "active"

    def prepare_cutover(self, *, fenced_state, search_ready):
        """Validate a stable current generation under the caller's exclusive fence.

        This does not swap/drop a database. The caller must fence every source
        writer, and search_ready must perform a bounded coverage check, not a
        rebuild. A compatible rollback keeps this current database.
        """
        with fenced_state():
            if not self.owner_guard():
                raise MigrationSuspended("Cutover owner changed")
            if self._inventory(pending=True, limit=1) or self._inventory(inactive=True, limit=1):
                raise ValueError("Transcript cutover requires verified current active roots")
            if not search_ready():
                raise ValueError("Search publication has not reached the fenced source generation")
            with self.store.reading(), self.store._connection() as db:
                db.execute("BEGIN")
                counts = db.execute("SELECT count(*),COALESCE(sum(turn_count),0) FROM transcript_generations").fetchone()
                capture = db.execute("SELECT 1 FROM sqlite_master WHERE name='search_publications'").fetchone()
                sequence = db.execute("SELECT COALESCE(max(sequence),0) FROM search_publications").fetchone()[0] if capture else 0
            return {"database": str(self.store.database_path), "threads": counts[0], "turns": counts[1],
                    "search_sequence": sequence, "rollback_uses_current_database": True}

    def _run(self):
        while not self.stopped.is_set():
            try:
                self.run_once()
            except MigrationSuspended:
                self.phase = "waiting"
            except Exception as error:
                self.phase, self.error = "retrying", str(error)
                logging.getLogger(__name__).exception("Transcript migration will retry from durable receipts")
            self.stopped.wait(self.interval)

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, name="transcript-migration", daemon=True)
            self.thread.start()
        return self

    def close(self, *, timeout=1.0):
        """Prevent new publications; the runtime must also drain admitted writes."""
        self.stopped.set()
        if self.thread is not None:
            self.thread.join(timeout)
        return self.thread is None or not self.thread.is_alive()


def start_runtime_migration(sessions, *, owner_guard, **options):
    """Call after durable runtime commit; retain the handle on BrowserSessions."""
    existing = getattr(sessions, "transcript_migration", None)
    if existing is not None and not existing.stopped.is_set():
        return existing
    worker = TranscriptMigrationWorker(sessions, owner_guard=owner_guard, **options)
    if not worker.owner_guard():
        return None
    sessions.transcript_migration = worker.start()
    return worker
