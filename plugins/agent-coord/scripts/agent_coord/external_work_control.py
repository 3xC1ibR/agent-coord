"""Local external work HTTP protocol and its command-line client."""
from __future__ import annotations

import http.client
import json
import os
import secrets
import stat
import sys
import tempfile
from http import HTTPStatus
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit

from .codex_app_server import BrowserBusyError
from .external_work import identifier
from .store import CoordinationError, default_database_path

ROUTE = "/api/external-work"


def token_file(database):
    return Path(str(Path(database).expanduser().resolve()) + ".external-work-token")


def read_token(path):
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW), "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise CoordinationError("External work credentials require an owner-only regular file.")
        raw = stream.read(259)
    token = raw.removesuffix(b"\n")
    if not 32 <= len(token) <= 256 or any(b <= 32 or b >= 127 for b in token):
        raise CoordinationError("External work credentials require a 32–256 character printable ASCII token.")
    return token


def handle_http(handler, engine):
    if (not handler._local_request() or handler.headers.get("Origin") is not None
            or handler.headers.get("Sec-Fetch-Site") not in (None, "none")):
        handler._json(HTTPStatus.FORBIDDEN, {"error": "External work requires a local application connection."})
        return
    try:
        token = read_token(token_file(engine.store.database_path))
    except (OSError, CoordinationError):
        handler._json(HTTPStatus.FORBIDDEN, {"error": "External work HTTP access is disabled or its credential file is unavailable."})
        return
    authorization = handler.headers.get("Authorization", "")
    if not secrets.compare_digest(authorization.encode("latin1"), b"Bearer " + token):
        handler._json(HTTPStatus.UNAUTHORIZED, {"error": "Supply the external work bearer token."})
        return
    parsed = urlsplit(handler.path)
    suffix = parsed.path[len(ROUTE):]
    parts = [unquote(part) for part in suffix[1:].split("/")] if suffix else []
    try:
        if handler.command == "GET" and not parts:
            query = parse_qs(parsed.query, keep_blank_values=True)
            if set(query) - {"source", "limit"} or any(len(v) != 1 for v in query.values()):
                raise CoordinationError("List accepts one source and one limit.")
            result = {"data": engine.list(query.get("source", [None])[0], int(query.get("limit", [50])[0]))}
        elif handler.command == "GET" and len(parts) == 2:
            result = engine.get(*parts)
        elif handler.command == "POST" and (not parts or (len(parts) == 3 and parts[2] == "cancel")):
            if handler.headers.get_content_type() != "application/json" or handler.headers.get("Transfer-Encoding"):
                handler._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "Send a JSON object with a Content-Length."})
                return
            size = int(handler.headers.get("Content-Length", 0))
            if not 0 < size <= 1024 * 1024:
                handler._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "Expected a JSON body of at most 1 MiB."})
                return
            handler.connection.settimeout(10)
            data = handler.rfile.read(size)
            handler._body_read = True
            body = json.loads(data)
            if not parts:
                required = {"source", "request_id", "settings", "prompt"}
                if not isinstance(body, dict) or not required <= set(body) <= required | {"name"}:
                    raise CoordinationError("Submit source, request_id, settings, prompt and optionally name.")
                result = engine.submit(**body)
            else:
                if body != {}:
                    raise CoordinationError("Cancel accepts an empty JSON object.")
                result = engine.cancel(*parts[:2])
        else:
            result = None
        if result is None:
            handler._json(HTTPStatus.NOT_FOUND, {"error": "External work request or route not found."})
        else:
            handler._json(HTTPStatus.OK, result)
    except BrowserBusyError as exc:
        handler._json(HTTPStatus.CONFLICT, {"error": str(exc)})
    except (CoordinationError, ValueError, RecursionError) as exc:
        handler._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})


def add_parser(commands):
    parser = commands.add_parser("external-work", help="Submit and manage authenticated local external work.")
    parser.add_argument("--url", help="Loopback Ribbon Field URL. Defaults to desktop runtime discovery.")
    operations = parser.add_subparsers(dest="work_operation", required=True)
    operations.add_parser("provision-token", help="Provision a private HTTP bearer credential without displaying it.")
    submit = operations.add_parser("submit")
    submit.add_argument("--settings", required=True, help="JSON object with provider creation settings.")
    submit.add_argument("--name")
    submit.add_argument("prompt", help="Assignment text or - for standard input.")
    get = operations.add_parser("get")
    cancel = operations.add_parser("cancel")
    for target in (submit, get, cancel):
        target.add_argument("--source", required=True)
        target.add_argument("--request-id", required=True)
    listing = operations.add_parser("list")
    listing.add_argument("--source")
    listing.add_argument("--limit", default=50, type=int)


    integrations = commands.add_parser("integration", help="Manage local Ribbon Field application integrations.")
    integrations.add_argument("--url", help="Loopback Ribbon Field URL. Defaults to desktop runtime discovery.")
    stoic = integrations.add_subparsers(dest="integration", required=True).add_parser("stoic")
    actions = stoic.add_subparsers(dest="integration_action", required=True)
    actions.add_parser("status")
    configure = actions.add_parser("configure")
    configure.add_argument("--settings", required=True, help="JSON object of Stoic connection settings.")


def provision_token(database):
    path = token_file(database)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Atomic publication makes concurrent readers see either a full token or no token.
    with tempfile.NamedTemporaryFile(dir=path.parent) as stream:
        stream.write(secrets.token_urlsafe(48).encode() + b"\n")
        stream.flush()
        os.fsync(stream.fileno())
        try:
            os.link(stream.name, path)
        except FileExistsError:
            pass
    read_token(path)
    return {"token_file": str(path)}


def run(arguments):
    try:
        return _run(arguments)
    except (OSError, ValueError, http.client.HTTPException, RuntimeError) as exc:
        raise CoordinationError(str(exc)) from exc


def _run(arguments):
    database = arguments.db or default_database_path()
    if getattr(arguments, "work_operation", None) == "provision-token":
        return provision_token(database)
    url = arguments.url
    if not url:
        from .desktop_runtime import discover
        record = discover(database)
        if not record:
            raise CoordinationError("Supply --url for a standalone Ribbon Field server.")
        url = record["url"]
    parsed = urlsplit(url)
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
            or parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
        raise CoordinationError("Choose a local HTTP server URL.")
    if arguments.command == "integration":
        return integration_request(arguments, parsed)
    token = read_token(token_file(database))
    path, body = ROUTE, None
    operation = arguments.work_operation
    if operation == "list":
        query = {"limit": arguments.limit}
        if arguments.source is not None:
            query["source"] = identifier(arguments.source)
        path += "?" + urlencode(query)
    elif operation == "submit":
        prompt = sys.stdin.read(100001) if arguments.prompt == "-" else arguments.prompt
        body = {"source": arguments.source, "request_id": arguments.request_id, "prompt": prompt,
                "name": arguments.name, "settings": json.loads(arguments.settings)}
    else:
        path += "/" + quote(identifier(arguments.source), safe=":") + "/" + quote(identifier(arguments.request_id), safe=":")
        if operation == "cancel":
            path += "/cancel"
            body = {}
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=120)
    try:
        conn.request("GET" if body is None else "POST", path, None if body is None else json.dumps(body),
                     {"Authorization": "Bearer " + token.decode("ascii"), "Content-Type": "application/json"})
        response = conn.getresponse()
        result = json.load(response)
        if response.status != 200:
            raise CoordinationError(result.get("error") or f"External work HTTP {response.status}.")
        return result
    finally:
        conn.close()


def integration_request(arguments, parsed):
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=15)
    try:
        conn.request("GET", "/api/browser/config")
        config = conn.getresponse()
        bootstrap = json.load(config)
        if config.status != 200 or not bootstrap.get("token") or bootstrap.get("remote"):
            raise CoordinationError("Local Ribbon Field configuration is unavailable.")
        headers = {"X-Agent-Coord-Token": bootstrap["token"], "Content-Type": "application/json"}
        configure = arguments.integration_action == "configure"
        conn.request("PATCH" if configure else "GET", "/api/browser/integrations/stoic",
                     arguments.settings.encode() if configure else None, headers)
        response = conn.getresponse()
        result = json.load(response)
        if response.status != 200:
            raise CoordinationError(result.get("error") or f"Integration HTTP {response.status}.")
        return result
    finally:
        conn.close()
