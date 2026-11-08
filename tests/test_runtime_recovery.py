from __future__ import annotations

import tempfile
import unittest
import json
import uuid
from unittest.mock import patch
from pathlib import Path

from test_codex_app_server import FakeCodex
from agent_coord.codex_app_server import BrowserSessions, BrowserBusyError
from agent_coord.store import CoordinationStore
from agent_coord.runtime_recovery import RuntimeRecovery


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = CoordinationStore(Path(temporary.name) / "state.sqlite3")
        self.sessions = BrowserSessions(self.store, temporary.name, rpc_factory=FakeCodex)
        self.addCleanup(self.sessions.close)
        self.thread = self.sessions.create({"name": "Recovery test"})["session"]["thread_id"]
        self.sessions.send(self.thread, {"message": "Complete the assignment"})
        self.sessions._await_persistence(self.thread)
        self.source = self.sessions.active[self.thread]
        self.recovery = RuntimeRecovery(self.sessions)
        self.recovery.prepare("update-test")

    def finish(self, status="interrupted"):
        self.sessions.runtime_handover = True
        turn = self.sessions.rpc.threads[self.thread]["turns"][-1]
        turn["status"] = status
        self.sessions._event({"method": "turn/completed", "params": {
            "threadId": self.thread, "turn": turn, "controlledShutdown": True}})
        self.sessions._await_persistence(self.thread)

    def state(self):
        with self.store._connection() as db:
            return dict(db.execute("SELECT * FROM runtime_update_recoveries").fetchone())

    def turns(self):
        return [call for call in self.sessions.rpc.calls if call[0] == "turn/start"]

    def test_continues_once_without_replacing_original_request(self):
        original = self.store.threads.get(self.thread)["original_request"]
        self.finish()
        self.recovery.recover("update-test")
        self.sessions._await_persistence(self.thread)
        self.recovery.recover("update-test")
        self.assertEqual(self.state()["state"], "continued")
        self.assertEqual(len(self.turns()), 2)
        self.assertEqual(self.store.threads.get(self.thread)["original_request"], original)

    def test_native_completion_overrides_failed_local_receipt(self):
        self.finish("failed")
        self.sessions.rpc.threads[self.thread]["turns"][-1]["status"] = "completed"
        self.recovery.recover("update-test")
        self.assertEqual(self.state()["state"], "skipped")
        self.assertEqual(len(self.turns()), 1)

    def test_stop_between_claim_and_send_is_respected(self):
        self.finish()
        run = self.sessions.submissions.run
        def stop_then_submit(*args, **kwargs):
            self.store.threads.request_stop(self.thread, self.source)
            return run(*args, **kwargs)
        self.sessions.submissions.run = stop_then_submit
        self.recovery.recover("update-test")
        self.assertEqual(self.state()["state"], "skipped")
        self.assertEqual(len(self.turns()), 1)

    def test_request_arriving_during_shutdown_excludes_recovery(self):
        self.sessions._event({"id": 123, "method": "item/commandExecution/requestApproval",
                              "params": {"threadId": self.thread}})
        self.finish()
        self.recovery.finalize("update-test")
        self.recovery.recover("update-test")
        self.assertEqual(self.state()["state"], "skipped")
        self.assertEqual(len(self.turns()), 1)

    def test_transport_failure_is_never_replayed(self):
        self.finish()
        self.sessions.rpc.fail_turn = True
        self.recovery.recover("update-test")
        self.assertEqual(self.state()["state"], "uncertain")
        self.sessions.rpc.fail_turn = False
        self.recovery.recover("update-test")
        self.assertEqual(len(self.turns()), 2)

    def test_crashed_claim_is_uncertain_without_submission(self):
        self.finish()
        with self.store._connection() as db:
            db.execute("UPDATE runtime_update_recoveries SET state='submitting'")
        self.recovery.recover("update-test")
        self.assertEqual(self.state()["state"], "uncertain")
        self.assertEqual(len(self.turns()), 1)

    def test_missing_or_newer_native_history_is_not_restarted(self):
        self.finish()
        self.sessions.rpc.threads[self.thread]["turns"][-1]["id"] = "newer-native-turn"
        self.recovery.recover("update-test")
        self.assertEqual(self.state()["state"], "skipped")
        self.assertEqual(len(self.turns()), 1)

    def test_runtime_stop_between_claim_and_send_is_respected(self):
        self.finish()
        run = self.sessions.submissions.run
        def stop_then_submit(*args, **kwargs):
            self.recovery.permitted = lambda: False
            return run(*args, **kwargs)
        self.sessions.submissions.run = stop_then_submit
        self.recovery.recover("update-test")
        self.assertEqual(self.state()["state"], "skipped")
        self.assertEqual(len(self.turns()), 1)

    def test_existing_failure_pause_requires_review(self):
        self.finish()
        self.sessions.inbox_wake.pause(self.thread, "Inspect failed work", kind="failure")
        self.recovery.recover("update-test")
        self.assertEqual(self.state()["state"], "skipped")
        self.assertEqual(len(self.turns()), 1)

    def test_empty_native_history_does_not_use_display_cache(self):
        self.finish()
        self.sessions.rpc.threads[self.thread]["turns"] = []
        self.recovery.recover("update-test")
        self.assertEqual(self.state()["state"], "skipped")
        self.assertEqual(len(self.turns()), 1)

    def test_claude_native_source_and_completion_are_checked(self):
        native = str(uuid.uuid4())
        with self.store._connection() as db:
            db.execute("INSERT INTO claude_sessions(thread_id,native_id,started) VALUES (?,?,1)", (self.thread, native))
        root = self.store.database_path.parent / "native-claude"
        directory = root / "projects" / "project"
        directory.mkdir(parents=True)
        transcript = directory / (native + ".jsonl")
        source = {"type": "user", "uuid": "source-input", "message": {"content": "assignment"}}
        turn = {"items": [{"type": "userMessage", "id": "source-input"}]}
        with patch.dict("os.environ", {"CLAUDE_CONFIG_DIR": str(root)}):
            transcript.write_text(json.dumps(source) + "\n")
            self.recovery.verify_claude(self.thread, turn)
            for successor in (
                {"type": "assistant", "message": {"stop_reason": "end_turn"}},
                {"type": "assistant", "message": {}},
                {"type": "user", "uuid": "new-input", "message": {"content": "new assignment"}},
            ):
                with self.subTest(successor=successor):
                    transcript.write_text(json.dumps(source) + "\n" + json.dumps(successor) + "\n")
                    with self.assertRaises(BrowserBusyError):
                        self.recovery.verify_claude(self.thread, turn)
            transcript.write_text(json.dumps({**source, "uuid": "wrong-source"}) + "\n")
            with self.assertRaises(BrowserBusyError):
                self.recovery.verify_claude(self.thread, turn)
