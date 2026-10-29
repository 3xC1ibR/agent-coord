from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))

from agent_coord.cli import _parser, run
from agent_coord.hook import handle, _actionable_context
from agent_coord.message_delivery import MessageDelivery
from agent_coord.store import CoordinationError, CoordinationStore
import test_codex_app_server as codex_fakes
import test_browser_providers as provider_fakes
import test_antigravity as gravity_fakes


class DeferredMessageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = CoordinationStore(self.root / "state.sqlite3")
        for identity in ("sender", "recipient", "successor"):
            self.store.register(session_id=identity, client="codex", cwd=str(self.root))
        env = patch.dict("os.environ", {"AGENT_COORD_CLIENT": "codex",
                         "AGENT_COORD_SESSION_ID": "", "AGENT_COORD_ZELLIJ_WAKE": "0"})
        env.start()
        self.addCleanup(env.stop)

    def send(self, recipient="recipient", **kwargs):
        return self.store.send_message(sender_session_id="sender", recipient_session_id=recipient,
            body="Before a heavy test, defer it until 20:32 UTC; after that this constraint expires.",
            classification="deferred", **kwargs)

    def payload(self, event, **kwargs):
        return {"hook_event_name": event, "session_id": "recipient", "cwd": str(self.root), **kwargs}

    def sessions(self):
        sessions = codex_fakes.BrowserSessions(self.store, str(self.root), rpc_factory=codex_fakes.FakeCodex,
            claude_factory=provider_fakes.claude_factory,
            antigravity_factory=lambda s, event: gravity_fakes.AntigravityRPC(
                s, event, connection_factory=gravity_fakes.FakeACP))
        self.addCleanup(sessions.close)
        return sessions

    def test_cli_sends_deferred_without_reply_and_rejects_reply_obligation_atomically(self):
        parser = _parser()
        argv = ["--db", str(self.store.database_path), "send", "--from-session", "sender",
                "--session", "recipient", "--classification", "deferred"]
        result = run(parser.parse_args([*argv, "--no-reply-required", "Use the agreed API."]))
        self.assertEqual(result["classification"], "deferred")
        self.assertFalse(result["reply_required"])
        with self.assertRaisesRegex(CoordinationError, "cannot request a reply"):
            run(parser.parse_args([*argv, "--reply-required", "Confirm the constraint."]))
        self.assertEqual(len(self.store.inbox("recipient", mark_delivered=False)), 1)
        with self.assertRaisesRegex(CoordinationError, "does not request a reply"):
            self.store.reply_message(sender_session_id="recipient", message_id=result["id"], body="Received")

    def test_hooks_deliver_at_next_boundary_once_and_leave_information_in_history(self):
        for event in ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse"):
            with self.subTest(event=event):
                msg = self.send()
                info = self.store.send_message(sender_session_id="sender", recipient_session_id="recipient",
                    body="History only", classification="informational", reply_required=False)
                result = handle(self.payload(event, prompt="Continue", tool_name="Bash",
                                            tool_input={"command": "pwd"}), self.store)
                context = result["hookSpecificOutput"]["additionalContext"]
                self.assertIn(msg["body"], context)
                self.assertIn("classification=deferred", context)
                self.assertIn("reply_required=false", context)
                self.assertIn("not requests to start work or send a receipt", context)
                self.assertNotIn("History only", context)
                self.assertEqual(_actionable_context(self.store, "recipient"), "")
                history = {m["id"]: m for m in self.store.inbox("recipient", include_delivered=True,
                                                               mark_delivered=False)}
                self.assertIsNotNone(history[msg["id"]]["delivered_at"])
                self.assertIsNone(history[msg["id"]]["acknowledged_at"])
                self.assertIsNone(history[info["id"]]["delivered_at"])

    def test_provider_hook_boundaries_preserve_deferred_until_consumable(self):
        for client in ("claude", "codex", "muse", "kimi"):
            with self.subTest(client=client), patch.dict("os.environ", {
                    "AGENT_COORD_CLIENT": client, "AGENT_COORD_SESSION_ID": "recipient"}):
                msg = self.send()
                if client == "kimi":
                    self.assertEqual(_actionable_context(self.store, "recipient", "PreToolUse"), "")
                context = _actionable_context(self.store, "recipient", "UserPromptSubmit")
                self.assertIn(msg["body"], context)
                self.store.acknowledge("recipient", msg["id"])

    def test_restart_preserves_deferred_and_acknowledgement_is_silent(self):
        msg = self.send()
        reopened = CoordinationStore(self.store.database_path)
        self.assertEqual(reopened.pending_wake_message_ids("recipient"), [])
        self.assertIn(msg["body"], _actionable_context(reopened, "recipient"))
        reopened.acknowledge("recipient", msg["id"])
        self.assertEqual(reopened.inbox("recipient", unread_only=True), [])
        self.assertEqual(reopened.inbox("sender"), [])

    def test_terminal_wake_claim_cannot_claim_deferred_even_with_explicit_id(self):
        self.store.register_zellij_wake(session_id="recipient", zellij_session="test", pane_id="terminal_1")
        msg = self.send()
        self.assertEqual(self.store.pending_wake_message_ids("recipient"), [])
        self.assertEqual(self.store.claim_wake_messages("recipient", [msg["id"]]), [])
        self.assertIn(msg["body"], _actionable_context(self.store, "recipient"))

    def test_closure_withdraws_pending_constraint_for_hooks_and_provider_delivery(self):
        msg = self.send()
        self.store.send_message(sender_session_id="sender", recipient_session_id="recipient",
            body="The test finished; withdraw this hold.", classification="closure",
            thread_id=msg["thread_id"], reply_required=False)
        self.assertEqual(_actionable_context(self.store, "recipient"), "")
        self.assertIsNone(MessageDelivery(self.store).prepare("recipient", "next-turn"))
        with self.store._connection() as db:
            self.assertIsNotNone(db.execute("SELECT suppressed_at FROM messages WHERE id=?", (msg["id"],)).fetchone()[0])
        next_msg = self.send(thread_id=msg["thread_id"])
        self.assertIn(f"Message #{next_msg['id']}", _actionable_context(self.store, "recipient"))

    def test_app_deferral_neither_wakes_nor_reopens_and_reaches_later_user_turn(self):
        sessions = self.sessions()
        for client in ("codex", "claude"):
            with self.subTest(client=client):
                thread = sessions.create({"client": client})["session"]["thread_id"]
                self.send(thread)
                sessions.inbox_wake.process_once()
                self.assertNotIn(thread, sessions.active)
                with self.store._connection() as db:
                    self.assertEqual(db.execute("SELECT count(*) FROM message_wake_attempts WHERE session_id=?", (thread,)).fetchone()[0], 0)
                sessions.close_work_thread(thread)
                msg = self.send(thread)
                sessions.inbox_wake.process_once()
                self.assertEqual(self.store.threads.get(thread)["attention"], "archived")
                self.assertNotIn(thread, sessions.active)
                with self.store._connection() as db:
                    self.assertEqual(db.execute("SELECT count(*) FROM message_wake_attempts WHERE message_id=?", (msg["id"],)).fetchone()[0], 0)
                    self.assertIsNone(db.execute("SELECT delivered_at FROM messages WHERE id=?", (msg["id"],)).fetchone()[0])
                sessions.reopen_work_thread(thread)
                sessions.send(thread, {"message": "Continue my task"})
                self.assertIn(msg["body"], _actionable_context(self.store, thread))

    def test_new_deferred_during_work_does_not_start_followup_turn(self):
        sessions = self.sessions()
        thread = sessions.create({"client": "codex"})["session"]["thread_id"]
        sessions.send(thread, {"message": "Do my task"})
        self.send(thread)
        turn = sessions.rpc.threads[thread]["turns"][-1]
        turn["status"] = "completed"
        sessions._event({"method": "turn/completed", "params": {"threadId": thread, "turn": turn}})
        sessions.inbox_wake.process_once()
        self.assertEqual(len(sessions.rpc.threads[thread]["turns"]), 1)
        self.assertNotIn(thread, sessions.active)

    def test_mixed_queue_wakes_only_for_action_and_delivers_both(self):
        sessions = self.sessions()
        thread = sessions.create({"client": "codex"})["session"]["thread_id"]
        deferred = self.send(thread)
        action = self.store.send_message(sender_session_id="sender", recipient_session_id=thread,
                                        body="Actual work", reply_required=False)
        sessions.inbox_wake.process_once()
        with self.store._connection() as db:
            attempts = [r[0] for r in db.execute("SELECT message_id FROM message_wake_attempts")]
        self.assertEqual(attempts, [action["id"]])
        context = _actionable_context(self.store, thread)
        self.assertIn(deferred["body"], context)
        self.assertIn(action["body"], context)

    def test_antigravity_injects_deferred_on_user_prompt_without_automatic_turn(self):
        sessions = self.sessions()
        thread = sessions.create({"client": "antigravity"})["session"]["thread_id"]
        msg = self.send(thread)
        sessions.inbox_wake.process_once()
        self.assertNotIn(thread, sessions.active)
        sessions.send(thread, {"message": "Continue my task"})
        connection = sessions.antigravity.connections[thread]
        prompts = [params for method, params in connection.calls if method == "session/prompt"]
        self.assertEqual(len(prompts), 1)
        self.assertIn(msg["body"], prompts[0]["prompt"][-1]["text"])
        self.assertIn('"classification": "deferred"', prompts[0]["prompt"][-1]["text"])

    def test_provider_delivery_retries_unacknowledged_deferred_on_next_actual_turn(self):
        msg = self.send()
        delivery = MessageDelivery(self.store)
        batch = delivery.prepare("recipient", "turn1")
        self.assertEqual(batch["ids"], [msg["id"]])
        delivery.attempted("turn1", batch)
        delivery.submitted("recipient", "turn1", batch)
        self.assertIsNone(delivery.prepare("recipient", "turn1"))
        self.assertEqual(delivery.prepare("recipient", "turn2")["ids"], [msg["id"]])
        self.assertEqual(self.store.pending_wake_message_ids("recipient"), [])
        self.store.acknowledge("recipient", msg["id"])
        self.assertIsNone(delivery.prepare("recipient", "turn2"))
