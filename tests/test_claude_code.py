from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from test_browser_providers import CoordinationStore, CoordinationError
from agent_coord.claude_code import ClaudeConnection


CLI = '''import json, os, sys
for line in sys.stdin:
    message = json.loads(line)
    if message.get("type") == "control_request":
        if message["request"]["subtype"] == "disconnect":
            break
        response = {"args": sys.argv[1:], "parent": "CODEX_THREAD_ID" in os.environ,
                    "client": os.environ.get("AGENT_COORD_CLIENT")}
        print(json.dumps({"type": "control_response", "response": {
            "subtype": "success", "request_id": message["request_id"], "response": response}}), flush=True)
    elif message.get("type") == "user":
        print(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Hello"}]}}), flush=True)
        print(json.dumps({"type": "result", "subtype": "success"}), flush=True)
'''


class ClaudeTransportTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = CoordinationStore(self.root / "state.sqlite3")
        self.fixture = self.root / "cli.py"
        self.fixture.write_text(CLI)

    def connection(self, callback=lambda message: None, **options):
        connection = ClaudeConnection(self.store, callback,
            {"threadId": "test-session", "cwd": str(self.root), **options},
            command=[sys.executable, "-u", str(self.fixture)])
        self.addCleanup(connection.close)
        return connection

    def test_control_multiplexing_streaming_and_default_permissions(self):
        messages, complete = [], threading.Event()
        def receive(message):
            messages.append(message)
            if message["type"] == "result":
                complete.set()
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "parent", "CLAUDECODE": "1"}):
            connection = self.connection(receive)
        result = connection.control("initialize")
        self.assertIn("stdio", result["args"])
        self.assertIn("--session-id", result["args"])
        self.assertEqual(result["args"][result["args"].index("--permission-mode") + 1], "default")
        self.assertNotIn("--allow-dangerously-skip-permissions", result["args"])
        self.assertFalse(result["parent"])
        self.assertEqual(result["client"], "claude")
        connection.write({"type": "user", "message": {"role": "user", "content": "Hello"}})
        self.assertTrue(complete.wait(3))
        self.assertEqual([m["type"] for m in messages], ["assistant", "result"])

    def test_resume_yolo_and_settings_are_explicit(self):
        connection = self.connection(resume=True, yolo=True, model="sonnet", effort="high", developerInstructions="Checkpoints")
        args = connection.control("initialize")["args"]
        self.assertIn("--resume", args)
        self.assertNotIn("--session-id", args)
        self.assertIn("bypassPermissions", args)
        self.assertIn("--allow-dangerously-skip-permissions", args)
        self.assertEqual(args[args.index("--model") + 1], "sonnet")
        self.assertEqual(args[args.index("--effort") + 1], "high")

    def test_eof_releases_pending_control_request_and_notifies(self):
        event = threading.Event()
        connection = self.connection(lambda message: event.set() if message["type"] == "disconnected" else None)
        with self.assertRaisesRegex(CoordinationError, "disconnected"):
            connection.control("disconnect", timeout=3)
        self.assertTrue(event.wait(3))
        self.assertFalse(connection.pending)

    def test_missing_cli_is_actionable(self):
        with self.assertRaisesRegex(CoordinationError, "Install Claude Code"):
            ClaudeConnection(self.store, lambda message: None,
                {"threadId": "test", "cwd": str(self.root)}, command=[str(self.root / "missing")])

    def test_model_probe_cannot_create_a_phantom_session_through_hooks(self):
        args = self.connection(probe=True).control("initialize")["args"]
        self.assertIn("--no-session-persistence", args)
        self.assertEqual(json.loads(args[args.index("--settings") + 1]), {"disableAllHooks": True})
