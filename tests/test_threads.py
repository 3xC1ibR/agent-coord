from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))

from agent_coord.cli import _parser, run
from agent_coord.store import CoordinationError, CoordinationStore, derive_scope_cluster
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

    def test_domain_is_optional_sticky_and_can_follow_a_new_assignment(self):
        thread = self.threads.get("conversation")
        self.assertIsNone(thread["domain"])
        self.assertIsNone(thread["domain_source"])
        self.assertIsNone(thread["scope_cluster"])
        self.save(title="Product development", domain="  WEB  ")
        self.threads.start_turn("conversation", prompt="Continue the implementation.", turn_id="second")
        thread = self.save(summary="Continued implementation.")
        self.assertEqual((thread["domain"], thread["domain_source"]), ("web", "agent"))
        self.save(domain="Backend", title="Another assignment")
        reopened = CoordinationStore(self.store.database_path).threads.get("conversation")
        self.assertEqual((reopened["domain"], reopened["domain_source"]), ("backend", "agent"))
        self.assertEqual(reopened["title"], "Product development")
        self.assertEqual(reopened["original_request"], "Continue the implementation.")

    def test_user_domain_is_protected_and_reset_permits_agent_reporting(self):
        self.save(domain="web")
        self.threads.update("conversation", domain="  User   Interface ")
        self.store.begin_work(session_id="conversation", scopes=["backend/**"])
        thread = self.save(domain="backend", summary="Saved despite protected domain.")
        self.assertEqual((thread["domain"], thread["domain_source"]), ("user interface", "user"))
        self.assertEqual(thread["checkpoint"]["summary"], "Saved despite protected domain.")
        self.assertEqual(thread["scope_cluster"], "backend")
        reset = self.threads.update("conversation", domain=None)
        self.assertEqual((reset["domain"], reset["domain_source"]), ("backend", "derived"))
        self.assertEqual(self.save(domain="infra")["domain_source"], "agent")
        self.threads.checkpoint("conversation", {"phase": "discussion", "summary": "My note.", "domain": "docs"}, author="user")
        self.assertEqual(self.save(domain="web")["domain"], "docs")
        self.threads.checkpoint("conversation", {"phase": "discussion", "summary": "Reset.", "domain": None}, author="user")
        self.assertEqual(self.save(domain="web")["domain"], "web")

    def test_reset_without_scope_leaves_domain_unset_and_omission_preserves_override(self):
        self.threads.update("conversation", domain="research")
        self.threads.update("conversation", title="Research conversation")
        self.assertEqual(self.save()["domain_source"], "user")
        reset = self.threads.update("conversation", domain=None)
        self.assertIsNone(reset["domain"])
        self.assertIsNone(reset["domain_source"])
        self.assertEqual(self.save(domain="docs")["domain_source"], "agent")

    def test_invalid_domain_cannot_partially_save(self):
        self.save(domain="web")
        before = self.threads.get("conversation", history=True)
        for domain in (None, "", "  ", 1, True, [], {}, "x" * 65, "ß" * 64):
            with self.subTest(domain=domain):
                with self.assertRaisesRegex(CoordinationError, "Thread domain"):
                    self.save(domain=domain, summary="Invalid change.", links=[{"kind": "issue", "target": "issue-1"}])
                self.assertEqual(self.threads.get("conversation", history=True), before)
        for domain in ("", False, [], "x" * 65):
            with self.assertRaisesRegex(CoordinationError, "Thread domain"):
                self.threads.update("conversation", domain=domain, title="Must not change")
            self.assertEqual(self.threads.get("conversation", history=True), before)
        self.assertEqual(self.save(domain="x" * 64)["domain"], "x" * 64)

    def test_scope_fallback_survives_end_work_and_follows_new_scopes(self):
        self.store.begin_work(session_id="conversation", scopes=["src/web/app.js", "src/web/index.html"])
        self.store.end_work("conversation")
        reopened = CoordinationStore(self.store.database_path)
        thread = reopened.threads.get("conversation")
        self.assertEqual((thread["domain"], thread["domain_source"], thread["scope_cluster"]), ("src/web", "derived", "src/web"))
        reopened.begin_work(session_id="conversation", scopes=["backend/**"])
        self.assertEqual(reopened.threads.get("conversation")["domain"], "backend")
        reopened.threads.checkpoint("conversation", {"phase": "implementation", "summary": "Explicit label.", "domain": "api"})
        reopened.begin_work(session_id="conversation", scopes=["infra/**"])
        thread = reopened.threads.get("conversation")
        self.assertEqual((thread["domain"], thread["domain_source"], thread["scope_cluster"]), ("api", "agent", "infra"))

    def test_scope_clusters_are_structural_and_bounded_by_the_workspace(self):
        (self.root / "web").mkdir()
        (self.root / "web" / "LICENSE").write_text("license")
        cases = [([], None), (["src/web/*.js", "src/web/styles/**"], "src/web"),
                 (["src/web/button*.js"], "src/web"), (["src/**", "tests/**"], "."),
                 (["README.md"], "."), (["web"], "web"), (["web/LICENSE"], "web"), (["**/*"], "."),
                 (["test*.py"], "."), (["src/web*/**"], "src")]
        for scopes, expected in cases:
            with self.subTest(scopes=scopes):
                self.assertEqual(derive_scope_cluster(scopes, str(self.root)), expected)
        self.store.begin_work(session_id="conversation", scopes=[str(self.root / "src/web/app.js")])
        self.assertEqual(self.threads.get("conversation")["scope_cluster"], "src/web")

    def test_conflicting_scope_cannot_change_domain_and_handoff_persists_fallback(self):
        self.store.register(session_id="peer", client="claude", cwd=str(self.root))
        self.store.begin_work(session_id="conversation", scopes=["web/**"])
        with self.assertRaises(CoordinationError):
            self.store.begin_work(session_id="peer", scopes=["web/app.js"])
        self.assertIsNone(self.threads.get("peer")["domain"])
        self.store.handoff_work(sender_session_id="conversation", recipient_session_id="peer", patch_label="Web change",
                           validation_boundary="Unit tests pass.", validation_responsibility="Review.", mode="validation")
        self.store.end_work("peer")
        thread = self.threads.get("peer")
        self.assertEqual((thread["domain"], thread["domain_source"], thread["scope_cluster"]), ("web", "derived", "web"))

    def test_domain_migration_backfills_active_scope_without_changing_checkpoint(self):
        self.save()
        self.store.begin_work(session_id="conversation", scopes=["web/**"])
        before = self.threads.get("conversation")["checkpoint"]
        with self.store._connection() as db:
            for field in ("domain", "domain_source", "scope_cluster"):
                db.execute(f"ALTER TABLE work_threads DROP COLUMN {field}")
        reopened = CoordinationStore(self.store.database_path)
        thread = reopened.threads.get("conversation")
        self.assertEqual((thread["domain"], thread["domain_source"], thread["scope_cluster"]), ("web", "derived", "web"))
        self.assertEqual(thread["checkpoint"], before)
        reopened.end_work("conversation")
        self.assertEqual(CoordinationStore(self.store.database_path).threads.get("conversation")["domain"], "web")

    def test_workspace_domain_vocabulary_is_normalized_open_explicit_and_bounded(self):
        self.save(domain="  WEB ")
        self.store.register(session_id="duplicate", client="claude", cwd=str(self.root))
        self.threads.update("duplicate", domain="web")
        self.store.register(session_id="derived", client="kimi", cwd=str(self.root))
        self.store.begin_work(session_id="derived", scopes=["generated/**"])
        self.store.register(session_id="closed", client="codex", cwd=str(self.root))
        self.threads.update("closed", domain="closed", attention="archived")
        other = self.root / "other"
        other.mkdir()
        self.store.register(session_id="elsewhere", client="antigravity", cwd=str(other))
        self.threads.update("elsewhere", domain="elsewhere")
        self.assertEqual(self.threads.known_domains(str(self.root)), ["web"])
        for index in range(23):
            session_id = f"vocabulary-{index}"
            self.store.register(session_id=session_id, client="codex", cwd=str(self.root))
            self.threads.update(session_id, domain=f"domain-{index:02}")
        names = self.threads.known_domains(str(self.root))
        self.assertEqual(len(names), 20)
        self.assertEqual(names[0], "web")
        self.assertEqual(names[1], "domain-00")

    def test_cli_domain_override_reset_and_checkpoint_reporting(self):
        def run_cli(*args):
            return run(_parser().parse_args(["--db", str(self.store.database_path), *args]))
        run_cli("thread", "update", "--session-id", "conversation", "--domain", "WEB")
        thread = run_cli("checkpoint", "--session-id", "conversation", "--json",
                         json.dumps({"phase": "discussion", "summary": "New assignment.", "domain": "docs"}))
        self.assertEqual((thread["domain"], thread["domain_source"]), ("web", "user"))
        thread = run_cli("thread", "update", "--session-id", "conversation", "--no-domain")
        self.assertIsNone(thread["domain"])
        self.assertEqual(self.save(domain="docs")["domain_source"], "agent")

    def test_first_request_survives_later_turns_and_user_title(self):
        self.threads.start_turn("conversation", prompt="Investigate the database writes.", turn_id="first")
        self.threads.update("conversation", title="Database performance", attention="later")
        self.threads.start_turn("conversation", prompt="Try a different approach.", turn_id="second")
        thread = self.threads.get("conversation")
        self.assertEqual(thread["original_request"], "Investigate the database writes.")
        self.assertEqual(thread["title"], "Database performance")
        self.assertEqual(thread["attention"], "later")

    def test_work_phase_retains_inquiries_and_requires_evidence_of_delivery(self):
        self.assertEqual(self.threads.get("conversation")["work_phase"], "new")
        for activity in ("discussion", "investigation", "planning", "orchestrating", "implementation", "validation", "deployment"):
            with self.subTest(activity=activity):
                self.save(phase=activity, next_actor="nobody", next_action="")
                done = self.save(phase="finished", next_actor="nobody", next_action="")
                expected = "finished" if activity in {"implementation", "validation", "deployment"} else "investigation" if activity == "discussion" else activity
                self.assertEqual(done["work_phase"], expected)
                self.assertEqual(done["checkpoint"]["phase"], "finished")
                self.assertEqual(done["attention"], "now")
                self.assertEqual(CoordinationStore(self.store.database_path).threads.get("conversation")["work_phase"], expected)

    def test_orchestrating_cli_persists_without_changing_placement_or_requiring_user_action(self):
        self.threads.update("conversation", attention="later", title="Dispatcher")
        payload = dict(phase="orchestrating", summary="Routing specialist work.", next_action="", next_actor="nobody")
        args = _parser().parse_args(["--db", str(self.store.database_path), "checkpoint", "--session-id",
                                    "conversation", "--json", json.dumps(payload)])
        result = run(args)
        reopened = CoordinationStore(self.store.database_path).threads.get("conversation")
        for thread in (result, reopened):
            self.assertEqual(thread["work_phase"], "orchestrating")
            self.assertEqual(thread["checkpoint"]["next_actor"], "nobody")
            self.assertEqual(thread["checkpoint"]["next_action"], "")
            self.assertEqual(thread["title"], "Dispatcher")
            self.assertEqual(thread["attention"], "later")
        instructions = self.threads.instructions("conversation")
        self.assertIn("planning, orchestrating, implementation", instructions)
        self.assertIn("including while a specialist implements or deploys", instructions)
        with self.assertRaises(SystemExit) as help_exit, patch("sys.stdout") as stdout:
            _parser().parse_args(["checkpoint", "--help"])
        self.assertEqual(help_exit.exception.code, 0)
        self.assertIn("orchestrating", "".join(call.args[0] for call in stdout.write.call_args_list))

    def test_finished_without_an_activity_does_not_invent_delivery(self):
        saved = self.save(phase="finished", next_actor="nobody", next_action="")
        self.assertEqual(saved["work_phase"], "new")


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

    def test_routine_checkpoint_preserves_established_title_and_explicit_rename_remains_available(self):
        first = self.save(title="Database write performance")
        changed = self.save(title="Database connection pooling")
        self.assertEqual(changed["title"], "Database write performance")
        self.assertEqual(changed["checkpoint"]["id"], first["checkpoint"]["id"])
        self.assertEqual(len(self.save(title="Database connection pooling")["checkpoints"]), 1)
        renamed = self.threads.update("conversation", title="Database connection pooling")
        self.assertEqual(renamed["title"], "Database connection pooling")
        self.assertEqual(renamed["title_source"], "user")

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
        self.assertEqual(self.save(title="x" * 160)["title"], "Valid title")
        self.assertEqual(self.threads.update("conversation", title="x" * 160)["title"], "x" * 160)

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

    def test_partial_checkpoints_preserve_values_and_save_complete_snapshots(self):
        self.save(title="Database performance", domain="backend", links=[{"kind": "issue", "target": "issue-1"}])
        changed = self.threads.checkpoint("conversation", {"phase": "planning"})
        self.assertEqual(changed["checkpoint"]["summary"], "Two options remain.")
        self.assertEqual(changed["checkpoint"]["next_actor"], "user")
        self.assertEqual(changed["checkpoint"]["next_action"], "Choose an approach.")
        self.assertEqual(changed["title"], "Database performance")
        self.assertEqual(changed["domain"], "backend")
        self.assertEqual(changed["links"][0]["target"], "issue-1")
        self.assertEqual([cp["phase"] for cp in changed["checkpoints"]], ["planning", "investigation"])
        updated = self.threads.checkpoint("conversation", {"summary": "Decision recorded.", "next_step": None})
        self.assertEqual(updated["checkpoint"]["phase"], "planning")
        self.assertEqual((updated["checkpoint"]["next_action"], updated["checkpoint"]["next_actor"]), ("", "nobody"))
        repeated = self.threads.checkpoint("conversation", {"summary": "Decision recorded."})
        self.assertEqual(repeated["checkpoint"]["id"], updated["checkpoint"]["id"])

    def test_first_checkpoint_requires_phase_and_summary(self):
        for payload in ({}, {"summary": "Incomplete"}, {"phase": "discussion"}):
            with self.subTest(payload=payload), self.assertRaisesRegex(CoordinationError, "first checkpoint"):
                self.threads.checkpoint("conversation", payload)
        self.assertIsNone(self.threads.get("conversation")["checkpoint"])
        saved = self.threads.checkpoint("conversation", {"phase": "discussion", "summary": "Started."})
        self.assertEqual(saved["checkpoint"]["next_actor"], "nobody")

    def test_next_step_is_atomic_and_invalid_patches_cannot_change_state(self):
        self.save()
        saved = self.threads.checkpoint("conversation", {"next_step": {"actor": "external", "action": "Wait for CI."}})
        self.assertEqual(saved["checkpoint"]["next_actor"], "external")
        self.assertEqual(saved["checkpoint"]["next_action"], "Wait for CI.")
        for payload in ({"next_step": {}}, {"next_step": {"actor": "user"}},
                        {"next_step": {"actor": "nobody", "action": ""}},
                        {"next_step": {"actor": "user", "action": ""}},
                        {"next_step": {"actor": [], "action": "Do it"}},
                        {"next_step": None, "next_actor": "nobody", "next_action": ""},
                        {"next_actor": "agent"}, {"next_action": "Do it"},
                        {"phase": None}, {"summary": None}):
            with self.subTest(payload=payload), self.assertRaises(CoordinationError):
                self.threads.checkpoint("conversation", {**payload, "title": "Must not rename", "domain": "web",
                                                         "links": [{"kind": "issue", "target": "issue-2"}]})
            self.assertEqual(self.threads.get("conversation", history=True), saved)
        cleared = self.threads.checkpoint("conversation", {"next_actor": "nobody", "next_action": ""})
        self.assertEqual(cleared["checkpoint"]["next_actor"], "nobody")

    def test_partial_updates_keep_turn_guards_and_protected_metadata(self):
        self.threads.start_turn("conversation", turn_id="first")
        self.save()
        self.threads.update("conversation", title="User name", domain="research")
        self.now += 10
        self.threads.start_turn("conversation", turn_id="second")
        with self.assertRaisesRegex(CoordinationError, "older"):
            self.threads.checkpoint("conversation", {"phase": "validation", "turn_id": "first"})
        saved = self.threads.checkpoint("conversation", {"summary": "Continued.", "title": "Agent name", "domain": "web"})
        self.assertFalse(saved["checkpoint_stale"])
        self.assertEqual(saved["checkpoint"]["turn_id"], "second")
        self.assertEqual((saved["title"], saved["domain"]), ("User name", "research"))

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

    def test_checkpoint_accepts_validated_app_links_and_rejects_invalid_routes_or_schemes(self):
        from agent_coord.navigation import NavigationStore
        child_id = "7d724292-3c97-422a-8e38-ea0ef3c1a687"
        self.store.register(session_id=child_id, client="kimi", cwd=str(self.root), name="Hide empty Agents tab")
        nav = NavigationStore(self.store)
        minted = nav.link(thread=child_id)["url"]
        message_link = nav.link(thread=child_id, turn="turn-1", item="item-1")["url"]
        saved = self.threads.checkpoint("conversation", {
            "phase": "orchestrating",
            "summary": "Dispatched specialist for empty Agents tab.",
            "next_step": None,
            "links": [
                {"kind": "other", "label": "Hide empty Agents tab", "target": minted},
                {"kind": "other", "label": "Updated label", "target": f"agentcoord://thread/{child_id}"},
                {"kind": "other", "label": "Matched message", "target": message_link},
            ],
        })
        self.assertEqual([(link["label"], link["target"]) for link in saved["links"]], [
            ("Updated label", minted),
            ("Matched message", message_link),
        ])
        other_nav = NavigationStore(CoordinationStore(self.root / "other.sqlite3"))
        before = self.threads.get("conversation", history=True)
        for bad in (
            "agentcoord://thread/missing",
            other_nav.link()["url"],
            "agentcoord://send?message=oops",
            "agentcoord://attribution?download_token=7d724292-3c97-422a-8e38-ea0ef3c1a687",
            f"{minted}&window=7d724292-3c97-422a-8e38-ea0ef3c1a687",
            f"{minted}&request=7d724292-3c97-422a-8e38-ea0ef3c1a687",
            f"agentcoord://thread/{child_id}?turn=only-turn",
            f"agentcoord://thread/{child_id}#frag",
            "javascript:alert(1)",
            "file:///etc/passwd",
            "data:text/plain,hello",
        ):
            with self.subTest(bad=bad), self.assertRaises(CoordinationError):
                self.threads.checkpoint("conversation", {
                    "summary": "Must not save.",
                    "links": [{"kind": "other", "label": "Bad", "target": bad}],
                })
            self.assertEqual(self.threads.get("conversation", history=True), before)

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
