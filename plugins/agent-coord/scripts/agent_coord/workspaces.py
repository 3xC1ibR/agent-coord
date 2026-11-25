"""Workspace choices and directory filters shared by the browser and monitor."""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

from .organization import repository_root
from .store import CoordinationError


def resolve_workspace(
    path: str,
    *,
    workspace: str | None = None,
    extra_workspaces: Iterable[str] = (),
) -> str:
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
        if not matches_any_workspace(str(directory), workspace, extra_workspaces):
            raise CoordinationError("Choose a folder within this UI workspace.")
        return str(directory)
    except CoordinationError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise CoordinationError("This folder is unavailable. Choose an existing folder.") from exc


def matches_workspace(cwd: str, workspace: str | None) -> bool:
    """Include descendants and preserve grouping of a repository's worktrees."""
    if workspace is None:
        return True
    directory = Path(cwd).expanduser().resolve()
    root = Path(workspace).expanduser().resolve()
    if directory.is_relative_to(root):
        return True
    repository = repository_root(str(directory))
    return repository is not None and repository == repository_root(str(root))


def matches_any_workspace(
    cwd: str,
    workspace: str | None,
    extra_workspaces: Iterable[str] = (),
) -> bool:
    """Match the primary UI workspace or any explicitly registered cross-repo root."""
    if workspace is None or matches_workspace(cwd, workspace):
        return True
    return any(matches_workspace(cwd, extra) for extra in extra_workspaces if extra)


def _row_in_workspace(row, workspace: str | None, extra_workspaces: tuple[str, ...]) -> bool:
    if workspace is None:
        return True
    if matches_any_workspace(row["cwd"], workspace, extra_workspaces):
        return True
    if row["group_id"] is not None:
        group_cwd = row["group_cwd"]
        if group_cwd is None or matches_any_workspace(group_cwd, workspace, extra_workspaces):
            return True
    return False


def thread_in_workspace(
    store,
    thread_id: str,
    workspace: str | None,
    *,
    cwd: str | None = None,
    project_cwd: str | None = None,
    extra_workspaces: Iterable[str] = (),
) -> bool:
    """Allow cross-repo threads in registered repos or shared groups while preserving CLI --cwd bounds."""
    del project_cwd
    if workspace is None:
        return True
    extras = tuple(extra_workspaces)
    if cwd is not None and matches_any_workspace(cwd, workspace, extras):
        return True
    with store.reading(), store._connection() as db:
        has_org = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='thread_organization'"
        ).fetchone()
        if has_org:
            row = db.execute(
                """SELECT s.cwd, t.project_cwd, p.id AS group_id, p.cwd AS group_cwd
                   FROM work_threads t
                   JOIN sessions s ON s.session_id = t.thread_id
                   LEFT JOIN thread_organization o ON o.thread_id = t.thread_id
                   LEFT JOIN named_projects p ON p.id = o.project_id
                   WHERE t.thread_id = ?""",
                (thread_id,),
            ).fetchone()
        else:
            row = db.execute(
                """SELECT s.cwd, t.project_cwd, NULL AS group_id, NULL AS group_cwd
                   FROM work_threads t JOIN sessions s ON s.session_id = t.thread_id
                   WHERE t.thread_id = ?""",
                (thread_id,),
            ).fetchone()
    return row is not None and _row_in_workspace(row, workspace, extras)


def require_thread_workspaces(
    store,
    thread_ids,
    workspace,
    *,
    extra_workspaces: Iterable[str] = (),
):
    """Check a bounded set of saved threads without projecting their live state."""
    identities = set(thread_ids)
    if not identities:
        return
    extras = tuple(extra_workspaces)
    with store.reading(), store._connection() as db:
        slots = ",".join("?" for _ in identities)
        has_org = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='thread_organization'"
        ).fetchone()
        if has_org:
            rows = db.execute(
                f"""SELECT t.thread_id, s.cwd, t.project_cwd, p.id AS group_id, p.cwd AS group_cwd
                    FROM work_threads t
                    JOIN sessions s ON s.session_id = t.thread_id
                    LEFT JOIN thread_organization o ON o.thread_id = t.thread_id
                    LEFT JOIN named_projects p ON p.id = o.project_id
                    WHERE t.thread_id IN ({slots})""",
                tuple(identities),
            ).fetchall()
        else:
            rows = db.execute(
                f"""SELECT t.thread_id, s.cwd, t.project_cwd, NULL AS group_id, NULL AS group_cwd
                    FROM work_threads t JOIN sessions s ON s.session_id = t.thread_id
                    WHERE t.thread_id IN ({slots})""",
                tuple(identities),
            ).fetchall()
    if len(rows) != len(identities) or not all(_row_in_workspace(r, workspace, extras) for r in rows):
        raise CoordinationError("Work thread not found in this workspace.")


def workspace_choices(
    cwd: str,
    known: Iterable[str] = (),
    *,
    workspace: str | None = None,
    extra_workspaces: Iterable[str] = (),
) -> list[dict]:
    """Suggest immediate folders and known workspaces without recursively scanning."""
    extras = tuple(extra_workspaces)
    root = Path(cwd).expanduser().resolve()
    candidates = {root, *(Path(path).expanduser() for path in known), *(Path(path).expanduser() for path in extras)}
    try:
        candidates.update(path for path in root.iterdir() if not path.name.startswith("."))
    except OSError:
        pass
    directories = set()
    for candidate in candidates:
        try:
            directory = candidate.resolve()
            if directory.is_dir() and matches_any_workspace(str(directory), workspace, extras):
                directories.add(directory)
        except (OSError, RuntimeError):
            continue
    return [{"cwd": str(path), "name": path.name or str(path)}
            for path in sorted(directories, key=lambda path: (path != root, path.name.casefold(), str(path)))]
