"""Durable personal work threads. No issue tracker or running client is required."""
from __future__ import annotations

import json
import shlex
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from .store import CoordinationError
from .organization import OrganizationStore, UNSET

PHASES = {"discussion", "investigation", "planning", "implementation", "validation", "deployment", "finished"}
ATTENTION_STATES = {"now", "later", "archived"}
NEXT_ACTORS = {"user", "agent", "external", "nobody"}
LINK_KINDS = {"pull_request", "document", "issue", "bead", "branch", "other"}


def _text(value, label, limit, *, empty=False):
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        raise CoordinationError(f"{label} must be {'non-empty ' if not empty else ''}text of at most {limit} characters.")
    return value.strip()


def project_root(cwd: str) -> str:
    """Find a repository, including linked worktrees, without running git."""
    directory = Path(cwd).expanduser().resolve()
    for candidate in (directory, *directory.parents):
        marker = candidate / ".git"
        if marker.is_dir():
            return str(candidate)
        if marker.is_file():
            try:
                text = marker.read_text().strip()
                if text.startswith("gitdir:"):
                    gitdir = (candidate / text[7:].strip()).resolve()
                    common_file = gitdir / "commondir"
                    if common_file.is_file():
                        common = (gitdir / common_file.read_text().strip()).resolve()
                        return str(common.parent if common.name == ".git" else common)
            except (OSError, UnicodeError):
                pass
            return str(candidate)
    return str(directory)


class ThreadStore:
    def __init__(self, store):
        self.store = store
        with store._connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS work_projects (
                    id TEXT PRIMARY KEY, root TEXT NOT NULL UNIQUE, name TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS work_threads (
                    thread_id TEXT PRIMARY KEY REFERENCES sessions(session_id),
                    project_id TEXT NOT NULL REFERENCES work_projects(id),
                    title TEXT NOT NULL, original_request TEXT NOT NULL DEFAULT '',
                    title_source TEXT NOT NULL DEFAULT 'auto',
                    attention TEXT NOT NULL DEFAULT 'now',
                    pinned INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    turn_started_at REAL, turn_id TEXT, turn_key TEXT,
                    seen_checkpoint_id INTEGER NOT NULL DEFAULT 0,
                    seen_completion_id INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS thread_checkpoints (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    thread_id TEXT NOT NULL REFERENCES work_threads(thread_id),
                    phase TEXT NOT NULL, summary TEXT NOT NULL,
                    next_action TEXT NOT NULL, next_actor TEXT NOT NULL,
                    session_id TEXT NOT NULL, turn_id TEXT, author TEXT NOT NULL DEFAULT 'agent',
                    created_at REAL NOT NULL, refreshed_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS checkpoints_thread_idx
                    ON thread_checkpoints(thread_id, id);
                CREATE TABLE IF NOT EXISTS thread_links (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    thread_id TEXT NOT NULL REFERENCES work_threads(thread_id),
                    kind TEXT NOT NULL, label TEXT NOT NULL, target TEXT NOT NULL,
                    project_id TEXT NOT NULL REFERENCES work_projects(id),
                    UNIQUE(thread_id, kind, target, project_id)
                );
                CREATE INDEX IF NOT EXISTS threads_project_idx ON work_threads(project_id, attention);
                CREATE TABLE IF NOT EXISTS turn_completions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    thread_id TEXT NOT NULL REFERENCES work_threads(thread_id),
                    turn_key TEXT NOT NULL, status TEXT NOT NULL,
                    completed_at REAL NOT NULL, notified_at REAL,
                    UNIQUE(thread_id, turn_key)
                );
            """)
            # Serialize upgrades across independently running terminal hooks.
            db.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(work_threads)")}
            if "pinned" not in columns:
                db.execute("ALTER TABLE work_threads ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
            if "turn_key" not in columns:
                db.execute("ALTER TABLE work_threads ADD COLUMN turn_key TEXT")
            if "seen_completion_id" not in columns:
                db.execute("ALTER TABLE work_threads ADD COLUMN seen_completion_id INTEGER NOT NULL DEFAULT 0")
                # Preserve the old read marker where a checkpoint was already
                # opened. Future replies have their own independent receipt.
                db.execute("""UPDATE work_threads SET seen_completion_id = COALESCE(
                    (SELECT MAX(id) FROM turn_completions WHERE thread_id = work_threads.thread_id), 0)
                    WHERE seen_checkpoint_id > 0 AND seen_checkpoint_id >= COALESCE(
                    (SELECT MAX(id) FROM thread_checkpoints WHERE thread_id = work_threads.thread_id), 0)""")
            if "title_source" not in columns:
                db.execute("ALTER TABLE work_threads ADD COLUMN title_source TEXT NOT NULL DEFAULT 'auto'")
                # Older versions stored prompt excerpts and user names together.
                # Only known placeholders/excerpts are eligible for agent naming.
                for row in db.execute("""SELECT t.thread_id, t.title, t.original_request, s.name
                                         FROM work_threads t JOIN sessions s ON s.session_id = t.thread_id""").fetchall():
                    excerpt = " ".join(row["original_request"].split())[:100]
                    if row["title"] not in {"New thread", "New session"} and (row["title"] != excerpt or row["title"] == row["name"]):
                        db.execute("UPDATE work_threads SET title_source = 'user' WHERE thread_id = ?", (row["thread_id"],))

        self.organization = OrganizationStore(store)

    def ensure(self, session_id: str) -> dict:
        session = self.store.get_session(session_id)
        with self.store._connection() as db:
            existing = db.execute("SELECT project_id FROM work_threads WHERE thread_id = ?", (session_id,)).fetchone()
            if not existing:
                root = project_root(session["cwd"])
                project_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "agent-coord:" + root))
                db.execute("INSERT OR IGNORE INTO work_projects VALUES (?, ?, ?)",
                           (project_id, root, Path(root).name or root))
                now = self.store.clock()
                title = session["name"] or "New thread"
                source = "auto" if title in {"New thread", "New session"} else "user"
                db.execute("INSERT OR IGNORE INTO work_threads (thread_id, project_id, title, title_source, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                           (session_id, project_id, title, source, now, now))
            self.organization.ensure_thread(db, session_id, session["cwd"])
            if session["bead_id"]:
                self._save_link(db, session_id, ("bead", session["bead_id"], session["bead_id"], existing["project_id"] if existing else project_id))
        return self.get(session_id)

    def _read(self, db, row, *, history=False):
        result = dict(row)
        result["pinned"] = bool(row["pinned"])
        latest = db.execute("SELECT * FROM thread_checkpoints WHERE thread_id = ? ORDER BY id DESC LIMIT 1", (row["thread_id"],)).fetchone()
        result["checkpoint"] = dict(latest) if latest else None
        result["checkpoint_stale"] = bool(latest and row["turn_started_at"] and latest["refreshed_at"] < row["turn_started_at"])
        completion = db.execute("""SELECT id, status, completed_at FROM turn_completions
                                   WHERE thread_id = ? AND turn_key = ?""",
                                (row["thread_id"], row["turn_key"])).fetchone()
        result["turn_completion"] = dict(completion) if completion else None
        result["unread_result"] = bool(completion and completion["status"] != "interrupted"
                                       and completion["id"] > row["seen_completion_id"])
        result["unread"] = bool(result["unread_result"] or (latest and latest["id"] > row["seen_checkpoint_id"]))
        result["links"] = [dict(link) for link in db.execute("SELECT * FROM thread_links WHERE thread_id = ? ORDER BY id", (row["thread_id"],))]
        if history:
            result["checkpoints"] = [dict(cp) for cp in db.execute("SELECT * FROM thread_checkpoints WHERE thread_id = ? ORDER BY id DESC", (row["thread_id"],))]
        return self.organization.describe(db, result)

    _SELECT = """SELECT t.*, p.name AS project_name, p.root AS project_root,
                        s.cwd, s.client, s.ended_at, s.turn_active
                 FROM work_threads t JOIN work_projects p ON p.id = t.project_id
                 JOIN sessions s ON s.session_id = t.thread_id"""

    def get(self, thread_id: str, *, history=False) -> dict:
        self.organization.backfill()
        with self.store._connection() as db:
            row = db.execute(self._SELECT + " WHERE t.thread_id = ?", (thread_id,)).fetchone()
            if row is None:
                raise CoordinationError("Work thread not found.")
            return self._read(db, row, history=history)

    def list(self, *, archived=False, cwd=None, repository_id=UNSET, project_id=UNSET) -> list[dict]:
        self.organization.backfill()
        with self.store._connection() as db:
            rows = db.execute(self._SELECT + " WHERE (t.attention = 'archived') = ? ORDER BY t.updated_at DESC, t.thread_id", (int(archived),)).fetchall()
            threads = [self._read(db, row) for row in rows if cwd is None or row["project_root"] == project_root(cwd)]
            return [thread for thread in threads
                    if (repository_id is UNSET or thread["repository_id"] == repository_id)
                    and (project_id is UNSET or thread["project_id"] == project_id)]

    def update(self, thread_id: str, *, title=None, attention=None, pinned=UNSET, seen=False,
               seen_checkpoint_id=None, seen_completion_id=None, repository_id=UNSET, project_id=UNSET) -> dict:
        self.get(thread_id)
        if title is not None:
            title = _text(title, "Thread title", 160)
        if attention is not None and (not isinstance(attention, str) or attention not in ATTENTION_STATES):
            raise CoordinationError("Attention must be now, later, or archived.")
        if pinned is not UNSET and not isinstance(pinned, bool):
            raise CoordinationError("Pinned must be true or false.")
        if not seen and (seen_checkpoint_id is not None or seen_completion_id is not None):
            raise CoordinationError("Read markers require seen: true.")
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if seen:
                self._mark_seen(db, thread_id, seen_checkpoint_id, seen_completion_id)
            self.organization.update(db, thread_id, repository_id=repository_id, project_id=project_id)
            if title is not None:
                db.execute("UPDATE work_threads SET title = ?, title_source = 'user' WHERE thread_id = ?", (title, thread_id))
            if attention is not None:
                db.execute("UPDATE work_threads SET attention = ? WHERE thread_id = ?", (attention, thread_id))
            if pinned is not UNSET:
                db.execute("UPDATE work_threads SET pinned = ? WHERE thread_id = ?", (int(pinned), thread_id))
        return self.get(thread_id, history=True)

    @staticmethod
    def _mark_seen(db, thread_id, checkpoint_id=None, completion_id=None):
        # UI receipts name only the records actually displayed. A reply arriving
        # between the detail read and this update must remain unread.
        for table, field, cursor in (("thread_checkpoints", "seen_checkpoint_id", checkpoint_id),
                                     ("turn_completions", "seen_completion_id", completion_id)):
            if cursor is not None and (type(cursor) is not int or cursor < 0 or (cursor > 0 and not db.execute(
                    f"SELECT 1 FROM {table} WHERE thread_id = ? AND id = ?", (thread_id, cursor)).fetchone())):
                raise CoordinationError("Read markers must identify a displayed checkpoint or completion in this thread.")
            db.execute(f"""UPDATE work_threads SET {field} = MAX({field}, COALESCE(
                (SELECT MAX(id) FROM {table} WHERE thread_id = ? AND (? IS NULL OR id <= ?)), 0))
                WHERE thread_id = ?""", (thread_id, cursor, cursor, thread_id))

    def start_turn(self, session_id: str, *, prompt=None, turn_id=None) -> None:
        self.ensure(session_id)
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM work_threads WHERE thread_id = ?", (session_id,)).fetchone()
            # App-server notifications and lifecycle hooks may describe the same turn.
            if turn_id and row["turn_id"] == turn_id:
                return
            self._mark_seen(db, session_id)
            now = self.store.clock()
            request = row["original_request"]
            title = row["title"]
            if not request and isinstance(prompt, str) and prompt.strip():
                request = prompt.strip()[:100000]
                if row["title_source"] == "auto" and title in {"New thread", "New session"}:
                    title = " ".join(request.split())[:100]
            db.execute("UPDATE work_threads SET original_request = ?, title = ?, turn_started_at = ?, turn_id = ?, turn_key = ?, updated_at = ? WHERE thread_id = ?",
                       (request, title, now, turn_id, turn_id or str(uuid.uuid4()), now, session_id))

    def is_browser_session(self, session_id: str) -> bool:
        with self.store._connection() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'browser_sessions'").fetchone():
                return False
            return db.execute("SELECT 1 FROM browser_sessions WHERE thread_id = ?", (session_id,)).fetchone() is not None

    def finish_turn(self, session_id: str, *, turn_id=None, status="completed") -> None:
        """Persist one completion per turn, independently of UI polling or process lifetime."""
        if status not in {"completed", "failed", "interrupted"}:
            return
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            turn = db.execute("SELECT turn_id, turn_key FROM work_threads WHERE thread_id = ?", (session_id,)).fetchone()
            if not turn or not turn["turn_key"] or (turn_id and turn_id != turn["turn_id"]):
                return
            now = self.store.clock()
            inserted = db.execute("INSERT OR IGNORE INTO turn_completions (thread_id, turn_key, status, completed_at) VALUES (?, ?, ?, ?)",
                                  (session_id, turn["turn_key"], status, now)).rowcount
            if inserted:
                db.execute("UPDATE work_threads SET updated_at = ? WHERE thread_id = ?", (now, session_id))

    def completion_cursor(self) -> int:
        with self.store._connection() as db:
            return db.execute("SELECT COALESCE(MAX(id), 0) FROM turn_completions").fetchone()[0]

    def completions_after(self, sequence: int) -> dict:
        self.organization.backfill()
        with self.store._connection() as db:
            latest = db.execute("SELECT COALESCE(MAX(id), 0) FROM turn_completions").fetchone()[0]
            rows = db.execute("""SELECT c.id, c.thread_id, c.status, c.completed_at,
                                        t.title, t.attention, p.name AS project_name,
                                        r.name AS repository_name, s.cwd
                                 FROM turn_completions c JOIN work_threads t ON t.thread_id = c.thread_id
                                 LEFT JOIN thread_organization o ON o.thread_id = t.thread_id
                                 LEFT JOIN named_projects p ON p.id = o.project_id
                                 LEFT JOIN work_repositories r ON r.id = o.repository_id
                                 JOIN sessions s ON s.session_id = c.thread_id
                                 WHERE c.id > ? AND c.id <= ? ORDER BY c.id LIMIT 100""", (sequence, latest)).fetchall()
            return {"seq": rows[-1]["id"] if rows else latest, "events": [dict(row) for row in rows]}

    def claim_notification(self, completion_id: int) -> bool:
        """Only one open UI tab may deliver a given desktop notification."""
        with self.store._connection() as db:
            return db.execute("UPDATE turn_completions SET notified_at = ? WHERE id = ? AND notified_at IS NULL AND status != 'interrupted'",
                              (self.store.clock(), completion_id)).rowcount == 1

    def capture_request(self, session_id: str, prompt: str) -> None:
        """Record the first accepted user request, without changing turn freshness."""
        request = prompt.strip()[:100000]
        with self.store._connection() as db:
            db.execute("UPDATE work_threads SET original_request = ?, title = CASE WHEN title_source = 'auto' AND title IN ('New session', 'New thread') THEN ? ELSE title END WHERE thread_id = ? AND original_request = ''",
                       (request, " ".join(request.split())[:100], session_id))

    def checkpoint(self, session_id: str, payload: dict, *, author="agent") -> dict:
        if not isinstance(payload, dict) or set(payload) - {"phase", "summary", "next_action", "next_actor", "links", "turn_id", "title"}:
            raise CoordinationError("Checkpoint accepts phase, summary, next_action, next_actor, links, turn_id, and optional title.")
        phase, actor = payload.get("phase"), payload.get("next_actor", "nobody")
        if not isinstance(phase, str) or phase not in PHASES:
            raise CoordinationError("Choose a supported checkpoint phase: " + ", ".join(sorted(PHASES)))
        if not isinstance(actor, str) or actor not in NEXT_ACTORS:
            raise CoordinationError("Next actor must be user, agent, external, or nobody.")
        summary = _text(payload.get("summary"), "Checkpoint summary", 2000)
        action = _text(payload.get("next_action", ""), "Next action", 1000, empty=True)
        title = _text(payload["title"], "Thread title", 160) if "title" in payload else None
        if bool(action) != (actor != "nobody"):
            raise CoordinationError("A next action needs an owner; use nobody with an empty next action.")
        thread = self.ensure(session_id)
        links = payload.get("links", [])
        if not isinstance(links, list) or len(links) > 100:
            raise CoordinationError("Links must be a list of at most 100 items.")
        normalized = [self._link(link, thread) for link in links]
        now = self.store.clock()
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT turn_id, title_source FROM work_threads WHERE thread_id = ?", (session_id,)).fetchone()
            turn_id = payload.get("turn_id", current["turn_id"])
            if turn_id is not None and (not isinstance(turn_id, str) or turn_id != current["turn_id"]):
                raise CoordinationError("Checkpoint belongs to an older or unknown turn.")
            latest = db.execute("SELECT * FROM thread_checkpoints WHERE thread_id = ? ORDER BY id DESC LIMIT 1", (session_id,)).fetchone()
            content = (phase, summary, action, actor)
            if latest and tuple(latest[key] for key in ("phase", "summary", "next_action", "next_actor")) == content and latest["turn_id"] == turn_id and latest["author"] == author:
                db.execute("UPDATE thread_checkpoints SET refreshed_at = ? WHERE id = ?", (now, latest["id"]))
            else:
                db.execute("INSERT INTO thread_checkpoints (thread_id, phase, summary, next_action, next_actor, session_id, turn_id, author, created_at, refreshed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           (session_id, *content, session_id, turn_id, author, now, now))
            for link in normalized:
                self._save_link(db, session_id, link)
            if title is not None and (author == "user" or current["title_source"] != "user"):
                source = "user" if author == "user" else "agent"
                db.execute("UPDATE work_threads SET title = ?, title_source = ? WHERE thread_id = ?", (title, source, session_id))
            db.execute("UPDATE work_threads SET updated_at = ? WHERE thread_id = ?", (now, session_id))
        return self.get(session_id, history=True)

    def _link(self, payload, thread):
        if not isinstance(payload, dict) or set(payload) - {"kind", "label", "target", "workspace_id", "project_id"}:
            raise CoordinationError("Link accepts kind, label, target, and optional workspace_id.")
        if "workspace_id" in payload and "project_id" in payload:
            raise CoordinationError("Use workspace_id; project_id is a legacy alias.")
        kind = payload.get("kind")
        if not isinstance(kind, str) or kind not in LINK_KINDS:
            raise CoordinationError("Choose a supported link kind.")
        target = _text(payload.get("target"), "Link target", 4000)
        label = _text(payload.get("label") or target, "Link label", 4000)
        try:
            parsed = urlsplit(target)
        except ValueError as exc:
            raise CoordinationError("Link URL is invalid.") from exc
        if parsed.scheme and parsed.scheme not in {"http", "https"}:
            raise CoordinationError("Use an HTTP(S) URL, file path, or issue identifier.")
        if parsed.scheme and not parsed.netloc:
            raise CoordinationError("Link URL must include a host.")
        if kind == "document" and not parsed.scheme:
            target = str((Path(thread["cwd"]) / target).resolve())
        project_id = payload.get("workspace_id", payload.get("project_id", thread["workspace_id"]))
        if not isinstance(project_id, str):
            raise CoordinationError("Link workspace must be a known workspace ID.")
        with self.store._connection() as db:
            if db.execute("SELECT id FROM work_projects WHERE id = ?", (project_id,)).fetchone() is None:
                raise CoordinationError("Link workspace must be a known workspace ID.")
        return kind, label, target, project_id

    @staticmethod
    def _save_link(db, thread_id, link):
        db.execute("INSERT INTO thread_links (thread_id, kind, label, target, project_id) VALUES (?, ?, ?, ?, ?) ON CONFLICT(thread_id, kind, target, project_id) DO UPDATE SET label = excluded.label",
                   (thread_id, *link))

    def add_link(self, thread_id: str, payload: dict) -> dict:
        link = self._link(payload, self.get(thread_id))
        with self.store._connection() as db:
            self._save_link(db, thread_id, link)
        return self.get(thread_id, history=True)

    def remove_link(self, thread_id: str, link_id: int) -> dict:
        self.get(thread_id)
        with self.store._connection() as db:
            db.execute("DELETE FROM thread_links WHERE thread_id = ? AND id = ?", (thread_id, link_id))
        return self.get(thread_id, history=True)

    def instructions(self, session_id: str | None = None) -> str:
        command = shlex.quote(str(Path(__file__).resolve().parents[1] / "agent-coord"))
        command += " --db " + shlex.quote(str(self.store.database_path))
        identity = shlex.quote(session_id) if session_id else '"$CODEX_THREAD_ID"'
        text = (
            "Maintain the Agent Coord work-thread checkpoint before returning control to the user, "
            "at a phase change, or after a significant result. Run " + command +
            " checkpoint --session-id " + identity + " --json '<object>'. "
            'The object has phase (discussion, investigation, planning, implementation, validation, deployment, finished), '
            'summary (1–2 factual sentences), next_action (empty if none), next_actor (user, agent, external, nobody), '
            'optional title (a concise 3–6 word thread name), and optional links [{"kind":"document","label":"Design","target":"docs/design.md"}]. '
            "Set title at the first meaningful checkpoint when title_source is auto. "
            "Keep the name stable: omit title on later checkpoints unless the thread purpose changes substantially. "
            "User-chosen titles (title_source user) take precedence and cannot be overwritten by agent checkpoints. "
            "Distinguish proposals, implemented changes, validation, and deployment. "
            'Set next_actor to user only when progress or completion requires a specific user answer, approval, decision, or action; describe it in next_action. '
            "Keep optional advice, invitations to continue, and nonblocking reminders in summary. "
            'When the requested work is complete and nothing remains, use phase finished, next_action "", and next_actor nobody. '
            "If work remains, keep its actual phase and assign any required next step to its actual owner. Ending an agent turn does not create a required user action. "
            "Record unresolved decisions without inventing follow-up work. Skip unchanged checkpoints. "
            "Use exact artifact references; link kinds are pull_request, document, issue, bead, branch, other. "
            "Preserve the original request; only move Now/Later/Archived when the user requests it. "
            "Beads is optional for this thread."
        )
        if session_id:
            thread = self.ensure(session_id)
            text += "\nThread context (saved data): " + json.dumps({
                "repository": thread["repository_name"], "project": thread["project_name"],
                "workspace": thread["cwd"], "attention": thread["attention"],
                "title": thread["title"], "title_source": thread["title_source"],
                "checkpoint": thread["checkpoint"], "links": thread["links"],
            })
        return text
