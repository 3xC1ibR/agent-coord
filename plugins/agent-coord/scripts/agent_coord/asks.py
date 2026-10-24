"""Decisions only the user can make: durable asks from agents, answered in one place.

An ask is a persisted question tied to the asking session. The answer travels
back through the ordinary message path, so an idle asker resumes exactly the way
it does for a peer message. Blocking asks also mark the thread as needing the
user; non-blocking asks state a default and let the agent continue.
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable

from .store import CoordinationError, _iso

MAX_QUESTION = 2000
MAX_CONTEXT = 4000
MAX_OPTION = 200
MAX_OPTIONS = 8
MAX_ANSWER = 4000
STATUSES = ("open", "answered", "withdrawn")
DEFAULT_POLL_SECONDS = 2.0
THREAD_PREFIX = "ask:"


def initialize(db) -> None:
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
    db.execute("CREATE INDEX IF NOT EXISTS asks_open_idx ON asks(status, blocking, created_at)")
    db.execute("CREATE INDEX IF NOT EXISTS asks_session_idx ON asks(session_id, status)")


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


def _record(row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "session_id": row["session_id"],
        "question": row["question"],
        "options": json.loads(row["options_json"]),
        "blocking": bool(row["blocking"]),
        "context": row["context"],
        "status": row["status"],
        "answer": row["answer"],
        "answer_message_id": row["answer_message_id"],
        "supersedes": row["supersedes"],
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
        "answered_at": _iso(row["answered_at"]),
        "withdrawn_at": _iso(row["withdrawn_at"]),
        "notification_claimed": row["notification_claimed_at"] is not None,
    }


def answer_body(ask: dict[str, Any], answer: str) -> str:
    kind = "blocking" if ask["blocking"] else "non-blocking"
    lines = [f"The user answered your {kind} ask #{ask['id']}.", "", f"Question: {ask['question']}"]
    if ask["context"]:
        lines.append(f"Your context: {ask['context']}")
    lines += ["", f"Answer: {answer}"]
    return "\n".join(lines)


class AskStore:
    def __init__(self, store):
        self.store = store
        with store._connection() as db:
            initialize(db)

    # -- reads -----------------------------------------------------------
    def get(self, ask_id: Any) -> dict[str, Any]:
        if not isinstance(ask_id, int) or isinstance(ask_id, bool) or ask_id <= 0:
            raise CoordinationError("Ask ID must be a positive integer.")
        with self.store._connection() as db:
            row = db.execute("SELECT * FROM asks WHERE id = ?", (ask_id,)).fetchone()
        if row is None:
            raise CoordinationError(f"Unknown ask: {ask_id}.")
        return _record(row)

    def list(self, *, status: str | None = "open", session_id: str | None = None,
             blocking: bool | None = None) -> list[dict[str, Any]]:
        if status is not None and status not in STATUSES:
            raise CoordinationError("Ask status must be open, answered, or withdrawn.")
        clauses, params = [], []
        if status is not None:
            clauses.append("status = ?"); params.append(status)
        if session_id is not None:
            clauses.append("session_id = ?"); params.append(session_id)
        if blocking is not None:
            clauses.append("blocking = ?"); params.append(int(blocking))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self.store._connection() as db:
            rows = db.execute("SELECT * FROM asks" + where + " ORDER BY blocking DESC, created_at, id", params).fetchall()
        return [_record(row) for row in rows]

    def overview(self) -> list[dict[str, Any]]:
        """Open asks across every thread with the display fields the UI needs."""
        now = self.store.clock()
        with self.store._connection() as db:
            rows = db.execute("""SELECT a.*, t.title AS thread_title, t.attention, p.name AS project_name,
                    s.cwd, s.client, a.created_at AS created_raw
                FROM asks a JOIN sessions s ON s.session_id = a.session_id
                LEFT JOIN work_threads t ON t.thread_id = a.session_id
                LEFT JOIN work_projects p ON p.id = t.project_id
                WHERE a.status = 'open' ORDER BY a.blocking DESC, a.created_at, a.id""").fetchall()
        result = []
        for row in rows:
            item = _record(row)
            item.update(thread_title=row["thread_title"] or row["session_id"], attention=row["attention"],
                        project_name=row["project_name"], cwd=row["cwd"], client=row["client"],
                        age_seconds=max(0, int(now - row["created_raw"])))
            result.append(item)
        return result

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
               context: Any = None, supersedes: Any = None) -> dict[str, Any]:
        if not isinstance(session_id, str) or not session_id.strip():
            raise CoordinationError("No caller session. Pass session_id from your hook context.")
        if not isinstance(blocking, bool):
            raise CoordinationError("Choose blocking or non-blocking explicitly.")
        text = _text(question, "Question", MAX_QUESTION, required=True)
        choices = _options(options)
        why = _text(context, "Context", MAX_CONTEXT, required=False)
        if not blocking and not why:
            raise CoordinationError("A non-blocking ask must state the default you will proceed with in context.")
        if supersedes is not None and (not isinstance(supersedes, int) or isinstance(supersedes, bool) or supersedes <= 0):
            raise CoordinationError("supersedes must be the ID of your own open ask.")
        self.store.get_session(session_id)
        now = self.store.clock()
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if supersedes is not None:
                earlier = db.execute("SELECT * FROM asks WHERE id = ?", (supersedes,)).fetchone()
                if earlier is None or earlier["session_id"] != session_id:
                    raise CoordinationError(f"Ask {supersedes} is not yours.")
                if earlier["status"] != "open":
                    raise CoordinationError(f"Ask {supersedes} is already {earlier['status']}.")
                db.execute("UPDATE asks SET status = 'withdrawn', withdrawn_at = ?, updated_at = ? WHERE id = ?",
                           (now, now, supersedes))
            cursor = db.execute("""INSERT INTO asks (session_id, question, options_json, blocking, context, status,
                supersedes, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'open', ?, ?, ?)""",
                (session_id, text, json.dumps(choices), int(blocking), why, supersedes, now, now))
            row = db.execute("SELECT * FROM asks WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return _record(row)

    def answer(self, ask_id: Any, *, answer: Any = None, option: Any = None) -> dict[str, Any]:
        """Record the user's answer and deliver it as an ordinary inbox message."""
        if (answer is None) == (option is None):
            raise CoordinationError("Provide exactly one of answer (free text) or option.")
        current = self.get(ask_id)
        if current["status"] != "open":
            raise CoordinationError(f"Ask {ask_id} is already {current['status']}.")
        if option is not None:
            text = _text(option, "Option", MAX_OPTION, required=True)
            if text not in current["options"]:
                raise CoordinationError(f"Option {text!r} is not one of the offered choices.")
        else:
            text = _text(answer, "Answer", MAX_ANSWER, required=True)
        now = self.store.clock()
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM asks WHERE id = ?", (ask_id,)).fetchone()
            if row["status"] != "open":
                raise CoordinationError(f"Ask {ask_id} is already {row['status']}.")
            # Self-addressed so delivery, wake, and hook paths need no new sender kind.
            message, _ = self.store._insert_message(
                db, sender_session_id=current["session_id"], recipient_session_id=current["session_id"],
                body=answer_body(current, text), classification="action_required",
                thread_id=f"{THREAD_PREFIX}{ask_id}", reply_required=False, now=now)
            db.execute("""UPDATE asks SET status = 'answered', answer = ?, answer_message_id = ?,
                answered_at = ?, updated_at = ? WHERE id = ?""", (text, message["id"], now, now, ask_id))
            saved = db.execute("SELECT * FROM asks WHERE id = ?", (ask_id,)).fetchone()
        return _record(saved)

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
        """Block until the ask leaves the open state; the direct answer acknowledges its message."""
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
        if current["status"] == "answered" and current["answer_message_id"]:
            self.store.acknowledge(session_id, current["answer_message_id"])
        return {**current, "timed_out": False}
