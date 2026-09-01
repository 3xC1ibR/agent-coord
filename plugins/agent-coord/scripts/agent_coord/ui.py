from __future__ import annotations

import errno
import ipaddress
import json
import socket
import socketserver
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .managed_pty import read_delegation_output
from .store import CoordinationError, CoordinationStore

DEFAULT_UI_HOST = "127.0.0.1"
DEFAULT_UI_PORT = 8765
DEFAULT_OUTPUT_BYTES = 24 * 1024
SORT_KEYS = {"created", "last_activity", "name"}
_CLIENT_DISCONNECT_ERRNOS = {errno.ECONNABORTED, errno.ECONNRESET, errno.EPIPE}

_UI_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Agent Coord</title>
<style>
:root{color-scheme:light dark;font-family:ui-sans-serif,system-ui,-apple-system,sans-serif;background:#10151c;color:#e7edf4}*{box-sizing:border-box}body{margin:0;background:#10151c}header{display:flex;justify-content:space-between;gap:12px;padding:13px 16px;border-bottom:1px solid #303a46;background:#171d25}header strong{font-weight:600}.muted{color:#9ca9b8}.shell{display:grid;grid-template-columns:minmax(260px,30%) minmax(0,1fr);min-height:calc(100vh - 49px)}nav{padding:12px;background:#131922;border-right:1px solid #303a46}.tree-tools{display:flex;align-items:center;justify-content:space-between;gap:8px;margin:2px 7px 9px}.label{margin:0;color:#9ca9b8;font-size:11px;text-transform:uppercase;letter-spacing:.06em}.tree-tools select{max-width:135px;padding:4px 6px;color:inherit;border:1px solid #3a4654;border-radius:5px;background:#1a222c;font-size:12px}.node{display:grid;width:100%;grid-template-columns:10px minmax(0,1fr) auto;gap:8px;align-items:center;padding:9px;color:inherit;text-align:left;border:1px solid transparent;border-radius:7px;background:transparent;cursor:pointer}.node.child{padding-left:25px}.node.selected{background:#222b36;border-color:#465363}.dot{width:8px;height:8px;border-radius:50%;background:#94a3b8}.dot.running,.dot.active{background:#56d487}.dot.waiting,.dot.idle,.dot.launching,.dot.launched{background:#efad4c}.dot.failed,.dot.stale,.dot.offline{background:#f16c6c}.name,.task{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.name{font-weight:500}.task,.state{color:#a9b4c2;font-size:12px}main{min-width:0;padding:18px;background:#171d25}.head{display:flex;justify-content:space-between;gap:12px}.head h1{margin:0;font-size:19px}.facts{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:1px;margin:15px 0;background:#303a46;border:1px solid #303a46;border-radius:8px;overflow:hidden}.fact{padding:10px;background:#11161d}.fact small{display:block;margin-bottom:4px;color:#9ca9b8;text-transform:uppercase}.fact span{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.tabs{display:flex;gap:3px;border-bottom:1px solid #303a46}.tabs button{padding:8px 9px;color:#a9b4c2;border:0;border-bottom:2px solid transparent;background:transparent;cursor:pointer}.tabs button.selected{color:#72b7ff;border-bottom-color:#72b7ff}.child-overview{display:grid;gap:10px;margin-top:10px}.child-card{display:block;width:100%;padding:12px;color:inherit;text-align:left;border:1px solid #303a46;border-radius:8px;background:#11161d;cursor:pointer}.child-card:hover{border-color:#5a6a7d;background:#151c25}.child-card-head{display:flex;justify-content:space-between;gap:10px;margin-bottom:7px}.child-card .task{margin-bottom:8px}.child-card pre{min-height:72px;max-height:180px;margin:0;overflow:auto;pointer-events:none}pre{min-height:220px;margin:9px 0 0;padding:13px;border:1px solid #303a46;border-radius:8px;background:#0d1218;color:#dbe5ef;white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace}.empty{padding:22px;color:#9ca9b8}@media(max-width:650px){.shell{grid-template-columns:1fr}nav{border-right:0;border-bottom:1px solid #303a46}.facts{grid-template-columns:1fr}.head{flex-direction:column}}
</style>
</head>
<body>
<header><strong>Agent Coord</strong><span class="muted" id="health">Loading…</span></header>
<div class="shell"><nav><div class="tree-tools"><p class="label">Process tree</p><select id="sort" aria-label="Sort process tree"><option value="last_activity">Last activity</option><option value="created">Created</option><option value="name">Name</option></select></div><div id="tree"></div></nav><main id="detail"><div class="empty">Select a process.</div></main></div>
<script>
const tree=document.getElementById('tree'),detail=document.getElementById('detail'),health=document.getElementById('health'),sortControl=document.getElementById('sort');const pageQuery=new URLSearchParams(location.search),allowedSorts=['last_activity','created','name'];let snapshot={parents:[]},selected=null,tab='output',sortKey=allowedSorts.includes(pageQuery.get('sort'))?pageQuery.get('sort'):'last_activity';sortControl.value=sortKey;
const esc=v=>String(v??'—').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const messageText=m=>{const state=m.acknowledged_at?'acknowledged':m.delivered_at?'delivered':'pending',sender=m.sender_name||m.sender_session_id;return `${m.created_at}  ${m.classification} · ${state}\nFrom: ${sender}\nThread: ${m.thread_id}\n${m.body}`};
function allNodes(){const nodes=[];for(const p of snapshot.parents){nodes.push({kind:'parent',id:p.session_id,data:p});for(const c of p.children)nodes.push({kind:'child',id:c.delegation_id,data:c,parent:p});}return nodes}
function renderTree(){const nodes=allNodes();if(!nodes.some(n=>n.id===selected))selected=nodes.length?nodes[0].id:null;tree.innerHTML=nodes.map(n=>{const count=n.data.unacknowledged_message_count||0,status=n.data.display_status+(count?' · '+count+' unacked':'');return `<button class="node ${n.kind==='child'?'child ':''}${selected===n.id?'selected':''}" data-id="${esc(n.id)}"><span class="dot ${esc(n.data.display_status)}"></span><span><span class="name">${esc(n.kind==='parent'?(n.data.name||n.data.client+' parent'):(n.data.name||n.data.client+' · '+n.data.bead_id))}</span><span class="task">${esc(n.kind==='parent'?n.data.activity:n.data.instructions)}</span></span><span class="state">${esc(status)}</span></button>`}).join('')||'<div class="empty">No delegations found.</div>';tree.querySelectorAll('button').forEach(b=>b.onclick=()=>{selected=b.dataset.id;renderTree();renderDetail()})}
function renderDetail(){const node=allNodes().find(n=>n.id===selected);if(!node){detail.innerHTML='<div class="empty">No process selected.</div>';return}const d=node.data;const isChild=node.kind==='child';const history=(d.messages||[]).map(messageText).join('\n\n')||'No received messages.';const panes=isChild?{output:d.output||'No output captured yet.',messages:history,activity:`Delegation: ${d.delegation_id}\nParent: ${d.parent_session_id}\nChild session: ${d.child_session_id||'not attached'}\nRuntime: ${d.runtime_kind}\nSupervisor PID: ${d.supervisor_pid||'—'}\nChild PID: ${d.child_pid||'—'}\nCreated: ${d.created_at}\nLast activity: ${d.last_activity_at}`}:{output:'',messages:history,activity:`Session: ${d.session_id}\nClient: ${d.client}\nPresence: ${d.presence}\nActivity: ${d.activity}\nCreated: ${d.created_at}\nLast activity: ${d.last_activity_at}`};const childOverview=!isChild&&tab==='output'?`<div class="child-overview">${(d.children||[]).map(c=>{const childName=c.name||c.client+' · '+c.bead_id,output=(c.output||'No output captured yet.').slice(-2000);return `<button class="child-card" data-child="${esc(c.delegation_id)}"><span class="child-card-head"><span class="name">${esc(childName)}</span><span class="state">${esc(c.display_status)}</span></span><span class="task">${esc(c.instructions)}</span><pre>${esc(output)}</pre></button>`}).join('')||'<div class="empty">This parent has no delegated children.</div>'}</div>`:`<pre>${esc(panes[tab])}</pre>`;detail.innerHTML=`<div class="head"><div><h1>${esc(isChild?(d.name||d.client+' · '+d.bead_id):(d.name||d.client+' parent'))}</h1><div class="muted">${esc(isChild?d.instructions:d.cwd)}</div></div><span>${esc(d.display_status)}</span></div><div class="facts"><div class="fact"><small>${isChild?'Bead':'Session'}</small><span>${esc(isChild?d.bead_id:d.session_id)}</span></div><div class="fact"><small>${isChild?'Scope':'Activity'}</small><span>${esc(isChild?(d.write_scope||[]).join(', '):d.activity)}</span></div><div class="fact"><small>${isChild?'Runtime':'Children'}</small><span>${esc(isChild?d.runtime_kind:d.children.length)}</span></div></div><div class="tabs">${['output','messages','activity'].map(t=>`<button class="${tab===t?'selected':''}" data-tab="${t}">${t[0].toUpperCase()+t.slice(1)}</button>`).join('')}</div>${childOverview}`;detail.querySelectorAll('.tabs button').forEach(b=>b.onclick=()=>{tab=b.dataset.tab;renderDetail()});detail.querySelectorAll('.child-card').forEach(b=>b.onclick=()=>{selected=b.dataset.child;tab='output';renderTree();renderDetail()})}
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
        delegations = [item for item in delegations if item["cwd"] == repository]
    parent_ids = sorted({str(item["parent_session_id"]) for item in delegations})
    if parent_session_id is not None and parent_session_id not in parent_ids:
        requested_parent = store.get_session(parent_session_id)
        if repository is None or requested_parent["cwd"] == repository:
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
                    "default-src 'self'; style-src 'unsafe-inline'; "
                    "script-src 'unsafe-inline'; connect-src 'self'",
                )
                self.end_headers()
                self.wfile.write(body)
            except OSError as exc:
                if exc.errno not in _CLIENT_DISCONNECT_ERRNOS:
                    raise
                self.close_connection = True

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/":
                self._send(
                    HTTPStatus.OK,
                    "text/html; charset=utf-8",
                    _UI_HTML.encode(),
                )
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

        def log_message(self, _format: str, *args: Any) -> None:
            return

    return Handler


class LoopbackHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

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
) -> ThreadingHTTPServer:
    _validate_loopback(host)
    if not 0 <= port <= 65535:
        raise CoordinationError("Agent Coord UI port must be between 0 and 65535.")
    server_type = LoopbackIPv6HTTPServer if ":" in host else LoopbackHTTPServer
    return server_type((host, port), _handler(store, parent_session_id, cwd))


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
