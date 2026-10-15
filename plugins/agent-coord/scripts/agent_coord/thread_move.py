"""Move an app conversation and its execution directory as one transaction."""
from pathlib import Path
import json
import uuid

from .organization import repository_root
from .store import CoordinationError
from .threads import project_root
from .workspaces import matches_workspace


def destination_directory(cwd):
    if not isinstance(cwd, str) or not cwd.strip() or not Path(cwd).expanduser().is_absolute():
        raise CoordinationError("Choose an absolute project directory.")
    try:
        directory = Path(cwd).expanduser().resolve(strict=True)
        if directory.is_dir():
            return str(directory)
    except (OSError, RuntimeError, ValueError):
        pass
    raise CoordinationError("Choose an existing project directory.")


def move_work_thread(sessions, thread_id, cwd, *, expected_settings=None):
    """The owning runtime applies the same cwd mechanism used by /cd."""
    directory = destination_directory(cwd)
    if not matches_workspace(directory, sessions.cwd):
        raise CoordinationError("Choose a project within this app runtime's workspace.")
    with sessions._thread_lock(thread_id):
        thread = sessions.work_thread(thread_id, history=False)
        if not thread["browser_session"]:
            raise CoordinationError("Moving requires an app conversation; terminal sessions cannot change projects here.")
        record = sessions._record(thread_id)
        if record["client"] not in {"codex", "claude"}:
            raise CoordinationError("This provider keeps its original workspace. Start a new conversation in the destination project.")
        # Refresh provider activity, rather than trusting only the saved flag.
        sessions.read(thread_id)
        with sessions.lock:
            if thread_id in sessions.active:
                raise CoordinationError("Wait for the running turn to finish before moving this thread.")
        if sessions.queue.list(thread_id):
            raise CoordinationError("Wait for queued messages to finish before moving this thread.")
        root = project_root(directory)
        workspace_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "agent-coord:" + root))
        repo_root = repository_root(directory)
        repo_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "agent-coord:repository:" + repo_root)) if repo_root else None
        now = sessions.store.clock()
        with sessions.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT * FROM browser_sessions WHERE thread_id = ?", (thread_id,)).fetchone()
            state = db.execute("SELECT turn_active, write_scope_json FROM sessions WHERE session_id = ?", (thread_id,)).fetchone()
            if expected_settings and any(current[key] != value for key, value in expected_settings.items()):
                raise CoordinationError("Conversation settings changed after the move was queued. Inspect the thread before retrying.")
            if current["archived"] or db.execute("SELECT attention FROM work_threads WHERE thread_id = ?", (thread_id,)).fetchone()[0] == "archived":
                raise CoordinationError("Reopen the thread before moving it.")
            if state["turn_active"]:
                raise CoordinationError("Wait for the running turn to finish before moving this thread.")
            if json.loads(state["write_scope_json"]):
                raise CoordinationError("Release the thread's work scope with agent-coord end-work before moving it.")
            if db.execute("SELECT 1 FROM sqlite_master WHERE name = 'thread_close_requests'").fetchone() and db.execute(
                    "SELECT 1 FROM thread_close_requests WHERE thread_id = ? AND status IN ('queued', 'closing')", (thread_id,)).fetchone():
                raise CoordinationError("Cancel the pending close before moving the thread.")
            db.execute("INSERT OR IGNORE INTO work_projects VALUES (?, ?, ?)", (workspace_id, root, Path(root).name or root))
            if repo_id:
                db.execute("INSERT OR IGNORE INTO work_repositories VALUES (?, ?, ?)", (repo_id, repo_root, Path(repo_root).name or repo_root))
            db.execute("UPDATE browser_sessions SET cwd = ?, updated_at = ? WHERE thread_id = ?", (directory, now, thread_id))
            db.execute("UPDATE sessions SET cwd = ? WHERE session_id = ?", (directory, thread_id))
            db.execute("UPDATE work_threads SET project_cwd = ?, project_id = ?, updated_at = ? WHERE thread_id = ?",
                       (directory, workspace_id, now, thread_id))
            # Groups belong to a project directory; preserve only a matching group.
            db.execute("""UPDATE thread_organization SET repository_id = ?, project_id = CASE WHEN
                (SELECT cwd FROM named_projects WHERE id = thread_organization.project_id) = ?
                THEN project_id ELSE NULL END WHERE thread_id = ?""", (repo_id, directory, thread_id))
        sessions._publish("browser/changed", {"threadId": thread_id, "projectCwd": directory})
        return sessions.work_thread(thread_id, history=False)
