"""Browser conversation opening, revisions, and workspace boundaries."""
from __future__ import annotations

import http.client
import hashlib
import json
import tempfile
import threading
import unittest
import uuid
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode
from unittest.mock import patch

from test_codex_app_server import BrowserSessions, CoordinationStore, FakeCodex
from agent_coord import runtime_edge, ui
from agent_coord.app_control import AppControl
from agent_coord.store import CoordinationError
from agent_coord.transcript_store import TranscriptStore
from agent_coord.ui import make_ui_server


class ConversationOpenTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.first, self.second = self.root / "first", self.root / "second"
        for path in (self.first, self.second):
            (path / ".git").mkdir(parents=True)
        self.store = CoordinationStore(self.root / "coordination.sqlite3")
        self.sessions = BrowserSessions(self.store, None, rpc_factory=FakeCodex)
        self.addCleanup(self.sessions.close)

    def browser_thread(self, cwd, **values):
        thread = self.sessions.create({"cwd": str(cwd), **values})["session"]["thread_id"]
        self.assertTrue(self.sessions.persistence.flush(key=thread))
        return thread

    def serve(self, sessions):
        server = make_ui_server(self.store, port=0, cwd=sessions.cwd, browser_sessions=sessions)
        worker = threading.Thread(target=server.serve_forever)
        worker.start()

        def close():
            server.shutdown()
            server.server_close()
            worker.join(2)
        self.addCleanup(close)

        def get(route, headers=None):
            with closing(http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)) as connection:
                connection.request("GET", "/api/browser/" + route, headers=headers or {})
                response = connection.getresponse()
                return response.status, json.loads(response.read())
        get.server_port = server.server_port
        return get

    def test_open_seeks_indexed_history_and_revisions_match_recent_detail(self):
        thread = self.browser_thread(self.first)
        transcript = {"id": thread, "turns": [
            {"id": str(index), "status": "completed", "items": [{
                "id": str(index), "type": "commandExecution", "aggregatedOutput": "a" * 64000}]}
            for index in range(100)]}
        transcripts = TranscriptStore(self.store)
        transcripts.save(thread, transcript, activate=True)
        with self.sessions.lock:
            self.sessions.histories.pop(thread, None)
        self.store.asks.create(thread, "Choose an implementation.", blocking=True)
        get = self.serve(self.sessions)
        _, config = get("config")
        self.assertEqual(config["conversationOpen"], 1)
        self.assertTrue(config["conversationRevisionEpoch"])
        self.assertTrue(config["conversationCacheScope"])
        route = f"conversations/{thread}/open"
        provider_calls = list(self.sessions.rpc.calls)
        with patch.object(self.sessions, "_resume_thread", side_effect=AssertionError("provider admission")), \
                patch.object(TranscriptStore, "read_full", side_effect=AssertionError("full history")):
            status, result = get(route)
            self.assertEqual(status, 200)
            self.assertEqual(self.sessions.rpc.calls, provider_calls)
            page = result["recent_turns"]
            self.assertEqual((page["beforeSeq"], page["earlierTurns"], page["totalTurns"], len(page["turns"])), (60, 60, 100, 40))
            self.assertEqual(len(page["turns"][0]["items"][0]["aggregatedOutput"]), 512)
            self.assertEqual(result["agent_tree"]["root_thread_id"], thread)
            self.assertEqual(result["asks"][0]["session_id"], thread)
            self.assertTrue(result["work_thread"]["needs_attention"])
            self.assertEqual(result["sidecars"], {"watches": [], "requests": []})
            self.assertEqual(result["schedules"], [])
            self.assertIsNone(result["models"])
            keys = ("history_revision", "stream_seq", "metadata_revision")
            versions = {key: result[key] for key in keys}
            self.assertTrue(all(type(value) is int for value in versions.values()))
            _, repeated = get(route)
            self.assertEqual({key: repeated[key] for key in keys}, versions)
            _, normal = get("sessions/" + thread, {"X-Agent-Coord-History-View": "recent",
                                                   "X-Agent-Coord-Conversation-Revisions": "1"})
            self.assertEqual(normal["thread"], page)
            self.assertEqual({key: normal[key] for key in keys}, versions)
        self.store.threads.update(thread, title="Changed title")
        _, renamed = get(route)
        self.assertEqual(renamed["history_revision"], result["history_revision"])
        self.assertGreater(renamed["metadata_revision"], result["metadata_revision"])
        transcript["turns"][-1]["items"] = [{"id": "replacement", "type": "agentMessage", "text": "Changed"}]
        transcripts.save(thread, transcript, activate=True)
        _, replaced = get(route)
        self.assertGreater(replaced["history_revision"], renamed["history_revision"])
        self.assertEqual(replaced["recent_turns"]["turns"][-1]["items"][0]["text"], "Changed")
        self.sessions._publish("item/agentMessage/delta", {"threadId": thread, "delta": "more"})
        _, streamed = get(route)
        self.assertGreater(streamed["stream_seq"], replaced["stream_seq"])

    def test_legacy_open_marks_deferred_without_observing_provider(self):
        self.store.register(session_id="terminal", client="codex", cwd=str(self.first))
        with patch.object(self.sessions, "_stored_thread", side_effect=AssertionError("legacy provider read")):
            result = self.sessions.conversation_open.read("terminal")
        self.assertTrue(result["legacyDetailRequired"])
        self.assertTrue(result["deferReadAcknowledgment"])
        self.assertEqual(result["recent_turns"]["turns"], [])

    def test_cross_repository_workspace_membership_family_and_scoped_denial(self):
        group = self.store.threads.organization.create_project("Migration", str(self.first))
        parent = self.browser_thread(self.first, name="Migration supervisor", project_id=group["id"])
        child = self.browser_thread(self.second, name="Migration specialist", project_id=group["id"])
        other = self.browser_thread(self.second, name="Migration unrelated")
        control = AppControl(self.store)
        request = control.request("create", parent, {"mode": "orchestration", "prompt": "Migration work"})
        control.claim(request["request_id"], "test")
        control.identified(request["request_id"], child)
        control.complete(request["request_id"], result={"thread_id": child})
        get = self.serve(self.sessions)
        _, detail = get(f"conversations/{child}/open")
        self.assertEqual(detail["work_thread"]["cwd"], str(self.second))
        self.assertEqual(detail["work_thread"]["project_launch_cwd"], str(self.first))
        self.assertEqual(detail["agent_tree"]["root_thread_id"], parent)
        self.assertEqual({row["thread_id"] for row in detail["agent_tree"]["data"]}, {parent, child})
        selected = {"project": group["id"], "project_cwd": str(self.first)}
        _, listing = get("threads?" + urlencode(selected))
        self.assertEqual({t["thread_id"] for t in listing["data"]}, {parent, child})
        _, folder = get("threads?" + urlencode({"project_cwd": str(self.first)}))
        self.assertEqual({t["thread_id"] for t in folder["data"]}, {parent, child})
        _, filtered = get("threads?" + urlencode({**selected, "directory_root": str(self.second)}))
        self.assertEqual([t["thread_id"] for t in filtered["data"]], [child])
        _, results = get("search?" + urlencode({"q": "Migration", "scope": "current", **selected}))
        self.assertEqual({t["thread_id"] for t in results["items"]}, {parent, child})
        _, folder_results = get("search?" + urlencode({"q": "Migration", "scope": "all", "project_cwd": str(self.first)}))
        self.assertEqual({t["thread_id"] for t in folder_results["items"]}, {parent, child})

        scoped = BrowserSessions(self.store, str(self.first), rpc_factory=FakeCodex)
        self.addCleanup(scoped.close)
        scoped_get = self.serve(scoped)
        for route in (f"conversations/{child}/open", f"threads/{child}", f"threads/{child}/agent-tree",
                      f"threads/{child}/scratchpad", f"sessions/{child}?before_seq=10"):
            self.assertEqual(scoped_get(route)[0], 400, route)
        _, scoped_results = scoped_get("search?" + urlencode({"q": "Migration", "scope": "current", **selected}))
        self.assertEqual([t["thread_id"] for t in scoped_results["items"]], [parent])
        _, scoped_detail = scoped_get(f"conversations/{parent}/open")
        self.assertEqual([t["thread_id"] for t in scoped_detail["agent_tree"]["data"]], [parent])
        self.assertNotIn(other, {t["thread_id"] for t in listing["data"]})

    def test_managed_worker_cannot_open_as_an_independent_conversation(self):
        self.store.register(session_id="parent", client="codex", cwd=str(self.first))
        self.store.register(session_id="managed", client="codex", cwd=str(self.second))
        delegation = self.store.create_delegation(parent_session_id="parent", cwd=str(self.second),
            scopes=["managed.py"], instructions="Work in the managed pane.", mode="reviewed")
        self.store.attach_delegation(delegation["delegation_id"], "managed")
        with self.assertRaisesRegex(CoordinationError, "not found in this workspace"):
            self.sessions.conversation_open.read("managed")

    def test_open_unloaded_history_does_not_wait_for_an_unrelated_provider_commit(self):
        self.store.register(session_id="indexed", client="codex", cwd=str(self.first))
        TranscriptStore(self.store).save("indexed", {"id": "indexed", "turns": [
            {"id": "saved-turn", "items": [], "status": "completed"}]}, activate=True)
        observation = SimpleNamespace(unloaded_observation={"excluded": {"unrelated"}})
        with patch.object(self.sessions, "projection", observation, create=True), self.sessions.lock:
            result, finished = [], threading.Event()

            def read():
                result.append(self.sessions.conversation_open.read("indexed"))
                finished.set()

            worker = threading.Thread(target=read)
            worker.start()
            fast = finished.wait(2)
        worker.join(5)
        self.assertTrue(fast)
        self.assertEqual(result[0]["recent_turns"]["turns"][0]["id"], "saved-turn")

    def test_connected_stream_invalidates_external_ask_and_scratchpad_writes(self):
        thread = self.browser_thread(self.first)
        get = self.serve(self.sessions)
        _, config = get("config")
        self.assertEqual(config["conversationMetadataEvents"], 1)
        route = "/api/browser/events?" + urlencode({"after": config["eventCursor"],
            "completion_after": config["completionCursor"], "metadata_after": config["conversationMetadataCursor"]})
        with closing(http.client.HTTPConnection("127.0.0.1", get.server_port, timeout=5)) as connection:
            connection.request("GET", route)
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            writer = CoordinationStore(self.store.database_path)
            writer.asks.create(thread, "Read from another store.", blocking=True)
            writer.threads.scratchpad(thread, "Updated externally")
            revisions, topics = [], set()
            while not {"asks", "scratchpad"} <= topics:
                line = response.readline()
                self.assertTrue(line)
                if not line.startswith(b"data: "):
                    continue
                batch = json.loads(line[6:])
                for event in batch["events"]:
                    if event["method"] == "browser/conversationInvalidated":
                        data = event["params"]
                        self.assertEqual(data["threadIds"], [thread])
                        self.assertFalse(data["reset"])
                        revisions.append(data["revision"])
                        topics.update(data["topics"])
            self.assertGreater(revisions[0], config["conversationMetadataCursor"])

    def test_metadata_revisions_follow_settings_membership_asks_and_schedules(self):
        thread = self.browser_thread(self.second)
        group = self.store.threads.organization.create_project("Delivery", str(self.first))
        actions = [
            lambda: self.store.threads.update(thread, pinned=True),
            lambda: self.store.threads.update(thread, quiet=True),
            lambda: self.store.threads.update(thread, project_id=group["id"]),
            lambda: self.store.asks.create(thread, "Which branch?", blocking=False, context="Default: main."),
            lambda: self.sessions.schedules.create({"id": str(uuid.uuid4()), "target_thread_id": thread,
                "message": "Check deployment.", "run_at": self.store.clock() + 3600, "timezone": "UTC"}),
        ]
        old = self.sessions.conversation_open.read(thread)
        for change in actions:
            change()
            fresh = self.sessions.conversation_open.read(thread)
            self.assertGreater(fresh["metadata_revision"], old["metadata_revision"])
            self.assertEqual(fresh["history_revision"], old["history_revision"])
            old = fresh
        self.assertTrue(fresh["work_thread"]["pinned"])
        self.assertTrue(fresh["work_thread"]["quiet"])
        self.assertEqual(fresh["work_thread"]["project_id"], group["id"])
        self.assertEqual(fresh["asks"][0]["question"], "Which branch?")
        self.assertEqual(fresh["schedules"][0]["target_thread_id"], thread)

    def test_stable_edge_forwards_bundle_and_open_with_its_origin(self):
        thread = self.browser_thread(self.first)
        get = self.serve(self.sessions)
        _, config = get("config")
        edge_directory = self.root / "edge"
        edge_directory.mkdir()
        edge = runtime_edge.EdgeServer(edge_directory, {})
        worker = threading.Thread(target=edge.serve_forever)
        worker.start()
        self.addCleanup(edge.server_close)
        self.addCleanup(worker.join, 3)
        self.addCleanup(edge.shutdown)
        runtime_edge.publish(edge_directory, {"url": f"http://127.0.0.1:{get.server_port}/",
            "instance": "a" * 32, "status": "serving"}, config["token"], ready=True)
        web = self.root / "web"
        dist = web / "dist"
        dist.mkdir(parents=True)
        script = b"globalThis.fixtureReady = true;"
        token_css = b":root { --surface: white; }"
        name = "app-" + hashlib.sha256(script).hexdigest()[:20] + ".js"
        document = f'<html><script src="/dist/{name}"></script></html>'.encode()
        (dist / name).write_bytes(script)
        (dist / "index.html").write_bytes(document)
        (web / "tokens.css").write_bytes(token_css)
        (web / "openui-frame.html").write_bytes(b"<!doctype html>")
        with patch.object(ui, "_WEB_ROOT", web), closing(
                http.client.HTTPConnection("127.0.0.1", edge.server_port, timeout=5)) as client:
            expected = {"/": document, f"/dist/{name}": script, "/tokens.css": token_css}
            for route, body in expected.items():
                client.request("GET", route)
                response = client.getresponse()
                self.assertEqual((response.status, response.read()), (200, body))
                if route.startswith("/dist/"):
                    self.assertIn("immutable", response.getheader("Cache-Control"))
            client.request("GET", "/openui-frame.html")
            response = client.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIn("script-src " + edge.origin, response.getheader("Content-Security-Policy"))
            response.read()
            client.request("GET", f"/api/browser/conversations/{thread}/open")
            response = client.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read())["session"]["thread_id"], thread)
