from __future__ import annotations

import copy
import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from test_codex_app_server import BrowserSessions, CoordinationStore, FakeCodex
from test_browser_providers import claude_factory
from agent_coord.app_control import AppControlWorker
from agent_coord.browser_wake import BrowserInboxWake, WAKE_PROMPT
from agent_coord.hook import _actionable_context
from agent_coord.store import CoordinationError
from agent_coord.thread_control import ThreadControl


class BrowserWakeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.store = CoordinationStore(self.root / "state.sqlite3")
        self.sessions = self.make_sessions()
        self.store.register(session_id="sender", client="claude", cwd=str(self.root))

    def make_sessions(self, cwd=None):
        sessions = BrowserSessions(self.store, str(cwd or self.root), rpc_factory=FakeCodex,
                                   claude_factory=claude_factory)
        self.addCleanup(sessions.close)
        return sessions

    def create(self, client="codex"):
        thread_id = self.sessions.create({"client": client})["session"]["thread_id"]
        self.assertTrue(self.sessions.persistence.flush(key=thread_id))
        return thread_id

    def message(self, recipient, classification="action_required", **values):
        return self.store.send_message(sender_session_id="sender", recipient_session_id=recipient,
                                       body="Please report your findings", classification=classification, **values)

    def finish(self, thread_id, status="completed"):
        if self.sessions._record(thread_id)["client"] == "claude":
            self.sessions.claude.connections[thread_id].callback({"type": "result", "subtype": "success"})
        else:
            turn = self.sessions.rpc.threads[thread_id]["turns"][-1]
            turn["status"] = status
            self.sessions._event({"method": "turn/completed", "params": {
                "threadId": thread_id, "turn": copy.deepcopy(turn)}})
        self.assertTrue(self.sessions.persistence.flush(key=thread_id))

    def attempts(self):
        with self.store._connection() as db:
            return [dict(row) for row in db.execute("SELECT * FROM message_wake_attempts ORDER BY message_id")]

    def turns(self, thread_id):
        return self.sessions.read(thread_id)["thread"]["turns"]

    def test_codex_and_claude_coalesce_actionable_messages_without_delivering_them(self):
        for client in ("codex", "claude"):
            with self.subTest(client=client):
                thread = self.create(client)
                first, second = self.message(thread), self.message(thread, reply_required=False)
                self.sessions.inbox_wake.process_once()
                self.sessions.inbox_wake.process_once()
                turns = self.turns(thread)
                self.assertEqual(len(turns), 1)
                self.assertEqual(turns[0]["items"][0]["content"][0]["text"], WAKE_PROMPT)
                rows = [a for a in self.attempts() if a["session_id"] == thread]
                self.assertEqual([a["message_id"] for a in rows], [first["id"], second["id"]])
                self.assertTrue(all(a["outcome"] == "sent" for a in rows))
                cards = self.sessions.read(thread)["coordinationMessages"]
                self.assertTrue(all(card["started_turn"] for card in cards))
                self.assertTrue(all(card["turn_id"] == turns[0]["id"] for card in cards))
                with self.store._connection() as db:
                    self.assertEqual(db.execute("SELECT count(*) FROM messages WHERE recipient_session_id = ? AND delivered_at IS NULL AND acknowledged_at IS NULL", (thread,)).fetchone()[0], 2)
                self.finish(thread)
                self.sessions.inbox_wake.process_once()
                self.assertEqual(len(self.turns(thread)), 1)

    def test_busy_waits_and_new_message_after_wake_gets_a_later_turn(self):
        thread = self.create()
        self.sessions.send(thread, {"message": "User task"})
        self.message(thread)
        self.sessions.inbox_wake.process_once()
        self.assertEqual(self.attempts(), [])
        self.finish(thread)
        self.sessions.inbox_wake.process_once()
        self.message(thread)
        self.sessions.inbox_wake.process_once()
        self.assertEqual(len(self.turns(thread)), 2)
        self.finish(thread)
        self.sessions.inbox_wake.process_once()
        self.assertEqual(len(self.turns(thread)), 3)
        self.assertFalse(any(m == "turn/steer" for m, _ in self.sessions.rpc.calls))

    def test_reply_wakes_requester_and_is_delivered_without_another_reply_request(self):
        for client in ("codex", "claude"):
            with self.subTest(client=client):
                thread = self.create(client)
                original = self.store.send_message(sender_session_id=thread, recipient_session_id="sender",
                                                   body="Please confirm", reply_required=True)
                reply = self.store.reply_message(sender_session_id="sender", message_id=original["id"], body="Received")
                self.sessions.inbox_wake.process_once()
                self.assertEqual(len(self.turns(thread)), 1)
                context = _actionable_context(self.store, thread)
                self.assertIn("Received", context)
                self.assertIn("reply_required=false", context)
                self.assertIn("agent-coord reply --message-id", context)
                self.store.acknowledge(thread, reply["id"])
                self.finish(thread)
                self.sessions.inbox_wake.process_once()
                self.assertEqual(len(self.turns(thread)), 1)
                card = self.sessions.read(thread)["coordinationMessages"][-1]
                self.assertFalse(card["reply_required"])
                self.assertEqual(card["status"], "Delivered to agent")

    def test_informational_closure_and_already_delivered_messages_do_not_wake(self):
        thread = self.create()
        self.message(thread, "informational")
        self.message(thread, "closure")
        self.message(thread)
        self.store.inbox(thread)
        self.sessions.inbox_wake.process_once()
        self.assertEqual(self.turns(thread), [])
        self.assertEqual(self.attempts(), [])

    def test_closure_supersedes_pending_action(self):
        thread = self.create()
        message = self.message(thread)
        self.message(thread, "closure", thread_id=message["thread_id"])
        self.sessions.inbox_wake.process_once()
        self.assertEqual(self.turns(thread), [])

    def test_stop_persists_across_restart_and_user_input_resumes(self):
        for client in ("codex", "claude"):
            with self.subTest(client=client):
                thread = self.create(client)
                self.sessions.send(thread, {"message": "Initial work"})
                self.message(thread)
                self.sessions.interrupt(thread)
                self.message(thread)
                # A second dispatcher sees the durable Stop, even for new messages.
                BrowserInboxWake(self.sessions).process_once()
                self.assertEqual(len(self.turns(thread)), 1)
                self.assertFalse(any(a["session_id"] == thread for a in self.attempts()))
                self.sessions.send(thread, {"message": "Continue"})
                self.finish(thread)
                self.sessions.inbox_wake.process_once()
                self.assertEqual(len(self.turns(thread)), 3)

    def test_failed_turn_does_not_restart_itself(self):
        thread = self.create()
        self.sessions.send(thread, {"message": "Initial work"})
        self.message(thread)
        self.finish(thread, "failed")
        self.sessions.close_work_thread(thread)
        self.message(thread)
        self.sessions.inbox_wake.process_once()
        self.assertEqual(self.store.threads.get(thread)["attention"], "now")
        self.assertEqual(len(self.turns(thread)), 1)

    def test_submission_failure_is_not_retried_even_after_restart(self):
        thread = self.create()
        self.message(thread)
        self.sessions.rpc.fail_turn = True
        self.sessions.inbox_wake.process_once()
        self.assertEqual(self.attempts()[0]["outcome"], "failed")
        self.assertFalse(self.sessions.read(thread)["coordinationMessages"][0]["started_turn"])
        self.sessions.rpc.fail_turn = False
        self.sessions.close_work_thread(thread)
        self.message(thread)
        BrowserInboxWake(self.sessions).process_once()
        self.assertEqual(self.store.threads.get(thread)["attention"], "now")
        self.assertEqual(self.turns(thread), [])
        self.assertEqual(len(self.attempts()), 1)

    def test_transient_pre_submit_lock_retries_after_backoff_without_a_claim(self):
        thread = self.create()
        self.message(thread)
        self.store.clock = lambda: 2000000000.0
        with patch.object(self.sessions, "read", side_effect=sqlite3.OperationalError("database is locked")):
            self.sessions.inbox_wake.process_once()
        self.assertEqual(self.attempts(), [])
        with self.store._connection() as db:
            wake = db.execute("SELECT * FROM browser_inbox_wake WHERE thread_id=?", (thread,)).fetchone()
        self.assertFalse(wake["paused"])
        self.assertEqual(wake["retry_at"], 2000000005.0)
        self.sessions.inbox_wake.process_once()
        self.assertEqual(self.turns(thread), [])
        self.store.clock = lambda: 2000000005.0
        BrowserInboxWake(self.sessions).process_once()
        self.assertEqual(len(self.turns(thread)), 1)
        self.assertEqual(self.attempts()[0]["outcome"], "sent")

    def test_pre_submit_lock_after_claim_is_released_and_retry_cap_survives_restart(self):
        thread = self.create()
        self.message(thread)
        self.wall = 2000000000.0
        self.store.clock = lambda: self.wall
        for delay in (5, 30, None):
            with patch.object(self.sessions.dispatch, "prepare_context", side_effect=sqlite3.OperationalError("database is locked")):
                BrowserInboxWake(self.sessions).process_once()
            self.assertEqual(self.attempts(), [])
            self.assertNotIn(thread, self.sessions.active)
            if delay:
                self.wall += delay
        with self.store._connection() as db:
            wake = db.execute("SELECT * FROM browser_inbox_wake WHERE thread_id=?", (thread,)).fetchone()
        self.assertTrue(wake["paused"])
        self.assertEqual(wake["pause_kind"], "pre_submit")
        BrowserInboxWake(self.sessions).process_once()
        self.assertEqual(self.turns(thread), [])
        view = self.sessions.work_thread(thread)
        self.assertTrue(view["needs_attention"])
        self.assertIn("database is locked", view["attention_action"])

    def test_post_submit_database_lock_remains_uncertain_and_is_never_retried(self):
        thread = self.create()
        self.message(thread)
        request = self.sessions.rpc.request
        def accepted_then_locked(method, params=None):
            result = request(method, params)
            if method == "turn/start":
                raise sqlite3.OperationalError("database is locked")
            return result
        with patch.object(self.sessions.rpc, "request", side_effect=accepted_then_locked):
            self.sessions.inbox_wake.process_once()
        self.finish(thread)
        BrowserInboxWake(self.sessions).process_once()
        self.assertEqual(len(self.turns(thread)), 1)
        self.assertEqual(self.attempts()[0]["outcome"], "failed")
        with self.store._connection() as db:
            self.assertEqual(db.execute("SELECT pause_kind FROM browser_inbox_wake WHERE thread_id=?", (thread,)).fetchone()[0], "unknown")

    def test_authoritative_completed_turn_reconciles_false_failure_but_not_stop_or_uncertain(self):
        for kind in ("failure", "stop", "unknown"):
            with self.subTest(kind=kind):
                thread = self.create()
                self.sessions.send(thread, {"message": "Work"})
                self.finish(thread, "failed")
                self.sessions.rpc.threads[thread]["turns"][-1]["status"] = "completed"
                self.sessions.inbox_wake.pause(thread, "Old failure", kind=kind)
                self.message(thread)
                BrowserInboxWake(self.sessions).process_once()
                self.assertEqual(len(self.turns(thread)), 1)
                with self.store._connection() as db:
                    wake = db.execute("SELECT * FROM browser_inbox_wake WHERE thread_id=?", (thread,)).fetchone()
                self.assertTrue(wake["paused"])
                self.assertEqual(wake["pause_kind"], kind)
                self.assertEqual(wake["reconciled_turn_id"] is not None, kind == "failure")

    def test_creatorless_lead_pause_is_visible_in_inventory_and_preserves_checkpoint(self):
        thread = self.create()
        self.store.threads.checkpoint(thread, {"phase": "orchestrating", "summary": "Coordinating work.",
                                              "next_step": {"actor": "agent", "action": "Await worker"}})
        message = self.message(thread)
        self.sessions.inbox_wake.pause(thread, "Submission outcome unknown")
        view = next(t for t in self.sessions.list_work_threads() if t["thread_id"] == thread)
        self.assertTrue(view["needs_attention"])
        self.assertIn("Submission outcome unknown", view["attention_action"])
        self.assertEqual(view["checkpoint"]["summary"], "Coordinating work.")
        self.assertEqual([m["id"] for m in self.store.inbox(thread, mark_delivered=False)], [message["id"]])

    def test_false_failure_reconciliation_waits_for_late_authoritative_completion(self):
        thread = self.create()
        self.sessions.send(thread, {"message": "Work"})
        self.finish(thread, "failed")
        self.message(thread)
        self.wall = 2000000000.0
        self.store.clock = lambda: self.wall
        self.sessions.inbox_wake.process_once()
        self.assertEqual(len(self.turns(thread)), 1)
        self.sessions.rpc.threads[thread]["turns"][-1]["status"] = "completed"
        self.wall += 5
        self.sessions.inbox_wake.process_once()
        self.assertEqual(len(self.turns(thread)), 1)
        self.assertEqual(self.store.threads.get(thread)["turn_completion"]["status"], "completed")

    def test_corrected_historical_failure_requires_explicit_recovery_and_new_instructions(self):
        worker = AppControlWorker(self.sessions)
        self.addCleanup(worker.close)
        request = worker.control.request("create", "sender", {
            "cwd": str(self.root), "client": "codex", "prompt": "Historical work"})
        worker.process_once()
        thread = worker.control.status(request["request_id"])["thread_id"]
        self.sessions.inbox_wake.process_once()
        self.store.inbox(thread)
        self.finish(thread, "failed")
        turn = self.sessions.rpc.threads[thread]["turns"][-1]
        expected = turn["id"]
        turn["status"] = "completed"
        self.store.threads.update(thread, attention="later")
        stale = self.message(thread)  # External retirement intent is not encoded by this old request.
        self.sessions.inbox_wake.process_once()
        BrowserInboxWake(self.sessions).process_once()
        self.assertEqual(len(self.turns(thread)), 1)
        self.assertEqual(self.store.threads.get(thread)["attention"], "later")
        self.assertFalse(any(a["message_id"] == stale["id"] for a in self.attempts()))
        with self.assertRaisesRegex(CoordinationError, "fresh actionable"):
            self.sessions.inbox_wake.recover(thread, expected, "sender")
        self.store.send_message(sender_session_id="sender", recipient_session_id=thread,
                                body="Inspect old outcomes and do only the newly authorized work",
                                classification="action_required", reply_required=False)
        self.sessions.inbox_wake.process_once()
        self.assertEqual(len(self.turns(thread)), 1)  # A fresh peer message alone is not authorization.
        recovery = worker.control.request("resume", "sender", {"expected_turn_id": expected}, thread_id=thread)
        worker.process_once()
        self.assertEqual(worker.control.status(recovery["request_id"])["status"], "completed")
        self.sessions.inbox_wake.process_once()
        self.assertEqual(len(self.turns(thread)), 2)
        self.store.inbox(thread)
        self.finish(thread)
        self.message(thread)
        self.sessions.inbox_wake.process_once()
        self.assertEqual(len(self.turns(thread)), 3)  # Ordinary future wakes retain their behavior.

    def test_false_failure_reconciliation_resolves_the_old_failure_deadline(self):
        worker = AppControlWorker(self.sessions)
        self.addCleanup(worker.close)
        request = worker.control.request("create", "sender", {
            "cwd": str(self.root), "client": "codex", "prompt": "Inspect results"})
        worker.process_once()
        thread = worker.control.status(request["request_id"])["thread_id"]
        self.sessions.inbox_wake.process_once()
        self.store.inbox(thread)
        self.finish(thread, "failed")
        self.sessions.rpc.threads[thread]["turns"][-1]["status"] = "completed"
        self.message(thread)
        self.sessions.inbox_wake.process_once()
        with self.store._connection() as db:
            followup = db.execute("SELECT * FROM app_agent_followups WHERE thread_id=?", (thread,)).fetchone()
        self.assertEqual(followup["resolution"], "native_completion")
        self.assertIsNotNone(followup["resolved_at"])

    def test_automatic_wake_prompt_explicitly_identifies_runtime_provenance(self):
        self.assertIn("not a human message", WAKE_PROMPT)
        self.assertIn("generated by the runtime", WAKE_PROMPT)

    def test_native_claude_false_disconnect_requires_matching_prompt_and_successful_stop(self):
        for proof in ("complete", "wrong_prompt", "wrong_uuid", "prevented", "tool_still_running", "later_prompt", "partial_tail", "late_tool_result"):
            with self.subTest(proof=proof):
                thread = self.create("claude")
                self.sessions.send(thread, {"message": "Inspect the job results"})
                self.sessions.claude._finish(thread, "failed", "Claude Code disconnected. Send again to resume.")
                self.assertTrue(self.sessions.persistence.flush(key=thread))
                self.message(thread)
                current = self.store.threads.get(thread)
                user_id = self.turns(thread)[-1]["items"][0]["id"]
                stamp = datetime.fromtimestamp(current["turn_started_at"] + 1, timezone.utc).isoformat()
                entries = [
                    {"type": "user", "uuid": user_id if proof != "wrong_uuid" else "another-input", "message": {"content": "Inspect the job results" if proof != "wrong_prompt" else "An older request"}},
                    {"type": "assistant", "message": {"content": [{"type": "text", "text": "Done"}], "stop_reason": "end_turn"}},
                    {"type": "system", "subtype": "stop_hook_summary", "preventedContinuation": proof == "prevented", "hookErrors": [], "stopReason": ""},
                ]
                if proof == "tool_still_running":
                    entries.append({"type": "api-request"})
                if proof == "later_prompt":
                    entries.append({"type": "user", "message": {"content": "New task"}})
                if proof == "late_tool_result":
                    entries.append({"type": "user", "message": {"content": [{"type": "tool_result", "content": "Late outcome"}]}})
                config = self.root / "claude-config"
                path = config / "projects" / "project" / (thread + ".jsonl")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("\n".join(json.dumps({"sessionId": thread, "timestamp": stamp, **e}) for e in entries) + "\n")
                if proof == "partial_tail":
                    with path.open("a") as stream:
                        stream.write('{"type":"user"')
                with patch.dict("os.environ", {"CLAUDE_CONFIG_DIR": str(config)}):
                    BrowserInboxWake(self.sessions).process_once()
                turns = self.turns(thread)
                self.assertEqual(len(turns), 1)
                if proof == "complete":
                    self.assertEqual(turns[0]["status"], "completed")
                    self.assertIn("historical inbox work remains paused", self.sessions.work_thread(thread)["wake_pause_reason"])

    def test_claim_survives_crash_and_blocks_new_messages_until_user_input(self):
        thread = self.create()
        first = self.message(thread)
        self.assertEqual(self.sessions.inbox_wake._claim(thread), [first["id"]])
        self.sessions.close_work_thread(thread)
        self.message(thread)
        BrowserInboxWake(self.sessions).process_once()
        self.assertEqual(self.store.threads.get(thread)["attention"], "now")
        self.assertEqual(self.turns(thread), [])
        self.assertEqual(len(self.attempts()), 1)

    def test_multiple_dispatchers_claim_only_one_turn(self):
        thread = self.create()
        self.message(thread)
        other = BrowserInboxWake(self.sessions)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(w.process_once) for w in (self.sessions.inbox_wake, other)]
            for future in futures:
                future.result()
        self.assertEqual(len(self.turns(thread)), 1)
        self.assertEqual(len(self.attempts()), 1)

    def test_database_claim_is_exclusive_across_independent_runtimes(self):
        thread = self.create()
        message = self.message(thread)
        other = self.make_sessions().inbox_wake
        barrier = threading.Barrier(2)
        def claim(wake):
            barrier.wait(timeout=5)
            return wake._claim(thread)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(claim, w) for w in (self.sessions.inbox_wake, other)]
            results = [future.result() for future in futures]
        self.assertEqual(sorted(results, key=len), [[], [message["id"]]])

    def test_lost_response_after_provider_start_never_replays(self):
        thread = self.create()
        self.message(thread)
        request = self.sessions.rpc.request
        def lose_response(method, params=None):
            result = request(method, params)
            if method == "turn/start":
                raise TimeoutError("Response lost after provider accepted turn")
            return result
        self.sessions.rpc.request = lose_response
        self.sessions.inbox_wake.process_once()
        self.finish(thread)
        self.sessions.rpc.request = request
        self.sessions.close()
        reopened = self.make_sessions()
        reopened.rpc.threads = self.sessions.rpc.threads
        reopened.inbox_wake.process_once()
        self.assertEqual(len(reopened.read(thread)["thread"]["turns"]), 1)
        self.assertFalse(any(m == "turn/start" for m, _ in reopened.rpc.calls))

    def test_accepted_wake_is_not_replayed_after_runtime_restart(self):
        thread = self.create()
        self.message(thread)
        self.sessions.inbox_wake.process_once()
        self.finish(thread)
        self.sessions.close()
        reopened = self.make_sessions()
        reopened.rpc.threads = self.sessions.rpc.threads
        reopened.inbox_wake.process_once()
        self.assertFalse(any(m == "turn/start" for m, _ in reopened.rpc.calls))
        self.assertEqual(self.attempts()[0]["outcome"], "sent")

    def test_pending_close_threads_stay_asleep(self):
        thread = self.create()
        self.message(thread)
        ThreadControl(self.store).request_close(thread)
        self.sessions.inbox_wake.process_once()
        self.assertEqual(self.attempts(), [])
        self.assertFalse(any(m == "turn/start" for m, _ in self.sessions.rpc.calls))

    def test_closed_specialists_reopen_and_resume_context_settings(self):
        for client in ("codex", "claude"):
            with self.subTest(client=client):
                thread = self.sessions.create({"client": client, "yolo": True,
                    "model": "sonnet" if client == "claude" else "available-model", "effort": "high"})["session"]["thread_id"]
                self.sessions.send(thread, {"message": "Remember the original coding request"})
                if client == "claude":
                    self.sessions.claude.connections[thread].callback({"type": "system", "subtype": "init"})
                self.finish(thread)
                old = self.message(thread)
                self.sessions.close_work_thread(thread)
                before = self.store.threads.get(thread)
                new = self.message(thread)
                self.assertEqual(self.store.threads.get(thread)["attention"], "now")
                self.sessions.inbox_wake.process_once()
                self.assertEqual(len(self.turns(thread)), 2)
                self.assertFalse(self.sessions._record(thread)["archived"])
                self.assertIn(thread, self.sessions.loaded)
                after = self.store.threads.get(thread)
                for key in ("original_request", "title", "repository_id", "project_id"):
                    self.assertEqual(before[key], after[key], key)
                self.assertEqual(after["attention"], "now")
                attempts = [a for a in self.attempts() if a["session_id"] == thread]
                self.assertEqual([a["message_id"] for a in attempts], [old["id"], new["id"]])
                if client == "codex":
                    methods = [m for m, _ in self.sessions.rpc.calls]
                    self.assertLess(methods.index("thread/unarchive"), methods.index("thread/resume"))
                    params = [p for m, p in self.sessions.rpc.calls if m == "turn/start"][-1]
                    self.assertEqual((params["model"], params["effort"], params["approvalPolicy"]),
                                     ("available-model", "high", "never"))
                else:
                    options = self.sessions.claude.connections[thread].options
                    self.assertTrue(options["resume"])
                    self.assertEqual((options["model"], options["effort"], options["yolo"]), ("sonnet", "high", True))
                self.finish(thread)
                # Direct user revisions need no separate UI Reopen action.
                self.sessions.send(thread, {"message": "Revise that code"})
                self.finish(thread)
                self.message(thread)
                self.sessions.inbox_wake.process_once()
                self.assertEqual(len(self.turns(thread)), 4)
                self.assertEqual(self.turns(thread)[0]["items"][0]["content"][0]["text"],
                                 "Remember the original coding request")
                self.finish(thread)

    def test_closed_wake_survives_runtime_restart_for_both_providers(self):
        threads = [self.create(client) for client in ("codex", "claude")]
        for thread in threads:
            self.sessions.send(thread, {"message": "Keep my context"})
            if self.sessions._record(thread)["client"] == "claude":
                self.sessions.claude.connections[thread].callback({"type": "system", "subtype": "init"})
            self.finish(thread)
            self.sessions.close_work_thread(thread)
            self.message(thread)
        self.sessions.close()
        resumed = self.make_sessions()
        resumed.rpc.threads = self.sessions.rpc.threads
        for thread in threads:
            self.assertEqual(self.store.threads.get(thread)["attention"], "now")
            self.assertTrue(resumed._record(thread)["archived"])
        resumed.inbox_wake.process_once()
        for thread in threads:
            self.assertEqual(len(resumed.read(thread)["thread"]["turns"]), 2)
            self.assertEqual(self.store.threads.get(thread)["attention"], "now")
        self.assertEqual([a["outcome"] for a in self.attempts()], ["sent", "sent"])

    def test_closed_does_not_override_stop_or_queued_user_input(self):
        for client in ("codex", "claude"):
            with self.subTest(client=client):
                stopped, queued = self.create(client), self.create(client)
                self.sessions.send(stopped, {"message": "Working"})
                self.sessions.close_work_thread(stopped)
                self.message(stopped)
                self.sessions.send(queued, {"message": "Working"})
                self.sessions.queue.enqueue(queued, {"message": "User revision"})
                self.finish(queued)
                self.sessions.close_work_thread(queued)
                self.message(queued)
                for thread in (stopped, queued):
                    self.assertEqual(self.store.threads.get(thread)["attention"], "now")
                    self.assertTrue(self.sessions._record(thread)["archived"])
        self.sessions.inbox_wake.process_once()
        self.assertEqual(self.attempts(), [])

    def test_closed_unarchive_failure_leaves_visible_thread_paused(self):
        thread = self.create()
        self.sessions.close_work_thread(thread)
        self.message(thread)
        original = self.sessions.rpc.request
        def fail(method, params=None):
            if method == "thread/unarchive":
                raise RuntimeError("Provider unavailable")
            return original(method, params)
        self.sessions.rpc.request = fail
        self.sessions.inbox_wake.process_once()
        self.sessions.inbox_wake.process_once()
        self.assertEqual(self.attempts(), [])  # Rejected before turn/start, not an uncertain submission.
        with self.store._connection() as db:
            self.assertEqual(db.execute("SELECT pause_kind FROM browser_inbox_wake WHERE thread_id=?", (thread,)).fetchone()[0], "pre_submit")
        self.assertEqual(self.store.threads.get(thread)["attention"], "now")
        self.assertEqual(self.turns(thread), [])

    def test_cli_send_reopens_closed_app_threads_with_no_new_options(self):
        script = Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts/agent-coord"
        for client in ("codex", "claude"):
            with self.subTest(client=client):
                thread = self.create(client)
                self.sessions.close_work_thread(thread)
                result = subprocess.run([sys.executable, str(script), "--db", str(self.root / "state.sqlite3"),
                    "send", "--from-session", "sender", "--session", thread,
                    "--classification", "action_required", "--no-reply-required", "Answer the user here"],
                    capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)
                sent = json.loads(result.stdout)
                self.assertEqual(sent["recipient_session_id"], thread)
                self.assertEqual(self.store.threads.get(thread)["attention"], "now")
                self.assertTrue(self.sessions._record(thread)["archived"])
                self.sessions.inbox_wake.process_once()
                self.assertEqual(len(self.turns(thread)), 1)
                self.finish(thread)

    def test_busy_hook_delivery_reopens_without_steering_or_extra_turn(self):
        for client in ("codex", "claude"):
            with self.subTest(client=client):
                thread = self.create(client)
                self.sessions.send(thread, {"message": "Already working"})
                # Placement alone can be closed independently of a running turn.
                self.store.threads.update(thread, attention="archived")
                message = self.message(thread)
                self.assertEqual(self.store.threads.get(thread)["attention"], "now")
                self.assertIn(message["body"], _actionable_context(self.store, thread))
                self.sessions.inbox_wake.process_once()
                self.finish(thread)
                self.sessions.inbox_wake.process_once()
                self.assertEqual(len(self.turns(thread)), 1)
                self.assertEqual(self.attempts(), [])
        self.assertFalse(any(m == "turn/steer" for m, _ in self.sessions.rpc.calls))

    def test_informational_closure_and_terminal_messages_do_not_reopen(self):
        for client in ("codex", "claude"):
            thread = self.create(client)
            self.sessions.close_work_thread(thread)
            self.message(thread, "informational")
            self.message(thread, "closure")
            self.assertEqual(self.store.threads.get(thread)["attention"], "archived")
        self.store.register(session_id="terminal", client="codex", cwd=str(self.root))
        self.store.threads.update("terminal", attention="archived")
        self.message("terminal")
        self.sessions.inbox_wake.process_once()
        self.assertEqual(self.store.threads.get("terminal")["attention"], "archived")
        self.assertEqual(self.attempts(), [])

    def test_repeated_delivery_and_ack_do_not_reopen_again(self):
        thread = self.create()
        original = self.store.send_message(sender_session_id=thread, recipient_session_id="sender",
                                           body="Please confirm", reply_required=True)
        self.sessions.close_work_thread(thread)
        reply = self.store.reply_message(sender_session_id="sender", message_id=original["id"], body="Confirmed")
        self.assertEqual(self.store.threads.get(thread)["attention"], "now")
        self.sessions.inbox_wake.process_once()
        self.assertIn("Confirmed", _actionable_context(self.store, thread))
        self.finish(thread)
        self.sessions.close_work_thread(thread)
        for _ in range(2):
            self.store.inbox(thread, include_delivered=True)
            self.store.acknowledge(thread, reply["id"])
            _actionable_context(self.store, thread)
            self.sessions.inbox_wake.process_once()
            self.assertEqual(len(self.turns(thread)), 1)
            self.assertEqual(self.store.threads.get(thread)["attention"], "archived")

    def test_direct_input_resumes_routed_closed_stopped_conversation(self):
        worker = AppControlWorker(self.sessions)
        self.addCleanup(worker.close)
        for client in ("codex", "claude"):
            with self.subTest(client=client):
                thread = self.create(client)
                self.sessions.send(thread, {"message": "Original work"})
                self.sessions.close_work_thread(thread)
                self.message(thread)
                self.sessions.inbox_wake.process_once()
                self.assertEqual(self.attempts(), [])
                self.assertEqual(len(self.turns(thread)), 1)
                request = worker.control.request("settings", "sender", {"effort": "high"}, thread_id=thread)
                worker.process_once()
                self.assertEqual(worker.control.status(request["request_id"])["status"], "completed")
                self.sessions.send(thread, {"message": "Continue with my revision"})
                self.assertFalse(self.sessions._record(thread)["archived"])
                self.assertEqual(len(self.turns(thread)), 2)
                self.assertEqual(self.sessions._record(thread)["effort"], "high")
                _actionable_context(self.store, thread)
                self.finish(thread)

    def test_work_queued_before_close_reopens_when_eventually_dispatched(self):
        thread = self.create()
        self.message(thread)
        self.sessions.close_work_thread(thread)
        self.sessions.inbox_wake.process_once()
        self.assertEqual(self.store.threads.get(thread)["attention"], "now")
        self.assertEqual(len(self.turns(thread)), 1)

    def test_user_can_resume_queued_revision_after_automatic_reopening(self):
        # Keep queue delivery deterministic while exercising the real user action.
        self.sessions.queue.wake = lambda: None
        for client in ("codex", "claude"):
            with self.subTest(client=client):
                thread = self.create(client)
                self.sessions.send(thread, {"message": "Initial work"})
                self.sessions.queue.enqueue(thread, {"message": "Queued user revision"})
                self.finish(thread)
                self.sessions.close_work_thread(thread)
                self.message(thread)
                self.sessions.inbox_wake.process_once()
                self.assertEqual(self.attempts(), [])
                self.assertEqual(self.sessions.queue.list(thread)[0]["state"], "paused")
                self.sessions.queue.change(thread, {"action": "resume"})
                self.sessions.queue._dispatch(thread)
                self.assertEqual(self.sessions.queue.list(thread), [])
                self.assertEqual(len(self.turns(thread)), 2)
                self.assertEqual(self.turns(thread)[-1]["items"][0]["content"][0]["text"], "Queued user revision")
                self.assertFalse(self.sessions._record(thread)["archived"])
                _actionable_context(self.store, thread)
                self.finish(thread)

    def test_later_placement_and_snooze_are_preserved(self):
        thread = self.create()
        self.sessions.send(thread, {"message": "Original request"})
        self.finish(thread)
        self.store.threads.update(thread, snoozed_until=self.store.clock() + 3600)
        before = self.store.threads.get(thread)
        self.message(thread)
        self.sessions.inbox_wake.process_once()
        after = self.store.threads.get(thread)
        for key in ("attention", "snoozed_until", "original_request", "title"):
            self.assertEqual(before[key], after[key], key)

    def test_cold_open_thread_resumes_but_other_workspaces_do_not(self):
        thread = self.create()
        self.sessions.loaded.discard(thread)
        self.store.end_session(thread)
        self.message(thread)
        outside = self.root / "other"
        outside.mkdir()
        self.make_sessions(outside).inbox_wake.process_once()
        self.assertEqual(self.attempts(), [])
        self.sessions.inbox_wake.process_once()
        self.assertEqual(len(self.turns(thread)), 1)
        self.assertTrue(any(m == "thread/resume" for m, _ in self.sessions.rpc.calls))

    def test_stop_racing_with_provider_read_prevents_dispatch(self):
        thread = self.create()
        self.message(thread)
        original = self.sessions.read
        def read(thread_id):
            result = original(thread_id)
            self.sessions.inbox_wake.pause(thread_id, "Stop raced with read")
            return result
        self.sessions.read = read
        self.sessions.inbox_wake.process_once()
        self.assertEqual(self.attempts(), [])

    def test_closure_between_claim_and_submission_cancels_wake_without_pausing(self):
        thread = self.create()
        message = self.message(thread)
        # Submission prepares the provider context after the claim. An idle
        # loaded conversation is not reread there, so close it at that step.
        resume = self.sessions._resume_thread
        def close_during_submission(thread_id, record):
            resume(thread_id, record)
            self.message(thread, "closure", thread_id=message["thread_id"])
        self.sessions._resume_thread = close_during_submission
        self.sessions.inbox_wake.process_once()
        self.assertEqual(self.attempts(), [])
        self.assertEqual(self.turns(thread), [])
        self.sessions._resume_thread = resume
        self.message(thread, thread_id=message["thread_id"])
        self.sessions.inbox_wake.process_once()
        self.assertEqual(len(self.turns(thread)), 1)

    def test_user_queue_takes_priority(self):
        thread = self.create()
        self.message(thread)
        self.sessions.send(thread, {"message": "Initial work"})
        self.sessions.queue.enqueue(thread, {"message": "User follow-up"})
        self.sessions.queue.pause(thread, "User paused queue")
        self.finish(thread)
        self.sessions.inbox_wake.process_once()
        self.assertEqual(len(self.turns(thread)), 1)

    def test_worker_observes_cli_style_database_writes_and_shuts_down(self):
        thread = self.create()
        self.sessions.inbox_wake.start()
        self.message(thread)
        with self.sessions.changed:
            self.assertTrue(self.sessions.changed.wait_for(lambda: thread in self.sessions.active, timeout=5))
        self.sessions.inbox_wake.close()
        self.assertFalse(self.sessions.inbox_wake.worker.is_alive())
