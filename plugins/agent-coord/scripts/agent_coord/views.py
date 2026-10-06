"""Named overview filters shared by every UI using the coordination database."""
from __future__ import annotations

import json
import uuid

from .store import CoordinationError

DEFAULT_FILTERS = {"repository": "", "project": "", "phase": "", "show": "active", "search": ""}
PHASES = {"", "discussion", "investigation", "planning", "implementation", "validation", "deployment", "finished"}
GROUPS = {"phase", "repository", "project", "none"}


class ViewStore:
    def __init__(self, store):
        self.store = store
        with store._connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS saved_views (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, filters TEXT NOT NULL,
                group_by TEXT NOT NULL, position INTEGER NOT NULL,
                version INTEGER NOT NULL DEFAULT 1
            )""")

    @staticmethod
    def _decode(row):
        result = dict(row)
        result["filters"] = json.loads(result["filters"])
        return result

    def list(self):
        with self.store._connection() as db:
            return [self._decode(row) for row in db.execute("SELECT * FROM saved_views ORDER BY position, id")]

    @staticmethod
    def _get(db, view_id):
        row = db.execute("SELECT * FROM saved_views WHERE id = ?", (view_id,)).fetchone()
        if row is None:
            raise CoordinationError("This view no longer exists. Choose another view.")
        return row

    @staticmethod
    def _version(row, body):
        if type(body.get("version")) is not int or body["version"] != row["version"]:
            raise CoordinationError("This view changed in another window. Reset the view and try again.")

    @staticmethod
    def _validate(db, body, view_id=None):
        name = body.get("name")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 160:
            raise CoordinationError("View names must contain 1–160 characters.")
        name = name.strip()
        if name.casefold() == "all work" or any(
            row["id"] != view_id and row["name"].casefold() == name.casefold()
            for row in db.execute("SELECT id, name FROM saved_views")
        ):
            raise CoordinationError("A view with that name already exists. Choose another name.")
        filters = body.get("filters", {})
        if not isinstance(filters, dict) or set(filters) - DEFAULT_FILTERS.keys():
            raise CoordinationError("Choose supported view filters.")
        filters = {**DEFAULT_FILTERS, **filters}
        if any(not isinstance(value, str) for value in filters.values()):
            raise CoordinationError("View filters must be strings.")
        if filters["show"] not in {"active", "attention", "completed", "archived"} or filters["phase"] not in PHASES:
            raise CoordinationError("Choose a supported thread status and phase.")
        if len(filters["search"]) > 1000:
            raise CoordinationError("View searches must be at most 1000 characters.")
        filters["search"] = filters["search"].strip()
        for key, table in (("repository", "work_repositories"), ("project", "named_projects")):
            selected = filters[key]
            if selected not in {"", "__none__"} and not db.execute(
                f"SELECT 1 FROM {table} WHERE id = ?", (selected,)
            ).fetchone():
                raise CoordinationError(f"Choose an existing {key} for this view.")
        group = body.get("group_by", "phase")
        if not isinstance(group, str) or group not in GROUPS:
            raise CoordinationError("Choose a supported view grouping.")
        return name, json.dumps(filters), group

    def create(self, body):
        if set(body) - {"name", "filters", "group_by"}:
            raise CoordinationError("View creation accepts a name, filters, and grouping.")
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            name, filters, group = self._validate(db, body)
            view_id = str(uuid.uuid4())
            position = db.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM saved_views").fetchone()[0]
            db.execute("INSERT INTO saved_views (id, name, filters, group_by, position) VALUES (?, ?, ?, ?, ?)",
                       (view_id, name, filters, group, position))
            return self._decode(self._get(db, view_id))

    def update(self, view_id, body):
        if set(body) - {"name", "filters", "group_by", "version"} or not set(body) - {"version"}:
            raise CoordinationError("View updates accept a name, filters, grouping, and current version.")
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._get(db, view_id)
            self._version(current, body)
            name, filters, group = self._validate(db, {**self._decode(current), **body}, view_id)
            db.execute("UPDATE saved_views SET name = ?, filters = ?, group_by = ?, version = version + 1 WHERE id = ?",
                       (name, filters, group, view_id))
            return self._decode(self._get(db, view_id))

    def delete(self, view_id, body):
        if set(body) != {"version"}:
            raise CoordinationError("Deleting a view requires its current version.")
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._version(self._get(db, view_id), body)
            db.execute("DELETE FROM saved_views WHERE id = ?", (view_id,))

    def move(self, view_id, body):
        if set(body) != {"direction"} or body["direction"] not in ("left", "right"):
            raise CoordinationError("Choose left or right to move the view.")
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._get(db, view_id)
            ordered = [row["id"] for row in db.execute("SELECT id FROM saved_views ORDER BY position, id")]
            index = ordered.index(view_id)
            neighbor = index + (-1 if body["direction"] == "left" else 1)
            if 0 <= neighbor < len(ordered):
                ordered[index], ordered[neighbor] = ordered[neighbor], ordered[index]
                db.executemany("UPDATE saved_views SET position = ? WHERE id = ?", enumerate(ordered))
