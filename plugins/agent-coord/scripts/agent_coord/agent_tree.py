"""Read-only agent families, independent of overview filters and thread placement."""
from collections import defaultdict

from .store import CoordinationError


def agent_tree(sessions, thread_id):
    # This must not resume a provider, mark messages read, or recover usage logs.
    selected = sessions.work_thread(thread_id, history=False)
    creators = sessions._app_agent_creators()
    parents = {child: creator["thread_id"] for child, creator in creators.items()
               if creator["mode"] == "orchestration"}
    with sessions.store._connection() as db:
        for row in db.execute("""SELECT parent_session_id, child_session_id FROM delegations
                                 WHERE child_session_id IS NOT NULL ORDER BY created_at, delegation_id"""):
            parents.setdefault(row[1], row[0])
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
                records[identifier] = sessions.work_thread(identifier, history=False, _creators=creators)
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
