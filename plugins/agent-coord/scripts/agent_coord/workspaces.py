"""Workspace choices and directory filters shared by the browser and monitor."""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

from .organization import repository_root


def matches_workspace(cwd: str, workspace: str | None) -> bool:
    """Include descendants and preserve grouping of a repository's worktrees."""
    if workspace is None:
        return True
    directory = Path(cwd).expanduser().resolve()
    root = Path(workspace).expanduser().resolve()
    repository = repository_root(str(directory))
    return directory.is_relative_to(root) or (repository is not None and repository == repository_root(str(root)))


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
