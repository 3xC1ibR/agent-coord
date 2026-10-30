from __future__ import annotations

import unittest
import tempfile

import test_codex_app_server
import test_ui
from test_subagent_close import attach
from agent_coord import supervision
from agent_coord.agent_tree import agent_tree
from agent_coord.store import CoordinationError


class AgentTreeTests(unittest.TestCase):
    setUp = test_codex_app_server.BrowserSessionTests.setUp
    create = test_codex_app_server.BrowserSessionTests.create

    def test_family_contains_closed_ancestors_siblings_and_terminal_delegates(self):
        parent, child, sibling, independent = [self.create() for _ in range(4)]
        attach(self.store, parent, child)
        attach(self.store, parent, sibling)
        attach(self.store, parent, independent, mode=None)
        self.sessions.close_work_thread(parent)
        self.store.threads.update(sibling, attention="later")
        self.store.register(session_id="delegate", client="codex", cwd=str(self.root))
        delegation = self.store.create_delegation(parent_session_id=child, cwd=str(self.root),
            scopes=["file.py"], instructions="Validate", mode="reviewed")
        self.store.attach_delegation(delegation["delegation_id"], "delegate")
        calls = list(self.sessions.rpc.calls)
        result = agent_tree(self.sessions, child)
        self.assertEqual(result["root_thread_id"], parent)
        by_id = {t["thread_id"]: t for t in result["data"]}
        self.assertEqual(set(by_id), {parent, child, sibling, "delegate"})
        self.assertEqual(by_id["delegate"]["agent_parent_id"], child)
        self.assertFalse(by_id["delegate"]["browser_session"])
        self.assertEqual(by_id[parent]["attention"], "archived")
        self.assertEqual(self.sessions.rpc.calls, calls)

    def test_supervision_transfer_moves_the_branch_to_the_successor(self):
        lead, successor, child, nested, kept = [self.create() for _ in range(5)]
        attach(self.store, lead, child)
        attach(self.store, child, nested)
        attach(self.store, lead, kept)
        supervision.transfer(self.store, requester=lead, from_session=lead, to_session=successor,
                             thread_ids=[child], request_id="adopt", reason="Replace lead", workspace=str(self.root))
        result = agent_tree(self.sessions, nested)
        self.assertEqual(result["root_thread_id"], successor)
        by_id = {t["thread_id"]: t for t in result["data"]}
        self.assertEqual(set(by_id), {successor, child, nested})
        self.assertEqual(by_id[child]["agent_parent_id"], successor)
        self.assertEqual(by_id[child]["created_by_thread_id"], lead)
        self.assertEqual({t["thread_id"] for t in agent_tree(self.sessions, lead)["data"]}, {lead, kept})

    def test_forks_share_a_family_with_closed_sources_siblings_and_delegated_agents(self):
        source, fork, sibling, nested, agent = [self.create() for _ in range(5)]
        for child, parent in ((fork, source), (sibling, source), (nested, fork)):
            self.store.threads.record_fork(child, self.store.threads.get(parent))
        attach(self.store, fork, agent)
        self.sessions.close_work_thread(source)
        calls = list(self.sessions.rpc.calls)
        for selected in (source, fork, sibling, nested, agent):
            result = agent_tree(self.sessions, selected)
            self.assertEqual(result["root_thread_id"], source)
            by_id = {t["thread_id"]: t for t in result["data"]}
            self.assertEqual(set(by_id), {source, fork, sibling, nested, agent})
            self.assertEqual(by_id[fork]["agent_parent_id"], source)
            self.assertEqual(by_id[nested]["agent_parent_id"], fork)
            self.assertEqual(by_id[agent]["agent_parent_id"], fork)
            self.assertEqual(by_id[fork]["forked_from"]["thread_id"], source)
            self.assertIsNone(by_id[fork]["created_by_thread_id"])
        self.assertEqual(self.sessions.rpc.calls, calls)

    def test_fork_lineage_respects_workspace_boundaries(self):
        source, fork = self.create(), self.create()
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        self.store.register(session_id="outside", client="codex", cwd=outside.name)
        self.store.threads.record_fork(fork, self.store.threads.get("outside"))
        self.store.threads.record_fork("outside", self.store.threads.get(source))
        self.assertEqual([t["thread_id"] for t in agent_tree(self.sessions, source)["data"]], [source])
        result = agent_tree(self.sessions, fork)
        self.assertEqual(result["root_thread_id"], fork)
        self.assertIsNone(result["data"][0]["agent_parent_id"])

    def test_workspace_boundary_hides_inaccessible_ancestors_and_children(self):
        child = self.create()
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        self.store.register(session_id="outside", client="codex", cwd=outside.name)
        attach(self.store, "outside", child)
        result = agent_tree(self.sessions, child)
        self.assertEqual(result["root_thread_id"], child)
        self.assertEqual([t["thread_id"] for t in result["data"]], [child])
        self.assertIsNone(result["data"][0]["agent_parent_id"])
        with self.assertRaises(CoordinationError):
            agent_tree(self.sessions, "outside")

    def test_cycles_have_a_consistent_root_from_every_entry_point(self):
        a, b, c = [self.create() for _ in range(3)]
        attach(self.store, a, b)
        attach(self.store, b, a)
        attach(self.store, b, c)
        roots = [agent_tree(self.sessions, ident)["root_thread_id"] for ident in [a, b, c]]
        self.assertEqual(len(set(roots)), 1)
        self.assertEqual(len(agent_tree(self.sessions, c)["data"]), 3)


class AgentTreeHTTPTests(unittest.TestCase):
    setUp = test_ui.BrowserHTTPTests.setUp
    stop_server = test_ui.BrowserHTTPTests.stop_server
    request = test_ui.BrowserHTTPTests.request

    def test_endpoint_and_assets_are_available_without_provider_activity(self):
        parent, child = [self.sessions.create({})["session"]["thread_id"] for _ in range(2)]
        attach(self.store, parent, child)
        calls = list(self.sessions.rpc.calls)
        status, result = self.request("/api/browser/threads/" + child + "/agent-tree")
        self.assertEqual(status, 200)
        self.assertEqual(result["root_thread_id"], parent)
        self.assertEqual(len(result["data"]), 2)
        self.assertEqual(self.sessions.rpc.calls, calls)
        import urllib.request
        for asset in ("agent-tree.js", "agent-tree.css"):
            with urllib.request.urlopen(self.url + "/" + asset) as response:
                self.assertEqual(response.status, 200)
                self.assertGreater(len(response.read()), 100)
