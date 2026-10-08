from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))

from agent_coord.codex_app_server import BrowserSessions
from agent_coord.hook import handle
from agent_coord.store import CoordinationError, CoordinationStore
from agent_coord.ui import make_ui_server
from test_codex_app_server import FakeCodex


class TurnNotificationTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict("os.environ", {"AGENT_COORD_CLIENT": "codex", "AGENT_COORD_ZELLIJ_WAKE": "0", "AGENT_COORD_DELEGATION_ID": ""})
        environment.start()
        self.addCleanup(environment.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = CoordinationStore(self.root / "coord.sqlite3", clock=lambda: 1000.0)
        self.sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(self.sessions.close)

    def hook(self, event, session_id="terminal", **values):
        return handle({"session_id": session_id, "cwd": str(self.root), "hook_event_name": event, **values}, self.store)

    def complete(self, turn_id="one", session_id="terminal"):
        self.hook("UserPromptSubmit", session_id, prompt="Review the change.", turn_id=turn_id)
        self.hook("Stop", session_id, turn_id=turn_id)
        return self.store.threads.completion_cursor()

    def approval(self, thread_id=None, method="item/commandExecution/requestApproval", request_id=42):
        thread_id = thread_id or self.sessions.create({"name": "Approval test"})["session"]["thread_id"]
        self.sessions._event({"id": request_id, "method": method, "params": {"threadId": thread_id}})
        return thread_id, next(reversed(self.sessions.requests))

    def test_all_supported_approvals_notify_without_completing_the_turn(self):
        for index, method in enumerate(("item/commandExecution/requestApproval", "item/fileChange/requestApproval", "item/permissions/requestApproval")):
            self.approval(method=method, request_id=index)
        self.approval(method="item/tool/requestUserInput", request_id=4)
        self.approval(method="mcpServer/elicitation/request", request_id=5)
        events = self.sessions.approval_notifications()
        self.assertEqual(len(events), 3)
        self.assertEqual({event["status"] for event in events}, {"approval"})
        self.assertTrue(all(event["title"] == "Approval test" for event in events))
        self.assertEqual(self.store.threads.completion_cursor(), 0)

    def test_approval_claim_is_atomic_and_resolved_requests_stay_quiet(self):
        thread_id, key = self.approval()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.sessions.claim_approval_notification(key), range(8)))
        self.assertEqual(sum(results), 1)
        self.assertEqual(self.sessions.approval_notifications(), [])
        self.sessions.answer(thread_id, key, {"decision": "decline"})
        self.assertFalse(self.sessions.claim_approval_notification(key))
        _, resolved = self.approval(request_id=43)
        self.sessions._event({"method": "serverRequest/resolved", "params": {"requestId": 43}})
        self.assertFalse(self.sessions.claim_approval_notification(resolved))
        self.assertEqual(self.sessions.approval_notifications(), [])

    def test_pending_approval_survives_event_replay_gaps_and_fresh_connections(self):
        _, key = self.approval()
        for index in range(1505):
            self.sessions._publish("progress", {"index": index})
        self.assertTrue(self.sessions.events_after(0, timeout=0)["reset"])
        self.assertEqual(self.sessions.approval_notifications()[0]["request_key"], key)
        self.assertEqual(self.sessions.approval_notifications()[0]["request_key"], key)

    def test_approval_claims_filter_workspaces_closed_threads_and_nonapprovals(self):
        thread_id, key = self.approval()
        self.store.threads.update(thread_id, attention="archived")
        self.assertEqual(self.sessions.approval_notifications(), [])
        self.assertFalse(self.sessions.claim_approval_notification(key))
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        other = BrowserSessions(self.store, outside.name, rpc_factory=FakeCodex)
        self.addCleanup(other.close)
        other.rpc.threads.update(self.sessions.rpc.threads)
        outside_thread = other.create({})["session"]["thread_id"]
        self.sessions.rpc.threads.update(other.rpc.threads)
        _, outside_key = self.approval(outside_thread)
        self.assertEqual(self.sessions.approval_notifications(), [])
        with self.assertRaises(CoordinationError):
            self.sessions.claim_approval_notification(outside_key)
        _, question = self.approval(method="item/tool/requestUserInput")
        self.assertFalse(self.sessions.claim_approval_notification(question))
        for invalid in (None, 42, ""):
            with self.assertRaises(CoordinationError):
                self.sessions.claim_approval_notification(invalid)

    def test_short_terminal_turns_survive_reopen_and_repeated_stop(self):
        self.hook("SessionStart")
        self.assertEqual(self.store.threads.completion_cursor(), 0)
        self.complete()
        self.hook("Stop", turn_id="one")
        self.complete("two")
        self.hook("SessionEnd")
        reopened = CoordinationStore(self.store.database_path)
        events = reopened.threads.completions_after(0)["events"]
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["title"], "Review the change.")
        self.assertEqual(events[0]["status"], "completed")
        self.assertIsNone(events[0]["project_name"])
        self.assertIsNone(events[0]["repository_name"])

    def test_no_turn_ids_still_distinguish_turns_at_the_same_clock_time(self):
        self.complete(None)
        self.hook("Stop")
        self.complete(None)
        self.assertEqual(len(self.store.threads.completions_after(0)["events"]), 2)

    def test_startup_exit_and_stale_completions_do_not_notify(self):
        self.hook("SessionStart")
        self.hook("Stop")
        self.hook("SessionEnd")
        self.hook("UserPromptSubmit", turn_id="current")
        self.hook("Stop", turn_id="old")
        self.assertEqual(self.store.threads.completion_cursor(), 0)
        self.hook("Stop", turn_id="current")
        self.assertEqual(len(self.store.threads.completions_after(0)["events"]), 1)

    def test_browser_completion_is_authoritative_over_hooks(self):
        thread_id = self.sessions.create({"name": "Browser turn"})["session"]["thread_id"]
        self.sessions.send(thread_id, {"message": "Do the work."})
        turn = self.sessions.rpc.threads[thread_id]["turns"][-1]
        self.hook("UserPromptSubmit", thread_id, prompt="Do the work.")
        self.hook("Stop", thread_id)
        self.assertEqual(self.store.threads.completion_cursor(), 0)
        self.assertEqual(self.store.threads.get(thread_id)["turn_id"], turn["id"])
        event = {"method": "turn/completed", "params": {"threadId": thread_id, "turn": {**turn, "status": "completed"}}}
        self.sessions._event(event)
        self.sessions._event(event)
        self.hook("Stop", thread_id)
        events = self.sessions.completions_after(0)["events"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["title"], "Browser turn")

    def test_failed_and_interrupted_browser_turns_are_not_successes(self):
        for status in ("failed", "interrupted"):
            thread_id = self.sessions.create({})["session"]["thread_id"]
            self.sessions.send(thread_id, {"message": "Start."})
            turn = self.sessions.rpc.threads[thread_id]["turns"][-1]
            self.sessions._event({"method": "turn/completed", "params": {"threadId": thread_id, "turn": {**turn, "status": status}}})
        events = self.sessions.completions_after(0)["events"]
        self.assertEqual([event["status"] for event in events], ["failed", "interrupted"])
        self.assertFalse(self.sessions.claim_notification(events[1]["id"]))

    def test_atomic_claim_prevents_duplicate_notifications_across_tabs_and_restart(self):
        event_id = self.complete()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.sessions.claim_notification(event_id), range(8)))
        self.assertEqual(sum(results), 1)
        reopened = CoordinationStore(self.store.database_path)
        self.assertFalse(reopened.threads.claim_notification(event_id))

    def test_workspace_and_archived_filters_also_apply_to_notification_claims(self):
        first = self.complete()
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        self.store.register(session_id="outside", client="codex", cwd=outside.name)
        self.store.threads.start_turn("outside", turn_id="outside")
        self.store.threads.finish_turn("outside")
        outside_id = self.store.threads.completion_cursor()
        self.assertEqual([e["id"] for e in self.sessions.completions_after(0)["events"]], [first])
        with self.assertRaises(CoordinationError):
            self.sessions.claim_notification(outside_id)
        self.store.threads.update("terminal", attention="archived")
        batch = self.sessions.completions_after(0)
        self.assertEqual(batch["events"], [])
        self.assertEqual(batch["seq"], outside_id)
        self.assertFalse(self.sessions.claim_notification(first))

    def test_fresh_cursor_skips_history_and_bounded_batches_do_not_skip_turns(self):
        cursor = self.complete()
        for index in range(105):
            self.store.threads.start_turn("terminal", turn_id=str(index))
            self.store.threads.finish_turn("terminal")
        first = self.sessions.completions_after(cursor)
        second = self.sessions.completions_after(first["seq"])
        self.assertEqual(len(first["events"]), 100)
        self.assertEqual(len(second["events"]), 5)
        self.assertEqual(self.sessions.completions_after(second["seq"])["events"], [])

    def test_upgrade_preserves_existing_threads_without_synthetic_completions(self):
        self.hook("SessionStart")
        with self.store._connection() as db:
            db.execute("ALTER TABLE work_threads DROP COLUMN turn_key")
        reopened = CoordinationStore(self.store.database_path)
        self.assertEqual(reopened.threads.get("terminal")["title"], "New thread")
        reopened.threads.finish_turn("terminal")
        self.assertEqual(reopened.threads.completion_cursor(), 0)
        reopened.threads.start_turn("terminal", turn_id="new")
        reopened.threads.finish_turn("terminal")
        self.assertEqual(len(reopened.threads.completions_after(0)["events"]), 1)


class NotificationHTTPTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = CoordinationStore(self.root / "coord.sqlite3")
        self.store.register(session_id="terminal", client="codex", cwd=str(self.root))
        self.sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.server = make_ui_server(self.store, port=0, cwd=str(self.root), browser_sessions=self.sessions)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self.stop_server)
        self.url = "http://127.0.0.1:" + str(self.server.server_address[1])
        with urllib.request.urlopen(self.url + "/api/browser/config", timeout=3) as response:
            self.config = json.load(response)

    def stop_server(self):
        self.sessions.close()
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(2)

    def complete(self, turn_id):
        self.store.threads.start_turn("terminal", turn_id=turn_id)
        self.store.threads.finish_turn("terminal")
        return self.store.threads.completion_cursor()

    def event_batch(self, path, last_id=None):
        request = urllib.request.Request(self.url + path, headers={"Last-Event-ID": last_id} if last_id else {})
        with urllib.request.urlopen(request, timeout=3) as response:
            while True:
                line = response.readline().decode().strip()
                if line.startswith("data: "):
                    return json.loads(line[6:])

    def test_stream_delivers_external_completions_and_resumes_without_replay(self):
        self.assertEqual(self.config["completionCursor"], 0)
        first = self.complete("first")
        batch = self.event_batch("/api/browser/events?completion_after=0")
        self.assertEqual([event["id"] for event in batch["completions"]], [first])
        second = self.complete("second")
        resumed = self.event_batch("/api/browser/events?completion_after=0", f'{batch["seq"]}:{batch["completionSeq"]}')
        self.assertEqual([event["id"] for event in resumed["completions"]], [second])
        fresh = self.event_batch("/api/browser/events")
        self.assertEqual(fresh["completions"], [], "a new page must not replay old completions")
        self.assertEqual(self.sessions.rpc.calls, [], "terminal notifications do not launch app-server")

    def test_claim_endpoint_is_atomic_and_requires_the_ui_token(self):
        event_id = self.complete("first")
        url = self.url + "/api/browser/notifications/claim"
        body = json.dumps({"completion_id": event_id}).encode()
        with self.assertRaises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}), timeout=3)
        self.assertEqual(denied.exception.code, 403)
        denied.exception.close()
        for expected in (True, False):
            request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "X-Agent-Coord-Token": self.config["token"]})
            with urllib.request.urlopen(request, timeout=3) as response:
                self.assertEqual(json.load(response)["claimed"], expected)

    def test_stream_includes_pending_approvals_on_a_fresh_connection(self):
        thread_id = self.sessions.create({"name": "Needs approval"})["session"]["thread_id"]
        self.sessions._event({"id": 42, "method": "item/fileChange/requestApproval", "params": {"threadId": thread_id}})
        batch = self.event_batch("/api/browser/events", str(self.sessions.sequence))
        self.assertEqual(batch["events"], [])
        self.assertEqual(batch["approvals"][0]["thread_id"], thread_id)
        key = batch["approvals"][0]["request_key"]
        url = self.url + "/api/browser/notifications/claim"
        body = json.dumps({"request_key": key}).encode()
        with self.assertRaises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}), timeout=3)
        self.assertEqual(denied.exception.code, 403)
        denied.exception.close()
        for expected in (True, False):
            request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "X-Agent-Coord-Token": self.config["token"]})
            with urllib.request.urlopen(request, timeout=3) as response:
                self.assertEqual(json.load(response)["claimed"], expected)

    def test_invalid_stream_cursors_fail_before_stream_headers(self):
        for cursor in ("bad", "-1", "1:bad", "1:-1", "1:2:3"):
            request = urllib.request.Request(self.url + "/api/browser/events", headers={"Last-Event-ID": cursor})
            with self.assertRaises(urllib.error.HTTPError) as invalid:
                urllib.request.urlopen(request, timeout=3)
            self.assertEqual(invalid.exception.code, 400)
            invalid.exception.close()


class NotificationJavaScriptTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is needed only for frontend unit tests")
    def test_notification_client(self):
        result = subprocess.run([shutil.which("node"), "--test", str(Path(__file__).with_name("test_web_notifications.js"))],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
