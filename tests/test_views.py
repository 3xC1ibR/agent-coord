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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))

from agent_coord.codex_app_server import BrowserSessions
from agent_coord.store import CoordinationError, CoordinationStore
from agent_coord.ui import make_ui_server
from agent_coord.views import DEFAULT_FILTERS, ViewStore
from test_codex_app_server import FakeCodex


class SavedViewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = CoordinationStore(self.root / "state.sqlite3")
        self.views = ViewStore(self.store)
        self.repo = self.root / "rig"
        (self.repo / ".git").mkdir(parents=True)
        self.repository = self.store.threads.organization.add_repository(str(self.repo))
        self.project = self.store.threads.organization.create_project("Migration")

    def test_orchestrating_filter_survives_restart_and_retains_associations(self):
        view = self.views.create({"name": "Dispatchers", "filters": {
            "phase": "orchestrating", "repository": self.repository["id"], "project": self.project["id"]}})
        reopened = ViewStore(CoordinationStore(self.store.database_path))
        self.assertEqual(next(v for v in reopened.list() if v["id"] == view["id"])["filters"], view["filters"])
        self.assertEqual(view["filters"]["phase"], "orchestrating")

    def test_round_trip_uses_stable_associations_and_survives_restart(self):
        original = self.views.create({"name": " Rig ", "filters": {
            "repository": self.repository["id"], "project": self.project["id"],
            "phase": "validation", "show": "completed", "search": " release "}, "group_by": "project"})
        self.assertEqual(original["name"], "Rig")
        self.assertEqual(original["filters"]["search"], "release")
        reopened = ViewStore(CoordinationStore(self.store.database_path))
        self.assertEqual(reopened.list(), [original])
        updated = reopened.update(original["id"], {"name": "Review", "version": 1})
        self.assertEqual(updated["filters"], original["filters"])
        self.assertEqual(updated["group_by"], "project")
        self.assertEqual(updated["version"], 2)
        self.assertEqual(self.views.list(), [updated])

    def test_defaults_none_filters_and_overlapping_views(self):
        all_open = self.views.create({"name": "Everything"})
        self.assertEqual(all_open["filters"], DEFAULT_FILTERS)
        self.assertEqual(all_open["group_by"], "phase")
        for filters in ({"repository": "__none__"}, {"project": "__none__"}, {"show": "later"}, {"phase": "new"},
                        {"repository": self.repository["id"]}, {"project": self.project["id"]}):
            item = self.views.create({"name": str(filters), "filters": filters})
            self.assertEqual(item["filters"], {**DEFAULT_FILTERS, **filters})

    def test_invalid_definitions_leave_store_unchanged(self):
        for body in ({}, {"name": ""}, {"name": " "}, {"name": "x" * 161}, {"name": True},
                     {"name": "All work"}, {"name": "bad", "unknown": True},
                     {"name": "bad", "filters": []}, {"name": "bad", "filters": {"unknown": "x"}},
                     {"name": "bad", "filters": {"show": "working"}}, {"name": "bad", "filters": {"phase": []}},
                     {"name": "bad", "filters": {"repository": "missing"}},
                     {"name": "bad", "filters": {"project": "missing"}},
                     {"name": "bad", "filters": {"search": "x" * 1001}}, {"name": "bad", "group_by": []}):
            with self.subTest(body=body), self.assertRaises(CoordinationError):
                self.views.create(body)
            self.assertEqual(self.views.list(), [])
        item = self.views.create({"name": "Rig"})
        for body in ({"name": " rig "}, {"name": "RIG"}):
            with self.assertRaises(CoordinationError):
                self.views.create(body)
        with self.assertRaises(CoordinationError):
            self.views.update(item["id"], {"name": "Changed", "filters": {"project": "missing"}, "version": 1})
        self.assertEqual(self.views.list(), [item])

    def test_stale_updates_and_deletes_cannot_overwrite_another_window(self):
        item = self.views.create({"name": "Rig"})
        updated = self.views.update(item["id"], {"filters": {"show": "attention"}, "version": 1})
        for version in (None, True, 1, "2"):
            with self.assertRaises(CoordinationError):
                self.views.update(item["id"], {"name": "Wrong", "version": version})
            with self.assertRaises(CoordinationError):
                self.views.delete(item["id"], {"version": version})
        self.assertEqual(self.views.list(), [updated])

    def test_concurrent_creates_and_ordering_are_atomic(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            items = list(pool.map(lambda i: self.views.create({"name": str(i)}), range(8)))
        self.assertEqual(len({item["position"] for item in items}), 8)
        ids = [item["id"] for item in self.views.list()]
        self.views.move(ids[2], {"direction": "left"})
        moved = [ids[0], ids[2], ids[1], *ids[3:]]
        self.assertEqual([item["id"] for item in self.views.list()], moved)
        self.views.move(ids[2], {"direction": "right"})
        self.views.move(ids[0], {"direction": "left"})
        self.views.move(ids[-1], {"direction": "right"})
        self.assertEqual([item["id"] for item in self.views.list()], ids)

    def test_delete_does_not_change_threads_or_other_views(self):
        self.store.register(session_id="work", client="codex", cwd=str(self.repo))
        before = self.store.threads.get("work")
        item = self.views.create({"name": "Rig", "filters": {"repository": self.repository["id"]}})
        other = self.views.create({"name": "Review"})
        self.views.delete(item["id"], {"version": 1})
        self.assertEqual(self.views.list(), [other])
        self.assertEqual(self.store.threads.get("work"), before)
        for action in (lambda: self.views.delete(item["id"], {"version": 1}),
                       lambda: self.views.update("all", {"name": "Oops", "version": 1}),
                       lambda: self.views.move("all", {"direction": "right"})):
            with self.assertRaises(CoordinationError):
                action()

    def test_http_views_share_database_and_keep_workspace_scope(self):
        sessions = BrowserSessions(self.store, str(self.repo), rpc_factory=FakeCodex)
        self.addCleanup(sessions.close)
        server = make_ui_server(self.store, port=0, browser_sessions=sessions)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(worker.join)
        self.addCleanup(server.shutdown)
        origin = "http://127.0.0.1:" + str(server.server_address[1])
        base = origin + "/api/browser/"
        with urllib.request.urlopen(base + "config") as response:
            token = json.load(response)["token"]

        def request(path, body=None, authenticated=True):
            headers = {"Content-Type": "application/json"}
            if authenticated:
                headers["X-Agent-Coord-Token"] = token
            req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None, headers=headers)
            with urllib.request.urlopen(req) as response:
                return json.load(response)

        one = request("views", {"name": "Rig", "filters": {"repository": self.repository["id"]}})
        two = request("views", {"name": "Unassigned", "filters": {"project": "__none__"}})
        self.assertEqual(request("views")["data"], self.views.list())
        updated = request("views/" + one["id"], {"name": "Rig review", "version": 1})
        self.assertEqual(updated["version"], 2)
        moved = request("views/" + two["id"] + "/move", {"direction": "left"})["data"]
        self.assertEqual([v["id"] for v in moved], [two["id"], one["id"]])
        request("views/" + one["id"] + "/delete", {"version": 2})
        for path, body, authenticated, status in (
            ("views", {"name": "Forged"}, False, 403),
            ("views/" + two["id"], {"version": 1}, True, 400),
            ("views/" + two["id"] + "/move", {"direction": "down"}, True, 400),
            ("views/" + two["id"] + "/delete", {}, True, 400),
        ):
            with self.assertRaises(urllib.error.HTTPError) as error:
                request(path, body, authenticated)
            self.assertEqual(error.exception.code, status)
            error.exception.close()
        self.store.register(session_id="inside", client="codex", cwd=str(self.repo))
        self.store.register(session_id="outside", client="codex", cwd=str(self.root))
        self.assertEqual([t["thread_id"] for t in request("threads")["data"]], ["inside"])
        self.assertEqual(sessions.rpc.calls, [])
        for path in ("/views.js", "/views.css"):
            with urllib.request.urlopen(origin + path) as response:
                self.assertTrue(response.read())

    @unittest.skipUnless(shutil.which("node"), "Node is required for view interaction tests")
    def test_browser_views(self):
        result = subprocess.run(["node", "--test", str(Path(__file__).with_name("test_web_views.js"))],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
