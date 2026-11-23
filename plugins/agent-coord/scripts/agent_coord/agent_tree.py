"""Read-only agent and fork families, independent of filters and placement."""
from collections import defaultdict

from .store import CoordinationError
from .supervision import overrides as supervision_overrides


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
