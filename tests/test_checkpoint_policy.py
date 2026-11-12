from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))

from agent_coord.checkpoint_mcp import TOOL
from agent_coord.context_lifecycle import REPORTING_INSTRUCTIONS
from agent_coord.store import CoordinationStore


class CheckpointPolicyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.now = 1000.0
        self.store = CoordinationStore(self.root / "state.sqlite3", clock=lambda: self.now)
        self.store.register(session_id="conversation", client="codex", cwd=str(self.root))
        with self.store._connection() as db:
            db.execute("CREATE TABLE browser_sessions (thread_id TEXT PRIMARY KEY)")
            db.execute("INSERT INTO browser_sessions VALUES ('conversation')")

    def complete_turn(self, turn_id):
        self.now += 10
        self.store.threads.start_turn("conversation", turn_id=turn_id,
                                     notification_context={"origin": "user"})
        self.now += 1
        self.store.threads.finish_turn("conversation", turn_id=turn_id)
        return self.store.threads.get("conversation", history=True)

    def test_guidance_requires_material_changes_and_exempts_routine_replies(self):
        instructions = self.store.threads.instructions("conversation", caller_context=True)
        for required in ("saved assignment state materially changes", "Ending a reply is not a checkpoint trigger",
                         "Skip routine explanations, clarifications, reformatting", "acknowledgments",
                         "Do not rewrite the summary to describe each answer"):
            self.assertIn(required, instructions)
        self.assertNotIn("checkpoint before returning control", instructions)
        self.assertIn("not routine discussion or each reply", TOOL["description"])

    def test_lifecycle_guidance_does_not_require_reports_for_every_checkpoint(self):
        self.assertIn("substantial milestone or actual handoff", REPORTING_INSTRUCTIONS)
        self.assertIn("a checkpoint does not itself require a handover", REPORTING_INSTRUCTIONS)
        self.assertIn("Omit it for routine discussion", REPORTING_INSTRUCTIONS)
        self.assertNotIn("before returning control", REPORTING_INSTRUCTIONS)

    def test_discussion_turns_record_completion_and_activity_without_checkpoints(self):
        for turn_id in ("question", "clarification", "acknowledgment"):
            with self.subTest(turn_id=turn_id):
                thread = self.complete_turn(turn_id)
                self.assertIsNone(thread["checkpoint"])
                self.assertEqual(thread["checkpoints"], [])
                self.assertEqual(thread["turn_completion"]["status"], "completed")
                self.assertTrue(thread["unread_result"])
        with self.store.reading(), self.store._connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM turn_completions").fetchone()[0], 3)
            events = [json.loads(row[0]) for row in db.execute(
                "SELECT details_json FROM activity_events WHERE kind='turn_ended'")]
        self.assertEqual(len(events), 3)
        self.assertTrue(all(event["trigger"] == "human" for event in events))

    def test_followup_preserves_assignment_checkpoint_and_updates_completion(self):
        self.store.threads.start_turn("conversation", turn_id="assignment")
        saved = self.store.threads.checkpoint("conversation", {
            "phase": "planning", "summary": "Use the agreed storage layout.",
            "next_step": {"actor": "agent", "action": "Implement the agreed layout."},
        })
        self.store.threads.finish_turn("conversation", turn_id="assignment")
        previous = self.store.threads.get("conversation")
        for turn_id in ("explanation", "formatting"):
            with self.subTest(turn_id=turn_id):
                current = self.complete_turn(turn_id)
                self.assertEqual(current["checkpoint"], saved["checkpoint"])
                self.assertEqual(len(current["checkpoints"]), 1)
                self.assertEqual(current["work_phase"], "planning")
                self.assertGreater(current["turn_completion"]["id"], previous["turn_completion"]["id"])
                self.assertTrue(current["unread_result"])
                previous = current


if __name__ == "__main__":
    unittest.main()
