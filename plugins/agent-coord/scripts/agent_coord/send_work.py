"""Explicit, typed-only work delivery without a source conversation.

Reuse dispatch receipts and the durable queue. A workspace channel owns palette
receipts; it is not an app conversation and never receives provider input.
"""
from __future__ import annotations

import hashlib
import json
import re
import time

from .attention import ClassificationError, load_config
from .dispatch_routing import RoutingPolicy, build_state, classify
from .dispatch_snapshot import read_snapshot
from .store import CoordinationError
from .workspaces import resolve_workspace


def channel(cwd):
    return "send-work:" + hashlib.sha256(cwd.encode()).hexdigest()


def directory(runtime, body):
    if not isinstance(body.get("cwd"), str) or not body["cwd"].strip():
        raise CoordinationError("Open a project before sending work.")
    return resolve_workspace(body["cwd"], workspace=runtime.sessions.cwd)


def suggestions(runtime, body):
    if set(body) - {"cwd", "message"}:
        raise CoordinationError("Suggestions accept only a project and typed request.")
    cwd = directory(runtime, body)
    message = runtime.sessions._text(body.get("message"), "Request", 100000)
    snapshot = read_snapshot(runtime.store.database_path, cwd=cwd)
    policy = RoutingPolicy(timeout=3)
    # A common short action should retrieve deployment history before Jev sees
    # the bounded shortlist. The classifier still receives the original input.
    query = re.sub(r"\bredeploy\b", "deploy deployment", message, flags=re.IGNORECASE)
    state = build_state(snapshot, {"cwd": cwd, "message": query,
                                  "request_id": "preview", "dispatcher_id": channel(cwd)}, policy)
    state["request"]["message"] = message[:6000]
    state["request"]["truncated"] = len(message) > 6000
    candidates = state["candidates"]
    ranked = False
    config = load_config(runtime.store.database_path)
    if config and candidates:
        try:
            answer = classify(state, config, policy)
            candidates = sorted(candidates, key=lambda c: answer["probabilities"][c["key"]], reverse=True)
            candidates = [c for c in candidates if answer["probabilities"][c["key"]] >= .1]
            ranked = True
        except ClassificationError:
            pass  # Local matches remain available without Jev.
    if not ranked:
        candidates = [c for c in candidates if c["retrieval_score"] > .5]
    return {"candidates": [{"id": c["thread_id"], "title": c["title"],
                            "summary": c["checkpoint"]["text"], "running": c["running"],
                            "closed": c["attention"] == "archived"} for c in candidates],
            "ranked": ranked}


def submit(runtime, body):
    if set(body) - {"cwd", "message", "destination", "requestId"}:
        raise CoordinationError("Send work accepts only the typed request and destination.")
    cwd = directory(runtime, body)
    message = runtime.sessions._text(body.get("message"), "Request", 100000)
    key = runtime.sessions._text(body.get("requestId"), "Request ID", 160)
    destination = body.get("destination")
    if not isinstance(destination, dict) or not isinstance(destination.get("kind"), str) or destination["kind"] not in {"thread", "new", "dispatcher"}:
        raise CoordinationError("Choose a conversation, New conversation, or Ask dispatcher.")
    if set(destination) != ({"kind", "id"} if destination["kind"] == "thread" else {"kind"}):
        raise CoordinationError("Invalid destination.")
    if destination["kind"] == "thread":
        runtime.sessions._text(destination["id"], "Destination ID", 200)
        target = runtime.sessions._record(destination["id"])
        if target["cwd"] != cwd or runtime.settings(destination["id"])["enabled"]:
            raise CoordinationError("Choose a normal conversation in this project.")
    saved = {"message": message, "images": [], "mentions": [], "windowId": None,
             "sendWork": destination}
    payload = json.dumps(saved, sort_keys=True)
    owner = channel(cwd)
    with runtime.store._connection() as db:
        db.execute("BEGIN IMMEDIATE")
        prior = db.execute("SELECT * FROM dispatch_requests WHERE dispatcher_id=? AND request_id=?", (owner, key)).fetchone()
        if prior:
            if prior["body_json"] != payload:
                raise CoordinationError("This requestId already belongs to different input.")
            return {"receipt": status(runtime, {"cwd": cwd, "id": prior["id"]}), "replayed": True}
        cursor = db.execute("""INSERT INTO dispatch_requests
            (dispatcher_id,request_id,body_json,cwd,state,created_at) VALUES (?,?,?,?,'pending',?)""",
                            (owner, key, payload, cwd, time.time()))
        identity = cursor.lastrowid
        runtime.transition(db, identity, "pending")
    runtime.wake()
    return {"receipt": status(runtime, {"cwd": cwd, "id": identity}), "replayed": False}


def status(runtime, body):
    if set(body) != {"cwd", "id"} or type(body["id"]) is not int:
        raise CoordinationError("Choose a send-work receipt.")
    cwd = directory(runtime, body)
    receipt = runtime.get(body["id"])
    if receipt["dispatcher_id"] != channel(cwd):
        raise CoordinationError("Send-work receipt not found in this project.")
    with runtime.store._connection() as db:
        row = db.execute("SELECT title FROM work_threads WHERE thread_id=?", (receipt["target_id"],)).fetchone()
        queued = db.execute("SELECT state,owner,error FROM browser_message_queue WHERE dispatch_id=?", (receipt["id"],)).fetchone()
    receipt["target_title"] = row[0] if row else None
    if queued and (queued["state"] == "paused" or queued["owner"] != runtime.sessions.queue.owner):
        receipt.update(state="uncertain" if receipt["state"] in {"sending", "uncertain"} else "paused",
                       error=queued["error"] or "Review the destination queue before resuming.")
    return receipt


def execute(runtime, item):
    identity, body, cwd = item["id"], item["body"], item["cwd"]
    destination = body["sendWork"]
    target = destination.get("id")
    if destination["kind"] == "dispatcher":
        with runtime.store._connection() as db:
            existing = db.execute("""SELECT b.thread_id FROM browser_sessions b
                JOIN dispatch_settings d ON d.thread_id=b.thread_id AND d.enabled=1
                JOIN work_threads t ON t.thread_id=b.thread_id
                WHERE b.cwd=? AND t.attention!='archived' ORDER BY t.updated_at DESC LIMIT 1""", (cwd,)).fetchone()
        target = existing[0] if existing else None
    if not target:
        runtime.update(identity, "creating")
        created = runtime.sessions.create({"cwd": cwd, "name": "Dispatcher" if destination["kind"] == "dispatcher" else body["message"][:80]},
            on_created=lambda tid: runtime.update(identity, "creating", target_id=tid))
        target = created["session"]["thread_id"]
        if destination["kind"] == "dispatcher":
            runtime.configure(target, {"enabled": True})
    runtime.update(identity, "routing", target_id=target,
                   decision_json=json.dumps({"action": destination["kind"], "thread_id": target, "reason": "user_selected"}))
    with runtime.sessions._thread_lock(target):
        if runtime.sessions._record(target)["cwd"] != cwd:
            raise CoordinationError("The destination moved to another project. Choose it again.")
        # An explicit selection never falls back to a different recipient.
        runtime.sessions.queue.enqueue(target, body, dispatch_id=identity)
