"""Durable personal work threads. No issue tracker or running client is required."""
from __future__ import annotations

import json
import logging
import math
import shlex
import uuid
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from .context import caller_session, command_prefix
from .store import CoordinationError, derive_scope_cluster
from .organization import OrganizationStore, UNSET
from .attention import AttentionStore
from . import context_lifecycle
from . import notification_policy

PHASES = {"discussion", "investigation", "planning", "orchestrating", "implementation", "validation", "deployment", "finished"}
ATTENTION_STATES = {"now", "later", "archived"}
NEXT_ACTORS = {"user", "agent", "external", "nobody"}
LINK_KINDS = {"pull_request", "document", "issue", "bead", "branch", "other"}


def _text(value, label, limit, *, empty=False):
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        raise CoordinationError(f"{label} must be {'non-empty ' if not empty else ''}text of at most {limit} characters.")
    return value.strip()


def _domain(value):
    return _text(" ".join(_text(value, "Thread domain", 64).split()).casefold(), "Thread domain", 64)


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
                    domain TEXT, domain_source TEXT, scope_cluster TEXT,
                    attention TEXT NOT NULL DEFAULT 'now',
                    pinned INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    turn_started_at REAL, turn_id TEXT, turn_key TEXT,
                    seen_checkpoint_id INTEGER NOT NULL DEFAULT 0,
                    seen_completion_id INTEGER NOT NULL DEFAULT 0,
                    handled_checkpoint_id INTEGER NOT NULL DEFAULT 0,
                    handled_completion_id INTEGER NOT NULL DEFAULT 0
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
                CREATE TABLE IF NOT EXISTS thread_scratchpads (
                    thread_id TEXT PRIMARY KEY REFERENCES work_threads(thread_id),
                    text TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS project_scratchpads (
                    cwd TEXT PRIMARY KEY,
                    text TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS turn_completions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    thread_id TEXT NOT NULL REFERENCES work_threads(thread_id),
                    turn_key TEXT NOT NULL, status TEXT NOT NULL,
                    completed_at REAL NOT NULL, notified_at REAL,
                    UNIQUE(thread_id, turn_key)
                );
                CREATE TABLE IF NOT EXISTS turn_stop_requests (
                    thread_id TEXT NOT NULL REFERENCES work_threads(thread_id),
                    turn_id TEXT NOT NULL, requested_at REAL NOT NULL,
                    PRIMARY KEY(thread_id, turn_id)
                );
                CREATE TABLE IF NOT EXISTS snooze_notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    thread_id TEXT NOT NULL REFERENCES work_threads(thread_id),
                    snoozed_until REAL NOT NULL, notified_at REAL,
                    UNIQUE(thread_id, snoozed_until)
                );
            """)
            notification_policy.initialize(db)
            # Serialize upgrades across independently running terminal hooks.
            db.execute("BEGIN IMMEDIATE")
            checkpoint_columns = {row["name"] for row in db.execute("PRAGMA table_info(thread_checkpoints)")}
            for field in ("context_lifecycle_json", "context_lifecycle_turn_key"):
                if field not in checkpoint_columns:
                    db.execute(f"ALTER TABLE thread_checkpoints ADD COLUMN {field} TEXT")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(work_threads)")}
            for field in ("domain", "domain_source", "scope_cluster"):
                if field not in columns:
                    db.execute(f"ALTER TABLE work_threads ADD COLUMN {field} TEXT")
            # Include declarations made by an older runtime. Persist once so
            # releasing the lease or restarting does not erase the fallback.
            for row in db.execute("""SELECT t.thread_id, s.write_scope_json, s.cwd FROM work_threads t
                                     JOIN sessions s ON s.session_id = t.thread_id
                                     WHERE t.scope_cluster IS NULL AND s.write_scope_json != '[]'""").fetchall():
                self.record_scope(db, row["thread_id"], json.loads(row["write_scope_json"]), row["cwd"])
            if "project_cwd" not in columns:
                # Preserve the exact launch directory independently of mutable
                # execution cwd and the legacy repository-based artifact root.
                db.execute("ALTER TABLE work_threads ADD COLUMN project_cwd TEXT")
                db.execute("""UPDATE work_threads SET project_cwd =
                              (SELECT cwd FROM sessions WHERE session_id = thread_id)""")
            if "snoozed_until" not in columns:
                db.execute("ALTER TABLE work_threads ADD COLUMN snoozed_until REAL")
            if "deferred_at" not in columns:
                db.execute("ALTER TABLE work_threads ADD COLUMN deferred_at REAL")
                # Existing Later cards remain set aside; only future activity
                # should surface when upgrading to the new attention behavior.
                db.execute("UPDATE work_threads SET deferred_at = ? WHERE attention = 'later'", (self.store.clock(),))
            for field in ("forked_from_thread_id", "forked_from_turn_id"):
                if field not in columns:
                    db.execute(f"ALTER TABLE work_threads ADD COLUMN {field} TEXT")
            db.execute("CREATE INDEX IF NOT EXISTS threads_fork_source_idx ON work_threads(forked_from_thread_id)")
            # Reading a response or finishing work never established that the
            # user handled it. Existing open responses therefore remain pending.
            for field in ("handled_checkpoint_id", "handled_completion_id"):
                if field not in columns:
                    db.execute(f"ALTER TABLE work_threads ADD COLUMN {field} INTEGER NOT NULL DEFAULT 0")
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
        self.attention = AttentionStore(store)
        from .activity import ActivityStore
        self.activity = ActivityStore(store)

    @staticmethod
    def record_scope(db, thread_id, scopes, cwd):
        cluster = derive_scope_cluster(scopes, cwd)
        db.execute("""UPDATE work_threads SET scope_cluster = ?,
                      domain = CASE WHEN domain_source IS NULL OR domain_source = 'derived' THEN ? ELSE domain END,
                      domain_source = CASE WHEN domain_source IS NULL OR domain_source = 'derived' THEN ? ELSE domain_source END
                      WHERE thread_id = ?""",
                   (cluster, cluster, "derived" if cluster is not None else None, thread_id))

    @staticmethod
    def _save_domain(db, thread_id, domain, author):
        if domain is None:
            # User reset returns to the last scope fallback, or an unset value.
            db.execute("""UPDATE work_threads SET domain = scope_cluster,
                          domain_source = CASE WHEN scope_cluster IS NULL THEN NULL ELSE 'derived' END
                          WHERE thread_id = ?""", (thread_id,))
        else:
            db.execute("""UPDATE work_threads SET domain = ?, domain_source = ?
                          WHERE thread_id = ? AND (? = 'user' OR domain_source IS NULL OR domain_source != 'user')""",
                       (domain, author, thread_id, author))

    def known_domains(self, cwd: str) -> list[str]:
        """Bounded explicit vocabulary from open threads in this workspace."""
        with self.store._connection() as db:
            rows = db.execute("""SELECT t.domain, COUNT(*) AS uses FROM work_threads t
                                 JOIN sessions s ON s.session_id = t.thread_id
                                 WHERE s.cwd = ? AND t.attention != 'archived'
                                   AND t.domain_source IN ('agent', 'user') AND t.domain IS NOT NULL
                                 GROUP BY t.domain ORDER BY uses DESC, t.domain LIMIT 20""", (cwd,)).fetchall()
        return [row["domain"] for row in rows]

    def ensure(self, session_id: str, *, project_cwd: str | None = None) -> dict:
        session = self.store.get_session(session_id)
        launch_directory = str(Path(project_cwd or session["cwd"]).expanduser().resolve())
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
                db.execute("INSERT OR IGNORE INTO work_threads (thread_id, project_id, title, title_source, created_at, updated_at, project_cwd) VALUES (?, ?, ?, ?, ?, ?, ?)",
                           (session_id, project_id, title, source, now, now, launch_directory))
            # Older installed clients can still create rows after migration.
            db.execute("UPDATE work_threads SET project_cwd = ? WHERE thread_id = ? AND project_cwd IS NULL",
                       (launch_directory, session_id))
            self.organization.ensure_thread(db, session_id, session["cwd"])
            if session["bead_id"]:
                self._save_link(db, session_id, ("bead", session["bead_id"], session["bead_id"], existing["project_id"] if existing else project_id))
        return self.get(session_id)

    def _read(self, db, row, *, history=False, inventory=None):
        result = dict(row)
        from .execution_state import read_many
        result["execution"] = (inventory.executions[row["thread_id"]] if inventory is not None else
                               read_many(self.store, db, [row["thread_id"]])[row["thread_id"]])
        context = (inventory.notification_contexts.get(row["thread_id"]) if inventory is not None else
                   db.execute("SELECT * FROM turn_notification_context WHERE thread_id = ? AND turn_key = ?",
                              (row["thread_id"], row["turn_key"])).fetchone())
        result["notification_context"] = dict(context) if context else None
        result["automatic_turn"] = (row["thread_id"] in inventory.automatic_turns if inventory is not None else
            db.execute("SELECT 1 FROM messages WHERE recipient_session_id = ? AND wake_turn_id = ? LIMIT 1",
                       (row["thread_id"], row["turn_id"])).fetchone() is not None)
        result["project_cwd"] = result["project_cwd"] or result["cwd"]
        deadline = row["snoozed_until"]
        result["snoozed"] = bool(deadline is not None and row["attention"] == "later" and deadline > self.store.clock())
        result["snooze_due"] = bool(deadline is not None and row["attention"] == "now" and deadline <= self.store.clock())
        result["pinned"] = bool(row["pinned"])
        latest = (inventory.latest.get(row["thread_id"]) if inventory is not None else
                  db.execute("SELECT * FROM thread_checkpoints WHERE thread_id = ? ORDER BY id DESC LIMIT 1", (row["thread_id"],)).fetchone())
        result["checkpoint"] = context_lifecycle.checkpoint_record(latest) if latest else None
        result["checkpoint_stale"] = bool(latest and row["turn_started_at"] and latest["refreshed_at"] < row["turn_started_at"])
        completion = inventory.completion(row["thread_id"]) if inventory is not None else db.execute("""SELECT id, status, completed_at FROM turn_completions
                                   WHERE thread_id = ? AND turn_key = ?""",
                                (row["thread_id"], row["turn_key"])).fetchone()
        result["turn_completion"] = dict(completion) if completion else None
        result["response_classification"] = (inventory.classification(result) if inventory is not None
                                              else self.attention.read(db, result))
        # Completing a reply does not advance an investigation to delivery. Keep
        # its last real activity, including checkpoints written by older clients.
        phase = latest["phase"] if latest else "new"
        classification = result["response_classification"]
        choice = classification["choice"] if classification and classification["status"] == "classified" else None
        if phase == "finished":
            if inventory is not None:
                prior = inventory.prior.get(row["thread_id"], "new")
            else:
                previous = db.execute("""SELECT phase FROM thread_checkpoints WHERE thread_id = ?
                                         AND phase != 'finished' ORDER BY id DESC LIMIT 1""", (row["thread_id"],)).fetchone()
                prior = previous["phase"] if previous else "new"
            if choice == "findings":
                phase = prior if prior in {"discussion", "investigation", "planning", "orchestrating"} else "investigation"
            elif choice in {"update", "blocked", "review"}:
                phase = prior
            elif choice != "done" and latest["author"] != "user" and prior not in {"implementation", "validation", "deployment"}:
                phase = prior
        result["work_phase"] = "investigation" if phase == "discussion" else phase
        result["unread_result"] = bool(completion and completion["status"] != "interrupted"
                                       and completion["id"] > row["seen_completion_id"])
        result["unread"] = bool(result["unread_result"] or (latest and latest["id"] > row["seen_checkpoint_id"]))
        result["unhandled_response"] = bool(
            (completion and completion["status"] != "interrupted" and completion["id"] > row["handled_completion_id"])
            # A changed checkpoint after completion is a new result too. An
            # unchanged heartbeat keeps its ID and does not revive attention.
            # Legacy threads may have a saved result without turn receipts.
            or (latest and not result["checkpoint_stale"] and latest["id"] > row["handled_checkpoint_id"]
                and (not row["turn_key"] or completion and completion["status"] != "interrupted")))
        result["links"] = [dict(link) for link in (inventory.links[row["thread_id"]] if inventory is not None else
                           db.execute("SELECT * FROM thread_links WHERE thread_id = ? ORDER BY id", (row["thread_id"],)))]
        if history:
            result["checkpoints"] = [context_lifecycle.checkpoint_record(cp) for cp in db.execute("SELECT * FROM thread_checkpoints WHERE thread_id = ? ORDER BY id DESC", (row["thread_id"],))]
        if inventory is not None:
            return self.organization.describe(db, result, association=inventory.associations.get(row["thread_id"]))
        return self.organization.describe(db, result)

    _SELECT = """SELECT t.*, p.name AS project_name, p.root AS project_root,
                        s.cwd, s.client, s.ended_at, s.turn_active
                 FROM work_threads t JOIN work_projects p ON p.id = t.project_id
                 JOIN sessions s ON s.session_id = t.thread_id"""

    def get(self, thread_id: str, *, history=False) -> dict:
        with self.store._connection() as db:
            self._wake_snoozes(db)
            row = db.execute(self._SELECT + " WHERE t.thread_id = ?", (thread_id,)).fetchone()
            if row is None:
                raise CoordinationError("Work thread not found.")
            return self._read(db, row, history=history)

    def scratchpad(self, thread_id: str, text=UNSET) -> dict:
        """Keep user notes separate from agent context and thread activity."""
        if text is not UNSET and (not isinstance(text, str) or len(text) > 100_000):
            raise CoordinationError("Scratchpad must be text of at most 100,000 characters.")
        with self.store._connection() as db:
            if not db.execute("SELECT 1 FROM work_threads WHERE thread_id = ?", (thread_id,)).fetchone():
                raise CoordinationError("Work thread not found.")
            if text is not UNSET:
                db.execute("""INSERT INTO thread_scratchpads (thread_id, text) VALUES (?, ?)
                              ON CONFLICT(thread_id) DO UPDATE SET text = excluded.text""", (thread_id, text))
            row = db.execute("SELECT text FROM thread_scratchpads WHERE thread_id = ?", (thread_id,)).fetchone()
            return {"text": row["text"] if row else ""}

    def project_scratchpad(self, cwd: str, text=UNSET) -> dict:
        """One note per canonical project directory, independent of its threads."""
        cwd = str(Path(cwd).expanduser().resolve())
        if text is not UNSET and (not isinstance(text, str) or len(text) > 100_000):
            raise CoordinationError("Scratchpad must be text of at most 100,000 characters.")
        with self.store._connection() as db:
            if text is not UNSET:
                db.execute("""INSERT INTO project_scratchpads (cwd, text) VALUES (?, ?)
                              ON CONFLICT(cwd) DO UPDATE SET text = excluded.text""", (cwd, text))
            row = db.execute("SELECT text FROM project_scratchpads WHERE cwd = ?", (cwd,)).fetchone()
            return {"text": row["text"] if row else ""}

    def list(self, *, archived=False, cwd=None, repository_id=UNSET, project_id=UNSET) -> list[dict]:
        from .thread_inventory import ThreadInventory
        with self.store._connection() as db:
            inventory = ThreadInventory(self, db, archived=archived)
            root = project_root(cwd) if cwd is not None else None
            threads = [inventory.read(db, row) for row in inventory.rows if root is None or row["project_root"] == root]
            return [thread for thread in threads
                    if (repository_id is UNSET or thread["repository_id"] == repository_id)
                    and (project_id is UNSET or thread["project_id"] == project_id)]

    def record_fork(self, thread_id: str, source: dict, *, turn_id: str | None = None) -> None:
        """Copy context and lineage, leaving all execution and receipt state fresh."""
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE work_threads SET project_cwd = ? WHERE thread_id = ?",
                       (source["project_cwd"], thread_id))
            db.execute("""UPDATE work_threads SET forked_from_thread_id = ?, forked_from_turn_id = ?,
                          original_request = ?, title_source = 'auto' WHERE thread_id = ?""",
                       (source["thread_id"], turn_id, source["original_request"], thread_id))
            self.organization.update(db, thread_id, repository_id=source["repository_id"], project_id=source["project_id"])

    def _wake_snoozes(self, db):
        # Persist wake-up on observation, including the first read after restart.
        # Keep the deadline until an explicit user action acknowledges the reminder.
        now = self.store.clock()
        # Most overview refreshes only observe existing state. Even a no-op
        # INSERT/UPDATE takes SQLite's sole writer lock, once for every thread
        # read; streaming refreshes must not compete with active agent writes.
        pending = db.execute("""SELECT 1 FROM work_threads t
            WHERE t.attention IN ('now', 'later') AND t.snoozed_until <= ?
              AND (t.attention = 'later' OR NOT EXISTS (
                SELECT 1 FROM snooze_notifications n WHERE n.thread_id = t.thread_id
                  AND n.snoozed_until = t.snoozed_until)) LIMIT 1""", (now,)).fetchone()
        if pending is None:
            return
        # The reminder and placement change commit together. Include already
        # returned reminders from older runtimes; the unique receipt survives
        # rereads, competing processes and restarts without creating fake turns.
        db.execute("""INSERT OR IGNORE INTO snooze_notifications (thread_id, snoozed_until)
                      SELECT thread_id, snoozed_until FROM work_threads
                      WHERE attention IN ('now', 'later') AND snoozed_until <= ?
                        AND NOT EXISTS (SELECT 1 FROM snooze_notifications n
                                        WHERE n.thread_id = work_threads.thread_id
                                          AND n.snoozed_until = work_threads.snoozed_until)""", (now,))
        db.execute("""UPDATE work_threads SET attention = 'now'
                      WHERE attention = 'later' AND snoozed_until <= ?""", (now,))

    def snooze_notifications(self, *, include_claimed=False) -> list[dict]:
        with self.store._connection() as db:
            self._wake_snoozes(db)
            rows = db.execute("""SELECT n.id, n.thread_id, n.snoozed_until, 'snooze' AS status,
                                         t.title, p.name AS project_name, r.name AS repository_name, s.cwd
                                  FROM snooze_notifications n JOIN work_threads t ON t.thread_id = n.thread_id
                                  LEFT JOIN thread_organization o ON o.thread_id = t.thread_id
                                  LEFT JOIN named_projects p ON p.id = o.project_id
                                  LEFT JOIN work_repositories r ON r.id = o.repository_id
                                  JOIN sessions s ON s.session_id = n.thread_id
                                  WHERE t.attention = 'now' AND t.snoozed_until = n.snoozed_until
                                    AND n.snoozed_until <= ?
                                    AND (? OR n.notified_at IS NULL) ORDER BY n.id""", (self.store.clock(), include_claimed)).fetchall()
            return [dict(row) for row in rows]

    def claim_snooze_notification(self, notification_id: int) -> bool:
        with self.store._connection() as db:
            # Check the current snooze inside the atomic claim: a replacement,
            # resume, placement change or user message invalidates old receipts.
            return db.execute("""UPDATE snooze_notifications SET notified_at = ?
                                  WHERE id = ? AND notified_at IS NULL AND snoozed_until <= ? AND EXISTS (
                                      SELECT 1 FROM work_threads t
                                      WHERE t.thread_id = snooze_notifications.thread_id
                                        AND t.attention = 'now'
                                        AND t.snoozed_until = snooze_notifications.snoozed_until)""",
                              (self.store.clock(), notification_id, self.store.clock())).rowcount == 1

    def update(self, thread_id: str, *, title=None, domain=UNSET, attention=None, pinned=UNSET, seen=False,
               seen_checkpoint_id=None, seen_completion_id=None, handled=False,
               handled_checkpoint_id=None, handled_completion_id=None, repository_id=UNSET, project_id=UNSET,
               snoozed_until=UNSET, resume_snooze=UNSET) -> dict:
        self.get(thread_id)
        if title is not None:
            title = _text(title, "Thread title", 160)
        if domain is not UNSET and domain is not None:
            domain = _domain(domain)
        if attention is not None and (not isinstance(attention, str) or attention not in ATTENTION_STATES):
            raise CoordinationError("Attention must be now, later, or archived.")
        if pinned is not UNSET and not isinstance(pinned, bool):
            raise CoordinationError("Pinned must be true or false.")
        if not seen and (seen_checkpoint_id is not None or seen_completion_id is not None):
            raise CoordinationError("Read markers require seen: true.")
        if not isinstance(handled, bool):
            raise CoordinationError("Handled must be true or false.")
        if handled and (handled_checkpoint_id is None or handled_completion_id is None):
            raise CoordinationError("Handling requires the displayed checkpoint and completion IDs.")
        if not handled and (handled_checkpoint_id is not None or handled_completion_id is not None):
            raise CoordinationError("Handled markers require handled: true.")
        if snoozed_until is not UNSET or resume_snooze is not UNSET:
            if attention is not None or (snoozed_until is not UNSET and resume_snooze is not UNSET):
                raise CoordinationError("Choose either snooze, resume, or a placement change.")
            value = snoozed_until if snoozed_until is not UNSET else resume_snooze
            if type(value) not in (int, float) or not 0 < value <= 253402300799 or not math.isfinite(value):
                raise CoordinationError("Snooze time must be a valid Unix timestamp.")
            if snoozed_until is not UNSET and snoozed_until <= self.store.clock():
                raise CoordinationError("Choose a snooze time in the future.")
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if snoozed_until is not UNSET or resume_snooze is not UNSET:
                current = db.execute("SELECT attention, snoozed_until FROM work_threads WHERE thread_id = ?", (thread_id,)).fetchone()
                if current["attention"] == "archived":
                    raise CoordinationError("Reopen this thread before snoozing or resuming it.")
                if resume_snooze is not UNSET and current["snoozed_until"] != resume_snooze:
                    raise CoordinationError("This snooze changed. Refresh the thread before resuming it.")
                db.execute("UPDATE work_threads SET attention = ?, snoozed_until = ? WHERE thread_id = ?",
                           ("later" if snoozed_until is not UNSET else "now",
                            snoozed_until if snoozed_until is not UNSET else None, thread_id))
            if handled:
                # This receipt acknowledges the displayed result. It does not
                # resolve its blocker, answer provider input, or resume work.
                self._mark_receipts(db, thread_id, handled_checkpoint_id, handled_completion_id, kind="handled")
            if seen:
                self._mark_seen(db, thread_id, seen_checkpoint_id, seen_completion_id)
            self.organization.update(db, thread_id, repository_id=repository_id, project_id=project_id)
            if title is not None:
                db.execute("UPDATE work_threads SET title = ?, title_source = 'user' WHERE thread_id = ?", (title, thread_id))
            if domain is not UNSET:
                self._save_domain(db, thread_id, domain, "user")
            if attention is not None:
                db.execute("UPDATE work_threads SET attention = ?, snoozed_until = NULL, deferred_at = ? WHERE thread_id = ?",
                           (attention, self.store.clock() if attention == "later" else None, thread_id))
            if pinned is not UNSET:
                db.execute("UPDATE work_threads SET pinned = ? WHERE thread_id = ?", (int(pinned), thread_id))
        return self.get(thread_id, history=True)

    @staticmethod
    def _mark_seen(db, thread_id, checkpoint_id=None, completion_id=None):
        ThreadStore._mark_receipts(db, thread_id, checkpoint_id, completion_id, kind="seen")

    @staticmethod
    def _mark_receipts(db, thread_id, checkpoint_id=None, completion_id=None, *, kind):
        # UI receipts name only the records actually displayed. A reply arriving
        # between rendering and this update must remain unread and unhandled.
        for table, field, cursor in (("thread_checkpoints", kind + "_checkpoint_id", checkpoint_id),
                                     ("turn_completions", kind + "_completion_id", completion_id)):
            if cursor is not None and (type(cursor) is not int or cursor < 0 or (cursor > 0 and not db.execute(
                    f"SELECT 1 FROM {table} WHERE thread_id = ? AND id = ?", (thread_id, cursor)).fetchone())):
                label = "Read" if kind == "seen" else "Handled"
                raise CoordinationError(f"{label} markers must identify a displayed checkpoint or completion in this thread.")
            db.execute(f"""UPDATE work_threads SET {field} = MAX({field}, COALESCE(
                (SELECT MAX(id) FROM {table} WHERE thread_id = ? AND (? IS NULL OR id <= ?)), 0))
                WHERE thread_id = ?""", (thread_id, cursor, cursor, thread_id))

    def user_message(self, session_id: str) -> None:
        """Resume a parked thread when a user message is accepted."""
        from .thread_control import cancel_pending
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            cancel_pending(db, session_id, self.store.clock())
            db.execute("""UPDATE work_threads SET attention = 'now', snoozed_until = NULL, updated_at = ?
                          WHERE thread_id = ? AND (attention = 'later' OR (attention = 'now' AND snoozed_until IS NOT NULL))""",
                       (self.store.clock(), session_id))

    def user_turn_context(self, session_id: str, turn_id: str) -> None:
        """An accepted direct follow-up takes precedence over automatic origin."""
        self.remember_notification_context(session_id, turn_id, {"origin": "user", "owner_thread_id": session_id})

    def remember_notification_context(self, session_id: str, turn_id: str, context: dict) -> None:
        with self.store._connection() as db:
            notification_policy.record_context(db, session_id, turn_id, context)
            notification_policy.snapshot_checkpoint(db, session_id, turn_id)

    def start_turn(self, session_id: str, *, prompt=None, turn_id=None, notification_context=None, occurred_at=None) -> None:
        from .thread_control import cancel_pending
        self.ensure(session_id)
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM work_threads WHERE thread_id = ?", (session_id,)).fetchone()
            if notification_context and turn_id:
                notification_policy.record_context(db, session_id, turn_id, notification_context)
            # App-server notifications and lifecycle hooks may describe the same turn.
            if turn_id and row["turn_id"] == turn_id:
                return
            self._mark_seen(db, session_id)
            self._mark_receipts(db, session_id, kind="handled")
            now = self.store.clock() if occurred_at is None else occurred_at
            cancel_pending(db, session_id, now)
            request = row["original_request"]
            title = row["title"]
            if not request and isinstance(prompt, str) and prompt.strip():
                request = prompt.strip()[:100000]
                if row["title_source"] == "auto" and title in {"New thread", "New session"}:
                    title = " ".join(request.split())[:100]
            turn_key = turn_id or str(uuid.uuid4())
            db.execute("UPDATE work_threads SET original_request = ?, title = ?, turn_started_at = ?, turn_id = ?, turn_key = ?, updated_at = ? WHERE thread_id = ?",
                       (request, title, now, turn_id, turn_key, now, session_id))
            self._record_turn_activity(db, "turn_started", session_id, turn_key, turn_id=turn_id,
                                       started_at=now, occurred_at=now, context=notification_context)

    def _record_turn_activity(self, db, kind, session_id, turn_key, **metadata):
        """Discard failed telemetry writes without undoing lifecycle state."""
        try:
            db.execute("SAVEPOINT turn_activity")
            try:
                if kind == "turn_started" and metadata.get("context") is None and metadata.get("turn_id"):
                    context = db.execute("SELECT origin FROM turn_notification_context WHERE thread_id=? AND turn_key=?",
                                         (session_id, metadata["turn_id"])).fetchone()
                    metadata["context"] = dict(context) if context else None
                self.activity.record_turn(db, kind, session_id, turn_key, **metadata)
            except Exception:
                db.execute("ROLLBACK TO SAVEPOINT turn_activity")
                raise
            finally:
                db.execute("RELEASE SAVEPOINT turn_activity")
        except Exception:
            logging.getLogger(__name__).exception("Could not record %s activity for thread %s", kind, session_id)

    def is_browser_session(self, session_id: str) -> bool:
        with self.store._connection() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'browser_sessions'").fetchone():
                return False
            return db.execute("SELECT 1 FROM browser_sessions WHERE thread_id = ?", (session_id,)).fetchone() is not None

    def request_stop(self, session_id: str, turn_id: str) -> None:
        """Save user intent before interrupting native execution, scoped to one turn.

        Unlike the inbox pause this survives user continuation and provider
        disconnects while completion bookkeeping is still pending.
        """
        with self.store._connection() as db:
            db.execute("INSERT OR IGNORE INTO turn_stop_requests VALUES (?, ?, ?)",
                       (session_id, turn_id, self.store.clock()))

    def stop_requested(self, session_id: str, turn_id: str) -> bool:
        with self.store._connection() as db:
            return db.execute("SELECT 1 FROM turn_stop_requests WHERE thread_id = ? AND turn_id = ?",
                              (session_id, turn_id)).fetchone() is not None

    def finish_turn(self, session_id: str, *, turn_id=None, status="completed", occurred_at=None) -> None:
        """Persist one completion per turn, independently of UI polling or process lifetime."""
        if status not in {"completed", "failed", "interrupted"}:
            return
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            turn = db.execute("SELECT turn_id, turn_key, turn_started_at FROM work_threads WHERE thread_id = ?", (session_id,)).fetchone()
            if not turn or not turn["turn_key"] or (turn_id and turn_id != turn["turn_id"]):
                return
            now = self.store.clock() if occurred_at is None else occurred_at
            inserted = db.execute("INSERT OR IGNORE INTO turn_completions (thread_id, turn_key, status, completed_at) VALUES (?, ?, ?, ?)",
                                  (session_id, turn["turn_key"], status, now)).rowcount
            if inserted:
                self._record_turn_activity(db, "turn_ended", session_id, turn["turn_key"],
                                           turn_id=turn["turn_id"], started_at=turn["turn_started_at"],
                                           occurred_at=now, status=status)
                db.execute("""UPDATE turn_notification_context SET checkpoint_id = (
                    SELECT id FROM thread_checkpoints WHERE thread_id = ? AND turn_id IS ? ORDER BY id DESC LIMIT 1)
                    WHERE thread_id = ? AND turn_key = ?""", (session_id, turn["turn_id"], session_id, turn["turn_key"]))
                db.execute("UPDATE work_threads SET updated_at = ? WHERE thread_id = ?", (now, session_id))

    def completion_cursor(self) -> int:
        with self.store._connection() as db:
            return db.execute("SELECT COALESCE(MAX(id), 0) FROM turn_completions").fetchone()[0]

    def completions_after(self, sequence: int) -> dict:
        with self.store._connection() as db:
            latest = db.execute("SELECT COALESCE(MAX(id), 0) FROM turn_completions").fetchone()[0]
            rows = db.execute("""SELECT c.id, c.thread_id, c.status, c.completed_at,
                                        t.title, t.attention, t.snoozed_until, p.name AS project_name,
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
        if not isinstance(payload, dict) or set(payload) - {"phase", "summary", "next_step", "next_action", "next_actor", "links", "turn_id", "title", "domain", "context_lifecycle"}:
            raise CoordinationError("Checkpoint accepts phase, summary, next_step, links, turn_id, title, domain, context_lifecycle, and legacy next_action/next_actor.")
        report = context_lifecycle.validate_report(payload["context_lifecycle"]) if "context_lifecycle" in payload else UNSET
        legacy_step = {"next_action", "next_actor"} & payload.keys()
        if "next_step" in payload and legacy_step:
            raise CoordinationError("Use next_step or legacy next_action/next_actor, not both.")
        step = payload.get("next_step")
        if "next_step" in payload and step is not None:
            if not isinstance(step, dict) or set(step) != {"actor", "action"}:
                raise CoordinationError("Next step must contain actor and action, or be null to clear it.")
        title = _text(payload["title"], "Thread title", 160) if "title" in payload else None
        domain = payload.get("domain", UNSET)
        if domain is not UNSET and not (domain is None and author == "user"):
            domain = _domain(domain)
        thread = self.ensure(session_id)
        links = payload.get("links", [])
        if not isinstance(links, list) or len(links) > 100:
            raise CoordinationError("Links must be a list of at most 100 items.")
        normalized = [self._link(link, thread) for link in links]
        now = self.store.clock()
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT turn_id, turn_key, title_source FROM work_threads WHERE thread_id = ?", (session_id,)).fetchone()
            turn_id = payload.get("turn_id", current["turn_id"])
            if turn_id is not None and (not isinstance(turn_id, str) or turn_id != current["turn_id"]):
                raise CoordinationError("Checkpoint belongs to an older or unknown turn.")
            latest = db.execute("SELECT * FROM thread_checkpoints WHERE thread_id = ? ORDER BY id DESC LIMIT 1", (session_id,)).fetchone()
            if report is not UNSET and report is not None:
                context_lifecycle.validate_assignment(db, session_id, report)
            lifecycle_json, lifecycle_turn = None, current["turn_key"]
            if report is not UNSET:
                lifecycle_json = context_lifecycle.encode(report)
            elif latest and latest["context_lifecycle_turn_key"] == current["turn_key"]:
                lifecycle_json, lifecycle_turn = latest["context_lifecycle_json"], latest["context_lifecycle_turn_key"]
            if latest is None and not {"phase", "summary"} <= payload.keys():
                raise CoordinationError("The first checkpoint requires phase and summary.")
            phase = payload.get("phase", latest["phase"] if latest else None)
            summary = _text(payload.get("summary", latest["summary"] if latest else None), "Checkpoint summary", 2000)
            action, actor = (latest["next_action"], latest["next_actor"]) if latest else ("", "nobody")
            if "next_step" in payload:
                action, actor = (step["action"], step["actor"]) if step is not None else ("", "nobody")
                if step is not None and actor == "nobody":
                    raise CoordinationError("Next step actor must be user, agent, or external; use null to clear it.")
            elif legacy_step:
                # Retain legacy defaults for old clients, including next_actor=nobody alone.
                action, actor = payload.get("next_action", ""), payload.get("next_actor", "nobody")
            if not isinstance(phase, str) or phase not in PHASES:
                raise CoordinationError("Choose a supported checkpoint phase: " + ", ".join(sorted(PHASES)))
            if not isinstance(actor, str) or actor not in NEXT_ACTORS:
                raise CoordinationError("Next actor must be user, agent, external, or nobody.")
            action = _text(action, "Next action", 1000, empty=True)
            if bool(action) != (actor != "nobody"):
                raise CoordinationError("A next action needs an owner; use nobody with an empty next action.")
            content = (phase, summary, action, actor, lifecycle_json, lifecycle_turn)
            if latest and tuple(latest[key] for key in ("phase", "summary", "next_action", "next_actor", "context_lifecycle_json", "context_lifecycle_turn_key")) == content and latest["turn_id"] == turn_id and latest["author"] == author:
                db.execute("UPDATE thread_checkpoints SET refreshed_at = ? WHERE id = ?", (now, latest["id"]))
            else:
                db.execute("INSERT INTO thread_checkpoints (thread_id, phase, summary, next_action, next_actor, context_lifecycle_json, context_lifecycle_turn_key, session_id, turn_id, author, created_at, refreshed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           (session_id, *content, session_id, turn_id, author, now, now))
            for link in normalized:
                self._save_link(db, session_id, link)
            if title is not None and (author == "user" or current["title_source"] == "auto"):
                source = "user" if author == "user" else "agent"
                db.execute("UPDATE work_threads SET title = ?, title_source = ? WHERE thread_id = ?", (title, source, session_id))
            if domain is not UNSET:
                self._save_domain(db, session_id, domain, "user" if author == "user" else "agent")
            db.execute("UPDATE work_threads SET updated_at = ? WHERE thread_id = ?", (now, session_id))
        from .launch_metrics import record_action
        record_action(self.store, "checkpoint")
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
        if parsed.scheme == "agentcoord":
            from .navigation import NavigationStore
            if set(dict(parse_qsl(parsed.query, keep_blank_values=True))) & {"request", "window"}:
                raise CoordinationError("Invalid Ribbon Field route.")
            target = NavigationStore(self.store).resolve(target)["url"]
        elif parsed.scheme and parsed.scheme not in {"http", "https"}:
            raise CoordinationError("Use an HTTP(S) URL, file path, or issue identifier.")
        elif parsed.scheme and not parsed.netloc:
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

    def instructions(self, session_id: str | None = None, *, caller_context: bool = False, command: str | None = None) -> str:
        command = command or ("agent-coord" if caller_context else command_prefix(self.store.database_path))
        identity = ""
        if session_id and not caller_context and caller_session() != session_id:
            identity = " --session-id " + shlex.quote(session_id)
        text = (
            "Maintain the Agent Coord work-thread checkpoint before returning control to the user, "
            "at a phase change, or after a significant result. Prefer the update_checkpoint tool when available. "
            "Run " + command + " checkpoint" + identity + " --json '<object>' as the CLI fallback. "
            'Send only changed fields; omitted fields keep their saved values. The first checkpoint requires phase and summary. '
            'Fields are phase (discussion, investigation, planning, orchestrating, implementation, validation, deployment, finished), '
            'summary (1–2 factual sentences), next_step ({"actor":"user|agent|external","action":"required action"}, or null to clear), '
            'optional title (a concise 3–6 word thread name), optional domain (a short assignment category), and optional links [{"kind":"document","label":"Design","target":"docs/design.md"}]. '
            'If the MCP tool has no bound caller identity, use the CLI from your current tool shell instead of supplying an ID from history. Success returns only status and checkpoint_id. '
            "Set title at the first meaningful checkpoint when title_source is auto. "
            "Keep established conversation names stable across assignments: omit title on later checkpoints. Use thread update --title only for a deliberate rename. "
            "User-chosen titles (title_source user) take precedence and cannot be overwritten by agent checkpoints. "
            "Set domain once per assignment and omit it on later checkpoints until the work moves; reuse a workspace domain unless the work is clearly elsewhere, and preserve a user-chosen domain (domain_source user). "
            "Use summary for the changing current/latest work, keeping its first sentence concise and descriptive. Update it when a new assignment starts; describe completed or failed results as outcomes, never as ongoing work. Preserve useful progress detail in the following sentence. Distinguish proposals, implemented changes, validation, and deployment. "
            'Set next_step.actor to user only when progress or completion requires a specific user answer, approval, decision, or action; describe it in next_step.action. '
            "Keep optional advice, invitations to continue, and nonblocking reminders in summary. "
            'Use phase for the underlying activity, not whether you finished replying: an answered investigation stays investigation, and a completed plan stays planning. '
            'Use orchestrating for ongoing routing and coordination of specialist agents, including while a specialist implements or deploys; it does not itself require user action. '
            'Use finished only after actually delivering the requested implementation or execution, including validation/deployment if requested, with nothing remaining; clear next_step with null. '
            'For a status question during deployment, keep deployment. Required user review belongs in validation with next_step.actor user. '
            "If work remains, keep its actual phase and assign any required next step to its actual owner. Ending an agent turn does not create a required user action. "
            "Record unresolved decisions without inventing follow-up work. Skip unchanged checkpoints. "
            "Use exact artifact references; link kinds are pull_request, document, issue, bead, branch, other. "
            "Preserve the original request; only move Now/Later/Archived when the user requests it. "
            "Beads is optional for this thread."
        )
        if session_id:
            thread = self.ensure(session_id)
            text = (
                f"Your current Agent Coord session ID is {session_id}. "
                "Caller-owned coordination actions must use this identity; omit caller ID arguments when possible. "
                "Session IDs in inherited messages or tool calls are historical, not your current identity.\n"
            ) + text
            if thread.get("forked_from_thread_id"):
                text = (
                    f"This is a fork of {thread['forked_from_thread_id']}. "
                    "Do not use the parent's identity for checkpoints, reservations, inbox delivery, or sending messages.\n"
                ) + text
            with self.store._connection() as db:
                if context_lifecycle.is_app_agent(db, session_id):
                    text += "\n" + context_lifecycle.REPORTING_INSTRUCTIONS
            text += "\nWorkspace domains: " + json.dumps(self.known_domains(thread["cwd"]))
            text += "\nThread context (saved data): " + json.dumps({
                "project": thread["project_cwd"], "group": thread["project_name"],
                "workspace": thread["cwd"], "attention": thread["attention"],
                "title": thread["title"], "title_source": thread["title_source"],
                "domain": thread["domain"], "domain_source": thread["domain_source"],
                "scope_cluster": thread["scope_cluster"],
                "checkpoint": thread["checkpoint"], "links": thread["links"],
            })
        return text
