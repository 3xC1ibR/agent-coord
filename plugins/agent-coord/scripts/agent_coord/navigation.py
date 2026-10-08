"""Stable app routes, source-window bindings, and acknowledged UI navigation."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit
import uuid

from .store import CoordinationError
from .views import ViewStore

APP_ID = "com.agentcoord.desktop"


def window_id(value):
    if value is None:
        return None
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise CoordinationError("Invalid source window ID.") from exc


class NavigationStore:
    def __init__(self, store):
        self.store = store
        self.database_id = hashlib.sha256(str(store.database_path.resolve()).encode()).hexdigest()[:24]
        self.views = ViewStore(store)
        with store._connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS ui_turn_windows (
                    session_id TEXT PRIMARY KEY, window_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ui_navigation_requests (
                    id TEXT PRIMARY KEY, route TEXT NOT NULL, status TEXT NOT NULL,
                    window_id TEXT, error TEXT, created_at REAL NOT NULL
                );
            """)

    def origin(self, session_id):
        with self.store._connection() as db:
            row = db.execute("SELECT window_id FROM ui_turn_windows WHERE session_id = ?", (session_id,)).fetchone()
        return row[0] if row else None

    def bind(self, session_id, value):
        value = window_id(value)
        with self.store._connection() as db:
            if value is None:
                db.execute("DELETE FROM ui_turn_windows WHERE session_id = ?", (session_id,))
            else:
                db.execute("INSERT OR REPLACE INTO ui_turn_windows VALUES (?, ?)", (session_id, value))

    @contextmanager
    def sending(self, session_id, value):
        """Publish before RPC so even a fast turn can navigate; roll back failures."""
        previous = self.origin(session_id)
        self.bind(session_id, value)
        try:
            yield
        except Exception:
            self.bind(session_id, previous)
            raise

    @staticmethod
    def _choose(items, value, kind):
        for item in items:
            if item["id"] == value:
                return item
        candidates = [item for item in items if item["name"].casefold() == value.casefold()
                      or item.get("root") == value]
        if len(candidates) == 1:
            return candidates[0]
        if candidates:
            choices = "; ".join(f"{item['id']} ({item.get('root', item['name'])})" for item in candidates)
            raise CoordinationError(f"Ambiguous {kind} {value!r}. Use an ID: {choices}")
        raise CoordinationError(f"Unknown {kind} {value!r}. List available {kind}s first.")

    def link(self, *, project=None, repository=None, view=None, thread=None):
        if (view is not None or thread is not None) and sum(x is not None for x in (project, repository, view, thread)) != 1:
            raise CoordinationError("Choose a saved view, a thread, or overview filters.")
        query = {}
        if view is not None:
            selected = self._choose([{"id": "all", "name": "All work"}, *self.views.list()], view, "view")
            route = {"kind": "view", "id": selected["id"]}
            label = selected["name"]
        elif thread is not None:
            selected = self.store.threads.get(thread)
            route = {"kind": "thread", "id": thread}
            label = selected["title"]
        else:
            organization = self.store.threads.organization.list()
            names = []
            for key, value, items in (("project", project, organization["projects"]),
                                      ("repository", repository, organization["repositories"])):
                if value is None:
                    continue
                if value == "__none__":
                    query[key] = value
                    names.append("No " + key)
                else:
                    # Accept an explicitly given repository path from any working directory.
                    if key == "repository" and (value.startswith(("/", "~", "."))):
                        value = str(Path(value).expanduser().resolve())
                    selected = self._choose(items, value, key)
                    query[key] = selected["id"]
                    names.append(selected["name"])
            route = {"kind": "overview", "filters": query.copy()}
            label = " · ".join(names) or "All work"
        path = route["kind"]
        if "id" in route:
            path += "/" + quote(route["id"], safe="")
        query["database"] = self.database_id
        return {"url": "agentcoord://" + path + "?" + urlencode(query), "route": route, "label": label}

    def resolve(self, value):
        if not isinstance(value, str) or len(value) > 8192 or any(ord(c) < 33 or c == "\\" for c in value):
            raise CoordinationError("Invalid Ribbon Field link.")
        try:
            url = urlsplit(value)
            # Python 3.10 rejects an empty query with strict parsing enabled.
            pairs = parse_qsl(url.query, keep_blank_values=True, strict_parsing=True) if url.query else []
            query = dict(pairs)
            if (url.scheme != "agentcoord" or url.netloc not in {"overview", "view", "thread"}
                    or url.fragment or len(query) != len(pairs)
                    or set(query) - {"project", "repository", "database", "request", "window"}):
                raise ValueError()
        except ValueError as exc:
            raise CoordinationError("Invalid Ribbon Field route.") from exc
        if query.get("database", self.database_id) != self.database_id:
            raise CoordinationError("This link belongs to a different Ribbon Field database.")
        if url.netloc == "overview":
            if url.path not in {"", "/"}:
                raise CoordinationError("Invalid overview route.")
            # Routes contain exact IDs. Never reinterpret a deleted ID as a name.
            org = self.store.threads.organization.list()
            for key, items in (("project", org["projects"]), ("repository", org["repositories"])):
                if key in query and query[key] != "__none__" and not any(i["id"] == query[key] for i in items):
                    raise CoordinationError(f"The linked {key} is unavailable.")
            target = self.link(project=query.get("project"), repository=query.get("repository"))
        else:
            identity = unquote(url.path[1:]) if url.path.startswith("/") else ""
            if not identity or "/" in identity or set(query) & {"project", "repository"}:
                raise CoordinationError("Invalid view or thread route.")
            if url.netloc == "view" and not any(item["id"] == identity for item in [{"id": "all"}, *self.views.list()]):
                raise CoordinationError("The linked view is unavailable.")
            target = self.link(**{url.netloc: identity})
        if "window" in query:
            window_id(query["window"])
        request_id = query.get("request")
        if request_id:
            request = self.request(request_id)
            if request["route"] != target["route"]:
                raise CoordinationError("The navigation request does not match this link.")
        return {**target, "request_id": request_id}

    def request(self, request_id):
        with self.store._connection() as db:
            row = db.execute("SELECT * FROM ui_navigation_requests WHERE id = ?", (request_id,)).fetchone()
        if row is None or row["created_at"] < time.time() - 300:
            raise CoordinationError("This navigation request expired. Open the original link again.")
        result = dict(row)
        result["route"] = json.loads(result["route"])
        return result

    def acknowledge(self, body):
        if set(body) - {"request_id", "status", "window_id", "error"} or body.get("status") not in {"displayed", "failed"}:
            raise CoordinationError("Invalid navigation acknowledgement.")
        request_id = body.get("request_id")
        if not isinstance(request_id, str):
            raise CoordinationError("Choose a navigation request.")
        self.request(request_id)
        window = window_id(body.get("window_id"))
        error = body.get("error")
        if error is not None and (not isinstance(error, str) or len(error) > 2000):
            raise CoordinationError("Invalid navigation error.")
        with self.store._connection() as db:
            db.execute("UPDATE ui_navigation_requests SET status = ?, window_id = ?, error = ? WHERE id = ? AND status = 'requested'",
                       (body["status"], window, error, request_id))
        return self.request(request_id)

    def open(self, target, *, from_session=None, wait=5):
        if not math.isfinite(wait) or not 0 <= wait <= 30:
            raise CoordinationError("Navigation wait must be between 0 and 30 seconds.")
        if sys.platform != "darwin":
            raise CoordinationError("ui open requires macOS with Ribbon Field.app installed. Use ui link to get a link.")
        origin = None
        if from_session:
            self.store.threads.get(from_session)
            origin = self.origin(from_session)
        request_id = str(uuid.uuid4())
        with self.store._connection() as db:
            db.execute("DELETE FROM ui_navigation_requests WHERE created_at < ?", (time.time() - 86400,))
            db.execute("INSERT INTO ui_navigation_requests VALUES (?, ?, 'requested', NULL, NULL, ?)",
                       (request_id, json.dumps(target["route"]), time.time()))
        url = target["url"] + "&" + urlencode({"request": request_id, **({"window": origin} if origin else {})})
        try:
            opened = subprocess.run(["/usr/bin/open", "-b", APP_ID, url], capture_output=True, text=True, timeout=10, check=False)
            if opened.returncode:
                raise CoordinationError("Could not open Ribbon Field.app. Install the app with link support. " + opened.stderr.strip())
        except (OSError, subprocess.TimeoutExpired, CoordinationError) as exc:
            self.acknowledge({"request_id": request_id, "status": "failed", "error": str(exc)[:2000]})
            raise CoordinationError(str(exc)) from exc
        deadline = time.monotonic() + wait
        while True:
            request = self.request(request_id)
            if request["status"] != "requested" or time.monotonic() >= deadline:
                return {**target, "status": request["status"], "request_id": request_id,
                        "window_id": request["window_id"], "error": request["error"]}
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))
