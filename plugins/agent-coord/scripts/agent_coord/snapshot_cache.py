"""Bounded serialized projections invalidated by their durable source writes.

Triggers include previous-build and independent-process writes. Provider output,
visits, heartbeats and other unrelated transactions do not invalidate a saved
message timeline or another thread's usage. Unknown schema changes disable the
cache until the next explicit owner initialization; reads never install schema.
"""
from collections import OrderedDict
import json
import os
import threading

from .transcript_store import encode


def _increment(kind, identities):
    return f"""INSERT INTO snapshot_cache_revisions(kind,identity,revision)
        SELECT '{kind}', identity, 1 FROM ({identities}) WHERE identity IS NOT NULL
        ON CONFLICT(kind,identity) DO UPDATE SET revision=revision+1;"""


def _install(db):
    db.execute("""CREATE TABLE IF NOT EXISTS snapshot_cache_revisions (
        kind TEXT NOT NULL, identity TEXT NOT NULL, revision INTEGER NOT NULL,
        PRIMARY KEY(kind,identity)) WITHOUT ROWID""")
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    def trigger(table, event, suffix, statements, when=None):
        if table not in tables:
            return
        condition = " WHEN " + when if when else ""
        db.execute(f"""CREATE TRIGGER IF NOT EXISTS snapshot_cache_{table}_{event.lower()}_{suffix}
            AFTER {event} ON {table}{condition} BEGIN {statements} END""")

    for event in ("INSERT", "UPDATE", "DELETE"):
        records = ("OLD", "NEW") if event == "UPDATE" else ("OLD" if event == "DELETE" else "NEW",)
        participants = " UNION ".join(f"SELECT {r}.{column} AS identity" for r in records
                                      for column in ("sender_session_id", "recipient_session_id"))
        trigger("messages", event, "timeline", _increment("timeline", participants))
        for table in ("message_delivery_attempts", "message_wake_attempts"):
            ids = ",".join(r + ".message_id" for r in records)
            participants = f"""SELECT sender_session_id AS identity FROM messages WHERE id IN ({ids})
                UNION SELECT recipient_session_id FROM messages WHERE id IN ({ids})"""
            trigger(table, event, "timeline", _increment("timeline", participants))
        exchanges = ",".join(r + ".thread_id" for r in records)
        participants = f"""SELECT sender_session_id AS identity FROM messages WHERE thread_id IN ({exchanges})
            UNION SELECT recipient_session_id FROM messages WHERE thread_id IN ({exchanges})"""
        trigger("message_threads", event, "timeline", _increment("timeline", participants),
                "OLD.closed_at IS NOT NEW.closed_at" if event == "UPDATE" else None)
        creators = " UNION ".join(f"SELECT {r}.sender_session_id AS identity" for r in records)
        trigger("app_control_requests", event, "timeline", _increment("timeline", creators),
                " OR ".join(f"{r}.operation='create'" for r in records))
        for table in ("thread_usage_samples", "thread_usage_baselines", "thread_usage_context",
                      "thread_usage_notes", "thread_usage_epochs"):
            identities = " UNION ".join(f"SELECT {r}.thread_id AS identity" for r in records)
            trigger(table, event, "usage", _increment("usage", identities))
        session_ids = " UNION ".join(f"SELECT {r}.session_id AS identity" for r in records)
        trigger("sessions", event, "usage", _increment("usage", session_ids),
                "OLD.client IS NOT NEW.client OR OLD.session_id IS NOT NEW.session_id" if event == "UPDATE" else None)
        trigger("sessions", event, "timeline", _increment("timeline", "SELECT '*' AS identity"),
                " OR ".join(f"OLD.{col} IS NOT NEW.{col}" for col in ("name", "cwd", "client", "session_id"))
                if event == "UPDATE" else None)
        thread_ids = " UNION ".join(f"SELECT {r}.thread_id AS identity" for r in records)
        trigger("work_threads", event, "usage", _increment("usage", thread_ids),
                "OLD.forked_from_thread_id IS NOT NEW.forked_from_thread_id OR OLD.thread_id IS NOT NEW.thread_id"
                if event == "UPDATE" else None)
        trigger("work_threads", event, "timeline", _increment("timeline", "SELECT '*' AS identity"),
                "OLD.title IS NOT NEW.title OR OLD.thread_id IS NOT NEW.thread_id" if event == "UPDATE" else None)


class SnapshotCache:
    MAX_BYTES = 32 * 1024 * 1024
    MAX_ENTRIES = 64

    def __init__(self, file_identity, schema_version):
        self.file_identity = file_identity
        self.schema_version = schema_version
        self.lock = threading.Lock()
        self.entries = OrderedDict()
        self.bytes = 0
        self.hits = self.misses = self.uncacheable = 0

    def snapshot(self):
        with self.lock:
            return dict(hits=self.hits, misses=self.misses, uncacheable=self.uncacheable,
                        entries=len(self.entries), bytes=self.bytes)

    def _revision(self, store, kind, identity):
        # A rolled-back write can reuse its revision later. Never publish a
        # cache entry from an admitted, uncommitted writer observation.
        if store._writer.owner == threading.get_ident():
            return None
        info = os.stat(store.database_path)
        if (info.st_dev, info.st_ino) != self.file_identity:
            return None
        with store.reading(), store._connection() as db:
            db.execute("BEGIN")
            if db.execute("PRAGMA schema_version").fetchone()[0] != self.schema_version:
                return None
            rows = dict(db.execute("""SELECT identity,revision FROM snapshot_cache_revisions
                WHERE kind=? AND identity IN (?, '*')""", (kind, identity)).fetchall())
        return rows.get("*", 0), rows.get(identity, 0)

    def read(self, store, kind, identity, scope, produce):
        revision = self._revision(store, kind, identity)
        key = kind, identity, scope
        if revision is not None:
            with self.lock:
                cached = self.entries.get(key)
                if cached is not None and cached[0] == revision:
                    self.entries.move_to_end(key)
                    self.hits += 1
                    return cached[1]
        with self.lock:
            self.misses += 1
            self.uncacheable += int(revision is None)
        body = encode(produce())
        # A commit during production may make its snapshot older or newer than
        # the observed revision. Return it normally, but never cache ambiguity.
        if revision is not None and len(body) <= self.MAX_BYTES and self._revision(store, kind, identity) == revision:
            with self.lock:
                previous = self.entries.pop(key, None)
                if previous is not None:
                    self.bytes -= len(previous[1])
                self.entries[key] = revision, body
                self.bytes += len(body)
                while self.bytes > self.MAX_BYTES or len(self.entries) > self.MAX_ENTRIES:
                    _, (_, removed) = self.entries.popitem(last=False)
                    self.bytes -= len(removed)
        return body


def prepare_snapshot_cache(store):
    """Install invalidation inside an explicit runtime-owner startup scope."""
    with store.database_operation("snapshot_cache.schema", priority="background"), store._connection() as db:
        _install(db)
        version = db.execute("PRAGMA schema_version").fetchone()[0]
    info = os.stat(store.database_path)
    store.snapshot_cache = SnapshotCache((info.st_dev, info.st_ino), version)


def read_timeline_json(store, identity, cwd):
    from .message_timeline import message_timeline
    from .store import CoordinationError
    from .workspaces import matches_workspace
    # Keep the normal workspace check even when the body is already serialized.
    with store.reading():
        if not matches_workspace(store.get_session(identity)["cwd"], cwd):
            raise CoordinationError("Conversation not found in this workspace.")
        produce = lambda: message_timeline(store, identity, cwd)
        cache = getattr(store, "snapshot_cache", None)
        return cache.read(store, "timeline", identity, cwd, produce) if cache else encode(produce())


def read_usage(store, identity):
    cache = getattr(store, "snapshot_cache", None)
    if cache is None:
        return store.usage.summary(identity)
    # Decode only the small aggregate. Returning a detached value keeps callers
    # from mutating the cache or another response's usage fields.
    return json.loads(cache.read(store, "usage", identity, None, lambda: store.usage.summary(identity)))
