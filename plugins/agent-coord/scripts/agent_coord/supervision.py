"""Effective supervision of app agents, separate from immutable creator provenance.

A create request permanently records who created an app agent. Supervision is
the current responsibility for directing, recovering and receiving reports from
it. It defaults to the creator. An explicit transfer moves selected idle direct
workers to a successor; descendants keep their own supervisors, so a
transferred subtree remains intact without rewriting it.

A supervisor is either a session or the user. The user supervises agents whose
supervisor retired without a successor; their reports surface in the app
instead of waking or reopening the retired conversation.

Two sessions may transfer: the current supervisor handing off its own
workers, or a successor adopting from a supervisor that has been closed (or,
for a terminal session, has ended). An app conversation unloaded while idle is
not retired. Workspace roles and retirement succession use the same move,
with the user or the runtime as requester. Moves never wake, resume or stop
anyone, never clear a deliberate Stop or uncertain-execution pause, and never
deliver queued requests. Their durable notices are informational, so they
neither wake nor reopen a retired conversation. Identity is the cooperative
local session model: this is coordination authority, not authentication
against processes sharing the account.
"""
from __future__ import annotations

import json
import sqlite3
import uuid

from .store import CoordinationError
from .workspaces import matches_workspace

MAX_SELECTION = 100
SESSION, USER, RUNTIME = "session", "user", "runtime"


def ensure_schema(db):
    """Additive schema; call outside a transaction because executescript commits."""
    db.executescript("""
        CREATE TABLE IF NOT EXISTS agent_supervisors (
            thread_id TEXT PRIMARY KEY REFERENCES sessions(session_id),
            supervisor_session_id TEXT NOT NULL REFERENCES sessions(session_id),
            transfer_id TEXT NOT NULL, updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS supervision_transfers (
            transfer_id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
            from_session_id TEXT NOT NULL REFERENCES sessions(session_id),
            to_session_id TEXT NOT NULL REFERENCES sessions(session_id),
            requester_session_id TEXT NOT NULL REFERENCES sessions(session_id),
            thread_ids_json TEXT NOT NULL, reason TEXT NOT NULL,
            mode TEXT NOT NULL, moved_subscriptions_json TEXT NOT NULL,
            notice_message_ids_json TEXT NOT NULL, created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS supervision_transfer_threads (
            transfer_id TEXT NOT NULL REFERENCES supervision_transfers(transfer_id),
            thread_id TEXT NOT NULL, from_session_id TEXT NOT NULL,
            PRIMARY KEY(transfer_id, thread_id)
        );
        CREATE INDEX IF NOT EXISTS supervision_transfer_threads_retired
            ON supervision_transfer_threads(thread_id, from_session_id);
        CREATE TABLE IF NOT EXISTS supervisor_retirements (
            session_id TEXT PRIMARY KEY, retired_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS supervision_pending_moves (
            thread_id TEXT PRIMARY KEY, from_kind TEXT NOT NULL, from_session_id TEXT,
            to_kind TEXT NOT NULL, to_session_id TEXT, role_id TEXT, mode TEXT NOT NULL,
            reason TEXT NOT NULL, blocker TEXT, created_at REAL NOT NULL
        );
    """)
    # The user and the runtime are not sessions. Kind columns qualify the
    # session columns, which then hold the nearest session for history.
    for table, column, definition in (
            ("agent_supervisors", "supervisor_kind", "TEXT NOT NULL DEFAULT 'session'"),
            ("agent_supervisors", "role_id", "TEXT"),
            ("supervision_transfers", "from_kind", "TEXT NOT NULL DEFAULT 'session'"),
            ("supervision_transfers", "to_kind", "TEXT NOT NULL DEFAULT 'session'"),
            ("supervision_transfers", "requester_kind", "TEXT NOT NULL DEFAULT 'session'"),
            ("supervision_transfers", "role_id", "TEXT")):
        if column not in _columns(db, table):
            try:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc):
                    raise


def _exists(db, table):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone() is not None


def _columns(db, table):
    return {row[1] for row in db.execute(f"PRAGMA table_info({table})")}


def _row(db, thread_id):
    """The explicit supervision record of one thread, or None."""
    if not _exists(db, "agent_supervisors"):
        return None
    kind = "supervisor_kind" if "supervisor_kind" in _columns(db, "agent_supervisors") else "'session'"
    role = "role_id" if "role_id" in _columns(db, "agent_supervisors") else "NULL"
    row = db.execute(f"""SELECT supervisor_session_id, {kind} AS kind, {role} AS role_id
        FROM agent_supervisors WHERE thread_id = ?""", (thread_id,)).fetchone()
    return None if row is None else {"session": row[0], "kind": row[1], "role_id": row[2]}


def overrides(db) -> dict:
    """Explicit supervisors only; the user is None. Read paths never create the schema."""
    if not _exists(db, "agent_supervisors"):
        return {}
    kind = "supervisor_kind" if "supervisor_kind" in _columns(db, "agent_supervisors") else "'session'"
    return {row[0]: (row[1] if row[2] == SESSION else None) for row in
            db.execute(f"SELECT thread_id, supervisor_session_id, {kind} FROM agent_supervisors")}


def override(db, thread_id):
    """The transferred supervising session of one thread; None for the creator or the user."""
    row = _row(db, thread_id)
    return row["session"] if row and row["kind"] == SESSION else None


def supervised_by_user(db, thread_id) -> bool:
    row = _row(db, thread_id)
    return bool(row) and row["kind"] == USER


def creator(db, thread_id):
    """The completed creator, matching the existing recovery authority rule."""
    if not _exists(db, "app_control_requests"):
        return None
    row = db.execute("""SELECT sender_session_id FROM app_control_requests
        WHERE operation = 'create' AND thread_id = ? AND status = 'completed'
        AND sender_session_id != thread_id ORDER BY created_at, request_id LIMIT 1""", (thread_id,)).fetchone()
    return row[0] if row else None


def effective_supervisor(db, thread_id):
    """The supervising session; None when the user supervises or nobody does."""
    row = _row(db, thread_id)
    if row is not None:
        return row["session"] if row["kind"] == SESSION else None
    return creator(db, thread_id)


def authorizes(db, session_id, thread_id) -> bool:
    """Integration seam for recovery, failure reports and interactive control."""
    return session_id is not None and effective_supervisor(db, thread_id) == session_id


def retired_supervisor(db, session_id, thread_id) -> bool:
    """True when a transfer moved this thread away from the session, which no longer supervises it."""
    if not _exists(db, "supervision_transfer_threads"):
        return False
    moved = db.execute("""SELECT 1 FROM supervision_transfer_threads
        WHERE thread_id = ? AND from_session_id = ? LIMIT 1""", (thread_id, session_id)).fetchone()
    return bool(moved) and effective_supervisor(db, thread_id) != session_id


def report_recipient(db, sender_session_id, recipient_session_id):
    """Route a worker's report addressed to a retired supervisor to the current one.

    Integration seam for replies to assignments sent before a transfer. When the
    user supervises the worker, the retired recipient is kept; the caller makes
    that report informational so it cannot wake or reopen the retired conversation.
    """
    if retired_supervisor(db, recipient_session_id, sender_session_id):
        return effective_supervisor(db, sender_session_id) or recipient_session_id
    return recipient_session_id


def relations(db) -> dict:
    """One batched read for display: explicit supervisors and role holders."""
    roles = {}
    if _exists(db, "workspace_roles"):
        roles = {row[0]: {"role_id": row[1], "name": row[2]} for row in db.execute(
            "SELECT holder_session_id, role_id, name FROM workspace_roles WHERE holder_session_id IS NOT NULL")}
    return {"overrides": overrides(db), "roles": roles}


def display_fields(thread_id, created_by, related) -> dict:
    """Supervision fields of a thread payload; the creator remains separate history."""
    explicit = related["overrides"]
    if thread_id in explicit:
        supervisor = explicit[thread_id]
    else:
        supervisor = created_by["thread_id"] if created_by and created_by.get("mode") == "orchestration" else None
    return {"supervisor_thread_id": supervisor,
            "supervised_by_user": thread_id in explicit and explicit[thread_id] is None,
            "role": related["roles"].get(thread_id)}


def is_browser_session(db, session_id) -> bool:
    return _exists(db, "browser_sessions") and db.execute(
        "SELECT 1 FROM browser_sessions WHERE thread_id = ?", (session_id,)).fetchone() is not None


def _archived(db, session_id) -> bool:
    if not _exists(db, "work_threads"):
        return False
    thread = db.execute("SELECT attention FROM work_threads WHERE thread_id = ?", (session_id,)).fetchone()
    return bool(thread) and thread["attention"] == "archived"


def retired(db, session_id) -> bool:
    """Durable retirement: a closed conversation, or a terminal session that ended.

    The runtime also ends an app conversation's session when it unloads it while
    idle; opening resumes it, so that is not retirement.
    """
    if _archived(db, session_id):
        return True
    row = db.execute("SELECT ended_at FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
    return bool(row) and row["ended_at"] is not None and not is_browser_session(db, session_id)


def record_retirement(db, session_id, now):
    """Integration seam for closure and session end; the succession scan does the work.

    Runs inside the caller's transaction, so it uses execute, never executescript.
    """
    db.execute("""CREATE TABLE IF NOT EXISTS supervisor_retirements (
        session_id TEXT PRIMARY KEY, retired_at REAL NOT NULL)""")
    db.execute("INSERT OR IGNORE INTO supervisor_retirements (session_id, retired_at) VALUES (?, ?)",
               (session_id, now))


def passing_from(db, supervisor, thread_id) -> bool:
    """True while a closed supervisor's worker is passing, or has passed, to someone else.

    Reports from that worker to the closed supervisor must not wake or reopen it.
    """
    if not retired(db, supervisor):
        return False
    if effective_supervisor(db, thread_id) == supervisor and not supervised_by_user(db, thread_id):
        return True  # Closed but not yet scanned: succession will pass this worker on.
    if _exists(db, "supervision_pending_moves") and db.execute("""SELECT 1 FROM supervision_pending_moves
            WHERE thread_id = ? AND from_session_id = ? AND from_kind = 'session'""", (thread_id, supervisor)).fetchone():
        return True
    return supervised_by_user(db, thread_id) and retired_supervisor(db, supervisor, thread_id)


def supervised_threads(db, supervisor):
    """Open threads that the session currently supervises directly; the user is None."""
    found = []
    if _exists(db, "agent_supervisors"):
        kind = "supervisor_kind" if "supervisor_kind" in _columns(db, "agent_supervisors") else "'session'"
        if supervisor is None:
            found = [row[0] for row in db.execute(f"SELECT thread_id FROM agent_supervisors WHERE {kind} = 'user'")]
        else:
            found = [row[0] for row in db.execute(f"""SELECT thread_id FROM agent_supervisors
                WHERE supervisor_session_id = ? AND {kind} = 'session'""", (supervisor,))]
    if supervisor is not None and _exists(db, "app_control_requests"):
        explicit = overrides(db)
        for row in db.execute("""SELECT DISTINCT thread_id FROM app_control_requests WHERE operation = 'create'
                AND sender_session_id = ? AND status = 'completed' AND thread_id IS NOT NULL
                AND thread_id != sender_session_id""", (supervisor,)):
            if row[0] not in explicit and creator(db, row[0]) == supervisor:
                found.append(row[0])
    return sorted(thread for thread in set(found) if not _archived(db, thread))


def _parents(db):
    """Effective parent graph for cycle checks: every creation, delegates, then overrides."""
    parents = {}
    if _exists(db, "app_control_requests"):
        for row in db.execute("""SELECT thread_id, sender_session_id FROM app_control_requests
                WHERE operation = 'create' AND thread_id IS NOT NULL
                AND status IN ('running', 'completed', 'uncertain') ORDER BY created_at, request_id"""):
            if row[0] != row[1]:
                parents.setdefault(row[0], row[1])
    if _exists(db, "delegations"):
        for row in db.execute("""SELECT parent_session_id, child_session_id FROM delegations
                WHERE child_session_id IS NOT NULL ORDER BY created_at, delegation_id"""):
            parents.setdefault(row[1], row[0])
    parents.update(overrides(db))
    return parents


def descends_from(db, session_id, thread_ids) -> str | None:
    """The selected thread that is the session or one of its ancestors, if any."""
    parents, ancestor, seen = _parents(db), session_id, set()
    while ancestor and ancestor not in seen:
        if ancestor in thread_ids:
            return ancestor
        seen.add(ancestor)
        ancestor = parents.get(ancestor)
    return None


def _session(db, session_id, label, workspace):
    row = db.execute("SELECT session_id, cwd, turn_active, ended_at FROM sessions WHERE session_id = ?",
                     (session_id,)).fetchone()
    if row is None or not matches_workspace(row["cwd"], workspace):
        raise CoordinationError(f"{label} {session_id!r} was not found in this workspace.")
    return row


def blocker(db, thread_id, retired_session):
    """Describe why a worker is not idle and safe to move, or return None."""
    if db.execute("SELECT turn_active FROM sessions WHERE session_id = ?", (thread_id,)).fetchone()[0]:
        return "a turn is active"
    if _exists(db, "browser_inbox_wake"):
        wake = db.execute("SELECT dispatching FROM browser_inbox_wake WHERE thread_id = ?", (thread_id,)).fetchone()
        if wake and wake["dispatching"]:
            return "an inbox wake is dispatching"
    if _exists(db, "browser_message_queue") and db.execute(
            "SELECT 1 FROM browser_message_queue WHERE thread_id = ? LIMIT 1", (thread_id,)).fetchone():
        return "user input is queued"
    if _exists(db, "thread_close_requests") and db.execute(
            "SELECT 1 FROM thread_close_requests WHERE thread_id = ? AND status IN ('queued', 'closing')",
            (thread_id,)).fetchone():
        return "a close request is pending"
    if retired_session and db.execute("""SELECT 1 FROM messages WHERE sender_session_id = ? AND recipient_session_id = ?
            AND delivered_at IS NULL AND classification IN ('action_required', 'deferred') LIMIT 1""",
            (retired_session, thread_id)).fetchone():
        return "an undelivered request from the retired supervisor is queued"
    return None


def _subscriptions(db, retired_session, selected, *, strict):
    """Pending subscriptions owned by the retired supervisor that watch only selected workers."""
    if not retired_session or not _exists(db, "event_subscriptions"):
        return []
    moved = []
    for row in db.execute("SELECT id FROM event_subscriptions WHERE owner_session_id = ? AND state = 'pending'",
                          (retired_session,)).fetchall():
        targets = {item[0] for item in db.execute(
            "SELECT session_id FROM event_subscription_items WHERE subscription_id = ?", (row[0],))}
        if not targets & selected:
            continue
        if not targets <= selected:
            if strict:
                raise CoordinationError(f"Subscription {row[0]} also watches agents outside this selection. "
                                        "Include them, or cancel or let that subscription finish first.")
            continue
        moved.append(row[0])
    return moved


def _handover(db, thread_id, retired_session):
    title = db.execute("SELECT title FROM work_threads WHERE thread_id = ?", (thread_id,)).fetchone() \
        if _exists(db, "work_threads") else None
    checkpoint = db.execute("""SELECT phase, summary, next_action, next_actor, created_at FROM thread_checkpoints
        WHERE thread_id = ? ORDER BY id DESC LIMIT 1""", (thread_id,)).fetchone() \
        if _exists(db, "thread_checkpoints") else None
    wake = db.execute("SELECT paused, pause_kind FROM browser_inbox_wake WHERE thread_id = ?", (thread_id,)).fetchone() \
        if _exists(db, "browser_inbox_wake") else None
    outstanding = [row[0] for row in db.execute("""SELECT m.id FROM messages m
        WHERE m.sender_session_id = ? AND m.recipient_session_id = ? AND m.reply_required = 1
        AND NOT EXISTS (SELECT 1 FROM messages r WHERE r.in_reply_to = m.id) ORDER BY m.id""",
        (retired_session, thread_id))] if retired_session else []
    scope = db.execute("SELECT write_scope_json FROM sessions WHERE session_id = ?", (thread_id,)).fetchone()
    return {
        "thread_id": thread_id, "title": title[0] if title else None,
        "checkpoint": dict(checkpoint) if checkpoint else None,
        "paused": bool(wake and wake["paused"]), "pause_kind": wake["pause_kind"] if wake else None,
        "outstanding_reply_message_ids": outstanding,
        "write_scope": json.loads(scope[0]) if scope and scope[0] else [],
    }


def _label(session_id):
    return session_id or "the user"


def _notices(store, db, *, transfer_id, mode, requester, requester_kind, source, target, thread_ids, agents,
             reason, now):
    """Informational receipts for every party except the requester; they never wake or reopen anyone."""
    rerouted = {agent["thread_id"]: agent["outstanding_reply_message_ids"] for agent in agents
                if agent["outstanding_reply_message_ids"]}
    common = {"event": "supervision_transferred", "transfer_id": transfer_id, "mode": mode,
              "from_session_id": source, "to_session_id": target, "requester_session_id": requester,
              "requester_kind": requester_kind, "thread_ids": thread_ids, "reason": reason,
              "rerouted_reply_message_ids": rerouted}
    guidance = {}
    if source:
        guidance[source] = (f"You no longer supervise these agents; {_label(target)} does. Replies to your earlier "
                            "assignments listed here no longer reach you. No reply is required.")
    if target:
        guidance[target] = (f"You now supervise these agents, which {_label(source)} supervised. Replies to the "
                            "listed earlier assignments will reach you. Nothing was resumed. No reply is required.")
    acting = requester if requester_kind == SESSION else None
    recipients = [session for session in (source, target) if session and session != acting]
    recipients += [thread_id for thread_id in thread_ids if thread_id != acting]
    from .sidecars import guard_message
    identifiers = []
    for recipient in recipients:
        # The user and the runtime are not sessions, so their notices are self-addressed records.
        sender = requester if requester_kind == SESSION else recipient
        # A sidecar must not reach its watched agent outside the watch's limits; refusing rolls back the move.
        guard_message(db, sender, recipient)
        text = guidance.get(recipient, f"Your supervisor is now {_label(target)}; report to it. Replies to earlier "
                                       f"assignments from {_label(source)} follow the new supervisor. "
                                       "No action is required.")
        message, _ = store._insert_message(db, sender_session_id=sender, recipient_session_id=recipient,
            body=json.dumps({**common, "guidance": text}, ensure_ascii=True), classification="informational",
            thread_id=f"supervision-transfer:{transfer_id}:{recipient}", reply_required=False, now=now)
        identifiers.append(message["id"])
    return identifiers


def move(store, db, *, source, target, thread_ids, mode, requester, requester_kind, reason, request_id,
         role_id=None, strict=True, workspace=None):
    """Move threads from one supervisor to another inside the caller's write transaction.

    source and target are sessions, or None for the user. The caller has checked
    authority. Every selected thread must be supervised by source and be idle and
    safe; otherwise nothing changes. Subscriptions the retired supervisor owns
    that watch only moved threads follow a session target and are cancelled for
    the user, whose app shows the result directly.
    """
    selected = set(thread_ids)
    if source is not None and source == target:
        raise CoordinationError("The successor must differ from the current supervisor.")
    if (source and source in selected) or (target and target in selected):
        raise CoordinationError("A supervisor cannot be one of the transferred agents.")
    nearest = {}
    for thread_id in thread_ids:
        _session(db, thread_id, "Agent", workspace)
        record = _row(db, thread_id)
        if source is None:
            if not supervised_by_user(db, thread_id):
                raise CoordinationError(f"Agent {thread_id} is not supervised by the user.")
            nearest[thread_id] = record["session"]
        else:
            supervisor = effective_supervisor(db, thread_id)
            if supervisor != source or supervised_by_user(db, thread_id):
                raise CoordinationError(f"Agent {thread_id} is supervised by {supervisor or 'the user or nobody'}, "
                                        f"not {source}. Refresh and select its current supervisor.")
            nearest[thread_id] = source
    if target is not None:
        cycle = descends_from(db, target, selected)
        if cycle:
            raise CoordinationError(f"The successor is a descendant of {cycle}; adopting it would create a cycle.")
    blocked = {thread_id: why for thread_id in thread_ids if (why := blocker(db, thread_id, source))}
    if blocked:
        detail = "; ".join(f"{thread_id}: {why}" for thread_id, why in blocked.items())
        raise CoordinationError(f"Transfer refused until the selected agents are idle and safe ({detail}). "
                                "Nothing was changed; no agent was interrupted.")
    subscriptions = _subscriptions(db, source, selected, strict=strict)
    now, transfer_id = store.clock(), str(uuid.uuid4())
    agents = [_handover(db, thread_id, source) for thread_id in thread_ids]
    notices = _notices(store, db, transfer_id=transfer_id, mode=mode, requester=requester,
                       requester_kind=requester_kind, source=source, target=target, thread_ids=thread_ids,
                       agents=agents, reason=reason, now=now)
    first = nearest[thread_ids[0]]
    # Session columns are required; for the user or runtime they hold the nearest session.
    db.execute("""INSERT INTO supervision_transfers (transfer_id, request_id, from_session_id, to_session_id,
        requester_session_id, thread_ids_json, reason, mode, moved_subscriptions_json, notice_message_ids_json,
        created_at, from_kind, to_kind, requester_kind, role_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (transfer_id, request_id, first, target or first, requester if requester_kind == SESSION else (target or first),
         json.dumps(thread_ids), reason, mode, json.dumps(subscriptions), json.dumps(notices), now,
         SESSION if source else USER, SESSION if target else USER, requester_kind, role_id))
    for thread_id in thread_ids:
        db.execute("INSERT INTO supervision_transfer_threads VALUES (?, ?, ?)", (transfer_id, thread_id, nearest[thread_id]))
        db.execute("""INSERT INTO agent_supervisors (thread_id, supervisor_session_id, transfer_id, updated_at,
            supervisor_kind, role_id) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(thread_id) DO UPDATE SET
            supervisor_session_id = excluded.supervisor_session_id, transfer_id = excluded.transfer_id,
            updated_at = excluded.updated_at, supervisor_kind = excluded.supervisor_kind, role_id = excluded.role_id""",
            (thread_id, target or nearest[thread_id], transfer_id, now, SESSION if target else USER, role_id))
        if _exists(db, "supervision_pending_moves"):
            db.execute("DELETE FROM supervision_pending_moves WHERE thread_id = ?", (thread_id,))
    for subscription_id in subscriptions:
        if target:
            owner = db.execute("SELECT cwd FROM sessions WHERE session_id = ?", (target,)).fetchone()
            db.execute("UPDATE event_subscriptions SET owner_session_id = ?, cwd = ?, updated_at = ? WHERE id = ?",
                       (target, owner["cwd"], now, subscription_id))
        else:
            db.execute("UPDATE event_subscriptions SET state = 'cancelled', updated_at = ? WHERE id = ?",
                       (now, subscription_id))
    return db.execute("SELECT * FROM supervision_transfers WHERE transfer_id = ?", (transfer_id,)).fetchone()


def transfer(store, *, requester, from_session, to_session, thread_ids, request_id, reason, workspace=None):
    """Atomically move selected idle direct workers to a successor supervisor.

    The requester is either the current supervisor handing off (requester ==
    from_session), or the successor adopting (requester == to_session) from a
    supervisor that has retired. Repeating the same request ID returns the
    recorded transfer with a current handover snapshot.
    """
    for value, label in ((request_id, "Request ID"), (reason, "Reason")):
        if not isinstance(value, str) or not value.strip() or len(value) > 2000 or "\0" in value:
            raise CoordinationError(f"{label} must be nonempty text of at most 2000 characters.")
    if (not isinstance(thread_ids, list) or not thread_ids or len(thread_ids) > MAX_SELECTION
            or any(not isinstance(item, str) or not item for item in thread_ids) or len(set(thread_ids)) != len(thread_ids)):
        raise CoordinationError(f"Select between 1 and {MAX_SELECTION} distinct agent threads.")
    if from_session == to_session:
        raise CoordinationError("The successor must differ from the current supervisor.")
    if requester not in (from_session, to_session):
        raise CoordinationError("Only the current supervisor can hand off its agents, and only a successor can "
                                "adopt them from a closed or ended supervisor.")
    mode = "handoff" if requester == from_session else "adoption"
    selected = set(thread_ids)
    if to_session in selected or from_session in selected:
        raise CoordinationError("A supervisor cannot be one of the transferred agents.")
    with store._connection() as db:
        ensure_schema(db)
    with store._connection() as db:
        db.execute("BEGIN IMMEDIATE")
        previous = db.execute("SELECT * FROM supervision_transfers WHERE request_id = ?", (request_id,)).fetchone()
        if previous is not None:
            if (previous["from_session_id"], previous["to_session_id"], previous["requester_session_id"],
                    sorted(json.loads(previous["thread_ids_json"]))) != (from_session, to_session, requester, sorted(thread_ids)):
                raise CoordinationError(f"Request ID {request_id!r} was already used for a different transfer.")
            return _result(db, previous, replayed=True)
        current = _session(db, from_session, "Current supervisor", workspace)
        successor = _session(db, to_session, "Successor", workspace)
        if successor["ended_at"] is not None and not is_browser_session(db, to_session):
            raise CoordinationError("The successor session has ended; register or resume it first.")
        if retired(db, to_session):
            raise CoordinationError("The successor conversation is closed; reopen it first.")
        for thread_id in thread_ids:
            _session(db, thread_id, "Agent", workspace)
            supervisor = effective_supervisor(db, thread_id)
            if supervisor != from_session:
                raise CoordinationError(f"Agent {thread_id} is supervised by {supervisor or 'the user or nobody'}, "
                                        f"not {from_session}. Refresh and select its current supervisor.")
        if mode == "adoption":
            if not retired(db, from_session):
                raise CoordinationError(f"Supervisor {from_session} has not ended or been closed, so its agents "
                                        "cannot be adopted. Ask it to hand them off, or close it first.")
            if current["turn_active"]:
                raise CoordinationError(f"Supervisor {from_session} still has an active turn; nothing was changed.")
        row = move(store, db, source=from_session, target=to_session, thread_ids=thread_ids, mode=mode,
                   requester=requester, requester_kind=SESSION, reason=reason, request_id=request_id,
                   workspace=workspace)
        return _result(db, row, replayed=False)


def _result(db, row, *, replayed):
    thread_ids = json.loads(row["thread_ids_json"])
    keys = row.keys()
    from_kind = row["from_kind"] if "from_kind" in keys else SESSION
    to_kind = row["to_kind"] if "to_kind" in keys else SESSION
    source = row["from_session_id"] if from_kind == SESSION else None
    return {
        "transfer_id": row["transfer_id"], "request_id": row["request_id"],
        "from_session_id": source, "to_session_id": row["to_session_id"] if to_kind == SESSION else None,
        "from_kind": from_kind, "to_kind": to_kind,
        "requester_kind": row["requester_kind"] if "requester_kind" in keys else SESSION,
        "role_id": row["role_id"] if "role_id" in keys else None,
        "reason": row["reason"], "mode": row["mode"], "created_at": row["created_at"], "replayed": replayed,
        "notice_message_ids": json.loads(row["notice_message_ids_json"]),
        "moved_subscription_ids": json.loads(row["moved_subscriptions_json"]),
        "agents": [_handover(db, thread_id, source) for thread_id in thread_ids],
    }


def history(store, thread_id):
    """Creator provenance, current supervisor and every transfer of one agent."""
    with store._connection() as db:
        records = []
        if _exists(db, "supervision_transfer_threads"):
            columns = _columns(db, "supervision_transfers")
            kinds = ", ".join(f"t.{name}" for name in ("from_kind", "to_kind", "requester_kind", "role_id")
                              if name in columns)
            records = [dict(row) for row in db.execute(f"""SELECT t.transfer_id, t.request_id, t.from_session_id,
                t.to_session_id, t.requester_session_id, t.mode, t.reason, t.created_at{', ' + kinds if kinds else ''}
                FROM supervision_transfer_threads s JOIN supervision_transfers t ON t.transfer_id = s.transfer_id
                WHERE s.thread_id = ? ORDER BY t.created_at, t.transfer_id""", (thread_id,))]
        record = _row(db, thread_id)
        return {"thread_id": thread_id, "created_by": creator(db, thread_id),
                "supervisor": effective_supervisor(db, thread_id),
                "supervised_by_user": supervised_by_user(db, thread_id),
                "role_id": record["role_id"] if record else None, "transfers": records}
