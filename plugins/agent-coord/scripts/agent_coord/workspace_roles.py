"""Workspace roles and supervisor succession.

A role is a user-named position in a workspace, such as a lead, held by at most
one session. A session holds at most one role per workspace, so the agents it
supervises are that role's agents. Appointing a holder moves the previous
holder's agents to it: idle agents immediately, busy agents when they become
idle and safe. When a supervisor is closed (or a terminal supervisor ends), its
open agents pass to the next holder of its role, or to the user when there is
none. Agents supervised by the user for a role move to its next holder.

The user appoints any holder; a session can create a role for itself and hand
its own role to another session. Nothing here wakes, resumes or stops an agent.
"""
from __future__ import annotations

import logging
import uuid

from . import supervision
from .store import CoordinationError
from .supervision import RUNTIME, SESSION, USER
from .workspaces import matches_workspace

MAX_ROLE_NAME = 80


def ensure_schema(db):
    supervision.ensure_schema(db)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS workspace_roles (
            role_id TEXT PRIMARY KEY, workspace TEXT NOT NULL, name TEXT NOT NULL COLLATE NOCASE,
            holder_session_id TEXT REFERENCES sessions(session_id),
            created_at REAL NOT NULL, updated_at REAL NOT NULL, UNIQUE(workspace, name)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS workspace_roles_holder
            ON workspace_roles(holder_session_id) WHERE holder_session_id IS NOT NULL;
        CREATE TABLE IF NOT EXISTS workspace_role_appointments (
            id INTEGER PRIMARY KEY AUTOINCREMENT, role_id TEXT NOT NULL REFERENCES workspace_roles(role_id),
            from_session_id TEXT, to_session_id TEXT, requester_kind TEXT NOT NULL,
            requester_session_id TEXT, reason TEXT NOT NULL, created_at REAL NOT NULL
        );
    """)


def _prepare(store):
    # Schema preparation commits and rereads the catalog; do it once per store.
    if getattr(store, "_workspace_roles_prepared", False):
        return
    with store._connection() as db:
        ensure_schema(db)
    store._workspace_roles_prepared = True


def _name(value):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > MAX_ROLE_NAME or "\0" in value:
        raise CoordinationError(f"A role name must contain 1–{MAX_ROLE_NAME} characters.")
    return " ".join(value.split())


def _describe(db, row):
    holder = row["holder_session_id"]
    title = db.execute("SELECT title FROM work_threads WHERE thread_id = ?", (holder,)).fetchone() if holder else None
    return {"role_id": row["role_id"], "workspace": row["workspace"], "name": row["name"],
            "holder_session_id": holder, "holder_title": title[0] if title else None,
            "agent_count": len(supervision.supervised_threads(db, holder)) if holder else 0,
            "user_supervised_count": db.execute("""SELECT COUNT(*) FROM agent_supervisors
                WHERE supervisor_kind = 'user' AND role_id = ?""", (row["role_id"],)).fetchone()[0],
            "pending_move_count": db.execute("SELECT COUNT(*) FROM supervision_pending_moves WHERE role_id = ?",
                                             (row["role_id"],)).fetchone()[0],
            "created_at": row["created_at"], "updated_at": row["updated_at"]}


def _visible(role_workspace, workspace):
    """A role belongs to a workspace that contains, or is contained by, the caller's."""
    return workspace is None or matches_workspace(role_workspace, workspace) or matches_workspace(workspace, role_workspace)


def _role(db, role_id, workspace):
    row = db.execute("SELECT * FROM workspace_roles WHERE role_id = ?", (role_id,)).fetchone()
    if row is None or not _visible(row["workspace"], workspace):
        raise CoordinationError("Role not found in this workspace.")
    return row


def held_role(db, session_id):
    """The role a session holds, or None; read paths never create the schema."""
    if not supervision._exists(db, "workspace_roles"):
        return None
    row = db.execute("SELECT role_id, name FROM workspace_roles WHERE holder_session_id = ?", (session_id,)).fetchone()
    return {"role_id": row["role_id"], "name": row["name"]} if row else None


def list_roles(store, workspace):
    _prepare(store)
    with store._connection() as db:
        rows = db.execute("SELECT * FROM workspace_roles ORDER BY name COLLATE NOCASE, role_id").fetchall()
        return {"data": [_describe(db, row) for row in rows if _visible(row["workspace"], workspace)]}


def create_role(store, *, workspace, name, requester_kind, requester_session=None):
    """Create a vacant role; a session creating one for itself becomes its holder."""
    name = _name(name)
    if requester_kind not in {USER, SESSION}:
        raise CoordinationError("Roles are created by the user or by a session for itself.")
    _prepare(store)
    with store._connection() as db:
        db.execute("BEGIN IMMEDIATE")
        for row in db.execute("SELECT workspace FROM workspace_roles WHERE name = ?", (name,)):
            if _visible(row["workspace"], workspace):
                raise CoordinationError(f"A role named {name!r} already exists in this workspace.")
        now, role_id = store.clock(), str(uuid.uuid4())
        db.execute("""INSERT INTO workspace_roles (role_id, workspace, name, holder_session_id, created_at, updated_at)
            VALUES (?, ?, ?, NULL, ?, ?)""", (role_id, workspace, name, now, now))
        if requester_kind == SESSION:
            _appoint(store, db, role_id=role_id, holder=requester_session, requester_kind=SESSION,
                     requester_session=requester_session, reason="Created the role for itself.", workspace=workspace)
        return _describe(db, db.execute("SELECT * FROM workspace_roles WHERE role_id = ?", (role_id,)).fetchone())


def appoint(store, *, role_id, holder, requester_kind, requester_session=None, reason=None, workspace=None):
    """Make a session the holder of a role, or vacate it with holder None.

    The user may appoint anyone. A session may hand its own role to another live
    session or vacate it. Agents of the previous holder move as idle as possible.
    """
    if requester_kind not in {USER, SESSION}:
        raise CoordinationError("Only the user or the current holder can appoint a role holder.")
    reason = reason or ("Appointed by the user." if requester_kind == USER else "Handed off by the previous holder.")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
        raise CoordinationError("Reason must be nonempty text of at most 2000 characters.")
    _prepare(store)
    with store._connection() as db:
        db.execute("BEGIN IMMEDIATE")
        role = _role(db, role_id, workspace)
        if requester_kind == SESSION and requester_session != role["holder_session_id"]:
            raise CoordinationError("Only the user or the role's current holder can change its holder.")
        result = _appoint(store, db, role_id=role_id, holder=holder, requester_kind=requester_kind,
                          requester_session=requester_session, reason=reason, workspace=workspace)
        return {**_describe(db, db.execute("SELECT * FROM workspace_roles WHERE role_id = ?", (role_id,)).fetchone()),
                **result}


def delete_role(store, *, role_id, workspace=None):
    """User-only: vacate the role first; its user-supervised agents stay with the user."""
    _prepare(store)
    with store._connection() as db:
        db.execute("BEGIN IMMEDIATE")
        role = _role(db, role_id, workspace)
        if role["holder_session_id"]:
            raise CoordinationError("Vacate the role before deleting it.")
        db.execute("UPDATE agent_supervisors SET role_id = NULL WHERE role_id = ?", (role_id,))
        db.execute("UPDATE supervision_pending_moves SET role_id = NULL WHERE role_id = ?", (role_id,))
        db.execute("DELETE FROM workspace_role_appointments WHERE role_id = ?", (role_id,))
        db.execute("DELETE FROM workspace_roles WHERE role_id = ?", (role_id,))
        return {"role_id": role_id, "deleted": True}


def _appoint(store, db, *, role_id, holder, requester_kind, requester_session, reason, workspace):
    role = db.execute("SELECT * FROM workspace_roles WHERE role_id = ?", (role_id,)).fetchone()
    previous = role["holder_session_id"]
    if holder == previous:
        raise CoordinationError("That session already holds this role." if holder else "The role is already vacant.")
    if holder is not None:
        supervision._session(db, holder, "Holder", workspace)
        if supervision.retired(db, holder):
            raise CoordinationError("A closed or ended session cannot hold a role.")
    now = store.clock()
    other = db.execute("SELECT role_id FROM workspace_roles WHERE holder_session_id = ?", (holder,)).fetchone() \
        if holder is not None else None
    if other and not (requester_kind == USER or holder == requester_session):
        raise CoordinationError("That session already holds another role; only the user or that session can change it.")
    if other:
        # A session holds one role. Its agents stay with it and now belong to the new role.
        db.execute("UPDATE workspace_roles SET holder_session_id = NULL, updated_at = ? WHERE role_id = ?",
                   (now, other["role_id"]))
        db.execute("""INSERT INTO workspace_role_appointments (role_id, from_session_id, to_session_id, requester_kind,
            requester_session_id, reason, created_at) VALUES (?, ?, NULL, ?, ?, ?, ?)""",
            (other["role_id"], holder, requester_kind, requester_session, "The holder took another role.", now))
    db.execute("UPDATE workspace_roles SET holder_session_id = ?, updated_at = ? WHERE role_id = ?", (holder, now, role_id))
    db.execute("""INSERT INTO workspace_role_appointments (role_id, from_session_id, to_session_id, requester_kind,
        requester_session_id, reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (role_id, previous, holder, requester_kind, requester_session, reason, now))
    agents = []
    if previous:
        agents += [(thread_id, previous) for thread_id in supervision.supervised_threads(db, previous)]
    if holder is not None:
        # Vacating leaves agents that already wait with the user for this role where they are.
        agents += [(row[0], None) for row in db.execute("""SELECT thread_id FROM agent_supervisors
            WHERE supervisor_kind = 'user' AND role_id = ?""", (role_id,))]
    requester = requester_session if requester_kind == SESSION else None
    moved, deferred, kept = _rehome(store, db, agents, target=holder, role_id=role_id, mode="appointment",
                                    requester=requester, requester_kind=requester_kind, reason=reason)
    # Busy agents already waiting for this role follow its current holder when they move.
    return {"moved_thread_ids": moved, "deferred_thread_ids": deferred, "kept_thread_ids": kept}


def _rehome(store, db, agents, *, target, role_id, mode, requester, requester_kind, reason):
    """Move each (thread, source) now when idle and safe, otherwise record a deferred move."""
    moved, deferred, kept, ready = [], [], [], {}
    for thread_id, source in agents:
        if thread_id == target or (target and supervision.descends_from(db, target, {thread_id})):
            kept.append(thread_id)  # A new holder never supervises itself or an ancestor.
            continue
        waiting = db.execute("SELECT role_id, to_kind FROM supervision_pending_moves WHERE thread_id = ?",
                             (thread_id,)).fetchone()
        if waiting and not role_id and (waiting["role_id"] or waiting["to_kind"] == SESSION):
            # A move already bound for a role or a chosen session wins over passing it to the user.
            kept.append(thread_id)
            continue
        if supervision.blocker(db, thread_id, source):
            _defer(store, db, thread_id, source=source, target=target, role_id=role_id, mode=mode, reason=reason)
            deferred.append(thread_id)
        else:
            ready.setdefault(source, []).append(thread_id)
    for source, thread_ids in ready.items():
        supervision.move(store, db, source=source, target=target, thread_ids=thread_ids, mode=mode,
                         requester=requester or target or source or thread_ids[0], requester_kind=requester_kind,
                         reason=reason, request_id=f"{mode}:{uuid.uuid4()}", role_id=role_id, strict=False)
        moved += thread_ids
    return moved, deferred, kept


def _defer(store, db, thread_id, *, source, target, role_id, mode, reason):
    db.execute("""INSERT INTO supervision_pending_moves (thread_id, from_kind, from_session_id, to_kind,
        to_session_id, role_id, mode, reason, blocker, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
        ON CONFLICT(thread_id) DO UPDATE SET from_kind = excluded.from_kind, from_session_id = excluded.from_session_id,
        to_kind = excluded.to_kind, to_session_id = excluded.to_session_id, role_id = excluded.role_id,
        mode = excluded.mode, reason = excluded.reason""",
        (thread_id, SESSION if source else USER, source, SESSION if target else USER, target, role_id, mode,
         reason, store.clock()))


def assign_supervisor(store, *, thread_id, holder, workspace=None):
    """User-only: make a live session the supervisor of one agent; a busy agent moves when idle."""
    _prepare(store)
    with store._connection() as db:
        db.execute("BEGIN IMMEDIATE")
        supervision._session(db, thread_id, "Agent", workspace)
        supervision._session(db, holder, "Supervisor", workspace)
        if supervision.retired(db, holder):
            raise CoordinationError("A closed or ended session cannot supervise agents.")
        if supervision._archived(db, thread_id):
            raise CoordinationError("Reopen the agent before changing its supervisor.")
        if supervision.supervised_by_user(db, thread_id):
            source = None
        else:
            source = supervision.effective_supervisor(db, thread_id)
            if source is None:
                raise CoordinationError("This conversation has no supervisor to replace.")
        if holder == source or holder == thread_id:
            raise CoordinationError("Choose a different supervisor.")
        if supervision.descends_from(db, holder, {thread_id}):
            raise CoordinationError("An agent cannot be supervised by one of its own agents.")
        role = held_role(db, holder)
        role_id, reason = (role["role_id"] if role else None), "Assigned by the user."
        # The user's explicit choice replaces any move this agent was waiting for.
        if supervision.blocker(db, thread_id, source):
            _defer(store, db, thread_id, source=source, target=holder, role_id=role_id, mode="assignment", reason=reason)
            return {"thread_id": thread_id, "supervisor": holder, "moved_thread_ids": [], "deferred_thread_ids": [thread_id]}
        supervision.move(store, db, source=source, target=holder, thread_ids=[thread_id], mode="assignment",
                         requester=holder, requester_kind=USER, reason=reason, request_id=f"assignment:{uuid.uuid4()}",
                         role_id=role_id, strict=False)
        return {"thread_id": thread_id, "supervisor": holder, "moved_thread_ids": [thread_id], "deferred_thread_ids": []}


def _sweep(store, workspace):
    """Once per store: supervisors retired before succession existed still pass on their open agents."""
    if getattr(store, "_workspace_roles_swept", False):
        return
    with store._connection() as db:
        db.execute("BEGIN IMMEDIATE")
        supervisors = set()
        if supervision._exists(db, "app_control_requests"):
            supervisors |= {row[0] for row in db.execute("""SELECT DISTINCT sender_session_id FROM app_control_requests
                WHERE operation = 'create' AND status = 'completed' AND thread_id IS NOT NULL""")}
        supervisors |= {row[0] for row in db.execute(
            "SELECT DISTINCT supervisor_session_id FROM agent_supervisors WHERE supervisor_kind = 'session'")}
        now = store.clock()
        for session_id in sorted(supervisors):
            row = db.execute("SELECT cwd FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
            if row and (not workspace or matches_workspace(row["cwd"], workspace)) and supervision.retired(db, session_id) \
                    and supervision.supervised_threads(db, session_id):
                supervision.record_retirement(db, session_id, now)
    store._workspace_roles_swept = True


def _succeed(store, db, session_id):
    """A retired supervisor's role becomes vacant; its open agents pass to the user for that role."""
    role = held_role(db, session_id)
    if role:
        now = store.clock()
        db.execute("UPDATE workspace_roles SET holder_session_id = NULL, updated_at = ? WHERE role_id = ?",
                   (now, role["role_id"]))
        db.execute("""INSERT INTO workspace_role_appointments (role_id, from_session_id, to_session_id, requester_kind,
            requester_session_id, reason, created_at) VALUES (?, ?, NULL, ?, NULL, ?, ?)""",
            (role["role_id"], session_id, RUNTIME, "The holder was closed or ended.", now))
    agents = [(thread_id, session_id) for thread_id in supervision.supervised_threads(db, session_id)]
    if not agents and not role:
        return 0
    _rehome(store, db, agents, target=None, role_id=role["role_id"] if role else None, mode="succession",
            requester=None, requester_kind=RUNTIME,
            reason=f"Supervisor {session_id} was closed or ended" + (f" while holding {role['name']!r}." if role else "."))
    return 1


def _advance(store, db, pending):
    """Complete one deferred move when its agent is idle and safe; drop it when it no longer applies."""
    thread_id, source = pending["thread_id"], pending["from_session_id"] if pending["from_kind"] == SESSION else None
    current_ok = (supervision.supervised_by_user(db, thread_id) if source is None
                  else supervision.effective_supervisor(db, thread_id) == source
                  and not supervision.supervised_by_user(db, thread_id))
    target = pending["to_session_id"] if pending["to_kind"] == SESSION else None
    if pending["role_id"]:
        role = db.execute("SELECT holder_session_id FROM workspace_roles WHERE role_id = ?",
                          (pending["role_id"],)).fetchone()
        target = role["holder_session_id"] if role else target
    if target and supervision.retired(db, target):
        target = None
    reopened = pending["mode"] == "succession" and source is not None and not supervision.retired(db, source)
    if (reopened or not current_ok or supervision._archived(db, thread_id) or target == source
            or (target and supervision.descends_from(db, target, {thread_id}))):
        db.execute("DELETE FROM supervision_pending_moves WHERE thread_id = ?", (thread_id,))
        return
    why = supervision.blocker(db, thread_id, source)
    if why:
        db.execute("UPDATE supervision_pending_moves SET blocker = ? WHERE thread_id = ?", (why, thread_id))
        return
    supervision.move(store, db, source=source, target=target, thread_ids=[thread_id], mode=pending["mode"],
                     requester=target or source or thread_id, requester_kind=RUNTIME, reason=pending["reason"],
                     request_id=f"{pending['mode']}:{uuid.uuid4()}", role_id=pending["role_id"], strict=False)
    return True


def process(store, workspace=None):
    """One succession and deferred-move pass; returns how many records changed supervision."""
    _prepare(store)
    _sweep(store, workspace)
    with store._connection() as db:
        retirements = [row[0] for row in db.execute("SELECT session_id FROM supervisor_retirements ORDER BY retired_at")]
        pending = [row[0] for row in db.execute("SELECT thread_id FROM supervision_pending_moves ORDER BY created_at")]
    changed = 0
    for session_id in retirements:
        try:
            with store._connection() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT cwd FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
                if row and workspace and not matches_workspace(row["cwd"], workspace):
                    continue  # Another runtime's workspace owns this succession.
                if row and supervision.retired(db, session_id):
                    changed += _succeed(store, db, session_id)
                db.execute("DELETE FROM supervisor_retirements WHERE session_id = ?", (session_id,))
        except Exception:
            logging.getLogger(__name__).exception("Supervisor succession failed for %s", session_id)
    for thread_id in pending:
        try:
            with store._connection() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT * FROM supervision_pending_moves WHERE thread_id = ?", (thread_id,)).fetchone()
                session = db.execute("SELECT cwd FROM sessions WHERE session_id = ?", (thread_id,)).fetchone()
                if row is None or (session and workspace and not matches_workspace(session["cwd"], workspace)):
                    continue
                changed += bool(_advance(store, db, row))
        except Exception:
            logging.getLogger(__name__).exception("Deferred supervision move failed for %s", thread_id)
    return changed
