"""Serve committed conversation views during a local coordinator handover."""
from __future__ import annotations

from contextlib import closing, contextmanager, nullcontext
import copy
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace
import weakref
import time
from urllib.parse import unquote
import zlib

from .activity_api import activity_context
from .agent_tree import family_parents, project_family
from .asks import AskStore, MAX_LISTED, _DISPLAY_QUERY, timestamp
from .codex_app_server import BrowserSessions
from .coordinator_projection import unpack
from .desktop_runtime import SCHEMA_VERSION, schema_fingerprint
from .dispatch_runtime import DispatchRuntime
from .execution_state import read_many
from .history_persistence import SavedHistoryWindows, read_history
from .image_inputs import message_images
from .inventory_projection import list_card
from .message_timeline import message_timeline
from .navigation import window_id
from .organization import app_agent_creators, describe_creators, repository_root
from .permission_modes import selected_mode
from .reviews import ReviewRuntime
from .schedules import ScheduledPrompts
from .sidecars import Sidecars
from .store import CoordinationError, CoordinationStore
from .supervision import display_fields, relations
from .thread_control import ThreadControl
from .thread_inventory import ThreadInventory, has_table
from .thread_mentions import thread_mentions
from .thread_search import _snippet
from .threads import ThreadStore
from .views import ViewStore
from .workspaces import matches_workspace, resolve_workspace, workspace_choices, workspace_members


class _PreviousSchemaReader:
    """Narrow observer for shared SQL layouts while an older payload owns schema.

    The full store requires an exact Python-source fingerprint even for queries.
    This reader exposes only committed database access, no schema initializers,
    provider adapters, supervision, reconciliation or application commands.
    """

    clock = staticmethod(time.time)
    reading = staticmethod(nullcontext)

    def __init__(self, path):
        self.database_path = path
        self.threads = ThreadStore(self, initialize=False)
        with self._connection() as db:
            if not has_table(db, "app_agent_stall_incidents"):
                # Build 64 predates durable stall incidents. Project the real
                # pending inbox and leave the unavailable incident field empty.
                self.threads._SELECT = self.threads._SELECT.replace(self.threads.THREAD_FIELDS_SQL, """
                    (SELECT json_object('count', COUNT(*), 'oldest', MIN(created_at)) FROM messages
                     WHERE recipient_session_id=t.thread_id AND classification='action_required'
                     AND delivered_at IS NULL AND suppressed_at IS NULL) AS stall_inbox_json,
                    NULL AS stall_incident_json""")

    @contextmanager
    def _connection(self):
        with closing(sqlite3.connect(self.database_path.as_uri() + "?mode=ro", uri=True, timeout=.5)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            yield db

    def get_session(self, identity):
        with self._connection() as db:
            row = db.execute("SELECT session_id,cwd,client,ended_at FROM sessions WHERE session_id=?", (identity,)).fetchone()
        if row is None:
            raise CoordinationError("Conversation not found.")
        return dict(row)


class _SavedReviews(ReviewRuntime):
    def __init__(self, store):
        # Normal runtime constructors initialize and migrate their feature tables.
        self.store = store


class _SavedSidecars(Sidecars):
    def __init__(self, store):
        self.store = store


class _SavedSchedules(ScheduledPrompts):
    def __init__(self, store, workspace):
        self.store = store
        self.sessions = SimpleNamespace(cwd=workspace)


class _SavedCloseStatus(ThreadControl):
    def __init__(self, store):
        self.store = store


class _RequestText(dict):
    def __init__(self, connection):
        super().__init__()
        self.connection = connection

    def __missing__(self, digest):
        row = self.connection.execute(
            "SELECT compressed FROM coordinator_projection_payloads WHERE digest=?", (digest,)).fetchone()
        if row is None:
            raise CoordinationError("Saved pending request is incomplete.")
        data = zlib.decompress(row[0])
        if hashlib.sha256(data).hexdigest() != digest:
            raise CoordinationError("Saved pending request failed its integrity check.")
        self[digest] = data.decode("utf-8", "surrogatepass")
        return self[digest]


def _packed_field(mapping, name):
    return next((v for k, v in mapping["$map"] if k == name), None)


class _SnapshotReader:
    """Serialize access to one persistent, committed, read-only SQLite snapshot."""

    def __init__(self, database):
        self.database = database
        self.lock = threading.Lock()
        self.connection = self.file_identity = self.finalize = None

    @contextmanager
    def observe(self):
        with self.lock:
            stat = self.database.stat()
            identity = stat.st_dev, stat.st_ino
            if self.file_identity != identity:
                if self.finalize:
                    self.finalize()
                self.connection = sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True,
                                                  timeout=.5, check_same_thread=False)
                self.finalize = weakref.finalize(self, self.connection.close)
                self.connection.row_factory = sqlite3.Row
                self.connection.execute("PRAGMA query_only=ON")
                self.file_identity = identity
            db = self.connection
            db.execute("BEGIN")
            try:
                db.execute("SELECT name FROM sqlite_schema LIMIT 1").fetchone()
                generation = db.execute("PRAGMA data_version").fetchone()[0]
                yield db, (identity, generation)
            finally:
                db.rollback()

    def close(self):
        with self.lock:
            if self.finalize:
                self.finalize()
            self.connection = self.file_identity = self.finalize = None


@dataclass
class _RetainedProjection:
    generation: tuple
    source: tuple | None
    active: dict | None = None
    requests: dict | None = None


class SavedRuntimeViews:
    def __init__(self, database, workspace=None, *, current_directory=None):
        path = Path(database).expanduser().resolve()
        schema = path.parent / ("." + path.name + ".runtime") / "schema.json"
        metadata = json.loads(schema.read_bytes())
        if metadata.get("schema") != SCHEMA_VERSION:
            raise CoordinationError("Unsupported saved-view database schema.")
        if metadata.get("fingerprint") == schema_fingerprint():
            self.store = CoordinationStore(path, read_only=True)
        else:
            self.store = _PreviousSchemaReader(path)
            with self.store._connection() as db:
                row = db.execute("SELECT fingerprint FROM coordination_schema WHERE id=1").fetchone()
                if not row or row[0] != metadata.get("fingerprint"):
                    raise CoordinationError("Saved-view database and schema record disagree.")
        self.cwd = workspace
        self.current_directory = workspace or current_directory or str(Path.cwd())
        self.windows = SavedHistoryWindows()
        self._input_reader = _SnapshotReader(path)
        self._inventory_reader = _SnapshotReader(path)
        self._projection = None
        self._inventories = {}
        self.reviews = _SavedReviews(self.store)

    def _work_thread(self, identity, *, history=False):
        with self.store._connection() as db:
            return self._saved_thread(db, identity, history=history)

    def _saved_thread(self, db, identity, *, history=False):
        thread = self.store.threads.read_thread(db, identity, history=history)
        if not matches_workspace(thread["cwd"], self.cwd):
            raise CoordinationError("Conversation not found in this workspace.")
        thread["open_ask"] = AskStore.open_blocking(db, [identity]).get(identity)
        thread["browser_session"] = db.execute(
            "SELECT 1 FROM browser_sessions WHERE thread_id=?", (identity,)).fetchone() is not None
        thread.update(status="saved", turn_health=None, continuitySnapshot=True)
        return thread

    def _agent_tree(self, identity):
        # Relationships and cards must come from the same committed snapshot.
        with self.store._connection() as db:
            db.execute("BEGIN")
            selected = self._saved_thread(db, identity)
            creators = app_agent_creators(db)
            includes = self._workspace_filter()
            describe_creators(creators, includes)
            supervision = relations(db)
            parents = family_parents(db, creators, supervision["overrides"])

            def card(thread):
                key = thread["thread_id"]
                creator = creators.get(key)
                thread.update(created_by=creator, created_by_thread_id=creator["thread_id"] if creator else None,
                              creation_mode=creator["mode"] if creator else None,
                              orchestration_workspaces=creator["workspaces"] if creator else [],
                              **display_fields(key, creator, supervision))
                thread["forked_from"] = None
                source_id = thread["forked_from_thread_id"]
                if source_id:
                    source = db.execute("""SELECT t.title,s.cwd FROM work_threads t JOIN sessions s
                        ON s.session_id=t.thread_id WHERE t.thread_id=?""", (source_id,)).fetchone()
                    if source and includes(source["cwd"]):
                        thread["forked_from"] = {"thread_id": source_id, "title": source["title"]}
                return thread

            graph = project_family(card(selected), parents, lambda key: card(self._saved_thread(db, key)))
            return {**graph, "continuitySnapshot": True}

    def close(self):
        self._input_reader.close()
        self._inventory_reader.close()
        self._projection = None
        self._inventories.clear()

    def _retained_input(self, db, generation, identity):
        cached = self._projection
        if cached is None or cached.generation != generation:
            metadata = (db.execute("SELECT version,length(state) FROM coordinator_projection WHERE singleton=1").fetchone()
                        if has_table(db, "coordinator_projection") else None)
            if cached is not None and cached.generation[0] != generation[0]:
                cached = None
            source = browser = None
            if metadata is not None and metadata[0] in (1, 2, 3):
                version, size = metadata
                # The signed writer places the browser map immediately after
                # the version. Old projections can then contain tens of MB of
                # unrelated Claude tool output. Read just the actual browser
                # map when it fits the bounded prefix of this committed blob.
                if size > 65536 and hasattr(db, "blobopen"):
                    with db.blobopen("coordinator_projection", "state", 1, readonly=True) as stream:
                        prefix = stream.read(65536)
                    source = ("prefix", version, prefix)
                    if cached is None or source != cached.source:
                        raw = zlib.decompressobj().decompress(prefix, max_length=512*1024)
                        header = '{"$map":[["version",' + str(version) + '],["browser",'
                        if raw.startswith(header.encode()):
                            try:
                                text = raw.decode("ascii")
                                value, end = json.JSONDecoder().raw_decode(text, len(header))
                                if text[end:end+2] == "]," and isinstance(value, dict) and "$map" in value:
                                    browser = value
                            except (UnicodeError, ValueError):
                                pass
                        if browser is None:
                            source = None
                if source is None:
                    blob = db.execute("SELECT state FROM coordinator_projection WHERE singleton=1").fetchone()[0]
                    source = ("complete", version, blob)
                    if cached is None or source != cached.source:
                        browser = _packed_field(json.loads(zlib.decompress(blob)), "browser")
            if cached is not None and cached.source == source:
                cached.generation = generation
            else:
                cached = _RetainedProjection(generation, source)
                if browser is not None:
                    cached.active = unpack(_packed_field(browser, "active"))
                    requests = unpack(_packed_field(browser, "requests"), _RequestText(db))
                    cached.requests = {}
                    for request in BrowserSessions._public_requests(requests.values()):
                        cached.requests.setdefault(request["params"].get("threadId"), []).append(request)
                self._projection = cached
        if cached.active is None:
            return None, None, None
        return cached.active.get(identity), identity in cached.active, cached.requests.get(identity, [])

    def _member(self, db, identity):
        if not isinstance(identity, str) or not identity or len(identity) > 200 or "\0" in identity:
            raise CoordinationError("Invalid conversation ID.")
        row = db.execute("""SELECT s.cwd,s.client,b.cwd AS browser_cwd,b.client AS browser_client,
            b.archived,t.attention,t.turn_id,t.turn_key FROM work_threads t
            JOIN sessions s ON s.session_id=t.thread_id
            JOIN browser_sessions b ON b.thread_id=t.thread_id WHERE t.thread_id=?""", (identity,)).fetchone()
        if (row is None or row["browser_client"] != row["client"] or not matches_workspace(row["cwd"], self.cwd)
                or not matches_workspace(row["browser_cwd"], self.cwd)):
            raise CoordinationError("App conversation not found in this workspace.")
        return row

    def authorize_thread(self, thread_id):
        """Check selected-thread receipt visibility, including archived threads."""
        with self.store.reading(), self.store._connection() as db:
            self._member(db, thread_id)
        return True

    def _composer_mentions(self, db, body):
        refs = body.get("mentions", [])
        if not isinstance(refs, list) or len(refs) > 50:
            raise CoordinationError("Choose at most 50 thread references.")
        if not refs:
            return
        identities = [r["session_id"] for r in refs if isinstance(r, dict) and isinstance(r.get("session_id"), str)]
        rows = db.execute("""SELECT t.thread_id,s.cwd FROM json_each(?) selected
            JOIN work_threads t ON t.thread_id=selected.value JOIN sessions s ON s.session_id=t.thread_id
            WHERE NOT EXISTS(SELECT 1 FROM delegations d JOIN work_threads parent ON parent.thread_id=d.parent_session_id
                             WHERE d.child_session_id=t.thread_id)""", (json.dumps(identities),))
        visible = [dict(r) for r in rows if matches_workspace(r["cwd"], self.cwd)]
        # The existing validator checks the exact UTF-16 spans and file scope.
        # Only the selected references can matter to its membership lookup.
        selector = SimpleNamespace(cwd=self.cwd, list_work_threads=lambda *, archived: [] if archived else visible)
        thread_mentions(body["message"], body, selector)

    def authorize_composer(self, thread_id, body, *, receiver_owns_turn=False, legacy_delivery=False):
        """Admit ordinary composer input. The coordinator repeats its own checks on delivery."""
        if not isinstance(body, dict) or set(body) - {
                "message", "images", "mentions", "requestId", "expectedTurnId", "activity", "windowId"}:
            raise CoordinationError("Only ordinary conversation messages can be queued during an update.")
        for key in ("requestId", "expectedTurnId"):
            if key == "requestId" or key in body:
                value = body.get(key)
                if not isinstance(value, str) or not value.strip() or len(value) > 200 or "\0" in value:
                    raise CoordinationError("A valid submission and expected-turn identity are required.")
        message, _ = message_images(body)
        if message.startswith("/"):
            raise CoordinationError("Conversation commands require the serving coordinator.")
        activity_context(body)
        window_id(body.get("windowId"))
        with self._input_reader.observe() as (db, version):
            member = self._member(db, thread_id)
            if member["archived"] or member["attention"] == "archived":
                raise CoordinationError("Reopen this conversation before sending.")
            if has_table(db, "dispatch_settings") and db.execute(
                    "SELECT 1 FROM dispatch_settings WHERE thread_id=? AND enabled!=0", (thread_id,)).fetchone():
                raise CoordinationError("Routed messages require the dispatch coordinator.")
            if has_table(db, "review_runs") and self.reviews.active(thread_id, db):
                raise CoordinationError("Reviewed conversations require the review coordinator.")
            if has_table(db, "thread_close_requests") and db.execute(
                    "SELECT 1 FROM thread_close_requests WHERE thread_id=? AND status IN ('queued','closing')",
                    (thread_id,)).fetchone():
                raise CoordinationError("This conversation has a pending close request.")
            active, running, requests = self._retained_input(db, version, thread_id)
            if requests is None:
                raise CoordinationError("Conversation input state is unavailable during the update.")
            if requests or AskStore.open_blocking(db, [thread_id]):
                raise CoordinationError("A decision is pending. Answer it in the conversation.")
            # The receipt-first native receiver can prove whether the exact
            # targeted turn just finished successfully and hand off once.
            # An ordinary draft has no turn target. The durable queue can
            # accept it while a native start awaits its ID. The legacy HTTP
            # delivery waits for that start before attempting transport.
            if ((legacy_delivery and running and not active) or
                    (not receiver_owns_turn and "expectedTurnId" in body and body["expectedTurnId"] != active)):
                raise CoordinationError("The active turn changed. Refresh before sending.")
            if running and member["client"] not in {"codex", "claude"}:
                raise CoordinationError("This provider needs its serving coordinator to receive active-turn input.")
            self._composer_mentions(db, body)
        return True

    def _session(self, identity, recent, controls):
        thread = self._work_thread(identity)
        with self.store._connection() as db:
            db.execute("BEGIN")
            row = db.execute("SELECT * FROM browser_sessions WHERE thread_id=?", (identity,)).fetchone()
            if row and not matches_workspace(row["cwd"], self.cwd):
                raise CoordinationError("Conversation not found in this workspace.")
            session = dict(row) if row else {k: thread[k] for k in ("thread_id", "cwd", "client")}
            if row:
                session["permission_mode"] = selected_mode(session)
            queued = []
            if has_table(db, "browser_message_queue"):
                for entry in db.execute("SELECT * FROM browser_message_queue WHERE thread_id=? ORDER BY created_at,rowid", (identity,)):
                    item = dict(entry)
                    item.pop("owner", None)
                    for name in ("images", "mentions"):
                        item[name] = json.loads(item.pop(name + "_json", "[]"))
                    queued.append(item)
            dispatch_receipts = []
            if has_table(db, "dispatch_requests"):
                dispatch_receipts = [DispatchRuntime.receipt(row) for row in db.execute(
                    "SELECT * FROM dispatch_requests WHERE dispatcher_id=? ORDER BY id", (identity,))]
            dispatch_settings = {"enabled": False, "defaults": {}}
            if has_table(db, "dispatch_settings"):
                dispatch = db.execute("SELECT enabled,defaults_json FROM dispatch_settings WHERE thread_id=?",
                                      (identity,)).fetchone()
                if dispatch:
                    dispatch_settings = {"enabled": bool(dispatch[0]), "defaults": json.loads(dispatch[1])}
            reviews_available = has_table(db, "review_runs")
        with self._input_reader.observe() as (db, generation):
            active, running, requests = self._retained_input(db, generation, identity)
            requests = copy.deepcopy(requests)
        history = read_history(self.store, identity, windows=self.windows if recent else None, live_turn=active)
        if history is None:
            history = {"id": identity, "turns": [], "historyUnavailable": True}
        if reviews_available:
            history = self.reviews.public_history(identity, history)
        if controls:
            thread["close_when_done"] = _SavedCloseStatus(self.store).status(identity)
        if requests:
            thread["status"] = "needs input"
        elif running:
            thread["status"] = "running"
        return {"session": session, "thread": history, "work_thread": thread, "running": running,
                "activeTurn": active, "requests": requests or [], "requestsAvailable": requests is not None,
                "coordinationMessages": message_timeline(self.store, identity, self.cwd),
                "dispatchRequests": dispatch_receipts, "dispatchSettings": dispatch_settings,
                "queuedMessages": queued, "reviews": self.reviews.list(identity) if reviews_available else [],
                "continuitySnapshot": True}

    def _threads(self, archived):
        # Keep the expensive immutable batches from the same committed generation.
        # Time-dependent execution health, workspace membership and due snoozes
        # are reevaluated on each request. This reader never blocks the composer.
        with self._inventory_reader.observe() as (db, generation):
            cached = self._inventories.get(archived)
            if cached is None or cached[0] != generation:
                inventory = ThreadInventory(self.store.threads, db, archived=archived, cards_only=True)
                delegated = {r[0] for r in db.execute("""SELECT d.child_session_id FROM delegations d
                    JOIN work_threads t ON t.thread_id=d.parent_session_id WHERE d.child_session_id IS NOT NULL""")}
                browser = {r[0] for r in db.execute("SELECT thread_id FROM browser_sessions")}
                asks = AskStore.open_blocking(db)
                self._inventories[archived] = generation, inventory, delegated, browser, asks
            else:
                _, inventory, delegated, browser, asks = cached
                inventory.executions = read_many(self.store, db, [r["thread_id"] for r in inventory.rows])
            cards = []
            includes = self._workspace_filter()
            for row in inventory.rows:
                identity = row["thread_id"]
                if identity in delegated or not includes(row["cwd"]):
                    continue
                card = list_card(inventory.read(db, row))
                card.update(browser_session=identity in browser, status="saved", turn_health=None,
                            open_ask=asks.get(identity), continuitySnapshot=True)
                cards.append(card)
        return cards

    @staticmethod
    def _indexed_matches(db, identity, terms, user_only):
        # Seek one committed head and its newest eight immutable document
        # headers. Never backfill, scan raw archives, or use a stale head.
        head = db.execute("""SELECT h.generation FROM conversation_search_heads h
            JOIN conversation_search_revisions r ON r.thread_id=h.thread_id
            WHERE h.thread_id=? AND h.revision=r.revision AND r.indexed_revision=r.revision""",
            (identity,)).fetchone()
        if head is None:
            return []
        rows = db.execute("""SELECT e.turn_id,e.item_id,e.role,e.timestamp,substr(d.excerpt,1,1024) AS excerpt
            FROM conversation_search_document_entries e CROSS JOIN conversation_search_documents d
            ON d.document=e.document WHERE e.generation=? ORDER BY e.position DESC LIMIT 8""", (head[0],))
        return [dict(row, excerpt=_snippet(row["excerpt"], terms, 320)) for row in rows
                if (not user_only or row["role"] == "user") and all(term in row["excerpt"].casefold() for term in terms)][:3]

    def _search(self, query):
        # The ordinary search path can backfill, migrate or persist indexes.
        # Keep this explicitly limited to committed routing metadata.
        phrase = query.get("q", [""])[0].strip().casefold()
        if not phrase or len(phrase) > 500:
            raise CoordinationError("Search query must contain 1–500 characters.")
        if query.get("cursor"):
            raise CoordinationError("Restart search after the coordinator resumes to read another page.")
        user_only = query.get("my_messages") == ["true"]
        scope, show = query.get("scope", ["all"])[0], query.get("show", ["active"])[0]
        if scope not in {"all", "current", "all_projects"}:
            raise CoordinationError("Invalid search scope.")
        terms = phrase.split()
        items = []
        inspected = 0
        filters = {key: val for key, val in query.items() if key == "directory_root"
                   or key in {"project_cwd", "project"} and scope != "all_projects"}
        includes = workspace_members(filters)
        with self.store._connection() as db:
            db.execute("BEGIN")
            indexed = all(has_table(db, table) for table in (
                "conversation_search_heads", "conversation_search_revisions",
                "conversation_search_document_entries", "conversation_search_documents"))
            rows = db.execute("""SELECT t.thread_id,t.title,t.attention,t.original_request,t.project_cwd,
                s.cwd,s.client,c.summary,c.phase,c.next_action,c.next_actor,o.project_id,o.repository_id,
                b.thread_id IS NOT NULL AS app_conversation, b.cwd AS browser_cwd
                FROM work_threads t JOIN sessions s ON s.session_id=t.thread_id
                LEFT JOIN browser_sessions b ON b.thread_id=t.thread_id
                LEFT JOIN thread_organization o ON o.thread_id=t.thread_id
                LEFT JOIN thread_checkpoints c ON c.id=(SELECT MAX(id) FROM thread_checkpoints WHERE thread_id=t.thread_id)
                WHERE NOT EXISTS (SELECT 1 FROM delegations d JOIN work_threads p ON p.thread_id=d.parent_session_id
                                  WHERE d.child_session_id=t.thread_id)
                ORDER BY t.updated_at DESC,t.thread_id""")
            creators = {}
            if filters.get("project_cwd", [""])[0] and not filters.get("project", [""])[0]:
                creators = app_agent_creators(db)
                describe_creators(creators, lambda cwd: matches_workspace(cwd, self.cwd))
            for row in rows:
                if (not matches_workspace(row["cwd"], self.cwd) or
                        not matches_workspace(row["browser_cwd"] or row["cwd"], self.cwd)):
                    continue
                if scope == "current":
                    if show in {"attention", "completed"}:
                        raise CoordinationError("Live attention search is available when the coordinator resumes.")
                    placement = {"archived": "archived", "later": "later"}.get(show, "now")
                    if row["attention"] != placement or query.get("phase", [""])[0] not in ("", row["phase"]):
                        continue
                    if "repository" in query and row["repository_id"] != (
                            None if query["repository"][0] == "__none__" else query["repository"][0]):
                        continue
                item = dict(row)
                item.pop("browser_cwd")
                item["project_cwd"] = item["project_cwd"] or item["cwd"]
                item["orchestration_workspaces"] = creators.get(item["thread_id"], {}).get("workspaces", ())
                if not includes(item):
                    continue
                context = "\n".join(row[k] or "" for k in ("title", "original_request", "summary")).casefold()
                context_match = not user_only and all(term in context for term in terms)
                matches = []
                if indexed and inspected < 64:
                    inspected += 1
                    matches = self._indexed_matches(db, row["thread_id"], terms, user_only)
                if context_match or matches:
                    item.update(original_request=_snippet(row["original_request"], terms, 240),
                                summary=_snippet(row["summary"], terms, 320), matches=matches, context_match=context_match,
                                message_count=len(matches))
                    items.append(item)
                    if len(items) == 20:
                        break
        return {"items": items, "next_cursor": None, "coverage": {
            "scope": "Saved routing metadata and up to 1,024 characters of the eight latest indexed messages "
                     "from each of the first 64 eligible conversations. Up to 20 results during the update.",
            "indexed_threads_checked": inspected, "partial": True}, "continuitySnapshot": True}

    def _workspace_filter(self):
        # Membership can require resolving a linked Git worktree. Multiple rows
        # in a single result commonly refer to the same project directory.
        @lru_cache(maxsize=None)
        def includes(directory):
            return self.cwd is None or bool(directory and matches_workspace(directory, self.cwd))
        return includes

    def _saved_views(self, query):
        selected = None
        if "cwd" in query:
            values = query["cwd"]
            if len(values) != 1:
                raise CoordinationError("Select one project directory.")
            if values[0]:
                selected = resolve_workspace(values[0], workspace=self.cwd)
        includes = self._workspace_filter()
        with self.store._connection() as db:
            if not has_table(db, "saved_views"):
                raise KeyError("views")
            rows = db.execute("SELECT * FROM saved_views ORDER BY position,id")
            items = []
            for row in rows:
                directory = row["cwd"] if "cwd" in row.keys() else None
                if includes(directory) and ("cwd" not in query or directory == selected):
                    items.append({**ViewStore._decode(row), "cwd": directory})
        return {"data": items, "continuitySnapshot": True}

    def _organization(self):
        result = self.store.threads.organization.list()
        includes = self._workspace_filter()
        return {"repositories": [r for r in result["repositories"] if includes(r["root"])],
                "projects": [p for p in result["projects"] if includes(p["cwd"])],
                "continuitySnapshot": True}

    def _workspaces(self):
        with self.store._connection() as db:
            known = [r[0] for r in db.execute("SELECT DISTINCT cwd FROM sessions UNION SELECT root FROM work_projects")]
        return {"data": workspace_choices(self.current_directory, known, workspace=self.cwd), "continuitySnapshot": True}

    def open_workspace(self, body):
        """Restore a real local folder if opening it does not require registering a repository."""
        if not isinstance(body, dict) or set(body) != {"path"}:
            raise CoordinationError("Opening a folder accepts a path.")
        directory = resolve_workspace(body["path"], workspace=self.cwd)
        root = repository_root(directory)
        repository = None
        if root is not None:
            with self.store._connection() as db:
                row = db.execute("SELECT * FROM work_repositories WHERE root=?", (root,)).fetchone()
            if row is None:
                raise CoordinationError("Register this repository when the coordinator resumes.")
            repository = dict(row)
        return {"cwd": directory, "repository": repository, "continuitySnapshot": True}

    def _asks(self, query):
        status = (query.get("status") or ["open"])[-1] or "open"
        if status not in {"open", "resolved", "withdrawn", "all"}:
            raise CoordinationError("Ask status must be open, resolved, withdrawn, or all.")
        since = timestamp((query.get("since") or [None])[-1] or None, "Since")
        asks = AskStore(self.store, initialize=False)
        clause, args = asks._status_clause(status)
        conditions = [clause] if clause else []
        if since is not None:
            conditions.append("a.created_at>=?")
            args.append(since)
        with self.store._connection() as db:
            if not has_table(db, "asks"):
                raise KeyError("asks")
            if self.cwd is not None:
                db.create_function("saved_workspace_contains", 1, self._workspace_filter())
                conditions.append("saved_workspace_contains(s.cwd)")
            where = " WHERE " + " AND ".join(conditions) if conditions else ""
            order = (" ORDER BY a.blocking DESC,a.created_at,a.id" if status == "open" else
                     f" ORDER BY a.updated_at DESC,a.id DESC LIMIT {MAX_LISTED}")
            now = self.store.clock()
            items = [asks._display(row, now) for row in db.execute(_DISPLAY_QUERY + where + order, args)]
        return {"data": items, "continuitySnapshot": True}

    def get(self, route, query=None, *, recent=False, thread_controls=False):
        query = query or {}
        if route == "views":
            return self._saved_views(query)
        if route == "organization":
            return self._organization()
        if route == "workspaces":
            return self._workspaces()
        if route == "workspaces/scratchpad":
            directory = resolve_workspace(query.get("path", [None])[0], workspace=self.cwd)
            return self.store.threads.project_scratchpad(directory)
        if route == "asks":
            return self._asks(query)
        if route == "schedules":
            with self.store._connection() as db:
                if not has_table(db, "scheduled_prompt_runs") or not has_table(db, "scheduled_prompts"):
                    raise KeyError(route)
            return {"data": _SavedSchedules(self.store, self.cwd).list(), "continuitySnapshot": True}
        if route == "threads":
            return {"data": self._threads(query.get("archived") == ["true"])}
        if route == "search":
            return self._search(query)
        segments = route.split("/")
        if len(segments) not in (2, 3) or not segments[1]:
            raise KeyError(route)
        identity = unquote(segments[1], errors="strict")
        if len(segments) == 2 and segments[0] == "sessions":
            return self._session(identity, recent, thread_controls)
        if segments[0] != "threads":
            raise KeyError(route)
        if segments[2:] == ["agent-tree"]:
            return self._agent_tree(identity)
        if segments[2:] == ["scratchpad"]:
            with self.store._connection() as db:
                row = db.execute("""SELECT s.cwd,p.text FROM work_threads t JOIN sessions s ON s.session_id=t.thread_id
                    LEFT JOIN thread_scratchpads p ON p.thread_id=t.thread_id WHERE t.thread_id=?""", (identity,)).fetchone()
                if not row or not matches_workspace(row["cwd"], self.cwd):
                    raise CoordinationError("Work thread not found in this workspace.")
                return {"text": row["text"] or ""}
        thread = self._work_thread(identity, history=len(segments) == 2)
        if len(segments) == 2:
            thread["close_when_done"] = _SavedCloseStatus(self.store).status(identity)
            return thread
        if segments[2] == "sidecars":
            with self.store._connection() as db:
                watches = _SavedSidecars(self.store).list(identity) if has_table(db, "sidecar_watches") else []
                requests = []
                if has_table(db, "app_control_requests"):
                    for row in db.execute("""SELECT request_id,operation,status,thread_id,error,result_json
                        FROM app_control_requests WHERE operation='sidecar' AND status NOT IN ('completed','cancelled')
                        AND json_extract(payload_json,'$.target_id')=? ORDER BY created_at,request_id""", (identity,)):
                        item = dict(row)
                        item["result"] = json.loads(item.pop("result_json") or "null")
                        if item["status"] == "queued_sidecar":
                            item["status"] = "queued"
                        requests.append(item)
                return {"watches": watches, "requests": requests, "continuitySnapshot": True}
        raise KeyError(route)
