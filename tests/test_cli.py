from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PLUGIN_SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"
sys.path.insert(0, str(PLUGIN_SCRIPTS))

from agent_coord.cli import _parser, run as cli_run, validate_claimed_bead
from agent_coord.store import CoordinationError


class CliTests(unittest.TestCase):
    @patch("agent_coord.cli.validate_claimed_bead")
    @patch("agent_coord.cli.CoordinationStore")
    def test_begin_work_accepts_scope_without_bead(self, store_class, validate) -> None:
        store = MagicMock()
        store_class.return_value = store
        store.get_session.return_value = {"cwd": "/tmp/repo"}
        store.begin_work.return_value = {"write_scope": ["src/**"], "bead_id": None}
        arguments = _parser().parse_args(
            [
                "begin-work",
                "--session-id",
                "one",
                "--scope",
                "src/**",
            ]
        )

        result = cli_run(arguments)

        validate.assert_not_called()
        store.begin_work.assert_called_once_with(
            session_id="one",
            scopes=["src/**"],
            bead_id=None,
            activity="implementing",
            lease_mode="write",
        )
        self.assertIsNone(result["bead_id"])

    def test_send_reply_required_boolean_option_parses(self) -> None:
        parser = _parser()
        self.assertIsNone(
            parser.parse_args(
                ["send", "--from-session", "one", "--session", "two", "hello"]
            ).reply_required
        )
        self.assertFalse(
            parser.parse_args(
                [
                    "send", "--from-session", "one", "--session", "two",
                    "--no-reply-required", "hello",
                ]
            ).reply_required
        )
        self.assertTrue(
            parser.parse_args(
                [
                    "send", "--from-session", "one", "--session", "two",
                    "--reply-required", "hello",
                ]
            ).reply_required
        )

    def test_handoff_parses_audited_whole_declaration_transfer(self) -> None:
        arguments = _parser().parse_args(
            [
                "handoff",
                "--from-session",
                "sender",
                "--to-session",
                "recipient",
                "--target-bead",
                "work-b",
                "--scope",
                "src/**",
                "--patch-label",
                "adapter-v2",
                "--validation-boundary",
                "focused tests passed",
                "--validation-responsibility",
                "recipient runs full suite",
                "--mode",
                "validation",
            ]
        )

        self.assertEqual(arguments.recipient_session_id, "recipient")
        self.assertEqual(arguments.target_bead_id, "work-b")
        self.assertEqual(arguments.mode, "validation")

    @patch("agent_coord.cli.validate_claimed_bead")
    @patch("agent_coord.cli.CoordinationStore")
    def test_handoff_revalidates_changed_target_bead_immediately(
        self, store_class, validate
    ) -> None:
        store = MagicMock()
        store_class.return_value = store
        store.get_session.return_value = {
            "bead_id": "work-a",
            "cwd": "/tmp/repo",
        }
        store.handoff_work.return_value = {"handoff_id": "handoff-a"}
        arguments = _parser().parse_args(
            [
                "handoff",
                "--from-session",
                "sender",
                "--session",
                "recipient",
                "--bead",
                "work-b",
                "--patch-label",
                "adapter-v2",
                "--validation-boundary",
                "focused tests passed",
                "--validation-responsibility",
                "recipient",
                "--mode",
                "write",
            ]
        )

        result = cli_run(arguments)

        validate.assert_called_once_with("work-b", "/tmp/repo")
        store.handoff_work.assert_called_once()
        self.assertEqual(result["handoff_id"], "handoff-a")

    def test_delegate_parses_client_model_and_effort(self) -> None:
        arguments = _parser().parse_args(
            [
                "delegate",
                "--from-session",
                "parent",
                "--bead",
                "work-a",
                "--scope",
                "src/**",
                "--client",
                "claude",
                "--model",
                "opus",
                "--effort",
                "high",
                "--lease-mode",
                "validation",
                "Implement the feature.",
            ]
        )

        self.assertEqual(arguments.client, "claude")
        self.assertEqual(arguments.model, "opus")
        self.assertEqual(arguments.reasoning_effort, "high")
        self.assertEqual(arguments.lease_mode, "validation")

    def test_delegate_defaults_to_codex_and_keeps_reasoning_effort_alias(self) -> None:
        arguments = _parser().parse_args(
            [
                "delegate",
                "--from-session",
                "parent",
                "--bead",
                "work-a",
                "--scope",
                "src/**",
                "--reasoning-effort",
                "medium",
                "Implement the feature.",
            ]
        )

        self.assertEqual(arguments.client, "codex")
        self.assertEqual(arguments.reasoning_effort, "medium")
        self.assertEqual(arguments.lease_mode, "write")

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

    def test_explicit_unread_inbox_and_ack_all_unread(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite3"
            for session_id, client in (("one", "codex"), ("two", "claude")):
                self._run(
                    database,
                    "register",
                    "--session-id",
                    session_id,
                    "--client",
                    client,
                    "--cwd",
                    directory,
                )
            self._run(
                database,
                "send",
                "--from-session",
                "one",
                "--session",
                "two",
                "--classification",
                "informational",
                "FYI",
            )
            self._run(
                database, "send", "--from-session", "one", "--session", "two", "act"
            )
            self._run(database, "inbox", "--session-id", "two")

            unread = self._run(
                database, "inbox", "--session-id", "two", "--unread", "--peek"
            )
            acknowledged = self._run(
                database, "ack", "--session-id", "two", "--all-unread"
            )
            empty = self._run(
                database, "inbox", "--session-id", "two", "--unread", "--peek"
            )

        self.assertEqual(unread.returncode, 0, unread.stderr)
        self.assertEqual(len(json.loads(unread.stdout)), 2)
        self.assertEqual(json.loads(acknowledged.stdout)["acknowledged"], 2)
        self.assertEqual(json.loads(empty.stdout), [])

    def test_send_json_exposes_reply_required_and_accepts_opt_out_flag(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite3"
            for session_id, client in (("one", "codex"), ("two", "claude")):
                self._run(
                    database,
                    "register",
                    "--session-id",
                    session_id,
                    "--client",
                    client,
                    "--cwd",
                    directory,
                )
            result = self._run(
                database,
                "send",
                "--from-session",
                "one",
                "--session",
                "two",
                "--no-reply-required",
                "handoff received",
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(json.loads(result.stdout)["reply_required"])


if __name__ == "__main__":
    unittest.main()
