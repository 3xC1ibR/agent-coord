from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PLUGIN_SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"
sys.path.insert(0, str(PLUGIN_SCRIPTS))

from agent_coord.workspaces import matches_workspace, workspace_choices


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.projects = self.root / "projects"
        self.projects.mkdir()

    def test_parent_directory_includes_repositories_and_plain_folders(self):
        repo = self.projects / "repo"
        (repo / ".git").mkdir(parents=True)
        folder = self.projects / "folder"
        folder.mkdir()
        for path in (self.projects, repo, folder, repo / "src"):
            self.assertTrue(matches_workspace(str(path), str(self.projects)))
        for path in (self.root, self.root / "projects-other"):
            self.assertFalse(matches_workspace(str(path), str(self.projects)))

    def test_linked_worktrees_stay_in_the_repository_filter(self):
        repo = self.projects / "repo"
        gitdir = repo / ".git/worktrees/feature"
        gitdir.mkdir(parents=True)
        (gitdir / "commondir").write_text("../..")
        worktree = self.root / "feature"
        worktree.mkdir()
        (worktree / ".git").write_text("gitdir: " + str(gitdir))
        self.assertTrue(matches_workspace(str(worktree), str(repo)))
        self.assertTrue(matches_workspace(str(repo), str(worktree)))

    def test_unrelated_plain_folders_do_not_match_each_other(self):
        one, two = self.projects / "one", self.projects / "two"
        one.mkdir()
        two.mkdir()
        self.assertFalse(matches_workspace(str(one), str(two)))
        self.assertTrue(matches_workspace(str(one / "nested"), str(one)))

    def test_choices_are_shallow_sorted_deduplicated_and_include_known_paths(self):
        for name in ("zeta", "Alpha", ".hidden", "Alpha/nested"):
            (self.projects / name).mkdir(parents=True, exist_ok=True)
        (self.projects / "file.txt").write_text("not a workspace")
        known = [str(self.projects / "Alpha"), str(self.projects / "Alpha/nested"), str(self.root / "missing")]
        choices = workspace_choices(str(self.projects), known, workspace=str(self.projects))
        self.assertEqual([item["name"] for item in choices], ["projects", "Alpha", "nested", "zeta"])
        self.assertEqual(choices[1]["cwd"], str(self.projects / "Alpha"))
        self.assertNotIn("nested", [item["name"] for item in workspace_choices(str(self.projects))])

    def test_symlink_escape_is_excluded_but_internal_alias_is_deduplicated(self):
        outside = self.root / "outside"
        outside.mkdir()
        inside = self.projects / "inside"
        inside.mkdir()
        (self.projects / "escape").symlink_to(outside, target_is_directory=True)
        (self.projects / "alias").symlink_to(inside, target_is_directory=True)
        (self.projects / "loop").symlink_to(self.projects / "loop")
        self.assertFalse(matches_workspace(str(self.projects / "escape"), str(self.projects)))
        choices = workspace_choices(str(self.projects), [str(outside)], workspace=str(self.projects))
        self.assertEqual([item["cwd"] for item in choices], [str(self.projects), str(inside)])
        self.assertIn(str(outside), [item["cwd"] for item in workspace_choices(str(self.projects))])

    def test_unreadable_directory_still_offers_known_workspaces(self):
        known = self.projects / "known"
        known.mkdir()
        with patch.object(Path, "iterdir", side_effect=PermissionError):
            choices = workspace_choices(str(self.projects), [str(known)], workspace=str(self.projects))
        self.assertEqual([item["cwd"] for item in choices], [str(self.projects), str(known)])


if __name__ == "__main__":
    unittest.main()
