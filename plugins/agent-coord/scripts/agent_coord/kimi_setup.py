"""Discover Kimi Code CLI and register hooks limited to app-owned processes."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile
import threading

from .context import cli_path
from .store import CoordinationError

START = "# BEGIN RIBBON FIELD KIMI HOOKS"
END = "# END RIBBON FIELD KIMI HOOKS"
CONFIG_LOCK = threading.Lock()
INSTALL = "Install Kimi Code CLI (brew install kimi-code or npm install -g @moonshot-ai/kimi-code), then run kimi login."


def kimi_home():
    return Path(os.environ.get("KIMI_CODE_HOME") or Path.home() / ".kimi-code").expanduser().resolve()


def resolve_runtime():
    configured = os.environ.get("AGENT_COORD_KIMI_BIN")
    candidates = ([configured] if configured else [shutil.which("kimi"),
        str(Path.home() / ".kimi-code/bin/kimi"),
        str(Path.home() / ".local/bin/kimi"), "/opt/homebrew/bin/kimi", "/usr/local/bin/kimi"])
    for candidate in candidates:
        if candidate:
            path = Path(candidate).expanduser()
            if path.is_file() and os.access(path, os.X_OK):
                return str(path.absolute())
    raise CoordinationError("Kimi Code CLI is unavailable. " + INSTALL +
                            " Set AGENT_COORD_KIMI_BIN to its executable if needed.")


def ensure_hooks():
    """Preserve user settings and replace only our managed hook block.

    Kimi reads hooks only from its user config. Each command is inert unless
    the process was launched with a Ribbon Field session identity. Discovery
    does not call this function; a selected Kimi conversation does.
    """
    hook = cli_path().with_name("agent-coord-hook")
    if not hook.is_file():
        raise CoordinationError(f"Ribbon Field's Kimi hook is missing: {hook}. Reinstall or rebuild Ribbon Field.")
    command = ('if [ "$AGENT_COORD_CLIENT" = kimi ] && [ -n "$AGENT_COORD_SESSION_ID" ]; then '
               + shlex.join([sys.executable, str(hook)]) + '; fi')
    rules = [START]
    for event in ("UserPromptSubmit", "PreToolUse", "PostToolUse"):
        rules.extend(["[[hooks]]", "event = " + json.dumps(event),
                      "command = " + json.dumps(command), "timeout = 10", ""])
    rules.append(END)
    block = "\n".join(rules) + "\n"
    with CONFIG_LOCK:
        path = (kimi_home() / "config.toml").resolve()
        original = path.read_text(encoding="utf-8") if path.exists() else ""
        if START in original or END in original:
            if original.count(START) != 1 or original.count(END) != 1 or original.index(END) < original.index(START):
                raise CoordinationError(f"Cannot update the Ribbon Field hook block in {path}; repair its markers first.")
            before, rest = original.split(START, 1)
            _, after = rest.split(END, 1)
            updated = before + block.rstrip("\n") + after
        else:
            updated = original.rstrip("\n") + "\n\n" + block
        if updated == original:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as output:
                temporary = Path(output.name)
                output.write(updated)
            temporary.chmod(path.stat().st_mode & 0o777 if path.exists() else 0o600)
            # Do not overwrite a config concurrently changed by Kimi or a user.
            current = path.read_text(encoding="utf-8") if path.exists() else ""
            if current != original:
                raise CoordinationError("Kimi configuration changed during hook setup. Try again.")
            os.replace(temporary, path)
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)
