from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN_SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"
sys.path.insert(0, str(PLUGIN_SCRIPTS))

from agent_coord.delegate import delegate_work, validate_ready_bead
from agent_coord.store import CoordinationError, CoordinationStore


class FakeRun:
    def __init__(
        self,
        root: Path,
        *,
        launch_returncode: int = 0,
        claude_logged_in: bool = True,
    ) -> None:
        self.root = root
        self.launch_returncode = launch_returncode
        self.claude_logged_in = claude_logged_in
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(self, command, **options):
        command = list(command)
        self.calls.append((command, options))
        if command[0] == "/mock/git":
            return subprocess.CompletedProcess(
                command, 0, stdout=f"{self.root}\n", stderr=""
            )
        if command[:3] == ["/mock/bd", "show", "work-a"]:
            issue = {
                "id": "work-a",
                "status": "open",
                "assignee": None,
                "title": "Delegated work",
            }
            return subprocess.CompletedProcess(
                command, 0, stdout=json.dumps([issue]), stderr=""
            )
        if command[:3] == ["/mock/bd", "ready", "--json"]:
            return subprocess.CompletedProcess(
                command, 0, stdout=json.dumps([{"id": "work-a"}]), stderr=""
            )
        if command[:3] == ["/mock/codex", "login", "status"]:
            return subprocess.CompletedProcess(
                command, 0, stdout="Logged in\n", stderr=""
            )
        if command[:3] == ["/mock/claude", "auth", "status"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps({"loggedIn": self.claude_logged_in}) + "\n",
                stderr="",
            )
        if command[0] == "/mock/zellij":
            return subprocess.CompletedProcess(
                command,
                self.launch_returncode,
                stdout="terminal_42\n" if self.launch_returncode == 0 else "",
                stderr="pane failed" if self.launch_returncode else "",
            )
        raise AssertionError(f"Unexpected command: {command}")


class DelegateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = CoordinationStore(self.root / "state.sqlite3")
        self.store.register(
            session_id="parent",
            client="codex",
            cwd=str(self.root),
            name="parent",
        )

    @staticmethod
    def which(name: str) -> str:
        return f"/mock/{name}"

    def delegate(self, runner: FakeRun, **overrides):
        arguments = {
            "parent_session_id": "parent",
            "cwd": str(self.root),
            "bead_id": "work-a",
            "scopes": ["src/**"],
            "instructions": "Implement the delegated feature.",
            "zellij_session": "friendly-lemur",
            "pane_name": "delegated-work",
            "floating": True,
            "which": self.which,
            "run": runner,
        }
        arguments.update(overrides)
        return delegate_work(self.store, **arguments)

    def test_launch_constructs_reviewed_floating_codex_command(self) -> None:
        runner = FakeRun(self.root)

        result = self.delegate(runner)

        delegation = result["delegation"]
        self.assertEqual(delegation["status"], "launched")
        self.assertEqual(delegation["pane_id"], "terminal_42")
        command = result["command"]
        self.assertIn("--floating", command)
        self.assertIn("--approve-for-me", command)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)
        self.assertIn("AGENT_COORD_DELEGATION_ID=" + delegation["delegation_id"], command)
        self.assertIn("AGENT_COORD_DB=" + str(self.store.database_path), command)
        codex = command.index("/mock/codex")
        self.assertEqual(command[codex + 1], "--cd")
        self.assertNotIn("exec", command)
        add_dir = command.index("--add-dir")
        self.assertEqual(command[add_dir + 1], str(self.store.database_path.parent))
        self.assertNotIn("--model", command)
        self.assertFalse(
            any(item.startswith("model_reasoning_effort=") for item in command)
        )
        self.assertIsNone(delegation["model"])
        self.assertIsNone(delegation["reasoning_effort"])
        self.assertEqual(delegation["lease_mode"], "write")

    def test_validation_launch_records_lease_and_generates_non_editing_prompt(self) -> None:
        runner = FakeRun(self.root)

        result = self.delegate(
            runner,
            lease_mode="validation",
            instructions="Run the complete test suite and report the verdict.",
        )

        delegation = result["delegation"]
        prompt = result["command"][-1]
        self.assertEqual(delegation["lease_mode"], "validation")
        self.assertIn("delegated Codex validator", prompt)
        self.assertIn("--activity validating --lease-mode validation", prompt)
        self.assertIn("Do not edit repository files", prompt)
        self.assertIn("A failed check is a completed validation result", prompt)
        self.assertNotIn("Implement only the requested work", prompt)

    def test_validation_launch_rejects_yolo(self) -> None:
        runner = FakeRun(self.root)

        with self.assertRaisesRegex(
            CoordinationError, "Validation-only delegation cannot use --yolo"
        ):
            self.delegate(runner, lease_mode="validation", yolo=True)

    def test_launch_passes_and_records_model_and_reasoning_effort(self) -> None:
        runner = FakeRun(self.root)

        result = self.delegate(
            runner,
            model="gpt-5.6-terra",
            reasoning_effort="high",
        )

        command = result["command"]
        model = command.index("--model")
        self.assertEqual(command[model + 1], "gpt-5.6-terra")
        config = command.index("--config")
        self.assertEqual(config + 1, command.index('model_reasoning_effort="high"'))
        self.assertEqual(result["delegation"]["model"], "gpt-5.6-terra")
        self.assertEqual(result["delegation"]["reasoning_effort"], "high")

    def test_launch_constructs_reviewed_claude_command(self) -> None:
        runner = FakeRun(self.root)

        result = self.delegate(
            runner,
            client="claude",
            model="opus",
            reasoning_effort="high",
        )

        delegation = result["delegation"]
        self.assertEqual(delegation["client"], "claude")
        command = result["command"]
        self.assertIn("AGENT_COORD_CLIENT=claude", command)
        claude = command.index("/mock/claude")
        self.assertEqual(command[claude + 1], "--add-dir")
        self.assertNotIn("--cd", command)
        self.assertIn("--permission-mode", command)
        self.assertEqual(command[command.index("--permission-mode") + 1], "auto")
        self.assertEqual(command[command.index("--model") + 1], "opus")
        self.assertEqual(command[command.index("--effort") + 1], "high")
        self.assertNotIn("--approve-for-me", command)
        self.assertIn("delegated Claude Code worker", command[-1])

    def test_claude_yolo_and_hook_trust_flags_are_client_specific(self) -> None:
        runner = FakeRun(self.root)

        result = self.delegate(runner, client="claude", yolo=True)

        command = result["command"]
        self.assertIn("--dangerously-skip-permissions", command)
        self.assertNotIn("--permission-mode", command)
        with self.assertRaisesRegex(CoordinationError, "only supported for Codex"):
            self.delegate(runner, client="claude", bypass_hook_trust=True)

    def test_claude_login_must_report_logged_in(self) -> None:
        runner = FakeRun(self.root, claude_logged_in=False)

        with self.assertRaisesRegex(CoordinationError, "loggedIn=false"):
            self.delegate(runner, client="claude")

        self.assertEqual(self.store.list_delegations(), [])

    def test_yolo_requires_the_explicit_flag(self) -> None:
        runner = FakeRun(self.root)

        result = self.delegate(runner, yolo=True)

        command = result["command"]
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", command)
        self.assertNotIn("--approve-for-me", command)
        self.assertEqual(result["delegation"]["mode"], "yolo")

    def test_hook_trust_bypass_requires_the_explicit_flag(self) -> None:
        runner = FakeRun(self.root)

        result = self.delegate(runner, bypass_hook_trust=True)

        command = result["command"]
        self.assertIn("--dangerously-bypass-hook-trust", command)
        self.assertIn("--approve-for-me", command)
        self.assertTrue(result["delegation"]["bypass_hook_trust"])

    def test_dry_run_validates_without_creating_or_launching(self) -> None:
        runner = FakeRun(self.root)

        result = self.delegate(
            runner,
            dry_run=True,
            model="gpt-5.6-luna",
            reasoning_effort="low",
        )

        self.assertEqual(result["status"], "dry-run")
        self.assertEqual(self.store.list_delegations(), [])
        self.assertFalse(any(call[0][0] == "/mock/zellij" for call in runner.calls))
        self.assertIn("<dry-run>", " ".join(result["command"]))
        self.assertEqual(result["delegation"]["model"], "gpt-5.6-luna")
        self.assertEqual(result["delegation"]["reasoning_effort"], "low")
        self.assertEqual(result["delegation"]["lease_mode"], "write")

    def test_empty_model_or_reasoning_effort_is_rejected(self) -> None:
        runner = FakeRun(self.root)

        with self.assertRaisesRegex(CoordinationError, "model must not be empty"):
            self.delegate(runner, model="  ")
        with self.assertRaisesRegex(
            CoordinationError, "effort must not be empty"
        ):
            self.delegate(runner, reasoning_effort="  ")

    def test_launch_failure_is_durable(self) -> None:
        runner = FakeRun(self.root, launch_returncode=1)

        with self.assertRaisesRegex(CoordinationError, "pane failed"):
            self.delegate(runner)

        delegations = self.store.list_delegations()
        self.assertEqual(delegations[0]["status"], "failed")
        self.assertIn("pane failed", delegations[0]["error"])

    def test_not_ready_bead_is_rejected(self) -> None:
        def not_ready(command, **_options):
            if command[1] == "show":
                payload = [{"id": "work-a", "status": "open", "assignee": None}]
            else:
                payload = []
            return subprocess.CompletedProcess(
                command, 0, stdout=json.dumps(payload), stderr=""
            )

        with self.assertRaisesRegex(CoordinationError, "is not ready"):
            validate_ready_bead(
                "work-a",
                str(self.root),
                bd_executable="/mock/bd",
                run=not_ready,
            )


if __name__ == "__main__":
    unittest.main()
