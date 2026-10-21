from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))
from agent_coord.kimi_status import PromptJournal


class KimiStatusTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, {"KIMI_CODE_HOME": str(self.root / "kimi")})
        environment.start()
        self.addCleanup(environment.stop)
        self.directory = self.root / "kimi/sessions/workspace/session_native"
        self.directory.mkdir(parents=True)
        (self.directory / "state.json").write_text(json.dumps({"id": "session_native", "cwd": str(self.root)}))
        self.path = self.directory / "agents/main/wire.jsonl"
        self.path.parent.mkdir(parents=True)
        self.header()

    def header(self, version="1.5"):
        self.path.write_text(json.dumps({"type": "metadata", "protocol_version": version}) + "\n")

    def append(self, *records):
        with self.path.open("a") as journal:
            for record in records:
                journal.write(json.dumps(record) + "\n")

    def cursor(self):
        return PromptJournal("session_native", str(self.root))

    def prompt(self, turn=0):
        return {"type": "turn.prompt", "agentId": "main", "turnId": turn, "promptId": str(turn), "time": 10**15}

    def ended(self, turn=0, *, agent="main", reason="failed"):
        return {"type": "turn.ended", "agentId": agent, "turnId": turn, "reason": reason,
                "error": {"message": "429 five-hour quota reached", "code": "provider.api_error",
                          "name": "APIProviderQuotaExhaustedError", "retryable": False,
                          "details": {"statusCode": 429, "private": "omit"}}}

    def test_only_new_matching_main_turn_failure_is_retained(self):
        self.append(self.prompt(), self.ended())
        cursor = self.cursor()
        self.assertIsNone(cursor.failure())
        self.append(self.prompt(1), self.ended(1, agent="worker"), self.ended(0))
        self.assertIsNone(cursor.failure())
        self.append(self.ended(1))
        self.assertEqual(cursor.failure(), {"message": "429 five-hour quota reached", "code": "provider.api_error",
            "name": "APIProviderQuotaExhaustedError", "retryable": False, "statusCode": 429})
        next_prompt = self.cursor()
        self.append(self.prompt(2), self.ended(2, reason="completed"))
        self.assertIsNone(next_prompt.failure())

    def test_first_prompt_can_create_journal_but_old_timestamps_are_excluded(self):
        self.path.unlink()
        cursor = self.cursor()
        self.header()
        old = self.prompt()
        old["time"] = 0
        self.append(old, self.ended())
        self.assertIsNone(cursor.failure())
        self.append(self.prompt(1), self.ended(1))
        self.assertEqual(cursor.failure()["statusCode"], 429)

    def test_missing_state_at_submission_can_be_resolved_for_first_prompt(self):
        state = self.directory / "state.json"
        value = state.read_bytes()
        state.unlink()
        cursor = self.cursor()
        state.write_bytes(value)
        self.append(self.prompt(), self.ended())
        self.assertEqual(cursor.failure()["statusCode"], 429)

    def test_replaced_and_rewritten_journal_are_not_attributed_to_prompt(self):
        cursor = self.cursor()
        self.path.rename(self.path.with_suffix(".old"))
        self.header()
        self.append(self.prompt(), self.ended())
        self.assertIsNone(cursor.failure())
        cursor = self.cursor()
        self.header()
        self.append(self.prompt(1), self.ended(1))
        self.assertIsNone(cursor.failure())

    def test_unsupported_or_malformed_journal_cannot_invent_failure(self):
        self.header("2.0")
        cursor = self.cursor()
        self.append(self.prompt(), self.ended())
        self.assertIsNone(cursor.failure())
        self.header()
        cursor = self.cursor()
        with self.path.open("ab") as journal:
            journal.write(b'invalid json\n')
        self.assertIsNone(cursor.failure())

    def test_reader_is_bounded_and_ignores_incomplete_tail_after_error(self):
        cursor = self.cursor()
        self.append(self.prompt(), self.ended())
        with patch("agent_coord.kimi_status.MAX_BYTES", 1):
            self.assertIsNone(cursor.failure())
        with patch("agent_coord.kimi_status.MAX_LINE", 100):
            self.assertIsNone(cursor.failure())
        with self.path.open("ab") as journal:
            journal.write(b'{"type":"unfinished')
        self.assertEqual(cursor.failure()["statusCode"], 429)

    def test_mapped_session_identity_and_cwd_are_revalidated(self):
        cursor = self.cursor()
        self.append(self.prompt(), self.ended())
        (self.directory / "state.json").write_text(json.dumps({"id": "session_native", "cwd": "/other"}))
        self.assertIsNone(cursor.failure())

    def test_prompt_failure_without_details_remains_failure(self):
        cursor = self.cursor()
        self.append(self.prompt(), {"type": "prompt.completed", "agentId": "main", "promptId": "0", "reason": "failed"})
        self.assertIn("failed without error details", cursor.failure()["message"])
