"""Exercise SQLite contention through the real Claude stdout reader and browser."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from test_codex_app_server import BrowserSessions, CoordinationError, CoordinationStore, FakeCodex, PLUGIN_SCRIPTS
from agent_coord.claude_code import ClaudeConnection, ClaudeRPC


CLI = '''import json, os, sys
sys.path.insert(0, sys.argv[1])
from agent_coord.store import CoordinationStore
from agent_coord.browser_wake import WAKE_PROMPT
prompt = None
def emit(message):
    if message.get("type") == "result":
        message["user_message_uuid"] = prompt
    print(json.dumps(message), flush=True)
for line in sys.stdin:
    message = json.loads(line)
    if message.get("type") == "control_request":
        request = message["request"]
        if request["subtype"] == "disconnect":
            break
        for frame in request.get("frames", []):
            emit(frame)
        emit({"type": "control_response", "response": {
            "request_id": message["request_id"], "subtype": "success",
            "response": {"models": [{"value": "default"}]}}})
    elif message.get("type") == "user":
        prompt = message["uuid"]
        text = "\\n".join(p.get("text", "") for p in message["message"]["content"])
        if text == WAKE_PROMPT:
            store = CoordinationStore(os.environ["AGENT_COORD_DB"])
            messages = store.inbox(os.environ["AGENT_COORD_SESSION_ID"])
            emit({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "\\n".join(m["body"] for m in messages)}]}})
            emit({"type": "result", "subtype": "success"})
'''


class ClaudeContentionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = CoordinationStore(self.root / "state.sqlite3")

        def connection(store, callback, options):
            return ClaudeConnection(store, callback, options,
                command=[sys.executable, "-u", "-c", CLI, str(PLUGIN_SCRIPTS)])

        self.sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex,
            claude_factory=lambda store, callback: ClaudeRPC(store, callback, connection_factory=connection))
        self.addCleanup(self.sessions.close)
        self.thread = self.sessions.create({"client": "claude"})["session"]["thread_id"]
        self.turn = self.sessions.send(self.thread, {"message": "Start"})["turn"]["id"]
        self.connection = self.sessions.claude.connections[self.thread]
        self.store.register(session_id="sender", client="codex", cwd=str(self.root))

    @contextmanager
    def database_locked(self):
        connect = self.store._connect

        def short_timeout():
            connection = connect()
            connection.execute("PRAGMA busy_timeout = 20")
            return connection

        blocker = connect()
        try:
            blocker.execute("BEGIN IMMEDIATE")
            with patch.object(self.store, "_connect", short_timeout):
                yield
        finally:
            blocker.rollback()
            blocker.close()

    def test_locked_init_preserves_completion_and_next_inbox_delivery(self):
        pending = self.store.send_message(sender_session_id="sender", recipient_session_id=self.thread,
            body="Next scheduled task", classification="action_required", reply_required=False)
        self.sessions.inbox_wake.process_once()
        with self.database_locked():
            self.connection.control("emit", timeout=3, frames=[
                {"type": "system", "subtype": "init", "session_id": self.thread},
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "Finished once"}]}},
                {"type": "result", "subtype": "success"},
            ])
            self.assertTrue(self.connection.readers[0].is_alive())
            self.assertIsNone(self.connection.process.poll())
            self.assertNotIn(self.thread, self.sessions.active)
            self.assertFalse(self.sessions.persistence.flush(timeout=.02))
        self.assertTrue(self.sessions.claude.persistence.flush(timeout=3))
        self.assertTrue(self.sessions.persistence.flush(timeout=3))
        with self.store._connection() as db:
            history = json.loads(db.execute("SELECT history_json FROM browser_history WHERE thread_id=?", (self.thread,)).fetchone()[0])
            self.assertEqual(db.execute("SELECT started FROM claude_sessions WHERE thread_id=?", (self.thread,)).fetchone()[0], 1)
            self.assertEqual([r[0] for r in db.execute("SELECT status FROM turn_completions WHERE thread_id=?", (self.thread,))], ["completed"])
            self.assertFalse(db.execute("SELECT 1 FROM browser_inbox_wake WHERE thread_id=? AND paused=1", (self.thread,)).fetchone())
        self.assertEqual(history["turns"][0]["status"], "completed")
        self.assertEqual([i["text"] for i in history["turns"][0]["items"] if i["type"] == "agentMessage"], ["Finished once"])
        self.sessions.inbox_wake.process_once()
        # The provider consumes the inbox before acknowledging this control.
        self.connection.control("barrier", timeout=3)
        self.assertTrue(self.sessions.persistence.flush(timeout=3))
        self.sessions.inbox_wake.process_once()
        with self.store._connection() as db:
            self.assertIsNotNone(db.execute("SELECT delivered_at FROM messages WHERE id=?", (pending["id"],)).fetchone()[0])
            self.assertEqual([r[0] for r in db.execute("SELECT outcome FROM message_wake_attempts WHERE message_id=?", (pending["id"],))], ["sent"])
            self.assertEqual([r[0] for r in db.execute("SELECT status FROM turn_completions WHERE thread_id=? ORDER BY id", (self.thread,))], ["completed", "completed"])
        turns = self.sessions.read(self.thread)["thread"]["turns"]
        self.assertEqual(len(turns), 2)
        self.assertEqual([i["text"] for i in turns[-1]["items"] if i["type"] == "agentMessage"], ["Next scheduled task"])

    def test_real_failure_still_pauses_inbox(self):
        self.connection.control("emit", frames=[{"type": "result", "subtype": "error_during_execution",
            "is_error": True, "errors": ["Provider failed"]}])
        self.assertTrue(self.sessions.persistence.flush(key=self.thread))
        with self.store._connection() as db:
            self.assertEqual(db.execute("SELECT status FROM turn_completions WHERE turn_key=?", (self.turn,)).fetchone()[0], "failed")
            self.assertEqual(tuple(db.execute("SELECT paused,pause_kind FROM browser_inbox_wake WHERE thread_id=?", (self.thread,)).fetchone()), (1, "failure"))

    def test_replacement_resumes_before_started_write_recovers(self):
        rpc = self.sessions.claude
        with self.database_locked():
            self.connection.control("emit", timeout=3, frames=[
                {"type": "system", "subtype": "init"}, {"type": "result", "subtype": "success"}])
            with self.store._connection() as db:
                self.assertEqual(db.execute("SELECT started FROM claude_sessions WHERE thread_id=?", (self.thread,)).fetchone()[0], 0)
            replacement = rpc._connection(self.thread, {**rpc.options[self.thread], "effort": "low"})
            self.assertIn("--resume", replacement.process.args)
            self.assertNotIn("--session-id", replacement.process.args)
        self.assertTrue(rpc.persistence.flush(timeout=3))
        self.assertTrue(self.sessions.persistence.flush(timeout=3))

    def test_real_eof_still_fails_the_turn(self):
        with self.assertRaisesRegex(CoordinationError, "disconnected"):
            self.connection.control("disconnect", timeout=3)
        self.connection.readers[0].join(timeout=3)
        self.assertFalse(self.connection.readers[0].is_alive())
        self.assertTrue(self.sessions.persistence.flush(key=self.thread))
        with self.store._connection() as db:
            self.assertEqual(db.execute("SELECT status FROM turn_completions WHERE turn_key=?", (self.turn,)).fetchone()[0], "failed")
            self.assertEqual(tuple(db.execute("SELECT paused,pause_kind FROM browser_inbox_wake WHERE thread_id=?", (self.thread,)).fetchone()), (1, "failure"))

    def test_deliberate_stop_still_pauses_inbox(self):
        self.sessions.interrupt(self.thread)
        self.assertTrue(self.sessions.persistence.flush(key=self.thread))
        with self.store._connection() as db:
            self.assertEqual(db.execute("SELECT status FROM turn_completions WHERE turn_key=?", (self.turn,)).fetchone()[0], "interrupted")
            self.assertEqual(tuple(db.execute("SELECT paused,pause_kind FROM browser_inbox_wake WHERE thread_id=?", (self.thread,)).fetchone()), (1, "stop"))
