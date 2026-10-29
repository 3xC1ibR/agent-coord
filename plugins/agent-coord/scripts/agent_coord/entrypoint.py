"""Run coordination clients with the code owned by the active desktop runtime."""
from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import sys

from . import desktop_runtime as runtime


MODULES = {"cli": "cli", "hook": "hook", "mcp": "checkpoint_mcp"}


def client_database(kind, argv):
    """Only the CLI's leading global options can override the database."""
    from .store import default_database_path

    database = default_database_path()
    remaining = list(argv)
    if kind == "cli":
        while remaining:
            if remaining[0] == "--db" and len(remaining) > 1:
                database = Path(remaining[1]).expanduser()
                remaining = remaining[2:]
            elif remaining[0].startswith("--db="):
                database = Path(remaining[0].split("=", 1)[1]).expanduser()
                remaining = remaining[1:]
            else:
                break
    return database.resolve(), remaining


def route_to_owner(kind, argv):
    module = MODULES[kind]
    database, remaining = client_database(kind, argv)
    # Installation must retain this updated launcher, not pin an old payload.
    if kind == "cli" and remaining[:1] in (["install-cli"], ["--help"], ["-h"]):
        return
    directory = database.parent / ("." + database.name + ".runtime")
    if not directory.exists():
        return
    directory = runtime.runtime_directory(database)
    if runtime.owner_available(directory):
        return
    record = runtime.discover(database, timeout=2)
    if record is None:
        raise RuntimeError("The runtime owner is unavailable. Try again shortly.")
    payload = Path(record["payload"]).resolve()
    if payload.parent != directory / "payloads" or not payload.is_dir():
        raise RuntimeError("The runtime owner did not identify a retained payload.")
    backend = payload / "backend"
    source = backend / "agent_coord" / (module + ".py")
    if not source.is_file() or not source.resolve().is_relative_to(payload):
        raise RuntimeError("The runtime payload is missing the coordination client.")
    if Path(__file__).resolve().parent == backend / "agent_coord":
        return
    python = runtime.payload_command(payload)[0]
    # Older retained payloads have the MCP module but no MCP executable. Invoke
    # main directly, which also avoids recursively dispatching through wrappers.
    # Isolated Python prevents a caller's PYTHONPATH from mixing runtime copies;
    # exec preserves stdin, cwd, session identity, DB overrides and hook exports.
    code = ("import sys; sys.path.insert(0, sys.argv[1]); sys.argv = sys.argv[2:]; "
            f"from agent_coord.{module} import main; raise SystemExit(main())")
    os.execv(python, [python, "-I", "-B", "-u", "-c", code, str(backend), sys.argv[0], *argv])


def main(kind):
    try:
        route_to_owner(kind, sys.argv[1:])
    except Exception as exc:  # Malformed owner metadata must also block write hooks.
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    return importlib.import_module("agent_coord." + MODULES[kind]).main()
