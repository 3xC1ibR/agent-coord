from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN_SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"
sys.path.insert(0, str(PLUGIN_SCRIPTS))

from unittest.mock import patch

from agent_coord.store import CoordinationError, CoordinationStore
from agent_coord.zellij_wake import (
    ZellijCommandError,
    ZellijWakeWatcher,
    enable_zellij_wake,
    environment_requests_wake,
    prompt_is_empty,
    watch_zellij,
)


class FakeZellijClient:
    def __init__(self, screen: str = "❯\n", error: str | None = None) -> None:
        self.screen = screen
        self.error = error
        self.dumps = 0
        self.wakes: list[str] = []

    def dump_screen(self) -> str:
        self.dumps += 1
        if self.error:
            raise ZellijCommandError(self.error)
        return self.screen

    def wake(
        self, prompt: str = "Check and handle your unread agent-coord messages."
    ) -> None:
        if self.error:
            raise ZellijCommandError(self.error)
        self.wakes.append(prompt)


class ZellijWakeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = CoordinationStore(self.root / "state.sqlite3")
        self.store.register(
            session_id="sender",
            client="codex",
            cwd=str(self.root),
            name="sender",
        )
        self.store.register(
            session_id="receiver",
            client="claude",
            cwd=str(self.root),
            name="receiver",
        )
        self.store.register_zellij_wake(
            session_id="receiver",
            zellij_session="test-session",
            pane_id="terminal_7",
        )

    def watcher(self, fake: FakeZellijClient) -> ZellijWakeWatcher:
        return ZellijWakeWatcher(
            self.store,
            "receiver",
            client_factory=lambda **_arguments: fake,
        )

    def send(self, body: str = "hello") -> int:
        message = self.store.send_message(
            sender_session_id="sender",
            recipient_session_id="receiver",
            body=body,
        )
        return message["id"]

    def test_prompt_detection_rejects_unsubmitted_input(self) -> None:
        self.assertTrue(prompt_is_empty("header\n❯\u00a0\n-- INSERT --", "claude"))
        self.assertFalse(
            prompt_is_empty("header\n❯\u00a0commit this\n-- INSERT --", "claude")
        )
        self.assertTrue(
            prompt_is_empty("status\n› Ask Codex to do anything\nmodel", "codex")
        )
        self.assertFalse(prompt_is_empty("status\n› run the tests\nmodel", "codex"))

    def test_wake_opt_in_environment_is_explicit(self) -> None:
        self.assertTrue(environment_requests_wake({"AGENT_COORD_ZELLIJ_WAKE": "true"}))
        self.assertFalse(environment_requests_wake({}))

    def test_foreground_watcher_clears_pid_when_it_exits(self) -> None:
        result = watch_zellij(self.store, "receiver", once=True)

        self.assertEqual(result["status"], "waiting")
        self.assertIsNone(self.store.get_zellij_wake("receiver")["watcher_pid"])

    @patch("agent_coord.zellij_wake.shutil.which", return_value="/usr/bin/zellij")
    def test_enable_registers_target_and_starts_detached_watcher(self, _which) -> None:
        calls: list[tuple[list[str], dict[str, object]]] = []

        class Process:
            pid = 4321

        def fake_popen(command, **options):
            calls.append((command, options))
            return Process()

        result = enable_zellij_wake(
            self.store,
            "receiver",
            environ={
                "ZELLIJ_SESSION_NAME": "friendly-lemur",
                "ZELLIJ_PANE_ID": "7",
            },
            popen=fake_popen,
        )

        self.assertEqual(result["pane_id"], "terminal_7")
        self.assertEqual(result["watcher_pid"], 4321)
        self.assertTrue(result["watcher_started"])
        self.assertIn("wake-zellij", calls[0][0])
        self.assertTrue(calls[0][1]["start_new_session"])

    @patch("agent_coord.zellij_wake.shutil.which", return_value="/usr/bin/zellij")
    def test_enable_disables_target_when_watcher_cannot_start(self, _which) -> None:
        def fail_to_start(*_arguments, **_options):
            raise OSError("process limit")

        with self.assertRaisesRegex(
            CoordinationError, "Cannot start Zellij wake watcher"
        ):
            enable_zellij_wake(
                self.store,
                "receiver",
                zellij_session="friendly-lemur",
                pane_id="7",
                popen=fail_to_start,
            )

        self.assertFalse(self.store.get_zellij_wake("receiver")["enabled"])

    def test_active_turn_is_not_inspected_or_woken(self) -> None:
        self.send()
        self.store.touch("receiver", "waiting", turn_active=True)
        fake = FakeZellijClient()

        result = self.watcher(fake).run_once()

        self.assertEqual(result["status"], "busy")
        self.assertEqual(fake.dumps, 0)
        self.assertEqual(fake.wakes, [])

    def test_typed_prompt_waits_then_wakes_exactly_once(self) -> None:
        message_id = self.send()
        self.store.touch("receiver", "waiting", turn_active=False)
        fake = FakeZellijClient("❯ draft reply\n")
        watcher = self.watcher(fake)

        blocked = watcher.run_once()
        fake.screen = "❯\n"
        woke = watcher.run_once()
        repeated = watcher.run_once()

        self.assertEqual(blocked["status"], "prompt-not-empty")
        self.assertEqual(woke["status"], "woke")
        self.assertEqual(woke["message_ids"], [message_id])
        self.assertEqual(repeated["status"], "waiting")
        self.assertEqual(len(fake.wakes), 1)

    def test_missing_pane_does_not_change_message_delivery(self) -> None:
        message_id = self.send()
        self.store.touch("receiver", "idle", turn_active=False)
        fake = FakeZellijClient(error="pane not found")

        result = self.watcher(fake).run_once()

        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(self.store.pending_wake_message_ids("receiver"), [message_id])
        self.assertEqual(
            self.store.inbox("receiver", mark_delivered=False)[0]["body"], "hello"
        )
        self.assertIn(
            "pane not found", self.store.get_zellij_wake("receiver")["last_error"]
        )


if __name__ == "__main__":
    unittest.main()
