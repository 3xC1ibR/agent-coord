"""Bounded, read-only metadata batches for background observers."""
from __future__ import annotations

from .store import CoordinationError
from .workspaces import matches_workspace


MAX_METADATA_THREADS = 200
METADATA_FIELDS = ("thread_id", "created_at", "forked_from_thread_id", "title", "client", "attention")


def metadata(store, thread_ids, *, allowed_cwd=None):
    """Return requested thread metadata from one snapshot, without waking agents.

    Input order is preserved and duplicate identifiers are returned once. IDs
    outside the allowed workspace are indistinguishable from missing records.
    """
    if not isinstance(thread_ids, (list, tuple)) or len(thread_ids) > MAX_METADATA_THREADS:
        raise CoordinationError(f"Thread metadata accepts at most {MAX_METADATA_THREADS} thread IDs.")
    if any(not isinstance(identity, str) or not identity.strip() or len(identity) > 256 or "\0" in identity
           for identity in thread_ids):
        raise CoordinationError("Thread IDs must contain 1–256 characters.")
    identities = list(dict.fromkeys(thread_ids))
    if not identities:
        return {"data": [], "missing": []}
    placeholders = ",".join("?" for _ in identities)
    with store.reading(), store._connection() as db:
        db.execute("BEGIN")
        rows = db.execute("""SELECT t.thread_id, t.created_at, t.forked_from_thread_id,
            t.title, s.client, s.cwd,
            CASE WHEN t.attention = 'later' AND t.snoozed_until <= ?
                 THEN 'now' ELSE t.attention END AS attention
            FROM work_threads t JOIN sessions s ON s.session_id = t.thread_id
            WHERE t.thread_id IN (""" + placeholders + ")", (store.clock(), *identities)).fetchall()
        permitted = {}
        selected = {}
        for row in rows:
            cwd = row["cwd"]
            if cwd not in permitted:
                permitted[cwd] = matches_workspace(cwd, allowed_cwd)
            if permitted[cwd]:
                selected[row["thread_id"]] = {field: row[field] for field in METADATA_FIELDS}
    return {"data": [selected[identity] for identity in identities if identity in selected],
            "missing": [identity for identity in identities if identity not in selected]}
