from __future__ import annotations

import copy
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))
from agent_coord.codex_app_server import BrowserSessions
from agent_coord.store import CoordinationError, CoordinationStore
from agent_coord import send_work
from test_codex_app_server import FakeCodex


class SendWorkTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = str(Path(tmp.name).resolve())
        self.store = CoordinationStore(Path(self.root) / "state.sqlite3")
        self.sessions = BrowserSessions(self.store, self.root, rpc_factory=FakeCodex)
        self.addCleanup(self.sessions.close)
        self.runtime = self.sessions.dispatch
        for owner in (self.runtime, self.sessions.queue):
            mock = patch.object(owner, "wake"); mock.start(); self.addCleanup(mock.stop)
        self.source = self.create("Unrelated conversation")
        self.target = self.create("Release deployment")

    def create(self, title):
        return self.sessions.create({"name": title})["session"]["thread_id"]

    def body(self, kind="thread", key="one", **extra):
        return {"cwd": self.root, "message": "Redeploy the release", "requestId": key,
                "destination": {"kind": kind, **({"id": self.target} if kind == "thread" else {})}, **extra}

    def send(self, kind="thread", key="one"):
        item = send_work.submit(self.runtime, self.body(kind, key))["receipt"]
        self.assertTrue(self.runtime.process_once())
        return self.runtime.get(item["id"])

    def test_direct_send_has_only_typed_text_and_does_not_create_a_dispatcher(self):
        self.sessions.send(self.source, {"message": "Private unrelated source context"})
        before = copy.deepcopy(self.sessions.rpc.threads[self.source])
        item = self.send()
        self.sessions.queue._dispatch(self.target)
        content = self.sessions.rpc.threads[self.target]["turns"][0]["items"][0]["content"]
        self.assertEqual(content, [{"type": "text", "text": "Redeploy the release"}])
        self.assertEqual(self.sessions.rpc.threads[self.source], before)
        self.assertEqual(len(self.sessions.rpc.threads), 2)
        self.assertEqual(self.runtime.receipts(self.source), [])
        self.assertEqual(self.runtime.receipts(self.target), [])
        self.assertEqual(self.runtime.get(item["id"])["state"], "accepted")

    def test_retries_do_not_duplicate_delivery_and_conflicting_ids_are_rejected(self):
        item = self.send()
        self.sessions.queue._dispatch(self.target)
        retry = send_work.submit(self.runtime, self.body())
        self.assertTrue(retry["replayed"])
        self.assertEqual(retry["receipt"]["id"], item["id"])
        self.assertFalse(self.runtime.process_once())
        with self.assertRaisesRegex(CoordinationError, "different input"):
            send_work.submit(self.runtime, self.body(message="Another request"))

    def test_busy_thread_queues_without_steering(self):
        self.sessions.send(self.target, {"message": "Current task"})
        self.send(); self.sessions.queue._dispatch(self.target)
        self.assertEqual(len(self.sessions.rpc.threads[self.target]["turns"]), 1)
        self.assertEqual(len(self.sessions.queue.list(self.target)), 1)
        self.assertFalse(any(method == "turn/steer" for method, _ in self.sessions.rpc.calls))

    def test_closed_thread_reopens(self):
        self.store.threads.update(self.target, attention="archived")
        self.assertEqual(self.send()["state"], "queued")
        self.assertEqual(self.store.threads.get(self.target)["attention"], "now")

    def test_paused_thread_is_not_silently_rerouted(self):
        self.sessions.send(self.target, {"message": "A task to stop"})
        self.sessions.interrupt(self.target)
        item = self.send()
        self.assertEqual(item["state"], "failed")
        self.assertEqual(item["target_id"], self.target)
        self.assertEqual(len(self.sessions.rpc.threads), 2)
        self.assertEqual(self.sessions.queue.list(self.target), [])

    def test_new_conversation_uses_typed_text_and_normal_permissions(self):
        item = self.send("new")
        target = item["target_id"]
        self.assertNotIn(target, {self.source, self.target})
        self.assertFalse(self.sessions._record(target)["yolo"])
        self.sessions.queue._dispatch(target)
        self.assertEqual(self.sessions.rpc.threads[target]["turns"][0]["items"][0]["content"],
                         [{"type": "text", "text": "Redeploy the release"}])

    def test_new_settings_are_saved_and_applied_before_the_first_turn(self):
        settings = {"client": "codex", "model": "other-model", "effort": "low", "yolo": True}
        body = self.body("new", settings=settings)
        before = len(self.sessions.rpc.threads)
        item = send_work.submit(self.runtime, body)["receipt"]
        self.assertEqual(len(self.sessions.rpc.threads), before)
        self.assertEqual(item["body"]["sendWorkSettings"], settings)
        self.runtime.process_once()
        item = self.runtime.get(item["id"])
        target = item["target_id"]
        record = self.sessions._record(target)
        self.assertEqual((record["client"], record["model"], record["effort"], bool(record["yolo"])),
                         ("codex", "other-model", "low", True))
        options = next(params for method, params in reversed(self.sessions.rpc.calls) if method == "thread/start")
        self.assertEqual(options["sandbox"], "danger-full-access")
        self.assertEqual(options["approvalPolicy"], "never")
        self.sessions.queue._dispatch(target)
        self.assertEqual(self.sessions.rpc.threads[target]["turns"][0]["items"][0]["content"],
                         [{"type": "text", "text": "Redeploy the release"}])
        self.assertTrue(send_work.submit(self.runtime, body)["replayed"])
        with self.assertRaisesRegex(CoordinationError, "different input"):
            send_work.submit(self.runtime, {**body, "settings": {**settings, "yolo": False}})

    def test_new_settings_reject_invalid_values_before_creating_or_saving(self):
        before = len(self.sessions.rpc.threads)
        for settings in (None, [], {"cwd": "/"}, {"client": "unknown"}, {"client": []},
                         {"model": "unavailable"}, {"model": "other-model", "effort": "high"},
                         {"effort": "high"}, {"yolo": "true"}):
            with self.subTest(settings=settings), self.assertRaises(CoordinationError):
                send_work.submit(self.runtime, self.body("new", settings=settings))
        self.assertEqual(len(self.sessions.rpc.threads), before)
        self.assertFalse(self.runtime.process_once())

    def test_settings_cannot_change_existing_thread_or_dispatcher(self):
        for kind in ("thread", "dispatcher"):
            with self.subTest(kind=kind), self.assertRaisesRegex(CoordinationError, "only to a new"):
                send_work.submit(self.runtime, self.body(kind, settings={"yolo": True}))
        self.assertFalse(self.sessions._record(self.target)["yolo"])

    def test_new_settings_select_the_receiving_provider(self):
        with patch.object(self.sessions, "models", return_value=[{"model": "claude-test", "supportedReasoningEfforts": []}]), \
             patch.object(self.sessions, "create", return_value={"session": {"thread_id": self.target}}) as create:
            send_work.submit(self.runtime, self.body("new", settings={"client": "claude", "model": "claude-test"}))
            self.runtime.process_once()
        self.assertEqual(create.call_args.args[0]["client"], "claude")
        self.assertEqual(create.call_args.args[0]["model"], "claude-test")

    def test_dispatcher_is_created_only_on_explicit_choice_and_reused(self):
        first = self.send("dispatcher")
        target = first["target_id"]
        self.assertTrue(self.runtime.settings(target)["enabled"])
        self.assertEqual(first["state"], "queued")
        self.assertEqual(self.send("dispatcher", "two")["target_id"], target)
        self.assertEqual(len(self.sessions.rpc.threads), 3)
        self.sessions.queue._dispatch(target)
        self.assertEqual(self.runtime.get(first["id"])["state"], "accepted")
        content = self.sessions.rpc.threads[target]["turns"][0]["items"][0]["content"]
        self.assertEqual(content[-1], {"type": "text", "text": "Redeploy the release"})
        self.assertNotIn("Unrelated conversation", str(content))

    def test_suggestions_are_read_only_and_have_no_source_context(self):
        before = list(self.sessions.rpc.calls)
        with patch("agent_coord.send_work.load_config", return_value=None):
            result = send_work.suggestions(self.runtime, {"cwd": self.root, "message": "deployment"})
        self.assertEqual(result["candidates"][0]["id"], self.target)
        self.assertEqual(self.sessions.rpc.calls, before)
        self.assertFalse(self.runtime.process_once())
        with patch("agent_coord.send_work.load_config", return_value=None):
            result = send_work.suggestions(self.runtime, {"cwd": self.root, "message": "redeploy"})
        self.assertEqual(result["candidates"][0]["id"], self.target)

    def test_jev_gets_only_query_and_candidate_evidence(self):
        def classify(state, *_):
            self.assertEqual(state["recent_context"], [])
            self.assertEqual(state["request"]["message"], "redeploy")
            return {"probabilities": {c["key"]: .9 if c["thread_id"] == self.target else .01 for c in state["candidates"]}}
        with patch("agent_coord.send_work.load_config", return_value={"api_key_file": "unused"}), patch("agent_coord.send_work.classify", side_effect=classify):
            result = send_work.suggestions(self.runtime, {"cwd": self.root, "message": "redeploy"})
        self.assertTrue(result["ranked"])
        self.assertEqual(result["candidates"][0]["id"], self.target)

    def test_source_context_fields_and_outside_workspace_are_rejected(self):
        for extra in ({"context": "unrelated"}, {"sourceThreadId": self.source}, {"cwd": "/"}, {"destination": {"kind": "bogus"}}):
            with self.subTest(extra=extra), self.assertRaises(CoordinationError):
                send_work.submit(self.runtime, self.body(**extra))

    def test_unknown_provider_result_is_not_replayed(self):
        item = self.send()
        self.sessions.rpc.fail_turn = True
        self.sessions.queue._dispatch(self.target)
        self.assertEqual(self.runtime.get(item["id"])["state"], "uncertain")
        self.assertTrue(send_work.submit(self.runtime, self.body())["replayed"])
        self.assertFalse(self.runtime.process_once())

    def test_uncertain_request_does_not_block_independent_new_work(self):
        item = self.send()
        self.sessions.rpc.fail_turn = True
        self.sessions.queue._dispatch(self.target)
        self.assertEqual(self.runtime.get(item["id"])["state"], "uncertain")
        self.sessions.rpc.fail_turn = False
        self.assertEqual(self.send("new", "independent")["state"], "queued")

    def test_http_routes_serve_assets_enforce_auth_and_deliver_typed_request(self):
        from agent_coord.ui import make_ui_server
        server = make_ui_server(self.store, port=0, cwd=self.root, browser_sessions=self.sessions)
        worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
        def close():
            server.shutdown(); server.server_close(); worker.join(2)
        self.addCleanup(close)
        base = "http://127.0.0.1:" + str(server.server_address[1])
        with urllib.request.urlopen(base + "/api/browser/config") as response:
            token = json.load(response)["token"]
        def post(action, body, authenticated=True):
            headers = {"Content-Type": "application/json"}
            if authenticated:
                headers["X-Agent-Coord-Token"] = token
            request = urllib.request.Request(base + "/api/browser/send-work/" + action,
                data=json.dumps(body).encode(), headers=headers)
            with urllib.request.urlopen(request) as response:
                return json.load(response)
        with self.assertRaises(urllib.error.HTTPError) as error:
            post("send", self.body(), False)
        self.assertEqual(error.exception.code, 403); error.exception.close()
        with patch("agent_coord.send_work.load_config", return_value=None):
            result = post("suggest", {"cwd": self.root, "message": "deployment"})
        self.assertEqual(result["candidates"][0]["id"], self.target)
        self.assertFalse(self.runtime.process_once())
        receipt = post("send", self.body())["receipt"]
        self.runtime.process_once(); self.sessions.queue._dispatch(self.target)
        self.assertEqual(post("status", {"cwd": self.root, "id": receipt["id"]})["state"], "accepted")
        for path in ("/send-work.js", "/send-work.css"):
            with urllib.request.urlopen(base + path) as response:
                self.assertEqual(response.status, 200)

    def test_receipt_is_scoped_to_workspace(self):
        item = self.send()
        receipt = send_work.status(self.runtime, {"cwd": self.root, "id": item["id"]})
        self.assertEqual(receipt["target_title"], "Release deployment")
        child = Path(self.root) / "child"; child.mkdir()
        with self.assertRaises(CoordinationError):
            send_work.status(self.runtime, {"cwd": str(child), "id": item["id"]})
