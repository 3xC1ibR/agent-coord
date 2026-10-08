"""Read-only workspace files for native and future remote clients.

Paths in the protocol are relative, slash-separated names; only the server
interprets its filesystem paths. Authentication belongs to the HTTP boundary.
This is a browsing boundary, not a sandbox for code running as the same user.
"""
from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import stat

from .store import CoordinationError

MAX_ENTRIES = 5000
MAX_PREVIEW_BYTES = 128 * 1024


def workspace_files(operation, body, workspace_root=None):
    if operation not in {"list", "preview"} or set(body) - {"root", "path", "hidden"}:
        raise CoordinationError("Unknown file operation or fields.")
    root, relative = body.get("root"), body.get("path", "")
    if (not isinstance(root, str) or not os.path.isabs(root) or "\0" in root
            or not isinstance(relative, str) or "\0" in relative or "\\" in relative
            or PurePosixPath(relative).is_absolute() or ".." in relative.split("/")
            or type(body.get("hidden", False)) is not bool):
        raise CoordinationError("Choose a workspace and a relative file path.")
    try:
        root_path = Path(root).resolve(strict=True)
        if workspace_root and not root_path.is_relative_to(Path(workspace_root).resolve()):
            raise CoordinationError("Folder is outside this workspace.")
        if not root_path.is_dir():
            raise CoordinationError("Choose a folder to browse.")
        path = root_path.joinpath(*PurePosixPath(relative).parts).resolve(strict=True)
        if not path.is_relative_to(root_path):
            raise CoordinationError("Symbolic link points outside this folder.")
        if operation == "list":
            entries, truncated = [], False
            with os.scandir(path) as listing:
                for entry in listing:
                    if not body.get("hidden", False) and entry.name.startswith("."):
                        continue
                    if len(entries) == MAX_ENTRIES:
                        truncated = True
                        break
                    # Links are leaf nodes. Never recursively follow directory links.
                    link = entry.is_symlink()
                    entries.append({"name": entry.name,
                        "path": str(PurePosixPath(relative) / entry.name),
                        "directory": entry.is_dir(follow_symlinks=False), "symlink": link})
            entries.sort(key=lambda e: (not e["directory"], e["name"].casefold(), e["name"]))
            return {"entries": entries, "truncated": truncated}
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise CoordinationError("Only regular files can be previewed.")
            data = stream.read(MAX_PREVIEW_BYTES + 1)
        truncated = len(data) > MAX_PREVIEW_BYTES
        sample = data[:MAX_PREVIEW_BYTES]
        try:
            # A bounded read can end midway through a UTF-8 character.
            import codecs
            content = codecs.getincrementaldecoder("utf-8")().decode(sample, final=not truncated)
            if "\0" in content:
                raise UnicodeError
            return {"text": content, "truncated": truncated, "size": info.st_size, "binary": False}
        except UnicodeError:
            return {"text": "Preview unavailable for this file type. Use Open to view it in another app.",
                    "truncated": False, "size": info.st_size, "binary": True}
    except (OSError, ValueError, RuntimeError) as exc:
        raise CoordinationError("Cannot access this file or folder. It may have moved or require permission.") from exc
