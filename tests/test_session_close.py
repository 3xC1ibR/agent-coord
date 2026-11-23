from __future__ import annotations

import json
import os
import signal
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))
from agent_coord.session_close import (_is_terminal_codex, _is_owned_provider, is_external_codex_session,
                                       stop_terminal_session, verify_terminal_handoff)
from agent_coord.store import CoordinationError


class TerminalCloseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.session = {"session_id": "01a11269-04c4-7ca3-bcc2-d495885e590f", "client": "codex", "presence": "online"}
        transcript = self.transcript = self.root / "sessions/2026/10/06" / ("rollout-2026-10-06-" + self.session["session_id"] + ".jsonl")
        transcript.parent.mkdir(parents=True)
        transcript.write_text(json.dumps({"type": "session_meta", "payload": {"id": self.session["session_id"]}}) + "\n")
        self.addCleanup(patch.stopall)
        patch.dict("os.environ", {"CODEX_HOME": str(self.root)}).start()
        self.kill = patch("agent_coord.session_close.os.kill").start()
        self.owners = patch("agent_coord.session_close._owners", return_value={43210}).start()
        self.terminal = patch("agent_coord.session_close._is_terminal_codex", return_value=True).start()

    def test_stops_only_verified_owner_and_waits_for_exit(self):
        self.kill.side_effect = [None, ProcessLookupError()]
        stop_terminal_session(self.session)
        self.assertEqual(self.owners.call_count, 2)
        self.assertEqual(self.kill.call_args_list[0].args, (43210, signal.SIGTERM))

    def test_external_identity_requires_matching_desktop_metadata(self):
        for source, session_id, expected in [
            ("vscode", self.session["session_id"], True),
            ("cli", self.session["session_id"], False),
            (None, self.session["session_id"], False),
            ("unknown", self.session["session_id"], False),
            ("vscode", "another-session", False),
        ]:
            with self.subTest(source=source, session_id=session_id):
                self.transcript.write_text(json.dumps({"type": "session_meta", "payload": {
                    "id": session_id, "source": source}}) + "\n")
                self.assertEqual(is_external_codex_session(self.session), expected)
        self.owners.assert_not_called()
        self.kill.assert_not_called()

    def test_missing_invalid_and_ambiguous_transcripts_do_not_bypass_terminal_guard(self):
        self.transcript.write_text("invalid json\n")
        self.assertFalse(is_external_codex_session(self.session))

        self.transcript.write_text(json.dumps({"type": "session_meta", "payload": {
            "id": self.session["session_id"], "source": "vscode"}}) + "\n")
        duplicate = self.transcript.with_name("duplicate-" + self.session["session_id"] + ".jsonl")
        duplicate.write_text(self.transcript.read_text())
        self.assertFalse(is_external_codex_session(self.session))
        duplicate.unlink()
        self.transcript.unlink()
        self.assertFalse(is_external_codex_session(self.session))

    def test_external_conversation_can_already_be_archived_in_codex(self):
        archived = self.root / "archived_sessions" / self.transcript.name
        archived.parent.mkdir()
        archived.write_text(json.dumps({"type": "session_meta", "payload": {
            "id": self.session["session_id"], "source": "vscode"}}) + "\n")
        self.transcript.unlink()
        self.assertTrue(is_external_codex_session(self.session))

    def test_reused_owner_shared_server_or_ambiguous_identity_is_never_signalled(self):
        for owners, terminal in [([{43210}, {43211}], True), ([{43210, 43211}], True), ([set()], True), ([{43210}], False)]:
            with self.subTest(owners=owners, terminal=terminal):
                self.owners.side_effect = owners
                self.terminal.return_value = terminal
                with self.assertRaises(CoordinationError):
                    stop_terminal_session(self.session)
                self.kill.assert_not_called()

    def test_offline_sessions_need_no_process_access(self):
        stop_terminal_session({**self.session, "presence": "offline"})
        self.owners.assert_not_called()
        self.kill.assert_not_called()

    def test_handoff_accepts_a_quiet_unowned_transcript_even_with_vscode_transport(self):
        self.transcript.write_text(json.dumps({"type": "session_meta", "payload": {
            "id": self.session["session_id"], "source": "vscode", "originator": "codex-tui"}}) + "\n")
        os.utime(self.transcript, (1, 1))
        self.owners.return_value = set()
        verify_terminal_handoff({**self.session, "presence": "stale", "turn_active": False})
        self.assertEqual(self.owners.call_count, 2)
        self.kill.assert_not_called()

    def test_handoff_rejects_recent_writes_live_or_ambiguous_owners_and_running_turns(self):
        session = {**self.session, "presence": "stale", "turn_active": False}
        with self.assertRaisesRegex(CoordinationError, "recent activity"):
            verify_terminal_handoff(session)
        os.utime(self.transcript, (1, 1))
        for owners in [{43210}, {43210, 43211}]:
            self.owners.return_value = owners
            with self.assertRaises(CoordinationError):
                verify_terminal_handoff(session)
        self.owners.return_value = set()
        for changes in [{"presence": "online"}, {"turn_active": True}]:
            with self.assertRaises(CoordinationError):
                verify_terminal_handoff({**session, **changes})
        self.kill.assert_not_called()

    def test_handoff_rechecks_for_new_owners_and_transcript_changes(self):
        os.utime(self.transcript, (1, 1))
        session = {**self.session, "presence": "stale", "turn_active": False}
        self.owners.side_effect = [set(), {43210}]
        with self.assertRaises(CoordinationError):
            verify_terminal_handoff(session)
        def append_event(_):
            with self.transcript.open("a") as stream:
                stream.write('{}\n')
            return set()
        self.owners.side_effect = append_event
        with self.assertRaises(CoordinationError):
            verify_terminal_handoff(session)
        self.kill.assert_not_called()

    def test_handoff_after_resume_permits_only_the_attached_provider(self):
        self.owners.return_value = {43210}
        session = {**self.session, "presence": "stale", "turn_active": False}
        verify_terminal_handoff(session, permitted_owner=43210)
        self.owners.return_value = {43210, 43211}
        with self.assertRaises(CoordinationError):
            verify_terminal_handoff(session, permitted_owner=43210)
        self.kill.assert_not_called()

    def test_native_app_server_child_is_owned_but_unrelated_or_reused_processes_are_not(self):
        for replies, expected in [
            (["/bin/codex app-server", "43210"], True),
            (["/bin/codex app-server", "43209"], False),
            (["/bin/codex app-server", "1"], False),
            (["/bin/codex app-server", "43211"], False),
            (["/bin/codex exec task"], False),
            (["/bin/codex exec resume target-thread app-server"], False),
            (["/bin/codex --model app-server resume target-thread"], False),
            (["/bin/zsh"], False),
        ]:
            with self.subTest(replies=replies), patch("agent_coord.session_close._command", side_effect=replies):
                self.assertEqual(_is_owned_provider(43211, 43210), expected)

    def test_non_exiting_process_reports_failure_without_forcing_it(self):
        with self.assertRaisesRegex(CoordinationError, "not exited"):
            stop_terminal_session(self.session, timeout=0)
        self.kill.assert_called_once_with(43210, signal.SIGTERM)

    def test_shared_app_server_and_shell_are_not_terminal_codex(self):
        # Call the original function, independent of the shutdown test patch.
        for command, tty, expected in [("/bin/codex resume abc", "ttys001", True),
                                       ("/bin/codex app-server", "ttys001", False),
                                       ("/bin/codex", "??", False),
                                       ("/bin/zsh", "ttys001", False)]:
            with self.subTest(command=command, tty=tty), patch("agent_coord.session_close._command", side_effect=[command, tty]):
                self.assertEqual(_is_terminal_codex(43210), expected)


if __name__ == "__main__":
    unittest.main()
