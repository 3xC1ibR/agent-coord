"""Authenticated high-level coordination requests executed by the database owner.

Transactions are complete inside the runtime. Client processes never hold an
open server transaction while waiting on a terminal, stdin, or a provider.
"""
from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import math
import time
import threading
from argparse import Namespace
from contextlib import contextmanager

from .context import (bind_caller, caller_directory, caller_environment, caller_session,
                      client_context, read_input, request_ownership)
from .store import (AmbiguousTargetError, ConflictError, CoordinationError,
                    InboxTimeoutError, CoordinationStore, default_database_path)

CLIENT_REQUEST_PROTOCOL = 1
ENVIRONMENT_FIELDS = frozenset({
    "AGENT_COORD_CLIENT", "AGENT_COORD_SESSION_ID", "AGENT_COORD_DELEGATION_ID",
    "AGENT_COORD_ZELLIJ_WAKE", "CODEX_THREAD_ID", "CLAUDE_ENV_FILE",
    "PLUGIN_ROOT", "CLAUDE_PLUGIN_ROOT", "ZELLIJ_SESSION_NAME", "ZELLIJ_PANE_ID",
    "PATH", "HOME", "SHELL", "TERM", "LANG", "LC_ALL", "TMPDIR",
})
PATH_ARGUMENTS = frozenset({"cwd", "ui_cwd", "workspace", "path", "transcript_path", "instructions_file", "bin_dir"})
_OPERATIONS = {}
_OWNER_LOCKS = {}
_OWNER_LOCKS_GUARD = threading.Lock()


class RuntimeRequestUnavailable(CoordinationError):
    """Transient owner/transport failure; only idempotent operations may retry."""


def _reset_owner_locks():
    global _OWNER_LOCKS, _OWNER_LOCKS_GUARD
    _OWNER_LOCKS, _OWNER_LOCKS_GUARD = {}, threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_owner_locks)


def owns_database(database):
    """Recognize this runtime or this request's bounded standalone ownership."""
    from . import desktop_runtime as runtime
    identity = str(Path(database).expanduser().resolve())
    return identity in runtime._OWNED_DATABASES or (os.getpid(), identity) in request_ownership.get()


@contextmanager
def owner_operation(database, *, timeout=None):
    """Reserve a complete finite local mutation, or yield False for routing.

    The same kernel lock gates runtime startup. Request-local ownership is
    reentrant but is neither inherited by other request threads nor trusted
    across a process fork.
    """
    from . import desktop_runtime as runtime
    database = Path(database or default_database_path()).expanduser().resolve()
    if owns_database(database):
        yield True
        return
    directory = runtime.runtime_directory(database)
    identity = (os.getpid(), str(database))
    # Concurrent standalone operations in this process queue behind one another.
    # Only the kernel lock and request context grant ownership; this mutex does
    # not allow bypassing an owner in another process.
    with _OWNER_LOCKS_GUARD:
        lock = _OWNER_LOCKS.setdefault(identity, threading.RLock())
    acquired = lock.acquire() if timeout is None else lock.acquire(timeout=max(0, timeout))
    if not acquired:
        error = sqlite3.OperationalError("Database ownership is busy with another local operation.")
        error.sqlite_errorcode = sqlite3.SQLITE_BUSY
        error.sqlite_errorname = "SQLITE_BUSY"
        raise error
    try:
        try:
            descriptor = runtime.lock_file(directory / "owner.lock")
        except BlockingIOError:
            yield False
            return
        token = request_ownership.set(request_ownership.get() | {identity})
        try:
            yield True
        finally:
            request_ownership.reset(token)
            os.close(descriptor)
    finally:
        lock.release()


@contextmanager
def connection_ownership(database, *, timeout=5):
    """Wait for finite standalone writes; recognize a verified runtime owner.

    Direct library connections are query-only under a live runtime. A short
    standalone reservation is bounded contention, not runtime ownership.
    """
    from . import desktop_runtime as runtime
    database = Path(database or default_database_path()).expanduser().resolve()
    deadline = time.monotonic() + timeout
    while True:
        with owner_operation(database, timeout=max(0, deadline - time.monotonic())) as owner:
            if owner:
                yield True
                return
            remaining = deadline - time.monotonic()
            if remaining > 0:
                try:
                    record = runtime.discover(database, timeout=min(.05, remaining))
                except (OSError, RuntimeError, ValueError):
                    record = None
                if record is not None:
                    yield False
                    return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            error = sqlite3.OperationalError("Database ownership is busy with another process.")
            error.sqlite_errorcode = sqlite3.SQLITE_BUSY
            error.sqlite_errorname = "SQLITE_BUSY"
            raise error
        time.sleep(min(.01, remaining))


def run_local_or_forward(kind, payload, local, database=None, *, read_only=False, timeout=45):
    """Complete finite work under ownership; never retry a submitted request."""
    database = Path(database or default_database_path()).expanduser().resolve()
    if read_only:
        forwarded, result = forward_request(kind, payload, database, timeout=timeout)
        return result if forwarded else local()
    deadline = time.monotonic() + timeout
    for _ in range(3):
        with owner_operation(database, timeout=max(0, deadline - time.monotonic())) as local_owner:
            if local_owner:
                return local()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeRequestUnavailable("Runtime ownership selection timed out; no request was submitted.")
            forwarded, result = forward_request(kind, payload, database, timeout=remaining)
            if forwarded:
                return result
            # The prior owner exited before request submission. Elect again;
            # transport failures raise and can never reach this branch.
    raise RuntimeRequestUnavailable("Runtime ownership changed repeatedly; no request was submitted.")


def register_operation(name, handler):
    """Install a finite server-side operation during runtime construction."""
    if not isinstance(name, str) or not name or not callable(handler):
        raise ValueError("A runtime operation needs a name and callable handler.")
    if name in _OPERATIONS and _OPERATIONS[name] is not handler:
        raise ValueError("Runtime operation is already registered: " + name)
    _OPERATIONS[name] = handler


def local_cli_command(arguments):
    """These commands retain their process; finite database calls route separately."""
    return (arguments.command in {"install-cli", "antigravity"}
            or arguments.command == "ui" and not arguments.ui_command
            or arguments.command == "delegation" and arguments.delegation_command == "supervise"
            or arguments.command == "wake-zellij" and arguments.wake_command == "watch"
            or arguments.command == "inbox" and arguments.wait
            or arguments.command == "ask" and arguments.wait is not None)


def capture_context():
    environment = caller_environment()
    return {"environment": {name: environment[name] for name in ENVIRONMENT_FIELDS if name in environment},
            "directory": caller_directory(), "session": caller_session()}


def _context(value):
    if not isinstance(value, dict) or set(value) - {"environment", "directory", "session"}:
        raise CoordinationError("Invalid runtime caller context.")
    environment, directory, session = value.get("environment"), value.get("directory"), value.get("session")
    if (not isinstance(environment, dict) or set(environment) - ENVIRONMENT_FIELDS
            or any(not isinstance(v, str) or len(v) > 65536 for v in environment.values())):
        raise CoordinationError("Invalid runtime caller environment.")
    if not isinstance(directory, str) or not Path(directory).is_absolute() or len(directory) > 4096:
        raise CoordinationError("Runtime caller directory must be absolute.")
    if session is not None and (not isinstance(session, str) or not session.strip()):
        raise CoordinationError("Runtime caller identity must be a nonempty string.")
    return environment, directory, session


def failure(error):
    result = {"type": type(error).__name__, "message": str(error)}
    for field in ("conflicts", "bead_id", "sessions", "session_id", "timeout_seconds", "sqlite_errorcode", "sqlite_errorname"):
        if hasattr(error, field):
            result[field] = getattr(error, field)
    return result


def raise_failure(value):
    name, message = value.get("type"), value.get("message", "Runtime request failed.")
    if name == "RuntimeRequestUnavailable" or message == "Runtime maintenance is pausing new coordination requests.":
        raise RuntimeRequestUnavailable(message)
    if name == "ConflictError":
        raise ConflictError(value["conflicts"])
    if name == "AmbiguousTargetError":
        raise AmbiguousTargetError(value["bead_id"], value["sessions"])
    if name == "InboxTimeoutError":
        raise InboxTimeoutError(value["session_id"], value["timeout_seconds"])
    if name in {"OperationalError", "IntegrityError", "DatabaseError"}:
        error = getattr(sqlite3, name)(message)
        for field in ("sqlite_errorcode", "sqlite_errorname"):
            if field in value:
                setattr(error, field, value[field])
        raise error
    raise CoordinationError(message)


def _discover_ready_owner(runtime, database, directory, timeout):
    """Retry only the handshake while a newly elected owner publishes itself."""
    deadline = time.monotonic() + min(2, timeout)
    while True:
        try:
            record = runtime.discover(database, timeout=max(.001, deadline - time.monotonic()))
        except RuntimeError as error:
            if str(error) != "The runtime owner is unavailable. Refusing a second owner.":
                raise
            record = None
        if record is not None:
            return record
        if runtime.owner_available(directory):
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeRequestUnavailable("The runtime owner is unavailable. No request was submitted.")
        time.sleep(min(.05, remaining))


def forward_request(kind, payload, database=None, *, timeout=45):
    """Forward once to a verified owner. Transport failure never permits replay."""
    from . import desktop_runtime as runtime

    database = Path(database or default_database_path()).expanduser().resolve()
    if owns_database(database):
        return False, None
    directory = database.parent / ("." + database.name + ".runtime")
    if not directory.exists() or runtime.owner_available(directory):
        return False, None
    try:
        record = _discover_ready_owner(runtime, database, directory, timeout)
        if record is None:
            return False, None
        if record.get("client_requests") != CLIENT_REQUEST_PROTOCOL:
            raise CoordinationError("The running runtime does not support coordination requests. Wait for its compatible update.")
        payload = payload() if callable(payload) else payload
        response = runtime.control(record, "client-request", timeout=timeout,
                                   request={"kind": kind, "payload": payload, "context": capture_context()})
    except CoordinationError:
        raise
    except (OSError, ValueError, RuntimeError) as error:
        raise RuntimeRequestUnavailable("Runtime request outcome is unavailable; inspect its state before retrying: " + str(error)) from error
    if response.get("failure"):
        raise_failure(response["failure"])
    if "result" not in response:
        raise RuntimeRequestUnavailable("Runtime request returned no result; inspect its state before retrying.")
    return True, response["result"]


def _cli_payload(arguments, argv, *, defer_wait=False):
    field = None
    if arguments.command == "checkpoint":
        field = "checkpoint_json"
    elif arguments.command in {"send", "reply"} or arguments.command == "session-control" and arguments.action == "submit":
        field = "message"
    elif arguments.command == "thread" and arguments.thread_command == "create":
        field = "prompt"
    elif arguments.command == "ask":
        field = "question"
    result = {"argv": argv}
    if field and getattr(arguments, field, None) == "-":
        value = read_input(1048577)
        if len(value) > 1048576:
            raise CoordinationError("Runtime command input exceeds 1 MiB.")
        result["stdin"] = value
    if defer_wait:
        result["defer_wait"] = True
    return result


def _local_cli(arguments, *, read_only=False, defer_wait=False):
    from .cli import run
    if defer_wait:
        arguments = Namespace(**vars(arguments))
        arguments.wait = None if arguments.command == "ask" else 0
    if read_only:
        store = CoordinationStore(arguments.db, read_only=True)
        with store.reading():
            return run(arguments, store=store)
    return run(arguments)


def _finite_operation(name, kwargs, database, *, read_only=False):
    def local():
        store = CoordinationStore(database, read_only=read_only)
        if read_only:
            with store.reading():
                return _OPERATIONS[name](store, **kwargs)
        return _OPERATIONS[name](store, **kwargs)
    return run_local_or_forward("operation", {"name": name, "kwargs": kwargs},
                                local, database, read_only=read_only)


def _control_wait_kind(arguments):
    if (arguments.command == "thread" and arguments.thread_command in {
            "create", "settings", "models", "provider-limits", "move", "resume", "request-status"}
            or arguments.command == "sidecar" and arguments.sidecar_command == "attach"
            or arguments.command == "session-control"):
        return "app"
    if arguments.command == "ui" and arguments.ui_command == "open":
        return "navigation"
    return None


def forward_cli(arguments, argv):
    """Keep blocking waits in the caller; each owner operation is finite."""
    if arguments.command == "inbox" and arguments.wait:
        from .cli import bind_command_caller
        bind_command_caller(arguments)
        identity = arguments.session_id or caller_session()
        if not identity:
            raise CoordinationError("No caller session for inbox wait.")
        if arguments.unread or arguments.include_delivered:
            raise CoordinationError("--wait cannot be combined with --unread or --all.")
        duration = arguments.timeout
        if duration is not None and (not math.isfinite(duration) or duration <= 0):
            raise CoordinationError("--timeout must be a positive number of seconds.")
        started = _finite_operation("inbox.wait-start", {
            "session_id": identity, "mark_delivered": not arguments.peek}, arguments.db, read_only=True)
        deadline = None if duration is None else time.monotonic() + duration
        while True:
            messages = _finite_operation("inbox.wait-poll", {
                "session_id": identity, "started_at": started["started_at"], "after": started["after"],
                "mark_delivered": not arguments.peek}, arguments.db, read_only=arguments.peek)
            if messages:
                return True, messages
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise InboxTimeoutError(identity, duration)
            time.sleep(.5 if remaining is None else min(.5, remaining))
    if arguments.command == "ask" and arguments.wait is not None:
        duration = arguments.wait
        if not math.isfinite(duration) or duration < 0 or not arguments.blocking:
            raise CoordinationError("Ask wait requires --blocking and a finite non-negative timeout.")
        current = run_local_or_forward("cli", lambda: _cli_payload(arguments, argv, defer_wait=True),
            lambda: _local_cli(arguments, defer_wait=True), arguments.db)
        deadline = time.monotonic() + duration if duration else None
        while current["status"] == "open":
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return True, {**current, "timed_out": True}
            time.sleep(.5 if remaining is None else min(.5, remaining))
            current = _finite_operation("asks.wait-poll", {
                "ask_id": current["id"], "session_id": current["session_id"]}, arguments.db)
        return True, {**current, "timed_out": False}
    control_kind = _control_wait_kind(arguments)
    if control_kind and arguments.wait:
        duration = arguments.wait
        if not math.isfinite(duration) or not 0 <= duration <= 30:
            raise CoordinationError("Wait must be between 0 and 30 seconds.")
        current = run_local_or_forward("cli", lambda: _cli_payload(arguments, argv, defer_wait=True),
            lambda: _local_cli(arguments, defer_wait=True), arguments.db)
        if "request_id" not in current:
            return True, current
        deadline = time.monotonic() + duration
        terminal = {"completed", "failed", "uncertain", "cancelled"}
        while (current["status"] == "requested" if control_kind == "navigation" else current["status"] not in terminal):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(.1, remaining))
            status = _finite_operation(control_kind + ".wait-poll", {
                "request_id": current["request_id"]}, arguments.db)
            current = {**current, **status} if control_kind == "navigation" else status
        return True, current
    if local_cli_command(arguments):
        return False, None
    read_only = _read_only_cli(arguments)
    result = run_local_or_forward("cli", lambda: _cli_payload(arguments, argv),
        lambda: _local_cli(arguments, read_only=read_only), arguments.db, read_only=read_only)
    return True, result


def _inbox_wait_start(store, session_id, mark_delivered=True):
    if type(mark_delivered) is not bool:
        raise CoordinationError("Invalid inbox delivery mode.")
    if mark_delivered:
        session_id = bind_caller(session_id)
    store.get_session(session_id)
    started_at = store.clock()
    with store._connection() as db:
        after = db.execute("SELECT COALESCE(MAX(id),0) FROM messages WHERE recipient_session_id=?", (session_id,)).fetchone()[0]
    return {"started_at": started_at, "after": after}


def _inbox_wait_poll(store, session_id, started_at, after, mark_delivered):
    if (not isinstance(started_at, (int, float)) or not math.isfinite(started_at)
            or type(after) is not int or after < 0 or type(mark_delivered) is not bool):
        raise CoordinationError("Invalid inbox wait cursor.")
    if mark_delivered:
        session_id = bind_caller(session_id)
        store.touch(session_id)
    return store.inbox(session_id, mark_delivered=mark_delivered,
                       wait_started_at=started_at, wait_after_message_id=after)


def _ask_wait_poll(store, ask_id, session_id):
    session_id = bind_caller(session_id)
    current = store.asks.get(ask_id)
    if current["session_id"] != session_id:
        raise CoordinationError("Ask does not belong to this caller.")
    if current["status"] == "open":
        store.touch(session_id)
    elif current["status"] == "answered" and current["answer_message_id"]:
        store.acknowledge(session_id, current["answer_message_id"])
    return current


def _app_wait_poll(store, request_id):
    from .app_control import AppControl
    return AppControl(store).status(request_id, wait=0)


def _navigation_wait_poll(store, request_id):
    from .navigation import NavigationStore
    result = NavigationStore(store).request(request_id)
    return {key: result[key] for key in ("status", "window_id", "error")}


register_operation("inbox.wait-start", _inbox_wait_start)
register_operation("inbox.wait-poll", _inbox_wait_poll)
register_operation("asks.wait-poll", _ask_wait_poll)
register_operation("app.wait-poll", _app_wait_poll)
register_operation("navigation.wait-poll", _navigation_wait_poll)


def _read_only_cli(arguments):
    return (arguments.command in {"activity", "status", "list", "conflicts"}
            or arguments.command == "thread" and (
                arguments.thread_command in {"list", "show", "metadata", "close-status"}
                or arguments.thread_command == "usage" and not arguments.backfill))


def execute_request(store, body):
    """Called only after the runtime authenticates the local request and instance."""
    try:
        request = body.get("request", body)
        if not isinstance(request, dict) or set(request) != {"kind", "payload", "context"}:
            raise CoordinationError("Invalid runtime client request.")
        environment, directory, session = _context(request["context"])
        payload = request["payload"]
        if not isinstance(payload, dict):
            raise CoordinationError("Runtime request payload must be an object.")
        input_text = payload.get("stdin") if request["kind"] == "cli" else None
        if input_text is not None and not isinstance(input_text, str):
            raise CoordinationError("Runtime stdin must be text.")
        with client_context(environment=environment, directory=directory, session=session, input_text=input_text):
            result = _execute(store, request["kind"], payload)
        return {"result": result}
    except Exception as error:
        return {"failure": failure(error)}


def _execute(store, kind, payload):
    if kind == "cli":
        from . import cli
        argv = payload.get("argv")
        if not isinstance(argv, list) or any(not isinstance(value, str) for value in argv):
            raise CoordinationError("Runtime CLI arguments must be a list of strings.")
        try:
            arguments = cli._parser().parse_args(argv)
        except SystemExit as error:
            raise CoordinationError("Invalid runtime CLI arguments.") from error
        if payload.get("defer_wait"):
            if arguments.command == "ask" and arguments.wait is not None:
                if not arguments.blocking or not math.isfinite(arguments.wait) or arguments.wait < 0:
                    raise CoordinationError("Ask wait requires --blocking and a finite non-negative timeout.")
                arguments.wait = None
            elif _control_wait_kind(arguments) and math.isfinite(arguments.wait) and 0 <= arguments.wait <= 30:
                arguments.wait = 0
            else:
                raise CoordinationError("Only a supported command can defer its wait to the caller.")
        if local_cli_command(arguments):
            raise CoordinationError("This command must retain its local process.")
        for field in PATH_ARGUMENTS:
            value = getattr(arguments, field, None)
            if value is not None:
                path = Path(value).expanduser()
                setattr(arguments, field, str(path if path.is_absolute() else Path(caller_directory()) / path))
        if arguments.db is not None:
            selected = Path(arguments.db).expanduser()
            selected = selected if selected.is_absolute() else Path(caller_directory()) / selected
            if selected.resolve() != store.database_path.resolve():
                raise CoordinationError("CLI database does not match the authenticated runtime.")
        arguments.db = str(store.database_path)
        if _read_only_cli(arguments):
            with store.reading():
                return cli.run(arguments, store=store)
        return cli.run(arguments, store=store)
    if kind == "hook":
        from . import hook
        value = payload.get("input")
        if not isinstance(value, dict):
            raise CoordinationError("Runtime hook input must be an object.")
        return hook.handle_antigravity(value, store=store) if hook._client() == "antigravity" else hook.handle(value, store=store)
    if kind == "mcp":
        from . import checkpoint_mcp
        return checkpoint_mcp.call_tool(payload.get("name"), payload.get("arguments"), store=store)
    if kind == "operation":
        name, args, kwargs = payload.get("name"), payload.get("args", []), payload.get("kwargs", {})
        if not isinstance(name, str) or name not in _OPERATIONS:
            raise CoordinationError("Unknown runtime operation.")
        if not isinstance(args, list) or not isinstance(kwargs, dict):
            raise CoordinationError("Invalid runtime operation arguments.")
        return _OPERATIONS[name](store, *args, **kwargs)
    raise CoordinationError("Unknown runtime client kind.")
