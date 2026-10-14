"""Muse discovery and process-local coordination hooks; never rewrite user settings."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile

from .context import cli_path
from .store import CoordinationError

TESTED_VERSION = "1.4.8"
SCHEMA_FINGERPRINT = "sha256:4e3200c3c9dc0cd7919d11022630d43f9bc1c5cdda375381e90b0183b5665069"
INSTALL = "Install Muse Code from https://dev.meta.ai/docs/muse-code, then run muse login on this machine."


def resolve_runtime(store):
    configured = os.environ.get("AGENT_COORD_MUSE_BIN")
    candidates = [configured] if configured else [shutil.which("muse"),
        str(Path.home() / ".local/bin/muse"), "/opt/homebrew/bin/muse", "/usr/local/bin/muse",
        str(store.database_path.parent / "providers/muse/runtime/muse")]
    for candidate in candidates:
        if candidate:
            path = Path(candidate).expanduser()
            if path.is_file() and os.access(path, os.X_OK):
                return str(path.absolute())
    raise CoordinationError("Muse Code is unavailable. " + INSTALL + " Set AGENT_COORD_MUSE_BIN if needed.")


def hook_config(store, session_id):
    """Merge user hooks into a host-only override, preserving managed/project hooks.

    Muse clears the hook environment. Put our identity in the command itself,
    not a global settings file or an assumed inherited variable. Native child
    tools share their owning Ribbon Field conversation's coordination scope.
    """
    path = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "muse/settings.json"
    try:
        settings = json.loads(path.read_text()) if path.exists() else {}
        hooks = copy.deepcopy(settings.get("hooks", {}))
        if not isinstance(hooks, dict):
            raise ValueError("hooks must be an object")
    except (OSError, ValueError, AttributeError) as exc:
        raise CoordinationError(f"Cannot read Muse hooks in {path}: {exc}") from exc
    hook = cli_path().with_name("agent-coord-hook")
    if not hook.is_file():
        raise CoordinationError("Ribbon Field's Muse hook is missing. Reinstall Ribbon Field.")
    command = shlex.join(["env", "AGENT_COORD_CLIENT=muse", f"AGENT_COORD_SESSION_ID={session_id}",
                          f"AGENT_COORD_DB={store.database_path}", sys.executable, str(hook)])
    for event in ("UserPromptSubmit", "PreToolUse", "PostToolUse"):
        if not isinstance(hooks.get(event, []), list):
            raise CoordinationError(f"Muse hook {event} must be a list.")
        hooks.setdefault(event, []).append({"hooks": [{"type": "command", "command": command, "timeout": 10}]})
    return "hooks=" + json.dumps(hooks, separators=(",", ":"))


def host_config(store, session_id):
    """A private settings overlay: 1.4.8 ignores `-c hooks` in its hook loader.

    Keep native credentials, trust, plugins and other XDG configuration reachable
    through symlinks. Only the settings file is copied; the originals stay intact.
    The caller retains this directory until its host exits.
    """
    source = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config").expanduser().resolve()
    settings_path = source / "muse/settings.json"
    hooks = json.loads(hook_config(store, session_id).removeprefix("hooks="))
    settings = json.loads(settings_path.read_text()) if settings_path.exists() else {"schema_version": 1}
    settings["hooks"] = hooks
    managed = settings.get("managed_hooks_path")
    if managed and not Path(managed).is_absolute():
        settings["managed_hooks_path"] = str(source / "muse" / managed)
    directory = tempfile.TemporaryDirectory(prefix="ribbon-muse-config-")
    try:
        target = Path(directory.name)
        for entry in source.iterdir() if source.is_dir() else []:
            if entry.name != "muse":
                (target / entry.name).symlink_to(entry)
        native = target / "muse"
        native.mkdir()
        for entry in (source / "muse").iterdir() if (source / "muse").is_dir() else []:
            if entry.name != "settings.json":
                (native / entry.name).symlink_to(entry)
        (native / "settings.json").write_text(json.dumps(settings))
        return directory
    except Exception:
        directory.cleanup()
        raise
