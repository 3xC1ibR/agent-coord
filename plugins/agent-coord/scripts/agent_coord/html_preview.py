"""Bounded, read-only HTML output for an authenticated conversation viewer."""
from __future__ import annotations

import os
from pathlib import Path
import stat

from .store import CoordinationError

MAX_HTML_BYTES = 8 * 1024 * 1024
PREVIEW_CSP = (
    "default-src 'none'; script-src 'unsafe-inline' https: data: blob:; "
    "style-src 'unsafe-inline' https:; img-src https: data: blob:; "
    "font-src https: data:; media-src data: blob:; connect-src 'none'; "
    "frame-src 'none'; worker-src 'none'; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'self'; sandbox allow-scripts"
)


def read_html(workspace: str, path: str) -> dict:
    if not isinstance(path, str) or not path or len(path) > 4096 or "\0" in path:
        raise CoordinationError("Choose an HTML file in this conversation’s project.")
    root = Path(workspace).resolve()
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        candidate = candidate.resolve(strict=True)
        if not candidate.is_relative_to(root):
            raise CoordinationError("This HTML file is outside the conversation’s project. Move it into the project to preview it here.")
        if candidate.suffix.lower() not in {".html", ".htm"}:
            raise CoordinationError("Only HTML files can be previewed here.")
        # Nonblocking open prevents a named pipe from hanging a request. Do not
        # follow a last-component symlink changed after the containment check.
        descriptor = os.open(candidate, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise CoordinationError("Choose an HTML file, not a folder or special file.")
            if info.st_size > MAX_HTML_BYTES:
                raise CoordinationError("This HTML file is too large to preview (8 MB limit).")
            data = source.read(MAX_HTML_BYTES + 1)
        if len(data) > MAX_HTML_BYTES:
            raise CoordinationError("This HTML file is too large to preview (8 MB limit).")
        html = data.decode("utf-8-sig")
        if not html.strip():
            raise CoordinationError("This HTML file is empty.")
        return {"name": candidate.name, "html": html}
    except CoordinationError:
        raise
    except (OSError, RuntimeError) as exc:
        raise CoordinationError("This HTML file is missing or cannot be read on your Mac.") from exc
    except UnicodeError as exc:
        raise CoordinationError("This HTML file must use UTF-8 text to preview it.") from exc
