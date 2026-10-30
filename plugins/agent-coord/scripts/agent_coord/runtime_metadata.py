"""Finite owner-routed metadata operations for detached local supervisors.

Processes, terminals, and their output pipes remain with their supervisors.
Only complete database operations cross the authenticated runtime boundary.
"""
from __future__ import annotations

from functools import wraps

from .runtime_requests import register_operation, run_local_or_forward


STORE_MUTATIONS = frozenset({
    "register", "touch", "record_tool", "clear_scope_requirement",
    "request_scope_declarations", "begin_work", "end_work", "handoff_work",
    "end_session", "create_delegation", "mark_delegation_launched",
    "set_delegation_child_process", "record_delegation_runtime_exit",
    "fail_delegation_launch", "cancel_delegation", "attach_delegation",
    "record_delegation_token_usage", "finish_delegation", "fail_active_delegations_for_child",
    "register_wake_target", "disable_wake", "set_wake_watcher_pid",
    "clear_wake_watcher_pid", "record_wake_check", "register_zellij_wake",
    "disable_zellij_wake", "set_zellij_watcher_pid", "clear_zellij_watcher_pid",
    "record_zellij_check", "claim_wake_messages", "complete_wake_attempts",
    "send_message", "reply_message", "inbox", "acknowledge", "acknowledge_all_unread",
})
STORE_READS = frozenset({"get_delegation", "get_wake_target", "get_session",
                         "pending_wake_message_ids", "get_zellij_wake"})


def _store_handler(name):
    def execute(store, *args, **kwargs):
        return getattr(store, name)(*args, **kwargs)
    return execute


_STORE_HANDLERS = {"store." + name: _store_handler(name) for name in STORE_MUTATIONS}


class MetadataStore:
    """Keep read connections local; route complete mutations to the owner."""

    def __init__(self, store, *, timeout=5):
        self.local = store
        self.timeout = timeout

    def __getattr__(self, name):
        value = getattr(self.local, name)
        if name in STORE_READS:
            @wraps(value)
            def read(*args, **kwargs):
                with self.local.reading():
                    return value(*args, **kwargs)
            return read
        if name not in STORE_MUTATIONS:
            return value

        @wraps(value)
        def execute(*args, **kwargs):
            return run_local_or_forward("operation", {
                "name": "store." + name, "args": list(args), "kwargs": kwargs,
            }, lambda: value(*args, **kwargs), self.local.database_path, timeout=self.timeout)
        return execute


def metadata_store(store):
    return store if isinstance(store, MetadataStore) else MetadataStore(store)


def job_operation(store, name, *args, timeout=5, **kwargs):
    handler = {"claim": _claim_job, "save_progress": _save_job_progress,
               "finish": _finish_job}[name]
    return run_local_or_forward("operation", {"name": "jobs." + name,
                                "args": list(args), "kwargs": kwargs},
                                lambda: handler(store, *args, **kwargs),
                                store.database_path, timeout=timeout)


def _claim_job(store, job_id, token, supervisor_pid):
    now = store.clock()
    with store._connection() as db:
        db.execute("BEGIN IMMEDIATE")
        claimed = db.execute("""UPDATE jobs SET state='running', started_at=?, heartbeat_at=?,
            updated_at=?, supervisor_pid=? WHERE id=? AND supervisor_token=? AND state='starting'
            AND cancel_requested_at IS NULL""", (now, now, now, supervisor_pid, job_id, token)).rowcount
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return {"claimed": bool(claimed), "row": dict(row) if row else None}


def _save_job_progress(store, job_id, token, child_pid, discarded):
    now = store.clock()
    with store.connection_timeout(0), store._connection() as db:
        db.execute("""UPDATE jobs SET child_pid=?, heartbeat_at=?, updated_at=?, output_discarded=?
            WHERE id=? AND supervisor_token=? AND state='running'""",
                   (child_pid, now, now, discarded, job_id, token))
    return True


def _finish_job(store, job_id, token, *, state, exit_code, error, discarded, finished_at):
    from .jobs import ManagedJobs
    with store.connection_timeout(0), store._connection() as db:
        db.execute("""UPDATE jobs SET state=?, exit_code=?, error=?, finished_at=?, updated_at=?,
            heartbeat_at=?, output_discarded=? WHERE id=? AND supervisor_token=? AND state='running'""",
                   (state, exit_code, error, finished_at, finished_at, finished_at, discarded, job_id, token))
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return ManagedJobs._record(row)


def install_operations():
    for name, handler in _STORE_HANDLERS.items():
        register_operation(name, handler)
    for name, handler in {"claim": _claim_job, "save_progress": _save_job_progress,
                          "finish": _finish_job}.items():
        register_operation("jobs." + name, handler)
