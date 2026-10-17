from __future__ import annotations

import copy
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))

from agent_coord.app_control import AppControl
from agent_coord.checkpoint_mcp import update_checkpoint
from agent_coord.cli import _parser, run
from agent_coord.context_lifecycle import (MAX_REPORT_CHARS, REPORTING_INSTRUCTIONS,
                                          snapshot_for_assignment, validate_report)
from agent_coord.hook import _format_messages
from agent_coord.message_delivery import MessageDelivery
from agent_coord.store import CoordinationError, CoordinationStore


class ContextLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = CoordinationStore(self.root / "state.sqlite3")
        for name in ("owner", "worker", "peer", "direct", "dispatched"):
            self.store.register(session_id=name, client="codex", cwd=str(self.root))
        self.control = AppControl(self.store)
        self.created("worker", "orchestration")
        self.created("dispatched", None)
        self.store.threads.start_turn("worker", turn_id="turn-1")
        self.assignment = self.assign()

    def created(self, child, mode):
        payload = {"cwd": str(self.root), "prompt": "Investigate", "client": "codex"}
        if mode:
            payload["mode"] = mode
        request = self.control.request("create", "owner", payload)
        self.control.claim(request["request_id"], "runtime")
        self.control.identified(request["request_id"], child)
        self.control.complete(request["request_id"], result={"thread_id": child})

    def assign(self, child="worker", **values):
        return self.store.send_message(sender_session_id="owner", recipient_session_id=child,
                                       body="Investigate the implementation", **values)

    def report(self, message_id=None):
        return {
            "assignment_message_id": message_id or self.assignment["id"],
            "recommendation": "reuse",
            "reason": "Retained debugging constraints help with related fixes.",
            "reuse_for": ["Fixing this implementation"],
            "fresh_for": ["Independent review"],
            "handover": {"confirmed_facts": ["The parser rejects empty input."],
                         "constraints": ["Keep the CLI dependency-free."],
                         "tried_and_failed": ["Trimming after parsing preserves invalid whitespace."],
                         "open_questions": []},
        }

    def save(self, report=None, **values):
        return self.store.threads.checkpoint("worker", {
            "phase": "investigation", "summary": "Checked the parser.",
            "context_lifecycle": report if report is not None else self.report(), **values,
        })

    def snapshot(self, message_id=None, turn_id="turn-1", **values):
        with self.store._connection() as db:
            return snapshot_for_assignment(db, "worker", message_id or self.assignment["id"], turn_id, **values)

    def test_mcp_and_cli_persist_structured_history_and_report_only_changes(self):
        receipt = update_checkpoint({"session_id": "worker", "phase": "investigation",
            "summary": "Checked the parser.", "context_lifecycle": self.report()}, store=self.store)
        self.assertEqual(set(receipt), {"status", "checkpoint_id"})
        self.assertEqual(self.store.threads.get("worker")["checkpoint"]["context_lifecycle"], self.report())
        repeated = self.save()["checkpoint"]
        self.assertEqual(repeated["id"], receipt["checkpoint_id"])
        changed = self.report()
        changed["recommendation"] = "fresh"
        args = _parser().parse_args(["--db", str(self.store.database_path), "checkpoint", "--session-id", "worker",
                                    "--json", json.dumps({"context_lifecycle": changed})])
        saved = run(args)
        self.assertNotEqual(saved["checkpoint"]["id"], receipt["checkpoint_id"])
        self.assertEqual(saved["checkpoint"]["context_lifecycle"], changed)
        self.assertEqual(saved["checkpoints"][1]["context_lifecycle"], self.report())
        self.assertNotIn("context_lifecycle_json", saved["checkpoint"])
        self.assertNotIn("context_lifecycle_turn_key", saved["checkpoint"])
        reopened = CoordinationStore(self.store.database_path)
        self.assertEqual(reopened.threads.get("worker")["checkpoint"]["context_lifecycle"], changed)

    def test_omission_preserves_only_within_turn_and_null_clears(self):
        self.save()
        kept = self.store.threads.checkpoint("worker", {"summary": "Result ready."})
        self.assertEqual(kept["checkpoint"]["context_lifecycle"], self.report())
        cleared = self.store.threads.checkpoint("worker", {"context_lifecycle": None})
        self.assertNotIn("context_lifecycle", cleared["checkpoint"])
        self.assertIsNone(self.snapshot())
        self.save()
        self.store.threads.start_turn("worker", turn_id="turn-2")
        self.assertIsNone(self.snapshot(turn_id="turn-2"))
        # Completion delivery may arrive after another turn starts: read the exact old turn.
        self.assertEqual(self.snapshot()["recommendation"], "reuse")
        next_checkpoint = self.store.threads.checkpoint("worker", {"summary": "Started another assignment."})
        self.assertNotIn("context_lifecycle", next_checkpoint["checkpoint"])
        self.assertIsNone(self.snapshot(turn_id="turn-2"))

    def test_providerless_turn_keys_prevent_report_reuse_and_honor_clearing(self):
        self.store.threads.start_turn("worker")
        self.save()
        with self.store._connection() as db:
            first_key = db.execute("SELECT turn_key FROM work_threads WHERE thread_id = 'worker'").fetchone()[0]
        self.assertIsNone(self.snapshot(turn_id=None))
        self.assertIsNotNone(self.snapshot(turn_id=None, turn_key=first_key))
        self.store.threads.checkpoint("worker", {"context_lifecycle": None})
        self.assertIsNone(self.snapshot(turn_id=None, turn_key=first_key))
        self.save()
        self.store.threads.start_turn("worker")
        response = self.store.reply_message(sender_session_id="worker", message_id=self.assignment["id"], body="Result")
        self.assertNotIn("context_lifecycle", response)

    def test_reply_snapshots_are_immutable_and_arrive_with_normal_delivery(self):
        checkpoint = self.save()["checkpoint"]
        response = self.store.reply_message(sender_session_id="worker", message_id=self.assignment["id"], body="Result")
        expected = {"checkpoint_id": checkpoint["id"], **self.report()}
        self.assertEqual(response["context_lifecycle"], expected)
        self.assertFalse(response["reply_required"])
        self.assertEqual(response["in_reply_to"], self.assignment["id"])
        self.assertIsNotNone(MessageDelivery(self.store).read("owner", self.assignment["id"])["acknowledged_at"])
        changed = self.report()
        changed["recommendation"] = "fresh"
        self.save(changed)
        reopened = CoordinationStore(self.store.database_path)
        inbox = reopened.inbox("owner", mark_delivered=False)
        self.assertEqual(len(inbox), 1)
        self.assertEqual(inbox[0]["context_lifecycle"], expected)
        lifecycle_line = next(line for line in _format_messages(inbox).splitlines()
                              if line.startswith("  Context lifecycle (result metadata): "))
        self.assertEqual(json.loads(lifecycle_line.split(": ", 1)[1]), expected)
        delivery = MessageDelivery(reopened)
        self.assertIn('"context_lifecycle"', delivery.prepare("owner", "parent-turn")["text"])
        self.assertEqual(delivery.read("owner", response["id"])["context_lifecycle"], expected)
        self.assertEqual(reopened.threads.get("worker")["attention"], "now")

    def test_wrong_assignments_and_stale_turns_do_not_attach_reports(self):
        self.save()
        other = self.assign()
        self.assertIsNone(self.snapshot(other["id"]))
        response = self.store.reply_message(sender_session_id="worker", message_id=other["id"], body="Other result")
        self.assertNotIn("context_lifecycle", response)
        self.store.threads.start_turn("worker", turn_id="turn-2")
        response = self.store.reply_message(sender_session_id="worker", message_id=self.assignment["id"], body="Result")
        self.assertNotIn("context_lifecycle", response)
        with self.assertRaisesRegex(CoordinationError, "older or unknown turn"):
            self.save(turn_id="turn-1")

    def test_missing_reports_do_not_block_replies(self):
        response = self.store.reply_message(sender_session_id="worker", message_id=self.assignment["id"], body="Result")
        self.assertNotIn("context_lifecycle", response)
        self.assertEqual(len(self.store.inbox("owner", mark_delivered=False)), 1)

    def test_completion_can_select_its_frozen_checkpoint_after_a_late_report(self):
        original = self.save()["checkpoint"]
        changed = self.report()
        changed["recommendation"] = "fresh"
        self.save(changed)
        self.assertEqual(self.snapshot()["recommendation"], "fresh")
        frozen = self.snapshot(checkpoint_id=original["id"])
        self.assertEqual(frozen, {"checkpoint_id": original["id"], **self.report()})
        self.assertIsNone(self.snapshot(turn_id="unknown", checkpoint_id=original["id"]))
        self.assertIsNone(self.snapshot(checkpoint_id=99999))

    def test_only_orchestration_creations_receive_guidance_and_can_report(self):
        self.assertIn(REPORTING_INSTRUCTIONS, self.store.threads.instructions("worker"))
        for child in ("direct", "dispatched"):
            with self.subTest(child=child):
                assignment = self.assign(child)
                self.assertNotIn(REPORTING_INSTRUCTIONS, self.store.threads.instructions(child))
                with self.assertRaisesRegex(CoordinationError, "orchestration-created"):
                    self.store.threads.checkpoint(child, {"phase": "investigation", "summary": "Result.",
                        "context_lifecycle": self.report(assignment["id"])})
                reply = self.store.reply_message(sender_session_id=child, message_id=assignment["id"], body="Result")
                self.assertNotIn("context_lifecycle", reply)
        # Reusing the orchestration worker from a peer does not change its creation intent.
        peer_request = self.store.send_message(sender_session_id="peer", recipient_session_id="worker", body="Related fix")
        self.save(self.report(peer_request["id"]))
        self.assertIsNotNone(self.snapshot(peer_request["id"]))

    def test_report_must_reference_an_actionable_assignment_to_its_owner(self):
        wrong_recipient = self.assign("peer")
        informational = self.assign(classification="informational", reply_required=False)
        closed = self.assign()
        self.store.send_message(sender_session_id="owner", recipient_session_id="worker", body="Closed",
                                classification="closure", thread_id=closed["thread_id"])
        for message_id in (99999, wrong_recipient["id"], informational["id"], closed["id"]):
            with self.subTest(message_id=message_id), self.assertRaisesRegex(CoordinationError, "actionable assignment"):
                self.save(self.report(message_id))
        # No-reply assignments still receive a checkpoint envelope for completion events.
        no_reply = self.assign(reply_required=False)
        self.save(self.report(no_reply["id"]))
        self.assertIsNotNone(self.snapshot(no_reply["id"]))

    def test_invalid_or_excessive_reports_are_rejected_without_partial_updates(self):
        invalid = [[], {}, {**self.report(), "unknown": "value"},
                   {**self.report(), "assignment_message_id": True},
                   {**self.report(), "assignment_message_id": -1},
                   {**self.report(), "recommendation": "terminate"},
                   {**self.report(), "recommendation": []},
                   {**self.report(), "reason": " "},
                   {**self.report(), "reason": "x" * 501},
                   {**self.report(), "reuse_for": "fixes"},
                   {**self.report(), "reuse_for": ["x"] * 9},
                   {**self.report(), "fresh_for": ["x" * 401]},
                   {**self.report(), "handover": {"confirmed_facts": []}}]
        oversized = self.report()
        for field in oversized["handover"]:
            oversized["handover"][field] = ["x" * 400] * 8
        self.assertGreater(len(json.dumps(oversized)), MAX_REPORT_CHARS)
        invalid.append(oversized)
        checkpoint = self.save()["checkpoint"]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(CoordinationError):
                self.save(value, summary="Must not persist")
            self.assertEqual(self.store.threads.get("worker")["checkpoint"]["id"], checkpoint["id"])
        normalized = copy.deepcopy(self.report())
        normalized["reason"] = "  Retained context helps.  "
        self.assertEqual(validate_report(normalized)["reason"], "Retained context helps.")

    def test_upgrade_from_checkpoint_schema_without_lifecycle_fields(self):
        legacy = self.root / "legacy.sqlite3"
        with closing(sqlite3.connect(legacy)) as db, db:
            db.execute("""CREATE TABLE thread_checkpoints (
                id INTEGER PRIMARY KEY AUTOINCREMENT, thread_id TEXT NOT NULL,
                phase TEXT NOT NULL, summary TEXT NOT NULL, next_action TEXT NOT NULL,
                next_actor TEXT NOT NULL, session_id TEXT NOT NULL, turn_id TEXT,
                author TEXT NOT NULL DEFAULT 'agent', created_at REAL NOT NULL, refreshed_at REAL NOT NULL)""")
            db.execute("""INSERT INTO thread_checkpoints (thread_id, phase, summary, next_action, next_actor,
                session_id, created_at, refreshed_at) VALUES ('legacy', 'investigation', 'Preserved', '',
                'nobody', 'legacy', 1, 1)""")
        upgraded = CoordinationStore(legacy)
        upgraded.register(session_id="legacy", client="codex", cwd=str(self.root))
        checkpoint = upgraded.threads.get("legacy")["checkpoint"]
        self.assertEqual(checkpoint["summary"], "Preserved")
        self.assertNotIn("context_lifecycle", checkpoint)
        upgraded.threads.checkpoint("legacy", {"summary": "Still works"})


if __name__ == "__main__":
    unittest.main()
