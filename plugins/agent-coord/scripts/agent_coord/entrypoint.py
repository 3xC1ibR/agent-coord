"""Run coordination clients with the code owned by the active desktop runtime."""
from __future__ import annotations

import http.client
import importlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError

from . import desktop_runtime as runtime


MODULES = {"cli": "cli", "hook": "hook", "mcp": "checkpoint_mcp"}
TRANSIENT_HTTP_STATUS_CODES = frozenset({500, 502, 503, 504})
TRANSIENT_RUNTIME_MESSAGES = (
    "Runtime discovery identity does not match the live instance.",
    "A different Ribbon Field runtime owns this database.",
    "Runtime maintenance is pausing",
    "The runtime owner is unavailable.",
)


def close_http_errors(error: BaseException | None) -> None:
    """Close any wrapped urllib HTTPError response sockets to prevent ResourceWarnings."""
    visited = set()
    current = error
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        if isinstance(current, HTTPError):
            try:
                current.close()
            except Exception:
                pass
        current = current.__cause__ or current.__context__


def is_transient_runtime_error(error: BaseException) -> bool:
    """Identify coordinator restart, handover, HTTP 5xx, or transient lock states."""
    from .browser_persistence import database_busy
    from .runtime_requests import RuntimeRequestUnavailable

    close_http_errors(error)
    if isinstance(error, RuntimeRequestUnavailable):
        return True
    if isinstance(error, sqlite3.OperationalError) and database_busy(error):
        return True
    if isinstance(error, HTTPError):
        return error.code in TRANSIENT_HTTP_STATUS_CODES
    if isinstance(error, URLError):
        reason = error.reason
        if isinstance(reason, HTTPError):
            return reason.code in TRANSIENT_HTTP_STATUS_CODES
        return isinstance(reason, (ConnectionError, TimeoutError, OSError))
    if isinstance(error, (ConnectionError, TimeoutError, http.client.HTTPException)):
        return True

    message = str(error)
    if any(message.startswith(prefix) for prefix in TRANSIENT_RUNTIME_MESSAGES):
        if isinstance(error, runtime.RuntimeOwnerUnavailableError):
            cause = error.__cause__ or error.__context__
            if cause is None:
                return True
            return is_transient_runtime_error(cause)
        return True
    return False


def _validate_local_discovery_metadata(database: Path) -> None:
    """Verify on-disk discovery.json structure without HTTP roundtrips."""
    directory = database.parent / ("." + database.name + ".runtime")
    if not directory.exists():
        return
    directory = runtime.runtime_directory(database)
    if runtime.owner_available(directory):
        return
    path = directory / "discovery.json"
    try:
        if path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
            raise RuntimeError("Runtime discovery permissions are unsafe.")
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return  # Transiently absent while a restarting owner publishes discovery.
    if not isinstance(record, dict):
        raise RuntimeError("Runtime discovery metadata must be a JSON object.")
    if record.get("database") != str(database.resolve()):
        raise RuntimeError("Runtime discovery refers to another database.")
    for required in ("instance", "control_url", "control_token"):
        if not isinstance(record.get(required), str) or not record[required]:
            raise RuntimeError(f"Runtime discovery is missing {required}.")


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


def route_to_owner(kind, argv, *, buffered_stdin: str | None = None):
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
    deadline = time.monotonic() + (1 if kind == "hook" else 2)
    while True:
        record = runtime.discover(database, timeout=max(.001, deadline - time.monotonic()))
        if record is not None:
            break
        # A standalone operation may release its reservation, or a starting
        # runtime may publish discovery. Neither has submitted this request.
        if runtime.owner_available(directory):
            return
        remaining_time = deadline - time.monotonic()
        if remaining_time <= 0:
            raise runtime.RuntimeOwnerUnavailableError("The runtime owner is unavailable. Try again shortly.")
        time.sleep(min(.05, remaining_time))
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
    if buffered_stdin is not None:
        with tempfile.TemporaryFile() as stream:
            stream.write(buffered_stdin.encode("utf-8"))
            stream.flush()
            stream.seek(0)
            os.dup2(stream.fileno(), 0)
    # Older retained payloads have the MCP module but no MCP executable. Invoke
    # main directly, which also avoids recursively dispatching through wrappers.
    # Isolated Python prevents a caller's PYTHONPATH from mixing runtime copies;
    # exec preserves stdin, cwd, session identity, DB overrides and hook exports.
    code = ("import sys; sys.path.insert(0, sys.argv[1]); sys.argv = sys.argv[2:]; "
            f"from agent_coord.{module} import main; raise SystemExit(main())")
    os.execv(python, [python, "-I", "-B", "-u", "-c", code, str(backend), sys.argv[0], *argv])


def main(kind):
    if kind == "hook":
        from .hook import main as hook_main

        raw_stdin = sys.stdin.read()
        try:
            payload = json.loads(raw_stdin)
        except ValueError as exc:
            return hook_main(payload_error=exc)
        if not isinstance(payload, dict):
            return hook_main(payload=payload)
        is_pre_tool = (
            os.environ.get("AGENT_COORD_CLIENT") == "antigravity"
            or payload.get("hook_event_name") == "PreToolUse"
        )
        if is_pre_tool:
            # Evaluate PreToolUse directly against state.sqlite3?mode=ro without
            # execv into payload python and without HTTP roundtrips to the coordinator.
            database, _ = client_database("hook", sys.argv[1:])
            try:
                _validate_local_discovery_metadata(database)
            except Exception as exc:
                close_http_errors(exc)
                print(json.dumps({"error": str(exc)}), file=sys.stderr)
                return 2
            return hook_main(payload=payload, pre_tool_fast_path=True)
        try:
            route_to_owner("hook", sys.argv[1:], buffered_stdin=raw_stdin)
        except Exception as exc:
            close_http_errors(exc)
            if is_transient_runtime_error(exc):
                return hook_main(payload=payload, unavailable=exc)
            print(json.dumps({"error": str(exc)}), file=sys.stderr)
            return 2
        return hook_main(payload=payload)

    try:
        route_to_owner(kind, sys.argv[1:])
    except Exception as exc:
        close_http_errors(exc)
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    return importlib.import_module("agent_coord." + MODULES[kind]).main()
