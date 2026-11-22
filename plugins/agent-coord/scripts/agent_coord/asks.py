"""Asks: durable items an agent addresses to the user, resolved in one place.

An ask is a persisted item tied to the asking session. It has a kind (decision,
action, approval, review or request) and the user closes it with a resolution
for that kind. The resolution travels back through the ordinary message path,
so an idle asker resumes exactly the way it does for a peer message. Blocking
asks also mark the thread as needing the user; non-blocking asks state a
default and let the agent continue.
"""
from __future__ import annotations

import json
import math
import statistics
import time
from datetime import datetime
from typing import Any, Callable
from urllib.parse import urlsplit

from .store import CoordinationError, _iso

MAX_QUESTION = 2000
MAX_CONTEXT = 4000
MAX_OPTION = 200
MAX_OPTIONS = 8
MAX_ANSWER = 4000
MAX_NOTE = 4000
MAX_UNBLOCKS = 1000
MAX_LINK = 2000
MAX_PROJECT = 160
MAX_REQUEST_ID = 200
MAX_EFFORT_MINUTES = 7 * 24 * 60
MAX_LISTED = 500
KINDS = ("decision", "action", "approval", "review", "request")
DEFAULT_KIND = "decision"
RESOLUTIONS = {
    "decision": ("answered",),
    "action": ("done", "not_done"),
    "approval": ("approved", "declined"),
    "review": ("accepted", "changes_requested"),
    "request": ("granted", "declined"),
}
DISMISSED = "dismissed"
ANSWERED = "answered"
STATUSES = ("open", "resolved", "withdrawn")
# Rows written before the kinds migration used this status for a resolved decision.
LEGACY_RESOLVED_STATUS = "answered"
KINDS_REQUIRING_UNBLOCKS = ("action", "request")
STALE_AFTER_SECONDS = 72 * 3600
DEFAULT_POLL_SECONDS = 2.0
THREAD_PREFIX = "ask:"

_ADDED_COLUMNS = (
    ("kind", "TEXT NOT NULL DEFAULT 'decision'"),
    ("unblocks", "TEXT"),
    ("effort_minutes", "INTEGER"),
    ("link", "TEXT"),
    ("due_at", "REAL"),
    ("project", "TEXT"),
    ("request_id", "TEXT"),
    ("resolution", "TEXT"),
    ("note", "TEXT"),
    ("resolved_at", "REAL"),
    ("snoozed_until", "REAL"),
)


def initialize_schema(db) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS asks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL REFERENCES sessions(session_id),
        question TEXT NOT NULL,
        options_json TEXT NOT NULL DEFAULT '[]',
        blocking INTEGER NOT NULL DEFAULT 0,
        context TEXT,
        status TEXT NOT NULL DEFAULT 'open',
        answer TEXT,
        answer_message_id INTEGER REFERENCES messages(id),
        supersedes INTEGER REFERENCES asks(id),
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        answered_at REAL,
        withdrawn_at REAL,
        notification_claimed_at REAL
    )""")
    existing = {row["name"] for row in db.execute("PRAGMA table_info(asks)")}
    for column, definition in _ADDED_COLUMNS:
        if column not in existing:
            db.execute(f"ALTER TABLE asks ADD COLUMN {column} {definition}")
    # Asks answered before kinds existed become resolved decisions.
    db.execute("""UPDATE asks SET status = 'resolved', resolution = COALESCE(resolution, 'answered'),
        resolved_at = COALESCE(resolved_at, answered_at, updated_at) WHERE status = 'answered'""")
    db.execute("CREATE INDEX IF NOT EXISTS asks_open_idx ON asks(status, blocking, created_at)")
    db.execute("CREATE INDEX IF NOT EXISTS asks_session_idx ON asks(session_id, status)")
    db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS asks_request_idx ON asks(session_id, request_id)
        WHERE request_id IS NOT NULL""")


def _text(value: Any, label: str, limit: int, *, required: bool) -> str | None:
    if value is None:
        if required:
            raise CoordinationError(f"{label} must not be empty.")
        return None
    if not isinstance(value, str):
        raise CoordinationError(f"{label} must be text.")
    text = value.strip()
    if not text:
        if required:
            raise CoordinationError(f"{label} must not be empty.")
        return None
    if len(text) > limit:
        raise CoordinationError(f"{label} must be at most {limit} characters.")
    return text


def _options(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise CoordinationError("Options must be a list of short answer choices.")
    options: list[str] = []
    for option in value:
        text = _text(option, "Each option", MAX_OPTION, required=True)
        if text in options:
            raise CoordinationError(f"Duplicate option: {text}.")
        options.append(text)
    if len(options) > MAX_OPTIONS:
        raise CoordinationError(f"Use at most {MAX_OPTIONS} options.")
    if len(options) == 1:
        raise CoordinationError("Offer at least two options, or none for a free-text answer.")
    return options


def _kind(value: Any) -> str:
    if value is None:
        return DEFAULT_KIND
    if value not in KINDS:
        raise CoordinationError("Ask kind must be one of: " + ", ".join(KINDS) + ".")
    return value


def _effort(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_EFFORT_MINUTES:
        raise CoordinationError(f"Effort must be a whole number of minutes from 1 to {MAX_EFFORT_MINUTES}.")
    return value


def _link(value: Any) -> str | None:
    """Where the user acts: an HTTPS URL, an agentcoord:// link or an absolute file path."""
    link = _text(value, "Link", MAX_LINK, required=False)
    if link is None:
        return None
    if any(ord(character) < 32 or ord(character) == 127 for character in link):
        raise CoordinationError("Link must not contain control characters.")
    if link.startswith("/"):
        return link
    if any(character.isspace() for character in link):
        raise CoordinationError("Link must not contain spaces.")
    parts = urlsplit(link)
    if parts.scheme == "https" and parts.netloc:
        return link
    if parts.scheme == "agentcoord" and (parts.netloc or parts.path):
        return link
    raise CoordinationError("Link must be an HTTPS URL, an agentcoord:// link, or an absolute file path.")


def timestamp(value: Any, label: str) -> float | None:
    """Unix seconds or ISO 8601 text; a date alone means the end of that local day."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise CoordinationError(f"{label} must be Unix seconds or an ISO 8601 date or time.")
    if isinstance(value, (int, float)):
        moment = float(value)
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        try:
            moment = float(text)
        except ValueError:
            try:
                if len(text) == 10:
                    parsed = datetime.fromisoformat(text + "T23:59:59")
                else:
                    parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text[-1] in "Zz" else text)
            except ValueError as exc:
                raise CoordinationError(f"{label} must be Unix seconds or an ISO 8601 date or time.") from exc
            # A time without an offset is local time, as the user would read it.
            moment = parsed.timestamp() if parsed.tzinfo else time.mktime(parsed.timetuple())
    else:
        raise CoordinationError(f"{label} must be Unix seconds or an ISO 8601 date or time.")
    if not math.isfinite(moment) or moment < 0:
        raise CoordinationError(f"{label} must be a valid time.")
    return moment


def normalized_title(text: str) -> str:
    """Comparison key for duplicate prevention: case, spacing and final punctuation are ignored."""
    return " ".join(text.casefold().split()).rstrip(" .?!")


def allowed_resolutions(kind: str) -> tuple[str, ...]:
    return (*RESOLUTIONS[kind], DISMISSED)


def _status(row) -> str:
    return "resolved" if row["status"] == LEGACY_RESOLVED_STATUS else row["status"]


def _record(row) -> dict[str, Any]:
    status = _status(row)
    resolution = row["resolution"] or (ANSWERED if row["status"] == LEGACY_RESOLVED_STATUS else None)
    resolved_at = row["resolved_at"] if row["resolved_at"] is not None else (
        row["answered_at"] if status == "resolved" else None)
    return {
        "id": row["id"],
        "session_id": row["session_id"],
        "kind": row["kind"],
        "question": row["question"],
        "options": json.loads(row["options_json"]),
        "blocking": bool(row["blocking"]),
        "context": row["context"],
        "unblocks": row["unblocks"],
        "effort_minutes": row["effort_minutes"],
        "link": row["link"],
        "due_at": _iso(row["due_at"]),
        "project": row["project"],
        "request_id": row["request_id"],
        "status": status,
        "resolution": resolution,
        "answer": row["answer"],
        "note": row["note"],
        "answer_message_id": row["answer_message_id"],
        "supersedes": row["supersedes"],
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
        "resolved_at": _iso(resolved_at),
        # Retained for readers written before kinds: set only for an answered decision.
        "answered_at": _iso(resolved_at if resolution == ANSWERED else None),
        "withdrawn_at": _iso(row["withdrawn_at"]),
        "snoozed_until": _iso(row["snoozed_until"]),
        "notification_claimed": row["notification_claimed_at"] is not None,
    }


def answer_body(ask: dict[str, Any], answer: str, note: str | None = None) -> str:
    mode = "blocking" if ask["blocking"] else "non-blocking"
    lines = [f"The user answered your {mode} ask #{ask['id']}.", "", f"Question: {ask['question']}"]
    if ask["context"]:
        lines.append(f"Your context: {ask['context']}")
    lines += ["", f"Answer: {answer}"]
    if note:
        lines.append(f"Note: {note}")
    return "\n".join(lines)


def resolution_body(ask: dict[str, Any], resolution: str, note: str | None = None) -> str:
    """Message delivered to the asking session when the user resolves a non-decision outcome."""
    mode = "blocking" if ask["blocking"] else "non-blocking"
    label = resolution.replace("_", " ")
    if resolution == DISMISSED:
        lines = [f"The user dismissed your {mode} {ask['kind']} ask #{ask['id']} without acting on it."]
    else:
        lines = [f"The user resolved your {mode} {ask['kind']} ask #{ask['id']}: {label}."]
    lines += ["", f"Ask: {ask['question']}"]
    if ask["context"]:
        lines.append(f"Your context: {ask['context']}")
    if ask["unblocks"]:
        lines.append(f"Unblocks: {ask['unblocks']}")
    lines += ["", f"Resolution: {label}"]
    if note:
        lines.append(f"Note: {note}")
    if resolution == "done":
        lines += ["", "Verify the result before relying on it."]
    elif resolution == DISMISSED:
        lines += ["", "Do not assume it happened. Continue on your stated default, and ask again only if it still matters."]
    return "\n".join(lines)


def _positive_id(value: Any, message: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise CoordinationError(message)
    return value


def _percentile(ordered: list[float], fraction: float) -> float:
    """Nearest-rank percentile; honest for the small samples asks produce."""
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


_DISPLAY_QUERY = """SELECT a.*, t.title AS thread_title, t.attention, p.name AS project_name,
        s.cwd, s.client, s.last_seen_at
    FROM asks a JOIN sessions s ON s.session_id = a.session_id
    LEFT JOIN work_threads t ON t.thread_id = a.session_id
    LEFT JOIN work_projects p ON p.id = t.project_id"""


class AskStore:
    def __init__(self, store, *, initialize=True):
        self.store = store
        if not initialize:
            return
        with store._connection() as db:
            initialize_schema(db)

    # -- reads -----------------------------------------------------------
    def get(self, ask_id: Any) -> dict[str, Any]:
        _positive_id(ask_id, "Ask ID must be a positive integer.")
        with self.store._connection() as db:
            row = db.execute("SELECT * FROM asks WHERE id = ?", (ask_id,)).fetchone()
        if row is None:
            raise CoordinationError(f"Unknown ask: {ask_id}.")
        return _record(row)

    @staticmethod
    def _status_clause(status: str | None) -> tuple[str | None, list[Any]]:
        if status in (None, "all"):
            return None, []
        if status in ("resolved", LEGACY_RESOLVED_STATUS):
            return "a.status IN ('resolved', 'answered')", []
        if status not in STATUSES:
            raise CoordinationError("Ask status must be open, resolved, withdrawn, or all.")
        return "a.status = ?", [status]

    def list(self, *, status: str | None = "open", session_id: str | None = None,
             blocking: bool | None = None, since: float | None = None) -> list[dict[str, Any]]:
        clause, params = self._status_clause(status)
        clauses = [clause] if clause else []
        if session_id is not None:
            clauses.append("a.session_id = ?"); params.append(session_id)
        if blocking is not None:
            clauses.append("a.blocking = ?"); params.append(int(blocking))
        if since is not None:
            clauses.append("a.created_at >= ?"); params.append(since)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self.store._connection() as db:
            rows = db.execute("SELECT a.* FROM asks a" + where + " ORDER BY a.blocking DESC, a.created_at, a.id",
                              params).fetchall()
        return [_record(row) for row in rows]

    def _display(self, row, now: float) -> dict[str, Any]:
        """One ask with the thread fields and derived states the Queue shows."""
        item = _record(row)
        thread_closed = row["attention"] == "archived"
        inactive = now - row["last_seen_at"] >= STALE_AFTER_SECONDS
        is_open = item["status"] == "open"
        stale_reason = None
        if is_open:
            stale_reason = "thread_closed" if thread_closed else "inactive" if inactive else None
        item.update(
            thread_title=row["thread_title"] or row["session_id"], attention=row["attention"],
            project_name=row["project_name"], project_label=row["project"] or row["project_name"],
            cwd=row["cwd"], client=row["client"],
            age_seconds=max(0, int(now - row["created_at"])),
            idle_seconds=max(0, int(now - row["last_seen_at"])),
            thread_closed=thread_closed, stale=stale_reason is not None, stale_reason=stale_reason,
            overdue=bool(is_open and row["due_at"] is not None and row["due_at"] < now),
            snoozed=bool(is_open and row["snoozed_until"] is not None and row["snoozed_until"] > now),
            resolutions=list(allowed_resolutions(row["kind"])))
        return item

    def overview(self, *, status: str | None = "open", since: float | None = None,
                 session_id: str | None = None) -> list[dict[str, Any]]:
        """Asks across every thread with display fields and stale, overdue and snoozed states.

        Open asks list blocking first, oldest first. Other selections list the
        most recently changed first, capped at MAX_LISTED.
        """
        clause, params = self._status_clause(status)
        clauses = [clause] if clause else []
        if since is not None:
            clauses.append("a.created_at >= ?"); params.append(since)
        if session_id is not None:
            clauses.append("a.session_id = ?"); params.append(session_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        order = (" ORDER BY a.blocking DESC, a.created_at, a.id" if status == "open"
                 else f" ORDER BY a.updated_at DESC, a.id DESC LIMIT {MAX_LISTED}")
        now = self.store.clock()
        with self.store.reading(), self.store._connection() as db:
            rows = db.execute(_DISPLAY_QUERY + where + order, params).fetchall()
        return [self._display(row, now) for row in rows]

    def show(self, ask_id: Any) -> dict[str, Any]:
        _positive_id(ask_id, "Ask ID must be a positive integer.")
        now = self.store.clock()
        with self.store.reading(), self.store._connection() as db:
            row = db.execute(_DISPLAY_QUERY + " WHERE a.id = ?", (ask_id,)).fetchone()
        if row is None:
            raise CoordinationError(f"Unknown ask: {ask_id}.")
        return self._display(row, now)

    def metrics(self, *, since: float | None = None) -> dict[str, Any]:
        """Measure how asks are used: resolution latency, closure, rewrites, requests and blocked time.

        The period selects asks created at or after ``since``. Stale asks are
        the current state of every open ask, whatever the period.
        """
        now = self.store.clock()
        with self.store.reading(), self.store._connection() as db:
            rows = db.execute("""SELECT a.id, a.kind, a.status, a.resolution, a.blocking, a.supersedes,
                    a.created_at, a.resolved_at, a.answered_at, a.withdrawn_at, a.updated_at
                FROM asks a WHERE (? IS NULL OR a.created_at >= ?) ORDER BY a.id""", (since, since)).fetchall()
            open_rows = db.execute(_DISPLAY_QUERY + " WHERE a.status = 'open' ORDER BY a.id").fetchall()
        selected = {row["id"] for row in rows}
        superseded = {row["supersedes"] for row in rows if row["supersedes"] is not None}
        counts = {status: 0 for status in STATUSES}
        by_kind = {kind: 0 for kind in KINDS}
        resolutions: dict[str, int] = {}
        durations: dict[str, list[float]] = {kind: [] for kind in KINDS}
        requests = {"filed": 0, "open": 0, "granted": 0, "declined": 0, "dismissed": 0, "withdrawn": 0}
        blocked_seconds = 0.0
        withdrawn_superseded = 0
        chain_length: dict[int, int] = {}
        root_of: dict[int, int] = {}
        for row in rows:
            status = _status(row)
            counts[status] += 1
            by_kind[row["kind"]] += 1
            resolved_at = row["resolved_at"] if row["resolved_at"] is not None else row["answered_at"]
            closed_at = resolved_at if status == "resolved" else row["withdrawn_at"] if status == "withdrawn" else None
            if status == "resolved":
                resolution = row["resolution"] or ANSWERED
                resolutions[resolution] = resolutions.get(resolution, 0) + 1
                if resolved_at is not None:
                    durations[row["kind"]].append(max(0.0, resolved_at - row["created_at"]))
            elif status == "withdrawn" and row["id"] in superseded:
                withdrawn_superseded += 1
            if row["blocking"]:
                blocked_seconds += max(0.0, (closed_at if closed_at is not None else now) - row["created_at"])
            if row["kind"] == "request":
                requests["filed"] += 1
                if status == "resolved":
                    outcome = row["resolution"]
                    requests[outcome if outcome in requests else "dismissed"] += 1
                else:
                    requests[status] += 1
            # A rewrite always supersedes an earlier ask, so ascending IDs visit a chain root first.
            root = root_of.get(row["supersedes"], row["id"]) if row["supersedes"] in selected else row["id"]
            root_of[row["id"]] = root
            chain_length[root] = chain_length.get(root, 0) + 1

        def latency(values: list[float]) -> dict[str, Any]:
            ordered = sorted(values)
            if not ordered:
                return {"count": 0, "median_seconds": None, "p90_seconds": None}
            return {"count": len(ordered), "median_seconds": round(statistics.median(ordered), 1),
                    "p90_seconds": round(_percentile(ordered, 0.9), 1)}

        closed = counts["resolved"] + counts["withdrawn"]
        share = lambda part, whole: round(part / whole, 3) if whole else None
        stale = [self._display(row, now) for row in open_rows]
        stale = [item for item in stale if item["stale"]]
        needs = len(chain_length)
        return {
            "period": {"since": _iso(since), "as_of": _iso(now)},
            "asks": {"total": len(rows), **counts, "by_kind": by_kind},
            "time_to_resolution": {
                "all": latency([value for values in durations.values() for value in values]),
                **{kind: latency(values) for kind, values in durations.items()}},
            "closure": {
                "closed": closed, "resolved_by_user": counts["resolved"], "withdrawn_by_agent": counts["withdrawn"],
                "withdrawn_superseded": withdrawn_superseded,
                "withdrawn_without_successor": counts["withdrawn"] - withdrawn_superseded,
                "resolved_share": share(counts["resolved"], closed), "withdrawn_share": share(counts["withdrawn"], closed),
                "resolutions": dict(sorted(resolutions.items()))},
            "supersede_chains": {
                "distinct_needs": needs, "asks_per_need": round(len(rows) / needs, 2) if needs else None,
                "rewritten_needs": sum(1 for length in chain_length.values() if length > 1),
                "longest_chain": max(chain_length.values(), default=0)},
            "requests": {**requests, "granted_share": share(requests["granted"], requests["filed"])},
            "blocked_hours": round(blocked_seconds / 3600, 1),
            "stale": {"count": len(stale), "ask_ids": [item["id"] for item in stale]},
        }

    @staticmethod
    def open_blocking(db, thread_ids=None) -> dict[str, dict[str, Any]]:
        """Oldest open blocking ask per thread, for attention computation."""
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'asks'").fetchone():
            return {}
        sql = "SELECT * FROM asks WHERE status = 'open' AND blocking = 1"
        params: list[Any] = []
        if thread_ids is not None:
            ids = list(thread_ids)
            if not ids:
                return {}
            sql += " AND session_id IN (" + ",".join("?" * len(ids)) + ")"
            params = ids
        result: dict[str, dict[str, Any]] = {}
        for row in db.execute(sql + " ORDER BY created_at, id", params):
            result.setdefault(row["session_id"], {"id": row["id"], "question": row["question"],
                                                  "created_at": row["created_at"], "blocking": True})
        return result

    # -- writes ----------------------------------------------------------
    def create(self, session_id: str, question: Any, *, options: Any = None, blocking: Any,
               context: Any = None, supersedes: Any = None, kind: Any = None, unblocks: Any = None,
               effort_minutes: Any = None, link: Any = None, due_at: Any = None, project: Any = None,
               request_id: Any = None) -> dict[str, Any]:
        """File an ask. A repeated request_id, or the title of an open ask, returns that ask."""
        if not isinstance(session_id, str) or not session_id.strip():
            raise CoordinationError("No caller session. Pass session_id from your hook context.")
        if not isinstance(blocking, bool):
            raise CoordinationError("Choose blocking or non-blocking explicitly.")
        kind = _kind(kind)
        text = _text(question, "Question", MAX_QUESTION, required=True)
        choices = _options(options)
        if choices and kind != "decision":
            raise CoordinationError(f"Options apply to decisions; a {kind} is resolved with "
                                    + " or ".join(RESOLUTIONS[kind]) + ".")
        why = _text(context, "Context", MAX_CONTEXT, required=False)
        if not blocking and not why:
            raise CoordinationError("A non-blocking ask must state the default you will proceed with in context.")
        gain = _text(unblocks, "Unblocks", MAX_UNBLOCKS, required=False)
        if kind in KINDS_REQUIRING_UNBLOCKS and not gain:
            raise CoordinationError(f"State what resolving this {kind} unblocks: the expected gain, "
                                    "and for a request what you do without it.")
        effort = _effort(effort_minutes)
        target = _link(link)
        due = timestamp(due_at, "Due time")
        label = _text(project, "Project", MAX_PROJECT, required=False)
        key = _text(request_id, "Request ID", MAX_REQUEST_ID, required=False)
        if supersedes is not None and (not isinstance(supersedes, int) or isinstance(supersedes, bool) or supersedes <= 0):
            raise CoordinationError("supersedes must be the ID of your own open ask.")
        self.store.get_session(session_id)
        title = normalized_title(text)
        now = self.store.clock()
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if key is not None:
                replayed = db.execute("SELECT * FROM asks WHERE session_id = ? AND request_id = ?",
                                      (session_id, key)).fetchone()
                if replayed is not None:
                    return {**_record(replayed), "existing": True}
            if supersedes is not None:
                earlier = db.execute("SELECT * FROM asks WHERE id = ?", (supersedes,)).fetchone()
                if earlier is None or earlier["session_id"] != session_id:
                    raise CoordinationError(f"Ask {supersedes} is not yours.")
                if _status(earlier) != "open":
                    raise CoordinationError(f"Ask {supersedes} is already {_status(earlier)}.")
            for candidate in db.execute("SELECT * FROM asks WHERE session_id = ? AND status = 'open' AND id IS NOT ?",
                                        (session_id, supersedes)):
                if normalized_title(candidate["question"]) == title:
                    return {**_record(candidate), "existing": True}
            if supersedes is not None:
                db.execute("UPDATE asks SET status = 'withdrawn', withdrawn_at = ?, updated_at = ? WHERE id = ?",
                           (now, now, supersedes))
            cursor = db.execute("""INSERT INTO asks (session_id, kind, question, options_json, blocking, context,
                unblocks, effort_minutes, link, due_at, project, request_id, status, supersedes, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)""",
                (session_id, kind, text, json.dumps(choices), int(blocking), why, gain, effort, target, due,
                 label, key, supersedes, now, now))
            row = db.execute("SELECT * FROM asks WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return _record(row)

    def resolve(self, ask_id: Any, resolution: Any, *, option: Any = None, answer: Any = None,
                note: Any = None) -> dict[str, Any]:
        """Record the user's resolution and deliver it as an ordinary inbox message."""
        current = self.get(ask_id)
        if current["status"] != "open":
            raise CoordinationError(f"Ask {ask_id} is already {current['status']}.")
        allowed = allowed_resolutions(current["kind"])
        if resolution not in allowed:
            raise CoordinationError(f"Ask {ask_id} is {'an' if current['kind'][0] == 'a' else 'a'} {current['kind']}; "
                                    "resolve it with " + ", ".join(allowed[:-1]) + f" or {allowed[-1]}.")
        remark = _text(note, "Note", MAX_NOTE, required=False)
        text = None
        if resolution == ANSWERED:
            if (answer is None) == (option is None):
                raise CoordinationError("Provide exactly one of answer (free text) or option.")
            if option is not None:
                text = _text(option, "Option", MAX_OPTION, required=True)
                if text not in current["options"]:
                    raise CoordinationError(f"Option {text!r} is not one of the offered choices.")
            else:
                text = _text(answer, "Answer", MAX_ANSWER, required=True)
            body = answer_body(current, text, remark)
        else:
            if answer is not None or option is not None:
                raise CoordinationError("An answer or option applies to an answered decision; use note for other resolutions.")
            body = resolution_body(current, resolution, remark)
        now = self.store.clock()
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM asks WHERE id = ?", (ask_id,)).fetchone()
            if row["status"] != "open":
                raise CoordinationError(f"Ask {ask_id} is already {_status(row)}.")
            # Self-addressed so delivery, wake, and hook paths need no new sender kind.
            message, _ = self.store._insert_message(
                db, sender_session_id=current["session_id"], recipient_session_id=current["session_id"],
                body=body, classification="action_required",
                thread_id=f"{THREAD_PREFIX}{ask_id}", reply_required=False, now=now)
            db.execute("""UPDATE asks SET status = 'resolved', resolution = ?, answer = ?, note = ?,
                answer_message_id = ?, resolved_at = ?, answered_at = ?, snoozed_until = NULL, updated_at = ?
                WHERE id = ?""", (resolution, text, remark, message["id"], now,
                                  now if resolution == ANSWERED else None, now, ask_id))
            saved = db.execute("SELECT * FROM asks WHERE id = ?", (ask_id,)).fetchone()
        return _record(saved)

    def answer(self, ask_id: Any, *, answer: Any = None, option: Any = None) -> dict[str, Any]:
        """Answer a decision; the form used before kinds existed."""
        if (answer is None) == (option is None):
            raise CoordinationError("Provide exactly one of answer (free text) or option.")
        return self.resolve(ask_id, ANSWERED, answer=answer, option=option)

    def snooze(self, ask_id: Any, until: Any) -> dict[str, Any]:
        """Hide an open ask from the open list until a time; None shows it again.

        Snoozing changes only the asks view. A snoozed blocking ask still marks its thread.
        """
        deadline = timestamp(until, "Snooze time")
        now = self.store.clock()
        if deadline is not None and deadline <= now:
            raise CoordinationError("Snooze time must be in the future.")
        current = self.get(ask_id)
        if current["status"] != "open":
            raise CoordinationError(f"Ask {ask_id} is already {current['status']}.")
        with self.store._connection() as db:
            cursor = db.execute("UPDATE asks SET snoozed_until = ?, updated_at = ? WHERE id = ? AND status = 'open'",
                                (deadline, now, ask_id))
            row = db.execute("SELECT * FROM asks WHERE id = ?", (ask_id,)).fetchone()
        if cursor.rowcount != 1:
            raise CoordinationError(f"Ask {ask_id} is already {_status(row)}.")
        return _record(row)

    def withdraw(self, ask_id: Any, session_id: str) -> dict[str, Any]:
        current = self.get(ask_id)
        if current["session_id"] != session_id:
            raise CoordinationError(f"Ask {ask_id} is not yours.")
        if current["status"] == "withdrawn":
            return current
        if current["status"] != "open":
            raise CoordinationError(f"Ask {ask_id} is already {current['status']}.")
        now = self.store.clock()
        with self.store._connection() as db:
            db.execute("UPDATE asks SET status = 'withdrawn', withdrawn_at = ?, updated_at = ? WHERE id = ? AND status = 'open'",
                       (now, now, ask_id))
            row = db.execute("SELECT * FROM asks WHERE id = ?", (ask_id,)).fetchone()
        return _record(row)

    def claim_notification(self, ask_id: int) -> bool:
        """Atomically claim the one desktop/push alert for an open blocking ask."""
        now = self.store.clock()
        with self.store._connection() as db:
            cursor = db.execute("""UPDATE asks SET notification_claimed_at = ? WHERE id = ? AND status = 'open'
                AND blocking = 1 AND notification_claimed_at IS NULL""", (now, ask_id))
        return cursor.rowcount == 1

    def wait(self, ask_id: int, session_id: str, *, timeout_seconds: float | None = None,
             poll_interval_seconds: float = DEFAULT_POLL_SECONDS,
             sleep: Callable[[float], None] | None = None) -> dict[str, Any]:
        """Block until the ask leaves the open state; the direct resolution records message delivery."""
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise CoordinationError("Wait timeout must be a positive number of seconds.")
        sleep = time.sleep if sleep is None else sleep
        current = self.get(ask_id)
        if current["session_id"] != session_id:
            raise CoordinationError(f"Ask {ask_id} is not yours.")
        deadline = None if timeout_seconds is None else self.store.clock() + timeout_seconds
        while current["status"] == "open":
            self.store.touch(session_id)
            if deadline is not None:
                remaining = deadline - self.store.clock()
                if remaining <= 0:
                    return {**current, "timed_out": True}
                sleep(min(poll_interval_seconds, remaining))
            else:
                sleep(poll_interval_seconds)
            current = self.get(ask_id)
        if current["status"] == "resolved" and current["answer_message_id"]:
            self.store.record_message_retrieval(session_id, current["answer_message_id"])
        return {**current, "timed_out": False}
