from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))

from agent_coord.cli import _parser, run
from agent_coord.app_control import AppControl, AppControlWorker
from agent_coord.codex_app_server import BrowserSessions
from agent_coord.store import CoordinationError, CoordinationStore
from agent_coord.ui import make_ui_server
from test_codex_app_server import FakeCodex


class ThreadOrganizationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / "backend"
        (self.repo / ".git").mkdir(parents=True)
        self.store = CoordinationStore(self.root / "state.sqlite3")
        self.sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(self.sessions.close)
        self.project = self.store.threads.organization.create_project("Migration")

    def create(self, **values):
        return self.sessions.create(values)["session"]["thread_id"]

    def create_agent(self, parent, *, mode="orchestration", **values):
        worker = AppControlWorker(self.sessions)
        self.addCleanup(worker.close)
        request = worker.control.request("create", parent, {
            "cwd": str(self.root), "prompt": "Build the assigned mockup.", "mode": mode, **values,
        })
        worker.process_once()
        result = worker.control.status(request["request_id"])
        self.assertEqual(result["status"], "completed", result)
        return result["thread_id"]

    def attach(self, parent, child):
        delegation = self.store.create_delegation(
            parent_session_id=parent, cwd=str(self.root), scopes=[child + ".txt"],
            instructions="Build a nested mockup.", mode="reviewed")
        self.store.attach_delegation(delegation["delegation_id"], child)
        return delegation["delegation_id"]

    def test_group_assignment_recurses_through_existing_agents_over_http_and_cli(self):
        parent = self.create()
        child = self.create_agent(parent)
        grandchild = self.create_agent(child)
        standalone = self.create_agent(parent, mode=None)
        independent_child = self.create_agent(standalone)
        other = self.store.threads.organization.create_project("Other")
        self.store.threads.update(child, project_id=other["id"], attention="later")
        self.store.threads.update(grandchild, attention="archived", repository_id=None)
        before = {tid: self.store.threads.get(tid, history=True) for tid in (parent, child, grandchild)}
        # Keep queued synthetic agents idle while checking group-assignment metadata.
        with patch.object(self.sessions.inbox_wake, "start"):
            request = self.start_ui()
        request("threads/" + parent, {"project_id": self.project["id"]})
        for tid, original in before.items():
            actual = self.store.threads.get(tid, history=True)
            self.assertEqual(actual["project_id"], self.project["id"])
            for field in ("cwd", "repository_id", "attention", "original_request", "checkpoints", "links", "updated_at"):
                self.assertEqual(actual[field], original[field], field)
        for tid in (standalone, independent_child):
            self.assertIsNone(self.store.threads.get(tid)["project_id"])
        # Clearing through the CLI uses the same recursive operation.
        run(_parser().parse_args(["--db", str(self.store.database_path), "thread", "update",
                                  "--session-id", parent, "--no-project"]))
        reopened = CoordinationStore(self.store.database_path)
        for tid in before:
            self.assertIsNone(reopened.threads.get(tid)["project_id"])

    def test_new_orchestration_agents_inherit_group_at_every_depth(self):
        parent = self.create(project_id=self.project["id"])
        child = self.create_agent(parent)
        grandchild = self.create_agent(child)
        self.assertEqual(self.store.threads.get(child)["project_id"], self.project["id"])
        self.assertEqual(self.store.threads.get(grandchild)["project_id"], self.project["id"])
        self.assertIsNone(self.store.threads.get(self.create_agent(parent, mode=None))["project_id"])
        # An explicit child assignment remains supported.
        other = self.store.threads.organization.create_project("Independent assignment")
        explicit = self.create_agent(parent, project_id=other["id"])
        self.assertEqual(self.store.threads.get(explicit)["project_id"], other["id"])

    def test_orchestration_workspace_visibility_survives_closed_ancestors_and_restart(self):
        other = self.root / "other"
        other.mkdir()
        parent = self.create(cwd=str(self.root))
        child = self.create_agent(parent, cwd=str(self.repo))
        grandchild = self.create_agent(child, cwd=str(other))
        self.store.threads.update(parent, attention="archived")
        self.store.threads.update(child, attention="later")
        # Execution cwd is mutable; the project where work began stays stable.
        with self.store._connection() as db:
            db.execute("UPDATE sessions SET cwd = ? WHERE session_id = ?", (str(self.repo), parent))
        restarted = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(restarted.close)
        listed = {t["thread_id"]: t for t in restarted.list_work_threads()}
        self.assertNotIn(parent, listed)
        self.assertEqual(listed[child]["orchestration_workspaces"], [str(self.root)])
        self.assertEqual(listed[grandchild]["orchestration_workspaces"], [str(self.repo), str(self.root)])
        self.assertEqual(listed[grandchild]["cwd"], str(other))
        self.assertEqual(listed[grandchild]["project_cwd"], str(other))
        self.assertEqual(restarted.work_thread(grandchild)["orchestration_workspaces"],
                         listed[grandchild]["orchestration_workspaces"])

    def test_dispatch_workspace_stays_independent_of_creator(self):
        parent = self.create(cwd=str(self.root))
        specialist = self.create_agent(parent, mode=None, cwd=str(self.repo))
        worker = self.create_agent(specialist)
        self.assertEqual(self.sessions.work_thread(specialist)["orchestration_workspaces"], [])
        self.assertEqual(self.sessions.work_thread(worker)["orchestration_workspaces"], [str(self.repo)])

    def test_workspace_visibility_handles_historical_creation_cycles(self):
        parent = self.create(cwd=str(self.root))
        child = self.create_agent(parent, cwd=str(self.repo))
        control = AppControl(self.store)
        request = control.request("create", child, {"prompt": "Old provenance", "mode": "orchestration"})
        control.claim(request["request_id"], "test")
        control.identified(request["request_id"], parent)
        control.complete(request["request_id"], result={"thread_id": parent})
        self.assertEqual(self.sessions.work_thread(child)["orchestration_workspaces"], [str(self.root)])
        self.assertEqual(self.sessions.work_thread(parent)["orchestration_workspaces"], [str(self.repo)])

    def test_group_recurses_through_mixed_terminal_and_app_descendants(self):
        parent = self.create(project_id=self.project["id"])
        self.store.register(session_id="terminal-child", client="codex", cwd=str(self.root))
        delegation = self.attach(parent, "terminal-child")
        self.assertEqual(self.store.threads.get("terminal-child")["project_id"], self.project["id"])
        grandchild = self.create_agent("terminal-child")
        # A malformed cycle cannot hang recursive group assignment.
        self.attach(grandchild, parent)
        other = self.store.threads.organization.create_project("Moved")
        self.sessions.update_work_thread(parent, {"project_id": other["id"]})
        for tid in (parent, "terminal-child", grandchild):
            self.assertEqual(self.store.threads.get(tid)["project_id"], other["id"])
        self.store.threads.update("terminal-child", project_id=None)
        self.store.attach_delegation(delegation, "terminal-child")
        self.assertIsNone(self.store.threads.get("terminal-child")["project_id"])

    def test_group_uses_first_identified_creation_and_handles_uncertain_delivery(self):
        parent = self.create(project_id=self.project["id"])
        child = self.create()
        control = AppControl(self.store)
        request = control.request("create", parent, {"prompt": "Mockup", "mode": "orchestration"})
        control.claim(request["request_id"], "test")
        control.identified(request["request_id"], child)
        self.assertEqual(self.store.threads.get(child)["project_id"], self.project["id"])
        control.complete(request["request_id"], error="Delivery interrupted", uncertain=True)
        self.store.threads.update(parent, project_id=None)
        self.assertIsNone(self.store.threads.get(child)["project_id"])
        # Later provenance cannot reparent an existing agent.
        stranger = self.create()
        later = control.request("create", stranger, {"prompt": "Retry", "mode": "orchestration"})
        control.claim(later["request_id"], "test")
        control.identified(later["request_id"], child)
        self.store.threads.update(stranger, project_id=self.project["id"])
        self.assertIsNone(self.store.threads.get(child)["project_id"])

    def test_reassigning_same_group_repairs_old_descendants_and_invalid_group_is_atomic(self):
        parent = self.create(project_id=self.project["id"])
        child = self.create_agent(parent)
        # Simulate membership left behind by the old parent-only update.
        with self.store._connection() as db:
            db.execute("UPDATE thread_organization SET project_id = NULL WHERE thread_id = ?", (child,))
        with self.assertRaises(CoordinationError):
            self.store.threads.update(parent, title="Changed", project_id="missing")
        self.assertIsNone(self.store.threads.get(child)["project_id"])
        self.assertNotEqual(self.store.threads.get(parent)["title"], "Changed")
        self.store.threads.update(parent, project_id=self.project["id"])
        self.assertEqual({t["thread_id"] for t in self.store.threads.list(project_id=self.project["id"])},
                         {parent, child})

    def test_create_defaults_and_explicit_nulls_are_distinct(self):
        code = self.create(cwd=str(self.repo))
        idea = self.create(cwd=str(self.repo), repository_id=None, project_id=self.project["id"])
        plain = self.create(cwd=str(self.root))
        self.assertIsNotNone(self.sessions.work_thread(code)["repository_id"])
        self.assertIsNone(self.sessions.work_thread(code)["project_id"])
        self.assertIsNone(self.sessions.work_thread(idea)["repository_id"])
        self.assertEqual(self.sessions.work_thread(idea)["project_name"], "Migration")
        self.assertIsNone(self.sessions.work_thread(plain)["repository_id"])
        self.assertIsNone(self.sessions.work_thread(plain)["project_id"])
        self.assertEqual(self.sessions.work_thread(idea)["cwd"], str(self.repo))

    def test_update_both_independently_preserves_history_and_artifact_context(self):
        thread_id = self.create(cwd=str(self.repo))
        self.store.threads.start_turn(thread_id, prompt="Explore the migration.", turn_id="one")
        self.store.threads.checkpoint(thread_id, {"phase": "planning", "summary": "Plan saved."})
        self.store.threads.add_link(thread_id, {"kind": "document", "target": "design.md"})
        before = self.sessions.work_thread(thread_id)
        assigned = self.sessions.update_work_thread(thread_id, {"project_id": self.project["id"]})
        self.assertEqual(assigned["repository_id"], before["repository_id"])
        cleared = self.sessions.update_work_thread(thread_id, {"repository_id": None})
        self.assertEqual(cleared["project_id"], self.project["id"])
        self.assertEqual(cleared["cwd"], before["cwd"])
        self.assertEqual(cleared["checkpoints"], before["checkpoints"])
        self.assertEqual(cleared["links"], before["links"])
        self.assertEqual(cleared["original_request"], before["original_request"])
        self.store.threads.add_link(thread_id, {"kind": "document", "target": "design.md"})
        self.assertEqual(len(self.sessions.work_thread(thread_id)["links"]), 1)
        reopened = CoordinationStore(self.store.database_path).threads.get(thread_id)
        self.assertIsNone(reopened["repository_id"])
        self.assertEqual(reopened["project_id"], self.project["id"])

    def test_filter_intersection_and_workspace_filter_after_clearing_repository(self):
        code = self.create(cwd=str(self.repo), project_id=self.project["id"])
        idea = self.create(project_id=self.project["id"])
        plain = self.create()
        repository_id = self.store.threads.get(code)["repository_id"]
        def ids(**filters):
            return {t["thread_id"] for t in self.store.threads.list(**filters)}
        self.assertEqual(ids(project_id=self.project["id"]), {code, idea})
        self.assertEqual(ids(repository_id=None), {idea, plain})
        self.assertEqual(ids(repository_id=repository_id, project_id=self.project["id"]), {code})
        self.assertEqual(ids(repository_id=None, project_id=None), {plain})
        self.sessions.update_work_thread(code, {"repository_id": None})
        self.assertEqual(ids(cwd=str(self.repo)), {code})

    def test_invalid_creation_and_update_have_no_partial_side_effects(self):
        calls = list(self.sessions.rpc.calls)
        with self.assertRaises(CoordinationError):
            self.create(repository_id="missing")
        self.assertEqual(self.sessions.rpc.calls, calls)
        thread_id = self.create()
        self.assertTrue(self.sessions.persistence.flush(timeout=3, key=thread_id))
        before = self.sessions.work_thread(thread_id)
        with self.assertRaises(CoordinationError):
            self.sessions.update_work_thread(thread_id, {"title": "Changed", "project_id": "missing"})
        self.assertEqual(self.sessions.work_thread(thread_id), before)

    def test_cli_create_assign_filter_and_clear(self):
        def cli(*args):
            return run(_parser().parse_args(["--db", str(self.store.database_path), *args]))
        project = cli("project", "create", "--name", "Performance")
        self.assertEqual(project["cwd"], str(Path.cwd().resolve()))
        local = cli("project", "create", "--name", "Performance", "--cwd", str(self.repo))
        self.assertEqual(local["cwd"], str(self.repo))
        self.assertNotEqual(local["id"], project["id"])
        repository = cli("repository", "add", "--path", str(self.repo))
        thread_id = self.create()
        updated = cli("thread", "update", "--session-id", thread_id, "--project", project["id"], "--repository", repository["id"])
        self.assertEqual(updated["project_name"], "Performance")
        self.assertEqual(updated["repository_name"], "backend")
        self.assertEqual(cli("thread", "list", "--project", project["id"])[0]["thread_id"], thread_id)
        cli("thread", "update", "--session-id", thread_id, "--no-repository")
        self.assertEqual(cli("thread", "list", "--no-repository")[0]["project_id"], project["id"])
        cli("thread", "update", "--session-id", thread_id, "--no-project")
        self.assertIsNone(cli("thread", "list", "--no-project")[0]["project_id"])
        self.assertEqual(len(cli("repository", "list")), 1)

    def start_ui(self):
        server = make_ui_server(self.store, port=0, browser_sessions=self.sessions)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(worker.join)
        self.addCleanup(server.shutdown)
        base = "http://127.0.0.1:" + str(server.server_address[1]) + "/api/browser/"
        with urllib.request.urlopen(base + "config") as response:
            token = json.load(response)["token"]
        def request(path, body=None):
            headers = {"Content-Type": "application/json", "X-Agent-Coord-Token": token}
            req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None, headers=headers)
            with urllib.request.urlopen(req) as response:
                return json.load(response)
        return request

    def test_http_organization_and_assignment_routes(self):
        request = self.start_ui()
        project = request("projects", {"name": "New initiative"})
        self.assertEqual(project["cwd"], str(self.root))
        repository = request("repositories", {"path": str(self.repo)})
        choices = request("organization")
        self.assertIn(project, choices["projects"])
        self.assertIn(repository, choices["repositories"])
        thread_id = request("sessions", {"cwd": str(self.root), "project_id": project["id"]})["session"]["thread_id"]
        updated = request("threads/" + thread_id, {"repository_id": repository["id"]})
        self.assertEqual(updated["project_id"], project["id"])
        self.assertEqual(updated["cwd"], str(self.root))
        self.assertIsNone(request("threads/" + thread_id, {"repository_id": None})["repository_id"])
        with self.assertRaises(urllib.error.HTTPError) as error:
            request("projects", {"name": "Bad", "extra": True})
        self.assertEqual(error.exception.code, 400)
        error.exception.close()

    def test_groups_created_in_different_projects_have_independent_identity(self):
        request = self.start_ui()
        parent = request("projects", {"name": "Launch", "cwd": str(self.root)})
        child = request("projects", {"name": "Launch", "cwd": str(self.repo)})
        self.assertNotEqual(parent["id"], child["id"])
        self.assertEqual(request("projects", {"name": "launch"}), parent)
        self.assertEqual(request("projects", {"name": "launch", "cwd": str(self.repo)}), child)
        self.assertIn(child, request("organization")["projects"])

    def test_group_rename_updates_thread_labels_and_preserves_saved_filters(self):
        request = self.start_ui()
        group = request("projects", {"name": "Launch", "cwd": str(self.root)})
        thread_id = self.create(project_id=group["id"])
        saved = request("views", {"name": "Needs me", "filters": {"project": group["id"], "show": "attention"}})
        before = request("threads/" + thread_id)
        updated = request("projects/" + group["id"], {"name": "Launch readiness"})
        self.assertEqual(updated, {**group, "name": "Launch readiness"})
        after = request("threads/" + thread_id)
        self.assertEqual(after["project_name"], "Launch readiness")
        for field in ("project_id", "cwd", "attention", "domain", "checkpoint"):
            self.assertEqual(after[field], before[field])
        self.assertEqual(request("views")["data"], [saved])

    def test_http_group_removal_ungroups_all_placements_without_starting_or_closing_threads(self):
        request = self.start_ui()
        group = request("projects", {"name": "Remove this"})
        other = request("projects", {"name": "Keep this"})
        for attention in ("now", "later", "archived"):
            thread_id = "keep-" + attention
            self.store.register(session_id=thread_id, client="claude", cwd=str(self.repo))
            self.store.threads.update(thread_id, project_id=group["id"], attention=attention)
        before = {id: request("threads/" + id) for id in ("keep-now", "keep-later", "keep-archived")}
        calls = list(self.sessions.rpc.calls)
        plan = request("projects/" + group["id"])
        self.assertEqual(plan, {**group, "thread_count": 3})
        for body in ({}, {"thread_count": 0}, {"thread_count": True}, {"thread_count": 3, "extra": True}):
            with self.assertRaises(urllib.error.HTTPError) as error:
                request("projects/" + group["id"] + "/delete", body)
            self.assertEqual(error.exception.code, 400)
            error.exception.close()
        self.assertEqual(request("projects/" + group["id"] + "/delete", {"thread_count": 3}), plan)
        self.assertIn(other, request("organization")["projects"])
        self.assertNotIn(group, request("organization")["projects"])
        for id, original in before.items():
            self.assertEqual(request("threads/" + id), {**original, "project_id": None, "project_name": None})
        self.assertEqual(self.sessions.rpc.calls, calls)

    def test_group_removal_keeps_scheduled_work_usable_without_the_group(self):
        group = self.store.threads.organization.create_project("Scheduled", str(self.root))
        scheduled = self.sessions.schedules.create({
            "id": str(uuid.uuid4()), "message": "Continue planned work", "run_at": self.store.clock() + 3600,
            "timezone": "UTC", "settings": {"cwd": str(self.root), "model": "available-model", "project_id": group["id"]}})
        self.store.threads.organization.delete_project(group["id"], expected_count=0)
        updated = next(item for item in self.sessions.schedules.list() if item["id"] == scheduled["id"])
        self.assertEqual(updated["settings"], {**scheduled["settings"], "project_id": None})
        for field in ("message", "run_at", "timezone", "state", "cwd"):
            self.assertEqual(updated[field], scheduled[field])

    def test_standalone_projects_and_existing_terminal_threads(self):
        request = self.start_ui()
        first = request("projects", {"name": "First project"})
        second = request("projects", {"name": "Second project"})
        self.assertEqual(request("threads")["data"], [])
        self.assertIn(first, request("organization")["projects"])
        self.assertEqual(self.sessions.rpc.calls, [])

        for attention in ("now", "later", "archived"):
            with self.subTest(attention=attention):
                thread_id = "terminal-" + attention
                self.store.register(session_id=thread_id, client="claude", cwd=str(self.repo))
                self.store.threads.start_turn(thread_id, prompt="Keep this conversation.", turn_id="one")
                self.store.threads.checkpoint(thread_id, {"phase": "planning", "summary": "Keep this plan."})
                self.store.threads.add_link(thread_id, {"kind": "document", "target": "design.md"})
                before = request("threads/" + thread_id, {"attention": attention})
                for project in (first, second, None):
                    project_id = project["id"] if project else None
                    updated = request("threads/" + thread_id, {"project_id": project_id})
                    self.assertEqual(updated["project_id"], project_id)
                    self.assertEqual(updated["project_name"], project["name"] if project else None)
                    for field in ("cwd", "repository_id", "attention", "original_request", "checkpoints", "links"):
                        self.assertEqual(updated[field], before[field], field)
                    reopened = CoordinationStore(self.store.database_path).threads.get(thread_id)
                    self.assertEqual(reopened["project_id"], project_id)
                    visible = request("threads?archived=" + str(attention == "archived").lower())["data"]
                    listed = next(thread for thread in visible if thread["thread_id"] == thread_id)
                    self.assertEqual(listed["project_id"], project_id)
        self.assertEqual(self.sessions.rpc.calls, [])

    @unittest.skipUnless(shutil.which("node"), "Node is required for browser grouping tests")
    def test_browser_grouping_and_filters(self):
        result = subprocess.run(["node", "--test", str(Path(__file__).with_name("test_web_organization.js"))],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
