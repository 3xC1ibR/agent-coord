from __future__ import annotations

import copy
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from test_codex_app_server import BrowserSessions, CoordinationStore, FakeCodex, PLUGIN_SCRIPTS
from test_browser_providers import claude_factory
from agent_coord.app_control import AppControl, AppControlWorker
from agent_coord.event_subscriptions import EventSubscriptions
from agent_coord.hook import handle
from agent_coord.message_timeline import message_timeline, message_cursor, message_changes
from agent_coord.store import CoordinationError
from agent_coord.thread_control import ThreadControl
from agent_coord.ui import make_ui_server


class AppControlTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = CoordinationStore(self.root / "state.sqlite3")
        self.store.register(session_id="dispatcher", client="codex", cwd=str(self.root))
        self.sessions, self.worker = self.runtime()
        self.control = self.worker.control

    def runtime(self, cwd=None):
        sessions = BrowserSessions(self.store, str(cwd or self.root), rpc_factory=FakeCodex, claude_factory=claude_factory)
        self.addCleanup(sessions.close)
        worker = AppControlWorker(sessions)
        self.addCleanup(worker.close)
        return sessions, worker

    def request(self, client="codex", **values):
        body = dict(cwd=str(self.root), client=client, name="Deployment specialist", prompt="Explain the release process.", yolo=True)
        body.update(values)
        return self.control.request("create", "dispatcher", body)

    def create(self, client="codex", **values):
        request = self.request(client, **values)
        self.worker.process_once()
        result = self.control.status(request["request_id"])
        self.assertEqual(result["status"], "completed", result)
        return result["thread_id"]

    def cli(self, *arguments, expected=0, stdin=None):
        return self.coord_cli("thread", *arguments, expected=expected, stdin=stdin)

    def coord_cli(self, *arguments, session_id="dispatcher", expected=0, stdin=None):
        env = dict(os.environ, AGENT_COORD_SESSION_ID=session_id)
        result = subprocess.run([str(PLUGIN_SCRIPTS / "agent-coord"), "--db", str(self.store.database_path),
                                 *arguments], cwd=self.root, env=env,
                                input=stdin, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, expected, result.stderr)
        return json.loads(result.stdout if expected == 0 else result.stderr)

    def finish(self, thread_id, text="Direct answer"):
        if self.control.settings(thread_id)["client"] == "claude":
            connection = self.sessions.claude.connections[thread_id]
            connection.callback({"type": "system", "subtype": "init"})
            connection.callback({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})
            connection.callback({"type": "result", "subtype": "success"})
        else:
            turn = self.sessions.rpc.threads[thread_id]["turns"][-1]
            turn["items"].append({"type": "agentMessage", "id": "answer-" + turn["id"], "text": text})
            turn["status"] = "completed"
            self.sessions._event({"method": "turn/completed", "params": {"threadId": thread_id, "turn": copy.deepcopy(turn)}})

    def deliver(self, thread_id):
        with patch.dict(os.environ, {"AGENT_COORD_CLIENT": self.control.settings(thread_id)["client"]}):
            return handle({"hook_event_name": "UserPromptSubmit", "session_id": thread_id, "cwd": str(self.root),
                           "prompt": "Check your Agent Coord inbox."}, self.store)

    def test_both_providers_answer_directly_then_accept_revision_and_later_dispatch(self):
        for client in ("codex", "claude"):
            with self.subTest(client=client):
                thread = self.create(client)
                self.sessions.inbox_wake.process_once()
                context = self.deliver(thread)
                self.assertIn("Answer the user directly", str(context))
                self.assertIn("reply_required=false", str(context))
                self.finish(thread)
                detail = self.sessions.read(thread)
                self.assertTrue(detail["work_thread"]["browser_session"])
                self.assertEqual(detail["work_thread"]["original_request"], "Explain the release process.")
                self.assertTrue(any(i.get("text") == "Direct answer" for t in detail["thread"]["turns"] for i in t["items"]))
                self.assertFalse(self.store.inbox("dispatcher", mark_delivered=False))
                self.store.begin_work(session_id=thread, scopes=["src/**"])
                self.sessions.send(thread, {"message": "Revise the implementation."})
                self.finish(thread, "Revision finished")
                self.store.end_work(thread)
                self.store.send_message(sender_session_id="dispatcher", recipient_session_id=thread,
                                        body="Revisit that release process.", reply_required=False)
                self.sessions.inbox_wake.process_once()
                self.assertTrue(self.sessions.read(thread)["running"])
                self.assertIn("Revisit", str(self.deliver(thread)))
                self.finish(thread)
                self.assertEqual(self.store.get_session(thread)["write_scope"], [])
                self.assertEqual(len(self.sessions.read(thread)["thread"]["turns"]), 3)
                self.assertEqual(self.store.list_delegations(), [])

    def test_initial_reply_returns_to_creator_and_later_requests_keep_their_own_contract(self):
        for client in ("codex", "claude"):
            with self.subTest(client=client):
                creator = self.sessions.create(dict(cwd=str(self.root), client=client, name="Orchestrator"))["session"]["thread_id"]
                request = self.cli("create", "--from-session", creator, "--client", client,
                                   "--name", "Specialist", "--reply-required", "--wait", "0", "Investigate the release.")
                self.worker.process_once()
                receipt = self.control.status(request["request_id"])
                self.assertEqual(receipt["status"], "completed", receipt)
                self.assertTrue(receipt["result"]["reply_required"])
                thread, initial_id = receipt["thread_id"], receipt["result"]["message_id"]
                initial = self.store.inbox(thread, mark_delivered=False)[0]
                self.assertEqual(initial["id"], initial_id)
                self.assertTrue(initial["reply_required"])
                self.assertNotIn("Answer the user directly", initial["body"])
                self.assertNotIn("does not require a reply", initial["body"])
                self.sessions.inbox_wake.process_once()
                context = str(self.deliver(thread))
                self.assertIn("reply_required=true", context)
                self.assertIn("agent-coord reply --message-id", context)
                self.finish(thread, "Findings available")
                self.assertFalse(self.store.inbox(creator, mark_delivered=False))

                reply = self.coord_cli("reply", "--message-id", str(initial_id),
                                       "Result: reviewed release artifacts; simulated validation passed.", session_id=thread)
                self.assertEqual(reply["in_reply_to"], initial_id)
                self.assertEqual(reply["thread_id"], initial["thread_id"])
                self.assertFalse(reply["reply_required"])
                self.sessions.inbox_wake.process_once()
                self.assertTrue(self.sessions.read(creator)["running"])
                self.assertIn("reviewed release artifacts", str(self.deliver(creator)))
                self.finish(creator, "Integrated result")

                # A direct revision can run while new coordination waits for idle.
                self.sessions.send(thread, {"message": "Revise the findings for me."})
                later = self.store.send_message(sender_session_id=creator, recipient_session_id=thread,
                                                body="Check the new evidence and return a revision.", reply_required=True)
                self.sessions.inbox_wake.process_once()
                self.assertEqual(len(self.sessions.read(thread)["thread"]["turns"]), 2)
                self.finish(thread, "Direct revision finished")
                self.sessions.inbox_wake.process_once()
                self.assertIn("new evidence", str(self.deliver(thread)))
                self.finish(thread, "Evidence reviewed")
                revision = self.coord_cli("reply", "--message-id", str(later["id"]), "Revised result", session_id=thread)
                self.assertEqual(revision["in_reply_to"], later["id"])
                self.assertFalse(revision["reply_required"])
                self.sessions.inbox_wake.process_once()
                self.assertIn("Revised result", str(self.deliver(creator)))
                self.finish(creator)

                self.store.send_message(sender_session_id=creator, recipient_session_id=thread,
                                        body="Answer this later question directly for the user.", reply_required=False)
                self.sessions.inbox_wake.process_once()
                context = str(self.deliver(thread))
                self.assertIn("reply_required=false", context)
                self.assertNotIn("Return the requested result to the creating agent", context)
                self.finish(thread, "Later direct answer")
                self.assertFalse(self.store.inbox(creator, mark_delivered=False))
                self.assertEqual(len(self.sessions.read(thread)["thread"]["turns"]), 4)

    def test_creator_projection_is_independent_of_provider_reply_contract_and_reuse(self):
        direct = self.sessions.create(dict(cwd=str(self.root), name="User thread"))["session"]["thread_id"]
        self.assertIsNone(self.sessions.work_thread(direct)["created_by_thread_id"])
        self.assertIsNone(self.sessions.work_thread(direct)["creation_mode"])
        for client in ("codex", "claude"):
            for reply_required in (False, True):
                with self.subTest(client=client, reply_required=reply_required):
                    child = self.create(client, reply_required=reply_required)
                    self.store.send_message(sender_session_id=direct, recipient_session_id=child,
                                            body="Reuse your research", reply_required=True)
                    detail = self.sessions.work_thread(child)
                    self.assertEqual(detail["created_by_thread_id"], "dispatcher")
                    self.assertEqual(detail["created_by"]["thread_id"], "dispatcher")
                    self.assertTrue(detail["created_by"]["available"])
                    self.assertIsNone(detail["creation_mode"])
        with patch.object(self.sessions, "_app_agent_creators", wraps=self.sessions._app_agent_creators) as projection:
            listed = self.sessions.list_work_threads()
        self.assertEqual(projection.call_count, 1)
        self.assertEqual(sum(t["created_by_thread_id"] == "dispatcher" for t in listed), 4)
        self.store.threads.update("dispatcher", attention="archived")
        self.assertEqual(sum(t["created_by_thread_id"] == "dispatcher"
                             for t in self.sessions.list_work_threads()), 4)

    def test_identified_creation_keeps_origin_when_delivery_is_uncertain(self):
        request = self.request(mode="orchestration")
        self.assertTrue(self.control.claim(request["request_id"], "test-runtime"))
        child = self.sessions.create(dict(cwd=str(self.root), name="Identified agent"))["session"]["thread_id"]
        self.control.identified(request["request_id"], child)
        self.control.complete(request["request_id"], error="Delivery interrupted", uncertain=True)
        self.assertEqual(self.sessions.work_thread(child)["created_by_thread_id"], "dispatcher")
        self.assertEqual(self.sessions.work_thread(child)["creation_mode"], "orchestration")

    def test_orchestration_mode_survives_cli_retry_restart_and_reuse_without_changing_reply_contract(self):
        for client in ("codex", "claude"):
            for reply_required in (False, True):
                with self.subTest(client=client, reply_required=reply_required):
                    args = ("create", "--client", client, "--name", "Research", "--mode", "orchestration",
                            "--reply-required" if reply_required else "--no-reply-required",
                            "--request-id", f"mode-{client}-{reply_required}", "--wait", "0", "Research this task.")
                    queued = self.cli(*args)
                    with patch.object(self.sessions, "create", wraps=self.sessions.create) as create:
                        self.worker.process_once()
                    self.assertNotIn("mode", create.call_args.args[0])
                    receipt = self.cli(*args)
                    self.assertEqual(receipt["status"], "completed")
                    self.assertEqual(receipt["result"]["mode"], "orchestration")
                    self.assertEqual(receipt["result"]["reply_required"], reply_required)
                    child = receipt["thread_id"]
                    self.assertEqual(self.sessions.work_thread(child)["creation_mode"], "orchestration")
                    self.store.send_message(sender_session_id="dispatcher", recipient_session_id=child,
                                            body="Answer this later request directly", reply_required=False)
                    reopened, _ = self.runtime()
                    projected = {t["thread_id"]: t for t in reopened.list_work_threads()}[child]
                    self.assertEqual(projected["creation_mode"], "orchestration")
                    self.assertEqual(projected["created_by_thread_id"], "dispatcher")
                    control = AppControl(CoordinationStore(self.store.database_path))
                    with self.store._connection() as db:
                        payload = json.loads(db.execute("SELECT payload_json FROM app_control_requests WHERE request_id = ?",
                                                        (queued["request_id"],)).fetchone()[0])
                    payload.pop("mode")
                    with self.assertRaisesRegex(CoordinationError, "different app request"):
                        control.request("create", "dispatcher", payload, request_id=queued["request_id"])
                    self.assertEqual(len(self.store.inbox(child, mark_delivered=False)), 2)

    def test_creation_mode_validation_and_omission_preserve_legacy_retries(self):
        for value in ("", "dispatch", "grouped", 0, False, [], {}):
            with self.subTest(value=value), self.assertRaisesRegex(CoordinationError, "Creation mode"):
                self.request(mode=value)
        self.assertEqual(self.control.pending(), [])
        self.assertFalse(self.sessions.rpc.calls)
        payload = dict(cwd=str(self.root), name="Agent", prompt="Question", client="codex")
        original = self.control.request("create", "dispatcher", payload, request_id="legacy-mode")
        self.assertEqual(self.control.request("create", "dispatcher", {**payload, "mode": None},
                                             request_id="legacy-mode"), original)
        self.worker.process_once()
        receipt = self.control.status(original["request_id"])
        self.assertIsNone(receipt["result"]["mode"])
        self.assertIsNone(self.sessions.work_thread(receipt["thread_id"])["creation_mode"])

    def test_creator_outside_workspace_remains_labelled_but_unavailable(self):
        other = self.root / "other"
        other.mkdir()
        child = self.create(cwd=str(other))
        restricted, _ = self.runtime(cwd=other)
        detail = restricted.work_thread(child)
        self.assertEqual(detail["created_by_thread_id"], "dispatcher")
        self.assertFalse(detail["created_by"]["available"])

    def test_cli_creation_is_queued_then_confirmed_with_identity_settings_and_link(self):
        receipt = self.cli("create", "--name", "Research specialist", "--yolo", "--model", "available-model",
                           "--effort", "high", "--request-id", "research-1", "--wait", "0", "-", stdin="Question\nwith literal $HOME and `text`.")
        self.assertEqual(receipt["status"], "queued")
        self.assertIsNone(receipt["thread_id"])
        self.worker.process_once()
        receipt = self.cli("request-status", "--request-id", "research-1")
        self.assertEqual(receipt["status"], "completed")
        record = receipt["result"]
        self.assertFalse(record["reply_required"])
        self.assertEqual((record["model"], record["effort"], record["yolo"]), ("available-model", "high", True))
        self.assertEqual(record["session_id"], receipt["thread_id"])
        self.assertTrue(record["url"].startswith("agentcoord://thread/" + record["thread_id"]))
        self.assertIn("literal $HOME", self.store.threads.get(record["thread_id"])["original_request"])
        self.assertEqual(self.cli("settings", "--session-id", record["thread_id"])["model"], "available-model")

    def test_cli_explicit_no_reply_matches_default_request_on_retry(self):
        args = ("create", "--name", "Direct specialist", "--request-id", "direct", "--wait", "0")
        self.cli(*args, "Question")
        self.cli(*args, "--no-reply-required", "Question")
        self.worker.process_once()
        receipt = self.control.status("direct")
        self.assertFalse(receipt["result"]["reply_required"])
        initial = self.store.inbox(receipt["thread_id"], mark_delivered=False)
        self.assertEqual(len(initial), 1)
        self.assertFalse(initial[0]["reply_required"])
        self.assertIn("Answer the user directly", initial[0]["body"])

    def test_default_subscription_returns_result_without_worker_reply_for_both_providers(self):
        for client in ("codex", "claude"):
            with self.subTest(client=client):
                creator = self.sessions.create(dict(client=client, name="Creator"))["session"]["thread_id"]
                queued = self.cli("create", "--from-session", creator, "--client", client,
                                  "--name", "Worker", "--mode", "orchestration", "--wait", "0", "Do the work.")
                self.worker.process_once()
                receipt = self.control.status(queued["request_id"])["result"]
                child = receipt["session_id"]
                self.assertTrue(receipt["subscribe"])
                self.assertFalse(receipt["reply_required"])
                self.assertTrue(receipt["subscription_id"])
                self.sessions.inbox_wake._dispatch(child)
                self.deliver(child)
                checkpoint = self.store.threads.checkpoint(child, {
                    "phase": "finished", "summary": "Implemented and checked.", "next_step": None,
                    "context_lifecycle": {
                        "assignment_message_id": receipt["message_id"], "recommendation": "reuse",
                        "reason": "The debugging constraints remain useful.", "reuse_for": ["Fixes"],
                        "fresh_for": ["Independent review"], "handover": {
                            "confirmed_facts": ["Tests passed."], "constraints": [],
                            "tried_and_failed": [], "open_questions": []}}})
                self.finish(child)
                # Late bookkeeping in the same turn must not replace the frozen report.
                self.store.threads.checkpoint(child, {"summary": "Late bookkeeping.", "context_lifecycle": None})
                # A later user turn must not replace the saved completion result.
                self.sessions.send(child, {"message": "A separate follow-up."})
                self.store.threads.checkpoint(child, {"phase": "discussion", "summary": "Another task."})
                self.sessions.event_subscriptions.process_once()
                messages = self.store.inbox(creator, mark_delivered=False)
                self.assertEqual(len(messages), 1)
                self.assertFalse(messages[0]["reply_required"])
                outcome = json.loads(messages[0]["body"])["outcomes"][0]
                self.assertEqual(outcome["assignment_message_id"], receipt["message_id"])
                self.assertEqual(outcome["checkpoint"]["id"], checkpoint["checkpoint"]["id"])
                self.assertEqual(outcome["context_lifecycle"]["recommendation"], "reuse")
                self.assertEqual(outcome["checkpoint"]["summary"], "Implemented and checked.")
                self.finish(child, "Follow-up done")
                self.sessions.event_subscriptions.process_once()
                self.assertEqual(len(self.store.inbox(creator, mark_delivered=False)), 1)
                self.sessions.inbox_wake._dispatch(creator)
                self.assertTrue(self.sessions.read(creator)["running"])
                self.finish(creator)

    def test_subscription_opt_out_preserves_result_and_does_not_wake_creator(self):
        request = self.cli("create", "--name", "Independent", "--no-subscribe", "--wait", "0", "Do the work.")
        self.worker.process_once()
        receipt = self.control.status(request["request_id"])["result"]
        self.assertFalse(receipt["subscribe"])
        self.assertIsNone(receipt["subscription_id"])
        child = receipt["session_id"]
        self.sessions.inbox_wake._dispatch(child)
        self.deliver(child)
        self.store.threads.checkpoint(child, {"phase": "finished", "summary": "Saved result."})
        self.finish(child)
        self.sessions.event_subscriptions.process_once()
        self.assertEqual(self.store.inbox("dispatcher", mark_delivered=False), [])
        self.assertEqual(self.store.threads.get(child)["checkpoint"]["summary"], "Saved result.")
        self.assertEqual(self.sessions.event_subscriptions.list("dispatcher"), [])

    def test_subscription_choice_is_validated_and_stable_across_retries(self):
        for value in (None, 0, 1, "false", [], {}):
            with self.subTest(value=value), self.assertRaisesRegex(CoordinationError, "subscribe must be a boolean"):
                self.request(subscribe=value)
        args = ("create", "--name", "Worker", "--request-id", "subscription-retry", "--wait", "0")
        self.cli(*args, "Work")
        self.cli(*args, "--subscribe", "Work")
        self.cli(*args, "--no-subscribe", "Work", expected=2)
        self.worker.process_once()
        result = self.control.status("subscription-retry")["result"]
        self.assertTrue(result["subscribe"])
        self.cli(*args, "--subscribe", "Work")
        self.assertEqual(len(self.sessions.event_subscriptions.list("dispatcher")), 1)

    def test_initial_message_and_subscription_roll_back_together(self):
        request = self.request()
        register = self.worker.subscriptions.create_for_message
        def fail_after_registration(db, message):
            register(db, message)
            raise RuntimeError("Subscription storage failed")
        with patch.object(self.worker.subscriptions, "create_for_message", side_effect=fail_after_registration):
            self.worker.process_once()
        receipt = self.control.status(request["request_id"])
        self.assertEqual(receipt["status"], "uncertain")
        self.assertEqual(self.store.inbox(receipt["thread_id"], mark_delivered=False), [])
        self.assertEqual(self.sessions.event_subscriptions.list("dispatcher"), [])
        self.assertIn("Subscription storage failed", self.store.inbox("dispatcher", mark_delivered=False)[0]["body"])

    def test_subscription_registered_before_a_fast_worker_can_finish(self):
        request = self.request()
        complete = self.control.complete
        def finish_before_receipt(request_id, **kwargs):
            child = kwargs["result"]["session_id"]
            self.sessions.inbox_wake._dispatch(child)
            self.deliver(child)
            self.finish(child)
            self.sessions.event_subscriptions.process_once()
            complete(request_id, **kwargs)
        with patch.object(self.control, "complete", side_effect=finish_before_receipt):
            self.worker.process_once()
        receipt = self.control.status(request["request_id"])
        restarted = EventSubscriptions(CoordinationStore(self.store.database_path))
        restarted.process_once()
        result = restarted.get("dispatcher", receipt["result"]["subscription_id"])
        self.assertEqual(result["state"], "fired")
        self.assertEqual(len(self.store.inbox("dispatcher", mark_delivered=False)), 1)
        outcome = result["items"][0]["outcome"]
        self.assertIsNone(outcome["checkpoint"])
        self.assertIsNone(outcome["context_lifecycle"])

    def test_interrupted_initial_turn_notifies_once_and_later_turns_do_not(self):
        child = self.create()
        self.sessions.inbox_wake._dispatch(child)
        # Wake evidence binds the turn even when prompt-hook delivery never ran.
        turn = self.sessions.rpc.threads[child]["turns"][-1]
        self.store.threads.finish_turn(child, turn_id=turn["id"], status="interrupted")
        self.sessions.event_subscriptions.process_once()
        self.sessions.event_subscriptions.process_once()
        messages = self.store.inbox("dispatcher", mark_delivered=False)
        self.assertEqual(len(messages), 1)
        self.assertEqual(json.loads(messages[0]["body"])["outcomes"][0]["state"], "interrupted")

    def test_initial_reply_choice_is_validated_before_queueing(self):
        for value in (None, 0, 1, "false", "true", [], {}):
            with self.subTest(value=value), self.assertRaisesRegex(CoordinationError, "reply_required must be a boolean"):
                self.request(reply_required=value)
        self.assertEqual(self.control.pending(), [])
        self.assertFalse(self.sessions.rpc.calls)

    def test_reply_contract_survives_restart_and_cannot_change_under_same_key(self):
        payload = dict(cwd=str(self.root), name="Agent", prompt="Question", client="codex", reply_required=True)
        self.control.request("create", "dispatcher", payload, request_id="reply-contract")
        control = AppControl(CoordinationStore(self.store.database_path))
        control.request("create", "dispatcher", payload, request_id="reply-contract")
        self.worker.process_once()
        completed = control.request("create", "dispatcher", payload, request_id="reply-contract")
        self.assertTrue(completed["result"]["reply_required"])
        for changed in ({**payload, "reply_required": False}, {k: v for k, v in payload.items() if k != "reply_required"}):
            with self.assertRaisesRegex(CoordinationError, "different app request"):
                control.request("create", "dispatcher", changed, request_id="reply-contract")
        self.worker.process_once()
        messages = self.store.inbox(completed["thread_id"], mark_delivered=False)
        self.assertEqual(len(messages), 1)
        self.assertTrue(messages[0]["reply_required"])
        self.assertEqual(sum(m == "thread/start" for m, _ in self.sessions.rpc.calls), 1)

    def test_duplicate_request_survives_restart_without_duplicate_agent_or_message(self):
        payload = dict(cwd=str(self.root), name="Agent", prompt="Question", client="codex")
        first = self.control.request("create", "dispatcher", payload, request_id="stable")
        self.worker.process_once()
        control = AppControl(CoordinationStore(self.store.database_path))
        repeated = control.request("create", "dispatcher", payload, request_id="stable")
        self.assertEqual(control.request("create", "dispatcher", {**payload, "reply_required": False},
                                        request_id="stable"), repeated)
        self.worker.process_once()
        self.assertEqual(first["request_id"], repeated["request_id"])
        self.assertEqual(repeated["status"], "completed")
        self.assertEqual(sum(m == "thread/start" for m, _ in self.sessions.rpc.calls), 1)
        self.assertEqual(len(self.store.inbox(repeated["thread_id"], mark_delivered=False)), 1)
        with self.assertRaisesRegex(CoordinationError, "different app request"):
            control.request("create", "dispatcher", {**payload, "prompt": "Different"}, request_id="stable")

    def test_creation_card_uses_exact_confirmed_message_and_survives_retry(self):
        for client in ("codex", "claude"):
            with self.subTest(client=client):
                payload = dict(cwd=str(self.root), client=client, name="Specialist", prompt="Do the task.")
                request = self.control.request("create", "dispatcher", payload)
                self.worker.process_once()
                receipt = self.control.status(request["request_id"])
                thread = receipt["thread_id"]
                message_id = receipt["result"]["message_id"]
                initial = next(m for m in message_timeline(self.store, "dispatcher") if m["id"] == message_id)
                self.assertEqual(initial["creation_request_id"], request["request_id"])
                self.assertIsNone(message_timeline(self.store, thread)[0]["creation_request_id"])
                later = self.store.send_message(sender_session_id="dispatcher", recipient_session_id=thread,
                                                body="A later request", reply_required=False)
                cursor = message_cursor(self.store)
                self.control.request("create", "dispatcher", payload, request_id=request["request_id"])
                self.worker.process_once()
                self.control.complete(request["request_id"], result=receipt["result"])
                reopened = CoordinationStore(self.store.database_path)
                timeline = message_timeline(reopened, "dispatcher")
                self.assertEqual(sum(m["id"] == message_id for m in timeline), 1)
                self.assertIsNone(next(m for m in timeline if m["id"] == later["id"])["creation_request_id"])
                self.assertEqual(message_cursor(self.store), cursor)

    def test_creation_confirmation_refreshes_existing_card_once(self):
        request = self.request()
        self.control.claim(request["request_id"], self.worker.owner)
        self.store.register(session_id="created", client="codex", cwd=str(self.root))
        self.control.identified(request["request_id"], "created")
        message = self.store.send_message(sender_session_id="dispatcher", recipient_session_id="created",
                                          body="Initial request", reply_required=False)
        cursor = message_cursor(self.store)
        self.assertIsNone(message_timeline(self.store, "dispatcher")[0]["creation_request_id"])
        self.control.complete(request["request_id"], result={"message_id": message["id"]})
        latest, changes = message_changes(self.store, cursor)
        self.assertGreater(latest, cursor)
        self.assertEqual({e["params"]["threadId"] for e in changes}, {"dispatcher", "created"})
        self.assertEqual(message_timeline(self.store, "dispatcher")[0]["creation_request_id"], request["request_id"])
        self.control.complete(request["request_id"], result={"message_id": message["id"]})
        self.assertEqual(message_cursor(self.store), latest)

    def test_model_discovery_uses_requested_provider_without_starting_a_conversation(self):
        for client in ("codex", "claude"):
            receipt = self.cli("models", "--client", client, "--wait", "0")
            self.worker.process_once()
            result = self.control.status(receipt["request_id"])
            self.assertEqual(result["status"], "completed")
            self.assertTrue(result["result"]["models"][0]["supportedReasoningEfforts"])
        self.assertEqual(self.sessions.list_sessions(), [])

    def test_settings_wait_for_idle_and_user_queue_then_apply_together(self):
        thread = self.create()
        self.sessions.inbox_wake.process_once()
        self.deliver(thread)
        request = self.cli("settings", "--session-id", thread, "--model", "other-model", "--effort", "low", "--default-permissions", "--wait", "0")
        self.worker.process_once()
        self.assertEqual(self.control.status(request["request_id"])["status"], "queued")
        self.sessions.queue.enqueue(thread, {"message": "User revision first"})
        self.sessions.queue.pause(thread, "Review")
        self.finish(thread)
        self.worker.process_once()
        self.assertEqual(self.control.status(request["request_id"])["status"], "queued")
        queued = self.sessions.queue.list(thread)[0]
        self.sessions.queue.change(thread, {"action": "cancel", "id": queued["id"]})
        self.worker.process_once()
        result = self.control.status(request["request_id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual((result["result"]["model"], result["result"]["effort"], result["result"]["yolo"]), ("other-model", "low", False))

    def test_invalid_settings_do_not_partially_change_permissions(self):
        thread = self.create()
        request = self.control.request("settings", "dispatcher", {"model": "missing", "yolo": False}, thread_id=thread)
        self.worker.process_once()
        self.assertEqual(self.control.status(request["request_id"])["status"], "failed")
        self.assertTrue(self.control.settings(thread)["yolo"])

    def test_user_settings_change_supersedes_pending_agent_settings(self):
        thread = self.create()
        request = self.control.request("settings", "dispatcher", {"effort": "high"}, thread_id=thread)
        self.sessions.update(thread, {"model": "other-model"})
        self.worker.process_once()
        status = self.control.status(request["request_id"])
        self.assertEqual(status["status"], "failed")
        self.assertIn("settings changed", status["error"])
        self.assertEqual(self.control.settings(thread)["model"], "other-model")

    def test_closed_and_pending_close_threads_reject_settings(self):
        for closed in (False, True):
            thread = self.create()
            request = self.control.request("settings", "dispatcher", {"yolo": False}, thread_id=thread)
            if closed:
                self.sessions.close_work_thread(thread)
            else:
                ThreadControl(self.store).request_close(thread)
            self.worker.process_once()
            self.assertEqual(self.control.status(request["request_id"])["status"], "failed")
            self.assertTrue(self.control.settings(thread)["yolo"])

    def test_restart_preserves_context_settings_and_routes_to_same_agent(self):
        for client in ("codex", "claude"):
            thread = self.create(client)
            self.sessions.inbox_wake.process_once()
            self.deliver(thread)
            self.finish(thread, "Remember this release context")
            old = self.sessions
            native = copy.deepcopy(old.rpc.threads)
            native_settings = copy.deepcopy(old.rpc.settings)
            old.close()
            self.sessions, self.worker = self.runtime()
            self.control = self.worker.control
            self.sessions.rpc.threads, self.sessions.rpc.settings = native, native_settings
            self.store.send_message(sender_session_id="dispatcher", recipient_session_id=thread,
                                    body="Use the earlier release context", reply_required=False)
            self.sessions.inbox_wake.process_once()
            self.assertIn("earlier release", str(self.deliver(thread)))
            detail = self.sessions.read(thread)
            self.assertTrue(detail["session"]["yolo"])
            self.assertEqual(len(detail["thread"]["turns"]), 2)
            self.assertTrue(any(i.get("text") == "Remember this release context" for t in detail["thread"]["turns"] for i in t["items"]))
            if client == "claude":
                self.assertTrue(self.sessions.claude.connections[thread].options["resume"])
            self.finish(thread)

    def test_invalid_creation_fails_before_provider_and_no_permissions_are_inherited(self):
        request = self.request(model="missing")
        self.worker.process_once()
        self.assertEqual(self.control.status(request["request_id"])["status"], "failed")
        self.assertFalse(any(m == "thread/start" for m, _ in self.sessions.rpc.calls))
        thread = self.create(yolo=False)
        self.assertFalse(self.control.settings(thread)["yolo"])

    def test_provider_creation_timeout_is_uncertain_and_never_replayed(self):
        request = self.request()
        original = self.sessions.rpc.request
        def lost_response(method, params=None):
            result = original(method, params)
            if method == "thread/start":
                raise CoordinationError("Response lost")
            return result
        with patch.object(self.sessions.rpc, "request", side_effect=lost_response):
            self.worker.process_once()
        status = self.control.status(request["request_id"])
        self.assertEqual(status["status"], "uncertain")
        self.worker.process_once()
        self.assertEqual(sum(m == "thread/start" for m, _ in self.sessions.rpc.calls), 1)

    def test_identity_is_saved_before_local_creation_failure(self):
        request = self.request()
        with patch.object(self.sessions, "_capture_settings", side_effect=CoordinationError("Local write failed")):
            self.worker.process_once()
        status = self.control.status(request["request_id"])
        self.assertEqual(status["status"], "uncertain")
        self.assertEqual(status["thread_id"], "thread-1")
        self.worker.process_once()
        self.assertEqual(sum(m == "thread/start" for m, _ in self.sessions.rpc.calls), 1)

    def test_abandoned_claim_is_uncertain_and_live_heartbeat_prevents_false_failure(self):
        request = self.request()
        self.control.claim(request["request_id"], self.worker.owner)
        with self.store._connection() as db:
            db.execute("UPDATE app_control_requests SET updated_at = 0")
        self.worker._heartbeat()
        self.assertEqual(self.control.status(request["request_id"])["status"], "running")
        self.worker.close()
        self.assertEqual(self.control.status(request["request_id"])["status"], "uncertain")
        self.worker.process_once()
        self.assertFalse(self.sessions.rpc.calls)

    def test_atomic_claim_and_workspace_and_runtime_ownership(self):
        request = self.request()
        other, worker = self.runtime(self.root / "other")
        worker.process_once()
        self.assertFalse(other.rpc.calls)
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(lambda owner: self.control.claim(request["request_id"], owner), ("one", "two")))
        self.assertEqual(sorted(claims), [False, True])
        thread = self.create()
        request = self.control.request("settings", "dispatcher", {"yolo": False}, thread_id=thread)
        other, worker = self.runtime()
        worker.process_once()
        self.assertEqual(self.control.status(request["request_id"])["status"], "queued")
        self.assertFalse(other.rpc.calls)
        self.worker.process_once()
        self.assertEqual(self.control.status(request["request_id"])["status"], "completed")

    def test_cancel_only_before_execution_and_invalid_wait_does_not_queue(self):
        request = self.request()
        self.assertEqual(self.cli("cancel-request", "--request-id", request["request_id"])["status"], "cancelled")
        self.worker.process_once()
        self.assertFalse(self.sessions.rpc.calls)
        other = self.request()
        self.control.claim(other["request_id"], "owner")
        with self.assertRaisesRegex(CoordinationError, "already executing"):
            self.control.cancel(other["request_id"])
        before = len(self.control.pending())
        self.cli("create", "--name", "Invalid", "--wait", "nan", "question", expected=2)
        self.assertEqual(len(self.control.pending()), before)

    def test_server_services_cli_without_browser_navigation(self):
        server = make_ui_server(self.store, port=0, cwd=str(self.root), browser_sessions=self.sessions)
        self.addCleanup(server.server_close)
        result = self.cli("create", "--name", "App agent", "--client", "claude", "--wait", "5", "Explain this project.")
        self.assertEqual(result["status"], "completed", result)
        self.assertTrue(self.sessions.work_thread(result["thread_id"])["browser_session"])


if __name__ == "__main__":
    unittest.main()
