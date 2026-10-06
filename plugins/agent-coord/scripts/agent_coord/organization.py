"""Optional thread organization, independent of execution workspaces."""
from __future__ import annotations

import uuid
from pathlib import Path

from .store import CoordinationError

UNSET = object()


def repository_root(cwd: str) -> str | None:
    """Resolve a Git repository and its linked worktrees without invoking Git."""
    directory = Path(cwd).expanduser().resolve()
    for candidate in (directory, *directory.parents):
        marker = candidate / ".git"
        if marker.is_dir():
            return str(candidate)
        if not marker.is_file():
            continue
        try:
            text = marker.read_text().strip()
            if not text.startswith("gitdir:"):
                continue
            gitdir = (candidate / text[7:].strip()).resolve()
            if not gitdir.is_dir():
                continue
            common_file = gitdir / "commondir"
            if common_file.is_file():
                common = (gitdir / common_file.read_text().strip()).resolve()
                return str(common.parent if common.name == ".git" else common)
            return str(candidate)
        except (OSError, UnicodeError, RuntimeError):
            continue
    return None


class OrganizationStore:
    def __init__(self, store):
        self.store = store
        with store._connection() as db:
            # Keep the legacy workspace tables intact for artifact references and
            # clients that were already running when this additive upgrade began.
            db.executescript("""
                CREATE TABLE IF NOT EXISTS work_repositories (
                    id TEXT PRIMARY KEY, root TEXT NOT NULL UNIQUE, name TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS named_projects (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    name_key TEXT NOT NULL UNIQUE
                );
                CREATE TABLE IF NOT EXISTS thread_organization (
                    thread_id TEXT PRIMARY KEY REFERENCES work_threads(thread_id) ON DELETE CASCADE,
                    repository_id TEXT REFERENCES work_repositories(id),
                    project_id TEXT REFERENCES named_projects(id)
                );
                CREATE INDEX IF NOT EXISTS organization_repository_idx
                    ON thread_organization(repository_id);
                CREATE INDEX IF NOT EXISTS organization_project_idx
                    ON thread_organization(project_id);
            """)
        self.backfill()

    def backfill(self):
        """Migrate once per thread, including threads created by older clients."""
        with self.store._connection() as db:
            rows = db.execute("""SELECT t.thread_id, w.root FROM work_threads t
                                 JOIN work_projects w ON w.id = t.project_id
                                 LEFT JOIN thread_organization o ON o.thread_id = t.thread_id
                                 WHERE o.thread_id IS NULL""").fetchall()
            if not rows:
                return
            db.execute("BEGIN IMMEDIATE")
            for row in rows:
                self.ensure_thread(db, row["thread_id"], row["root"])

    @staticmethod
    def ensure_thread(db, thread_id: str, cwd: str):
        if db.execute("SELECT 1 FROM thread_organization WHERE thread_id = ?", (thread_id,)).fetchone():
            return
        root = repository_root(cwd)
        repository_id = None
        if root is not None:
            repository_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "agent-coord:repository:" + root))
            db.execute("INSERT OR IGNORE INTO work_repositories (id, root, name) VALUES (?, ?, ?)",
                       (repository_id, root, Path(root).name or root))
        db.execute("INSERT OR IGNORE INTO thread_organization (thread_id, repository_id) VALUES (?, ?)",
                   (thread_id, repository_id))

    def list(self) -> dict:
        self.backfill()
        with self.store._connection() as db:
            return {
                "repositories": [dict(row) for row in db.execute("SELECT * FROM work_repositories ORDER BY name COLLATE NOCASE, root")],
                "projects": [dict(row) for row in db.execute("SELECT id, name FROM named_projects ORDER BY name_key")],
            }

    @staticmethod
    def describe(db, thread: dict) -> dict:
        """Expose logical associations while preserving legacy artifact context."""
        thread["workspace_id"] = thread.pop("project_id")
        thread["workspace_root"] = thread.pop("project_root")
        association = db.execute("""SELECT o.repository_id, r.name AS repository_name,
                                            r.root AS repository_root, o.project_id,
                                            p.name AS project_name
                                     FROM thread_organization o
                                     LEFT JOIN work_repositories r ON r.id = o.repository_id
                                     LEFT JOIN named_projects p ON p.id = o.project_id
                                     WHERE o.thread_id = ?""", (thread["thread_id"],)).fetchone()
        thread.update(dict(association))
        for link in thread["links"]:
            link["workspace_id"] = link.pop("project_id")
        return thread

    def create_project(self, name: str) -> dict:
        if not isinstance(name, str) or not name.strip() or len(name) > 160:
            raise CoordinationError("Project name must contain 1–160 characters.")
        name = name.strip()
        key = name.casefold()
        with self.store._connection() as db:
            db.execute("INSERT OR IGNORE INTO named_projects (id, name, name_key) VALUES (?, ?, ?)",
                       (str(uuid.uuid4()), name, key))
            return dict(db.execute("SELECT id, name FROM named_projects WHERE name_key = ?", (key,)).fetchone())

    def add_repository(self, path: str) -> dict:
        if not isinstance(path, str) or not path.strip() or len(path) > 4000:
            raise CoordinationError("Choose a Git repository path.")
        if not Path(path).expanduser().is_dir():
            raise CoordinationError("Choose an existing Git repository directory.")
        root = repository_root(path)
        if root is None:
            raise CoordinationError("That directory does not belong to a Git repository.")
        with self.store._connection() as db:
            repository_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "agent-coord:repository:" + root))
            db.execute("INSERT OR IGNORE INTO work_repositories (id, root, name) VALUES (?, ?, ?)",
                       (repository_id, root, Path(root).name or root))
            return dict(db.execute("SELECT * FROM work_repositories WHERE root = ?", (root,)).fetchone())

    @staticmethod
    def validate(db, *, repository_id=UNSET, project_id=UNSET):
        for value, table, label in ((repository_id, "work_repositories", "Repository"),
                                    (project_id, "named_projects", "Project")):
            if value is UNSET or value is None:
                continue
            if not isinstance(value, str) or not db.execute(f"SELECT 1 FROM {table} WHERE id = ?", (value,)).fetchone():
                raise CoordinationError(f"{label} must be a known ID or null.")

    def validate_assignments(self, **values):
        with self.store._connection() as db:
            self.validate(db, **values)

    @staticmethod
    def update(db, thread_id, *, repository_id=UNSET, project_id=UNSET):
        OrganizationStore.validate(db, repository_id=repository_id, project_id=project_id)
        if repository_id is not UNSET:
            db.execute("UPDATE thread_organization SET repository_id = ? WHERE thread_id = ?", (repository_id, thread_id))
        if project_id is not UNSET:
            db.execute("UPDATE thread_organization SET project_id = ? WHERE thread_id = ?", (project_id, thread_id))
