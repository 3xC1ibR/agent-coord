from __future__ import annotations

import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from test_browser_providers import claude_factory
from test_codex_app_server import BrowserSessions, BrowserBusyError, CoordinationError, CoordinationStore, FakeCodex


class ClaudeForkTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.config = self.root / "claude-config"
        env = patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.config)})
        env.start()
        self.addCleanup(env.stop)
        self.store = CoordinationStore(self.root / "state.sqlite3")
        self.sessions = self.make_sessions()

    def make_sessions(self):
        sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex, claude_factory=claude_factory)
        self.addCleanup(sessions.close)
        return sessions

    def create(self):
        return self.sessions.create({"client": "claude", "name": "Source", "model": "sonnet", "effort": "high"})["session"]["thread_id"]

    def turn(self, thread_id, text="Original", *, native=True):
        self.sessions.send(thread_id, {"message": text})
        connection = self.sessions.claude.connections[thread_id]
        if native:
            connection.callback({"type": "system", "subtype": "init"})
        connection.callback({"type": "assistant", "message": {"content": [{"type": "text", "text": "Answer"}]}})
        connection.callback({"type": "result", "subtype": "success"})

    def transcript(self, thread_id):
        path = self.config / "projects" / "workspace" / f"{thread_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        prompt_id = self.sessions.claude.connections[thread_id].writes[-1]["uuid"]
        entries = [
            {"type": "user", "sessionId": thread_id, "uuid": prompt_id, "parentUuid": None,
             "message": {"role": "user", "content": "Original"}},
            {"type": "assistant", "sessionId": thread_id, "uuid": str(uuid.uuid4()), "parentUuid": prompt_id,
             "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "tool-1", "name": "Read", "input": {"file_path": "notes"}}]}},
            {"type": "content-replacement", "replacements": [{"toolUseId": "tool-1", "content": "Saved context"}]},
        ]
        path.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
        return path

    def fork_path(self, child):
        with self.store._connection() as db:
            row = db.execute("SELECT fork_path FROM claude_sessions WHERE thread_id = ?", (child,)).fetchone()
        return Path(row[0]) if row[0] else None

    def test_history_settings_lineage_and_independent_execution(self):
        parent = self.create()
        self.turn(parent)
        path = self.transcript(parent)
        self.sessions.update(parent, {"yolo": True})
        self.store.threads.update(parent, pinned=True)
        self.store.threads.checkpoint(parent, {"phase": "planning", "summary": "Parent plan", "next_action": "", "next_actor": "nobody"})
        before = self.store.threads.get(parent, history=True)
        connections = dict(self.sessions.claude.connections)
        fork = self.sessions.fork_work_thread(parent)
        child = fork["thread_id"]
        self.assertEqual(fork["client"], "claude")
        self.assertEqual(fork["forked_from"], {"thread_id": parent, "title": "Source"})
        self.assertFalse(fork["pinned"])
        self.assertFalse(fork["unread"])
        self.assertIsNone(fork["checkpoint"])
        self.assertIsNone(fork["turn_completion"])
        self.assertEqual(fork["original_request"], before["original_request"])
        detail = self.sessions.read(child)
        self.assertEqual(detail["thread"]["turns"], self.sessions.read(parent)["thread"]["turns"])
        self.assertEqual(tuple(detail["session"][k] for k in ("client", "model", "effort", "yolo")), ("claude", "sonnet", "high", True))
        self.assertEqual(self.sessions.claude.connections, connections)
        self.assertEqual(self.store.threads.get(parent, history=True), before)
        self.assertEqual(self.fork_path(child).read_bytes(), path.read_bytes())
        self.assertEqual(self.fork_path(child).stat().st_mode & 0o777, 0o600)
        self.assertFalse(self.sessions.rpc.calls)
        self.sessions.send(child, {"message": "Different direction"})
        options = self.sessions.claude.connections[child].options
        self.assertEqual(options["forkPath"], str(self.fork_path(child)))
        self.assertEqual(options["threadId"], child)
        self.assertIn('"title": "Fork of Source"', options["developerInstructions"])
        self.assertNotIn("Parent plan", options["developerInstructions"])
        self.assertEqual(len(self.sessions.read(parent)["thread"]["turns"]), 1)

    def test_snapshot_survives_parent_progress_restart_and_nested_unused_forks(self):
        parent = self.create()
        self.turn(parent)
        original = self.transcript(parent)
        content = original.read_bytes()
        child = self.sessions.fork_work_thread(parent)["thread_id"]
        self.turn(parent, "Later parent message")
        original.write_text('{"type":"user","message":{"content":"Later parent message"}}\n')
        self.sessions.close()
        self.sessions = self.make_sessions()
        grandchild = self.sessions.fork_work_thread(child)["thread_id"]
        self.assertEqual(self.fork_path(grandchild).read_bytes(), content)
        self.assertNotEqual(self.fork_path(grandchild), self.fork_path(child))
        self.assertEqual(len(self.sessions.read(grandchild)["thread"]["turns"]), 1)
        self.assertFalse(self.sessions.claude.connections)
        self.sessions.send(child, {"message": "Branch"})
        self.assertEqual(self.sessions.claude.connections[child].options["forkPath"], str(self.fork_path(child)))
        self.assertEqual(self.fork_path(child).read_bytes(), content)
        self.sessions.claude.connections[child].callback({"type": "system", "subtype": "init"})
        self.sessions.claude.connections[child].callback({"type": "result", "subtype": "success"})
        self.sessions.close()
        self.sessions = self.make_sessions()
        self.sessions.send(child, {"message": "Continue branch"})
        options = self.sessions.claude.connections[child].options
        self.assertTrue(options["resume"])
        self.assertIsNone(options["forkPath"])
        self.assertEqual(len(self.sessions.read(child)["thread"]["turns"]), 3)
        self.assertFalse(self.sessions.rpc.calls)

    def test_empty_fork_and_failed_native_start_do_not_fabricate_context(self):
        parent = self.create()
        child = self.sessions.fork_work_thread(parent)["thread_id"]
        self.assertIsNone(self.fork_path(child))
        self.assertEqual(self.sessions.read(child)["thread"]["turns"], [])
        self.turn(parent, native=False)
        with self.assertRaisesRegex(CoordinationError, "native transcript"):
            self.sessions.fork_work_thread(parent)

    def test_failed_first_connection_can_retry_the_same_snapshot(self):
        parent = self.create()
        self.turn(parent)
        self.transcript(parent)
        child = self.sessions.fork_work_thread(parent)["thread_id"]
        snapshot = self.fork_path(child)
        with patch.object(self.sessions.claude, "connection_factory", side_effect=CoordinationError("CLI unavailable")):
            with self.assertRaisesRegex(CoordinationError, "CLI unavailable"):
                self.sessions.send(child, {"message": "Branch"})
        self.assertTrue(snapshot.is_file())
        self.sessions.send(child, {"message": "Retry"})
        self.assertEqual(self.sessions.claude.connections[child].options["forkPath"], str(snapshot))
        self.assertEqual(len(self.sessions.list_work_threads()), 2)

    def test_unavailable_or_incomplete_native_transcript_leaves_parent_unchanged(self):
        parent = self.create()
        self.turn(parent)
        before = self.store.threads.get(parent, history=True)
        with self.assertRaisesRegex(CoordinationError, "Cannot fork Claude history"):
            self.sessions.fork_work_thread(parent)
        path = self.transcript(parent)
        for content in ('{"partial":', '{"type":"progress"}\n'):
            path.write_text(content)
            with self.assertRaisesRegex(CoordinationError, "Cannot fork Claude history"):
                self.sessions.fork_work_thread(parent)
        self.assertEqual(self.store.threads.get(parent, history=True), before)
        self.assertEqual([t["thread_id"] for t in self.sessions.list_work_threads()], [parent])
        self.assertFalse(list((self.root / "claude-forks").glob("*")))

    def test_busy_closed_and_terminal_sources_are_rejected(self):
        parent = self.create()
        self.sessions.send(parent, {"message": "Running"})
        self.assertFalse(self.sessions.work_thread(parent)["can_fork"])
        with self.assertRaises(BrowserBusyError):
            self.sessions.fork_work_thread(parent)
        self.sessions.interrupt(parent)
        self.sessions.requests["approval"] = {"params": {"threadId": parent}}
        with self.assertRaises(BrowserBusyError):
            self.sessions.fork_work_thread(parent)
        self.sessions.requests.clear()
        self.store.threads.update(parent, attention="archived")
        with self.assertRaises(CoordinationError):
            self.sessions.fork_work_thread(parent)
        self.store.register(session_id="terminal", client="claude", cwd=str(self.root))
        self.store.end_session("terminal")
        self.assertFalse(self.sessions.work_thread("terminal")["can_fork"])
        with self.assertRaises(CoordinationError):
            self.sessions.fork_work_thread("terminal")


if __name__ == "__main__":
    unittest.main()
