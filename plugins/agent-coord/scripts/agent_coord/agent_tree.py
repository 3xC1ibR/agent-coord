"""Read-only agent and fork families, independent of filters and placement."""
from collections import defaultdict

from .organization import CREATION_MODE_SQL, app_agent_creators
from .store import CoordinationError
from .supervision import overrides as supervision_overrides
from .workspaces import matches_workspace, require_thread_workspaces


def agent_tree(sessions, thread_id):
    # This must not resume a provider, mark messages read, or recover usage logs.
    selected = sessions.work_thread(thread_id, history=False)
    creators = sessions._app_agent_creators()
    with sessions.store.reading(), sessions.store._connection() as db:
        parents = family_parents(db, creators, supervision_overrides(db))
    return project_family(selected, parents, lambda ident: sessions.work_thread(ident, history=False, _creators=creators))


def family_parents(db, creators, overrides):
    parents = {child: creator["thread_id"] for child, creator in creators.items()
               if creator["mode"] == "orchestration"}
    for row in db.execute("""SELECT parent_session_id, child_session_id FROM delegations
                             WHERE child_session_id IS NOT NULL ORDER BY created_at, delegation_id"""):
        parents.setdefault(row[1], row[0])
    # An explicit supervision transfer replaces the creator edge.
    parents.update(overrides)
    # Fork lineage is a display relationship, not supervision ownership.
    for row in db.execute("""SELECT thread_id, forked_from_thread_id FROM work_threads
                             WHERE forked_from_thread_id IS NOT NULL"""):
        parents.setdefault(row[0], row[1])
    return parents


def bounded_family(sessions, thread_id, *, limit, runtime):
    """Seek the selected family by identity and stop at the open-panel limit."""
    store = sessions.store
    parents, creators, relations = {}, {}, {}
    with store.reading(), store._connection() as db:
        db.execute("BEGIN")
        has_roles = db.execute("SELECT 1 FROM sqlite_master WHERE name='workspace_roles'").fetchone()
        supervisor_columns = {row[1] for row in db.execute("PRAGMA table_info(agent_supervisors)")}
        supervisor_kind = "supervisor_kind" if "supervisor_kind" in supervisor_columns else "'session'"

        def parent(identifier):
            if identifier not in parents:
                creator = app_agent_creators(db, identifier).get(identifier)
                supervisor = (db.execute(f"""SELECT supervisor_session_id, {supervisor_kind}
                    FROM agent_supervisors WHERE thread_id=?""", (identifier,)).fetchone() if supervisor_columns else None)
                role = db.execute("SELECT role_id, name FROM workspace_roles WHERE holder_session_id=?",
                                  (identifier,)).fetchone() if has_roles else None
                relations[identifier] = {"overrides": ({identifier: supervisor[0] if supervisor[1] == "session" else None}
                                                        if supervisor else {}),
                                         "roles": {identifier: dict(role)} if role else {}}
                fallback = db.execute("""SELECT
                    (SELECT d.parent_session_id FROM delegations d WHERE d.child_session_id=?
                     ORDER BY d.created_at, d.delegation_id LIMIT 1),
                    (SELECT forked_from_thread_id FROM work_threads WHERE thread_id=?)""",
                    (identifier, identifier)).fetchone()
                created = creator["thread_id"] if creator and creator["mode"] == "orchestration" else None
                parents[identifier] = (relations[identifier]["overrides"][identifier] if supervisor else
                                       created or fallback[0] or fallback[1])
                if creator and creator["cwd"] and matches_workspace(creator["cwd"], sessions.cwd):
                    cwd = creator.pop("cwd")
                    creator["available"] = True
                    creator["workspaces"] = [creator["project_cwd"] or cwd] if created else []
                    creators[identifier] = creator
            return parents[identifier]

        def visible(identifier):
            try:
                require_thread_workspaces(store, [identifier], sessions.cwd, exclude_managed=True)
            except CoordinationError:
                return False
            return True

        chain = [thread_id]
        while len(chain) < limit:
            ancestor = parent(chain[-1])
            if not ancestor or ancestor in chain or not visible(ancestor):
                break
            chain.append(ancestor)
        root = chain[-1]
        members = dict.fromkeys(reversed(chain))
        for identifier in members.copy():
            parent(identifier)
        pending = list(members)
        while pending and len(members) < limit:
            identifier = pending.pop()
            remaining = limit - len(members)
            supervisor_children = (f"""SELECT thread_id FROM agent_supervisors
                WHERE supervisor_session_id=? AND {supervisor_kind}='session' LIMIT ?"""
                if supervisor_columns else "SELECT NULL AS thread_id WHERE ? IS NULL LIMIT ?")
            candidates = db.execute(f"""SELECT thread_id FROM (SELECT thread_id
                    FROM app_control_requests WHERE sender_session_id=? AND operation='create'
                    AND status IN ('running', 'completed', 'uncertain') AND thread_id IS NOT NULL
                    AND {CREATION_MODE_SQL}='orchestration' LIMIT ?)
                UNION SELECT thread_id FROM ({supervisor_children})
                UNION SELECT thread_id FROM (SELECT thread_id FROM work_threads
                    WHERE forked_from_thread_id=? LIMIT ?)""",
                (identifier, remaining, identifier, remaining, identifier, remaining))
            for (child,) in candidates:
                if child not in members and parent(child) == identifier and visible(child):
                    members[child] = None
                    pending.append(child)
                    if len(members) == limit:
                        break
        # Breadcrumbs retain the full visible creation path of the selected
        # agent, even if its effective supervisor was transferred.
        for identifier in members:
            creator = creators.get(identifier)
            if not creator or creator["mode"] != "orchestration":
                continue
            seen, ancestor = {identifier}, creator["thread_id"]
            while ancestor in members and ancestor not in seen:
                seen.add(ancestor)
                previous = creators.get(ancestor)
                if not previous or previous["mode"] != "orchestration":
                    break
                for path in previous["workspaces"]:
                    if path not in creator["workspaces"]:
                        creator["workspaces"].append(path)
                ancestor = previous["thread_id"]

    records = {identifier: sessions.work_thread(identifier, history=False, _creators=creators,
                                               _related=relations[identifier],
                                               _runtime=runtime if identifier == thread_id else None)
               for identifier in members}
    shown = {identifier: parent_id for identifier, parent_id in parents.items()
             if identifier in records and parent_id in records}
    return project_family(records[thread_id], shown, records.__getitem__)


def project_family(selected, parents, read_thread):
    """Follow only visible ancestors and descendants; an inaccessible intermediate ends the branch."""
    thread_id = selected["thread_id"]
    # Old data can contain cycles. The same edge is dropped for every entry point.
    for child in sorted(parents):
        seen, parent = {child}, parents.get(child)
        while parent and parent not in seen:
            seen.add(parent)
            parent = parents.get(parent)
        if parent:
            parents.pop(child, None)
    children = defaultdict(list)
    for child, parent in parents.items():
        children[parent].append(child)
    records = {thread_id: selected}

    def read(identifier):
        if identifier not in records:
            try:
                records[identifier] = read_thread(identifier)
            except CoordinationError:
                return None  # Missing or outside this runtime's workspace.
        return records[identifier]

    root = thread_id
    while parents.get(root) and read(parents[root]) is not None:
        root = parents[root]
    pending, visible = [root], {}
    while pending:
        identifier = pending.pop()
        if identifier in visible:
            continue
        record = read(identifier)
        if record is None:
            continue
        visible[identifier] = record
        pending.extend(children[identifier])
    data = [{**record, "agent_parent_id": parents.get(identifier) if parents.get(identifier) in visible else None}
            for identifier, record in visible.items()]
    data.sort(key=lambda t: (t["created_at"], t["thread_id"]))
    return {"root_thread_id": root, "data": data}
