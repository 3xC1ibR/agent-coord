from __future__ import annotations

import json
import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PLUGIN_SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"
sys.path.insert(0, str(PLUGIN_SCRIPTS))

from agent_coord.cli import _parser, validate_claimed_bead
from agent_coord.cli import run as cli_run
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
        for flags in ([], ["--reply-required", "--no-reply-required"]):
            with self.subTest(flags=flags), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                parser.parse_args(["send", "--session", "two", *flags, "hello"])
            self.assertEqual(error.exception.code, 2)
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

    @patch("agent_coord.cli.delegate_work")
    @patch("agent_coord.cli.CoordinationStore")
    def test_delegate_accepts_a_prompt_and_scope_without_beads(self, store_class, delegate) -> None:
        arguments = _parser().parse_args([
            "delegate", "--from-session", "parent", "--scope", "src/**",
            "--dry-run", "Implement the feature.",
        ])

        cli_run(arguments)

        self.assertIsNone(delegate.call_args.kwargs["bead_id"])
        self.assertEqual(delegate.call_args.kwargs["scopes"], ["src/**"])
        self.assertTrue(delegate.call_args.kwargs["dry_run"])

    @patch("agent_coord.cli.validate_claimed_bead")
    @patch("agent_coord.cli.CoordinationStore")
    def test_handoff_accepts_a_scope_only_declaration(self, store_class, validate) -> None:
        store = store_class.return_value
        store.get_session.return_value = {
            "bead_id": None, "cwd": "/tmp/repo", "write_scope": ["src/**"],
        }
        arguments = _parser().parse_args([
            "handoff", "--from-session", "sender", "--to-session", "recipient",
            "--patch-label", "feature", "--validation-boundary", "unit tests passed",
            "--validation-responsibility", "run full suite", "--mode", "validation",
        ])

        cli_run(arguments)

        validate.assert_not_called()
        store.handoff_work.assert_called_once()
        self.assertIsNone(store.handoff_work.call_args.kwargs["target_bead_id"])

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
        self.assertIsNone(arguments.runtime)
        self.assertEqual(arguments.lease_mode, "write")

    def test_delegate_parses_managed_runtime_and_ui_options(self) -> None:
        delegate = _parser().parse_args(
            [
                "delegate",
                "--from-session",
                "parent",
                "--bead",
                "work-a",
                "--scope",
                "src/**",
                "--runtime",
                "managed-pty",
                "implement",
            ]
        )
        ui = _parser().parse_args(
            [
                "ui",
                "--parent-session",
                "parent",
                "--cwd",
                "/tmp/repo",
                "--port",
                "9000",
                "--no-browser",
            ]
        )

        self.assertEqual(delegate.runtime, "managed-pty")
        self.assertEqual(ui.parent_session, "parent")
        self.assertEqual(ui.ui_cwd, "/tmp/repo")
        self.assertEqual(ui.port, 9000)
        self.assertTrue(ui.no_browser)

    @patch("agent_coord.cli.serve_ui")
    @patch("agent_coord.cli.CoordinationStore")
    def test_ui_preserves_the_selected_directory(self, store_class, serve) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            directory = root / "nested"
            directory.mkdir()
            with patch("agent_coord.cli.find_repository_root", return_value=str(root)):
                cli_run(_parser().parse_args(["ui", "--cwd", str(directory), "--no-browser"]))
            self.assertEqual(serve.call_args.kwargs["cwd"], str(directory))

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
                database, "send", "--from-session", "one", "--session", "two", "--reply-required", "hi"
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
                "--no-reply-required",
                "FYI",
            )
            self._run(
                database, "send", "--from-session", "one", "--session", "two", "--reply-required", "act"
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

    def test_ambiguous_send_creates_no_message(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite3"
            for session in ("one", "two"):
                self._run(database, "register", "--session-id", session, "--client", "codex", "--cwd", directory)
            for classification in ("action_required", "informational", "closure"):
                result = self._run(database, "send", "--from-session", "one", "--session", "two",
                                   "--classification", classification, "Received, thanks!")
                self.assertEqual(result.returncode, 2)
                self.assertIn("--reply-required", result.stderr)
                self.assertIn("--no-reply-required", result.stderr)
            inbox = self._run(database, "inbox", "--session-id", "two", "--all", "--peek")
            self.assertEqual(json.loads(inbox.stdout), [])

    def test_reply_derives_target_and_reports_original_message(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite3"
            for session in ("one", "two"):
                self._run(database, "register", "--session-id", session, "--client", "codex", "--cwd", directory)
            sent = self._run(database, "send", "--from-session", "one", "--session", "two",
                             "--reply-required", "Please confirm receipt")
            original = json.loads(sent.stdout)
            reply = self._run(database, "reply", "--from-session", "two", "--message-id",
                              str(original["id"]), "Received, thanks!")
            self.assertEqual(reply.returncode, 0, reply.stderr)
            receipt = json.loads(reply.stdout)
            self.assertEqual(receipt["command"], "reply")
            self.assertEqual(receipt["recipient_session_id"], "one")
            self.assertEqual(receipt["thread_id"], original["thread_id"])
            self.assertEqual(receipt["in_reply_to"], original["id"])
            self.assertFalse(receipt["reply_required"])
            self.assertEqual(receipt["classification"], "action_required")
            inbox = self._run(database, "inbox", "--session-id", "one", "--peek")
            self.assertEqual(json.loads(inbox.stdout)[0]["body"], "Received, thanks!")
            invalid = self._run(database, "reply", "--from-session", "one", "--message-id",
                                str(receipt["id"]), "Thanks again")
            self.assertEqual(invalid.returncode, 2, invalid.stderr)
            self.assertIn("does not request a reply", invalid.stderr)
            override = self._run(database, "reply", "--from-session", "two", "--message-id",
                                 str(original["id"]), "--reply-required", "Another request")
            self.assertEqual(override.returncode, 2)


if __name__ == "__main__":
    unittest.main()
