from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PLUGIN_SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"
sys.path.insert(0, str(PLUGIN_SCRIPTS))

from agent_coord.managed_pty import (
    ManagedPTYWakeWatcher,
    _append_bounded_log,
    launch_managed_pty,
    output_log_path,
    read_delegation_output,
    read_output_tail,
    supervise_managed_pty,
)
from agent_coord.store import CoordinationStore


class FakeSupervisorProcess:
    def __init__(self, pid: int = 4321) -> None:
        self.pid = pid
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True


class ManagedPTYTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = CoordinationStore(self.root / "state.sqlite3")
        self.store.register(
            session_id="parent", client="codex", cwd=str(self.root), name="parent"
        )

    def create_delegation(self, identifier: str = "delegation-a") -> dict[str, object]:
        return self.store.create_delegation(
            parent_session_id="parent",
            cwd=str(self.root),
            bead_id="work-a",
            scopes=["src/**"],
            instructions="Implement work-a.",
            mode="reviewed",
            runtime_kind="managed-pty",
            delegation_id=identifier,
        )

    def test_launch_starts_detached_supervisor_and_records_runtime(self) -> None:
        self.create_delegation()
        calls: list[tuple[list[str], dict[str, object]]] = []

        def popen(command, **options):
            calls.append((list(command), options))
            return FakeSupervisorProcess()

        result = launch_managed_pty(self.store, "delegation-a", popen=popen)

        delegation = result["delegation"]
        self.assertEqual(delegation["runtime_kind"], "managed-pty")
        self.assertEqual(delegation["supervisor_pid"], 4321)
        self.assertTrue(delegation["output_log_path"].endswith("output.log"))
        self.assertTrue(calls[0][1]["start_new_session"])
        self.assertIn("supervise", calls[0][0])

    def test_managed_wake_injects_one_prompt_for_idle_child(self) -> None:
        self.create_delegation()
        self.store.register(
            session_id="child", client="codex", cwd=str(self.root), name="child"
        )
        self.store.attach_delegation("delegation-a", "child")
        self.store.begin_work(
            session_id="child",
            bead_id="work-a",
            scopes=["src/**"],
            activity="implementing",
        )
        self.store.touch("child", "waiting", turn_active=False)
        self.store.register_wake_target(
            session_id="child",
            transport="managed-pty",
            endpoint={"delegation_id": "delegation-a"},
            watcher_pid=os.getpid(),
        )
        message = self.store.send_message(
            sender_session_id="parent",
            recipient_session_id="child",
            body="Coordinate deployment ownership.",
        )
        writes: list[tuple[int, bytes]] = []
        watcher = ManagedPTYWakeWatcher(
            self.store,
            "child",
            write=lambda fd, value: writes.append((fd, value)) or len(value),
        )

        result = watcher.run_once(17)

        self.assertEqual(result["status"], "woke")
        self.assertEqual(result["message_ids"], [message["id"]])
        self.assertEqual(writes[0][0], 17)
        self.assertTrue(writes[0][1].endswith(b"\r"))
        self.assertEqual(watcher.run_once(17)["status"], "waiting")

    def test_output_log_is_bounded_and_read_without_terminal_escapes(self) -> None:
        path = self.root / "output.log"
        _append_bounded_log(
            path,
            b"old\n" * 20,
            limit_bytes=48,
            retain_bytes=24,
        )
        _append_bounded_log(path, b"\x1b[31mnew output\x1b[0m\r\n")

        output = read_output_tail(path)

        self.assertIn("older managed PTY output truncated", output)
        self.assertIn("new output", output)
        self.assertNotIn("\x1b", output)
        self.assertNotIn("\r", output)

    def test_output_log_renders_cursor_repaints_as_one_readable_screen(self) -> None:
        path = self.root / "output.log"
        _append_bounded_log(
            path,
            (
                b"\x1b[2J\x1b[1;1HWorking"
                b"\x1b[1;1HDone\x1b[K"
                b"\x1b[3;1Hfirst"
                b"\x1b[3;1Hfinal\x1b[K"
            ),
        )

        output = read_output_tail(path)

        self.assertEqual(output, "Done\n\nfinal")
        self.assertNotIn("Working", output)
        self.assertNotIn("first", output)

    def test_managed_output_survives_store_recreation(self) -> None:
        self.create_delegation()
        path = output_log_path(self.store, "delegation-a")
        self.store.mark_delegation_launched(
            "delegation-a",
            runtime_kind="managed-pty",
            supervisor_pid=4321,
            output_log_path=str(path),
        )
        _append_bounded_log(path, b"\x1b[1;1HPersisted output\r\n")

        reopened = CoordinationStore(self.store.database_path)

        self.assertIn(
            "Persisted output",
            read_delegation_output(reopened, "delegation-a"),
        )

    def test_zellij_output_is_snapshotted_and_reused_when_pane_is_gone(self) -> None:
        self.store.create_delegation(
            parent_session_id="parent",
            cwd=str(self.root),
            bead_id="work-zellij",
            scopes=["src/**"],
            instructions="Implement work-zellij.",
            mode="reviewed",
            runtime_kind="zellij",
            delegation_id="delegation-zellij",
        )
        self.store.mark_delegation_launched(
            "delegation-zellij",
            runtime_kind="zellij",
            zellij_session="test-session",
            pane_id="terminal_42",
        )
        with (
            patch(
                "agent_coord.managed_pty.shutil.which",
                return_value="/mock/zellij",
            ),
            patch(
                "agent_coord.managed_pty.ZellijClient.dump_screen",
                return_value="Readable Zellij screen\n",
            ),
        ):
            captured = read_delegation_output(self.store, "delegation-zellij")

        reopened = CoordinationStore(self.store.database_path)
        with patch("agent_coord.managed_pty.shutil.which", return_value=None):
            persisted = read_delegation_output(reopened, "delegation-zellij")

        self.assertEqual(captured, "Readable Zellij screen")
        self.assertEqual(persisted, captured)
        self.assertTrue(output_log_path(reopened, "delegation-zellij").is_file())

    @unittest.skipUnless(os.name == "posix", "managed PTY runtime requires POSIX")
    def test_supervisor_records_early_child_exit_and_output(self) -> None:
        self.create_delegation("delegation-exit")

        result = supervise_managed_pty(
            self.store,
            "delegation-exit",
            client_command=[
                sys.executable,
                "-c",
                "print('managed child output', flush=True)",
            ],
            environment={"PATH": os.environ.get("PATH", "")},
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["runtime_exit_code"], 0)
        self.assertIn(
            "managed child output",
            read_output_tail(result["output_log_path"]),
        )
        self.assertEqual(len(self.store.inbox("parent", mark_delivered=False)), 1)


if __name__ == "__main__":
    unittest.main()
