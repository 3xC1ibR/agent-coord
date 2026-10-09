from __future__ import annotations

import copy
import json
import sys
import tempfile
import threading
import urllib.request
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))
from agent_coord.codex_app_server import BrowserSessions
from agent_coord.dispatch_runtime import CONTEXT_PREFIX
from agent_coord.dispatch_runtime import routing_history
from agent_coord.dispatch_snapshot import read_snapshot
from agent_coord.store import CoordinationError, CoordinationStore
from test_codex_app_server import FakeCodex
from test_browser_providers import claude_factory


class DispatchRuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = CoordinationStore(self.root / "coord.sqlite3")
        self.sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(self.sessions.close)
        self.runtime = self.sessions.dispatch
        for owner in (self.runtime, self.sessions.queue):
            p = patch.object(owner, "wake")
            p.start(); self.addCleanup(p.stop)
        self.dispatcher = self.create("Entry point")
        self.vendor = self.create("Vendor form")
        self.runtime.configure(self.dispatcher, {"enabled": True})

    def create(self, name):
        return self.sessions.create({"name": name})["session"]["thread_id"]

    def submit(self, text="Add multiple vendors", key="one", **extras):
        return self.sessions.submit(self.dispatcher, {"message": text, "requestId": key, **extras})["receipt"]["id"]

    def plan(self, action="reuse", target=None):
        decision = {"action": action, "reason": "test", "thread_id": target or self.vendor}
        with patch("agent_coord.dispatch_runtime.route", return_value={"decision": decision}) as classifier:
            self.assertTrue(self.runtime.process_once())
        return classifier

    def drain(self, target=None):
        self.sessions.queue._dispatch(target or self.vendor)

    def finish(self, target):
        turn = self.sessions.rpc.threads[target]["turns"][-1]
        turn["status"] = "completed"
        self.sessions._event({"method": "turn/completed", "params": {"threadId": target, "turn": copy.deepcopy(turn)}})

    def test_opt_in_snapshot_and_normal_conversations(self):
        self.assertTrue(self.sessions.work_thread(self.dispatcher)["dispatcher"])
        self.assertTrue(next(t for t in read_snapshot(self.store.database_path)["threads"] if t["thread_id"] == self.dispatcher)["dispatcher"])
        self.sessions.submit(self.vendor, {"message": "Direct follow-up"})
        self.assertEqual(self.sessions.rpc.threads[self.vendor]["turns"][0]["items"][0]["content"], [{"type": "text", "text": "Direct follow-up"}])
        self.assertEqual(self.runtime.receipts(self.vendor), [])

    def test_reuse_is_ordinary_user_input_and_receipt_is_not_completion(self):
        identity = self.submit()
        self.plan()
        self.assertEqual(self.runtime.get(identity)["state"], "queued")
        self.assertEqual(self.sessions.rpc.threads[self.dispatcher]["turns"], [])
        self.drain()
        self.assertEqual(self.runtime.get(identity)["state"], "accepted")
        self.assertEqual(self.sessions.rpc.threads[self.vendor]["turns"][0]["items"][0]["content"], [{"type": "text", "text": "Add multiple vendors"}])
        self.assertTrue(self.sessions.read(self.vendor)["running"])

    def test_busy_destination_queues_without_steering(self):
        self.sessions.send(self.vendor, {"message": "Build vendor page"})
        self.submit(); self.plan(); self.drain()
        self.assertEqual(len(self.sessions.rpc.threads[self.vendor]["turns"]), 1)
        self.finish(self.vendor); self.drain()
        self.assertEqual(len(self.sessions.rpc.threads[self.vendor]["turns"]), 2)
        self.assertFalse(any(m == "turn/steer" for m, _ in self.sessions.rpc.calls))

    def test_request_retry_and_queue_retry_never_duplicate(self):
        identity = self.submit(); self.plan(); self.drain()
        self.assertEqual(self.submit(), identity)
        self.assertFalse(self.runtime.process_once())
        self.sessions.queue.enqueue(self.vendor, self.runtime.get(identity)["body"], dispatch_id=identity)
        self.assertEqual(self.sessions.queue.list(self.vendor), [])
        self.assertEqual(len(self.sessions.rpc.threads[self.vendor]["turns"]), 1)
        with self.assertRaisesRegex(CoordinationError, "different input"):
            self.submit("Changed request")

    def test_new_creation_and_immediate_followup_share_destination(self):
        identity = self.submit("Create a crossword generator")
        self.submit("Add custom word lists", "two")
        self.plan("new")
        target = self.runtime.get(identity)["target_id"]
        self.assertNotIn(target, {self.vendor, self.dispatcher})
        classifier = self.plan("reuse", target)
        recent = classifier.call_args.args[1]["recent_routes"]
        self.assertEqual(recent[-1]["thread_id"], target)
        self.assertIn("crossword", recent[-1]["message"])
        self.assertEqual(len(self.sessions.rpc.threads), 3)
        self.assertEqual(len(self.sessions.queue.list(target)), 2)
        self.assertFalse(self.sessions._record(target)["yolo"])

    def test_fallback_catches_up_only_after_provider_accepts(self):
        identity = self.submit(); self.plan(); self.drain()
        fallback = self.submit("Why did you choose that agent?", "two", askDispatcher=True)
        with patch("agent_coord.dispatch_runtime.route", side_effect=AssertionError("Explicit bypass called Jev")):
            self.assertTrue(self.runtime.process_once())
        self.assertEqual(self.runtime.get(fallback)["target_id"], self.dispatcher)
        before = self.runtime.prepare_context(self.dispatcher)
        self.assertIn("Add multiple vendors", before["text"])
        self.assertEqual(before, self.runtime.prepare_context(self.dispatcher))
        self.drain(self.dispatcher)
        content = self.sessions.rpc.threads[self.dispatcher]["turns"][0]["items"][0]["content"]
        self.assertTrue(content[0]["text"].startswith(CONTEXT_PREFIX))
        self.assertIn('"state": "accepted"', content[0]["text"])
        self.assertEqual(content[-1]["text"], "Why did you choose that agent?")
        self.assertNotIn("Add multiple vendors", self.runtime.prepare_context(self.dispatcher)["text"])
        self.assertEqual(self.runtime.get(identity)["state"], "accepted")

    def test_failed_fallback_does_not_consume_catchup_or_retry_send(self):
        identity = self.submit(askDispatcher=True)
        self.runtime.process_once()
        self.sessions.rpc.fail_turn = True
        self.drain(self.dispatcher)
        self.assertEqual(self.runtime.get(identity)["state"], "uncertain")
        self.assertIn("Add multiple vendors", self.runtime.prepare_context(self.dispatcher)["text"])
        with self.assertRaisesRegex(CoordinationError, "uncertain"):
            self.sessions.queue.change(self.dispatcher, {"action": "resume"})
        self.assertEqual(self.submit(askDispatcher=True), identity)

    def test_running_dispatcher_does_not_block_fast_route(self):
        self.sessions.send(self.dispatcher, {"message": "Resolve a question"})
        self.submit(); self.plan(); self.drain()
        self.assertEqual(len(self.sessions.rpc.threads[self.vendor]["turns"]), 1)
        self.assertEqual(len(self.sessions.rpc.threads[self.dispatcher]["turns"]), 1)

    def test_recipient_paused_after_plan_falls_back(self):
        self.submit()
        self.sessions.inbox_wake.pause(self.vendor, "Stopped")
        self.plan()
        self.assertEqual(self.runtime.receipts(self.dispatcher)[0]["target_id"], self.dispatcher)
        self.assertEqual(self.sessions.queue.list(self.vendor), [])

    def test_stop_between_queue_and_delivery_is_respected(self):
        identity = self.submit(); self.plan()
        self.sessions.inbox_wake.pause(self.vendor, "Stopped")
        self.drain()
        self.assertEqual(self.sessions.rpc.threads[self.vendor]["turns"], [])
        self.assertEqual(self.runtime.receipts(self.dispatcher)[0]["state"], "paused")
        self.assertEqual(self.runtime.get(identity)["state"], "queued")
        self.sessions.queue.change(self.vendor, {"action": "resume"})
        self.drain()
        self.assertEqual(self.runtime.get(identity)["state"], "accepted")

    def test_closed_recipient_reopens_without_losing_identity(self):
        self.store.threads.update(self.vendor, attention="archived")
        self.submit(); self.plan(); self.drain()
        self.assertEqual(self.store.threads.get(self.vendor)["attention"], "now")
        self.assertEqual(len(self.sessions.rpc.threads), 2)

    def test_creation_failure_is_uncertain_and_blocks_followups(self):
        identity = self.submit()
        with patch.object(self.sessions, "create", side_effect=CoordinationError("Lost reply")):
            self.plan("new")
        self.assertEqual(self.runtime.get(identity)["state"], "uncertain")
        self.submit("Also add emails", "two")
        self.assertFalse(self.runtime.process_once())
        self.runtime.resolve(self.dispatcher, {"id": identity, "action": "dismiss"})
        self.plan()

    def test_disabling_during_classification_prevents_delivery(self):
        self.submit()
        def decide(*args, **kwargs):
            self.runtime.configure(self.dispatcher, {"enabled": False})
            return {"decision": {"action": "reuse", "thread_id": self.vendor, "reason": "test"}}
        with patch("agent_coord.dispatch_runtime.route", side_effect=decide):
            self.runtime.process_once()
        self.assertEqual(self.runtime.receipts(self.dispatcher)[0]["state"], "cancelled")
        self.assertEqual(self.sessions.queue.list(self.vendor), [])

    def test_llm_assignments_inform_next_jev_request(self):
        self.store.send_message(sender_session_id=self.dispatcher, recipient_session_id=self.vendor,
                                classification="action_required", reply_required=False, body="User request:\nBuild vendor input")
        self.submit(); classifier = self.plan()
        self.assertTrue(any(r["message"] == "Build vendor input" and r["thread_id"] == self.vendor
                            for r in classifier.call_args.args[1]["recent_routes"]))

    def test_commands_bypass_routing(self):
        result = self.sessions.submit(self.dispatcher, {"message": "/help"})
        self.assertIn("command", result)
        self.assertEqual(self.runtime.receipts(self.dispatcher), [])

    def test_restart_keeps_accepted_receipts_and_pauses_queued_work(self):
        first = self.submit(); self.plan(); self.drain()
        self.submit("Follow-up", "two"); self.plan()
        self.sessions.close()
        reopened = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(reopened.close)
        reopened.rpc.threads = copy.deepcopy(self.sessions.rpc.threads)
        reopened.rpc.settings = copy.deepcopy(self.sessions.rpc.settings)
        self.assertTrue(reopened.dispatch.settings(self.dispatcher)["enabled"])
        replay = reopened.submit(self.dispatcher, {"message": "Add multiple vendors", "requestId": "one"})
        self.assertEqual(replay["receipt"]["id"], first)
        self.assertTrue(replay["replayed"])
        self.assertEqual(reopened.dispatch.receipts(self.dispatcher)[1]["state"], "paused")
        self.assertFalse(any(m == "turn/start" for m, _ in reopened.rpc.calls))

    def test_catchup_is_bounded_and_full_text_remains_pageable(self):
        message = "A long request " * 2000
        for index in range(8):
            self.submit(message, str(index))
        context = self.runtime.prepare_context(self.dispatcher)
        self.assertLess(len(context["text"]), 27000)
        self.assertIn('"truncated": true', context["text"])
        page = routing_history(self.store, self.dispatcher, limit=2)
        self.assertEqual(page["events"][0]["body"]["message"], message.strip())
        self.assertIsNotNone(page["next_cursor"])
        following = routing_history(self.store, self.dispatcher, after=page["next_cursor"], limit=2)
        self.assertGreater(following["events"][0]["event_id"], page["events"][-1]["event_id"])
        self.runtime.confirm_context(self.dispatcher, context)
        self.assertIn("A long request", self.runtime.prepare_context(self.dispatcher)["text"])

    def test_missing_jev_config_falls_back_without_losing_request(self):
        identity = self.submit()
        with patch("agent_coord.dispatch_runtime.load_config", return_value=None):
            self.runtime.process_once()
        self.assertEqual(self.runtime.get(identity)["target_id"], self.dispatcher)
        self.drain(self.dispatcher)
        self.assertEqual(self.runtime.get(identity)["state"], "accepted")

    def test_snapshot_excludes_dispatcher_and_other_workspace_from_jev(self):
        other = self.root / "other"; other.mkdir()
        foreign = self.sessions.create({"cwd": str(other), "name": "Foreign vendor form"})["session"]["thread_id"]
        identity = self.submit()
        from agent_coord.dispatch_routing import route
        def classifier(state, config, policy):
            self.assertEqual([c["thread_id"] for c in state["candidates"]], [self.vendor])
            choices = {"candidate_1": .95, "new": .03, "dispatcher": .02}
            return {"type": "choice", "choice": "candidate_1", "confidence": .95, "probabilities": choices}
        def actual(snapshot, request, config, **kwargs):
            return route(snapshot, request, config, classifier=classifier, **kwargs)
        with patch("agent_coord.dispatch_runtime.route", side_effect=actual):
            self.runtime.process_once()
        self.assertEqual(self.runtime.get(identity)["target_id"], self.vendor)
        self.assertEqual(self.sessions.rpc.threads[foreign]["turns"], [])

    def test_snapshot_uses_app_directory_when_hooks_register_repository_root(self):
        # Real provider hooks register the repository root in sessions.cwd while
        # the app and work thread retain a selected subdirectory as their cwd.
        with self.store._connection() as db:
            db.execute("UPDATE sessions SET cwd=? WHERE session_id=?", (str(self.root.parent), self.vendor))
        candidate = next(t for t in read_snapshot(self.store.database_path)["threads"] if t["thread_id"] == self.vendor)
        self.assertEqual(candidate["cwd"], str(self.root))
        from agent_coord.dispatch_routing import build_state
        state = build_state(read_snapshot(self.store.database_path), {"request_id":"test","dispatcher_id":self.dispatcher,
            "message":"Vendor form", "cwd":str(self.root)})
        self.assertEqual([c["thread_id"] for c in state["candidates"]], [self.vendor])

    def test_claude_destination_and_dispatcher_fallback_use_same_integration(self):
        sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex, claude_factory=claude_factory)
        self.addCleanup(sessions.close)
        with patch.object(sessions.queue, "wake"), patch.object(sessions.dispatch, "wake"):
            dispatcher = sessions.create({"client": "claude", "name": "Claude dispatcher"})["session"]["thread_id"]
            target = sessions.create({"client": "claude", "name": "Claude vendor"})["session"]["thread_id"]
            sessions.dispatch.configure(dispatcher, {"enabled": True})
            identity = sessions.submit(dispatcher, {"message": "Add vendor emails", "requestId": "claude"})["receipt"]["id"]
            with patch("agent_coord.dispatch_runtime.route", return_value={"decision": {"action": "reuse", "thread_id": target, "reason": "test"}}):
                sessions.dispatch.process_once()
            sessions.queue._dispatch(target)
            self.assertEqual(sessions.dispatch.get(identity)["state"], "accepted")
            followup = sessions.submit(dispatcher, {"message": "Explain routing", "requestId": "fallback", "askDispatcher": True})["receipt"]["id"]
            sessions.dispatch.process_once(); sessions.queue._dispatch(dispatcher)
            self.assertEqual(sessions.dispatch.get(followup)["state"], "accepted")
            text = json.dumps(sessions.claude.connections[dispatcher].writes)
            self.assertIn("Add vendor emails", text)
            self.assertIn("Explain routing", text)

    def test_http_entry_point_and_receipts(self):
        from agent_coord.ui import make_ui_server
        server = make_ui_server(self.store, port=0, cwd=str(self.root), browser_sessions=self.sessions)
        worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
        def close():
            server.shutdown(); server.server_close(); worker.join(2)
        self.addCleanup(close)
        base = "http://127.0.0.1:" + str(server.server_address[1])
        with urllib.request.urlopen(base + "/api/browser/config") as response:
            token = json.load(response)["token"]
        def post(path, body):
            request = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json", "X-Agent-Coord-Token": token})
            with urllib.request.urlopen(request) as response:
                return json.load(response)
        post(f"/api/browser/sessions/{self.dispatcher}/dispatch", {"enabled": True})
        result = post(f"/api/browser/sessions/{self.dispatcher}/messages", {"message": "HTTP vendor request", "requestId": "http"})
        self.assertTrue(result["dispatched"])
        self.plan(); self.drain()
        with urllib.request.urlopen(base + f"/api/browser/sessions/{self.dispatcher}") as response:
            detail = json.load(response)
        self.assertTrue(detail["work_thread"]["dispatcher"])
        self.assertEqual(detail["dispatchRequests"][0]["state"], "accepted")
        self.assertEqual(detail["dispatchRequests"][0]["target_title"], "Vendor form")
        for path in ("/dispatch.js", "/dispatch.css"):
            with urllib.request.urlopen(base + path) as response:
                self.assertEqual(response.status, 200)


if __name__ == "__main__":
    unittest.main()
