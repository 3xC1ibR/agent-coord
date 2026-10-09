from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.request
from urllib.parse import parse_qs, urlsplit
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))

from agent_coord.cli import _parser, run
from agent_coord.codex_app_server import BrowserSessions
from agent_coord.navigation import NavigationStore
from agent_coord.store import CoordinationError, CoordinationStore
from agent_coord.ui import make_ui_server
from test_codex_app_server import FakeCodex


class NavigationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = CoordinationStore(self.root / "state.sqlite3")
        self.store.register(session_id="manager", client="codex", cwd=str(self.root))
        self.nav = NavigationStore(self.store)
        self.billing = self.store.threads.organization.create_project("Billing & Renewals")

    def test_message_destinations_round_trip_and_reject_incomplete_targets(self):
        target = self.nav.link(thread="manager", turn="turn/id", item="item:1")
        self.assertEqual(self.nav.resolve(target["url"])["route"],
                         {"kind": "thread", "id": "manager", "turn": "turn/id", "item": "item:1"})
        for suffix in ("thread/manager?turn=t", "thread/manager?item=i", "overview?turn=t&item=i", "view/all?turn=t&item=i"):
            with self.assertRaises(CoordinationError):
                self.nav.resolve("agentcoord://" + suffix)

    def test_cli_resolves_case_insensitive_names_and_preserves_server_command(self):
        target = run(_parser().parse_args(["--db", str(self.store.database_path), "ui", "link", "--project", "billing & renewals"]))
        self.assertEqual(target["route"]["filters"], {"project": self.billing["id"]})
        self.assertEqual(self.nav.resolve(target["url"])["route"], target["route"])
        self.assertEqual(_parser().parse_args(["ui", "--port", "0", "--no-browser"]).ui_command, None)
        self.assertEqual(self.nav.link(project=self.billing["id"])["url"], target["url"])

    def test_repository_ambiguity_requires_exact_id_or_path(self):
        repos = []
        for parent in ("one", "two"):
            path = self.root / parent / "service"
            (path / ".git").mkdir(parents=True)
            repos.append(self.store.threads.organization.add_repository(str(path)))
        with self.assertRaisesRegex(CoordinationError, "Ambiguous repository"):
            self.nav.link(repository="service")
        self.assertEqual(self.nav.link(repository=repos[0]["root"])["route"]["filters"], {"repository": repos[0]["id"]})
        self.assertEqual(self.nav.link(repository=repos[1]["id"], project="__none__")["route"]["filters"],
                         {"repository": repos[1]["id"], "project": "__none__"})

    def test_views_threads_and_missing_destinations_never_mutate_metadata(self):
        saved = self.nav.views.create({"name": "Review", "filters": {"project": self.billing["id"], "search": "invoice"}})
        before = self.store.threads.get("manager", history=True)
        self.assertEqual(self.nav.resolve(self.nav.link(view="review")["url"])["route"], {"kind": "view", "id": saved["id"]})
        self.assertEqual(self.nav.resolve(self.nav.link(thread="manager")["url"])["route"], {"kind": "thread", "id": "manager"})
        self.assertEqual(self.store.threads.get("manager", history=True), before)
        self.assertEqual(self.nav.views.list(), [saved])
        for values in ({"project": "missing"}, {"thread": "missing"}, {"view": "missing"}, {"view": "Review", "project": self.billing["id"]}):
            with self.subTest(values=values), self.assertRaises(CoordinationError):
                self.nav.link(**values)
        self.nav.views.delete(saved["id"], {"version": saved["version"]})
        with self.assertRaises(CoordinationError):
            self.nav.resolve("agentcoord://view/" + saved["id"])

    def test_links_without_query_open_existing_destinations_without_mutation(self):
        saved = self.nav.views.create({"name": "Review", "filters": {"project": self.billing["id"], "search": "keep"}})
        before = self.store.threads.get("manager", history=True)
        destinations = {
            "agentcoord://overview": {"kind": "overview", "filters": {}},
            "agentcoord://view/" + saved["id"]: {"kind": "view", "id": saved["id"]},
            "agentcoord://thread/manager": {"kind": "thread", "id": "manager"},
        }
        for url, route in destinations.items():
            for suffix in ("", "?"):
                with self.subTest(url=url + suffix):
                    self.assertEqual(self.nav.resolve(url + suffix)["route"], route)
        self.assertEqual(self.nav.views.list(), [saved])
        self.assertEqual(self.store.threads.get("manager", history=True), before)

    def test_rejects_invalid_links_and_cross_database_destinations(self):
        other = NavigationStore(CoordinationStore(self.root / "other.sqlite3"))
        for url in ("https://example.com", "agentcoord://overview/extra", "agentcoord://overview?project=missing",
                    "agentcoord://overview?project=x&project=y", "agentcoord://overview?command=send",
                    "agentcoord://view/all?project=x", "agentcoord://thread/manager/extra", "agentcoord://user@overview",
                    "agentcoord://overview#foo", "agentcoord://overview?project", other.link()["url"]):
            with self.subTest(url=url), self.assertRaises(CoordinationError):
                self.nav.resolve(url)

    @patch("agent_coord.navigation.sys.platform", "darwin")
    def test_open_targets_origin_and_distinguishes_requested_displayed_and_failed(self):
        window = str(uuid.uuid4())
        self.nav.bind("manager", window)
        target = self.nav.link(project=self.billing["id"])
        with patch("agent_coord.navigation.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as opener:
            requested = self.nav.open(target, from_session="manager", wait=0)
        self.assertEqual(requested["status"], "requested")
        args = opener.call_args.args[0]
        self.assertEqual(args[:3], ["/usr/bin/open", "-b", "com.agentcoord.desktop"])
        self.assertEqual(parse_qs(urlsplit(args[-1]).query)["window"], [window])
        for status in ("displayed", "failed"):
            def opened(command, **_):
                resolved = self.nav.resolve(command[-1])
                self.nav.acknowledge({"request_id": resolved["request_id"], "status": status, "window_id": window})
                return subprocess.CompletedProcess([], 0, "", "")
            with patch("agent_coord.navigation.subprocess.run", side_effect=opened):
                self.assertEqual(self.nav.open(target)["status"], status)
        with patch("agent_coord.navigation.subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "App missing")):
            with self.assertRaisesRegex(CoordinationError, "Install the app"):
                self.nav.open(target)

    def test_origin_is_available_during_rpc_rolls_back_errors_and_follows_queued_turn(self):
        sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(sessions.close)
        thread = sessions.create({})["session"]["thread_id"]
        first, second = str(uuid.uuid4()), str(uuid.uuid4())
        sessions.send(thread, {"message": "first", "windowId": first})
        sessions.rpc.fail_steer = True
        with self.assertRaises(CoordinationError):
            sessions.send(thread, {"message": "failed", "windowId": second})
        self.assertEqual(self.nav.origin(thread), first)
        sessions.rpc.fail_steer = False
        sessions.queue.enqueue(thread, {"message": "later", "windowId": second})
        self.assertEqual(self.nav.origin(thread), first)
        turn = sessions.rpc.threads[thread]["turns"][-1]
        turn["status"] = "completed"
        sessions._event({"method": "turn/completed", "params": {"threadId": thread, "turn": turn}})
        with sessions.changed:
            self.assertTrue(sessions.changed.wait_for(lambda: not sessions.queue.list(thread), timeout=5))
        self.assertEqual(self.nav.origin(thread), second)
        # A non-UI turn must not retain an old window binding.
        sessions.send(thread, {"message": "terminal steering"})
        self.assertIsNone(self.nav.origin(thread))

    def test_navigation_http_resolves_and_acknowledges_with_csrf(self):
        sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        server = make_ui_server(self.store, port=0, browser_sessions=sessions)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base = f"http://127.0.0.1:{server.server_port}/api/browser/"
        try:
            with urllib.request.urlopen(base + "config") as response:
                token = json.load(response)["token"]
            body = {"url": self.nav.link(project=self.billing["id"])["url"]}
            request = urllib.request.Request(base + "navigation/resolve", data=json.dumps(body).encode(),
                                             headers={"Content-Type": "application/json", "X-Agent-Coord-Token": token})
            with urllib.request.urlopen(request) as response:
                self.assertEqual(json.load(response)["route"]["filters"], {"project": self.billing["id"]})
            request.remove_header("X-agent-coord-token")
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request)
            self.assertEqual(error.exception.code, 403)
            error.exception.close()
        finally:
            server.shutdown(); server.server_close(); worker.join(2)


if __name__ == "__main__":
    unittest.main()
