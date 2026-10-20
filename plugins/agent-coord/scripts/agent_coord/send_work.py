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


def dispatcher_profile(runtime, cwd):
    """The palette's dispatcher configuration never supplies worker defaults."""
    with runtime.store._connection() as db:
        profile = db.execute("SELECT * FROM send_work_dispatchers WHERE cwd=?", (cwd,)).fetchone()
        target = profile["thread_id"] if profile else None
        if not profile:
            existing = db.execute("""SELECT b.thread_id FROM browser_sessions b
                JOIN dispatch_settings d ON d.thread_id=b.thread_id AND d.enabled=1
                JOIN work_threads t ON t.thread_id=b.thread_id
                WHERE b.cwd=? AND t.attention!='archived' ORDER BY t.updated_at DESC LIMIT 1""", (cwd,)).fetchone()
            target = existing[0] if existing else None
        record = db.execute("""SELECT b.* FROM browser_sessions b
            JOIN dispatch_settings d ON d.thread_id=b.thread_id AND d.enabled=1
            JOIN work_threads t ON t.thread_id=b.thread_id
            WHERE b.thread_id=? AND b.cwd=? AND t.attention!='archived'""", (target, cwd)).fetchone()
    settings = json.loads(profile["settings_json"]) if profile else {
        "client": "codex", "model": None, "effort": None, "yolo": False}
    if record:
        settings = {key: record[key] for key in ("client", "model", "effort", "yolo")}
        settings["yolo"] = bool(settings["yolo"])
    return {"settings": settings, "thread_id": target if record else None}


def dispatcher_settings(runtime, body):
    if set(body) - {"cwd", "settings"}:
        raise CoordinationError("Choose dispatcher settings for this project.")
    cwd = directory(runtime, body)
    # Serialize settings with dispatcher selection/creation, including the first request.
    with runtime.sessions._thread_lock(channel(cwd)):
        profile = dispatcher_profile(runtime, cwd)
        if "settings" not in body:
            return profile
        settings = body["settings"]
        if not isinstance(settings, dict) or set(settings) != {"client", "model", "effort", "yolo"}:
            raise CoordinationError("Choose the dispatcher's provider, model, effort, and permissions.")
        for field, limit in (("client", 40), ("model", 200)):
            runtime.sessions._text(settings[field], field, limit)
        if settings["effort"] is not None:
            runtime.sessions._text(settings["effort"], "effort", 40)
        runtime.sessions._creation_settings({**settings, "cwd": cwd})
        target = profile["thread_id"]
        with runtime.store._connection() as db:
            pending = db.execute("""SELECT 1 FROM dispatch_requests WHERE dispatcher_id=?
                AND state IN ('pending','routing','creating','queued','sending','uncertain')
                AND json_extract(body_json, '$.sendWork.kind')='dispatcher' LIMIT 1""", (channel(cwd),)).fetchone()
        if pending:
            raise CoordinationError("Wait for pending dispatcher requests before changing its settings.")
        if target and settings["client"] == profile["settings"]["client"]:
            with runtime.sessions._thread_lock(target):
                if runtime.sessions.queue.list(target):
                    raise CoordinationError("Wait for queued dispatcher messages before changing its settings.")
                # Use the normal settings path for runtime reconciliation and busy/permission guards.
                runtime.sessions.update(target, {"yolo": settings["yolo"]})
                # Creation validation above also supports returning to model-default effort.
                with runtime.store._connection() as db:
                    db.execute("UPDATE browser_sessions SET model=?,effort=?,updated_at=? WHERE thread_id=?",
                               (settings["model"], settings["effort"], time.time(), target))
            runtime.changed(target)
        elif target:
            # Provider contexts cannot move between clients. Retain the old conversation.
            target = None
        with runtime.store._connection() as db:
            db.execute("""INSERT INTO send_work_dispatchers(cwd,settings_json,thread_id) VALUES (?,?,?)
                ON CONFLICT(cwd) DO UPDATE SET settings_json=excluded.settings_json,thread_id=excluded.thread_id""",
                       (cwd, json.dumps(settings), target))
        return {"settings": settings, "thread_id": target}


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
    if set(body) - {"cwd", "message", "destination", "requestId", "settings"}:
        raise CoordinationError("Send work accepts only the typed request and destination.")
    cwd = directory(runtime, body)
    message = runtime.sessions._text(body.get("message"), "Request", 100000)
    key = runtime.sessions._text(body.get("requestId"), "Request ID", 160)
    destination = body.get("destination")
    if not isinstance(destination, dict) or not isinstance(destination.get("kind"), str) or destination["kind"] not in {"thread", "new", "dispatcher"}:
        raise CoordinationError("Choose a conversation, New conversation, or Ask dispatcher.")
    if set(destination) != ({"kind", "id"} if destination["kind"] == "thread" else {"kind"}):
        raise CoordinationError("Invalid destination.")
    settings = body.get("settings")
    if "settings" in body:
        if destination["kind"] != "new":
            raise CoordinationError("Settings apply only to a new conversation.")
        if not isinstance(settings, dict) or set(settings) - {"client", "model", "effort", "yolo"}:
            raise CoordinationError("Invalid new conversation settings.")
        for field, limit in (("client", 40), ("model", 200), ("effort", 40)):
            if field in settings and settings[field] is not None:
                runtime.sessions._text(settings[field], field, limit)
        # Validate before saving a receipt or creating a provider conversation.
        runtime.sessions._creation_settings({**settings, "cwd": cwd})
    if destination["kind"] == "thread":
        runtime.sessions._text(destination["id"], "Destination ID", 200)
        target = runtime.sessions._record(destination["id"])
        if target["cwd"] != cwd or runtime.settings(destination["id"])["enabled"]:
            raise CoordinationError("Choose a normal conversation in this project.")
    saved = {"message": message, "images": [], "mentions": [], "windowId": None,
             "sendWork": destination}
    if settings is not None:
        saved["sendWorkSettings"] = settings
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
    # Prevent a settings save from racing dispatcher creation or delivery.
    with runtime.sessions._thread_lock(channel(item["cwd"])):
        _execute(runtime, item)


def _execute(runtime, item):
    identity, body, cwd = item["id"], item["body"], item["cwd"]
    destination = body["sendWork"]
    target = destination.get("id")
    if destination["kind"] == "dispatcher":
        profile = dispatcher_profile(runtime, cwd)
        target = profile["thread_id"]
    if not target:
        runtime.update(identity, "creating")
        settings = body.get("sendWorkSettings", {}) if destination["kind"] == "new" else profile["settings"]
        created = runtime.sessions.create({**settings, "cwd": cwd, "name": "Dispatcher" if destination["kind"] == "dispatcher" else body["message"][:80]},
            on_created=lambda tid: runtime.update(identity, "creating", target_id=tid))
        target = created["session"]["thread_id"]
        if destination["kind"] == "dispatcher":
            runtime.configure(target, {"enabled": True, "defaults": {
                "client": "codex", "model": None, "effort": None, "yolo": False}})
    if destination["kind"] == "dispatcher":
        with runtime.store._connection() as db:
            db.execute("""INSERT INTO send_work_dispatchers(cwd,settings_json,thread_id) VALUES (?,?,?)
                ON CONFLICT(cwd) DO UPDATE SET thread_id=excluded.thread_id""",
                       (cwd, json.dumps(profile["settings"]), target))
    runtime.update(identity, "routing", target_id=target,
                   decision_json=json.dumps({"action": destination["kind"], "thread_id": target, "reason": "user_selected"}))
    with runtime.sessions._thread_lock(target):
        if runtime.sessions._record(target)["cwd"] != cwd:
            raise CoordinationError("The destination moved to another project. Choose it again.")
        # An explicit selection never falls back to a different recipient.
        runtime.sessions.queue.enqueue(target, body, dispatch_id=identity)
