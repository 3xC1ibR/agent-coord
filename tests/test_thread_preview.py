from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))
from agent_coord.codex_app_server import BrowserSessions
from agent_coord.store import CoordinationStore
from agent_coord.thread_preview import latest_history_message, latest_terminal_message, thread_preview
from agent_coord.ui import make_ui_server
from test_codex_app_server import FakeCodex


class ThreadPreviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = CoordinationStore(self.root / "state.sqlite3")
        self.sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(self.sessions.close)

    def test_latest_message_skips_tools_and_empty_streaming_items(self):
        history = {"turns": [{"items": [{"type": "agentMessage", "text": "Earlier"}]},
                             {"items": [{"type": "userMessage", "content": [{"type": "text", "text": "Newest"}]},
                                        {"type": "commandExecution", "aggregatedOutput": "private tool output"},
                                        {"type": "agentMessage", "text": ""}]}]}
        self.assertEqual(latest_history_message(history)["text"], "Newest")
        history["turns"][-1]["items"].append({"type": "agentMessage", "text": "x" * 2000})
        message = latest_history_message(history)
        self.assertEqual(message["role"], "assistant")
        self.assertEqual(len(message["text"]), 1600)
        self.assertTrue(message["truncated"])

    def test_preview_never_resumes_or_marks_seen_including_saved_and_closed(self):
        thread_id = self.sessions.create({"name": "Preview"})["session"]["thread_id"]
        self.sessions._remember_thread({"id": thread_id, "turns": [{"items": [{"type": "agentMessage", "text": "Latest result"}]}]})
        self.sessions.loaded.clear()
        for closed in (False, True):
            if closed:
                self.store.threads.update(thread_id, attention="archived")
            before = self.store.threads.get(thread_id)
            calls = list(self.sessions.rpc.calls)
            self.assertEqual(thread_preview(self.sessions, thread_id)["latest_message"]["text"], "Latest result")
            self.assertEqual(self.sessions.rpc.calls, calls)
            self.assertEqual(self.store.threads.get(thread_id), before)
            self.assertNotIn(thread_id, self.sessions.loaded)

    def test_terminal_transcript_ignores_tools_and_partial_records(self):
        thread = {"thread_id": "11111111-1111-4111-8111-111111111111", "client": "codex"}
        directory = self.root / "sessions" / "2026"
        directory.mkdir(parents=True)
        path = directory / ("rollout-" + thread["thread_id"] + ".jsonl")
        def record(role, text):
            return {"type": "response_item", "timestamp": "2026-10-06T10:00:00Z", "payload": {"type": "message", "role": role, "content": [{"type": "output_text", "text": text}]}}
        path.write_text(json.dumps(record("assistant", "Actual latest message")) + "\n" + json.dumps(record("developer", "hidden instructions")) + '\n{"partial":')
        with patch.dict(os.environ, {"CODEX_HOME": str(self.root)}):
            message = latest_terminal_message(thread)
        self.assertEqual(message["text"], "Actual latest message")
        self.assertEqual(message["timestamp"], "2026-10-06T10:00:00Z")

    def test_claude_transcript_and_missing_history(self):
        thread = {"thread_id": "11111111-1111-4111-8111-111111111111", "client": "claude"}
        directory = self.root / "projects" / "workspace"
        directory.mkdir(parents=True)
        path = directory / (thread["thread_id"] + ".jsonl")
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.root)}):
            self.assertIsNone(latest_terminal_message(thread))
            path.write_text(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "Claude result"}, {"type": "tool_use", "name": "shell"}]}}) + "\n")
            self.assertEqual(latest_terminal_message(thread)["text"], "Claude result")

    def test_preview_route_and_assets_enforce_workspace(self):
        self.store.register(session_id="visible", client="codex", cwd=str(self.root))
        self.store.register(session_id="outside", client="codex", cwd=str(self.root.parent))
        server = make_ui_server(self.store, host="127.0.0.1", port=0, cwd=str(self.root), browser_sessions=self.sessions)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            base = "http://127.0.0.1:" + str(server.server_address[1])
            with urllib.request.urlopen(base + "/api/browser/threads/visible/preview") as response:
                self.assertIsNone(json.load(response)["latest_message"])
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(base + "/api/browser/threads/outside/preview")
            error.exception.close()
            for asset in ("thread-hover.js", "thread-hover.css"):
                with urllib.request.urlopen(base + "/" + asset) as response:
                    self.assertEqual(response.status, 200)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(5)


class LensPreviewTests(ThreadPreviewTests):
    """The preview carries what the lens needs beyond the latest message."""

    def test_preview_includes_orchestrated_children_and_the_previous_checkpoint(self):
        parent = self.sessions.create({"name": "Parent"})["session"]["thread_id"]
        child = self.sessions.create({"name": "Child"})["session"]["thread_id"]
        other = self.sessions.create({"name": "Unrelated"})["session"]["thread_id"]
        with self.store._connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS app_control_requests (
                    request_id TEXT PRIMARY KEY, operation TEXT NOT NULL,
                    sender_session_id TEXT NOT NULL, cwd TEXT NOT NULL,
                    thread_id TEXT, payload_json TEXT NOT NULL,
                    status TEXT NOT NULL, owner TEXT, result_json TEXT,
                    error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL
                );""")
            db.execute("INSERT INTO app_control_requests (request_id, operation, sender_session_id, cwd, thread_id, payload_json, status, created_at, updated_at) VALUES (?, 'create', ?, ?, ?, ?, 'completed', 1, 1)",
                       ("req-1", parent, str(self.root), child, json.dumps({"mode": "orchestration"})))
        before = thread_preview(self.sessions, parent)
        self.assertEqual([c["thread_id"] for c in before["children"]], [child])
        self.assertIsNone(before["previous_checkpoint"])
        self.assertEqual(thread_preview(self.sessions, other)["children"], [])
        self.store.threads.checkpoint(parent, {"phase": "planning", "summary": "Planned the change."})
        self.store.threads.checkpoint(parent, {"phase": "implementation", "summary": "Implementing it."})
        self.store.record_tool(child, "commandExecution", "pytest -q", kind="run")
        after = thread_preview(self.sessions, parent)
        self.assertEqual(after["previous_checkpoint"]["summary"], "Planned the change.")
        self.assertEqual(after["previous_checkpoint"]["phase"], "planning")
        self.assertEqual(after["thread"]["checkpoint"]["summary"], "Implementing it.")
        self.assertEqual(after["children"][0]["last_tool"]["label"], "pytest -q")
        self.assertIn("last_tool", after["thread"])
