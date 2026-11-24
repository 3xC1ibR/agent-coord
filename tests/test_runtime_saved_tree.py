"""Agent relationships remain visible through the update listener while the old server is blocked."""
from contextlib import closing
import http.client
import json
import shutil
import sqlite3
import tempfile
import threading
import unittest
import historical_gateway_fixture
from unittest.mock import patch

import test_ui
from test_subagent_close import attach
from historical_gateway import desktop_runtime, runtime_interaction_gateway as gateway
from agent_coord import supervision
from historical_gateway.runtime_saved_views import SavedRuntimeViews


class SavedTreeHTTPTests(unittest.TestCase):
    setUp = test_ui.BrowserHTTPTests.setUp
    stop_server = test_ui.BrowserHTTPTests.stop_server
    request = test_ui.BrowserHTTPTests.request

    def test_staged_and_offline_reads_match_native_family_and_scratchpad_when_coordinator_is_blocked(self):
        lead, successor, child, fork = [self.sessions.create({})["session"]["thread_id"] for _ in range(4)]
        attach(self.store, lead, child)
        self.store.threads.record_fork(fork, self.store.threads.get(child))
        supervision.transfer(self.store, requester=lead, from_session=lead, to_session=successor,
                             thread_ids=[child], request_id="transfer", reason="Move responsibility", workspace=str(self.root))
        self.store.register(session_id="terminal", client="codex", cwd=str(self.root))
        delegation = self.store.create_delegation(parent_session_id=child, cwd=str(self.root),
            scopes=["one"], instructions="Read", mode="reviewed")
        self.store.attach_delegation(delegation["delegation_id"], "terminal")
        elsewhere = tempfile.TemporaryDirectory()
        self.addCleanup(elsewhere.cleanup)
        self.store.register(session_id="other-workspace", client="codex", cwd=elsewhere.name)
        self.store.threads.record_fork("other-workspace", self.store.threads.get(successor))
        self.store.threads.update(fork, attention="archived")
        self.store.threads.scratchpad(child, "A committed note")
        route = "/api/browser/threads/" + child
        status, native = self.request(route + "/agent-tree")
        self.assertEqual(status, 200)
        self.assertEqual(native["root_thread_id"], successor)
        _, note = self.request(route + "/scratchpad")
        self.assertEqual(note, {"text": "A committed note"})

        # The source coordinator can still write activity while the gateway serves
        # reads. Isolate its committed state to detect writes by the gateway itself.
        saved_database = self.root / "saved.sqlite3"
        with closing(sqlite3.connect(self.store.database_path)) as source, closing(sqlite3.connect(saved_database)) as saved:
            source.backup(saved)
        saved_runtime = desktop_runtime.runtime_directory(saved_database)
        saved_runtime.mkdir(exist_ok=True)
        shutil.copyfile(desktop_runtime.runtime_directory(self.store.database_path) / "schema.json",
                        saved_runtime / "schema.json")
        observer = gateway.InteractionGateway(saved_database, self.root / "gateway", self.root)
        self.addCleanup(observer.delivery_pool.shutdown)
        observer.backend = (self.url + "/", "unused", "old", "old-payload")
        observer.views = SavedRuntimeViews(saved_database, str(self.root))
        self.addCleanup(observer.views.close)
        server = gateway.GatewayServer(("127.0.0.1", 0), gateway.GatewayHandler)
        server.gateway = observer
        observer.url = f"http://127.0.0.1:{server.server_port}/"
        self.addCleanup(server.server_close)
        worker = threading.Thread(target=server.serve_forever)
        worker.start()
        self.addCleanup(worker.join, 3)
        self.addCleanup(server.shutdown)
        def read(path):
            with closing(http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)) as conn:
                conn.request("GET", path)
                response = conn.getresponse()
                return response.status, json.loads(response.read())

        def unavailable(handler, *args):
            return handler.json(503, {"error": "The previous coordinator is blocked."})
        fields = ("thread_id", "title", "created_by", "creation_mode", "created_by_thread_id",
                  "orchestration_workspaces", "supervisor_thread_id", "supervised_by_user", "role",
                  "agent_parent_id", "browser_session", "attention", "forked_from", "checkpoint")
        def records(graph):
            return [{key: item.get(key) for key in fields} for item in graph["data"]]
        calls = list(self.sessions.rpc.calls)
        with patch.object(gateway.GatewayHandler, "relay", unavailable), closing(sqlite3.connect(saved_database)) as db:
            previous = db.execute("PRAGMA data_version").fetchone()
            for fence in ({}, {"state": "prepared"}):
                observer.fence = fence
                status, saved = read(route + "/agent-tree")
                self.assertEqual(status, 200)
                self.assertEqual(saved["root_thread_id"], native["root_thread_id"])
                self.assertEqual(records(saved), records(native))
                self.assertEqual({item["thread_id"] for item in saved["data"]}, {successor, child, fork, "terminal"})
                self.assertTrue(saved["continuitySnapshot"])
                self.assertEqual(read(route + "/scratchpad"), (200, note))
                for suffix in ("/agent-tree", "/scratchpad"):
                    for identity in ("missing", "other-workspace"):
                        self.assertEqual(read("/api/browser/threads/" + identity + suffix)[0], 409)
            self.assertEqual(db.execute("PRAGMA data_version").fetchone(), previous)
        self.assertEqual(self.sessions.rpc.calls, calls)
