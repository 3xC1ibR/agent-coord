from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))

from agent_coord.codex_app_server import BrowserSessions
from agent_coord.hook import handle
from agent_coord.store import CoordinationError, CoordinationStore
from agent_coord.zellij_wake import WAKE_PROMPT
from test_codex_app_server import FakeCodex


class LaterResumeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = CoordinationStore(self.root / "coord.sqlite3")
        self.sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(self.sessions.close)
        self.thread_id = self.sessions.create({"name": "Parked work"})["session"]["thread_id"]

    def park(self):
        self.store.threads.update(self.thread_id, attention="later")

    def attention(self):
        return self.store.threads.get(self.thread_id)["attention"]

    def test_browser_new_message_and_steering_resume_later(self):
        self.park()
        self.sessions.send(self.thread_id, {"message": "Start."})
        self.assertEqual(self.attention(), "now")
        self.park()
        self.sessions.send(self.thread_id, {"message": "Use the second option."})
        self.assertEqual(self.attention(), "now")

    def test_rejected_messages_preserve_later(self):
        self.park()
        self.sessions.rpc.fail_turn = True
        with self.assertRaises(CoordinationError):
            self.sessions.send(self.thread_id, {"message": "Start."})
        self.assertEqual(self.attention(), "later")
        self.sessions.rpc.fail_turn = False
        self.sessions.send(self.thread_id, {"message": "Start."})
        self.park()
        self.sessions.rpc.fail_steer = True
        with self.assertRaises(CoordinationError):
            self.sessions.send(self.thread_id, {"message": "Change direction."})
        self.assertEqual(self.attention(), "later")

    def test_read_command_and_background_events_preserve_later(self):
        self.sessions.send(self.thread_id, {"message": "Start."})
        self.park()
        self.sessions.read(self.thread_id)
        self.sessions.send(self.thread_id, {"message": "/help"})
        turn = self.sessions.rpc.threads[self.thread_id]["turns"][-1]
        self.sessions._event({"method": "turn/started", "params": {"threadId": self.thread_id, "turn": turn}})
        handle({"hook_event_name": "UserPromptSubmit", "session_id": self.thread_id,
                "cwd": str(self.root), "prompt": "Start."}, self.store)
        self.sessions._event({"method": "turn/completed", "params": {
            "threadId": self.thread_id, "turn": {**turn, "status": "completed"},
        }})
        self.assertEqual(self.attention(), "later")

    def test_closed_thread_is_not_reopened(self):
        self.store.threads.update(self.thread_id, attention="archived")
        with self.assertRaises(CoordinationError):
            self.sessions.send(self.thread_id, {"message": "Start."})
        self.store.threads.user_message(self.thread_id)
        self.assertEqual(self.attention(), "archived")

    def test_terminal_user_prompt_resumes_later_for_both_clients(self):
        for client in ("codex", "claude"):
            with self.subTest(client=client), patch.dict("os.environ", {
                "AGENT_COORD_CLIENT": client, "AGENT_COORD_ZELLIJ_WAKE": "0",
                "AGENT_COORD_DELEGATION_ID": "",
            }):
                session_id = "terminal-" + client
                self.store.register(session_id=session_id, client=client, cwd=str(self.root))
                self.store.threads.update(session_id, attention="later")
                result = handle({"hook_event_name": "UserPromptSubmit", "session_id": session_id,
                                 "cwd": str(self.root), "prompt": "Continue."}, self.store)
                self.assertEqual(self.store.threads.get(session_id)["attention"], "now")
                self.assertIn('"attention": "now"', result["hookSpecificOutput"]["additionalContext"])

    def test_automatic_terminal_wake_preserves_later(self):
        self.store.register(session_id="terminal", client="codex", cwd=str(self.root))
        self.store.threads.update("terminal", attention="later")
        handle({"hook_event_name": "UserPromptSubmit", "session_id": "terminal",
                "cwd": str(self.root), "prompt": WAKE_PROMPT}, self.store)
        self.assertEqual(self.store.threads.get("terminal")["attention"], "later")

    def test_terminal_prompt_without_text_resumes_later(self):
        self.store.register(session_id="terminal", client="codex", cwd=str(self.root))
        self.store.threads.update("terminal", attention="later")
        handle({"hook_event_name": "UserPromptSubmit", "session_id": "terminal",
                "cwd": str(self.root), "prompt": ""}, self.store)
        self.assertEqual(self.store.threads.get("terminal")["attention"], "now")


if __name__ == "__main__":
    unittest.main()
