"""Effective supervision of app agents, separate from immutable creator provenance.

A create request permanently records who created an app agent. Supervision is
the current responsibility for directing, recovering and receiving reports from
it. It defaults to the creator. An explicit transfer moves selected idle direct
workers to a successor; descendants keep their own supervisors, so a
transferred subtree remains intact without rewriting it.

Two requesters may transfer: the current supervisor handing off its own
workers, or a successor adopting from a supervisor that has durably ended or
been closed. Idle or stale presence is not retirement. Transfer never wakes,
resumes or stops anyone, never clears a deliberate Stop or uncertain-execution
pause, and never delivers queued requests. Its durable notices are
informational, so they neither wake nor reopen a retired conversation.
Identity is the cooperative local session model: this is coordination
authority, not authentication against processes sharing the account.
"""
from __future__ import annotations

import json
import uuid

from .store import CoordinationError
from .workspaces import matches_workspace

MAX_SELECTION = 100


def ensure_schema(db):
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
    """)


def _exists(db, table):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone() is not None


def overrides(db) -> dict:
    """Return explicit supervisors only; read paths never create the schema."""
    if not _exists(db, "agent_supervisors"):
        return {}
    return {row[0]: row[1] for row in db.execute("SELECT thread_id, supervisor_session_id FROM agent_supervisors")}


def override(db, thread_id):
    """The transferred supervisor of one thread, or None when the creator still supervises."""
    if not _exists(db, "agent_supervisors"):
        return None
    row = db.execute("SELECT supervisor_session_id FROM agent_supervisors WHERE thread_id = ?", (thread_id,)).fetchone()
    return row[0] if row else None


def creator(db, thread_id):
    """The completed creator, matching the existing recovery authority rule."""
    if not _exists(db, "app_control_requests"):
        return None
    row = db.execute("""SELECT sender_session_id FROM app_control_requests
        WHERE operation = 'create' AND thread_id = ? AND status = 'completed'
        AND sender_session_id != thread_id ORDER BY created_at, request_id LIMIT 1""", (thread_id,)).fetchone()
    return row[0] if row else None


def effective_supervisor(db, thread_id):
    return override(db, thread_id) or creator(db, thread_id)


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

    Integration seam for replies to assignments sent before a transfer. Any other
    recipient is returned unchanged.
    """
    if retired_supervisor(db, recipient_session_id, sender_session_id):
        return effective_supervisor(db, sender_session_id) or recipient_session_id
    return recipient_session_id


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


def _session(db, session_id, label, workspace):
    row = db.execute("SELECT session_id, cwd, turn_active, ended_at FROM sessions WHERE session_id = ?",
                     (session_id,)).fetchone()
    if row is None or not matches_workspace(row["cwd"], workspace):
        raise CoordinationError(f"{label} {session_id!r} was not found in this workspace.")
    return row


def _blocker(db, thread_id, retired):
    """Describe why a worker is not idle and safe to transfer, or return None."""
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
    if db.execute("""SELECT 1 FROM messages WHERE sender_session_id = ? AND recipient_session_id = ?
            AND delivered_at IS NULL AND classification IN ('action_required', 'deferred') LIMIT 1""",
            (retired, thread_id)).fetchone():
        return "an undelivered request from the retired supervisor is queued"
    return None


def _subscriptions(db, retired, selected):
    """Pending subscriptions owned by the retired supervisor that watch selected workers."""
    if not _exists(db, "event_subscriptions"):
        return []
    moved = []
    for row in db.execute("SELECT id FROM event_subscriptions WHERE owner_session_id = ? AND state = 'pending'",
                          (retired,)).fetchall():
        targets = {item[0] for item in db.execute(
            "SELECT session_id FROM event_subscription_items WHERE subscription_id = ?", (row[0],))}
        if not targets & selected:
            continue
        if not targets <= selected:
            raise CoordinationError(f"Subscription {row[0]} also watches agents outside this selection. "
                                    "Include them, or cancel or let that subscription finish first.")
        moved.append(row[0])
    return moved


def _handover(db, thread_id, retired):
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
        (retired, thread_id))]
    scope = db.execute("SELECT write_scope_json FROM sessions WHERE session_id = ?", (thread_id,)).fetchone()
    return {
        "thread_id": thread_id, "title": title[0] if title else None,
        "checkpoint": dict(checkpoint) if checkpoint else None,
        "paused": bool(wake and wake["paused"]), "pause_kind": wake["pause_kind"] if wake else None,
        "outstanding_reply_message_ids": outstanding,
        "write_scope": json.loads(scope[0]) if scope and scope[0] else [],
    }


def _retired(db, row):
    """Durable retirement: the session ended, or its conversation was closed."""
    if row["ended_at"] is not None:
        return True
    if not _exists(db, "work_threads"):
        return False
    thread = db.execute("SELECT attention FROM work_threads WHERE thread_id = ?", (row["session_id"],)).fetchone()
    return bool(thread) and thread["attention"] == "archived"


def _notices(store, db, *, transfer_id, mode, requester, from_session, to_session, thread_ids, agents, reason, now):
    """Informational receipts for every party except the requester; they never wake or reopen anyone."""
    rerouted = {agent["thread_id"]: agent["outstanding_reply_message_ids"] for agent in agents
                if agent["outstanding_reply_message_ids"]}
    common = {"event": "supervision_transferred", "transfer_id": transfer_id, "mode": mode,
              "from_session_id": from_session, "to_session_id": to_session, "requester_session_id": requester,
              "thread_ids": thread_ids, "reason": reason, "rerouted_reply_message_ids": rerouted}
    guidance = {
        from_session: f"You no longer supervise these agents; {to_session} does. Replies to your earlier "
                      "assignments listed here now reach the successor. No reply is required.",
        to_session: f"You now supervise these agents, which {from_session} supervised. Replies to the listed "
                    "earlier assignments will reach you. Nothing was resumed. No reply is required.",
    }
    recipients = [session for session in (from_session, to_session) if session != requester]
    recipients += [thread_id for thread_id in thread_ids if thread_id != requester]
    from .sidecars import guard_message
    identifiers = []
    for recipient in recipients:
        # A sidecar must not reach its watched agent outside the watch's limits; refusing rolls back the transfer.
        guard_message(db, requester, recipient)
        text = guidance.get(recipient, f"Your supervisor is now {to_session}; report to it. Replies to earlier "
                                       f"assignments from {from_session} reach {to_session}. No action is required.")
        message, _ = store._insert_message(db, sender_session_id=requester, recipient_session_id=recipient,
            body=json.dumps({**common, "guidance": text}, ensure_ascii=True), classification="informational",
            thread_id=f"supervision-transfer:{transfer_id}:{recipient}", reply_required=False, now=now)
        identifiers.append(message["id"])
    return identifiers


def transfer(store, *, requester, from_session, to_session, thread_ids, request_id, reason, workspace=None):
    """Atomically move selected idle direct workers to a successor supervisor.

    The requester is either the current supervisor handing off (requester ==
    from_session), or the successor adopting (requester == to_session) from a
    supervisor that has ended or been closed. Repeating the same request ID
    returns the recorded transfer with a current handover snapshot.
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
                                "adopt them from an ended or closed supervisor.")
    mode = "handoff" if requester == from_session else "adoption"
    selected = set(thread_ids)
    if to_session in selected or from_session in selected:
        raise CoordinationError("A supervisor cannot be one of the transferred agents.")
    with store._connection() as db:
        # executescript commits an open transaction, so prepare the schema first.
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
        if successor["ended_at"] is not None:
            raise CoordinationError("The successor session has ended; register or resume it first.")
        for thread_id in thread_ids:
            _session(db, thread_id, "Agent", workspace)
            supervisor = effective_supervisor(db, thread_id)
            if supervisor != from_session:
                raise CoordinationError(f"Agent {thread_id} is supervised by {supervisor or 'nobody'}, "
                                        f"not {from_session}. Refresh and select its current supervisor.")
        if mode == "adoption":
            if not _retired(db, current):
                raise CoordinationError(f"Supervisor {from_session} has not ended or been closed, so its agents "
                                        "cannot be adopted. Ask it to hand them off, or close it first.")
            if current["turn_active"]:
                raise CoordinationError(f"Supervisor {from_session} still has an active turn; nothing was changed.")
        parents = _parents(db)
        ancestor, seen = to_session, set()
        while ancestor and ancestor not in seen:
            if ancestor in selected:
                raise CoordinationError(f"The successor is a descendant of {ancestor}; adopting it would create a cycle.")
            seen.add(ancestor)
            ancestor = parents.get(ancestor)
        blocked = {thread_id: reason_ for thread_id in thread_ids
                   if (reason_ := _blocker(db, thread_id, from_session))}
        if blocked:
            detail = "; ".join(f"{thread_id}: {why}" for thread_id, why in blocked.items())
            raise CoordinationError(f"Transfer refused until the selected agents are idle and safe ({detail}). "
                                    "Nothing was changed; no agent was interrupted.")
        subscriptions = _subscriptions(db, from_session, selected)
        now, transfer_id = store.clock(), str(uuid.uuid4())
        agents = [_handover(db, thread_id, from_session) for thread_id in thread_ids]
        notices = _notices(store, db, transfer_id=transfer_id, mode=mode, requester=requester,
                           from_session=from_session, to_session=to_session, thread_ids=thread_ids,
                           agents=agents, reason=reason, now=now)
        db.execute("""INSERT INTO supervision_transfers (transfer_id, request_id, from_session_id, to_session_id,
            requester_session_id, thread_ids_json, reason, mode, moved_subscriptions_json, notice_message_ids_json,
            created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (transfer_id, request_id, from_session, to_session, requester, json.dumps(thread_ids), reason, mode,
             json.dumps(subscriptions), json.dumps(notices), now))
        for thread_id in thread_ids:
            db.execute("INSERT INTO supervision_transfer_threads VALUES (?, ?, ?)", (transfer_id, thread_id, from_session))
            db.execute("""INSERT INTO agent_supervisors (thread_id, supervisor_session_id, transfer_id, updated_at)
                VALUES (?, ?, ?, ?) ON CONFLICT(thread_id) DO UPDATE SET
                supervisor_session_id = excluded.supervisor_session_id,
                transfer_id = excluded.transfer_id, updated_at = excluded.updated_at""",
                (thread_id, to_session, transfer_id, now))
        for subscription_id in subscriptions:
            db.execute("UPDATE event_subscriptions SET owner_session_id = ?, cwd = ?, updated_at = ? WHERE id = ?",
                       (to_session, successor["cwd"], now, subscription_id))
        row = db.execute("SELECT * FROM supervision_transfers WHERE transfer_id = ?", (transfer_id,)).fetchone()
        return _result(db, row, replayed=False)


def _result(db, row, *, replayed):
    thread_ids = json.loads(row["thread_ids_json"])
    return {
        "transfer_id": row["transfer_id"], "request_id": row["request_id"],
        "from_session_id": row["from_session_id"], "to_session_id": row["to_session_id"],
        "reason": row["reason"], "mode": row["mode"], "created_at": row["created_at"], "replayed": replayed,
        "notice_message_ids": json.loads(row["notice_message_ids_json"]),
        "moved_subscription_ids": json.loads(row["moved_subscriptions_json"]),
        "agents": [_handover(db, thread_id, row["from_session_id"]) for thread_id in thread_ids],
    }


def history(store, thread_id):
    """Creator provenance, current supervisor and every transfer of one agent."""
    with store._connection() as db:
        records = []
        if _exists(db, "supervision_transfer_threads"):
            records = [dict(row) for row in db.execute("""SELECT t.transfer_id, t.request_id, t.from_session_id,
                t.to_session_id, t.requester_session_id, t.mode, t.reason, t.created_at FROM supervision_transfer_threads s
                JOIN supervision_transfers t ON t.transfer_id = s.transfer_id
                WHERE s.thread_id = ? ORDER BY t.created_at, t.transfer_id""", (thread_id,))]
        return {"thread_id": thread_id, "created_by": creator(db, thread_id),
                "supervisor": effective_supervisor(db, thread_id), "transfers": records}
