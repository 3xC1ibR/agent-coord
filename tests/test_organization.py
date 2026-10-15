from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))

from agent_coord.organization import OrganizationStore, repository_root
from agent_coord.store import CoordinationError, CoordinationStore
from agent_coord.views import ViewStore


class OrganizationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / "backend"
        (self.repo / ".git").mkdir(parents=True)
        self.store = CoordinationStore(self.root / "state.sqlite3")
        self.store.register(session_id="code", client="codex", cwd=str(self.repo))
        self.store.register(session_id="idea", client="codex", cwd=str(self.root))
        self.organization = OrganizationStore(self.store)

    def association(self, thread_id):
        with self.store._connection() as db:
            return dict(db.execute("SELECT * FROM thread_organization WHERE thread_id = ?", (thread_id,)).fetchone())

    def test_only_git_repositories_are_detected_and_worktrees_share_identity(self):
        self.assertIsNone(repository_root(str(self.root)))
        self.assertEqual(repository_root(str(self.repo / "src")), str(self.repo))
        worktree = self.root / "feature"
        worktree.mkdir()
        gitdir = self.repo / ".git/worktrees/feature"
        gitdir.mkdir(parents=True)
        (gitdir / "commondir").write_text("../..")
        (worktree / ".git").write_text("gitdir: " + str(gitdir))
        self.store.register(session_id="worktree", client="codex", cwd=str(worktree))
        self.organization.backfill()
        self.assertEqual(repository_root(str(worktree)), str(self.repo))
        self.assertEqual(self.association("code")["repository_id"], self.association("worktree")["repository_id"])
        self.assertIsNone(self.association("idea")["repository_id"])
        self.assertIsNone(self.association("code")["project_id"])

    def test_invalid_git_pointer_does_not_create_a_repository(self):
        folder = self.root / "plain"
        folder.mkdir()
        (folder / ".git").write_text("not a Git pointer")
        self.assertIsNone(repository_root(str(folder)))
        (folder / ".git").write_text("gitdir: missing")
        self.assertIsNone(repository_root(str(folder)))

    def test_all_four_association_states_and_assignment_do_not_change_workspace(self):
        project = self.organization.create_project("Anthropic migration")
        repo = self.association("code")["repository_id"]
        for repository_id, project_id in ((repo, project["id"]), (repo, None), (None, project["id"]), (None, None)):
            with self.subTest(repository_id=repository_id, project_id=project_id):
                with self.store._connection() as db:
                    self.organization.update(db, "code", repository_id=repository_id, project_id=project_id)
                self.assertEqual(self.association("code"), {"thread_id": "code", "repository_id": repository_id, "project_id": project_id})
                self.assertEqual(self.store.get_session("code")["cwd"], str(self.repo))

    def test_cleared_associations_survive_reopen_and_legacy_reregistration(self):
        with self.store._connection() as db:
            self.organization.update(db, "code", repository_id=None)
        self.store.register(session_id="code", client="codex", cwd=str(self.repo))
        OrganizationStore(CoordinationStore(self.store.database_path))
        self.assertIsNone(self.association("code")["repository_id"])

    def test_projects_span_repositories_and_are_reused_by_name(self):
        other = self.root / "frontend"
        (other / ".git").mkdir(parents=True)
        self.store.register(session_id="frontend", client="codex", cwd=str(other))
        self.organization.backfill()
        project = self.organization.create_project("Anthropic migration")
        self.assertEqual(self.organization.create_project("  ANTHROPIC MIGRATION  "), project)
        with self.store._connection() as db:
            for thread_id in ("code", "frontend", "idea"):
                self.organization.update(db, thread_id, project_id=project["id"])
        self.assertEqual(self.association("code")["project_id"], self.association("frontend")["project_id"])
        self.assertNotEqual(self.association("code")["repository_id"], self.association("frontend")["repository_id"])
        self.assertIsNone(self.association("idea")["repository_id"])

    def test_legacy_group_ownership_uses_first_members_original_project(self):
        legacy = self.organization.create_project("Launch")
        empty = self.organization.create_project("Empty")
        scoped = self.organization.create_project("Scoped", str(self.root))
        for group, thread_id in ((legacy, "code"), (legacy, "idea"), (scoped, "code")):
            with self.store._connection() as db:
                self.organization.update(db, thread_id, project_id=group["id"])
        with self.store._connection() as db:
            self.organization.update(db, "code", project_id=legacy["id"])
            db.execute("UPDATE work_threads SET created_at = 0 WHERE thread_id = 'code'")
        # Execution may leave the original project before the migration runs.
        self.store.register(session_id="code", client="codex", cwd=str(self.root))
        before = {id: self.store.threads.get(id) for id in ("code", "idea")}
        reopened = OrganizationStore(CoordinationStore(self.store.database_path))
        groups = {group["id"]: group for group in reopened.list()["projects"]}
        self.assertEqual(groups[legacy["id"]]["cwd"], str(self.repo))
        self.assertEqual(groups[scoped["id"]]["cwd"], str(self.root))
        self.assertIsNone(groups[empty["id"]]["cwd"])
        self.assertEqual(reopened.create_project("launch", str(self.repo))["id"], legacy["id"])
        for id, thread in before.items():
            self.assertEqual(self.store.threads.get(id), thread)
        self.assertEqual(OrganizationStore(self.store).list(), reopened.list())

    def test_legacy_group_migration_preserves_ids_when_scoped_name_exists(self):
        legacy = self.organization.create_project("Launch")
        scoped = self.organization.create_project("Launch", str(self.repo))
        with self.store._connection() as db:
            self.organization.update(db, "code", project_id=legacy["id"])
        reopened = OrganizationStore(CoordinationStore(self.store.database_path))
        groups = {group["id"]: group for group in reopened.list()["projects"]}
        self.assertEqual(groups[legacy["id"]]["cwd"], str(self.repo))
        self.assertEqual(groups[scoped["id"]], scoped)
        self.assertEqual(self.association("code")["project_id"], legacy["id"])

    def test_removing_group_preserves_threads_and_clears_only_matching_saved_filters(self):
        group = self.organization.create_project("Launch", str(self.repo))
        other = self.organization.create_project("Other", str(self.repo))
        for thread_id, attention in (("code", "later"), ("idea", "archived")):
            self.store.threads.start_turn(thread_id, prompt="Keep this work", turn_id="one")
            self.store.threads.checkpoint(thread_id, {"phase": "implementation", "summary": "Running work."})
            self.store.threads.update(thread_id, project_id=group["id"], attention=attention)
            self.store.threads.add_link(thread_id, {"kind": "document", "target": "plan.md"})
        views = ViewStore(self.store)
        saved = views.create({"name": "Review", "filters": {"project": group["id"], "show": "attention", "search": "work"}})
        unrelated = views.create({"name": "Other", "filters": {"project": other["id"]}})
        before = {id: self.store.threads.get(id, history=True) for id in ("code", "idea")}
        sessions = {id: self.store.get_session(id) for id in before}
        self.assertEqual(self.organization.get_project(group["id"])["thread_count"], 2)
        removed = self.organization.delete_project(group["id"], expected_count=2)
        self.assertEqual(removed["id"], group["id"])
        for id, thread in before.items():
            self.assertEqual(self.store.threads.get(id, history=True), {**thread, "project_id": None, "project_name": None})
            self.assertEqual(self.store.get_session(id), sessions[id])
        updated = next(view for view in views.list() if view["id"] == saved["id"])
        self.assertEqual(updated, {**saved, "filters": {**saved["filters"], "project": ""}, "version": saved["version"] + 1})
        self.assertIn(unrelated, views.list())
        self.assertEqual(self.organization.list()["projects"], [other])
        self.assertNotEqual(self.organization.create_project("Launch", str(self.repo))["id"], group["id"])

    def test_group_removal_rechecks_membership_and_invalid_requests_are_atomic(self):
        group = self.organization.create_project("Empty", str(self.repo))
        self.assertEqual(self.organization.get_project(group["id"])["thread_count"], 0)
        with self.store._connection() as db:
            self.organization.update(db, "code", project_id=group["id"])
        for count in (0, -1, True, "1", None):
            with self.subTest(count=count), self.assertRaises(CoordinationError):
                self.organization.delete_project(group["id"], expected_count=count)
            self.assertEqual(self.association("code")["project_id"], group["id"])
        self.organization.delete_project(group["id"], expected_count=1)
        for operation in (lambda: self.organization.get_project(group["id"]),
                          lambda: self.organization.delete_project(group["id"], expected_count=0)):
            with self.assertRaises(CoordinationError):
                operation()
        empty = self.organization.create_project("Empty", str(self.repo))
        self.assertEqual(self.organization.delete_project(empty["id"], expected_count=0)["thread_count"], 0)

    def test_invalid_assignments_are_atomic(self):
        before = self.association("code")
        for value in ("missing", "", 1, [], {}):
            with self.subTest(value=value), self.assertRaises(CoordinationError):
                with self.store._connection() as db:
                    self.organization.update(db, "code", repository_id=None, project_id=value)
            self.assertEqual(self.association("code"), before)
        for name in (None, " ", 2, "x" * 161):
            with self.assertRaises(CoordinationError):
                self.organization.create_project(name)

    def test_migration_preserves_history_links_and_attention(self):
        self.store.threads.start_turn("code", prompt="Migrate the provider.", turn_id="first")
        self.store.threads.checkpoint("code", {"phase": "planning", "summary": "Plan captured."})
        self.store.threads.add_link("code", {"kind": "document", "target": "docs/plan.md"})
        self.store.threads.update("code", attention="later")
        before = self.store.threads.get("code", history=True)
        with self.store._connection() as db:
            db.execute("DELETE FROM thread_organization")
            db.execute("DELETE FROM work_repositories")
        OrganizationStore(self.store)
        self.assertEqual(self.store.threads.get("code", history=True), before)
        self.assertIsNone(self.association("code")["project_id"])
        self.assertEqual(len(self.organization.list()["repositories"]), 1)
        self.assertEqual(self.organization.list()["projects"], [])
        with self.store._connection() as db:
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])


if __name__ == "__main__":
    unittest.main()
