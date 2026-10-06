from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))

from agent_coord.cli import _parser, run
from agent_coord.store import CoordinationError, CoordinationStore
from agent_coord.threads import project_root


class WorkThreadTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.now = 1000.0
        self.store = CoordinationStore(self.root / "state.sqlite3", clock=lambda: self.now)
        self.store.register(session_id="conversation", client="codex", cwd=str(self.root))
        self.threads = self.store.threads

    def save(self, **changes):
        return self.threads.checkpoint("conversation", {
            "phase": "investigation", "summary": "Two options remain.",
            "next_action": "Choose an approach.", "next_actor": "user", **changes,
        })

    def test_first_request_survives_later_turns_and_user_title(self):
        self.threads.start_turn("conversation", prompt="Investigate the database writes.", turn_id="first")
        self.threads.update("conversation", title="Database performance", attention="later")
        self.threads.start_turn("conversation", prompt="Try a different approach.", turn_id="second")
        thread = self.threads.get("conversation")
        self.assertEqual(thread["original_request"], "Investigate the database writes.")
        self.assertEqual(thread["title"], "Database performance")
        self.assertEqual(thread["attention"], "later")

    def test_checkpoint_title_names_thread_and_survives_later_turns(self):
        request = "Can you investigate the database writes and compare our options?"
        self.threads.start_turn("conversation", prompt=request, turn_id="first")
        named = self.save(title="  Database write performance  ")
        self.assertEqual(named["title"], "Database write performance")
        self.assertEqual(named["title_source"], "agent")
        self.assertEqual(named["original_request"], request)
        self.assertEqual(named["attention"], "now")
        self.threads.start_turn("conversation", prompt="Try the second option.", turn_id="second")
        self.save(summary="The second option is faster.")
        reopened = CoordinationStore(self.store.database_path)
        thread = reopened.threads.get("conversation")
        self.assertEqual(thread["title"], "Database write performance")
        self.assertEqual(thread["title_source"], "agent")
        self.assertEqual(thread["original_request"], request)

    def test_agent_can_deliberately_rename_its_title_without_duplicate_checkpoints(self):
        first = self.save(title="Database write performance")
        changed = self.save(title="Database connection pooling")
        self.assertEqual(changed["title"], "Database connection pooling")
        self.assertEqual(changed["checkpoint"]["id"], first["checkpoint"]["id"])
        self.assertEqual(len(self.save(title="Database connection pooling")["checkpoints"]), 1)

    def test_user_title_wins_over_agent_checkpoints_after_reopen(self):
        self.save(title="Database write performance")
        self.threads.update("conversation", title="My database investigation", attention="later")
        reopened = CoordinationStore(self.store.database_path)
        thread = reopened.threads.checkpoint("conversation", {
            "phase": "finished", "summary": "Measurements complete.", "title": "Agent replacement",
        })
        self.assertEqual(thread["title"], "My database investigation")
        self.assertEqual(thread["title_source"], "user")
        self.assertEqual(thread["checkpoint"]["summary"], "Measurements complete.")
        self.assertEqual(thread["attention"], "later")

    def test_user_checkpoint_title_and_initial_session_name_are_protected(self):
        self.threads.checkpoint("conversation", {
            "phase": "discussion", "summary": "A user note.", "title": "My investigation",
        }, author="user")
        self.assertEqual(self.save(title="Agent replacement")["title"], "My investigation")
        self.store.register(session_id="named", client="codex", cwd=str(self.root), name="My named session")
        thread = self.threads.checkpoint("named", {
            "phase": "discussion", "summary": "An agent note.", "title": "Agent replacement",
        })
        self.assertEqual(thread["title"], "My named session")
        self.assertEqual(thread["title_source"], "user")

    def test_user_placeholder_title_is_preserved_when_request_arrives(self):
        for capture in (False, True):
            with self.subTest(capture=capture):
                session_id = f"placeholder-{capture}"
                self.store.register(session_id=session_id, client="codex", cwd=str(self.root))
                self.threads.update(session_id, title="New thread")
                if capture:
                    self.threads.capture_request(session_id, "The actual request.")
                else:
                    self.threads.start_turn(session_id, prompt="The actual request.")
                thread = self.threads.get(session_id)
                self.assertEqual(thread["title"], "New thread")
                self.assertEqual(thread["title_source"], "user")
                self.assertEqual(thread["original_request"], "The actual request.")

    def test_invalid_checkpoint_title_cannot_partially_save(self):
        self.save(title="Valid title")
        before = self.threads.get("conversation", history=True)
        for title in (None, "", "  ", 1, True, [], {}, "x" * 161):
            with self.subTest(title=title), self.assertRaisesRegex(CoordinationError, "Thread title"):
                self.save(title=title, summary="Should not be saved.", links=[{"kind": "issue", "target": "issue-1"}])
            self.assertEqual(self.threads.get("conversation", history=True), before)
        self.assertEqual(self.save(title="x" * 160)["title"], "x" * 160)

    def test_legacy_titles_migrate_without_overwriting_custom_names(self):
        request = "Investigate  the\ndatabase writes. " + "A" * 120
        self.threads.start_turn("conversation", prompt=request, turn_id="first")
        self.save()
        self.threads.update("conversation", attention="later")
        self.store.register(session_id="custom", client="codex", cwd=str(self.root))
        self.threads.start_turn("custom", prompt="The original request.")
        self.threads.update("custom", title="My custom title")
        self.store.register(session_id="named", client="codex", cwd=str(self.root), name="An explicit name")
        self.threads.start_turn("named", prompt="An explicit name")
        self.store.register(session_id="empty", client="codex", cwd=str(self.root))
        with self.store._connection() as db:
            db.execute("ALTER TABLE work_threads DROP COLUMN title_source")
        reopened = CoordinationStore(self.store.database_path)
        self.assertEqual(reopened.threads.get("conversation")["title_source"], "auto")
        self.assertEqual(reopened.threads.get("empty")["title_source"], "auto")
        for session_id in ("custom", "named"):
            before = reopened.threads.get(session_id)
            self.assertEqual(before["title_source"], "user")
            after = reopened.threads.checkpoint(session_id, {
                "phase": "discussion", "summary": "New checkpoint.", "title": "Agent replacement",
            })
            self.assertEqual(after["title"], before["title"])
            self.assertEqual(after["original_request"], before["original_request"])
        migrated = reopened.threads.checkpoint("conversation", {
            "phase": "investigation", "summary": "Progress.", "title": "Database write performance",
        })
        self.assertEqual(migrated["title"], "Database write performance")
        self.assertEqual(migrated["original_request"], request)
        self.assertEqual(migrated["attention"], "later")
        self.assertEqual(len(migrated["checkpoints"]), 2)
        self.assertEqual(CoordinationStore(self.store.database_path).threads.get("conversation")["title_source"], "agent")

    def test_checkpoint_instructions_include_title_and_ownership(self):
        self.threads.update("conversation", title="My chosen title")
        instructions = self.threads.instructions("conversation")
        self.assertIn("optional title", instructions)
        self.assertIn("first meaningful checkpoint", instructions)
        self.assertIn("omit title on later checkpoints", instructions)
        context = json.loads(instructions.split("Thread context (saved data): ", 1)[1])
        self.assertEqual(context["title"], "My chosen title")
        self.assertEqual(context["title_source"], "user")

    def test_checkpoint_and_later_survive_runtime_end_and_database_reopen(self):
        self.threads.start_turn("conversation", prompt="Investigate.", turn_id="first")
        self.save()
        self.now += 2
        self.save(summary="The second approach needs a benchmark.")
        self.threads.update("conversation", attention="later")
        self.store.end_work("conversation")
        self.store.end_session("conversation")
        reopened = CoordinationStore(self.store.database_path, clock=lambda: self.now)
        reopened.register(session_id="conversation", client="codex", cwd=str(self.root))
        thread = reopened.threads.get("conversation", history=True)
        self.assertEqual(thread["attention"], "later")
        self.assertEqual(len(thread["checkpoints"]), 2)
        self.assertEqual(thread["checkpoint"]["phase"], "investigation")
        self.assertEqual(thread["checkpoint"]["summary"], "The second approach needs a benchmark.")

    def test_new_turn_marks_old_checkpoint_stale_until_refreshed(self):
        self.threads.start_turn("conversation", turn_id="first")
        self.save()
        self.now += 1
        self.threads.start_turn("conversation", turn_id="second")
        self.assertTrue(self.threads.get("conversation")["checkpoint_stale"])
        self.now += 1
        self.save()
        self.assertFalse(self.threads.get("conversation")["checkpoint_stale"])
        self.assertEqual(self.threads.get("conversation")["checkpoint"]["turn_id"], "second")

    def test_repeated_notifications_do_not_make_a_checkpoint_stale(self):
        self.threads.start_turn("conversation", turn_id="first")
        self.save()
        self.now += 3
        self.threads.start_turn("conversation", turn_id="first")
        self.assertFalse(self.threads.get("conversation")["checkpoint_stale"])

    def test_repeated_checkpoint_is_idempotent_and_read_state_is_independent(self):
        first = self.save()["checkpoint"]["id"]
        self.assertTrue(self.threads.get("conversation")["unread"])
        self.threads.update("conversation", seen=True)
        self.now += 1
        repeat = self.save()
        self.assertEqual(repeat["checkpoint"]["id"], first)
        self.assertFalse(repeat["unread"])
        changed = self.save(summary="A new finding.")
        self.assertTrue(changed["unread"])
        self.assertEqual(len(changed["checkpoints"]), 2)

    def test_stale_turn_cannot_overwrite_checkpoint(self):
        self.threads.start_turn("conversation", turn_id="current")
        self.save(title="Current investigation")
        with self.assertRaisesRegex(CoordinationError, "older"):
            self.save(turn_id="previous", summary="An old result.", title="Stale title")
        self.assertEqual(self.threads.get("conversation")["checkpoint"]["summary"], "Two options remain.")
        self.assertEqual(self.threads.get("conversation")["title"], "Current investigation")

    def test_invalid_checkpoint_and_links_are_atomic(self):
        self.save(title="Original title")
        with self.assertRaises(CoordinationError):
            self.save(title="Invalid replacement", summary="Must not replace the previous result.", links=[
                {"kind": "issue", "target": "issue-1"},
                {"kind": "document", "target": "javascript:alert(1)"},
            ])
        thread = self.threads.get("conversation", history=True)
        self.assertEqual(len(thread["checkpoints"]), 1)
        self.assertEqual(thread["links"], [])
        self.assertEqual(thread["title"], "Original title")

    def test_invalid_checkpoint_shapes_cannot_mutate_attention(self):
        for changes in [{"phase": []}, {"summary": ""}, {"next_actor": "unknown"},
                        {"next_actor": "nobody"}, {"links": {}}, {"attention": "archived"}]:
            with self.subTest(changes=changes), self.assertRaises(CoordinationError):
                self.save(**changes)
        self.assertEqual(self.threads.get("conversation")["attention"], "now")

    def test_links_are_multiple_deduplicated_and_paths_retain_workspace(self):
        payload = {"kind": "document", "label": "Design", "target": "docs/design.md"}
        self.threads.add_link("conversation", payload)
        self.threads.add_link("conversation", {**payload, "label": "Updated design"})
        self.threads.add_link("conversation", {"kind": "pull_request", "target": "https://example.com/repo/pull/1"})
        links = self.threads.get("conversation")["links"]
        self.assertEqual(len(links), 2)
        self.assertEqual(links[0]["target"], str(self.root / "docs/design.md"))
        self.assertEqual(links[0]["label"], "Updated design")
        self.assertNotIn("provider", links[0])
        self.threads.remove_link("conversation", links[0]["id"])
        self.assertEqual(len(self.threads.get("conversation")["links"]), 1)

    def test_repositories_disambiguate_equal_names_and_issue_identifiers(self):
        for session, root in [("one", self.root / "a/service"), ("two", self.root / "b/service")]:
            root.mkdir(parents=True)
            (root / ".git").mkdir()
            self.store.register(session_id=session, client="codex", cwd=str(root))
            self.threads.add_link(session, {"kind": "bead", "target": "work-1"})
        one, two = self.threads.get("one"), self.threads.get("two")
        self.assertEqual(one["repository_name"], two["repository_name"])
        self.assertNotEqual(one["repository_id"], two["repository_id"])
        self.assertNotEqual(one["links"][0]["workspace_id"], two["links"][0]["workspace_id"])
        self.assertEqual([t["thread_id"] for t in self.threads.list(cwd=str(self.root / "a/service"))], ["one"])

    def test_worktrees_group_under_one_repository_and_keep_working_directories(self):
        repo, worktree = self.root / "repo", self.root / "worktree"
        gitdir = repo / ".git/worktrees/feature"
        gitdir.mkdir(parents=True)
        worktree.mkdir()
        (worktree / ".git").write_text("gitdir: " + str(gitdir))
        (gitdir / "commondir").write_text("../..")
        for session, path in [("main", repo), ("feature", worktree)]:
            self.store.register(session_id=session, client="codex", cwd=str(path))
        main, feature = self.threads.get("main"), self.threads.get("feature")
        self.assertEqual(main["repository_id"], feature["repository_id"])
        self.assertIsNotNone(main["repository_id"])
        self.assertEqual(feature["cwd"], str(worktree))
        self.assertEqual(project_root(str(worktree)), str(repo))

    def test_checkpoint_cli_needs_no_beads_git_or_running_client(self):
        self.store.end_session("conversation")
        payload = {"phase": "finished", "summary": "Question answered.", "next_actor": "nobody", "title": "Database write performance"}
        args = _parser().parse_args(["--db", str(self.store.database_path), "checkpoint", "--session-id", "conversation", "--json", json.dumps(payload)])
        with patch("subprocess.run", side_effect=AssertionError("No external tools needed")):
            result = run(args)
        self.assertEqual(result["checkpoint"]["summary"], "Question answered.")
        self.assertEqual(result["title"], "Database write performance")
        self.assertEqual(result["title_source"], "agent")
        self.assertIsNone(self.store.get_session("conversation")["bead_id"])
        self.assertEqual(self.store.get_session("conversation")["presence"], "offline")


if __name__ == "__main__":
    unittest.main()
