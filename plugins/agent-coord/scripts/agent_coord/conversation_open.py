"""Bounded, read-only browser conversation snapshots."""
from __future__ import annotations

import copy
import hashlib
import threading
import uuid
from collections import OrderedDict

from .app_control import AppControl
from .history_response import opening_admission
from .history_window import RECENT_TURNS, recent_view
from .message_timeline import message_timeline
from .store import CoordinationError
from .thread_control import ThreadControl
from .transcript_store import TranscriptStore
from .workspaces import require_thread_workspaces

PANEL_LIMIT = 32
MESSAGE_LIMIT = 80
REVISION_CACHE_LIMIT = 256
PANELS = ("asks", "agent_tree", "sidecars", "schedules", "models", "scratchpad")

# These tables have discrete user-visible metadata transitions. Provider item
# deltas use the existing stream and never update this catalog.
CATALOG_FIELDS = {
    "work_threads": ("threads", "thread_id",
                     "attention title pinned quiet snoozed_until seen_checkpoint_id seen_completion_id "
                     "handled_checkpoint_id handled_completion_id project_cwd domain"),
    "thread_checkpoints": ("threads", "thread_id", "phase summary next_actor next_action"),
    "thread_organization": ("threads", "thread_id", "project_id repository_id"),
    "thread_links": ("threads", "thread_id", "label target"),
    "thread_scratchpads": ("scratchpad", "thread_id", "text"),
    "asks": ("asks", "session_id", "status resolution snoozed_until updated_at"),
    "sidecar_watches": ("sidecars", "target_id", "state updated_at"),
    "sidecar_reviews": ("sidecars", "NULL", "completed_at"),
    "scheduled_prompts": ("schedules", "target_thread_id", "state version run_at"),
    "browser_sessions": ("settings", "thread_id", "cwd name model effort yolo permission_mode service_tier archived"),
    "browser_stall_dismissals": ("threads", "thread_id", "dismissed_at"),
    "agent_supervisors": ("threads", "thread_id", "supervisor_session_id"),
    "named_projects": ("organization", "NULL", "cwd name"),
    "saved_views": ("organization", "NULL", "name filters group_by position"),
}
CATALOG_LIMIT = 128


class ConversationOpen:
    def __init__(self, sessions):
        self.sessions = sessions
        self.store = sessions.store
        self.transcripts = TranscriptStore(self.store)
        self.control = AppControl(self.store)
        self.epoch = uuid.uuid4().hex
        stat = self.store.database_path.stat()
        identity = (str(self.store.database_path), stat.st_dev, stat.st_ino, sessions.cwd)
        self.cache_scope = hashlib.sha256(repr(identity).encode()).hexdigest()
        self.lanes = tuple(threading.Lock() for _ in range(64))
        self.revision_lock = threading.Lock()
        self.next_revision = 0
        self.revisions = OrderedDict()

    def initialize_catalog(self):
        """Retain only the last invalidation of each thread and panel."""
        with self.store.database_operation("conversation.catalog.initialize", priority="background"), self.store._connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS conversation_catalog_cursor (
                id INTEGER PRIMARY KEY CHECK(id = 1), revision INTEGER NOT NULL)""")
            db.execute("INSERT OR IGNORE INTO conversation_catalog_cursor VALUES (1, 0)")
            db.execute("""CREATE TABLE IF NOT EXISTS conversation_catalog (
                topic TEXT NOT NULL, thread_id TEXT NOT NULL, revision INTEGER NOT NULL,
                PRIMARY KEY (topic, thread_id)) WITHOUT ROWID""")
            db.execute("CREATE INDEX IF NOT EXISTS conversation_catalog_revision ON conversation_catalog(revision)")
            for table, (topic, column, fields) in CATALOG_FIELDS.items():
                available = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
                if not available:
                    continue
                tracked = [field for field in fields.split() if field in available]
                for operation in ("INSERT", "UPDATE", "DELETE"):
                    when = ""
                    if operation == "UPDATE":
                        if not tracked:
                            continue
                        when = " WHEN " + " OR ".join(f"OLD.{field} IS NOT NEW.{field}" for field in tracked)
                    row = "OLD" if operation == "DELETE" else "NEW"
                    identity = "''" if column == "NULL" else f"COALESCE({row}.{column}, '')"
                    db.execute(f"""CREATE TRIGGER IF NOT EXISTS conversation_catalog_{table}_{operation.lower()}
                        AFTER {operation} ON {table}{when} BEGIN
                        UPDATE conversation_catalog_cursor SET revision = revision + 1 WHERE id = 1;
                        INSERT INTO conversation_catalog VALUES (
                            '{topic}', {identity}, (SELECT revision FROM conversation_catalog_cursor WHERE id = 1)
                        ) ON CONFLICT(topic, thread_id) DO UPDATE SET revision = excluded.revision;
                        END""")

    def catalog_cursor(self):
        with self.store.reading(), self.store._connection() as db:
            return db.execute("SELECT revision FROM conversation_catalog_cursor WHERE id = 1").fetchone()[0]

    def catalog_changes(self, since):
        with self.store.reading(), self.store._connection() as db:
            db.execute("BEGIN")
            revision = db.execute("SELECT revision FROM conversation_catalog_cursor WHERE id = 1").fetchone()[0]
            if since == revision:
                return None
            events = db.execute("""SELECT topic, thread_id FROM conversation_catalog
                WHERE revision > ? ORDER BY revision LIMIT ?""", (since, CATALOG_LIMIT + 1)).fetchall()
        reset = since > revision or len(events) > CATALOG_LIMIT
        thread_ids = set()
        if not reset:
            for row in events:
                if not row["thread_id"]:
                    continue
                try:
                    require_thread_workspaces(self.store, [row["thread_id"]], self.sessions.cwd, exclude_managed=True)
                except CoordinationError:
                    continue
                thread_ids.add(row["thread_id"])
        return {"revision": revision, "threadIds": sorted(thread_ids), "reset": reset,
                "topics": sorted({row["topic"] for row in events})}

    def read(self, thread_id, *, detail_only=False):
        # Serialize observations for the same conversation so an older HTTP
        # request cannot assign a newer revision after a racing observation.
        with (
            opening_admission(self.sessions),
            self.lanes[hash(thread_id) % len(self.lanes)],
            self.store.database_operation("conversation.open", priority="foreground"),
            self.store.reading(),
        ):
            return self._read(thread_id, detail_only=detail_only)

    def _read(self, thread_id, *, detail_only, synchronized=False):
        from .agent_tree import bounded_family

        sessions = self.sessions
        sessions._authorize_history(thread_id)
        projection = getattr(sessions, "projection", None)
        observation = None if synchronized else getattr(projection, "unloaded_observation", None)
        unloaded = observation is not None and thread_id not in observation["excluded"]
        if unloaded:
            # A different provider may hold the state lock while committing.
            # Its staged writes cannot change this conversation's resident view.
            runtime, active_turn = ({}, False, False, None), None
            stream_seq = sessions.thread_stream_sequences.get(thread_id, 0)
            resident_version, history = 0, None
        else:
            with sessions.lock:
                runtime = sessions._thread_runtime_state(thread_id)
                active_turn = sessions.active.get(thread_id)
                stream_seq = sessions.thread_stream_sequences.get(thread_id, 0)
                resident = sessions.histories.get(thread_id)
                resident_version = sessions.history_revisions.get(thread_id, 0)
                history = copy.deepcopy(recent_view(resident, live_turn=active_turn)) if resident is not None else None
                if history is not None:
                    history.update(beforeSeq=history["earlierTurns"],
                                   totalTurns=history["earlierTurns"] + len(history["turns"]))
        family = bounded_family(sessions, thread_id, limit=PANEL_LIMIT, runtime=runtime)
        work = next(thread for thread in family["data"] if thread["thread_id"] == thread_id)
        work = {**work, "close_when_done": ThreadControl(self.store).status(thread_id)}
        work.pop("agent_parent_id", None)
        browser = work["browser_session"]
        record = sessions._record(thread_id) if browser else {
            "thread_id": thread_id, "client": work["client"], "cwd": work["cwd"]}
        version = ("resident", resident_version)
        legacy_required = False
        if history is None:
            indexed = self.transcripts._read(thread_id, limit=RECENT_TURNS, with_checkpoint=True)
            if indexed is not None:
                history = indexed["history"]
                version = ("indexed", indexed["generation"])
            else:
                with self.store._connection() as db:
                    legacy_required = ((not browser and work["can_read_history"]) or db.execute(
                        "SELECT 1 FROM browser_history WHERE thread_id = ?", (thread_id,)).fetchone() is not None)
                history = {"id": thread_id, "turns": [], "view": "recent", "beforeSeq": 0,
                           "earlierTurns": 0, "totalTurns": 0, "historyUnavailable": True}
                version = ("unindexed",)
        history["historyPagination"] = version[0] != "unindexed"
        running = runtime[1] or work["status"] == "running"
        if active_turn is None and running:
            active_turn = work["turn_id"]
        reviews = sessions.reviews.list(thread_id, limit=PANEL_LIMIT) if browser else []
        if reviews:
            history = sessions.reviews.public_history(thread_id, history, runs=reviews)
        messages = message_timeline(self.store, thread_id, sessions.cwd, limit=MESSAGE_LIMIT + 1)
        asks = self.store.asks.overview(status="open", session_id=thread_id, limit=PANEL_LIMIT)
        asks += self.store.asks.overview(status="resolved", session_id=thread_id, limit=PANEL_LIMIT)
        sidecars = {"watches": [], "requests": []}
        if browser:
            sidecars = {"watches": sessions.sidecars.watches.list(thread_id, limit=PANEL_LIMIT),
                        "requests": self.control.sidecar_statuses(thread_id, limit=PANEL_LIMIT)}
        metadata = {
            "session": record, "work_thread": work, "asks": asks, "agent_tree": family, "sidecars": sidecars,
            "schedules": sessions.schedules.list(thread_id=thread_id, limit=PANEL_LIMIT),
            "models": sessions.model_catalogs.get(record["client"]),
            "scratchpad": self.store.threads.scratchpad(thread_id),
            "requests": sessions._public_requests(runtime[0].values()) if browser else [],
            "activeTurn": active_turn, "running": running,
            "acceptedMessages": sessions.submissions.accepted(thread_id, history, active_turn=active_turn) if browser else [],
            "coordinationMessages": messages[-MESSAGE_LIMIT:], "coordinationMessagesTruncated": len(messages) > MESSAGE_LIMIT,
            "dispatchRequests": sessions.dispatch.receipts(thread_id, limit=PANEL_LIMIT) if work["dispatcher"] else [],
            "dispatchSettings": sessions.dispatch.settings(thread_id) if browser else None,
            "reviews": reviews, "queuedMessages": sessions.queue.list(thread_id, limit=PANEL_LIMIT) if browser else [],
            "legacyDetailRequired": legacy_required,
        }
        if unloaded and projection.unloaded_observation is not observation:
            # Re-read under the runtime lock after a publication or rollback
            # rather than allocating a new revision to the stale observation.
            return self._read(thread_id, detail_only=detail_only, synchronized=True)
        history_revision, metadata_revision = self._versions(thread_id, version, metadata)
        if detail_only:
            metadata = {key: value for key, value in metadata.items() if key not in PANELS}
        return {**metadata, "thread" if detail_only else "recent_turns": history,
                "history_revision": history_revision, "stream_seq": stream_seq,
                "metadata_revision": metadata_revision, "deferReadAcknowledgment": legacy_required}

    def _versions(self, thread_id, history, metadata):
        # A clock ticking over does not change an Ask. Compare its durable and
        # actionable fields, which include stale/overdue transitions.
        signature = {**metadata, "asks": [
            {key: value for key, value in ask.items() if key not in {"age_seconds", "idle_seconds"}}
            for ask in metadata["asks"]],
            "queuedMessages": [{key: value for key, value in row.items() if key not in {"images", "message"}}
                               for row in metadata["queuedMessages"]],
            "coordinationMessages": [{key: value for key, value in row.items() if key != "body"}
                                     for row in metadata["coordinationMessages"]]}
        with self.revision_lock:
            previous = self.revisions.pop(thread_id, None)
            if previous is None or previous[0] != history:
                self.next_revision += 1
                history_revision = self.next_revision
            else:
                history_revision = previous[1]
            if previous is None or previous[2] != signature:
                self.next_revision += 1
                metadata_revision = self.next_revision
            else:
                metadata_revision = previous[3]
            self.revisions[thread_id] = (history, history_revision, signature, metadata_revision)
            if len(self.revisions) > REVISION_CACHE_LIMIT:
                self.revisions.popitem(last=False)
            return history_revision, metadata_revision


def open_route_thread(route):
    """Return the encoded thread segment for the one composite route."""
    parts = route.split("/")
    return parts[1] if len(parts) == 3 and parts[0] == "conversations" and parts[1] and parts[2] == "open" else None
