import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
import uuid
import urllib.error
import urllib.request
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"
sys.path.insert(0, str(SCRIPTS))
from agent_coord.store import CoordinationStore
from agent_coord.codex_app_server import BrowserSessions
from agent_coord.ui import make_ui_server
from test_codex_app_server import FakeCodex


class ActivityAPITests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.store = CoordinationStore(self.root / "state.sqlite3", clock=lambda: time.time() - 60)
        self.sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.store.clock = time.time
        self.thread = self.sessions.create({})["session"]["thread_id"]
        self.server = make_ui_server(self.store, host="127.0.0.1", port=0, cwd=str(self.root), browser_sessions=self.sessions)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self.close)
        self.url = "http://127.0.0.1:" + str(self.server.server_address[1])
        self.token = self.request("config")[1]["token"]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(2)
        self.sessions.close()

    def request(self, route, body=None, *, token=None):
        headers = {}
        if body is not None:
            headers.update({"Content-Type": "application/json", "X-Agent-Coord-Token": self.token if token is None else token})
        req = urllib.request.Request(self.url + "/api/browser/" + route,
            data=None if body is None else json.dumps(body).encode(), headers=headers)
        try:
            response = urllib.request.urlopen(req, timeout=5)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.status, json.loads(response.read())

    def event(self, kind="visit_started", identity="start", **changes):
        return {"event_id": identity, "kind": kind, "thread_id": self.thread,
                "occurred_at": self.store.clock() - 5, "window_id": "window", "visit_id": "visit",
                "source": "navigation", **changes}

    def test_provider_turn_events_are_queryable_and_dashboard_can_exclude_them(self):
        self.sessions.rpc.fast_turn = True
        self.store.threads.update(self.thread, title="Worker")
        self.assertEqual(self.request("sessions/" + self.thread + "/messages", {"message": "Private task"})[0], 200)
        self.sessions.persistence.flush()
        report = self.request("activity?start=0&include_turns=true")[1]
        turns = [e for e in report["events"] if e["kind"].startswith("turn_")]
        self.assertEqual([e["kind"] for e in turns], ["turn_started", "turn_ended"])
        self.assertEqual({e["turn_id"] for e in turns}, {"turn-0"})
        self.assertEqual(turns[1]["details"]["status"], "completed")
        self.assertEqual(turns[0]["details"]["trigger"], "human")
        self.assertNotIn("Private task", json.dumps(turns))
        dashboard = self.request("activity?start=0")[1]
        self.assertEqual([e["kind"] for e in dashboard["events"]], ["message_submitted"])
        self.assertEqual(report["totals"], dashboard["totals"])
        self.assertEqual(self.request("activity?start=0&include_turns=0")[0], 400)
        for kind in ("turn_started", "turn_ended"):
            self.assertEqual(self.request("activity/events", {"events": [self.event(kind=kind)]})[0], 400)

    def test_scheduled_turn_attribution_uses_exact_turn_and_survives_completion(self):
        self.sessions.rpc.fast_turn = True
        item = self.sessions.schedules.create({"id": str(uuid.uuid4()), "message": "Scheduled work", "run_at": time.time() + 10,
                                             "timezone": "America/Chicago", "settings": {"cwd": str(self.root), "model": "available-model"}})
        self.store.clock = lambda: item["run_at"]
        self.sessions.schedules.process_once()
        self.sessions.persistence.flush()
        self.store.clock = lambda: item["run_at"] + 1
        self.assertEqual(self.sessions.schedules.list()[0]["state"], "completed")
        events = self.request("activity?start=0&include_turns=true")[1]["events"]
        turns = [e for e in events if e["kind"].startswith("turn_")]
        self.assertEqual(len(turns), 2)
        self.assertEqual([e["details"]["trigger"] for e in turns], ["schedule", "schedule"])

    def test_inbox_wake_records_agent_execution_without_human_input(self):
        self.sessions.rpc.fast_turn = True
        self.store.register(session_id="sender", client="claude", cwd=str(self.root))
        self.store.send_message(sender_session_id="sender", recipient_session_id=self.thread,
                                body="An assignment", reply_required=False)
        self.sessions.inbox_wake.process_once()
        self.sessions.persistence.flush()
        report = self.request("activity?start=0&include_turns=true")[1]
        turns = [e for e in report["events"] if e["kind"].startswith("turn_")]
        self.assertEqual([e["details"]["trigger"] for e in turns], ["inbox", "inbox"])
        self.assertEqual(report["totals"]["messages"], 0)
        self.assertEqual(report["groups"], [])

    def test_provider_failure_interruption_and_disconnect_close_recorded_turns(self):
        for status in ("failed", "interrupted", "disconnect"):
            with self.subTest(status=status):
                thread = self.sessions.create({"name": "Worker"})["session"]["thread_id"]
                turn = self.sessions.send(thread, {"message": "Work"})["turn"]
                if status == "disconnect":
                    self.sessions._event({"method": "bridge/disconnected", "params": {}})
                else:
                    self.sessions._event({"method": "turn/completed", "params": {
                        "threadId": thread, "turn": {**turn, "status": status}}})
                self.sessions.persistence.flush()
                events = self.request("activity?start=0&include_turns=true&thread_id=" + thread)[1]["events"]
                self.assertEqual([e["kind"] for e in events], ["turn_started", "turn_ended"])
                self.assertEqual(events[1]["details"]["status"], "failed" if status == "disconnect" else status)

    def test_visit_upload_is_authenticated_idempotent_and_reconciles_summary(self):
        event = self.event()
        self.assertEqual(self.request("activity/events", {"events": [event]}, token="")[0], 403)
        self.assertEqual(self.request("activity/events", {"events": [event]})[0], 200)
        self.assertEqual(self.request("activity/events", {"events": [event]})[0], 200)
        end = self.event("visit_ended", "end", occurred_at=self.store.clock() - 1)
        self.assertEqual(self.request("activity/events", {"events": [end]})[0], 200)
        report = self.request("activity?start=0")[1]
        self.assertEqual(report["totals"]["visits"], 1)
        self.assertEqual(report["totals"]["visits_without_message"], 1)
        self.assertEqual(len(report["events"]), 2)

    def test_browser_cannot_fabricate_accepted_messages_and_restore_is_system(self):
        for changes in ({"kind": "message_submitted"}, {"actor": "human"}, {"source": "unknown"}, {"thread_id": []}):
            with self.subTest(changes=changes):
                self.assertEqual(self.request("activity/events", {"events": [self.event(**changes)]})[0], 400)
        self.assertEqual(self.request("activity/events", {"events": [self.event(source="restore")]})[0], 200)
        report = self.request("activity?start=0")[1]
        self.assertEqual(report["totals"]["visits"], 0)
        self.assertEqual(report["events"][0]["actor"], "system")

    def test_navigation_surface_is_recorded_validated_and_normalized(self):
        events = [self.event(identity="sidebar", surface="sidebar"),
                  self.event(identity="future", visit_id="later", surface="future-panel")]
        self.assertEqual(self.request("activity/events", {"events": events})[0], 200)
        self.assertEqual(self.request("activity/events", {"events": events[:1]})[0], 200)
        details = {event["event_id"]: event["details"] for event in self.request("activity?start=0")[1]["events"]}
        self.assertEqual(details, {"sidebar": {"surface": "sidebar"}, "future": {"surface": "other"}})
        for changes in ({"source": "focus", "surface": "sidebar"}, {"surface": ["sidebar"]}, {"surface": None}):
            with self.subTest(changes=changes):
                self.assertEqual(self.request("activity/events", {"events": [self.event(identity="bad", **changes)]})[0], 400)

    def test_no_provider_calls_for_activity_or_saved_evidence(self):
        with patch.object(self.sessions.rpc, "request", side_effect=AssertionError("Must not wake")):
            self.assertEqual(self.request("activity?start=0")[0], 200)
            status, history = self.request("activity/history?thread_id=" + self.thread)
        self.assertEqual(status, 200)
        self.assertIn(history["coverage"], {"unavailable", "saved"})

    def test_workspace_boundary_and_bad_query_rejected(self):
        self.store.register(session_id="outside", client="codex", cwd=str(self.root.parent))
        self.store.threads.ensure("outside")
        self.sessions.activity.record("message_submitted", "outside")
        self.assertEqual(self.request("activity?start=0")[1]["totals"]["messages"], 0)
        self.assertEqual(self.request("activity/history?thread_id=outside")[0], 400)
        self.assertEqual(self.request("activity/events", {"events": [self.event(thread_id="outside")]})[0], 400)
        for query in ("start=nan", "start=0&start=1", "limit=1.5", "ignored=true"):
            self.assertEqual(self.request("activity?" + query)[0], 400)
        self.assertEqual(self.request("activity/history")[0], 400)

    def test_pause_and_clear_are_explicit_authenticated_operations(self):
        self.assertEqual(self.request("activity/settings", {"enabled": False})[0], 200)
        self.assertEqual(self.request("activity/events", {"events": [self.event()]})[0], 200)
        self.assertEqual(self.request("activity?start=0")[1]["events"], [])
        self.assertEqual(self.request("activity/settings", {"enabled": True})[0], 200)
        self.assertEqual(self.request("activity/events", {"events": [self.event()]})[0], 200)
        self.assertEqual(self.request("activity/clear", {}, token="")[0], 403)
        self.assertEqual(self.request("activity/clear", {})[0], 200)
        self.assertEqual(self.request("activity?start=0")[1]["events"], [])

    def test_http_submission_stop_and_answer_are_recorded(self):
        base = "sessions/" + self.thread
        self.assertEqual(self.request(base + "/messages", {"message": "Work", "activity": {"event_id": "message"}})[0], 200)
        self.assertEqual(self.request(base + "/interrupt", {})[0], 200)
        self.assertEqual(self.request("activity?start=0")[1]["totals"]["messages"], 1)
        self.assertEqual(self.request("activity?start=0")[1]["totals"]["decisions"], 1)
        self.sessions._event({"id": 42, "method": "item/commandExecution/requestApproval",
            "params": {"threadId": self.thread, "command": "touch project.txt"}})
        key = self.sessions.pending_requests(self.thread)[0]["key"]
        self.assertEqual(self.request(base + "/requests/" + key, {"decision": "decline"})[0], 200)
        self.assertEqual(self.request(base + "/requests/" + key, {"decision": "decline"})[0], 400)
        self.assertEqual(self.request("activity?start=0")[1]["totals"]["decisions"], 2)

    def test_review_creation_over_http_and_retry(self):
        self.sessions.rpc.fast_turn = True
        body = {"filters": {"start": 0}, "request_id": "review-http", "cwd": str(self.root)}
        status, receipt = self.request("activity/review", body)
        self.assertEqual(status, 200)
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(self.request("activity/review", body)[1], receipt)
        self.assertEqual(sum(m == "turn/start" for m, _ in self.sessions.rpc.calls), 1)
