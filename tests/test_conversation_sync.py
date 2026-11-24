"""Consent, recovery and transport tests for completed Antigravity turns."""
import base64
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))
from agent_coord.conversation_sync import ConversationSync, _api, _token


def token(subject="owner"):
    body = base64.urlsafe_b64encode(json.dumps({"sub": subject}).encode()).decode().rstrip("=")
    return "header." + body + ".signature"


def completed(turn_id="turn", reply="Final answer", status="completed"):
    return {"id": turn_id, "status": status, "items": [
        {"type": "agentMessage", "text": "Progress"},
        {"type": "mcpToolCall", "result": "Private tool result"},
        {"type": "agentMessage", "text": reply}]}


class ConversationSyncTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.sync = ConversationSync(self.root / "sync")
        self.environment = patch.dict(os.environ, {"STOIC_API_TOKEN": token(),
            "STOIC_API_TOKEN_FILE": str(self.root / "absent"), "STOIC_API_URL": "https://stoic.example"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.calls = []
        self.remote = {}
        self.api = patch("agent_coord.conversation_sync._api", side_effect=self.server)
        self.api.start()
        self.addCleanup(self.api.stop)

    def server(self, path, credential, payload=None):
        self.calls.append((path, payload))
        if path.endswith("/me"):
            return {"actor": "agent"}
        key = payload["session_id"], payload["turn_id"]
        added = 0 if key in self.remote else 2
        self.remote[key] = dict(payload)
        return {"conversation_id": "conversation", "added": added}

    def state(self, session="session"):
        return self.sync._read(self.sync._directory(session))

    def stage(self, session="session", turn="turn", prompt="/stoic on"):
        self.sync.begin(session, turn, prompt)
        self.sync.finish(session, completed(turn), background=False)

    def test_consent_prefixes_and_off_precedence(self):
        for index, prompt in enumerate(["use Stoic", "please use Stoic for context", "/stoic",
                "$stoic", "/stoic on", "<command-name>/stoic</command-name>"]):
            with self.subTest(prompt=prompt):
                session = str(index)
                self.assertIn("is on", self.sync.begin(session, "turn", prompt))
                self.assertTrue(self.state(session)["enabled"])
                self.assertEqual(self.state(session)["pending"]["turn"]["prompt"], prompt)
        for prompt in ["/stoic off", "$stoic off", "stop using Stoic",
                       "<command-name>/stoic</command-name> off"]:
            self.stage()
            self.assertIn("is off", self.sync.begin("session", "off", prompt))
            self.assertFalse(self.state()["enabled"])
            self.assertFalse(self.state()["pending"])
        self.assertFalse(self.calls)

    def test_disabled_sessions_make_no_requests_or_save_text(self):
        for prompt in ["Hello", "I like Stoic", "/stoicism", "$stoical", "Let's use Stoic someday"]:
            self.sync.begin("session", "turn", prompt)
            self.sync.finish("session", completed(), background=False)
            self.sync.flush("session")
        self.assertFalse(self.sync.root.exists())
        self.assertFalse(self.calls)

    def test_coordination_delivery_cannot_change_consent(self):
        self.sync.begin("session", "turn", "/stoic on", allow_consent=False)
        self.assertFalse(self.sync.root.exists())
        self.stage()
        self.sync.begin("session", "peer", "/stoic off", allow_consent=False)
        self.assertTrue(self.state()["enabled"])

    def test_completed_turn_and_resume_use_stable_ids_and_only_final_reply(self):
        self.stage()
        self.sync = ConversationSync(self.sync.root)
        self.sync.flush("session")
        self.sync.finish("session", completed(), background=False)
        self.sync.flush("session")
        self.stage(turn="next", prompt="Continue")
        self.sync.flush("session")
        self.assertEqual(len(self.remote), 2)
        self.assertEqual(self.remote[("session", "turn")]["reply"], "Final answer")
        self.assertEqual(len([c for c in self.calls if c[1]]), 2)
        self.assertEqual(self.state()["last_turn_id"], "next")
        self.assertFalse(self.state()["pending"])

    def test_failed_and_interrupted_turns_do_not_upload(self):
        for status in ["failed", "interrupted"]:
            self.sync.begin("session", status, "/stoic")
            self.sync.finish("session", completed(status, status=status), background=False)
        self.sync.flush("session")
        self.assertFalse(self.calls)
        self.assertFalse(self.state()["pending"])

    def test_fork_needs_fresh_consent(self):
        self.stage()
        self.stage(session="fork", prompt="Continue inherited conversation")
        self.sync.flush("fork")
        self.assertFalse(self.state("fork"))
        self.assertFalse(self.calls)

    def test_missing_or_unreadable_credentials_leave_session_disabled(self):
        with patch.dict(os.environ, {"STOIC_API_TOKEN": ""}):
            self.assertIn("missing", self.sync.begin("session", "turn", "/stoic"))
        self.assertFalse(self.state().get("enabled"))
        self.assertFalse(self.calls)

    def test_account_change_cannot_upload_previous_account_turns(self):
        self.stage()
        with patch.dict(os.environ, {"STOIC_API_TOKEN": token("other")}):
            self.sync.flush("session")
            self.assertEqual(self.state()["last_error"], "credential_missing_or_account_changed")
            self.assertFalse(self.calls)
            self.stage(turn="other")
            self.sync.flush("session")
        self.assertEqual(list(self.remote), [("session", "other")])

    def test_lost_acknowledgment_retries_identical_payload(self):
        self.stage()
        def lost_receipt(path, credential, payload=None):
            result = self.server(path, credential, payload)
            if payload:
                raise OSError("lost acknowledgment")
            return result
        with patch("agent_coord.conversation_sync._api", side_effect=lost_receipt):
            self.sync.flush("session")
        original = self.state()["pending"]["turn"]
        self.sync = ConversationSync(self.sync.root)
        self.sync.flush("session")
        self.assertEqual(self.remote[("session", "turn")], original)
        self.assertEqual(len(self.remote), 1)
        self.assertFalse(self.state()["pending"])

    def test_off_discards_retry_and_reenable_does_not_restore_it(self):
        self.stage()
        with patch("agent_coord.conversation_sync._api", side_effect=OSError("offline")):
            self.sync.flush("session")
        self.sync.begin("session", "off", "/stoic off")
        self.sync.flush("session")
        self.stage(turn="reenabled")
        self.sync.flush("session")
        self.assertEqual(list(self.remote), [("session", "reenabled")])

    def test_off_during_upload_is_nonblocking_and_prevents_remaining_sends(self):
        self.stage()
        self.stage(turn="next", prompt="Another prompt")
        started, release = threading.Event(), threading.Event()
        def waiting_server(path, credential, payload=None):
            if payload:
                started.set()
                release.wait(3)
            return self.server(path, credential, payload)
        with patch("agent_coord.conversation_sync._api", side_effect=waiting_server):
            worker = threading.Thread(target=self.sync.flush, args=("session",))
            worker.start()
            try:
                self.assertTrue(started.wait(2))
                self.sync.begin("session", "off", "/stoic off")
                self.assertFalse(self.state()["pending"])
            finally:
                release.set()
                worker.join(3)
        self.assertEqual(list(self.remote), [("session", "turn")])
        self.assertFalse(self.state()["enabled"])

    def test_credential_and_exception_contents_never_enter_logs_or_state(self):
        self.stage()
        with patch("agent_coord.conversation_sync._api", side_effect=OSError(token())):
            with self.assertLogs("agent_coord.conversation_sync", level="WARNING") as logs:
                self.sync.flush("session")
        self.assertNotIn(token(), "".join(logs.output))
        self.assertNotIn(token(), json.dumps(self.state()))
        path = self.sync._directory("session") / "state.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_owner_token_is_not_used_to_import(self):
        self.stage()
        with patch("agent_coord.conversation_sync._api", return_value={"actor": "user"}):
            self.sync.flush("session")
        self.assertFalse(self.remote)
        self.assertEqual(self.state()["last_error"], "upload_deferred")

    def test_token_file_permissions_and_environment_precedence(self):
        path = self.root / "token"
        path.write_text(token("file"))
        path.chmod(0o600)
        with patch.dict(os.environ, {"STOIC_API_TOKEN_FILE": str(path)}):
            self.assertEqual(_token(), token())
            with patch.dict(os.environ, {"STOIC_API_TOKEN": ""}):
                self.assertEqual(_token(), token("file"))
                path.chmod(0o644)
                self.assertEqual(_token(), "")

    def test_insecure_nonlocal_origin_is_rejected_before_transport(self):
        with patch.dict(os.environ, {"STOIC_API_URL": "http://example.com"}):
            with self.assertRaises(ValueError):
                _api("/api/auth/web/me", token())


if __name__ == "__main__":
    unittest.main()
