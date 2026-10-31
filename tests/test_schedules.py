from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))
from agent_coord.codex_app_server import BrowserSessions
from agent_coord.store import CoordinationError, CoordinationStore
from agent_coord.ui import make_ui_server
from agent_coord.app_control import AppControlWorker
from agent_coord.checkpoint_mcp import schedule_prompt, dispatch
from test_codex_app_server import FakeCodex
from test_browser_providers import claude_factory


class ScheduledPromptTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.now = 1800000000.0
        self.store = CoordinationStore(self.root / "state.sqlite3", clock=lambda: self.now)
        self.sessions = self.runtime()
        self.schedules = self.sessions.schedules

    def runtime(self, cwd=None):
        sessions = BrowserSessions(self.store, str(cwd or self.root), rpc_factory=FakeCodex, claude_factory=claude_factory)
        self.addCleanup(sessions.close)
        return sessions

    def body(self, **changes):
        return {"id": str(uuid.uuid4()), "message": "Run the backfill, validate and deploy", "run_at": self.now + 60,
                "timezone": "America/Chicago", "settings": {"cwd": str(self.root), "model": "available-model", "effort": "high", "yolo": True}, **changes}

    def item(self, item):
        self.assertTrue(self.sessions.persistence.flush())
        return next(row for row in self.schedules.list() if row["id"] == item["id"])

    def starts(self, sessions=None):
        return [params for method, params in (sessions or self.sessions).rpc.calls if method == "turn/start"]

    def launch(self, **changes):
        item = self.schedules.create(self.body(**changes))
        self.now = item["run_at"]
        self.schedules.process_once()
        return self.item(item)

    def existing_thread(self, **settings):
        return self.sessions.create({"cwd": str(self.root), "model": "available-model", **settings})["session"]["thread_id"]

    def target_body(self, thread_id, **changes):
        body = self.body(target_thread_id=thread_id, **changes)
        body.pop("settings")
        return body

    def complete_turn(self, thread_id, status="completed"):
        turn = self.sessions.rpc.threads[thread_id]["turns"][-1]
        turn["status"] = status
        self.sessions._event({"method": "turn/completed", "params": {"threadId": thread_id, "turn": turn}})
        self.assertTrue(self.sessions.persistence.flush(key=thread_id))

    def test_existing_thread_receives_verbatim_message_with_current_settings(self):
        thread_id = self.existing_thread()
        self.sessions.rpc.fast_turn = True
        self.sessions.send(thread_id, {"message": "Earlier context"})
        message = "  continue\n\nKeep this indentation.\n  "
        item = self.schedules.create(self.target_body(thread_id, message=message))
        self.assertEqual(item["thread_id"], thread_id)
        self.assertEqual(len(self.starts()), 1)
        self.sessions.update(thread_id, {"effort": "high", "yolo": True})
        self.now = item["run_at"]
        self.schedules.process_once()
        self.assertEqual(self.item(item)["state"], "completed")
        self.assertEqual(len(self.sessions.rpc.threads), 1)
        start = self.starts()[-1]
        self.assertEqual(start["threadId"], thread_id)
        self.assertEqual(start["input"], [{"type": "text", "text": message}])
        self.assertEqual(start["effort"], "high")
        self.assertEqual(start["approvalPolicy"], "never")
        self.assertEqual(start["sandboxPolicy"], {"type": "dangerFullAccess"})
        self.assertEqual(len(self.sessions.rpc.threads[thread_id]["turns"]), 2)
        self.schedules.process_once()
        self.assertEqual(len(self.starts()), 2)

    def test_agent_tool_schedules_own_thread_while_busy_and_retries_idempotently(self):
        thread_id = self.existing_thread()
        self.sessions.send(thread_id, {"message": "Current work"})
        worker = AppControlWorker(self.sessions)
        self.addCleanup(worker.close)
        args = {"session_id": thread_id, "mode": "self", "message": "Follow up",
                "run_at": self.now + 60, "timezone": "UTC", "request_id": "self-schedule", "wait_seconds": 0}
        receipt = schedule_prompt(args, store=self.store)
        self.assertEqual(receipt["status"], "queued")
        with self.store._connection() as db:
            self.assertEqual(db.execute("SELECT status FROM app_control_requests WHERE request_id = ?", ("self-schedule",)).fetchone()[0], "queued_schedule")
        worker.process_once()
        saved = schedule_prompt(args, store=self.store)
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(saved["result"]["id"], receipt["schedule_id"])
        self.assertEqual(saved["result"]["target_thread_id"], thread_id)
        self.assertEqual(len(self.schedules.list()), 1)
        self.now += 60
        self.schedules.process_once()
        self.assertEqual(self.item(saved["result"])["state"], "waiting")
        self.complete_turn(thread_id)
        self.schedules.process_once()
        self.assertEqual(self.item(saved["result"])["state"], "running")
        self.assertEqual(self.starts()[-1]["input"][0]["text"], "Follow up")
        with self.assertRaisesRegex(CoordinationError, "different app request"):
            schedule_prompt({**args, "message": "Different"}, store=self.store)

    def test_agent_tool_schedules_configured_new_agent(self):
        self.store.register(session_id="caller", client="codex", cwd=str(self.root))
        worker = AppControlWorker(self.sessions)
        self.addCleanup(worker.close)
        args = {"session_id": "caller", "mode": "new", "message": "Do the work", "run_at": self.now + 60,
                "timezone": "UTC", "request_id": "new-schedule", "wait_seconds": 0,
                "settings": {"cwd": str(self.root), "client": "claude", "model": "sonnet", "effort": "high", "yolo": True}}
        receipt = schedule_prompt(args, store=self.store)
        worker.process_once()
        saved = worker.control.status(receipt["request_id"])
        self.assertEqual(saved["status"], "completed", saved)
        self.assertEqual(saved["result"]["settings"]["client"], "claude")
        self.assertEqual(saved["result"]["settings"]["model"], "sonnet")
        self.assertEqual(saved["result"]["settings"]["effort"], "high")
        self.assertTrue(saved["result"]["settings"]["yolo"])
        self.assertFalse(self.sessions.rpc.threads)
        self.now += 60
        self.schedules.process_once()
        item = self.item(saved["result"])
        self.assertEqual(item["state"], "running")
        connection = self.sessions.claude.connections[item["thread_id"]]
        self.assertEqual(connection.options["model"], "sonnet")
        self.assertEqual(connection.options["effort"], "high")

    def test_agent_tool_rejects_terminal_self_and_invalid_inputs(self):
        self.store.register(session_id="terminal", client="codex", cwd=str(self.root))
        base = {"session_id": "terminal", "mode": "self", "message": "Hello", "run_at": self.now + 60,
                "timezone": "UTC", "wait_seconds": 0}
        with self.assertRaisesRegex(CoordinationError, "terminal session"):
            schedule_prompt(base, store=self.store)
        for change in ({"mode": "new"}, {"mode": []}, {"settings": {}}, {"run_at": float("nan")},
                       {"run_at": 10**1000}, {"wait_seconds": True}, {"request_id": ""}, {"other": 1}):
            with self.subTest(change=change), self.assertRaises(CoordinationError):
                schedule_prompt({**base, **change}, store=self.store)
        catalog = dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
        self.assertIn("schedule_prompt", [tool["name"] for tool in catalog])

    def test_agent_mcp_stdio_schedules_using_caller_identity(self):
        import os
        thread_id = self.existing_thread()
        worker = AppControlWorker(self.sessions)
        self.addCleanup(worker.close)
        command = [sys.executable, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts/agent-coord-mcp")]
        call = {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "schedule_prompt", "arguments": {
            "mode": "self", "message": "Later", "run_at": self.now + 60, "timezone": "UTC", "wait_seconds": 0}}}
        env = {**os.environ, "AGENT_COORD_DB": str(self.store.database_path), "AGENT_COORD_SESSION_ID": thread_id}
        completed = subprocess.run(command, input=json.dumps(call) + "\n", text=True, capture_output=True, env=env, timeout=10, check=True)
        receipt = json.loads(json.loads(completed.stdout)["result"]["content"][0]["text"])
        self.assertEqual(receipt["status"], "queued")
        worker.process_once()
        saved = worker.control.status(receipt["request_id"])
        self.assertEqual(saved["status"], "completed", saved)
        self.assertEqual(saved["result"]["target_thread_id"], thread_id)

    def test_busy_target_waits_without_steering_or_becoming_missed(self):
        thread_id = self.existing_thread()
        self.sessions.send(thread_id, {"message": "Current work"})
        item = self.schedules.create(self.target_body(thread_id))
        self.now = item["run_at"]
        self.schedules.process_once()
        self.assertEqual(self.item(item)["state"], "waiting")
        self.assertEqual(len(self.starts()), 1)
        self.assertFalse(any(method == "turn/steer" for method, _ in self.sessions.rpc.calls))
        self.now += 300
        self.complete_turn(thread_id)
        self.assertEqual(self.item(item)["state"], "waiting")
        self.schedules.process_once()
        self.assertEqual(self.item(item)["state"], "running")
        self.assertEqual(len(self.starts()), 2)

    def test_waiting_schedule_can_be_cancelled_or_rescheduled(self):
        thread_id = self.existing_thread()
        self.sessions.send(thread_id, {"message": "Current work"})
        cancelled = self.schedules.create(self.target_body(thread_id))
        edited = self.schedules.create(self.target_body(thread_id))
        self.now += 60
        self.schedules.process_once()
        self.schedules.change(cancelled["id"], {"action": "cancel", "version": self.item(cancelled)["version"]})
        changed = self.schedules.change(edited["id"], {**self.target_body(thread_id, message="Later"),
            "action": "edit", "version": self.item(edited)["version"]})
        self.assertEqual(changed["target_thread_id"], thread_id)
        self.complete_turn(thread_id)
        self.schedules.process_once()
        self.assertEqual(len(self.starts()), 1)
        self.assertEqual(self.item(cancelled)["state"], "cancelled")
        self.assertEqual(self.item(edited)["state"], "scheduled")

    def test_multiple_schedules_in_one_thread_track_only_their_own_turns(self):
        thread_id = self.existing_thread()
        first = self.schedules.create(self.target_body(thread_id, message="First", run_at=self.now + 50))
        second = self.schedules.create(self.target_body(thread_id, message="Second"))
        self.now += 60
        self.schedules.process_once()
        self.assertEqual(self.item(first)["state"], "running")
        self.assertEqual(self.item(second)["state"], "waiting")
        self.complete_turn(thread_id)
        self.schedules.process_once()
        self.assertEqual(self.item(first)["state"], "completed")
        self.assertEqual(self.item(second)["state"], "running")
        self.complete_turn(thread_id, "failed")
        self.assertEqual(self.item(first)["state"], "completed")
        self.assertEqual(self.item(second)["state"], "failed")

    def test_existing_target_validation_and_destination_immutability(self):
        thread_id = self.existing_thread()
        for body in (self.target_body("missing"), self.target_body([]),
                     self.target_body(thread_id, message="  /permissions yolo"), self.body(target_thread_id=thread_id)):
            with self.subTest(body=body), self.assertRaises(CoordinationError):
                self.schedules.create(body)
        item = self.schedules.create(self.target_body(thread_id))
        with self.assertRaisesRegex(CoordinationError, "destination"):
            self.schedules.change(item["id"], {**self.body(), "action": "edit", "version": item["version"]})
        child = self.root / "child"; child.mkdir()
        other = self.runtime(child)
        with self.assertRaisesRegex(CoordinationError, "workspace"):
            other.schedules.create(self.target_body(thread_id))

    def test_closed_or_deleted_target_never_creates_a_replacement(self):
        for deleted in (False, True):
            thread_id = self.existing_thread()
            item = self.schedules.create(self.target_body(thread_id))
            if deleted:
                with self.store._connection() as db:
                    db.execute("DELETE FROM browser_sessions WHERE thread_id = ?", (thread_id,))
                self.sessions.loaded.discard(thread_id)
            else:
                self.store.threads.update(thread_id, attention="archived")
            self.now = item["run_at"]
            self.schedules.process_once()
            self.assertEqual(self.item(item)["state"], "failed")
            self.assertEqual(self.item(item)["thread_id"], thread_id)
        self.assertFalse(self.starts())
        self.assertEqual(len(self.sessions.rpc.threads), 2)

    def test_waiting_target_survives_restart_without_replaying_a_send(self):
        thread_id = self.existing_thread()
        self.sessions.send(thread_id, {"message": "Current work"})
        item = self.schedules.create(self.target_body(thread_id))
        self.now = item["run_at"]
        self.schedules.process_once()
        self.complete_turn(thread_id)
        saved_threads = copy.deepcopy(self.sessions.rpc.threads)
        self.sessions.close()
        self.now += 300
        restarted = self.runtime()
        restarted.rpc.threads = saved_threads
        restarted.schedules.process_once()
        self.assertEqual(self.item(item)["state"], "running")
        self.assertEqual(len(self.starts(restarted)), 1)
        self.assertEqual(self.starts(restarted)[0]["threadId"], thread_id)

    def test_waiting_schedule_gives_existing_queue_priority(self):
        thread_id = self.existing_thread()
        with patch.object(self.sessions.queue, "wake"):
            queued = self.sessions.queue.enqueue(thread_id, {"message": "Earlier queued message"})
        item = self.schedules.create(self.target_body(thread_id))
        self.now = item["run_at"]
        self.schedules.process_once()
        self.assertEqual(self.item(item)["state"], "waiting")
        self.assertFalse(self.starts())
        with patch.object(self.sessions.queue, "wake"):
            self.sessions.queue.change(thread_id, {"action": "cancel", "id": queued["id"]})
        self.schedules.process_once()
        self.assertEqual(self.item(item)["state"], "running")

    def test_two_runtimes_cannot_deliver_the_same_existing_thread_schedule(self):
        thread_id = self.existing_thread()
        item = self.schedules.create(self.target_body(thread_id))
        other = self.runtime()
        other.rpc.threads = self.sessions.rpc.threads
        self.now = item["run_at"]
        entered, release = threading.Event(), threading.Event()
        original = self.sessions.read
        def delayed(thread_id):
            entered.set()
            self.assertTrue(release.wait(5))
            return original(thread_id)
        with patch.object(self.sessions, "read", delayed), ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self.schedules.process_once)
            self.assertTrue(entered.wait(5))
            try:
                other.schedules.process_once()
            finally:
                release.set()
            pending.result(5)
        self.assertEqual(len(self.starts()) + len(self.starts(other)), 1)

    def test_cancel_during_destination_read_prevents_delivery(self):
        thread_id = self.existing_thread()
        item = self.schedules.create(self.target_body(thread_id))
        original = self.sessions.read
        def cancel_during_read(thread_id):
            self.schedules.change(item["id"], {"action": "cancel", "version": self.item(item)["version"]})
            return original(thread_id)
        self.now = item["run_at"]
        with patch.object(self.sessions, "read", cancel_during_read):
            self.schedules.process_once()
        self.assertEqual(self.item(item)["state"], "cancelled")
        self.assertFalse(self.starts())

    def test_old_database_migration_preserves_new_thread_schedule(self):
        from agent_coord.schedules import ScheduledPrompts
        item = self.schedules.create(self.body())
        with self.store._connection() as db:
            db.execute("ALTER TABLE scheduled_prompts DROP COLUMN target_thread_id")
        self.sessions.schedules = self.schedules = ScheduledPrompts(self.sessions)
        self.assertIsNone(self.item(item)["target_thread_id"])
        self.now = item["run_at"]
        self.schedules.process_once()
        self.assertEqual(self.item(item)["state"], "running")
        self.assertEqual(len(self.starts()), 1)

    def test_existing_target_lost_response_does_not_rebind_to_later_user_turn(self):
        thread_id = self.existing_thread()
        item = self.schedules.create(self.target_body(thread_id))
        self.now = item["run_at"]
        with patch.object(self.sessions, "send", side_effect=CoordinationError("Unknown transport outcome")):
            self.schedules.process_once()
        self.assertEqual(self.item(item)["state"], "review")
        self.sessions.rpc.fast_turn = True
        self.sessions.send(thread_id, {"message": "Separate user message"})
        self.assertEqual(self.item(item)["state"], "review")
        self.schedules.process_once()
        self.assertEqual(len(self.starts()), 1)

    def test_scheduled_user_message_resumes_failed_claude_thread(self):
        thread_id = self.existing_thread(client="claude", model="sonnet", effort="high")
        self.sessions.send(thread_id, {"message": "Earlier context"})
        self.sessions.claude.connections[thread_id].callback({"type": "result", "subtype": "error_during_execution", "is_error": True})
        item = self.schedules.create(self.target_body(thread_id, message="continue"))
        self.now = item["run_at"]
        self.schedules.process_once()
        self.assertEqual(self.item(item)["state"], "running")
        self.assertEqual(self.item(item)["thread_id"], thread_id)
        history = self.sessions.read(thread_id)["thread"]["turns"]
        self.assertEqual(len(history), 2)
        self.assertEqual(history[-1]["items"][0]["content"], [{"type": "text", "text": "continue"}])
        self.sessions.claude.connections[thread_id].callback({"type": "result", "subtype": "success"})
        self.assertEqual(self.item(item)["state"], "completed")

    def test_waiting_uses_no_turn_and_dispatch_preserves_settings(self):
        item = self.schedules.create(self.body())
        self.schedules.process_once()
        self.assertFalse(self.starts())
        self.assertFalse(self.sessions.rpc.threads)
        self.now = item["run_at"]
        self.schedules.process_once()
        result = self.item(item)
        self.assertEqual(result["state"], "running")
        start = self.starts()[0]
        self.assertEqual(start["model"], "available-model")
        self.assertEqual(start["effort"], "high")
        self.assertEqual(start["cwd"], str(self.root))
        self.assertEqual(start["approvalPolicy"], "never")
        self.assertEqual(start["sandboxPolicy"], {"type": "dangerFullAccess"})
        self.assertEqual(start["input"][0]["text"], item["message"])
        self.schedules.process_once()
        self.assertEqual(len(self.starts()), 1)

    def test_default_permissions_and_effort_are_saved_explicitly(self):
        item = self.launch(settings={"cwd": str(self.root), "model": "available-model"})
        self.assertFalse(item["settings"]["yolo"])
        self.assertEqual(item["settings"]["effort"], "medium")
        self.assertEqual(self.starts()[0]["approvalPolicy"], "on-request")

    def test_invalid_settings_dates_and_slash_commands_never_launch(self):
        bodies = [self.body(run_at=value) for value in (True, None, "2000000000", self.now, -1, float("nan"), float("inf"), 10**1000)]
        bodies += [self.body(timezone=value) for value in ("Bad/Zone", "/etc/passwd", None)]
        bodies += [self.body(message=value) for value in ("", "/permissions yolo", [])]
        for key, value in (("cwd", "/missing/workspace"), ("cwd", "/"), ("model", "missing"), ("effort", "ultra"), ("yolo", "yes"), ("client", "missing")):
            body = self.body(); body["settings"][key] = value; bodies.append(body)
        for body in bodies:
            with self.subTest(body=body), self.assertRaises(CoordinationError):
                self.schedules.create(body)
        self.assertFalse(self.schedules.list())
        self.assertFalse(self.sessions.rpc.threads)

    def test_create_is_idempotent_including_concurrent_retries(self):
        body = self.body()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.schedules.create(body), range(2)))
        self.assertEqual(results[0]["id"], results[1]["id"])
        self.now += 100
        self.assertEqual(self.schedules.create(body)["id"], results[0]["id"])
        with self.assertRaises(CoordinationError):
            self.schedules.create({**body, "message": "Something else"})
        self.assertEqual(len(self.schedules.list()), 1)

    def test_edit_cancel_and_stale_versions_are_atomic(self):
        item = self.schedules.create(self.body())
        updated = self.schedules.change(item["id"], {**self.body(message="Updated", run_at=self.now + 120), "action": "edit", "version": item["version"]})
        with self.assertRaisesRegex(CoordinationError, "changed"):
            self.schedules.change(item["id"], {"action": "cancel", "version": item["version"]})
        self.assertEqual(self.item(item)["message"], "Updated")
        self.schedules.change(item["id"], {"action": "cancel", "version": updated["version"]})
        self.now += 130
        self.schedules.process_once()
        self.assertEqual(self.item(item)["state"], "cancelled")
        self.assertFalse(self.starts())

    def test_sleep_marks_missed_and_run_now_is_explicit_and_once(self):
        item = self.schedules.create(self.body())
        self.now += 121
        self.schedules.process_once()
        missed = self.item(item)
        self.assertEqual(missed["state"], "missed")
        self.assertFalse(self.starts())
        self.schedules.change(item["id"], {"action": "run_now", "version": missed["version"]})
        self.schedules.process_once()
        self.assertEqual(len(self.starts()), 1)
        with self.assertRaises(CoordinationError):
            self.schedules.change(item["id"], {"action": "run_now", "version": missed["version"]})

    def test_restart_retains_future_schedules_but_marks_past_times_missed(self):
        item = self.schedules.create(self.body())
        future = self.schedules.create(self.body(run_at=self.now + 200))
        self.sessions.close()
        self.now += 61
        restarted = self.runtime()
        restarted.schedules.process_once()
        self.assertEqual(self.item(item)["state"], "missed")
        self.assertEqual(self.item(future)["state"], "scheduled")
        self.now = future["run_at"]
        restarted.schedules.process_once()
        self.assertEqual(len(self.starts(restarted)), 1)

    def test_two_runtimes_claim_only_once_and_cannot_cancel_after_claim(self):
        item = self.schedules.create(self.body())
        other = self.runtime()
        self.now = item["run_at"]
        entered, release = threading.Event(), threading.Event()
        original = self.sessions.create
        def delayed(body):
            entered.set()
            self.assertTrue(release.wait(5))
            return original(body)
        with patch.object(self.sessions, "create", delayed), ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self.schedules.process_once)
            self.assertTrue(entered.wait(5))
            try:
                other.schedules.process_once()
                with self.assertRaises(CoordinationError):
                    self.schedules.change(item["id"], {"action": "cancel", "version": self.item(item)["version"]})
            finally:
                release.set()
            pending.result(5)
        self.assertEqual(len(self.starts()) + len(self.starts(other)), 1)

    def test_fast_completion_and_followup_do_not_change_original_result(self):
        self.sessions.rpc.fast_turn = True
        item = self.launch()
        self.assertEqual(item["state"], "completed")
        self.sessions.rpc.fast_turn = False
        self.sessions.send(item["thread_id"], {"message": "Follow-up"})
        self.assertEqual(self.item(item)["state"], "completed")

    def test_other_input_in_new_thread_is_not_treated_as_scheduled_execution(self):
        original = self.sessions.create
        def interrupted_creation(body):
            result = original(body)
            self.sessions.rpc.fast_turn = True
            self.sessions.send(result["session"]["thread_id"], {"message": "A person sent this first"})
            return result
        with patch.object(self.sessions, "create", interrupted_creation):
            item = self.launch()
        self.assertEqual(item["state"], "failed")
        self.assertEqual(len(self.starts()), 1)
        self.assertEqual(self.starts()[0]["input"][0]["text"], "A person sent this first")

    def test_failed_and_interrupted_turns_show_failed(self):
        for status in ("failed", "interrupted"):
            item = self.launch()
            turn = self.sessions.rpc.threads[item["thread_id"]]["turns"][-1]
            turn["status"] = status
            self.sessions._event({"method": "turn/completed", "params": {"threadId": item["thread_id"], "turn": turn}})
            self.assertEqual(self.item(item)["state"], "failed")

    def test_lost_send_response_never_retries_and_keeps_thread(self):
        original = self.sessions.send
        def uncertain(*args, **kwargs):
            original(*args, **kwargs)
            raise CoordinationError("Reply lost")
        with patch.object(self.sessions, "send", uncertain):
            item = self.launch()
        self.assertEqual(item["state"], "review")
        self.assertTrue(item["thread_id"])
        self.schedules.process_once()
        self.assertEqual(len(self.starts()), 1)
        with self.assertRaises(CoordinationError):
            self.schedules.change(item["id"], {"action": "run_now", "version": item["version"]})

    def test_model_removed_at_launch_fails_without_substitution(self):
        item = self.schedules.create(self.body())
        self.now = item["run_at"]
        with patch.object(self.sessions, "models", return_value=[]):
            self.schedules.process_once()
        self.assertEqual(self.item(item)["state"], "failed")
        self.assertFalse(self.starts())

    def test_abandoned_dispatch_reconciles_known_turn_without_resubmission(self):
        item = self.launch()
        saved_threads = copy.deepcopy(self.sessions.rpc.threads)
        saved_threads[item["thread_id"]]["turns"][-1]["status"] = "completed"
        self.sessions.close()
        restarted = self.runtime()
        restarted.rpc.threads = saved_threads
        restarted.schedules.process_once()
        self.assertEqual(self.item(item)["state"], "completed")
        self.assertFalse(self.starts(restarted))

    def test_crash_before_thread_record_requires_review_without_retry(self):
        item = self.schedules.create(self.body())
        with self.store._connection() as db:
            db.execute("UPDATE scheduled_prompts SET state = 'starting', owner = 'dead' WHERE id = ?", (item["id"],))
        restarted = self.runtime()
        restarted.schedules.process_once()
        self.assertEqual(self.item(item)["state"], "review")
        self.assertFalse(self.starts(restarted))

    def test_workspace_filter_applies_to_listing_mutation_and_worker(self):
        item = self.schedules.create(self.body())
        child = self.root / "child"; child.mkdir()
        other = self.runtime(child)
        self.assertEqual(other.schedules.list(), [])
        with self.assertRaises(CoordinationError):
            other.schedules.change(item["id"], {"action": "cancel", "version": item["version"]})
        self.now = item["run_at"]
        other.schedules.process_once()
        self.assertFalse(self.starts(other))
        self.assertEqual(self.item(item)["state"], "scheduled")

    def test_claude_schedule_uses_existing_provider_and_completion(self):
        item = self.launch(settings={"cwd": str(self.root), "client": "claude", "model": "sonnet", "effort": "high", "yolo": True})
        self.assertEqual(item["state"], "running")
        connection = self.sessions.claude.connections[item["thread_id"]]
        self.assertEqual(connection.options["model"], "sonnet")
        self.assertEqual(connection.options["effort"], "high")
        connection.callback({"type": "result", "subtype": "success"})
        self.assertEqual(self.item(item)["state"], "completed")
        self.assertFalse(self.starts())

    def test_recurring_job_creates_distinct_threads_and_keeps_latest_result(self):
        item = self.launch(recurrence={"frequency": "interval", "minutes": 10})
        self.complete_turn(item["thread_id"])
        self.schedules.process_once()
        following = self.item(item)
        self.assertEqual(following["state"], "scheduled")
        self.assertEqual(following["run_at"], item["run_at"] + 600)
        self.assertEqual(following["last_run"]["state"], "completed")
        self.assertEqual(following["last_run"]["thread_id"], item["thread_id"])
        self.assertIsNone(following["turn_id"])
        self.now = following["run_at"]
        self.schedules.process_once()
        second = self.item(item)
        self.assertEqual(second["state"], "running")
        self.assertNotEqual(second["thread_id"], item["thread_id"])
        self.assertEqual(len(self.starts()), 2)

    def test_agent_tool_carries_authorized_recurrence_through_app_request(self):
        self.store.register(session_id="caller", client="codex", cwd=str(self.root))
        worker = AppControlWorker(self.sessions)
        self.addCleanup(worker.close)
        args = {"session_id": "caller", "mode": "new", "message": "Review progress", "run_at": self.now + 60,
                "timezone": "UTC", "request_id": "recurring-schedule", "wait_seconds": 0,
                "recurrence": {"frequency": "weekdays"},
                "settings": {"cwd": str(self.root), "client": "codex", "model": "available-model"}}
        receipt = schedule_prompt(args, store=self.store)
        worker.process_once()
        saved = worker.control.status(receipt["request_id"])
        self.assertEqual(saved["status"], "completed", saved)
        self.assertEqual(saved["result"]["recurrence"], {"frequency": "weekdays"})
        self.assertEqual(saved["result"]["recurrence_status"], "active")
        with self.assertRaisesRegex(CoordinationError, "repeat"):
            schedule_prompt({**args, "recurrence": {"frequency": "invalid"}}, store=self.store)

    def test_recurring_slow_run_never_overlaps_and_skips_elapsed_intervals(self):
        item = self.launch(recurrence={"frequency": "interval", "minutes": 1})
        self.now += 601
        self.schedules.process_once()
        self.assertEqual(len(self.starts()), 1)
        self.complete_turn(item["thread_id"])
        self.schedules.process_once()
        self.assertEqual(self.item(item)["run_at"], item["run_at"] + 660)
        self.assertEqual(len(self.starts()), 1)

    def test_recurring_existing_thread_repeats_without_replacing_conversation(self):
        thread_id = self.existing_thread()
        item = self.schedules.create(self.target_body(thread_id, recurrence={"frequency": "interval", "minutes": 5}))
        self.now = item["run_at"]
        self.schedules.process_once()
        self.complete_turn(thread_id)
        self.schedules.process_once()
        self.now = self.item(item)["run_at"]
        self.schedules.process_once()
        self.assertEqual(len(self.starts()), 2)
        self.assertEqual({start["threadId"] for start in self.starts()}, {thread_id})
        self.assertEqual(self.item(item)["state"], "running")

    def test_recurring_pause_resume_skips_downtime_and_preserves_paused_edits(self):
        item = self.schedules.create(self.body(recurrence={"frequency": "interval", "minutes": 10}))
        item = self.schedules.change(item["id"], {"action": "pause", "version": item["version"]})
        self.now += 1200
        self.schedules.process_once()
        self.assertFalse(self.starts())
        item = self.schedules.change(item["id"], {**self.body(recurrence=item["recurrence"]), "action": "edit", "version": item["version"]})
        self.assertEqual(item["state"], "paused")
        self.now += 1201
        item = self.schedules.change(item["id"], {"action": "resume", "version": item["version"]})
        self.assertEqual(item["state"], "scheduled")
        self.assertGreater(item["run_at"], self.now)
        self.assertLessEqual(item["run_at"] - self.now, 600)
        self.now = item["run_at"]
        self.sessions.rpc.fast_turn = True
        self.schedules.process_once()
        self.item(item)  # Flush the asynchronous completion before the next tick.
        self.schedules.process_once()
        self.assertEqual(self.item(item)["last_run"]["state"], "completed")
        self.assertEqual(len(self.starts()), 1)

    def test_pause_or_cancel_while_running_keeps_the_result_and_stops_future_runs(self):
        for action, status in [("pause", "paused"), ("cancel", "cancelled")]:
            item = self.launch(recurrence={"frequency": "interval", "minutes": 1})
            changed = self.schedules.change(item["id"], {"action": action, "version": item["version"]})
            self.assertEqual(changed["state"], "running")
            self.complete_turn(item["thread_id"])
            self.schedules.process_once()
            changed = self.item(item)
            self.assertEqual(changed["state"], status)
            self.assertEqual(changed["last_run"]["state"], "completed")
            before = len(self.starts())
            self.now += 600
            self.schedules.process_once()
            self.assertEqual(len(self.starts()), before)

    def test_resume_after_finished_run_clears_old_turn_before_fast_completion(self):
        item = self.launch(recurrence={"frequency": "interval", "minutes": 1})
        self.schedules.change(item["id"], {"action": "pause", "version": item["version"]})
        self.complete_turn(item["thread_id"])
        self.schedules.process_once()
        item = self.item(item)
        item = self.schedules.change(item["id"], {"action": "resume", "version": item["version"]})
        self.assertIsNone(item["turn_id"])
        self.now = item["run_at"]
        self.sessions.rpc.fast_turn = True
        self.schedules.process_once()
        self.item(item)
        self.schedules.process_once()
        self.assertEqual(self.item(item)["state"], "scheduled")
        self.assertEqual(self.item(item)["last_run"]["state"], "completed")

    def test_recurring_sleep_and_restart_skip_missed_runs_without_catchup(self):
        item = self.schedules.create(self.body(recurrence={"frequency": "interval", "minutes": 1}))
        self.sessions.close()
        self.now += 601
        restarted = self.runtime()
        restarted.schedules.process_once()
        item = self.item(item)
        self.assertEqual(item["state"], "scheduled")
        self.assertEqual(item["last_run"]["state"], "missed")
        self.assertEqual(item["run_at"], self.now + 59)
        self.assertFalse(self.starts(restarted))
        self.now = item["run_at"]
        restarted.schedules.process_once()
        self.assertEqual(len(self.starts(restarted)), 1)

    def test_recurring_uncertain_delivery_stays_for_review_without_repetition(self):
        with patch.object(self.sessions, "send", side_effect=CoordinationError("Reply lost")):
            item = self.launch(recurrence={"frequency": "interval", "minutes": 1})
        self.now += 600
        self.schedules.process_once()
        self.assertEqual(self.item(item)["state"], "review")
        self.assertEqual(self.item(item)["last_run"]["state"], "review")
        self.assertFalse(self.starts())

    def test_recurring_failed_turn_records_failure_and_continues_at_next_time(self):
        item = self.launch(recurrence={"frequency": "daily"})
        self.complete_turn(item["thread_id"], "failed")
        self.schedules.process_once()
        item = self.item(item)
        self.assertEqual(item["state"], "scheduled")
        self.assertEqual(item["last_run"]["state"], "failed")

    def test_recurring_two_runtimes_claim_each_occurrence_once(self):
        item = self.schedules.create(self.body(recurrence={"frequency": "interval", "minutes": 1}))
        other = self.runtime()
        self.sessions.rpc.fast_turn = other.rpc.fast_turn = True
        self.now = item["run_at"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda runtime: runtime.schedules.process_once(), [self.sessions, other]))
        self.assertEqual(len(self.starts()) + len(self.starts(other)), 1)
        self.assertTrue(other.persistence.flush())
        self.item(item)
        self.schedules.process_once()
        self.assertEqual(self.item(item)["state"], "scheduled")

    def test_old_database_migrates_recurrence_columns_without_changing_one_time_jobs(self):
        from agent_coord.schedules import ScheduledPrompts
        item = self.schedules.create(self.body())
        with self.store._connection() as db:
            for column in ("recurrence_json", "recurrence_status", "anchor_at"):
                db.execute("ALTER TABLE scheduled_prompts DROP COLUMN " + column)
        self.sessions.schedules = self.schedules = ScheduledPrompts(self.sessions)
        self.assertIsNone(self.item(item)["recurrence"])
        self.assertEqual(self.item(item)["state"], "scheduled")

    def test_weekday_job_requires_its_first_run_on_a_weekday(self):
        self.now = 1793448000  # Saturday, October 31, 2026.
        with self.assertRaisesRegex(CoordinationError, "weekday"):
            self.schedules.create(self.body(recurrence={"frequency": "weekdays"}))

    def test_stale_recovery_cannot_overwrite_a_later_recurring_run(self):
        item = self.launch(recurrence={"frequency": "interval", "minutes": 1})
        with self.store._connection() as db:
            old = dict(self.schedules._get(db, item["id"]))
        self.complete_turn(item["thread_id"])
        self.schedules.process_once()
        self.now = self.item(item)["run_at"]
        self.schedules.process_once()
        latest = self.item(item)
        self.schedules._recover(old)
        self.assertEqual(self.item(item)["state"], "running")
        self.assertEqual(self.item(item)["turn_id"], latest["turn_id"])


class ScheduleHTTPTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        store = CoordinationStore(self.root / "state.sqlite3")
        sessions = BrowserSessions(store, str(self.root), rpc_factory=FakeCodex)
        self.server = make_ui_server(store, port=0, cwd=str(self.root), browser_sessions=sessions)
        worker = threading.Thread(target=self.server.serve_forever, daemon=True); worker.start()
        def close():
            self.server.shutdown(); self.server.server_close(); worker.join(5)
        self.addCleanup(close)
        self.url = "http://127.0.0.1:" + str(self.server.server_address[1])
        self.token = self.request("/api/browser/config")["token"]

    def request(self, path, body=None, token=None):
        headers = {"Content-Type": "application/json"}
        if token: headers["X-Agent-Coord-Token"] = token
        request = urllib.request.Request(self.url + path, data=json.dumps(body).encode() if body is not None else None, headers=headers)
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.load(response)

    def test_routes_require_csrf_and_persist_cancelled_schedule(self):
        import time
        body = {"id": str(uuid.uuid4()), "message": "Test schedule", "run_at": time.time() + 600,
                "timezone": "UTC", "settings": {"cwd": str(self.root), "model": "available-model", "effort": "high"}}
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/browser/schedules", body)
        self.assertEqual(error.exception.code, 403)
        error.exception.close()
        created = self.request("/api/browser/schedules", body, self.token)
        self.assertEqual(self.request("/api/browser/schedules")["data"][0]["id"], created["id"])
        cancelled = self.request("/api/browser/schedules/" + created["id"], {"action": "cancel", "version": created["version"]}, self.token)
        self.assertEqual(cancelled["state"], "cancelled")
        with urllib.request.urlopen(self.url + "/schedules.js", timeout=5) as response:
            self.assertIn(b"ScheduledPromptsUI", response.read())

    @unittest.skipUnless(shutil.which("node"), "Node is required for browser tests")
    def test_browser_controls(self):
        result = subprocess.run(["node", "--test", str(Path(__file__).with_name("test_web_schedules.js"))], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_recurring_http_schedule_exposes_capability_and_pause_resume(self):
        import time
        self.assertTrue(self.request("/api/browser/config")["recurringSchedules"])
        body = {"id": str(uuid.uuid4()), "message": "Review progress", "run_at": time.time() + 600,
                "timezone": "UTC", "settings": {"cwd": str(self.root), "model": "available-model"},
                "recurrence": {"frequency": "daily"}}
        item = self.request("/api/browser/schedules", body, self.token)
        self.assertEqual(item["recurrence"], {"frequency": "daily"})
        for action, state in [("pause", "paused"), ("resume", "scheduled"), ("cancel", "cancelled")]:
            item = self.request("/api/browser/schedules/" + item["id"], {"action": action, "version": item["version"]}, self.token)
            self.assertEqual(item["state"], state)


if __name__ == "__main__":
    unittest.main()
