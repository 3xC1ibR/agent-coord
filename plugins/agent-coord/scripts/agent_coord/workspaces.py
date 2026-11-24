"""Workspace choices and directory filters shared by the browser and monitor."""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

from .organization import repository_root
from .store import CoordinationError


def resolve_workspace(path: str, *, workspace: str | None = None) -> str:
    """Validate an explicitly chosen working folder without starting an agent."""
    if not isinstance(path, str) or not path.strip() or len(path) > 4000 or "\0" in path:
        raise CoordinationError("Choose a working folder.")
    try:
        directory = Path(path).expanduser()
        if not directory.is_absolute():
            raise CoordinationError("Choose an absolute folder path.")
        directory = directory.resolve()
        if not directory.is_dir():
            raise CoordinationError("This folder is unavailable. Choose an existing folder.")
        if not matches_workspace(str(directory), workspace):
            raise CoordinationError("Choose a folder within this UI workspace.")
        return str(directory)
    except CoordinationError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise CoordinationError("This folder is unavailable. Choose an existing folder.") from exc


def matches_workspace(cwd: str, workspace: str | None) -> bool:
    """Include descendants and preserve grouping of a repository's worktrees."""
    if workspace is None or cwd == workspace:
        return True
    directory = Path(cwd).expanduser().resolve()
    root = Path(workspace).expanduser().resolve()
    if directory.is_relative_to(root):
        return True
    repository = repository_root(str(directory))
    return repository is not None and repository == repository_root(str(root))


def require_thread_workspaces(store, thread_ids, workspace, *, exclude_managed=False):
    """Check a bounded set of saved threads without projecting their live state."""
    identities = set(thread_ids)
    with store.reading(), store._connection() as db:
        slots = ",".join("?" for _ in identities)
        browser = any(column[1] == "cwd" for column in db.execute("PRAGMA table_info(browser_sessions)"))
        browser_cwd = "b.cwd" if browser else "NULL"
        join = "LEFT JOIN browser_sessions b ON b.thread_id=t.thread_id" if browser else ""
        managed = """AND NOT EXISTS (SELECT 1 FROM delegations d JOIN work_threads parent
            ON parent.thread_id=d.parent_session_id WHERE d.child_session_id=t.thread_id)""" if exclude_managed else ""
        rows = db.execute(f"""SELECT s.cwd, {browser_cwd} FROM work_threads t
            JOIN sessions s ON s.session_id=t.thread_id {join}
            WHERE t.thread_id IN ({slots}) {managed}""", tuple(identities)).fetchall()
    directories = {directory for row in rows for directory in row if directory is not None}
    if len(rows) != len(identities) or not all(matches_workspace(directory, workspace) for directory in directories):
        raise CoordinationError("Work thread not found in this workspace.")


def directory_roots(directories):
    if not isinstance(directories, list) or len(directories) > 16 or any(
            not isinstance(value, str) or not value or len(value) > 4000 or "\0" in value
            or not Path(value).is_absolute() for value in directories):
        raise CoordinationError("Choose up to 16 absolute directory filters.")
    return list(dict.fromkeys(str(Path(root).resolve()) for root in directories))


def workspace_members(query):
    """Build a view selector independent of server authorization and launch cwd."""
    project = query.get("project", [""])[0]
    location = query.get("project_cwd", [""])[0]
    roots = tuple(map(Path, directory_roots(query.get("directory_root", []))))
    folder = Path(location).expanduser().resolve() if location and project in {"", "__none__"} else None

    def includes(thread):
        if project and thread["project_id"] != (None if project == "__none__" else project):
            return False
        if roots and not any(Path(thread["cwd"]).resolve().is_relative_to(root) for root in roots):
            return False
        paths = (thread["project_cwd"], thread.get("project_launch_cwd"), *thread.get("orchestration_workspaces", ()))
        return folder is None or any(path and Path(path).resolve() == folder for path in paths)

    return includes


def workspace_choices(cwd: str, known: Iterable[str] = (), *, workspace: str | None = None) -> list[dict]:
    """Suggest immediate folders and known workspaces without recursively scanning."""
    root = Path(cwd).expanduser().resolve()
    candidates = {root, *(Path(path).expanduser() for path in known)}
    try:
        candidates.update(path for path in root.iterdir() if not path.name.startswith("."))
    except OSError:
        pass
    directories = set()
    for candidate in candidates:
        try:
            directory = candidate.resolve()
            if directory.is_dir() and matches_workspace(str(directory), workspace):
                directories.add(directory)
        except (OSError, RuntimeError):
            continue
    return [{"cwd": str(path), "name": path.name or str(path)}
            for path in sorted(directories, key=lambda path: (path != root, path.name.casefold(), str(path)))]
