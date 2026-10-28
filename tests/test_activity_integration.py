"""Verify activity at actual accepted-input boundaries, independent of providers."""
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"
sys.path.insert(0, str(SCRIPTS))
from agent_coord.activity_api import create_activity_review, read_activity_history
from agent_coord.codex_app_server import BrowserSessions
from agent_coord.store import CoordinationStore, CoordinationError
from agent_coord.checkpoint_mcp import query_activity
from test_codex_app_server import FakeCodex


class ActivityIntegrationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.store = CoordinationStore(self.root / "state.sqlite3")
        self.sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(self.sessions.close)
        self.thread = self.sessions.create({})["session"]["thread_id"]

    def report(self):
        return self.sessions.activity.query(start=0, end=self.store.clock() + 1)

    def test_direct_input_counts_once_and_internal_delivery_does_not(self):
        self.sessions.rpc.fast_turn = True
        self.store.threads.update(self.thread, title="UI task")
        for index in range(25):
            self.sessions.submit(self.thread, {"message": "Fix one thing", "activity": {"event_id": "input-" + str(index)}})
        for _ in range(25):
            self.sessions.send(self.thread, {"message": "An internal assignment"})
        result = self.report()
        self.assertEqual(result["totals"]["messages"], 25)
        self.assertTrue(all("Fix one thing" not in json.dumps(event) for event in result["events"]))

    def test_queue_acceptance_counts_before_delivery_and_replay_does_not(self):
        self.sessions.rpc.fast_turn = True
        with patch.object(self.sessions.queue, "wake"):
            receipt = self.sessions.submit(self.thread, {"message": "Follow-up", "activity": {"event_id": "queued"}}, queued=True)
        self.assertTrue(receipt["queued"])
        self.assertEqual(self.report()["totals"]["messages"], 1)
        self.sessions.send(self.thread, {"message": "Follow-up"}, start_only=True)
        self.assertEqual(self.report()["totals"]["messages"], 1)

    def test_rejected_input_and_invalid_metadata_do_not_create_events(self):
        self.sessions.rpc.fail_turn = True
        with self.assertRaises(CoordinationError):
            self.sessions.submit(self.thread, {"message": "Rejected"})
        calls = len(self.sessions.rpc.calls)
        with self.assertRaises(CoordinationError):
            self.sessions.submit(self.thread, {"message": "Invalid", "activity": {"event_id": []}})
        self.assertEqual(len(self.sessions.rpc.calls), calls)
        self.assertEqual(self.report()["totals"]["messages"], 0)

    def test_durable_request_retries_without_activity_metadata_count_once(self):
        for result in ({"dispatched": True, "receipt": {"id": 1}}, {"review": {"id": "run"}}):
            with self.subTest(result=result), patch.object(self.sessions, "_submit", return_value=result):
                body = {"message": "One request", "requestId": str(result)}
                self.sessions.submit(self.thread, body)
                self.sessions.submit(self.thread, body)
        self.assertEqual(self.report()["totals"]["messages"], 2)

    def test_recording_failure_does_not_report_an_accepted_prompt_as_failed(self):
        self.sessions.rpc.fast_turn = True
        with patch("agent_coord.activity_api.ActivityStore.record", side_effect=sqlite3.OperationalError("database is locked")):
            with self.assertWarnsRegex(RuntimeWarning, "action was accepted"):
                response = self.sessions.submit(self.thread, {"message": "Accepted"})
        self.assertIn("turn", response)
        self.assertEqual(sum(method == "turn/start" for method, _ in self.sessions.rpc.calls), 1)

    def test_commands_and_organization_updates_are_distinct_from_prompts(self):
        self.sessions.submit(self.thread, {"message": "/help"})
        self.sessions.update_work_thread(self.thread, {"domain": "web"})
        result = self.report()
        self.assertEqual(result["totals"]["messages"], 0)
        self.assertEqual(result["totals"]["decisions"], 1)
        self.assertEqual([e["kind"] for e in result["events"]], ["decision_submitted", "organization_changed"])

    def save_history(self, history):
        with self.store._connection() as db:
            db.execute("INSERT OR REPLACE INTO browser_history VALUES (?, ?)", (self.thread, json.dumps(history)))

    def test_history_pages_preserve_long_text_without_waking_provider(self):
        text = "evidence " * 12000
        self.save_history({"turns": [{"id": "turn", "startedAt": 100, "items": [
            {"type": "userMessage", "id": "input", "content": [{"type": "text", "text": text}]},
            {"type": "agentMessage", "id": "answer", "text": "Done"}]}]})
        with patch.object(self.sessions.rpc, "request", side_effect=AssertionError("must not wake")):
            cursor, recovered = None, ""
            while True:
                page = read_activity_history(self.store, self.thread, limit=2, cursor=cursor)
                recovered += "".join(m["text"] for m in page["messages"] if m["role"] == "user")
                cursor = page["next_cursor"]
                if cursor is None:
                    break
        self.assertEqual(recovered, text)
        self.assertEqual(page["coverage"], "saved")

    def test_history_cursor_rejects_changed_history_and_workspace_escape(self):
        self.save_history({"turns": [{"items": [{"type": "agentMessage", "text": "x" * 100000}]}]})
        page = read_activity_history(self.store, self.thread, limit=1)
        self.save_history({"turns": []})
        with self.assertRaisesRegex(CoordinationError, "changed"):
            read_activity_history(self.store, self.thread, limit=1, cursor=page["next_cursor"])
        with self.assertRaisesRegex(CoordinationError, "workspace"):
            read_activity_history(self.store, self.thread, allowed_cwd=str(self.root / "elsewhere"))

    def test_history_includes_original_dispatch_and_pending_queue_requests(self):
        with patch.object(self.sessions.dispatch, "wake"):
            self.sessions.dispatch.configure(self.thread, {"enabled": True})
            self.sessions.submit(self.thread, {"message": "Audit every navigation inconsistency", "requestId": "route-once"})
        self.sessions.dispatch.configure(self.thread, {"enabled": False})
        with patch.object(self.sessions.queue, "wake"):
            self.sessions.submit(self.thread, {"message": "Check the shared component"}, queued=True)
        with patch.object(self.sessions.rpc, "request", side_effect=AssertionError("must not wake")):
            page = read_activity_history(self.store, self.thread)
        saved = {m["source"]: m["text"] for m in page["messages"]}
        self.assertEqual(saved["dispatch_request"], "Audit every navigation inconsistency")
        self.assertEqual(saved["queued_request"], "Check the shared component")
        self.assertEqual(self.report()["totals"]["messages"], 2)

    def test_review_creation_is_idempotent_and_preserves_cutoff(self):
        self.sessions.rpc.fast_turn = True
        cutoff = self.store.clock()
        body = {"request_id": "review-once", "filters": {"start": cutoff - 86400, "end": cutoff},
                "question": "What could I batch?", "cwd": str(self.root)}
        first = create_activity_review(self.sessions, body)
        second = create_activity_review(self.sessions, body)
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "completed")
        self.assertEqual(first["filters"]["end"], cutoff)
        self.assertEqual(sum(method == "turn/start" for method, _ in self.sessions.rpc.calls), 1)
        prompt = next(params["input"][0]["text"] for method, params in self.sessions.rpc.calls if method == "turn/start")
        self.assertIn("analysis only", prompt)
        self.assertIn("What could I batch?", prompt)
        self.assertIn("--workspace " + str(self.root), prompt)
        self.assertEqual(first["filters"]["workspace"], str(self.root))
        with self.assertRaisesRegex(CoordinationError, "different settings"):
            create_activity_review(self.sessions, {**body, "question": "Changed"})

    def test_review_uncertain_start_preserves_created_thread_and_does_not_retry(self):
        self.sessions.rpc.fail_turn = True
        body = {"request_id": "failed-review", "filters": {"start": 0}, "cwd": str(self.root)}
        first = create_activity_review(self.sessions, body)
        self.assertEqual(first["status"], "uncertain")
        self.assertIsNotNone(first["thread_id"])
        calls = len(self.sessions.rpc.calls)
        self.assertEqual(create_activity_review(self.sessions, body), first)
        self.assertEqual(len(self.sessions.rpc.calls), calls)

    def test_cli_and_mcp_reconcile_with_shared_query(self):
        self.sessions.rpc.fast_turn = True
        self.sessions.submit(self.thread, {"message": "Hello"})
        end = self.store.clock()
        expected = self.sessions.activity.query(start=0, end=end)["totals"]
        self.assertEqual(query_activity({"start": 0, "end": end}, store=self.store)["totals"], expected)
        result = subprocess.run([sys.executable, str(SCRIPTS / "agent-coord"), "--db", str(self.store.database_path),
                                 "activity", "query", "--start", "0", "--end", str(end)],
                                capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)["totals"], expected)


class ActivityFrontendTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is required for activity browser tests")
    def test_browser_activity(self):
        result = subprocess.run(["node", "--test", str(Path(__file__).with_name("test_web_activity.js"))],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
