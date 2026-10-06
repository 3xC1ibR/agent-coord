from __future__ import annotations

import errno
import ipaddress
import json
import os
import secrets
import socket
import socketserver
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .codex_app_server import BrowserBusyError, BrowserSessions
from .managed_pty import read_delegation_output
from .store import CoordinationError, CoordinationStore
from .workspaces import matches_workspace

DEFAULT_UI_HOST = "127.0.0.1"
DEFAULT_UI_PORT = 8765
DEFAULT_OUTPUT_BYTES = 24 * 1024
SORT_KEYS = {"created", "last_activity", "name"}
_CLIENT_DISCONNECT_ERRNOS = {errno.ECONNABORTED, errno.ECONNRESET, errno.EPIPE}
_WEB_ROOT = Path(__file__).with_name("web")
_MAX_BODY_BYTES = 128 * 1024

_UI_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Agent Coord · Coordination</title>
<meta name="color-scheme" content="light">
<link rel="stylesheet" href="/styles.css">
</head>
<body class="monitor-page">
<header class="monitor-header"><a class="monitor-brand" href="/">a/c <span>Agent Coord</span></a><span class="monitor-heading">Coordination monitor</span><a class="monitor-back" href="/">← Workspace</a><span class="muted" id="health" role="status">Loading…</span></header>
<div class="shell"><nav aria-label="Process tree"><div class="tree-tools"><p class="label">Process tree</p><select id="sort" aria-label="Sort process tree"><option value="last_activity">Last activity</option><option value="created">Created</option><option value="name">Name</option></select></div><div id="tree"></div></nav><main id="detail"><div class="empty">Select a process to see its activity.</div></main></div>
<script src="/markdown.js"></script>
<script>
const tree=document.getElementById('tree'),detail=document.getElementById('detail'),health=document.getElementById('health'),sortControl=document.getElementById('sort');const pageQuery=new URLSearchParams(location.search),allowedSorts=['last_activity','created','name'];let snapshot={parents:[]},selected=null,tab='output',sortKey=allowedSorts.includes(pageQuery.get('sort'))?pageQuery.get('sort'):'last_activity';sortControl.value=sortKey;
const esc=v=>String(v??'—').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const messageHTML=m=>{const state=m.acknowledged_at?'acknowledged':m.delivered_at?'delivered':'pending',sender=m.sender_name||m.sender_session_id;return `<article class="message"><div class="message-meta">${esc(m.created_at)} ${esc(m.classification)} · ${state}<br>From: ${esc(sender)}<br>Thread: ${esc(m.thread_id)}</div><div class="markdown">${messageMarkdown.render(m.body)}</div></article>`};
function allNodes(){const nodes=[];for(const p of snapshot.parents){nodes.push({kind:'parent',id:p.session_id,data:p});for(const c of p.children)nodes.push({kind:'child',id:c.delegation_id,data:c,parent:p});}return nodes}
function renderTree(){const nodes=allNodes();if(!nodes.some(n=>n.id===selected))selected=nodes.length?nodes[0].id:null;tree.innerHTML=nodes.map(n=>{const count=n.data.unacknowledged_message_count||0,status=n.data.display_status+(count?' · '+count+' unacked':'');return `<button class="node ${n.kind==='child'?'child ':''}${selected===n.id?'selected':''}" aria-current="${selected===n.id?'true':'false'}" data-id="${esc(n.id)}"><span class="dot ${esc(n.data.display_status)}"></span><span><span class="name">${esc(n.kind==='parent'?(n.data.name||n.data.client+' parent'):(n.data.name||n.data.client+' · '+n.data.bead_id))}</span><span class="task">${esc(n.kind==='parent'?n.data.activity:n.data.instructions)}</span></span><span class="state">${esc(status)}</span></button>`}).join('')||'<div class="empty">No delegations found.</div>';tree.querySelectorAll('button').forEach(b=>b.onclick=()=>{selected=b.dataset.id;renderTree();renderDetail()})}
function renderDetail(){const node=allNodes().find(n=>n.id===selected);if(!node){detail.innerHTML='<div class="empty">No process selected.</div>';return}const d=node.data;const isChild=node.kind==='child';const history=(d.messages||[]).map(messageHTML).join('')||'<div class="empty">No received messages.</div>';const panes=isChild?{output:d.output||'No output captured yet.',messages:history,activity:`Delegation: ${d.delegation_id}\nParent: ${d.parent_session_id}\nChild session: ${d.child_session_id||'not attached'}\nRuntime: ${d.runtime_kind}\nSupervisor PID: ${d.supervisor_pid||'—'}\nChild PID: ${d.child_pid||'—'}\nCreated: ${d.created_at}\nLast activity: ${d.last_activity_at}`}:{output:'',messages:history,activity:`Session: ${d.session_id}\nClient: ${d.client}\nPresence: ${d.presence}\nActivity: ${d.activity}\nCreated: ${d.created_at}\nLast activity: ${d.last_activity_at}`};const childOverview=!isChild&&tab==='output'?`<div class="child-overview">${(d.children||[]).map(c=>{const childName=c.name||c.client+' · '+c.bead_id,output=(c.output||'No output captured yet.').slice(-2000);return `<button class="child-card" data-child="${esc(c.delegation_id)}"><span class="child-card-head"><span class="name">${esc(childName)}</span><span class="state">${esc(c.display_status)}</span></span><span class="task">${esc(c.instructions)}</span><pre>${esc(output)}</pre></button>`}).join('')||'<div class="empty">This parent has no delegated children.</div>'}</div>`:(tab==='messages'?history:`<pre>${esc(panes[tab])}</pre>`);detail.innerHTML=`<div class="head"><div><h1>${esc(isChild?(d.name||d.client+' · '+d.bead_id):(d.name||d.client+' parent'))}</h1><div class="muted">${esc(isChild?d.instructions:d.cwd)}</div></div><span>${esc(d.display_status)}</span></div><div class="facts"><div class="fact"><small>${isChild?'Bead':'Session'}</small><span>${esc(isChild?d.bead_id:d.session_id)}</span></div><div class="fact"><small>${isChild?'Scope':'Activity'}</small><span>${esc(isChild?(d.write_scope||[]).join(', '):d.activity)}</span></div><div class="fact"><small>${isChild?'Runtime':'Children'}</small><span>${esc(isChild?d.runtime_kind:d.children.length)}</span></div></div><div class="tabs">${['output','messages','activity'].map(t=>`<button class="${tab===t?'selected':''}" aria-pressed="${tab===t}" data-tab="${t}">${t[0].toUpperCase()+t.slice(1)}</button>`).join('')}</div>${childOverview}`;detail.querySelectorAll('.tabs button').forEach(b=>b.onclick=()=>{tab=b.dataset.tab;renderDetail()});detail.querySelectorAll('.child-card').forEach(b=>b.onclick=()=>{selected=b.dataset.child;tab='output';renderTree();renderDetail()})}
async function refresh(){try{const apiQuery=new URLSearchParams();for(const key of ['parent','cwd']){const value=pageQuery.get(key);if(value)apiQuery.set(key,value)}apiQuery.set('sort',sortKey);const response=await fetch('/api/snapshot?'+apiQuery.toString(),{cache:'no-store'});if(!response.ok)throw new Error(await response.text());snapshot=await response.json();const repository=snapshot.repository?' · '+snapshot.repository.split('/').pop():'';health.textContent=`runtime healthy · ${snapshot.process_count} processes${repository}`;renderTree();renderDetail()}catch(error){health.textContent='runtime unavailable';console.error(error)}}
sortControl.onchange=()=>{sortKey=sortControl.value;refresh()};
refresh();setInterval(refresh,1500);
</script>
</body>
</html>
"""


def _display_status(delegation: dict[str, Any], child: dict[str, Any] | None) -> str:
    status = str(delegation["status"])
    if status in {"completed", "failed", "launching", "launched"}:
        return status
    if child is None:
        return status
    if child["presence"] != "online":
        return str(child["presence"])
    if child["turn_active"]:
        return "running"
    return str(child["activity"])


def _delegation_output(
    store: CoordinationStore,
    delegation: dict[str, Any],
    *,
    max_bytes: int,
) -> str:
    output = read_delegation_output(
        store,
        str(delegation["delegation_id"]),
        max_bytes=max_bytes,
    )
    if output:
        return output
    if delegation["runtime_kind"] != "zellij" or delegation["status"] not in {
        "completed",
        "failed",
    }:
        return ""
    note = "[No terminal snapshot was captured before this Zellij pane ended.]"
    result = delegation["result_message"] or delegation["error"]
    if result:
        return f"{note}\n\nFinal result:\n{result}"
    return note


def _latest_timestamp(*values: str | None) -> str | None:
    available = [value for value in values if value]
    return max(available) if available else None


def _sort_nodes(
    nodes: list[dict[str, Any]], *, sort_by: str, parent: bool
) -> list[dict[str, Any]]:
    if sort_by == "name":
        return sorted(
            nodes,
            key=lambda item: str(
                item["name"]
                or (
                    f"{item['client']} parent"
                    if parent
                    else f"{item['client']} · {item['bead_id']}"
                )
            ).casefold(),
        )
    field = "created_at" if sort_by == "created" else "last_activity_at"
    return sorted(nodes, key=lambda item: item[field] or "", reverse=True)


def build_snapshot(
    store: CoordinationStore,
    *,
    parent_session_id: str | None = None,
    cwd: str | None = None,
    sort_by: str = "last_activity",
    output_bytes: int = DEFAULT_OUTPUT_BYTES,
) -> dict[str, Any]:
    normalized_sort = sort_by.strip().replace("-", "_")
    if normalized_sort not in SORT_KEYS:
        raise CoordinationError("UI sort must be name, created, or last_activity.")
    repository = str(Path(cwd).expanduser().resolve()) if cwd else None
    delegations = store.list_delegations(
        parent_session_id=parent_session_id,
        include_terminal=True,
    )
    if repository is not None:
        delegations = [item for item in delegations if matches_workspace(item["cwd"], repository)]
    parent_ids = sorted({str(item["parent_session_id"]) for item in delegations})
    if parent_session_id is None:
        child_ids = {item["child_session_id"] for item in delegations}
        parent_ids = sorted(set(parent_ids) | {
            item["session_id"] for item in store.list_sessions()
            if item["session_id"] not in child_ids and matches_workspace(item["cwd"], repository)
        })
    if parent_session_id is not None and parent_session_id not in parent_ids:
        requested_parent = store.get_session(parent_session_id)
        if matches_workspace(requested_parent["cwd"], repository):
            parent_ids.append(parent_session_id)
    parents: list[dict[str, Any]] = []
    process_count = 0
    for parent_id in parent_ids:
        parent = store.get_session(parent_id)
        parent_messages = store.inbox(
            parent_id,
            include_delivered=True,
            mark_delivered=False,
        )
        children: list[dict[str, Any]] = []
        for delegation in delegations:
            if delegation["parent_session_id"] != parent_id:
                continue
            child = None
            child_messages: list[dict[str, Any]] = []
            if delegation["child_session_id"]:
                try:
                    child = store.get_session(str(delegation["child_session_id"]))
                    child_messages = store.inbox(
                        child["session_id"],
                        include_delivered=True,
                        mark_delivered=False,
                    )
                except CoordinationError:
                    child = None
            last_activity_at = _latest_timestamp(
                delegation["updated_at"],
                delegation["runtime_started_at"],
                delegation["runtime_exited_at"],
                child["last_seen_at"] if child else None,
            )
            children.append(
                {
                    **delegation,
                    "name": delegation["name"] or (child["name"] if child else None),
                    "child": child,
                    "messages": child_messages,
                    "output": _delegation_output(
                        store,
                        delegation,
                        max_bytes=output_bytes,
                    ),
                    "display_status": _display_status(delegation, child),
                    "last_activity_at": last_activity_at,
                    "unacknowledged_message_count": sum(
                        message["acknowledged_at"] is None for message in child_messages
                    ),
                }
            )
        children = _sort_nodes(children, sort_by=normalized_sort, parent=False)
        parent_last_activity = _latest_timestamp(
            parent["last_seen_at"],
            *(child["last_activity_at"] for child in children),
        )
        parents.append(
            {
                **parent,
                "created_at": parent["started_at"],
                "last_activity_at": parent_last_activity,
                "messages": parent_messages,
                "unacknowledged_message_count": sum(
                    message["acknowledged_at"] is None for message in parent_messages
                ),
                "children": children,
                "display_status": (
                    "active" if parent["turn_active"] else parent["activity"]
                ),
            }
        )
        process_count += 1 + len(children)
    parents = _sort_nodes(parents, sort_by=normalized_sort, parent=True)
    return {
        "parents": parents,
        "process_count": process_count,
        "repository": repository,
        "sort_by": normalized_sort,
    }


def _validate_loopback(host: str) -> None:
    if host == "localhost":
        return
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise CoordinationError(
            "Agent Coord UI host must be localhost or a loopback IP address."
        ) from exc
    if not address.is_loopback:
        raise CoordinationError("Agent Coord UI only binds to loopback addresses.")


def _handler(
    store: CoordinationStore,
    parent_session_id: str | None,
    cwd: str | None,
    browser_sessions: BrowserSessions | None = None,
    csrf_token: str = "",
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: HTTPStatus, content_type: str, body: bytes) -> None:
            try:
                self.send_response(status.value)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                    "script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'",
                )
                self.end_headers()
                self.wfile.write(body)
            except OSError as exc:
                if exc.errno not in _CLIENT_DISCONNECT_ERRNOS:
                    raise
                self.close_connection = True

        def do_GET(self) -> None:
            if not self._valid_host():
                self._json(HTTPStatus.FORBIDDEN, {"error": "Invalid local host."})
                return
            parsed = urlparse(self.path)
            if parsed.path == "/":
                self._send(HTTPStatus.OK, "text/html; charset=utf-8", (_WEB_ROOT / "index.html").read_bytes())
                return
            if parsed.path in {"/app.js", "/markdown.js", "/thread-groups.js", "/notifications.js", "/organization.js", "/styles.css"}:
                content_type = "text/javascript" if parsed.path.endswith(".js") else "text/css"
                self._send(HTTPStatus.OK, content_type + "; charset=utf-8", (_WEB_ROOT / parsed.path[1:]).read_bytes())
                return
            if parsed.path == "/monitor":
                self._send(
                    HTTPStatus.OK,
                    "text/html; charset=utf-8",
                    _UI_HTML.encode(),
                )
                return
            if parsed.path.startswith("/api/browser/") and browser_sessions is not None:
                try:
                    self._browser_get(parsed)
                except CoordinationError as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            if parsed.path == "/api/snapshot":
                query = parse_qs(parsed.query)
                requested_parent = query.get("parent", [None])[0]
                requested_cwd = query.get("cwd", [None])[0]
                requested_sort = query.get("sort", ["last_activity"])[0]
                selected_parent = requested_parent or parent_session_id
                selected_cwd = requested_cwd or cwd
                try:
                    payload = build_snapshot(
                        store,
                        parent_session_id=selected_parent,
                        cwd=selected_cwd,
                        sort_by=requested_sort,
                    )
                except CoordinationError as exc:
                    self._send(
                        HTTPStatus.BAD_REQUEST,
                        "application/json; charset=utf-8",
                        json.dumps({"error": str(exc)}).encode(),
                    )
                    return
                self._send(
                    HTTPStatus.OK,
                    "application/json; charset=utf-8",
                    json.dumps(payload, sort_keys=True).encode(),
                )
                return
            self._send(
                HTTPStatus.NOT_FOUND,
                "application/json; charset=utf-8",
                b'{"error":"not found"}',
            )

        def _json(self, status: HTTPStatus, payload: Any) -> None:
            self._send(status, "application/json; charset=utf-8", json.dumps(payload).encode())

        def _valid_host(self) -> bool:
            host = self.headers.get("Host", "")
            port = self.server.server_address[1]
            bound = str(self.server.server_address[0])
            names = {"127.0.0.1", "localhost", "[::1]", f"[{bound}]" if ":" in bound else bound}
            return host in {f"{name}:{port}" for name in names} or (port == 80 and host in names)

        def _browser_get(self, parsed) -> None:
            route = parsed.path.removeprefix("/api/browser/")
            query = parse_qs(parsed.query)
            if route == "config":
                self._json(HTTPStatus.OK, {"token": csrf_token, "cwd": browser_sessions.cwd or os.getcwd(), "workspaceRoot": browser_sessions.cwd,
                                          "completionCursor": store.threads.completion_cursor()})
            elif route == "workspaces":
                self._json(HTTPStatus.OK, {"data": browser_sessions.list_workspaces()})
            elif route == "organization":
                self._json(HTTPStatus.OK, browser_sessions.store.threads.organization.list())
            elif route == "models":
                self._json(HTTPStatus.OK, {"data": browser_sessions.models()})
            elif route == "sessions":
                self._json(HTTPStatus.OK, {"data": browser_sessions.list_sessions(archived=query.get("archived") == ["true"])})
            elif route == "threads":
                self._json(HTTPStatus.OK, {"data": browser_sessions.list_work_threads(archived=query.get("archived") == ["true"])})
            elif route.startswith("threads/"):
                self._json(HTTPStatus.OK, browser_sessions.work_thread(unquote(route[8:])))
            elif route.startswith("sessions/"):
                self._json(HTTPStatus.OK, browser_sessions.read(unquote(route[9:])))
            elif route == "events":
                try:
                    cursor = (self.headers.get("Last-Event-ID") or query.get("after", ["0"])[0]).split(":")
                    if len(cursor) not in {1, 2}:
                        raise ValueError("Invalid cursor components.")
                    sequence = int(cursor[0])
                    completion_sequence = int(cursor[1] if len(cursor) == 2 else
                                              query.get("completion_after", [store.threads.completion_cursor()])[0])
                    if not all(0 <= value <= 9_223_372_036_854_775_807 for value in (sequence, completion_sequence)):
                        raise ValueError("Cursor out of range.")
                except ValueError as exc:
                    raise CoordinationError("Invalid event cursor.") from exc
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                try:
                    self.wfile.write(b"retry: 1500\n\n")
                    self.wfile.flush()
                    while not browser_sessions.closed:
                        # Terminal hooks run in separate processes. Read their durable
                        # events even when no app-server event wakes this connection.
                        batch = browser_sessions.events_after(sequence, timeout=1)
                        if browser_sessions.closed:
                            break
                        completions = browser_sessions.completions_after(completion_sequence)
                        sequence = batch["seq"]
                        completion_sequence = completions["seq"]
                        batch.update(completions=completions["events"], completionSeq=completion_sequence)
                        self.wfile.write(f"id: {sequence}:{completion_sequence}\ndata: {json.dumps(batch)}\n\n".encode())
                        self.wfile.flush()
                except (OSError, ValueError):
                    self.close_connection = True
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self) -> None:
            origin = self.headers.get("Origin")
            if (not self._valid_host() or not csrf_token or
                not secrets.compare_digest(self.headers.get("X-Agent-Coord-Token", ""), csrf_token) or
                (origin is not None and origin != "http://" + self.headers.get("Host", "")) or
                self.headers.get("Sec-Fetch-Site") == "cross-site"):
                self._json(HTTPStatus.FORBIDDEN, {"error": "Reload Agent Coord before making changes."})
                return
            if browser_sessions is None:
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Browser sessions unavailable."})
                return
            if self.headers.get_content_type() != "application/json" or self.headers.get("Transfer-Encoding"):
                self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "Send a JSON request."})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= _MAX_BODY_BYTES:
                    self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "Request body is empty or too large."})
                    return
                self.connection.settimeout(10)
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError("Expected a JSON object.")
                route = urlparse(self.path).path.split("/")[1:]
                if route == ["api", "browser", "projects"]:
                    if set(body) != {"name"}:
                        raise CoordinationError("Project creation accepts a name.")
                    self._json(HTTPStatus.CREATED, browser_sessions.store.threads.organization.create_project(body["name"]))
                    return
                if route == ["api", "browser", "repositories"]:
                    if set(body) != {"path"}:
                        raise CoordinationError("Repository registration accepts a path.")
                    self._json(HTTPStatus.CREATED, browser_sessions.store.threads.organization.add_repository(body["path"]))
                    return
                if route == ["api", "browser", "notifications", "claim"]:
                    if set(body) != {"completion_id"}:
                        raise CoordinationError("Notification claim requires a completion ID.")
                    self._json(HTTPStatus.OK, {"claimed": browser_sessions.claim_notification(body["completion_id"])})
                    return
                if route[:3] == ["api", "browser", "threads"]:
                    if len(route) not in {4, 5}:
                        raise CoordinationError("Choose a work thread.")
                    thread_id = unquote(route[3])
                    if len(route) == 4:
                        result = browser_sessions.update_work_thread(thread_id, body)
                    elif route[4] == "links":
                        result = browser_sessions.link_work_thread(thread_id, body)
                    elif route[4] == "checkpoint":
                        result = browser_sessions.checkpoint_work_thread(thread_id, body)
                    elif route[4] == "resume":
                        result = browser_sessions.resume_work_thread(thread_id)
                    elif route[4] in {"close", "reopen"}:
                        if body:
                            raise CoordinationError("Close and reopen accept an empty object.")
                        result = (browser_sessions.close_work_thread(thread_id) if route[4] == "close"
                                  else browser_sessions.reopen_work_thread(thread_id))
                    else:
                        raise CoordinationError("Unknown thread action.")
                    self._json(HTTPStatus.OK, result)
                    return
                if route[:3] != ["api", "browser", "sessions"]:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                    return
                if len(route) == 3:
                    self._json(HTTPStatus.CREATED, browser_sessions.create(body))
                    return
                thread_id = unquote(route[3])
                if len(route) == 4:
                    result = browser_sessions.update(thread_id, body)
                elif len(route) == 5 and route[4] == "messages":
                    result = browser_sessions.send(thread_id, body)
                elif len(route) == 5 and route[4] == "interrupt":
                    result = browser_sessions.interrupt(thread_id)
                elif len(route) == 6 and route[4] == "requests":
                    result = browser_sessions.answer(thread_id, unquote(route[5]), body)
                else:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                    return
                self._json(HTTPStatus.OK, result)
            except BrowserBusyError as exc:
                self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
            except (CoordinationError, ValueError, OSError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

        def log_message(self, _format: str, *args: Any) -> None:
            return

    return Handler


class LoopbackHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    browser_sessions: BrowserSessions | None = None

    def server_close(self) -> None:
        if self.browser_sessions is not None:
            self.browser_sessions.close()
        super().server_close()

    def server_bind(self) -> None:
        # HTTPServer performs a reverse-DNS lookup only to populate a cosmetic
        # server_name attribute. That lookup can block on offline machines.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)


class LoopbackIPv6HTTPServer(LoopbackHTTPServer):
    address_family = socket.AF_INET6


def make_ui_server(
    store: CoordinationStore,
    *,
    host: str = DEFAULT_UI_HOST,
    port: int = DEFAULT_UI_PORT,
    parent_session_id: str | None = None,
    cwd: str | None = None,
    browser_sessions: BrowserSessions | None = None,
) -> ThreadingHTTPServer:
    _validate_loopback(host)
    if not 0 <= port <= 65535:
        raise CoordinationError("Agent Coord UI port must be between 0 and 65535.")
    server_type = LoopbackIPv6HTTPServer if ":" in host else LoopbackHTTPServer
    sessions = browser_sessions or BrowserSessions(store, cwd)
    try:
        server = server_type((host, port), _handler(store, parent_session_id, cwd, sessions, secrets.token_urlsafe(32)))
    except Exception:
        sessions.close()
        raise
    server.browser_sessions = sessions
    return server


def serve_ui(
    store: CoordinationStore,
    *,
    host: str = DEFAULT_UI_HOST,
    port: int = DEFAULT_UI_PORT,
    parent_session_id: str | None = None,
    cwd: str | None = None,
    open_browser: bool = True,
) -> dict[str, Any]:
    server = make_ui_server(
        store,
        host=host,
        port=port,
        parent_session_id=parent_session_id,
        cwd=cwd,
    )
    bound_host, bound_port = server.server_address[:2]
    url_host = f"[{bound_host}]" if ":" in str(bound_host) else bound_host
    url = f"http://{url_host}:{bound_port}/"
    print(json.dumps({"status": "serving", "url": url}), flush=True)
    if open_browser:
        threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return {"status": "stopped", "url": url}
