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
        env = dict(os.environ, AGENT_COORD_SESSION_ID="dispatcher")
        result = subprocess.run([str(PLUGIN_SCRIPTS / "agent-coord"), "--db", str(self.store.database_path),
                                 "thread", *arguments], cwd=self.root, env=env,
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

    def test_cli_creation_is_queued_then_confirmed_with_identity_settings_and_link(self):
        receipt = self.cli("create", "--name", "Research specialist", "--yolo", "--model", "available-model",
                           "--effort", "high", "--request-id", "research-1", "--wait", "0", "-", stdin="Question\nwith literal $HOME and `text`.")
        self.assertEqual(receipt["status"], "queued")
        self.assertIsNone(receipt["thread_id"])
        self.worker.process_once()
        receipt = self.cli("request-status", "--request-id", "research-1")
        self.assertEqual(receipt["status"], "completed")
        record = receipt["result"]
        self.assertEqual((record["model"], record["effort"], record["yolo"]), ("available-model", "high", True))
        self.assertEqual(record["session_id"], receipt["thread_id"])
        self.assertTrue(record["url"].startswith("agentcoord://thread/" + record["thread_id"]))
        self.assertIn("literal $HOME", self.store.threads.get(record["thread_id"])["original_request"])
        self.assertEqual(self.cli("settings", "--session-id", record["thread_id"])["model"], "available-model")

    def test_duplicate_request_survives_restart_without_duplicate_agent_or_message(self):
        payload = dict(cwd=str(self.root), name="Agent", prompt="Question", client="codex")
        first = self.control.request("create", "dispatcher", payload, request_id="stable")
        self.worker.process_once()
        control = AppControl(CoordinationStore(self.store.database_path))
        repeated = control.request("create", "dispatcher", payload, request_id="stable")
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
