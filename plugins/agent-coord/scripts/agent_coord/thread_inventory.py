"""Per-read inventory data, fetched in batches without a persistent cache."""
from __future__ import annotations

import json
from collections import defaultdict

from .attention import AttentionStore, MODEL, POLICY_VERSION
from .organization import OrganizationStore
from .notification_policy import available_owners


class ThreadInventory:
    def __init__(self, threads, db, *, archived=False):
        self.threads = threads
        threads._wake_snoozes(db)
        self.selected = "SELECT thread_id FROM work_threads WHERE (attention = 'archived') = ?"
        self.params = (int(archived),)
        # Status and placement can change between queries. Keep every batch on
        # one SQLite snapshot; a normal read does not acquire the writer lock.
        writing = db.in_transaction
        if not writing:
            db.execute("BEGIN")
        self.rows, self.associations = self._base(db)
        if any(row["thread_id"] not in self.associations for row in self.rows):
            # A legacy writer created incomplete records after initialization.
            # Release the read snapshot before taking the writer lock, then
            # reload so concurrent placement changes cannot leave mixed data.
            if not writing:
                db.rollback()
                db.execute("BEGIN IMMEDIATE")
                self.rows, self.associations = self._base(db)
            for row in self.rows:
                thread_id = row["thread_id"]
                if thread_id not in self.associations:
                    OrganizationStore.ensure_thread(db, thread_id, row["cwd"])
                    self.associations[thread_id] = dict(OrganizationStore.association(db, thread_id))
        self.latest = {row["thread_id"]: dict(row) for row in db.execute("""
            SELECT c.* FROM thread_checkpoints c WHERE c.id IN (
                SELECT MAX(id) FROM thread_checkpoints WHERE thread_id IN (
        """ + self.selected + ") GROUP BY thread_id)", self.params)}
        self.notification_contexts = {row["thread_id"]: dict(row) for row in db.execute("""
            SELECT n.* FROM turn_notification_context n JOIN work_threads t
            ON t.thread_id = n.thread_id AND t.turn_key = n.turn_key
            WHERE (t.attention = 'archived') = ?""", self.params)}
        self.automatic_turns = {row[0] for row in db.execute("""SELECT DISTINCT t.thread_id
            FROM work_threads t JOIN messages m ON m.recipient_session_id = t.thread_id AND m.wake_turn_id = t.turn_id
            WHERE (t.attention = 'archived') = ?""", self.params)}
        self.prior = {row["thread_id"]: row["phase"] for row in db.execute("""
            SELECT c.thread_id, c.phase FROM thread_checkpoints c WHERE c.id IN (
                SELECT MAX(id) FROM thread_checkpoints WHERE phase != 'finished' AND thread_id IN (
        """ + self.selected + ") GROUP BY thread_id)", self.params)}
        self.completions = {row["thread_id"]: dict(row) for row in db.execute("""
            SELECT c.thread_id, c.id, c.status, c.completed_at FROM turn_completions c
            JOIN work_threads t ON t.thread_id = c.thread_id AND t.turn_key = c.turn_key
            WHERE (t.attention = 'archived') = ?
        """, self.params)}
        self.links = defaultdict(list)
        for row in db.execute("SELECT * FROM thread_links WHERE thread_id IN (" +
                              self.selected + ") ORDER BY id", self.params):
            self.links[row["thread_id"]].append(dict(row))
        self.classifications = {}
        for row in db.execute("""
            SELECT r.* FROM response_classification_cache r
            JOIN turn_completions c ON c.id = r.completion_id
            JOIN work_threads t ON t.thread_id = c.thread_id AND t.turn_key = c.turn_key
            WHERE (t.attention = 'archived') = ? AND r.model = ? AND r.policy_version = ?
        """, (*self.params, MODEL, POLICY_VERSION)):
            self.classifications[(row["completion_id"], row["checkpoint_id"], row["model"], row["policy_version"])] = row

    def _base(self, db):
        rows = db.execute(self.threads._SELECT +
            " WHERE (t.attention = 'archived') = ? ORDER BY t.updated_at DESC, t.thread_id",
            self.params).fetchall()
        associations = {}
        for row in db.execute("""
            SELECT o.thread_id, o.repository_id, r.name AS repository_name,
                   r.root AS repository_root, o.project_id, p.name AS project_name
            FROM thread_organization o
            LEFT JOIN work_repositories r ON r.id = o.repository_id
            LEFT JOIN named_projects p ON p.id = o.project_id
            WHERE o.thread_id IN (
        """ + self.selected + ")", self.params):
            association = dict(row)
            associations[association.pop("thread_id")] = association
        return rows, associations

    def completion(self, thread_id):
        row = self.completions.get(thread_id)
        return {key: row[key] for key in ("id", "status", "completed_at")} if row else None

    def classification(self, thread):
        if not thread["turn_completion"]:
            return None
        row = self.classifications.get(AttentionStore._key(thread))
        if row is None:
            return None
        result = {key: row[key] for key in ("completion_id", "checkpoint_id", "model", "policy_version",
                  "status", "choice", "confidence", "attempts", "error", "updated_at")}
        result["probabilities"] = json.loads(row["probabilities_json"]) if row["probabilities_json"] else None
        return result

    def load_runtime(self, db):
        """Load the app-only fields without starting or resuming providers."""
        self.notification_owners = available_owners(db, self.threads.store)
        self.sessions = {row["session_id"]: self.threads.store._row_to_session(row) for row in db.execute(
            "SELECT * FROM sessions WHERE session_id IN (" + self.selected + ")", self.params)}
        self.browser = {row[0] for row in db.execute(
            "SELECT thread_id FROM browser_sessions WHERE thread_id IN (" + self.selected + ")", self.params)}
        self.dispatchers = {row[0] for row in db.execute(
            "SELECT thread_id FROM dispatch_settings WHERE enabled != 0 AND thread_id IN (" + self.selected + ")", self.params)}
        self.delegated = {row[0] for row in db.execute("""SELECT d.child_session_id FROM delegations d
            JOIN work_threads p ON p.thread_id = d.parent_session_id WHERE d.child_session_id IS NOT NULL""")}
        self.titles = {row[0]: row[1] for row in db.execute("SELECT thread_id, title FROM work_threads")}
        self.forks = defaultdict(list)
        for row in db.execute("""SELECT thread_id, title, created_at, forked_from_turn_id, forked_from_thread_id
            FROM work_threads WHERE forked_from_thread_id IN (""" + self.selected +
            ") ORDER BY created_at, thread_id", self.params):
            fork = dict(row)
            self.forks[fork.pop("forked_from_thread_id")].append(fork)

    def read(self, db, row):
        return self.threads._read(db, row, inventory=self)
