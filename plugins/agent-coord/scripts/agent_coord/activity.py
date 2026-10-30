"""Local activity evidence, deliberately separate from conversation content.

Ranges are Unix seconds [start, end); the first query caps end at its clock.
Pagination freezes the event high-water mark and rejects changed organization
or cleared history. Summaries always cover the full range, not just the page.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import uuid
from collections import defaultdict
from pathlib import Path

from .organization import app_agent_creators
from .store import CoordinationError
from .workspaces import matches_workspace

KINDS = frozenset({"visit_started", "visit_ended", "message_submitted",
                   "decision_submitted", "organization_changed", "turn_started", "turn_ended"})
TURN_KINDS = frozenset({"turn_started", "turn_ended"})
VISIT_KINDS = frozenset({"visit_started", "visit_ended"})
ACTORS = frozenset({"human", "agent", "system"})
GROUPINGS = frozenset({"thread", "project", "domain", "group"})
METRICS = ("visits", "return_visits", "visits_without_message", "messages",
           "decisions", "threads", "incomplete_visits")
COVERAGE_NOTE = "No historical silent-view backfill. Only explicitly recorded activity is available."


def _text(value, label, limit=200, *, empty=False):
    if (not isinstance(value, str) or len(value) > limit or "\0" in value
            or (not empty and not value.strip())):
        raise CoordinationError(f"{label} must be {'non-empty ' if not empty else ''}text of at most {limit} characters.")
    return value


def _timestamp(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CoordinationError(f"{label} must be finite Unix epoch seconds.")
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise CoordinationError(f"{label} must be finite Unix epoch seconds.") from exc
    if not math.isfinite(result):
        raise CoordinationError(f"{label} must be finite Unix epoch seconds.")
    return result


def _path(value, label):
    _text(value, label, 4000)
    try:
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise ValueError("relative path")
        return str(path.resolve())
    except (OSError, ValueError, RuntimeError) as exc:
        raise CoordinationError(f"{label} must be an absolute directory path.") from exc


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _details(value):
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise CoordinationError("Activity details must be a metadata object.")
    # This is metadata, never a second copy of messages or decision answers.
    forbidden = {"body", "text", "message", "prompt", "content", "answer",
                 "instructions", "originalrequest", "messagebody", "promptbody",
                 "messagetext", "prompttext", "input", "output", "transcript"}

    remaining = 512

    def check(item, depth=0):
        nonlocal remaining
        remaining -= 1
        if remaining < 0:
            raise CoordinationError("Activity details contain too many values.")
        if depth > 4:
            raise CoordinationError("Activity details may be at most four levels deep.")
        if isinstance(item, dict):
            if len(item) > 64:
                raise CoordinationError("Activity details contain too many fields.")
            for key, child in item.items():
                _text(key, "Activity detail key", 64)
                if "".join(c for c in key.casefold() if c.isalnum()) in forbidden:
                    raise CoordinationError("Activity details cannot contain prompt, message, or answer bodies.")
                check(child, depth + 1)
        elif isinstance(item, list):
            if len(item) > 64:
                raise CoordinationError("Activity detail lists may contain at most 64 items.")
            for child in item:
                check(child, depth + 1)
        elif isinstance(item, str):
            _text(item, "Activity detail value", 1024, empty=True)
        elif item is None or isinstance(item, bool):
            pass
        elif isinstance(item, (int, float)):
            _timestamp(item, "Activity detail number")
        else:
            raise CoordinationError("Activity details must contain JSON values.")

    check(value)
    try:
        encoded = _json(value)
        if len(encoded.encode("utf-8")) > 8192:
            raise CoordinationError("Activity details must be at most 8192 bytes.")
    except (ValueError, UnicodeError) as exc:
        raise CoordinationError("Activity details must contain valid JSON metadata.") from exc
    return encoded


class ActivityStore:
    def __init__(self, store, *, initialize=True):
        self.store = store
        if not initialize:
            return
        now = _timestamp(store.clock(), "Clock")
        with store._connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS activity_settings (
                    id INTEGER PRIMARY KEY CHECK(id = 1), enabled INTEGER NOT NULL,
                    recording_started_at REAL NOT NULL, cursor_secret TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 0,
                    history_cleared_at REAL, cleared_before REAL
                );
                CREATE TABLE IF NOT EXISTS activity_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE, kind TEXT NOT NULL,
                    thread_id TEXT NOT NULL REFERENCES work_threads(thread_id),
                    occurred_at REAL NOT NULL, received_at REAL NOT NULL,
                    actor TEXT NOT NULL, source TEXT NOT NULL,
                    window_id TEXT, visit_id TEXT, turn_id TEXT,
                    details_json TEXT NOT NULL, snapshot_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS activity_events_time_idx
                    ON activity_events(occurred_at, sequence);
                CREATE INDEX IF NOT EXISTS activity_events_thread_idx
                    ON activity_events(thread_id, occurred_at, sequence);
                CREATE INDEX IF NOT EXISTS activity_events_visit_idx
                    ON activity_events(thread_id, window_id, visit_id, kind, actor);
                CREATE INDEX IF NOT EXISTS activity_events_turn_idx
                    ON activity_events(thread_id, turn_id, kind);
                CREATE TABLE IF NOT EXISTS activity_pauses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at REAL NOT NULL, ended_at REAL
                );
                CREATE TABLE IF NOT EXISTS activity_discarded_visits (
                    event_key TEXT PRIMARY KEY, reason TEXT NOT NULL
                );
            """)
            db.execute("""INSERT OR IGNORE INTO activity_settings
                          (id, enabled, recording_started_at, cursor_secret) VALUES (1, 1, ?, ?)""",
                       (now, uuid.uuid4().hex + uuid.uuid4().hex))

    @staticmethod
    def _settings(db):
        return dict(db.execute("SELECT * FROM activity_settings WHERE id=1").fetchone())

    @staticmethod
    def _public_settings(settings):
        return {"enabled": bool(settings["enabled"]),
                "recording_started_at": settings["recording_started_at"]}

    def settings(self):
        with self.store._connection() as db:
            return self._public_settings(self._settings(db))

    def configure(self, enabled: bool):
        if not isinstance(enabled, bool):
            raise CoordinationError("Activity recording enabled must be a boolean.")
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._settings(db)
            if bool(current["enabled"]) != enabled:
                now = _timestamp(self.store.clock(), "Clock")
                if enabled:
                    db.execute("UPDATE activity_pauses SET ended_at=? WHERE ended_at IS NULL", (now,))
                else:
                    db.execute("INSERT INTO activity_pauses (started_at) VALUES (?)", (now,))
                db.execute("UPDATE activity_settings SET enabled=?, revision=revision+1 WHERE id=1", (int(enabled),))
            return self._public_settings(self._settings(db))

    @staticmethod
    def _thread_rows(db, thread_id=None):
        sql = """SELECT t.thread_id, t.title, COALESCE(t.project_cwd, s.cwd) AS project_cwd,
                        t.domain, s.cwd, s.client, o.project_id AS group_id, p.name AS group_name,
                        t.forked_from_thread_id, t.forked_from_turn_id
                 FROM work_threads t JOIN sessions s ON s.session_id=t.thread_id
                 LEFT JOIN thread_organization o ON o.thread_id=t.thread_id
                 LEFT JOIN named_projects p ON p.id=o.project_id"""
        return db.execute(sql + (" WHERE t.thread_id=?" if thread_id is not None else ""),
                          (thread_id,) if thread_id is not None else ()).fetchall()

    @staticmethod
    def _creators(db):
        creators = app_agent_creators(db)
        for row in db.execute("""SELECT d.child_session_id, d.parent_session_id,
                t.title, s.cwd, COALESCE(t.project_cwd, s.cwd) AS project_cwd
                FROM delegations d JOIN sessions s ON s.session_id=d.parent_session_id
                LEFT JOIN work_threads t ON t.thread_id=d.parent_session_id
                WHERE d.child_session_id IS NOT NULL ORDER BY d.created_at, d.delegation_id"""):
            creators.setdefault(row["child_session_id"], {
                "thread_id": row["parent_session_id"], "title": row["title"],
                "cwd": row["cwd"], "project_cwd": row["project_cwd"], "mode": "delegation"})
        return creators

    @staticmethod
    def _snapshot(row, creators):
        result = dict(row)
        result["project"] = result["project_cwd"]
        result["domain"] = result["domain"] or None
        creator = creators.get(row["thread_id"])
        result["created_by"] = creator
        result["created_by_thread_id"] = creator["thread_id"] if creator else None
        result["creation_mode"] = creator["mode"] if creator else None
        return result

    @staticmethod
    def _event(row):
        result = dict(row)
        result["details"] = json.loads(result.pop("details_json"))
        result["snapshot"] = json.loads(result.pop("snapshot_json"))
        return result

    @staticmethod
    def _receipt(row, *, duplicate=False):
        return {"status": "duplicate" if duplicate else "recorded", "event_id": row["event_id"],
                "sequence": row["sequence"], "occurred_at": row["occurred_at"],
                "received_at": row["received_at"]}

    @staticmethod
    def _visit_key(settings, event_id):
        # Preserve deletion/rejection across retries without retaining an event
        # ID, thread ID, timestamp, or organization snapshot in the tombstone.
        return hmac.new(settings["cursor_secret"].encode(),
                        ("discarded-visit:" + event_id).encode(), hashlib.sha256).hexdigest()

    @staticmethod
    def _skip(db, event_id, reason, *, visit_key=None):
        if visit_key is not None:
            db.execute("INSERT OR IGNORE INTO activity_discarded_visits (event_key, reason) VALUES (?, ?)",
                       (visit_key, reason))
        return {"status": "skipped", "reason": reason, "event_id": event_id}

    def record(self, kind, thread_id, *, event_id=None, occurred_at=None, actor="human",
               source="app", window_id=None, visit_id=None, turn_id=None, details=None):
        if not isinstance(kind, str) or kind not in KINDS:
            raise CoordinationError("Unknown activity kind.")
        if not isinstance(actor, str) or actor not in ACTORS:
            raise CoordinationError("Activity actor must be human, agent, or system.")
        if kind in TURN_KINDS and actor != "agent":
            raise CoordinationError("Turn lifecycle activity must have an agent actor.")
        _text(thread_id, "Thread ID")
        event_id = str(uuid.uuid4()) if event_id is None else _text(event_id, "Event ID")
        _text(source, "Activity source", 64)
        for label, value in (("Window ID", window_id), ("Visit ID", visit_id), ("Turn ID", turn_id)):
            if value is not None:
                _text(value, label)
        details_json = _details(details)
        if occurred_at is not None:
            occurred_at = _timestamp(occurred_at, "Activity time")
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            return self._record(db, kind, thread_id, event_id=event_id, occurred_at=occurred_at,
                                actor=actor, source=source, window_id=window_id,
                                visit_id=visit_id, turn_id=turn_id, details_json=details_json)

    def _record(self, db, kind, thread_id, *, event_id, occurred_at, actor, source,
                window_id=None, visit_id=None, turn_id=None, details_json="{}"):
        """Insert validated metadata using the caller's transaction."""
        threads = self._thread_rows(db, thread_id)
        if not threads:
            raise CoordinationError("Work thread not found.")
        previous = db.execute("SELECT * FROM activity_events WHERE event_id=?", (event_id,)).fetchone()
        fields = dict(kind=kind, thread_id=thread_id, actor=actor, source=source,
                      window_id=window_id, visit_id=visit_id, turn_id=turn_id, details_json=details_json)
        if previous is not None:
            if (any(previous[key] != value for key, value in fields.items())
                    or occurred_at is not None and previous["occurred_at"] != occurred_at):
                raise CoordinationError("Event ID already belongs to different activity.")
            return self._receipt(previous, duplicate=True)
        settings = self._settings(db)
        visit_key = self._visit_key(settings, event_id) if kind in VISIT_KINDS else None
        if visit_key is not None:
            discarded = db.execute("SELECT reason FROM activity_discarded_visits WHERE event_key=?",
                                   (visit_key,)).fetchone()
            if discarded:
                return self._skip(db, event_id, discarded["reason"])
        if not settings["enabled"]:
            return self._skip(db, event_id, "recording_paused", visit_key=visit_key)
        received_at = _timestamp(self.store.clock(), "Clock")
        occurred_at = received_at if occurred_at is None else occurred_at
        # Visit timestamps come from buffered browser observations. Backend
        # submissions keep their existing acceptance-time behavior instead.
        if visit_key is not None:
            if settings["cleared_before"] is not None and occurred_at < settings["cleared_before"]:
                return self._skip(db, event_id, "history_cleared", visit_key=visit_key)
            if occurred_at < settings["recording_started_at"]:
                return self._skip(db, event_id, "before_recording_started", visit_key=visit_key)
            if db.execute("""SELECT 1 FROM activity_pauses WHERE started_at<=?
                              AND (ended_at IS NULL OR ?<ended_at) LIMIT 1""",
                          (occurred_at, occurred_at)).fetchone():
                return self._skip(db, event_id, "recording_paused_at_occurrence", visit_key=visit_key)
        snapshot = self._snapshot(threads[0], self._creators(db))
        db.execute("""INSERT INTO activity_events (event_id, kind, thread_id, occurred_at,
            received_at, actor, source, window_id, visit_id, turn_id, details_json, snapshot_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (event_id, kind, thread_id, occurred_at, received_at, actor, source,
             window_id, visit_id, turn_id, details_json, _json(snapshot)))
        return self._receipt(db.execute("SELECT * FROM activity_events WHERE event_id=?", (event_id,)).fetchone())

    def record_turn(self, db, kind, thread_id, turn_key, *, turn_id, started_at,
                    occurred_at, status=None, context=None):
        """Commit lifecycle evidence atomically with its thread state transition."""
        occurred_at = _timestamp(occurred_at, "Turn time")
        if started_at is not None:
            started_at = _timestamp(started_at, "Turn start")
        details = {"started_at": started_at, "trigger": "unknown"}
        if kind == "turn_started":
            origin = (context or {}).get("origin")
            details["trigger"] = {"user": "human", "coordination": "inbox",
                                  "dispatch": "inbox", "schedule": "schedule",
                                  "agent": "agent"}.get(origin, "unknown")
            if origin in {"user", "coordination", "dispatch", "schedule", "agent"}:
                details["origin"] = origin
            # The scheduler binds its accepted native turn before ThreadStore
            # receives the event. Match that exact turn, never thread ancestry.
            if turn_id and db.execute("SELECT 1 FROM sqlite_master WHERE name='scheduled_prompts'").fetchone():
                if db.execute("SELECT 1 FROM scheduled_prompts WHERE thread_id=? AND turn_id=?",
                              (thread_id, turn_id)).fetchone():
                    details["trigger"] = "schedule"
        else:
            start = db.execute("""SELECT details_json FROM activity_events
                WHERE thread_id=? AND turn_id=? AND kind='turn_started' ORDER BY sequence LIMIT 1""",
                               (thread_id, turn_key)).fetchone()
            if start:
                details.update(json.loads(start["details_json"]))
            details["status"] = status
        # Native IDs may be absent in terminal hooks; turn_key is the durable
        # fallback already used for completion deduplication.
        details["native_turn_id"] = turn_id
        event_id = "turn:" + hashlib.sha256(_json([kind, thread_id, turn_key]).encode()).hexdigest()
        return self._record(db, kind, thread_id, event_id=event_id, occurred_at=occurred_at,
                            actor="agent", source="turn_lifecycle", turn_id=turn_key,
                            details_json=_details(details))

    def clear(self, before=None):
        if before is not None:
            before = _timestamp(before, "Clear cutoff")
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            now = _timestamp(self.store.clock(), "Clock")
            settings = self._settings(db)
            # A full clear also deletes events at/after its clock (client skew).
            # Remember only keyed IDs so their exact retries stay discarded.
            removed_visits = db.execute("""SELECT event_id FROM activity_events
                WHERE kind IN ('visit_started', 'visit_ended')""" +
                (" AND occurred_at < ?" if before is not None else ""),
                (before,) if before is not None else ())
            for row in removed_visits:
                db.execute("""INSERT OR IGNORE INTO activity_discarded_visits (event_key, reason)
                              VALUES (?, 'history_cleared')""", (self._visit_key(settings, row["event_id"]),))
            deleted = db.execute("DELETE FROM activity_events" + (" WHERE occurred_at < ?" if before is not None else ""),
                                 (before,) if before is not None else ()).rowcount
            db.execute("""UPDATE activity_settings SET revision=revision+1, history_cleared_at=?,
                          cleared_before=MAX(COALESCE(cleared_before, ?), ?) WHERE id=1""",
                       (now, now if before is None else before, now if before is None else before))
            return {"status": "cleared", "deleted": deleted, "before": before}

    @staticmethod
    def _encode_cursor(value, secret):
        data = base64.urlsafe_b64encode(_json(value).encode()).decode().rstrip("=")
        signature = hmac.new(secret.encode(), data.encode(), hashlib.sha256).hexdigest()
        return data + "." + signature

    @staticmethod
    def _decode_cursor(cursor, secret):
        _text(cursor, "Activity cursor", 16000)
        try:
            data, signature = cursor.split(".")
            expected = hmac.new(secret.encode(), data.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, signature):
                raise ValueError("signature")
            result = json.loads(base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)))
            if result["version"] != 1:
                raise ValueError("version")
            return result
        except (ValueError, KeyError, TypeError, UnicodeError) as exc:
            raise CoordinationError("Invalid activity cursor. Start a new query.") from exc

    @staticmethod
    def _visible(snapshot, allowed_cwd):
        return (matches_workspace(snapshot["cwd"], allowed_cwd)
                and matches_workspace(snapshot["project_cwd"], allowed_cwd))

    @staticmethod
    def _redact_creator(snapshot, allowed_cwd):
        creator = snapshot.get("created_by")
        if creator and allowed_cwd is not None and not (
                creator.get("cwd") and creator.get("project_cwd")
                and matches_workspace(creator["cwd"], allowed_cwd)
                and matches_workspace(creator["project_cwd"], allowed_cwd)):
            snapshot["created_by"] = None
            snapshot["created_by_thread_id"] = None
            snapshot["creator_outside_workspace"] = True

    def query(self, *, start=None, end=None, project=None, domain=None, group=None,
              thread_id=None, group_by="thread", limit=100, cursor=None, allowed_cwd=None,
              include_turns=True):
        if not isinstance(group_by, str) or group_by not in GROUPINGS:
            raise CoordinationError("Group activity by thread, project, domain, or group.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise CoordinationError("Activity limit must be an integer from 1 to 500.")
        if not isinstance(include_turns, bool):
            raise CoordinationError("Include turns must be a boolean.")
        start = None if start is None else _timestamp(start, "Range start")
        end = None if end is None else _timestamp(end, "Range end")
        project = None if project is None else _path(project, "Project")
        allowed_cwd = None if allowed_cwd is None else _path(allowed_cwd, "Workspace")
        if domain is not None:
            domain = " ".join(_text(domain, "Domain", 64, empty=True).split()).casefold()
        if group is not None:
            group = _text(group, "Group", empty=True)
        if thread_id is not None:
            _text(thread_id, "Thread ID")
        filters = {"project": project, "domain": domain, "group": group,
                   "thread_id": thread_id, "allowed_cwd": allowed_cwd, "include_turns": include_turns}
        with self.store._connection() as db:
            db.execute("BEGIN")  # One coherent read snapshot; no writer lock.
            settings = self._settings(db)
            saved = self._decode_cursor(cursor, settings["cursor_secret"]) if cursor is not None else None
            if saved:
                if saved["filters"] != filters or saved["group_by"] != group_by:
                    raise CoordinationError("Activity cursor does not match these filters.")
                if saved["revision"] != settings["revision"]:
                    raise CoordinationError("Activity history was cleared or recording settings changed. Start a new query.")
                as_of = saved["as_of"]
                if ((start is not None and start != saved["start"])
                        or (end is not None and min(end, as_of) != saved["end"])):
                    raise CoordinationError("Activity cursor does not match this range.")
                start, end, high_water = saved["start"], saved["end"], saved["high_water"]
            else:
                as_of = _timestamp(self.store.clock(), "Clock")
                end = as_of if end is None else min(end, as_of)
                start = end - 86400 if start is None else start
                if start >= end:
                    raise CoordinationError("Activity range start must be earlier than end.")
                high_water = db.execute("SELECT COALESCE(MAX(sequence), 0) FROM activity_events").fetchone()[0]
            creators = self._creators(db)
            threads = {}
            for row in self._thread_rows(db, thread_id):
                item = self._snapshot(row, creators)
                if not self._visible(item, allowed_cwd):
                    continue
                if (project is not None and item["project_cwd"] != project
                        or domain is not None and (item["domain"] or "") != domain
                        or group is not None and (item["group_id"] or "") != group):
                    continue
                self._redact_creator(item, allowed_cwd)
                threads[item["thread_id"]] = item
            events = []
            first_event_at = None
            # Find the first accessible evidence without loading all old details.
            for row in db.execute("""SELECT thread_id, occurred_at, snapshot_json, kind FROM activity_events
                    WHERE sequence<=? AND occurred_at<? ORDER BY occurred_at, sequence""", (high_water, end)):
                if (include_turns or row["kind"] not in TURN_KINDS) and row["thread_id"] in threads and self._visible(json.loads(row["snapshot_json"]), allowed_cwd):
                    first_event_at = row["occurred_at"]
                    break
            # Aggregate the whole selected range, independently of page size.
            rows = db.execute("""SELECT * FROM activity_events WHERE sequence<=? AND occurred_at>=?
                                 AND occurred_at<? ORDER BY occurred_at, sequence""", (high_water, start, end))
            for row in rows:
                if row["thread_id"] not in threads or not include_turns and row["kind"] in TURN_KINDS:
                    continue
                event = self._event(row)
                if not self._visible(event["snapshot"], allowed_cwd):
                    continue
                self._redact_creator(event["snapshot"], allowed_cwd)
                event["current"] = threads[event["thread_id"]]
                events.append(event)
            # Unrelated new threads do not invalidate a page, but reassigning a
            # thread contributing evidence cannot silently change its summary.
            organization = hashlib.sha256(_json({e["thread_id"]: e["current"] for e in events}).encode()).hexdigest()
            if saved and saved["organization"] != organization:
                raise CoordinationError("Thread organization changed. Start a new activity query.")
            # Browser observation and backend acceptance clocks can differ. An
            # explicit visit identity is stronger evidence than timestamp order.
            # Messages before start can complete this association, but evidence
            # at/after the query cutoff cannot affect a historical report.
            messaged_visits = set()
            visit_keys = {(e["thread_id"], e["window_id"], e["visit_id"]) for e in events
                          if e["actor"] == "human" and e["kind"] == "visit_started" and e["visit_id"]}
            for key in visit_keys:
                messages = db.execute("""SELECT snapshot_json FROM activity_events
                    WHERE thread_id=? AND window_id IS ? AND visit_id=? AND kind='message_submitted'
                    AND actor='human' AND sequence<=? AND occurred_at<?""", (*key, high_water, end))
                if any(self._visible(json.loads(r["snapshot_json"]), allowed_cwd) for r in messages):
                    messaged_visits.add(key)
            pauses = [{"start": r["started_at"], "end": r["ended_at"]}
                      for r in db.execute("SELECT * FROM activity_pauses WHERE started_at<? ORDER BY id", (end,))
                      if r["ended_at"] is None or r["ended_at"] > start]
            coverage = {**self._public_settings(settings), "first_event_at": first_event_at,
                        "note": COVERAGE_NOTE, "paused_intervals": pauses,
                        "history_cleared_at": settings["history_cleared_at"],
                        "cleared_before": settings["cleared_before"],
                        "complete": (start >= settings["recording_started_at"] and not pauses
                                     and (settings["cleared_before"] is None or start >= settings["cleared_before"]))}

        totals, groups = self._summarize(events, threads, group_by, messaged_visits)
        after = tuple(saved["after"]) if saved else None
        remaining = [e for e in events if after is None or (e["occurred_at"], e["sequence"]) > after]
        page = remaining[:limit]
        next_cursor = None
        if len(remaining) > limit:
            last = page[-1]
            next_cursor = self._encode_cursor({"version": 1, "filters": filters, "group_by": group_by,
                "start": start, "end": end, "as_of": as_of, "high_water": high_water,
                "revision": settings["revision"], "organization": organization,
                "after": [last["occurred_at"], last["sequence"]]}, settings["cursor_secret"])
        return {"start": start, "end": end, "as_of": as_of, "group_by": group_by,
                "totals": totals, "groups": groups, "events": page, "next_cursor": next_cursor,
                "coverage": coverage, "filters": filters}

    @staticmethod
    def _summarize(events, threads, group_by, messaged_visits):
        by_thread = defaultdict(list)
        for event in events:
            if event["actor"] == "human":
                by_thread[event["thread_id"]].append(event)
        groups = {}
        totals = dict.fromkeys(METRICS, 0)
        for thread_id, evidence in by_thread.items():
            starts = [e for e in evidence if e["kind"] == "visit_started"]
            visits = defaultdict(list)
            for event in evidence:
                if event["visit_id"] is not None:
                    visits[(event["window_id"], event["visit_id"])].append(event)
            counts = dict.fromkeys(METRICS, 0)
            counts.update(visits=len(starts), return_visits=max(0, len(starts) - 1), threads=1,
                          messages=sum(e["kind"] == "message_submitted" for e in evidence),
                          decisions=sum(e["kind"] == "decision_submitted" for e in evidence))
            for visit in starts:
                related = visits.get((visit["window_id"], visit["visit_id"]), []) if visit["visit_id"] else []
                endings = [e for e in related if e["kind"] == "visit_ended" and e["occurred_at"] >= visit["occurred_at"]]
                if not endings:
                    counts["incomplete_visits"] += 1
                elif (thread_id, visit["window_id"], visit["visit_id"]) not in messaged_visits:
                    counts["visits_without_message"] += 1
            thread = threads[thread_id]
            key = {"thread": thread_id, "project": thread["project_cwd"],
                   "domain": thread["domain"], "group": thread["group_id"]}[group_by]
            label = {"thread": thread["title"], "project": thread["project_cwd"],
                     "domain": thread["domain"] or "No domain", "group": thread["group_name"] or "No group"}[group_by]
            if key not in groups:
                groups[key] = {"key": key, "label": label, **dict.fromkeys(METRICS, 0)}
                if group_by == "thread":
                    groups[key].update(thread)
            for metric in METRICS:
                groups[key][metric] += counts[metric]
                totals[metric] += counts[metric]
        return totals, sorted(groups.values(), key=lambda g: (g["label"].casefold(), g["key"] or ""))
