from __future__ import annotations

import fnmatch
import json
import os
import posixpath
import re
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

ACTIVITIES = {
    "idle",
    "discussing",
    "planning",
    "implementing",
    "validating",
    "waiting",
}
RELEVANT_ACTIVITIES = {"planning", "implementing", "validating", "waiting"}
DEFAULT_STALE_AFTER_SECONDS = 30 * 60
DEFAULT_INBOX_POLL_SECONDS = 0.5
WAKEABLE_ACTIVITIES = {"idle", "waiting"}
LEASE_MODES = {"write", "validation"}
MESSAGE_CLASSIFICATIONS = {"action_required", "informational", "closure"}
CLIENTS = {"claude", "codex"}
SCOPE_REQUEST_THREAD_PREFIX = "scope-request:"
_WILDCARD = re.compile(r"[*?[]")


class CoordinationError(RuntimeError):
    """A user-correctable coordination failure."""


class ConflictError(CoordinationError):
    def __init__(self, conflicts: list[dict[str, Any]]) -> None:
        super().__init__("The requested work conflicts with another live session.")
        self.conflicts = conflicts


class AmbiguousTargetError(CoordinationError):
    def __init__(self, bead_id: str, sessions: list[dict[str, Any]]) -> None:
        super().__init__(f"Bead {bead_id} is declared by more than one live session.")
        self.bead_id = bead_id
        self.sessions = sessions


class InboxTimeoutError(CoordinationError):
    def __init__(self, session_id: str, timeout_seconds: float) -> None:
        super().__init__(
            f"No message arrived for session {session_id} within {timeout_seconds}s."
        )
        self.session_id = session_id
        self.timeout_seconds = timeout_seconds


def default_database_path() -> Path:
    configured = os.environ.get("AGENT_COORD_DB")
    if configured:
        return Path(configured).expanduser()
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local/state"
    return base / "agent-coord" / "state.sqlite3"


def _iso(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def _json_scopes(value: str) -> list[str]:
    parsed = json.loads(value)
    if not isinstance(parsed, list) or not all(
        isinstance(item, str) for item in parsed
    ):
        raise CoordinationError("Stored write scope is invalid.")
    return parsed


def _json_object(value: str | None) -> dict[str, Any] | None:
    if value is None:
        return None
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise CoordinationError("Stored JSON object is invalid.")
    return parsed


def normalize_scope(value: str, cwd: str) -> str:
    candidate = value.strip().replace("\\", "/")
    if not candidate:
        raise CoordinationError("Write scopes must not be empty.")
    cwd_path = Path(cwd).resolve()
    if os.path.isabs(candidate):
        try:
            candidate = Path(candidate).resolve().relative_to(cwd_path).as_posix()
        except ValueError as exc:
            raise CoordinationError(
                f"Write scope {value!r} is outside repository {cwd}."
            ) from exc
    candidate = posixpath.normpath(candidate)
    if candidate == ".." or candidate.startswith("../"):
        raise CoordinationError(f"Write scope {value!r} escapes the repository.")
    candidate = candidate.removeprefix("./")
    return candidate


def normalize_target_path(value: str, cwd: str) -> str:
    return normalize_scope(value, cwd)


def path_is_in_scope(path: str, scope: str) -> bool:
    if scope in {".", "**", "**/*"}:
        return True
    if not _WILDCARD.search(scope):
        return path == scope or path.startswith(scope.rstrip("/") + "/")
    if scope.endswith("/**"):
        root = scope[:-3].rstrip("/")
        if path == root or path.startswith(root + "/"):
            return True
    pure_path = PurePosixPath(path)
    return pure_path.match(scope) or fnmatch.fnmatchcase(path, scope)


def _scope_root(scope: str) -> str:
    match = _WILDCARD.search(scope)
    if match is None:
        return scope.rstrip("/") or "."
    prefix = scope[: match.start()].rstrip("/")
    if not prefix:
        return "."
    if "/" in prefix and not scope[: match.start()].endswith("/"):
        prefix = prefix.rsplit("/", 1)[0]
    return prefix or "."


def scopes_overlap(left: str, right: str) -> bool:
    left_glob = _WILDCARD.search(left) is not None
    right_glob = _WILDCARD.search(right) is not None
    if not left_glob and not right_glob:
        return (
            left == right
            or left.startswith(right.rstrip("/") + "/")
            or right.startswith(left.rstrip("/") + "/")
        )
    if left_glob and not right_glob and path_is_in_scope(right, left):
        return True
    if right_glob and not left_glob and path_is_in_scope(left, right):
        return True
    left_root = _scope_root(left)
    right_root = _scope_root(right)
    if "." in {left_root, right_root}:
        return True
    return (
        left_root == right_root
        or left_root.startswith(right_root.rstrip("/") + "/")
        or right_root.startswith(left_root.rstrip("/") + "/")
    )


class CoordinationStore:
    def __init__(
        self,
        database_path: str | Path | None = None,
        *,
        stale_after_seconds: int | None = None,
        clock: Any = time.time,
    ) -> None:
        self.database_path = Path(database_path or default_database_path()).expanduser()
        configured_stale = os.environ.get("AGENT_COORD_STALE_AFTER_SECONDS")
        self.stale_after_seconds = stale_after_seconds or int(
            configured_stale or DEFAULT_STALE_AFTER_SECONDS
        )
        self.clock = clock
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    client TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    name TEXT,
                    activity TEXT NOT NULL,
                    bead_id TEXT,
                    write_scope_json TEXT NOT NULL,
                    started_at REAL NOT NULL,
                    last_seen_at REAL NOT NULL,
                    ended_at REAL,
                    turn_active INTEGER NOT NULL DEFAULT 0,
                    lease_mode TEXT NOT NULL DEFAULT 'write',
                    scope_requested_at REAL
                );

                CREATE INDEX IF NOT EXISTS sessions_bead_idx
                    ON sessions(bead_id) WHERE bead_id IS NOT NULL;
                CREATE INDEX IF NOT EXISTS sessions_cwd_idx ON sessions(cwd);

                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sender_session_id TEXT NOT NULL,
                    recipient_session_id TEXT NOT NULL,
                    body TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    delivered_at REAL,
                    acknowledged_at REAL,
                    classification TEXT NOT NULL DEFAULT 'action_required',
                    thread_id TEXT NOT NULL DEFAULT '',
                    reply_required INTEGER NOT NULL DEFAULT 1,
                    FOREIGN KEY(sender_session_id) REFERENCES sessions(session_id),
                    FOREIGN KEY(recipient_session_id) REFERENCES sessions(session_id)
                );

                CREATE INDEX IF NOT EXISTS messages_recipient_idx
                    ON messages(recipient_session_id, delivered_at, acknowledged_at);

                CREATE TABLE IF NOT EXISTS zellij_wake_targets (
                    session_id TEXT PRIMARY KEY,
                    zellij_session TEXT NOT NULL,
                    pane_id TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    watcher_pid INTEGER,
                    registered_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    last_checked_at REAL,
                    last_wake_at REAL,
                    last_error TEXT,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                );

                CREATE TABLE IF NOT EXISTS message_wake_attempts (
                    message_id INTEGER PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    attempted_at REAL NOT NULL,
                    outcome TEXT NOT NULL,
                    detail TEXT,
                    FOREIGN KEY(message_id) REFERENCES messages(id),
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                );

                CREATE TABLE IF NOT EXISTS delegations (
                    delegation_id TEXT PRIMARY KEY,
                    parent_session_id TEXT NOT NULL,
                    child_session_id TEXT,
                    client TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    bead_id TEXT NOT NULL,
                    write_scope_json TEXT NOT NULL,
                    instructions TEXT NOT NULL,
                    status TEXT NOT NULL,
                    zellij_session TEXT,
                    pane_id TEXT,
                    mode TEXT NOT NULL,
                    lease_mode TEXT NOT NULL DEFAULT 'write',
                    bypass_hook_trust INTEGER NOT NULL DEFAULT 0,
                    model TEXT,
                    reasoning_effort TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    completed_at REAL,
                    result_message TEXT,
                    error TEXT,
                    token_usage_json TEXT,
                    token_usage_captured_at REAL,
                    token_usage_capture_event TEXT,
                    token_usage_artifact_path TEXT,
                    token_usage_error TEXT,
                    FOREIGN KEY(parent_session_id) REFERENCES sessions(session_id),
                    FOREIGN KEY(child_session_id) REFERENCES sessions(session_id)
                );

                CREATE UNIQUE INDEX IF NOT EXISTS delegations_active_work_idx
                    ON delegations(cwd, bead_id)
                    WHERE status IN ('launching', 'launched', 'attached');
                CREATE INDEX IF NOT EXISTS delegations_parent_idx
                    ON delegations(parent_session_id, created_at);
                CREATE INDEX IF NOT EXISTS delegations_child_idx
                    ON delegations(child_session_id)
                    WHERE child_session_id IS NOT NULL;
                """
            )
            session_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(sessions)")
            }
            if "turn_active" not in session_columns:
                connection.execute(
                    "ALTER TABLE sessions ADD COLUMN turn_active "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            if "lease_mode" not in session_columns:
                connection.execute(
                    "ALTER TABLE sessions ADD COLUMN lease_mode "
                    "TEXT NOT NULL DEFAULT 'write'"
                )
            if "scope_requested_at" not in session_columns:
                connection.execute(
                    "ALTER TABLE sessions ADD COLUMN scope_requested_at REAL"
                )
            message_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(messages)")
            }
            if "classification" not in message_columns:
                connection.execute(
                    "ALTER TABLE messages ADD COLUMN classification "
                    "TEXT NOT NULL DEFAULT 'action_required'"
                )
            if "thread_id" not in message_columns:
                connection.execute(
                    "ALTER TABLE messages ADD COLUMN thread_id "
                    "TEXT NOT NULL DEFAULT ''"
                )
            if "reply_required" not in message_columns:
                # Existing rows predate classified messages, whose historical
                # behavior was to request a response. Preserve that contract.
                connection.execute(
                    "ALTER TABLE messages ADD COLUMN reply_required "
                    "INTEGER NOT NULL DEFAULT 1"
                )
            connection.execute(
                """
                UPDATE messages
                SET thread_id = 'legacy:' || id
                WHERE thread_id IS NULL OR thread_id = ''
                """
            )
            delegation_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(delegations)")
            }
            if "bypass_hook_trust" not in delegation_columns:
                connection.execute(
                    "ALTER TABLE delegations ADD COLUMN bypass_hook_trust "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            if "model" not in delegation_columns:
                connection.execute("ALTER TABLE delegations ADD COLUMN model TEXT")
            if "reasoning_effort" not in delegation_columns:
                connection.execute(
                    "ALTER TABLE delegations ADD COLUMN reasoning_effort TEXT"
                )
            if "lease_mode" not in delegation_columns:
                connection.execute(
                    "ALTER TABLE delegations ADD COLUMN lease_mode "
                    "TEXT NOT NULL DEFAULT 'write'"
                )
            for column, definition in (
                ("token_usage_json", "TEXT"),
                ("token_usage_captured_at", "REAL"),
                ("token_usage_capture_event", "TEXT"),
                ("token_usage_artifact_path", "TEXT"),
                ("token_usage_error", "TEXT"),
            ):
                if column not in delegation_columns:
                    connection.execute(
                        f"ALTER TABLE delegations ADD COLUMN {column} {definition}"
                    )
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS message_threads (
                    thread_id TEXT PRIMARY KEY,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    closed_at REAL
                );

                INSERT OR IGNORE INTO message_threads (
                    thread_id, created_at, updated_at, closed_at
                )
                SELECT thread_id, MIN(created_at), MAX(created_at), NULL
                FROM messages
                WHERE thread_id IS NOT NULL
                GROUP BY thread_id;

                CREATE INDEX IF NOT EXISTS messages_thread_idx
                    ON messages(thread_id, id);
                CREATE INDEX IF NOT EXISTS messages_actionable_recipient_idx
                    ON messages(recipient_session_id, classification, delivered_at);

                CREATE TABLE IF NOT EXISTS handoffs (
                    handoff_id TEXT PRIMARY KEY,
                    sender_session_id TEXT NOT NULL,
                    recipient_session_id TEXT NOT NULL,
                    source_bead_id TEXT NOT NULL,
                    target_bead_id TEXT NOT NULL,
                    scope_json TEXT NOT NULL,
                    patch_label TEXT NOT NULL,
                    validation_boundary TEXT NOT NULL,
                    validation_responsibility TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    notification_message_id INTEGER NOT NULL UNIQUE,
                    created_at REAL NOT NULL,
                    FOREIGN KEY(sender_session_id) REFERENCES sessions(session_id),
                    FOREIGN KEY(recipient_session_id) REFERENCES sessions(session_id),
                    FOREIGN KEY(thread_id) REFERENCES message_threads(thread_id),
                    FOREIGN KEY(notification_message_id) REFERENCES messages(id)
                );

                CREATE INDEX IF NOT EXISTS handoffs_sessions_idx
                    ON handoffs(sender_session_id, recipient_session_id, created_at);
                """
            )

    def _row_to_session(
        self, row: sqlite3.Row, now: float | None = None
    ) -> dict[str, Any]:
        current = self.clock() if now is None else now
        ended_at = row["ended_at"]
        if ended_at is not None:
            presence = "offline"
        elif current - row["last_seen_at"] > self.stale_after_seconds:
            presence = "stale"
        else:
            presence = "online"
        return {
            "session_id": row["session_id"],
            "client": row["client"],
            "cwd": row["cwd"],
            "name": row["name"],
            "presence": presence,
            "activity": row["activity"],
            "turn_active": bool(row["turn_active"]),
            "lease_mode": row["lease_mode"],
            "bead_id": row["bead_id"],
            "write_scope": _json_scopes(row["write_scope_json"]),
            "scope_required": row["scope_requested_at"] is not None,
            "scope_requested_at": _iso(row["scope_requested_at"]),
            "started_at": _iso(row["started_at"]),
            "last_seen_at": _iso(row["last_seen_at"]),
            "ended_at": _iso(ended_at),
        }

    def _row_to_delegation(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "delegation_id": row["delegation_id"],
            "parent_session_id": row["parent_session_id"],
            "child_session_id": row["child_session_id"],
            "client": row["client"],
            "cwd": row["cwd"],
            "bead_id": row["bead_id"],
            "write_scope": _json_scopes(row["write_scope_json"]),
            "instructions": row["instructions"],
            "status": row["status"],
            "zellij_session": row["zellij_session"],
            "pane_id": row["pane_id"],
            "mode": row["mode"],
            "lease_mode": row["lease_mode"],
            "bypass_hook_trust": bool(row["bypass_hook_trust"]),
            "model": row["model"],
            "reasoning_effort": row["reasoning_effort"],
            "created_at": _iso(row["created_at"]),
            "updated_at": _iso(row["updated_at"]),
            "completed_at": _iso(row["completed_at"]),
            "result_message": row["result_message"],
            "error": row["error"],
            "token_usage": _json_object(row["token_usage_json"]),
            "token_usage_captured_at": _iso(row["token_usage_captured_at"]),
            "token_usage_capture_event": row["token_usage_capture_event"],
            "token_usage_artifact_path": row["token_usage_artifact_path"],
            "token_usage_error": row["token_usage_error"],
        }

    def _row_to_message(
        self, row: sqlite3.Row, *, delivered_at: float | None = None
    ) -> dict[str, Any]:
        keys = set(row.keys())
        return {
            "id": row["id"],
            "sender_session_id": row["sender_session_id"],
            "sender_name": row["sender_name"] if "sender_name" in keys else None,
            "sender_client": row["sender_client"] if "sender_client" in keys else None,
            "sender_bead_id": (
                row["sender_bead_id"] if "sender_bead_id" in keys else None
            ),
            "recipient_session_id": row["recipient_session_id"],
            "body": row["body"],
            "classification": row["classification"],
            "thread_id": row["thread_id"],
            "reply_required": bool(row["reply_required"]),
            "terminal": row["classification"] == "closure",
            "created_at": _iso(row["created_at"]),
            "delivered_at": _iso(
                row["delivered_at"] if row["delivered_at"] is not None else delivered_at
            ),
            "acknowledged_at": _iso(row["acknowledged_at"]),
        }

    def _insert_message(
        self,
        connection: sqlite3.Connection,
        *,
        sender_session_id: str,
        recipient_session_id: str,
        body: str,
        classification: str,
        thread_id: str,
        reply_required: bool,
        now: float,
    ) -> tuple[sqlite3.Row, bool]:
        if classification not in MESSAGE_CLASSIFICATIONS:
            raise CoordinationError(
                f"Unknown message classification: {classification}."
            )
        normalized_thread = thread_id.strip()
        if not normalized_thread:
            raise CoordinationError("Message thread ID must not be empty.")
        connection.execute(
            """
            INSERT OR IGNORE INTO message_threads (
                thread_id, created_at, updated_at, closed_at
            ) VALUES (?, ?, ?, NULL)
            """,
            (normalized_thread, now, now),
        )
        thread = connection.execute(
            "SELECT * FROM message_threads WHERE thread_id = ?",
            (normalized_thread,),
        ).fetchone()
        assert thread is not None
        participant = connection.execute(
            """
            SELECT sender_session_id, recipient_session_id
            FROM messages
            WHERE thread_id = ?
            ORDER BY id
            LIMIT 1
            """,
            (normalized_thread,),
        ).fetchone()
        if participant is not None and not (
            (
                participant["sender_session_id"] == sender_session_id
                and participant["recipient_session_id"] == recipient_session_id
            )
            or (
                participant["sender_session_id"] == recipient_session_id
                and participant["recipient_session_id"] == sender_session_id
            )
        ):
            raise CoordinationError(
                f"Thread {normalized_thread} belongs to a different participant pair."
            )
        if classification == "closure" and thread["closed_at"] is not None:
            existing = connection.execute(
                """
                SELECT * FROM messages
                WHERE thread_id = ? AND classification = 'closure'
                ORDER BY id DESC LIMIT 1
                """,
                (normalized_thread,),
            ).fetchone()
            if existing is not None:
                # The participant-pair check above deliberately permits a
                # reverse-direction terminal reply in the same conversation.
                return existing, False
        if classification == "action_required":
            connection.execute(
                """
                UPDATE message_threads
                SET updated_at = ?, closed_at = NULL
                WHERE thread_id = ?
                """,
                (now, normalized_thread),
            )
        else:
            connection.execute(
                "UPDATE message_threads SET updated_at = ? WHERE thread_id = ?",
                (now, normalized_thread),
            )
        cursor = connection.execute(
            """
            INSERT INTO messages (
                sender_session_id, recipient_session_id, body, created_at,
                delivered_at, classification, thread_id, reply_required
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sender_session_id,
                recipient_session_id,
                body,
                now,
                now if classification == "closure" else None,
                classification,
                normalized_thread,
                int(reply_required),
            ),
        )
        if classification == "closure":
            # Closure suppresses every older pending actionable notification in
            # this conversation before any wake or hook can observe it.
            connection.execute(
                """
                UPDATE messages
                SET delivered_at = ?
                WHERE thread_id = ?
                  AND classification = 'action_required'
                  AND delivered_at IS NULL
                  AND id < ?
                """,
                (now, normalized_thread, int(cursor.lastrowid)),
            )
            connection.execute(
                """
                UPDATE message_threads
                SET updated_at = ?, closed_at = ?
                WHERE thread_id = ?
                """,
                (now, now, normalized_thread),
            )
        row = connection.execute(
            "SELECT * FROM messages WHERE id = ?", (int(cursor.lastrowid),)
        ).fetchone()
        assert row is not None
        return row, True

    def _row_to_handoff(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "handoff_id": row["handoff_id"],
            "sender_session_id": row["sender_session_id"],
            "recipient_session_id": row["recipient_session_id"],
            "source_bead_id": row["source_bead_id"],
            "target_bead_id": row["target_bead_id"],
            "scope": _json_scopes(row["scope_json"]),
            "patch_label": row["patch_label"],
            "validation_boundary": row["validation_boundary"],
            "validation_responsibility": row["validation_responsibility"],
            "mode": row["mode"],
            "thread_id": row["thread_id"],
            "notification_message_id": row["notification_message_id"],
            "created_at": _iso(row["created_at"]),
        }

    def register(
        self,
        *,
        session_id: str,
        client: str,
        cwd: str,
        name: str | None = None,
    ) -> dict[str, Any]:
        if not session_id.strip():
            raise CoordinationError("Session ID must not be empty.")
        if client not in CLIENTS:
            raise CoordinationError("Client must be claude or codex.")
        repository = str(Path(cwd).resolve())
        now = self.clock()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO sessions (
                    session_id, client, cwd, name, activity, bead_id,
                    write_scope_json, started_at, last_seen_at, ended_at
                ) VALUES (?, ?, ?, ?, 'idle', NULL, '[]', ?, ?, NULL)
                ON CONFLICT(session_id) DO UPDATE SET
                    client = excluded.client,
                    cwd = excluded.cwd,
                    name = COALESCE(excluded.name, sessions.name),
                    last_seen_at = excluded.last_seen_at,
                    ended_at = NULL
                """,
                (session_id, client, repository, name, now, now),
            )
        return self.get_session(session_id)

    def get_session(self, session_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        if row is None:
            raise CoordinationError(f"Session {session_id} is not registered.")
        return self._row_to_session(row)

    def touch(
        self,
        session_id: str,
        activity: str | None = None,
        *,
        turn_active: bool | None = None,
    ) -> dict[str, Any]:
        if activity is not None and activity not in ACTIVITIES:
            raise CoordinationError(f"Unknown activity: {activity}.")
        now = self.clock()
        updates = ["last_seen_at = ?", "ended_at = NULL"]
        values: list[Any] = [now]
        if activity is not None:
            updates.append("activity = ?")
            values.append(activity)
        if turn_active is not None:
            updates.append("turn_active = ?")
            values.append(int(turn_active))
        values.append(session_id)
        with self._connection() as connection:
            cursor = connection.execute(
                f"UPDATE sessions SET {', '.join(updates)} WHERE session_id = ?",
                values,
            )
            if cursor.rowcount != 1:
                raise CoordinationError(f"Session {session_id} is not registered.")
        return self.get_session(session_id)

    def _live_rows(self, connection: sqlite3.Connection, cwd: str) -> list[sqlite3.Row]:
        cutoff = self.clock() - self.stale_after_seconds
        return list(
            connection.execute(
                """
                SELECT * FROM sessions
                WHERE cwd = ? AND ended_at IS NULL AND last_seen_at >= ?
                """,
                (str(Path(cwd).resolve()), cutoff),
            )
        )

    @staticmethod
    def _row_has_active_work(row: sqlite3.Row) -> bool:
        return (
            bool(row["turn_active"])
            or row["activity"] in RELEVANT_ACTIVITIES
            or bool(_json_scopes(row["write_scope_json"]))
        )

    def scope_blocking_peers(self, session_id: str) -> list[dict[str, Any]]:
        """Return live peers that require an unscoped writer to coordinate."""
        with self._connection() as connection:
            current = connection.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if current is None:
                raise CoordinationError(f"Session {session_id} is not registered.")
            if _json_scopes(current["write_scope_json"]):
                return []
            peers = [
                row
                for row in self._live_rows(connection, str(current["cwd"]))
                if row["session_id"] != session_id and self._row_has_active_work(row)
            ]
            if not peers:
                return []
            if current["scope_requested_at"] is not None:
                return [self._row_to_session(row) for row in peers]

            current_has_work = current["activity"] in RELEVANT_ACTIVITIES
            current_order = (current["started_at"], current["session_id"])
            blockers: list[sqlite3.Row] = []
            for peer in peers:
                peer_has_work = (
                    peer["activity"] in RELEVANT_ACTIVITIES
                    or bool(_json_scopes(peer["write_scope_json"]))
                )
                peer_order = (peer["started_at"], peer["session_id"])
                if peer_has_work or (not current_has_work and peer_order < current_order):
                    blockers.append(peer)
            return [self._row_to_session(row) for row in blockers]

    @staticmethod
    def _clear_scope_requirement_in_connection(
        connection: sqlite3.Connection, session_id: str, now: float
    ) -> None:
        connection.execute(
            "UPDATE sessions SET scope_requested_at = NULL WHERE session_id = ?",
            (session_id,),
        )
        connection.execute(
            """
            UPDATE messages
            SET delivered_at = COALESCE(delivered_at, ?),
                acknowledged_at = COALESCE(acknowledged_at, ?)
            WHERE recipient_session_id = ?
              AND classification = 'action_required'
              AND thread_id LIKE ?
              AND acknowledged_at IS NULL
            """,
            (now, now, session_id, f"{SCOPE_REQUEST_THREAD_PREFIX}%"),
        )

    def clear_scope_requirement(self, session_id: str) -> dict[str, Any]:
        now = self.clock()
        self.get_session(session_id)
        with self._connection() as connection:
            self._clear_scope_requirement_in_connection(connection, session_id, now)
        return self.get_session(session_id)

    def request_scope_declarations(
        self, sender_session_id: str, peer_session_ids: Iterable[str]
    ) -> list[dict[str, Any]]:
        """Ask live, unscoped peers to declare scopes once per open request."""
        sender = self.get_session(sender_session_id)
        requested_ids = sorted(
            {peer_id for peer_id in peer_session_ids if peer_id != sender_session_id}
        )
        if not requested_ids:
            return []
        now = self.clock()
        inserted_messages: list[dict[str, Any]] = []
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            live_peers = {
                row["session_id"]: row
                for row in self._live_rows(connection, sender["cwd"])
                if row["session_id"] in requested_ids
            }
            for peer_id in requested_ids:
                peer = live_peers.get(peer_id)
                if peer is None or _json_scopes(peer["write_scope_json"]):
                    continue
                updated = connection.execute(
                    """
                    UPDATE sessions
                    SET scope_requested_at = ?
                    WHERE session_id = ? AND scope_requested_at IS NULL
                    """,
                    (now, peer_id),
                )
                if updated.rowcount != 1:
                    continue
                pair = sorted((sender_session_id, peer_id))
                thread_id = f"{SCOPE_REQUEST_THREAD_PREFIX}{pair[0]}:{pair[1]}"
                body = (
                    "Another live agent is ready to write in this repository. "
                    "Before your next write, declare the smallest current scope "
                    f"with agent-coord begin-work --session-id {peer_id} "
                    "--scope '<path-or-glob>'. A Beads issue is optional. If you "
                    "have no active work, end the session or run agent-coord "
                    f"end-work --session-id {peer_id}."
                )
                message_row, inserted = self._insert_message(
                    connection,
                    sender_session_id=sender_session_id,
                    recipient_session_id=peer_id,
                    body=body,
                    classification="action_required",
                    thread_id=thread_id,
                    reply_required=False,
                    now=now,
                )
                if inserted:
                    inserted_messages.append(self._row_to_message(message_row))
        return inserted_messages

    def _find_conflicts(
        self,
        connection: sqlite3.Connection,
        *,
        session_id: str,
        cwd: str,
        bead_id: str | None,
        scopes: list[str],
    ) -> list[dict[str, Any]]:
        conflicts: list[dict[str, Any]] = []
        for row in self._live_rows(connection, cwd):
            if row["session_id"] == session_id:
                continue
            other_scopes = _json_scopes(row["write_scope_json"])
            overlaps = sorted(
                {
                    f"{left} ↔ {right}"
                    for left in scopes
                    for right in other_scopes
                    if scopes_overlap(left, right)
                }
            )
            same_bead = bead_id is not None and row["bead_id"] == bead_id
            if same_bead or overlaps:
                conflicts.append(
                    {
                        "session_id": row["session_id"],
                        "client": row["client"],
                        "name": row["name"],
                        "activity": row["activity"],
                        "bead_id": row["bead_id"],
                        "write_scope": other_scopes,
                        "same_bead": same_bead,
                        "overlaps": overlaps,
                    }
                )
        return conflicts

    def begin_work(
        self,
        *,
        session_id: str,
        scopes: Iterable[str],
        bead_id: str | None = None,
        activity: str = "implementing",
        lease_mode: str = "write",
    ) -> dict[str, Any]:
        if activity not in {"planning", "implementing", "validating"}:
            raise CoordinationError(
                "Work activity must be planning, implementing, or validating."
            )
        normalized_bead = bead_id.strip() if bead_id is not None else None
        if bead_id is not None and not normalized_bead:
            raise CoordinationError("Bead ID must not be empty when supplied.")
        if lease_mode not in LEASE_MODES:
            raise CoordinationError("Lease mode must be write or validation.")
        if lease_mode == "validation":
            activity = "validating"
        session = self.get_session(session_id)
        normalized = sorted(
            {normalize_scope(scope, session["cwd"]) for scope in scopes}
        )
        if not normalized:
            raise CoordinationError("At least one repository scope is required.")
        now = self.clock()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current_row = connection.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            assert current_row is not None
            delegated_rows = list(
                connection.execute(
                    """
                    SELECT * FROM delegations
                    WHERE child_session_id = ? AND status = 'attached'
                    """,
                    (session_id,),
                )
            )
            if len(delegated_rows) > 1:
                raise CoordinationError(
                    f"Session {session_id} has more than one attached delegation."
                )
            if delegated_rows:
                delegated = delegated_rows[0]
                delegated_scopes = _json_scopes(delegated["write_scope_json"])
                if normalized_bead != delegated["bead_id"]:
                    raise CoordinationError(
                        f"Delegation {delegated['delegation_id']} requires Bead "
                        f"{delegated['bead_id']}."
                    )
                if normalized != delegated_scopes:
                    raise CoordinationError(
                        f"Delegation {delegated['delegation_id']} requires its exact scopes."
                    )
                if lease_mode != delegated["lease_mode"]:
                    raise CoordinationError(
                        f"Delegation {delegated['delegation_id']} requires "
                        f"lease mode {delegated['lease_mode']}."
                    )
            if current_row["scope_requested_at"] is None:
                requested_predecessors = [
                    row
                    for row in self._live_rows(connection, session["cwd"])
                    if row["session_id"] != session_id
                    and row["scope_requested_at"] is not None
                    and not _json_scopes(row["write_scope_json"])
                ]
                if requested_predecessors:
                    owners = ", ".join(
                        row["name"] or row["session_id"]
                        for row in requested_predecessors
                    )
                    raise CoordinationError(
                        "Wait for the requested incumbent scope declaration(s) "
                        f"before declaring newcomer work: {owners}."
                    )
            conflicts = self._find_conflicts(
                connection,
                session_id=session_id,
                cwd=session["cwd"],
                bead_id=normalized_bead,
                scopes=normalized,
            )
            if conflicts:
                raise ConflictError(conflicts)
            self._clear_scope_requirement_in_connection(connection, session_id, now)
            connection.execute(
                """
                UPDATE sessions
                SET bead_id = ?, write_scope_json = ?, activity = ?,
                    lease_mode = ?, last_seen_at = ?, ended_at = NULL
                WHERE session_id = ?
                """,
                (
                    normalized_bead,
                    json.dumps(normalized),
                    activity,
                    lease_mode,
                    now,
                    session_id,
                ),
            )
        return self.get_session(session_id)

    def check_conflicts(self, session_id: str) -> list[dict[str, Any]]:
        session = self.get_session(session_id)
        if not session["write_scope"]:
            return []
        with self._connection() as connection:
            return self._find_conflicts(
                connection,
                session_id=session_id,
                cwd=session["cwd"],
                bead_id=session["bead_id"],
                scopes=session["write_scope"],
            )

    def end_work(self, session_id: str) -> dict[str, Any]:
        now = self.clock()
        with self._connection() as connection:
            self._clear_scope_requirement_in_connection(connection, session_id, now)
            cursor = connection.execute(
                """
                UPDATE sessions
                SET bead_id = NULL, write_scope_json = '[]', activity = 'idle',
                    lease_mode = 'write', last_seen_at = ?
                WHERE session_id = ?
                """,
                (now, session_id),
            )
            if cursor.rowcount != 1:
                raise CoordinationError(f"Session {session_id} is not registered.")
        return self.get_session(session_id)

    def handoff_work(
        self,
        *,
        sender_session_id: str,
        recipient_session_id: str,
        patch_label: str,
        validation_boundary: str,
        validation_responsibility: str,
        mode: str,
        target_bead_id: str | None = None,
        scopes: Iterable[str] | None = None,
        handoff_id: str | None = None,
        thread_id: str | None = None,
    ) -> dict[str, Any]:
        if sender_session_id == recipient_session_id:
            raise CoordinationError("A work declaration cannot be handed to itself.")
        if mode not in LEASE_MODES:
            raise CoordinationError("Handoff mode must be write or validation.")
        audit_fields = {
            "patch label": patch_label,
            "validation boundary": validation_boundary,
            "validation responsibility": validation_responsibility,
        }
        for label, value in audit_fields.items():
            if not value.strip():
                raise CoordinationError(f"Handoff {label} must not be empty.")
        identifier = handoff_id.strip() if handoff_id is not None else str(uuid.uuid4())
        if not identifier:
            raise CoordinationError("Handoff ID must not be empty.")
        notification_thread = (
            thread_id.strip() if thread_id is not None else f"handoff:{identifier}"
        )
        if not notification_thread:
            raise CoordinationError("Handoff thread ID must not be empty.")
        now = self.clock()
        cutoff = now - self.stale_after_seconds
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            sender_row = connection.execute(
                "SELECT * FROM sessions WHERE session_id = ?",
                (sender_session_id,),
            ).fetchone()
            recipient_row = connection.execute(
                "SELECT * FROM sessions WHERE session_id = ?",
                (recipient_session_id,),
            ).fetchone()
            if sender_row is None:
                raise CoordinationError(
                    f"Session {sender_session_id} is not registered."
                )
            if recipient_row is None:
                raise CoordinationError(
                    f"Session {recipient_session_id} is not registered."
                )
            if (
                sender_row["ended_at"] is not None
                or sender_row["last_seen_at"] < cutoff
            ):
                raise CoordinationError(
                    f"Sender session {sender_session_id} must be online."
                )
            if sender_row["bead_id"] is None:
                raise CoordinationError(
                    "Atomic handoff requires a Bead-backed work declaration; "
                    f"session {sender_session_id} has no Bead."
                )
            sender_scopes = _json_scopes(sender_row["write_scope_json"])
            if not sender_scopes:
                raise CoordinationError("The sender declaration has no scopes.")
            requested_scopes = sender_scopes
            if scopes is not None:
                requested_scopes = sorted(
                    {
                        normalize_scope(scope, sender_row["cwd"])
                        for scope in scopes
                    }
                )
                if requested_scopes != sorted(sender_scopes):
                    raise CoordinationError(
                        "Partial scope handoffs are not supported; hand off the "
                        "whole declaration with its exact scopes."
                    )
            if recipient_row["cwd"] != sender_row["cwd"]:
                raise CoordinationError(
                    "Handoff recipient must be registered in the same repository."
                )
            if (
                recipient_row["ended_at"] is not None
                or recipient_row["last_seen_at"] < cutoff
            ):
                raise CoordinationError(
                    f"Recipient session {recipient_session_id} must be online."
                )
            if (
                recipient_row["activity"] != "idle"
                or bool(recipient_row["turn_active"])
                or recipient_row["bead_id"] is not None
                or _json_scopes(recipient_row["write_scope_json"])
            ):
                raise CoordinationError(
                    f"Recipient session {recipient_session_id} must be idle and "
                    "have no work declaration."
                )
            source_bead_id = str(sender_row["bead_id"])
            target = (
                source_bead_id
                if target_bead_id is None
                else target_bead_id.strip()
            )
            if not target:
                raise CoordinationError("Target Bead ID must not be empty.")

            connection.execute(
                """
                UPDATE sessions
                SET bead_id = NULL, write_scope_json = '[]', activity = 'idle',
                    lease_mode = 'write', last_seen_at = ?
                WHERE session_id = ?
                """,
                (now, sender_session_id),
            )
            conflicts = self._find_conflicts(
                connection,
                session_id=recipient_session_id,
                cwd=str(sender_row["cwd"]),
                bead_id=target,
                scopes=requested_scopes,
            )
            if conflicts:
                raise ConflictError(conflicts)
            recipient_activity = "validating" if mode == "validation" else "implementing"
            connection.execute(
                """
                UPDATE sessions
                SET bead_id = ?, write_scope_json = ?, activity = ?,
                    lease_mode = ?, last_seen_at = ?, ended_at = NULL
                WHERE session_id = ?
                """,
                (
                    target,
                    json.dumps(requested_scopes),
                    recipient_activity,
                    mode,
                    now,
                    recipient_session_id,
                ),
            )
            notification_body = (
                f"Handoff {identifier}: patch {patch_label.strip()} transferred "
                f"{source_bead_id} to {target} in {mode} mode. Validation boundary: "
                f"{validation_boundary.strip()}. Validation responsibility: "
                f"{validation_responsibility.strip()}."
            )
            message_row, inserted = self._insert_message(
                connection,
                sender_session_id=sender_session_id,
                recipient_session_id=recipient_session_id,
                body=notification_body,
                classification="action_required",
                thread_id=notification_thread,
                reply_required=False,
                now=now,
            )
            if not inserted:
                raise CoordinationError(
                    f"Handoff thread {notification_thread} is already closed."
                )
            connection.execute(
                """
                INSERT INTO handoffs (
                    handoff_id, sender_session_id, recipient_session_id,
                    source_bead_id, target_bead_id, scope_json, patch_label,
                    validation_boundary, validation_responsibility, mode,
                    thread_id, notification_message_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identifier,
                    sender_session_id,
                    recipient_session_id,
                    source_bead_id,
                    target,
                    json.dumps(requested_scopes),
                    patch_label.strip(),
                    validation_boundary.strip(),
                    validation_responsibility.strip(),
                    mode,
                    notification_thread,
                    message_row["id"],
                    now,
                ),
            )
            handoff_row = connection.execute(
                "SELECT * FROM handoffs WHERE handoff_id = ?", (identifier,)
            ).fetchone()
            updated_sender = connection.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (sender_session_id,)
            ).fetchone()
            updated_recipient = connection.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (recipient_session_id,)
            ).fetchone()
            assert handoff_row is not None
            assert updated_sender is not None
            assert updated_recipient is not None
            result = self._row_to_handoff(handoff_row)
            result["sender"] = self._row_to_session(updated_sender, now)
            result["recipient"] = self._row_to_session(updated_recipient, now)
            result["notification"] = self._row_to_message(message_row)
            return result

    def get_handoff(self, handoff_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM handoffs WHERE handoff_id = ?", (handoff_id,)
            ).fetchone()
        if row is None:
            raise CoordinationError(f"Handoff {handoff_id} does not exist.")
        return self._row_to_handoff(row)

    def end_session(self, session_id: str) -> dict[str, Any]:
        now = self.clock()
        with self._connection() as connection:
            self._clear_scope_requirement_in_connection(connection, session_id, now)
            cursor = connection.execute(
                """
                UPDATE sessions
                SET activity = 'idle', turn_active = 0,
                    last_seen_at = ?, ended_at = ?
                WHERE session_id = ?
                """,
                (now, now, session_id),
            )
            if cursor.rowcount != 1:
                raise CoordinationError(f"Session {session_id} is not registered.")
        return self.get_session(session_id)

    def list_sessions(
        self,
        *,
        cwd: str | None = None,
        relevant_only: bool = False,
        include_offline: bool = False,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if cwd is not None:
            clauses.append("cwd = ?")
            values.append(str(Path(cwd).resolve()))
        if not include_offline:
            clauses.append("ended_at IS NULL")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connection() as connection:
            rows = list(
                connection.execute(
                    f"SELECT * FROM sessions{where} ORDER BY last_seen_at DESC",
                    values,
                )
            )
        sessions = [self._row_to_session(row) for row in rows]
        if relevant_only:
            sessions = [
                session
                for session in sessions
                if session["bead_id"] is not None
                or session["activity"] in RELEVANT_ACTIVITIES
            ]
        return sessions

    def proposed_work_conflicts(
        self,
        *,
        cwd: str,
        bead_id: str,
        scopes: Iterable[str],
        exclude_session_id: str = "",
    ) -> list[dict[str, Any]]:
        repository = str(Path(cwd).resolve())
        normalized = sorted({normalize_scope(scope, repository) for scope in scopes})
        if not normalized:
            raise CoordinationError("At least one write scope is required.")
        with self._connection() as connection:
            return self._find_conflicts(
                connection,
                session_id=exclude_session_id,
                cwd=repository,
                bead_id=bead_id,
                scopes=normalized,
            )

    def create_delegation(
        self,
        *,
        parent_session_id: str,
        cwd: str,
        bead_id: str,
        scopes: Iterable[str],
        instructions: str,
        mode: str,
        lease_mode: str = "write",
        bypass_hook_trust: bool = False,
        model: str | None = None,
        reasoning_effort: str | None = None,
        client: str = "codex",
        delegation_id: str | None = None,
    ) -> dict[str, Any]:
        self.get_session(parent_session_id)
        repository = str(Path(cwd).resolve())
        if not bead_id.strip():
            raise CoordinationError("Delegated Bead ID must not be empty.")
        normalized = sorted(
            {normalize_scope(scope, repository) for scope in scopes}
        )
        if not normalized:
            raise CoordinationError("At least one delegated scope is required.")
        if not instructions.strip():
            raise CoordinationError("Delegated instructions must not be empty.")
        if client not in CLIENTS:
            raise CoordinationError("Delegated client must be claude or codex.")
        client_label = "Claude Code" if client == "claude" else "Codex"
        if bypass_hook_trust and client != "codex":
            raise CoordinationError("Hook-trust bypass is only supported for Codex.")
        if mode not in {"reviewed", "yolo"}:
            raise CoordinationError("Delegation mode must be reviewed or yolo.")
        if lease_mode not in LEASE_MODES:
            raise CoordinationError("Delegation lease mode must be write or validation.")
        if lease_mode == "validation" and mode == "yolo":
            raise CoordinationError("Validation-only delegation cannot use yolo mode.")
        normalized_model = model.strip() if model is not None else None
        if model is not None and not normalized_model:
            raise CoordinationError(f"{client_label} model must not be empty.")
        normalized_reasoning_effort = (
            reasoning_effort.strip() if reasoning_effort is not None else None
        )
        if reasoning_effort is not None and not normalized_reasoning_effort:
            raise CoordinationError(f"{client_label} effort must not be empty.")

        identifier = delegation_id or str(uuid.uuid4())
        now = self.clock()
        try:
            with self._connection() as connection:
                connection.execute(
                    """
                    INSERT INTO delegations (
                        delegation_id, parent_session_id, child_session_id,
                        client, cwd, bead_id, write_scope_json, instructions,
                        status, zellij_session, pane_id, mode, lease_mode,
                        bypass_hook_trust, model, reasoning_effort, created_at,
                        updated_at, completed_at, result_message, error
                    ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, 'launching', NULL,
                              NULL, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)
                    """,
                    (
                        identifier,
                        parent_session_id,
                        client,
                        repository,
                        bead_id.strip(),
                        json.dumps(normalized),
                        instructions.strip(),
                        mode,
                        lease_mode,
                        int(bypass_hook_trust),
                        normalized_model,
                        normalized_reasoning_effort,
                        now,
                        now,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            with self._connection() as connection:
                active = connection.execute(
                    """
                    SELECT delegation_id FROM delegations
                    WHERE cwd = ? AND bead_id = ?
                      AND status IN ('launching', 'launched', 'attached')
                    """,
                    (repository, bead_id.strip()),
                ).fetchone()
            if active is not None:
                raise CoordinationError(
                    f"Bead {bead_id} already has active delegation "
                    f"{active['delegation_id']}."
                ) from exc
            raise
        return self.get_delegation(identifier)

    def get_delegation(self, delegation_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM delegations WHERE delegation_id = ?",
                (delegation_id,),
            ).fetchone()
        if row is None:
            raise CoordinationError(f"Delegation {delegation_id} does not exist.")
        return self._row_to_delegation(row)

    def list_delegations(
        self,
        *,
        parent_session_id: str | None = None,
        include_terminal: bool = True,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if parent_session_id is not None:
            self.get_session(parent_session_id)
            clauses.append("parent_session_id = ?")
            values.append(parent_session_id)
        if not include_terminal:
            clauses.append("status IN ('launching', 'launched', 'attached')")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connection() as connection:
            rows = list(
                connection.execute(
                    f"SELECT * FROM delegations{where} ORDER BY created_at DESC",
                    values,
                )
            )
        return [self._row_to_delegation(row) for row in rows]

    def mark_delegation_launched(
        self,
        delegation_id: str,
        *,
        zellij_session: str,
        pane_id: str,
    ) -> dict[str, Any]:
        now = self.clock()
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE delegations
                SET status = CASE
                        WHEN status = 'launching' THEN 'launched'
                        ELSE status
                    END,
                    zellij_session = ?, pane_id = ?, updated_at = ?
                WHERE delegation_id = ?
                  AND status IN (
                      'launching', 'launched', 'attached', 'completed', 'failed'
                  )
                """,
                (zellij_session, pane_id, now, delegation_id),
            )
            if cursor.rowcount != 1:
                raise CoordinationError(
                    f"Delegation {delegation_id} cannot be marked launched."
                )
        return self.get_delegation(delegation_id)

    def fail_delegation_launch(
        self, delegation_id: str, error: str
    ) -> dict[str, Any]:
        now = self.clock()
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE delegations
                SET status = 'failed', error = ?, result_message = ?,
                    updated_at = ?, completed_at = ?
                WHERE delegation_id = ? AND status = 'launching'
                """,
                (error.strip(), error.strip(), now, now, delegation_id),
            )
            if cursor.rowcount != 1:
                raise CoordinationError(
                    f"Delegation {delegation_id} is no longer launching."
                )
        return self.get_delegation(delegation_id)

    def cancel_delegation(
        self,
        delegation_id: str,
        *,
        parent_session_id: str,
        reason: str,
    ) -> dict[str, Any]:
        if not reason.strip():
            raise CoordinationError("Delegation cancellation reason must not be empty.")
        now = self.clock()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM delegations WHERE delegation_id = ?",
                (delegation_id,),
            ).fetchone()
            if row is None:
                raise CoordinationError(
                    f"Delegation {delegation_id} does not exist."
                )
            if row["parent_session_id"] != parent_session_id:
                raise CoordinationError(
                    f"Session {parent_session_id} is not the parent of delegation "
                    f"{delegation_id}."
                )
            if row["status"] in {"completed", "failed"}:
                raise CoordinationError(
                    f"Delegation {delegation_id} is already {row['status']}."
                )
            connection.execute(
                """
                UPDATE delegations
                SET status = 'failed', result_message = ?, error = ?,
                    updated_at = ?, completed_at = ?
                WHERE delegation_id = ?
                """,
                (reason.strip(), reason.strip(), now, now, delegation_id),
            )
        return self.get_delegation(delegation_id)

    def attach_delegation(
        self, delegation_id: str, child_session_id: str
    ) -> dict[str, Any]:
        child = self.get_session(child_session_id)
        now = self.clock()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM delegations WHERE delegation_id = ?",
                (delegation_id,),
            ).fetchone()
            if row is None:
                raise CoordinationError(
                    f"Delegation {delegation_id} does not exist."
                )
            if child["cwd"] != row["cwd"]:
                raise CoordinationError(
                    f"Delegation {delegation_id} targets {row['cwd']}, but session "
                    f"{child_session_id} registered in {child['cwd']}."
                )
            if child["client"] != row["client"]:
                raise CoordinationError(
                    f"Delegation {delegation_id} targets {row['client']}, but session "
                    f"{child_session_id} registered as {child['client']}."
                )
            attached = row["child_session_id"]
            if attached is not None and attached != child_session_id:
                raise CoordinationError(
                    f"Delegation {delegation_id} is already attached to session "
                    f"{attached}."
                )
            if row["status"] in {"completed", "failed"}:
                if attached == child_session_id:
                    return self._row_to_delegation(row)
                raise CoordinationError(
                    f"Delegation {delegation_id} is already {row['status']}."
                )
            if row["status"] not in {"launching", "launched", "attached"}:
                raise CoordinationError(
                    f"Delegation {delegation_id} cannot attach from status "
                    f"{row['status']}."
                )
            connection.execute(
                """
                UPDATE delegations
                SET child_session_id = ?, status = 'attached', updated_at = ?
                WHERE delegation_id = ?
                """,
                (child_session_id, now, delegation_id),
            )
        return self.get_delegation(delegation_id)

    def record_delegation_token_usage(
        self,
        delegation_id: str,
        *,
        child_session_id: str,
        usage: dict[str, Any] | None,
        capture_event: str,
        artifact_path: str | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        if capture_event not in {"Stop", "SessionEnd"}:
            raise CoordinationError(
                "Token usage capture event must be Stop or SessionEnd."
            )
        if usage is None and not (error and error.strip()):
            raise CoordinationError("Token usage capture requires usage or an error.")
        encoded = json.dumps(usage, sort_keys=True) if usage is not None else None
        normalized_error = error.strip() if error and error.strip() else None
        now = self.clock()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM delegations WHERE delegation_id = ?",
                (delegation_id,),
            ).fetchone()
            if row is None:
                raise CoordinationError(
                    f"Delegation {delegation_id} does not exist."
                )
            if row["child_session_id"] != child_session_id:
                raise CoordinationError(
                    f"Session {child_session_id} is not attached to delegation "
                    f"{delegation_id}."
                )
            if row["status"] not in {"completed", "failed"}:
                raise CoordinationError(
                    f"Delegation {delegation_id} is not terminal."
                )
            if row["token_usage_json"] is not None:
                return self._row_to_delegation(row)
            if encoded is None:
                connection.execute(
                    """
                    UPDATE delegations
                    SET token_usage_capture_event = ?, token_usage_error = ?,
                        updated_at = ?
                    WHERE delegation_id = ? AND token_usage_json IS NULL
                    """,
                    (capture_event, normalized_error, now, delegation_id),
                )
            else:
                connection.execute(
                    """
                    UPDATE delegations
                    SET token_usage_json = ?, token_usage_captured_at = ?,
                        token_usage_capture_event = ?,
                        token_usage_artifact_path = ?, token_usage_error = ?,
                        updated_at = ?
                    WHERE delegation_id = ? AND token_usage_json IS NULL
                    """,
                    (
                        encoded,
                        now,
                        capture_event,
                        artifact_path,
                        normalized_error,
                        now,
                        delegation_id,
                    ),
                )
        return self.get_delegation(delegation_id)

    def finish_delegation(
        self,
        delegation_id: str,
        *,
        child_session_id: str,
        outcome: str,
        message: str,
    ) -> dict[str, Any]:
        if outcome not in {"completed", "failed"}:
            raise CoordinationError(
                "Delegation outcome must be completed or failed."
            )
        if not message.strip():
            raise CoordinationError("Delegation result message must not be empty.")
        now = self.clock()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM delegations WHERE delegation_id = ?",
                (delegation_id,),
            ).fetchone()
            if row is None:
                raise CoordinationError(
                    f"Delegation {delegation_id} does not exist."
                )
            if row["child_session_id"] != child_session_id:
                raise CoordinationError(
                    f"Session {child_session_id} is not attached to delegation "
                    f"{delegation_id}."
                )
            if row["status"] in {"completed", "failed"}:
                if row["status"] == outcome and row["result_message"] == message.strip():
                    return self._row_to_delegation(row)
                raise CoordinationError(
                    f"Delegation {delegation_id} is already {row['status']}."
                )
            if row["status"] != "attached":
                raise CoordinationError(
                    f"Delegation {delegation_id} is not attached."
                )
            connection.execute(
                """
                UPDATE delegations
                SET status = ?, result_message = ?, error = ?,
                    updated_at = ?, completed_at = ?
                WHERE delegation_id = ?
                """,
                (
                    outcome,
                    message.strip(),
                    message.strip() if outcome == "failed" else None,
                    now,
                    now,
                    delegation_id,
                ),
            )
            notification = (
                f"Delegation {delegation_id} for Bead {row['bead_id']} "
                f"{outcome}: {message.strip()}"
            )
            self._insert_message(
                connection,
                sender_session_id=child_session_id,
                recipient_session_id=row["parent_session_id"],
                body=notification,
                classification="action_required",
                thread_id=f"delegation:{delegation_id}",
                reply_required=True,
                now=now,
            )
        return self.get_delegation(delegation_id)

    def fail_active_delegations_for_child(
        self, child_session_id: str, reason: str
    ) -> list[dict[str, Any]]:
        if not reason.strip():
            raise CoordinationError("Delegation failure reason must not be empty.")
        now = self.clock()
        failed_ids: list[str] = []
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = list(
                connection.execute(
                    """
                    SELECT * FROM delegations
                    WHERE child_session_id = ?
                      AND status IN ('launching', 'launched', 'attached')
                    """,
                    (child_session_id,),
                )
            )
            for row in rows:
                connection.execute(
                    """
                    UPDATE delegations
                    SET status = 'failed', result_message = ?, error = ?,
                        updated_at = ?, completed_at = ?
                    WHERE delegation_id = ?
                    """,
                    (reason.strip(), reason.strip(), now, now, row["delegation_id"]),
                )
                notification = (
                    f"Delegation {row['delegation_id']} for Bead {row['bead_id']} "
                    f"failed: {reason.strip()}"
                )
                self._insert_message(
                    connection,
                    sender_session_id=child_session_id,
                    recipient_session_id=row["parent_session_id"],
                    body=notification,
                    classification="action_required",
                    thread_id=f"delegation:{row['delegation_id']}",
                    reply_required=True,
                    now=now,
                )
                failed_ids.append(row["delegation_id"])
        return [self.get_delegation(identifier) for identifier in failed_ids]

    def register_zellij_wake(
        self,
        *,
        session_id: str,
        zellij_session: str,
        pane_id: str,
        watcher_pid: int | None = None,
    ) -> dict[str, Any]:
        self.get_session(session_id)
        if not zellij_session.strip():
            raise CoordinationError("Zellij session name must not be empty.")
        if not pane_id.strip():
            raise CoordinationError("Zellij pane ID must not be empty.")
        now = self.clock()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO zellij_wake_targets (
                    session_id, zellij_session, pane_id, enabled, watcher_pid,
                    registered_at, updated_at
                ) VALUES (?, ?, ?, 1, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    zellij_session = excluded.zellij_session,
                    pane_id = excluded.pane_id,
                    enabled = 1,
                    watcher_pid = COALESCE(excluded.watcher_pid, watcher_pid),
                    updated_at = excluded.updated_at,
                    last_error = NULL
                """,
                (
                    session_id,
                    zellij_session.strip(),
                    pane_id.strip(),
                    watcher_pid,
                    now,
                    now,
                ),
            )
        return self.get_zellij_wake(session_id)

    def get_zellij_wake(self, session_id: str) -> dict[str, Any]:
        self.get_session(session_id)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM zellij_wake_targets WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            attempts = list(
                connection.execute(
                    """
                    SELECT message_id, attempted_at, outcome, detail
                    FROM message_wake_attempts
                    WHERE session_id = ?
                    ORDER BY message_id DESC
                    LIMIT 20
                    """,
                    (session_id,),
                )
            )
        if row is None:
            raise CoordinationError(
                f"Session {session_id} has no Zellij wake registration."
            )
        return {
            "session_id": row["session_id"],
            "zellij_session": row["zellij_session"],
            "pane_id": row["pane_id"],
            "enabled": bool(row["enabled"]),
            "watcher_pid": row["watcher_pid"],
            "registered_at": _iso(row["registered_at"]),
            "updated_at": _iso(row["updated_at"]),
            "last_checked_at": _iso(row["last_checked_at"]),
            "last_wake_at": _iso(row["last_wake_at"]),
            "last_error": row["last_error"],
            "recent_attempts": [
                {
                    "message_id": attempt["message_id"],
                    "attempted_at": _iso(attempt["attempted_at"]),
                    "outcome": attempt["outcome"],
                    "detail": attempt["detail"],
                }
                for attempt in attempts
            ],
        }

    def disable_zellij_wake(self, session_id: str) -> dict[str, Any] | None:
        self.get_session(session_id)
        now = self.clock()
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE zellij_wake_targets
                SET enabled = 0, watcher_pid = NULL, updated_at = ?
                WHERE session_id = ?
                """,
                (now, session_id),
            )
        if cursor.rowcount == 0:
            return None
        return self.get_zellij_wake(session_id)

    def set_zellij_watcher_pid(
        self, session_id: str, watcher_pid: int | None
    ) -> dict[str, Any]:
        now = self.clock()
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE zellij_wake_targets
                SET watcher_pid = ?, updated_at = ?
                WHERE session_id = ? AND enabled = 1
                """,
                (watcher_pid, now, session_id),
            )
        if cursor.rowcount != 1:
            raise CoordinationError(
                f"Session {session_id} has no enabled Zellij wake registration."
            )
        return self.get_zellij_wake(session_id)

    def clear_zellij_watcher_pid(self, session_id: str, watcher_pid: int) -> None:
        now = self.clock()
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE zellij_wake_targets
                SET watcher_pid = NULL, updated_at = ?
                WHERE session_id = ? AND watcher_pid = ?
                """,
                (now, session_id, watcher_pid),
            )

    def record_zellij_check(
        self,
        session_id: str,
        *,
        error: str | None = None,
        woke: bool = False,
    ) -> None:
        now = self.clock()
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE zellij_wake_targets
                SET last_checked_at = ?, last_wake_at = CASE
                        WHEN ? THEN ? ELSE last_wake_at END,
                    last_error = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (now, int(woke), now, error, now, session_id),
            )

    def pending_wake_message_ids(self, session_id: str) -> list[int]:
        self.get_session(session_id)
        with self._connection() as connection:
            rows = list(
                connection.execute(
                    """
                    SELECT messages.id
                    FROM messages
                    LEFT JOIN message_wake_attempts
                        ON message_wake_attempts.message_id = messages.id
                    WHERE messages.recipient_session_id = ?
                      AND messages.delivered_at IS NULL
                      AND messages.classification = 'action_required'
                      AND message_wake_attempts.message_id IS NULL
                    ORDER BY messages.id
                    """,
                    (session_id,),
                )
            )
        return [int(row["id"]) for row in rows]

    def claim_wake_messages(
        self, session_id: str, message_ids: Iterable[int]
    ) -> list[int]:
        requested = sorted({int(message_id) for message_id in message_ids})
        if not requested:
            return []
        now = self.clock()
        claimed: list[int] = []
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            session = connection.execute(
                """
                SELECT activity, turn_active, ended_at
                FROM sessions WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
            target = connection.execute(
                """
                SELECT enabled FROM zellij_wake_targets WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
            if (
                session is None
                or session["ended_at"] is not None
                or bool(session["turn_active"])
                or session["activity"] not in WAKEABLE_ACTIVITIES
                or target is None
                or not bool(target["enabled"])
            ):
                return []
            for message_id in requested:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO message_wake_attempts (
                        message_id, session_id, attempted_at, outcome, detail
                    )
                    SELECT id, recipient_session_id, ?, 'claimed', NULL
                    FROM messages
                    WHERE id = ? AND recipient_session_id = ?
                      AND delivered_at IS NULL
                      AND classification = 'action_required'
                    """,
                    (now, message_id, session_id),
                )
                if cursor.rowcount == 1:
                    claimed.append(message_id)
        return claimed

    def complete_wake_attempts(
        self,
        session_id: str,
        message_ids: Iterable[int],
        *,
        outcome: str,
        detail: str | None = None,
    ) -> None:
        if outcome not in {"sent", "failed"}:
            raise CoordinationError("Wake outcome must be sent or failed.")
        ids = sorted({int(message_id) for message_id in message_ids})
        with self._connection() as connection:
            connection.executemany(
                """
                UPDATE message_wake_attempts
                SET outcome = ?, detail = ?
                WHERE message_id = ? AND session_id = ? AND outcome = 'claimed'
                """,
                [(outcome, detail, message_id, session_id) for message_id in ids],
            )

    def send_message(
        self,
        *,
        sender_session_id: str,
        body: str,
        recipient_session_id: str | None = None,
        recipient_bead_id: str | None = None,
        classification: str = "action_required",
        thread_id: str | None = None,
        reply_required: bool | None = None,
    ) -> dict[str, Any]:
        if bool(recipient_session_id) == bool(recipient_bead_id):
            raise CoordinationError("Specify exactly one recipient session or bead.")
        if not body.strip():
            raise CoordinationError("Message body must not be empty.")
        if classification not in MESSAGE_CLASSIFICATIONS:
            raise CoordinationError(
                f"Unknown message classification: {classification}."
            )
        if reply_required is not None and not isinstance(reply_required, bool):
            raise CoordinationError("Message reply_required must be a boolean.")
        sender = self.get_session(sender_session_id)
        recipient: dict[str, Any]
        if recipient_session_id:
            recipient = self.get_session(recipient_session_id)
        else:
            candidates = [
                session
                for session in self.list_sessions(cwd=sender["cwd"])
                if session["bead_id"] == recipient_bead_id
                and session["presence"] == "online"
            ]
            if not candidates:
                raise CoordinationError(
                    f"No live session declares bead {recipient_bead_id}."
                )
            if len(candidates) > 1:
                raise AmbiguousTargetError(str(recipient_bead_id), candidates)
            recipient = candidates[0]
        now = self.clock()
        normalized_thread = thread_id.strip() if thread_id is not None else None
        if thread_id is not None and not normalized_thread:
            raise CoordinationError("Message thread ID must not be empty.")
        normalized_thread = normalized_thread or f"message:{uuid.uuid4()}"
        should_reply = (
            classification == "action_required"
            if reply_required is None
            else reply_required
        )
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row, inserted = self._insert_message(
                connection,
                sender_session_id=sender_session_id,
                recipient_session_id=recipient["session_id"],
                body=body.strip(),
                classification=classification,
                thread_id=normalized_thread,
                reply_required=should_reply,
                now=now,
            )
        result = self._row_to_message(row)
        result.update(
            {
                "recipient_name": recipient["name"],
                "recipient_bead_id": recipient["bead_id"],
                "idempotent": not inserted,
            }
        )
        return result

    def inbox(
        self,
        session_id: str,
        *,
        include_delivered: bool = False,
        mark_delivered: bool = True,
        unread_only: bool = False,
        classifications: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        self.get_session(session_id)
        if include_delivered and unread_only:
            raise CoordinationError("Unread inbox cannot be combined with all history.")
        clauses = ["messages.recipient_session_id = ?"]
        values: list[Any] = [session_id]
        if unread_only:
            clauses.append("messages.acknowledged_at IS NULL")
        elif not include_delivered:
            clauses.append("messages.delivered_at IS NULL")
        normalized_classifications = (
            sorted(set(classifications)) if classifications is not None else []
        )
        unknown = set(normalized_classifications) - MESSAGE_CLASSIFICATIONS
        if unknown:
            raise CoordinationError(
                "Unknown message classification(s): " + ", ".join(sorted(unknown))
            )
        if normalized_classifications:
            placeholders = ", ".join("?" for _ in normalized_classifications)
            clauses.append(f"messages.classification IN ({placeholders})")
            values.extend(normalized_classifications)
        where = " AND ".join(clauses)
        now = self.clock()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = list(
                connection.execute(
                    f"""
                    SELECT messages.*, sessions.name AS sender_name,
                           sessions.client AS sender_client,
                           sessions.bead_id AS sender_bead_id
                    FROM messages
                    JOIN sessions ON sessions.session_id = messages.sender_session_id
                    WHERE {where}
                    ORDER BY messages.id
                    """,
                    values,
                )
            )
            if mark_delivered and rows:
                connection.executemany(
                    """
                    UPDATE messages
                    SET delivered_at = COALESCE(delivered_at, ?)
                    WHERE id = ?
                    """,
                    [(now, row["id"]) for row in rows],
                )
        return [
            self._row_to_message(row, delivered_at=now if mark_delivered else None)
            for row in rows
        ]

    def inbox_wait(
        self,
        session_id: str,
        *,
        timeout_seconds: float | None = None,
        include_delivered: bool = False,
        mark_delivered: bool = True,
        classifications: Iterable[str] | None = None,
        poll_interval_seconds: float = DEFAULT_INBOX_POLL_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> list[dict[str, Any]]:
        if include_delivered:
            raise CoordinationError(
                "--wait cannot be combined with --all: previously delivered "
                "messages would make the blocking call return immediately."
            )
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise CoordinationError("--timeout must be a positive number of seconds.")
        self.get_session(session_id)
        deadline = None if timeout_seconds is None else self.clock() + timeout_seconds
        while True:
            self.touch(session_id)
            messages = self.inbox(
                session_id,
                include_delivered=False,
                mark_delivered=mark_delivered,
                classifications=classifications,
            )
            if messages:
                return messages
            if deadline is None:
                sleep(poll_interval_seconds)
                continue
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise InboxTimeoutError(session_id, timeout_seconds)
            sleep(min(poll_interval_seconds, remaining))

    def acknowledge(self, session_id: str, message_id: int) -> dict[str, Any]:
        now = self.clock()
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE messages
                SET delivered_at = COALESCE(delivered_at, ?),
                    acknowledged_at = COALESCE(acknowledged_at, ?)
                WHERE id = ? AND recipient_session_id = ?
                """,
                (now, now, message_id, session_id),
            )
            if cursor.rowcount != 1:
                raise CoordinationError(
                    f"Message {message_id} is not addressed to session {session_id}."
                )
            row = connection.execute(
                "SELECT * FROM messages WHERE id = ?", (message_id,)
            ).fetchone()
        assert row is not None
        return {
            "id": row["id"],
            "recipient_session_id": row["recipient_session_id"],
            "acknowledged_at": _iso(row["acknowledged_at"]),
        }

    def acknowledge_all_unread(self, session_id: str) -> dict[str, Any]:
        self.get_session(session_id)
        now = self.clock()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = list(
                connection.execute(
                    """
                    SELECT id FROM messages
                    WHERE recipient_session_id = ? AND acknowledged_at IS NULL
                    ORDER BY id
                    """,
                    (session_id,),
                )
            )
            message_ids = [int(row["id"]) for row in rows]
            if message_ids:
                connection.executemany(
                    """
                    UPDATE messages
                    SET delivered_at = COALESCE(delivered_at, ?),
                        acknowledged_at = COALESCE(acknowledged_at, ?)
                    WHERE id = ? AND recipient_session_id = ?
                    """,
                    [
                        (now, now, message_id, session_id)
                        for message_id in message_ids
                    ],
                )
        return {
            "session_id": session_id,
            "acknowledged": len(message_ids),
            "message_ids": message_ids,
            "acknowledged_at": _iso(now) if message_ids else None,
        }
