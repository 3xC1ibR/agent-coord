from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PLUGIN_SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"
sys.path.insert(0, str(PLUGIN_SCRIPTS))

from agent_coord.cli import _parser, validate_claimed_bead
from agent_coord.store import CoordinationError


class CliTests(unittest.TestCase):
    def test_delegate_parses_model_and_reasoning_effort(self) -> None:
        arguments = _parser().parse_args(
            [
                "delegate",
                "--from-session",
                "parent",
                "--bead",
                "work-a",
                "--scope",
                "src/**",
                "--model",
                "gpt-5.6-terra",
                "--reasoning-effort",
                "high",
                "Implement the feature.",
            ]
        )

        self.assertEqual(arguments.model, "gpt-5.6-terra")
        self.assertEqual(arguments.reasoning_effort, "high")

    @patch("agent_coord.cli.shutil.which", return_value="/usr/local/bin/bd")
    @patch("agent_coord.cli.subprocess.run")
    def test_claimed_in_progress_bead_is_accepted(self, run, _which) -> None:
        issue = {"id": "repo-abc", "status": "in_progress", "assignee": "r"}
        run.return_value = subprocess.CompletedProcess(
            ["bd"], 0, stdout=json.dumps([issue]), stderr=""
        )
        self.assertEqual(validate_claimed_bead("repo-abc", "/tmp"), issue)

    @patch("agent_coord.cli.shutil.which", return_value="/usr/local/bin/bd")
    @patch("agent_coord.cli.subprocess.run")
    def test_unclaimed_bead_is_rejected(self, run, _which) -> None:
        issue = {"id": "repo-abc", "status": "open", "assignee": None}
        run.return_value = subprocess.CompletedProcess(
            ["bd"], 0, stdout=json.dumps([issue]), stderr=""
        )
        with self.assertRaises(CoordinationError):
            validate_claimed_bead("repo-abc", "/tmp")

    def test_wrapper_runs_without_installing_package(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite3"
            wrapper = PLUGIN_SCRIPTS / "agent-coord"
            result = subprocess.run(
                [
                    str(wrapper),
                    "--db",
                    str(database),
                    "register",
                    "--session-id",
                    "integration",
                    "--client",
                    "codex",
                    "--cwd",
                    directory,
                ],
                check=False,
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["session_id"], "integration")

    @staticmethod
    def _run(database: Path, *args: str) -> subprocess.CompletedProcess[str]:
        wrapper = PLUGIN_SCRIPTS / "agent-coord"
        return subprocess.run(
            [str(wrapper), "--db", str(database), *args],
            check=False,
            capture_output=True,
            text=True,
        )

    def test_inbox_wait_delivers_existing_message_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite3"
            self._run(
                database,
                "register",
                "--session-id",
                "one",
                "--client",
                "codex",
                "--cwd",
                directory,
            )
            self._run(
                database,
                "register",
                "--session-id",
                "two",
                "--client",
                "claude",
                "--cwd",
                directory,
            )
            self._run(
                database, "send", "--from-session", "one", "--session", "two", "hi"
            )
            result = self._run(
                database, "inbox", "--session-id", "two", "--wait", "--timeout", "5"
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        messages = json.loads(result.stdout)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["body"], "hi")

    def test_inbox_wait_times_out_with_distinct_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite3"
            self._run(
                database,
                "register",
                "--session-id",
                "two",
                "--client",
                "claude",
                "--cwd",
                directory,
            )
            result = self._run(
                database, "inbox", "--session-id", "two", "--wait", "--timeout", "0.2"
            )
        self.assertEqual(result.returncode, 5, result.stderr)
        payload = json.loads(result.stderr)
        self.assertEqual(payload["session_id"], "two")
        self.assertIn("timeout_seconds", payload)

    def test_inbox_timeout_without_wait_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite3"
            self._run(
                database,
                "register",
                "--session-id",
                "two",
                "--client",
                "claude",
                "--cwd",
                directory,
            )
            result = self._run(
                database, "inbox", "--session-id", "two", "--timeout", "5"
            )
        self.assertEqual(result.returncode, 2, result.stderr)

    def test_inbox_wait_all_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite3"
            self._run(
                database,
                "register",
                "--session-id",
                "two",
                "--client",
                "claude",
                "--cwd",
                directory,
            )
            result = self._run(
                database, "inbox", "--session-id", "two", "--wait", "--all"
            )
        self.assertEqual(result.returncode, 2, result.stderr)


if __name__ == "__main__":
    unittest.main()
