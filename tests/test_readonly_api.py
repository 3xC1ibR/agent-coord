from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))

from agent_coord import observer_queries
from agent_coord.codex_app_server import BrowserSessions
from agent_coord.store import CoordinationStore
from agent_coord.ui import make_ui_server
from test_codex_app_server import FakeCodex


class ReadOnlyHTTPTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / ".git").mkdir()
        self.store = CoordinationStore(self.root / "state.sqlite3")
        self.sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.server = make_ui_server(self.store, port=0, cwd=str(self.root), browser_sessions=self.sessions)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self.stop_server)
        self.url = "http://127.0.0.1:" + str(self.server.server_address[1])
        self.identity = self.sessions.create({"name": "Saved conversation"})["session"]["thread_id"]
        self.assertTrue(self.sessions.persistence.flush(timeout=3, key=self.identity))

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(2)

    def request(self, path, *, headers=None):
        request = urllib.request.Request(self.url + path, headers=headers or {})
        try:
            response = urllib.request.urlopen(request, timeout=2)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.status, json.loads(response.read())

    @contextmanager
    def held_writer(self):
        writer = sqlite3.connect(self.store.database_path)
        try:
            writer.execute("BEGIN IMMEDIATE")
            yield writer
        finally:
            writer.rollback()
            writer.close()

    def test_thread_inventory_and_detail_return_while_writer_is_held(self):
        # Legacy association repair and expired reminders used to turn these
        # reads into writes. Neither is allowed to delay an HTTP observer.
        reminder = self.store.clock() + 3600
        self.store.threads.update(self.identity, snoozed_until=reminder)
        with self.store._connection() as db:
            db.execute("DELETE FROM thread_organization WHERE thread_id = ?", (self.identity,))
        with self.held_writer() as writer, patch.object(self.store, "clock", return_value=reminder + 1), patch.object(
            self.sessions.rpc, "request", side_effect=AssertionError("Read contacted the provider")
        ) as provider:
            for path in ("/api/browser/threads", "/api/browser/threads/" + self.identity):
                with self.subTest(path=path):
                    started = time.monotonic()
                    status, result = self.request(path)
                    self.assertLess(time.monotonic() - started, 1.0)
                    self.assertEqual(status, 200, result)
                    thread = result["data"][0] if path.endswith("threads") else result
                    self.assertEqual(thread["thread_id"], self.identity)
                    self.assertEqual(thread["attention"], "now")
                    self.assertTrue(thread["repository_id"])
            provider.assert_not_called()
            self.assertEqual(writer.execute("SELECT attention FROM work_threads WHERE thread_id = ?", (self.identity,)).fetchone()[0], "later")
            self.assertIsNone(writer.execute("SELECT 1 FROM thread_organization WHERE thread_id = ?", (self.identity,)).fetchone())

    def test_recent_and_full_history_return_saved_content_without_provider_admission(self):
        turns = [{"id": str(index), "status": "completed", "items": [
            {"id": "answer-" + str(index), "type": "agentMessage", "text": "Saved answer " + str(index)}
        ]} for index in range(45)]
        self.sessions._remember_thread({"id": self.identity, "turns": turns})
        self.assertTrue(self.sessions.persistence.flush(timeout=3, key=self.identity))
        self.sessions.loaded.discard(self.identity)
        self.sessions.histories.pop(self.identity)
        # A request already resuming this conversation also must not prevent
        # another window from reading the saved transcript.
        with self.held_writer(), self.sessions._thread_lock(self.identity), patch.object(
            self.sessions.rpc, "request", side_effect=AssertionError("Read contacted the provider")
        ) as provider:
            for view in ("recent", "complete"):
                with self.subTest(view=view):
                    started = time.monotonic()
                    status, result = self.request("/api/browser/sessions/" + self.identity,
                                                  headers={"X-Agent-Coord-History-View": view})
                    self.assertLess(time.monotonic() - started, 1.0)
                    self.assertEqual(status, 200, result)
                    self.assertEqual(result["session"]["thread_id"], self.identity)
                    self.assertEqual(result["thread"]["turns"][-1], turns[-1])
                    if view == "complete":
                        self.assertEqual(result["thread"]["turns"], turns)
                    else:
                        self.assertLess(len(result["thread"]["turns"]), len(turns))
                    self.assertFalse(result["running"])
            provider.assert_not_called()
            self.assertNotIn(self.identity, self.sessions.loaded)

    def test_metadata_batch_preserves_workspace_boundary_under_writer_contention(self):
        self.store.register(session_id="outside", client="codex", cwd=str(self.root.parent / "another-workspace"))
        query = urllib.parse.urlencode({"id": ["outside", self.identity, "missing", self.identity]}, doseq=True)
        with self.held_writer(), patch.object(
            self.sessions.rpc, "request", side_effect=AssertionError("Metadata contacted the provider")
        ) as provider:
            started = time.monotonic()
            status, result = self.request("/api/browser/threads/metadata?" + query)
            self.assertLess(time.monotonic() - started, 1.0)
            self.assertEqual(status, 200, result)
            self.assertEqual(result["missing"], ["outside", "missing"])
            self.assertEqual([row["thread_id"] for row in result["data"]], [self.identity])
            self.assertEqual(set(result["data"][0]), set(observer_queries.METADATA_FIELDS))
            provider.assert_not_called()

    def test_http_read_boundaries_reject_nested_database_mutations(self):
        # A held writer alone could conceal a write attempt until timeout.
        # With no competing writer, the database must still reject it.
        rejections = []

        def check_boundary(original):
            def checked(*args, **kwargs):
                with self.store._connection() as db:
                    try:
                        db.execute("UPDATE work_threads SET title = 'Unexpected write' WHERE thread_id = ?", (self.identity,))
                    except sqlite3.OperationalError as exc:
                        rejections.append(str(exc))
                    else:
                        rejections.append(None)
                return original(*args, **kwargs)
            return checked

        cases = (
            (self.sessions, "work_thread_inventory_response", "/api/browser/threads", None),
            (self.sessions, "work_thread", "/api/browser/threads/" + self.identity, None),
            (self.sessions, "read", "/api/browser/sessions/" + self.identity, {"X-Agent-Coord-History-View": "recent"}),
            (self.sessions, "read", "/api/browser/sessions/" + self.identity, None),
            (observer_queries, "matches_workspace", "/api/browser/threads/metadata?id=" + self.identity, None),
        )
        for target, name, path, headers in cases:
            with self.subTest(path=path, headers=headers):
                before = len(rejections)
                with patch.object(target, name, side_effect=check_boundary(getattr(target, name))):
                    status, result = self.request(path, headers=headers)
                self.assertEqual(status, 200, result)
                self.assertGreater(len(rejections), before)
                self.assertTrue(all(error and "readonly" in error.lower() for error in rejections[before:]), rejections)
        self.assertEqual(self.store.threads.get(self.identity)["title"], "Saved conversation")

    def test_metadata_rejects_oversized_batches(self):
        query = urllib.parse.urlencode({"id": [str(index) for index in range(201)]}, doseq=True)
        status, result = self.request("/api/browser/threads/metadata?" + query)
        self.assertEqual(status, 400)
        self.assertIn("at most 200", result["error"])


if __name__ == "__main__":
    unittest.main()
