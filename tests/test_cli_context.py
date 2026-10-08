from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))
from agent_coord.cli import main
from agent_coord.context import client_environment, cli_path, install_cli
from agent_coord.hook import handle
from agent_coord.store import CoordinationError, CoordinationStore


class CliContextTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database = self.root / "state.sqlite3"
        self.store = CoordinationStore(self.database)
        for identity in ("one", "two", "three"):
            self.store.register(session_id=identity, client="codex", cwd=str(self.root))
        environment = patch.dict(os.environ, {"PATH": os.environ["PATH"], "AGENT_COORD_DB": str(self.database),
                                              "CODEX_THREAD_ID": "one"}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def invoke(self, *args):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = main(args)
        return code, json.loads(output.getvalue() or errors.getvalue())

    def test_identity_precedence_and_explicit_cross_thread_override(self):
        self.assertEqual(self.invoke("status")[1]["session_id"], "one")
        with patch.dict(os.environ, {"AGENT_COORD_SESSION_ID": "two"}):
            self.assertEqual(self.invoke("status")[1]["session_id"], "two")
            self.assertEqual(self.invoke("status", "--session-id", "three")[1]["session_id"], "three")
            code, receipt = self.invoke("send", "--session", "one", "hello")
            self.assertEqual(code, 0)
            messages = self.store.inbox("one", mark_delivered=False)
            self.assertEqual(messages[0]["sender_session_id"], "two")
            self.assertEqual(receipt["id"], messages[0]["id"])
            self.assertNotIn("body", receipt)
            self.invoke("send", "--from-session", "three", "--session", "one", "override")
            self.assertEqual(self.store.inbox("one", mark_delivered=False)[-1]["sender_session_id"], "three")

    def test_missing_identity_fails_even_with_registered_sessions(self):
        with patch.dict(os.environ, {"AGENT_COORD_DB": str(self.database)}, clear=True):
            code, result = self.invoke("checkpoint", "--json", '{"phase":"investigation","summary":"x"}')
        self.assertEqual(code, 2)
        self.assertIn("--session-id", result["error"])
        self.assertIn("AGENT_COORD_SESSION_ID", result["error"])
        self.assertIsNone(self.store.threads.get("one")["checkpoint"])

    def test_database_override_and_xdg_default(self):
        alternate = self.root / "agent-coord/state.sqlite3"
        store = CoordinationStore(alternate)
        store.register(session_id="elsewhere", client="codex", cwd=str(self.root))
        self.assertEqual(self.invoke("--db", str(alternate), "status", "--session-id", "elsewhere")[0], 0)
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root), "CODEX_THREAD_ID": "elsewhere"}, clear=True):
            self.assertEqual(self.invoke("status")[1]["session_id"], "elsewhere")

    def test_short_workflow_and_compact_checkpoint_preserve_full_reads(self):
        self.assertEqual(self.invoke("inbox", "--unread"), (0, []))
        self.assertEqual(self.invoke("begin-work", "--scope", "src/**")[1]["write_scope"], ["src/**"])
        payload = json.dumps({"phase": "implementation", "summary": "Changed the CLI", "title": "Short CLI commands"})
        code, receipt = self.invoke("checkpoint", "--json", payload)
        self.assertEqual(code, 0)
        self.assertEqual(receipt["phase"], "implementation")
        self.assertEqual(receipt["title"], "Short CLI commands")
        self.assertNotIn("checkpoints", receipt)
        self.assertNotIn("original_request", receipt)
        self.assertLess(len(json.dumps(receipt)), 500)
        full = self.invoke("checkpoint", "--full", "--json", payload)[1]
        self.assertEqual(full["checkpoint"]["id"], receipt["checkpoint_id"])
        self.assertEqual(self.invoke("thread", "show")[1]["checkpoint"]["summary"], "Changed the CLI")
        self.assertEqual(self.invoke("end-work")[1]["write_scope"], [])

    def test_managed_launcher_runs_from_other_cwd_and_refreshes(self):
        # Spaces and shell metacharacters must remain literal in the launcher.
        directory = self.root / "plugin's scripts $(false)"
        directory.mkdir()
        executable = directory / "agent-coord"
        executable.write_text('#!/bin/sh\nprintf \'%s\\n\' "$@"\n')
        executable.chmod(0o755)
        destination = self.root / "bin"
        with patch("agent_coord.context.cli_path", return_value=executable):
            receipt = install_cli(str(destination))
        result = subprocess.run([receipt["path"], "literal $(false)", "two words"], cwd="/", text=True, capture_output=True, check=True)
        self.assertEqual(result.stdout.splitlines(), ["literal $(false)", "two words"])
        install_cli(str(destination))
        result = subprocess.run([receipt["path"], "status"], cwd="/", text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(result.stdout)["session_id"], "one")
        (destination / "agent-coord").write_text("unmanaged")
        with self.assertRaisesRegex(CoordinationError, "unmanaged"):
            install_cli(str(destination))

    def test_launch_context_clears_parent_identity_and_preserves_other_environment(self):
        parent = {"AGENT_COORD_SESSION_ID": "parent", "CODEX_THREAD_ID": "parent",
                  "CLAUDE_ENV_FILE": "/parent/exports", "PATH": "/bin", "KEEP": "yes"}
        codex = client_environment(parent, self.database, "codex")
        self.assertNotIn("AGENT_COORD_SESSION_ID", codex)
        self.assertNotIn("CODEX_THREAD_ID", codex)
        self.assertNotIn("CLAUDE_ENV_FILE", codex)
        self.assertEqual(codex["KEEP"], "yes")
        self.assertEqual(codex["PATH"].split(os.pathsep)[0], str(cli_path().parent))
        claude = client_environment(parent, self.database, "claude", session_id="child")
        self.assertEqual(claude["AGENT_COORD_SESSION_ID"], "child")
        self.assertEqual(parent["AGENT_COORD_SESSION_ID"], "parent")

    def test_claude_hook_exports_working_shell_context(self):
        exports = self.root / "exports"
        exports.write_text("export UNRELATED=preserved\n")
        with patch.dict(os.environ, {"AGENT_COORD_CLIENT": "claude", "CLAUDE_ENV_FILE": str(exports)}):
            result = handle({"hook_event_name": "SessionStart", "session_id": "claude-child", "cwd": str(self.root)}, self.store)
        self.assertIn("Run agent-coord checkpoint --json", result["hookSpecificOutput"]["additionalContext"])
        result = subprocess.run(["/bin/sh", "-c", '. "$1"; agent-coord status', "sh", str(exports)],
                                cwd="/", text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(result.stdout)["session_id"], "claude-child")
        self.assertTrue(exports.read_text().startswith("export UNRELATED=preserved\n"))

    def test_guidance_omits_redundancy_but_preserves_context_fallback(self):
        with patch("agent_coord.context.shutil.which", return_value="/bin/agent-coord"):
            text = self.store.threads.instructions("one")
        self.assertIn("Run agent-coord checkpoint --json", text)
        self.assertNotIn(" --db ", text)
        with patch.dict(os.environ, {}, clear=True):
            text = self.store.threads.instructions("one")
        self.assertIn(" --db ", text)
        self.assertIn(" --session-id one", text)
        # Provider launchers guarantee context before a new thread has an ID.
        self.assertIn("Run agent-coord checkpoint --json", self.store.threads.instructions(caller_context=True))


if __name__ == "__main__":
    unittest.main()
