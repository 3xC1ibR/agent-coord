from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins/agent-coord"
sys.path.insert(0, str(PLUGIN / "scripts"))

from agent_coord.checkpoint_mcp import dispatch, update_checkpoint
from agent_coord.store import CoordinationError, CoordinationStore


class CheckpointMCPTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = CoordinationStore(self.root / "state.sqlite3")
        for name in ("one", "two"):
            self.store.register(session_id=name, client="codex", cwd=str(self.root))

    def test_tool_uses_only_explicit_or_inherited_identity(self):
        with patch.dict(os.environ, {"AGENT_COORD_SESSION_ID": "one", "CODEX_THREAD_ID": "two"}, clear=True):
            receipt = update_checkpoint({"phase": "investigation", "summary": "Investigated."}, store=self.store)
            self.assertEqual(set(receipt), {"status", "checkpoint_id"})
            self.assertEqual(self.store.threads.get("one")["checkpoint"]["id"], receipt["checkpoint_id"])
            update_checkpoint({"session_id": "two", "phase": "discussion", "summary": "Other."}, store=self.store)
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(CoordinationError, "No caller session"):
                update_checkpoint({"phase": "validation"}, store=self.store)
        self.assertEqual(self.store.threads.get("two")["checkpoint"]["summary"], "Other.")

    def test_tool_accepts_minted_app_thread_links_and_rejects_unvalidated_routes(self):
        from agent_coord.navigation import NavigationStore
        child_id = "7d724292-3c97-422a-8e38-ea0ef3c1a687"
        self.store.register(session_id=child_id, client="kimi", cwd=str(self.root), name="Hide empty Agents tab")
        with self.store._connection() as db:
            db.execute("CREATE TABLE browser_sessions (thread_id TEXT PRIMARY KEY)")
            db.execute("INSERT INTO browser_sessions VALUES ('one')")
        self.store.threads.start_turn("one", turn_id="turn-1")
        target = NavigationStore(self.store).link(thread=child_id)["url"]
        receipt = update_checkpoint({
            "session_id": "one",
            "phase": "orchestrating",
            "summary": "Dispatched specialist.",
            "next_step": None,
            "links": [{"kind": "other", "label": "Hide empty Agents tab", "target": target}],
            "context_lifecycle": {
                "recommendation": "reuse",
                "reason": "Dispatcher tracks active specialists.",
                "reuse_for": ["Routing follow-ups"],
                "fresh_for": ["Unrelated work"],
                "handover": {"confirmed_facts": [], "constraints": [], "tried_and_failed": [], "open_questions": []},
            },
        }, store=self.store)
        self.assertEqual(receipt["status"], "ok")
        saved = self.store.threads.get("one")
        self.assertEqual(saved["links"][0]["target"], target)
        self.assertEqual(saved["links"][0]["label"], "Hide empty Agents tab")
        with self.assertRaises(CoordinationError):
            update_checkpoint({
                "session_id": "one",
                "links": [{"kind": "other", "label": "Missing", "target": "agentcoord://thread/missing"}],
            }, store=self.store)

    def test_stdio_handshake_discovery_updates_and_errors(self):
        config = json.loads((PLUGIN / ".mcp.json").read_text())["mcpServers"]["agent-coord"]
        command = [config["command"], *[arg.replace("${CLAUDE_PLUGIN_ROOT}", str(PLUGIN)) for arg in config["args"]]]
        messages = [
            {"id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"method": "notifications/initialized"},
            {"id": 2, "method": "tools/list"},
            {"id": 3, "method": "tools/call", "params": {"name": "update_checkpoint", "arguments": {
                "phase": "implementation", "summary": "Changed code.", "next_step": {"actor": "agent", "action": "Run tests."}}}},
            {"id": 4, "method": "tools/call", "params": {"name": "update_checkpoint", "arguments": {"phase": "validation"}}},
            {"id": 5, "method": "tools/call", "params": {"name": "update_checkpoint", "arguments": {"summary": "Tests passed.", "next_step": None}}},
            {"id": 6, "method": "tools/call", "params": {"name": "update_checkpoint", "arguments": {"next_step": {"actor": "user"}}}},
            {"id": 7, "method": "tools/call", "params": {"name": "unknown"}},
            {"id": 8, "method": "ping"},
        ]
        environment = {**os.environ, "AGENT_COORD_DB": str(self.store.database_path), "AGENT_COORD_SESSION_ID": "one"}
        result = subprocess.run(command, input="\n".join(json.dumps({"jsonrpc": "2.0", **message}) for message in messages) + "\n",
                                cwd=self.root, env=environment, text=True, capture_output=True, timeout=20, check=True)
        self.assertEqual(result.stderr, "")
        replies = {reply["id"]: reply for reply in map(json.loads, result.stdout.splitlines())}
        self.assertEqual(replies[1]["result"]["protocolVersion"], "2025-06-18")
        tool = replies[2]["result"]["tools"][0]
        self.assertEqual(tool["name"], "update_checkpoint")
        self.assertNotIn("required", tool["inputSchema"])
        for number in (3, 4, 5):
            receipt = json.loads(replies[number]["result"]["content"][0]["text"])
            self.assertEqual(receipt, {"status": "ok", "checkpoint_id": receipt["checkpoint_id"]})
        saved = self.store.threads.get("one", history=True)
        self.assertEqual(saved["checkpoint"]["phase"], "validation")
        self.assertEqual(saved["checkpoint"]["summary"], "Tests passed.")
        self.assertEqual(saved["checkpoint"]["next_actor"], "nobody")
        self.assertEqual(len(saved["checkpoints"]), 3)
        self.assertEqual(saved["checkpoints"][1]["next_action"], "Run tests.")
        self.assertTrue(replies[6]["result"]["isError"])
        self.assertEqual(replies[7]["error"]["code"], -32602)
        self.assertEqual(replies[8]["result"], {})
        self.assertIsNone(self.store.threads.get("two")["checkpoint"])

    def test_protocol_errors_and_notifications(self):
        self.assertEqual(dispatch([])["error"]["code"], -32600)
        self.assertIsNone(dispatch({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self.assertEqual(dispatch({"jsonrpc": "2.0", "id": 1, "method": "unknown"})["error"]["code"], -32601)
        self.assertEqual(dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": []})["error"]["code"], -32602)

    def test_both_client_configs_launch_the_shared_server(self):
        codex = json.loads((PLUGIN / ".codex-plugin/plugin.json").read_text())["mcpServers"]["agent-coord"]
        claude = json.loads((PLUGIN / ".mcp.json").read_text())["mcpServers"]["agent-coord"]
        for config, variable in ((codex, "${PLUGIN_ROOT}"), (claude, "${CLAUDE_PLUGIN_ROOT}")):
            with self.subTest(variable=variable):
                command = [config["command"], *[arg.replace(variable, str(PLUGIN)) for arg in config["args"]]]
                cwd = PLUGIN / config["cwd"] if "cwd" in config else self.root
                context = {"AGENT_COORD_DB": str(self.store.database_path), "AGENT_COORD_SESSION_ID": "one"}
                environment = {**os.environ, **context}
                if "env_vars" in config:
                    # Codex filters custom variables unless explicitly forwarded.
                    environment = {key: value for key, value in environment.items()
                                   if key in {"HOME", "PATH", *config["env_vars"]}}
                messages = [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                                "name": "update_checkpoint", "arguments": {"phase": "discussion", "summary": "Context forwarded."}}}]
                result = subprocess.run(command, input="\n".join(map(json.dumps, messages)) + "\n",
                                        cwd=cwd, env=environment, capture_output=True, text=True, timeout=20, check=True)
                discovered, saved = map(json.loads, result.stdout.splitlines())
                self.assertEqual(discovered["result"]["tools"][0]["name"], "update_checkpoint")
                self.assertNotIn("isError", saved["result"])
                self.assertEqual(self.store.threads.get("one")["checkpoint"]["summary"], "Context forwarded.")


if __name__ == "__main__":
    unittest.main()
