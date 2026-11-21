"""Authorized agent access to the app-owned scheduled prompt state machine."""
from __future__ import annotations

import math
from pathlib import Path
import uuid

from .store import CoordinationError

ACTIONS = {"create", "list", "read", "edit", "pause", "resume", "cancel", "run_now"}
EDIT_FIELDS = {"message", "run_at", "timezone", "recurrence", "settings"}


def validate_request(control, sender, payload):
    action = payload.get("action", "create")
    if not isinstance(action, str) or action not in ACTIONS:
        raise CoordinationError("Choose create, list, read, edit, pause, resume, cancel, or run_now.")
    allowed = {"action"}
    if action == "create":
        allowed |= {"mode", *EDIT_FIELDS, "yolo_authorized"}
    elif action != "list":
        allowed |= {"schedule_id", "authorized_schedule_id"}
        if action != "read":
            allowed.add("version")
        if action == "edit":
            allowed |= EDIT_FIELDS | {"yolo_authorized"}
    if set(payload) - allowed:
        raise CoordinationError("Unknown field for this schedule action.")
    if "yolo_authorized" in payload and type(payload["yolo_authorized"]) is not bool:
        raise CoordinationError("yolo_authorized must be a boolean confirming explicit user authorization.")
    if action not in {"create", "list"}:
        identifier = payload.get("schedule_id")
        try:
            if str(uuid.UUID(identifier)) != identifier:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise CoordinationError("Provide a canonical schedule_id.") from None
        if "authorized_schedule_id" in payload and payload["authorized_schedule_id"] != identifier:
            raise CoordinationError("User authorization must name this exact schedule ID.")
        if action != "read" and (type(payload.get("version")) is not int or payload["version"] < 1):
            raise CoordinationError("Provide the schedule's current integer version.")
        if action == "edit" and not EDIT_FIELDS.intersection(payload):
            raise CoordinationError("Supply at least one schedule setting to edit.")
        # Resolve the saved workspace without disclosing any other schedule data.
        with control.store.reading(), control.store._connection() as db:
            row = db.execute("SELECT * FROM scheduled_prompts WHERE id = ?", (identifier,)).fetchone()
        if row is None:
            raise CoordinationError("Scheduled prompt not found.")
        authorize(row, sender, payload, read=action == "read")
        return payload, row["cwd"]
    if action == "list":
        return payload, control.store.get_session(sender)["cwd"]
    if payload.get("mode") not in ("self", "new"):
        raise CoordinationError("Choose a self or new-agent schedule with valid fields.")
    run_at = payload.get("run_at")
    if (type(run_at) not in (int, float) or not 0 < run_at < 253402300800 or not math.isfinite(run_at)
            or not isinstance(payload.get("timezone"), str) or not payload["timezone"].strip()):
        raise CoordinationError("Provide a future Unix time and an IANA time zone.")
    message = payload.get("message")
    if not isinstance(message, str) or not message.strip() or len(message) > 100000:
        raise CoordinationError("Supply a prompt of 1–100000 characters.")
    if "recurrence" in payload:
        from .schedule_recurrence import validate_recurrence
        validate_recurrence(payload["recurrence"])
    if payload["mode"] == "self":
        if "settings" in payload:
            raise CoordinationError("A self schedule uses the conversation's current settings.")
        return payload, control.settings(sender)["cwd"]
    settings = payload.get("settings")
    if not isinstance(settings, dict) or any(not isinstance(settings.get(key), str) or not settings[key].strip()
                                            for key in ("cwd", "client", "model")):
        raise CoordinationError("New-agent schedules require workspace, client, and model settings.")
    if settings.get("yolo") and not payload.get("yolo_authorized"):
        raise CoordinationError("Enabling yolo requires yolo_authorized=true and explicit user authorization.")
    cwd = str(Path(settings["cwd"]).expanduser().resolve())
    if not Path(cwd).is_dir():
        raise CoordinationError("Choose an existing workspace directory.")
    return {**payload, "settings": {**settings, "cwd": cwd}}, cwd


def authorize(row, sender, payload, *, read=False):
    if (row["creator_session_id"] == sender or (read and row["target_thread_id"] == sender)
            or payload.get("authorized_schedule_id") == row["id"]):
        return
    raise CoordinationError("Only the schedule creator may change it; targets may read. Other access requires explicit user authorization naming authorized_schedule_id.")


def describe(schedules, row):
    item = schedules._describe(row)
    item["message_preview"] = item["message"][:240]
    item["next_run_at"] = item["run_at"] if item["state"] in {"scheduled", "waiting"} else None
    return item


def execute(worker, request, payload):
    schedules = worker.sessions.schedules
    action = payload.get("action", "create")
    sender = request["sender_session_id"]
    if action == "create":
        body = {key: payload[key] for key in ("message", "run_at", "timezone", "recurrence") if key in payload}
        body["id"] = str(uuid.uuid5(uuid.NAMESPACE_URL, "agent-coord:schedule:" + request["request_id"]))
        if payload["mode"] == "self":
            body["target_thread_id"] = sender
        else:
            body["settings"] = payload["settings"]
        return schedules.create(body, creator_session_id=sender)
    with schedules.store.reading(), schedules.store._connection() as db:
        if action == "list":
            rows = db.execute("""SELECT * FROM scheduled_prompts WHERE creator_session_id = ?
                OR target_thread_id = ? ORDER BY run_at DESC, id""", (sender, sender)).fetchall()
        else:
            rows = [schedules._get(db, payload["schedule_id"])]
    if action == "list":
        return {"schedules": [describe(schedules, row) for row in rows]}
    row = rows[0]
    authorize(row, sender, payload, read=action == "read")
    if action == "read":
        return describe(schedules, row)
    body = {"action": action, "version": payload["version"]}
    if action == "edit":
        item = schedules._describe(row)
        body.update({key: item[key] for key in ("message", "run_at", "timezone", "recurrence", "mentions")})
        if row["target_thread_id"]:
            body["target_thread_id"] = row["target_thread_id"]
            if "settings" in payload:
                raise CoordinationError("Existing-thread schedules use the thread's current settings.")
        else:
            body["settings"] = {key: item["settings"][key] for key in
                                ("cwd", "client", "model", "effort", "yolo", "repository_id", "project_id")
                                if key in item["settings"]}
            if "settings" in payload:
                if not isinstance(payload["settings"], dict):
                    raise CoordinationError("settings must be an object.")
                body["settings"].update(payload["settings"])
            if body["settings"].get("yolo") and not item["settings"].get("yolo") and not payload.get("yolo_authorized"):
                raise CoordinationError("Enabling yolo requires yolo_authorized=true and explicit user authorization.")
        body.update({key: payload[key] for key in EDIT_FIELDS - {"settings"} if key in payload})
        if "message" in payload:
            # Old mention offsets belong to the old text.
            body["mentions"] = []
    return schedules.change(payload["schedule_id"], body, request_id=request["request_id"])
