from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
import http.client
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from test_codex_app_server import FakeCodex
from agent_coord import cli
from agent_coord.codex_app_server import BrowserBusyError, BrowserSessions
from agent_coord.external_work_control import provision_token, read_token, token_file
from agent_coord.store import CoordinationError, CoordinationStore
from agent_coord.ui import make_ui_server


class ExternalWorkTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.store = CoordinationStore(self.root / "state.sqlite3")
        self.sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(self.sessions.close)
        self.work = self.sessions.external_work

    def submit(self, key="job", **kwargs):
        return self.work.submit("test", key, kwargs.get("settings", {}), kwargs.get("prompt", "Compute."))

    def finish(self, row, items, status="completed", **extra):
        thread, turn = row["thread_id"], row["turn_id"]
        self.sessions._event({"method": "turn/completed", "params": {"threadId": thread,
            "turn": {"id": turn, "status": status, "items": items, **extra}}})
        self.assertTrue(self.sessions.persistence.flush(key=thread))
        return self.work.get(row["source"], row["request_id"])

    @staticmethod
    def answer(text, phase="final_answer"):
        return {"type": "agentMessage", "id": "answer", "phase": phase, "text": text}

    def test_concurrent_idempotency_namespace_and_exact_settings(self):
        settings = {"client": "codex", "model": "available-model", "effort": "high", "service_tier": "fast"}
        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(lambda _: self.submit(settings=settings), range(5)))
        self.assertTrue(all(row["state"] in {"pending", "running"} for row in results))
        calls = self.sessions.rpc.calls
        self.assertEqual(sum(m == "thread/start" for m, _ in calls), 1)
        native = [p for m, p in calls if m == "turn/start"]
        self.assertEqual(len(native), 1)
        self.assertEqual([native[0][k] for k in ("model", "effort", "serviceTier")], ["available-model", "high", "fast"])
        self.assertEqual(native[0]["sandboxPolicy"]["type"], "workspaceWrite")
        with self.assertRaises(BrowserBusyError):
            self.submit(settings=settings, prompt="Different.")
        other = self.work.submit("other", "job", {"yolo": True}, "Compute.")
        self.assertNotEqual(other["thread_id"], self.work.get("test", "job")["thread_id"])
        self.assertEqual(self.work.list("other"), [other])
        self.assertEqual([p for m, p in calls if m == "turn/start"][-1]["sandboxPolicy"]["type"], "dangerFullAccess")

    def test_rejects_invalid_or_substituted_settings_before_turn(self):
        invalid = self.submit(settings={"model": "no-model"})
        self.assertEqual(invalid["state"], "failed")
        self.assertFalse(any(m == "thread/start" for m, _ in self.sessions.rpc.calls))
        original = self.sessions.rpc.request

        def substituted(method, params=None):
            result = original(method, params)
            if method == "thread/start":
                result["reasoningEffort"] = "medium"
            return result

        with patch.object(self.sessions.rpc, "request", substituted):
            rejected = self.submit("changed", settings={"model": "available-model", "effort": "high"})
        self.assertEqual(rejected["state"], "failed")
        self.assertIn("effort", rejected["error"])
        self.assertTrue(rejected["thread_id"])
        self.assertFalse(any(m == "turn/start" for m, _ in self.sessions.rpc.calls))

    def test_fast_terminal_event_before_provider_ack(self):
        original = self.sessions.rpc.request

        def fast(method, params=None):
            result = original(method, params)
            if method == "turn/start":
                self.sessions._event({"method": "turn/completed", "params": {"threadId": params["threadId"],
                    "turn": {"id": result["turn"]["id"], "status": "completed", "items": [
                        self.answer('{"text":"Report ready","artifacts":["report.pdf"]}', phase=None)]}}})
            return result

        with patch.object(self.sessions.rpc, "request", fast):
            row = self.submit()
        self.assertEqual(row["state"], "succeeded")
        self.assertEqual(row["result"], {"text": "Report ready", "artifacts": ["report.pdf"]})
        self.assertEqual(self.submit(), row)

    def test_requires_final_response_and_successful_correlated_completion(self):
        for case, items, status, extra, expected in [
            ("empty", [], "completed", {}, "failed"),
            ("comment", [self.answer("Looking", "commentary")], "completed", {}, "failed"),
            ("failed", [self.answer("All set")], "failed", {}, "failed"),
            ("error", [self.answer("All set")], "completed", {"error": {"message": "approval denied"}}, "failed"),
            ("interrupted", [self.answer("Partial")], "interrupted", {}, "failed"),
            ("stop", [self.answer("Partial")], "completed", {"termination": {"origin": "user"}}, "cancelled"),
            ("oversize", [self.answer("x" * 64001)], "completed", {}, "failed"),
            ("empty-structured", [self.answer('{"text":"","artifacts":[]}')], "completed", {}, "failed"),
            ("final", [self.answer("Verified.")], "completed", {}, "succeeded"),
        ]:
            with self.subTest(case=case):
                row = self.submit(case)
                actual = self.finish(row, items, status, **extra)
                self.assertEqual(actual["state"], expected, actual)
                if expected != "succeeded":
                    self.assertIsNone(actual["result"])

    def test_pending_approval_and_delta_only_never_succeed(self):
        row = self.submit()
        params = {"threadId": row["thread_id"], "turnId": row["turn_id"]}
        self.sessions._event({"method": "item/commandExecution/requestApproval", "id": 33, "params": params})
        self.assertEqual(self.finish(row, [self.answer("Draft")])["state"], "failed")
        row = self.submit("delta")
        params = {"threadId": row["thread_id"], "turnId": row["turn_id"]}
        self.sessions._event({"method": "item/started", "params": {**params, "item": self.answer("")}})
        self.sessions._event({"method": "item/agentMessage/delta", "params": {**params, "itemId": "answer", "delta": "Partial"}})
        self.assertEqual(self.finish(row, [])["state"], "failed")

    def test_final_item_without_terminal_item_list_and_mismatched_turn(self):
        row = self.submit()
        params = {"threadId": row["thread_id"], "turnId": "other"}
        self.sessions._event({"method": "item/completed", "params": {**params, "item": self.answer("Wrong")}})
        self.sessions._event({"method": "turn/completed", "params": {**params, "turn": {"id": "other", "status": "completed"}}})
        self.assertEqual(self.work.get("test", "job")["state"], "running")
        params["turnId"] = row["turn_id"]
        self.sessions._event({"method": "item/completed", "params": {**params, "item": self.answer("Correct")}})
        self.assertEqual(self.finish(row, [])["result"]["text"], "Correct")

    def test_lost_acknowledgement_never_replays(self):
        original = self.sessions.rpc.request

        def lost(method, params=None):
            value = original(method, params)
            if method == "turn/start":
                raise OSError("Connection lost after acceptance")
            return value

        with patch.object(self.sessions.rpc, "request", lost):
            row = self.submit()
        self.assertEqual(row["state"], "uncertain")
        self.assertEqual(self.submit(), row)
        self.assertEqual(sum(m == "turn/start" for m, _ in self.sessions.rpc.calls), 1)

    def test_maintenance_cancel_and_persistent_user_stop(self):
        self.sessions.admission.pause()
        with self.assertRaises(BrowserBusyError):
            self.submit()
        self.assertIsNone(self.work.get("test", "job"))
        self.sessions.admission.resume()
        row = self.submit()
        self.work.cancel("test", "job")
        self.assertTrue(self.sessions.persistence.flush(key=row["thread_id"]))
        self.assertEqual(self.submit()["state"], "cancelled")
        self.assertEqual(self.work.cancel("test", "job")["state"], "cancelled")
        self.assertTrue(self.store.threads.stop_requested(row["thread_id"], row["turn_id"]))
        self.assertEqual(sum(m == "turn/interrupt" for m, _ in self.sessions.rpc.calls), 1)
        other = self.submit("human")
        self.sessions.interrupt(other["thread_id"])
        self.assertTrue(self.sessions.persistence.flush(key=other["thread_id"]))
        self.assertEqual(self.work.get("test", "human")["state"], "cancelled")

    def test_cancel_during_creation_or_provider_acceptance(self):
        for method, key in [("thread/start", "before"), ("turn/start", "during")]:
            original = self.sessions.rpc.request
            reached, resume = threading.Event(), threading.Event()

            def delayed(name, params=None):
                if name == method:
                    reached.set()
                    self.assertTrue(resume.wait(10))
                return original(name, params)

            with patch.object(self.sessions.rpc, "request", delayed), ThreadPoolExecutor(1) as pool:
                future = pool.submit(self.submit, key)
                try:
                    self.assertTrue(reached.wait(10))
                    self.assertTrue(self.work.cancel("test", key)["cancel_requested"])
                finally:
                    resume.set()
                row = future.result(10)
            self.assertTrue(self.sessions.persistence.flush(key=row["thread_id"]))
            self.assertEqual(self.work.get("test", key)["state"], "cancelled")
        self.assertEqual(sum(m == "turn/start" for m, _ in self.sessions.rpc.calls), 1)

    def test_restart_preserves_terminal_and_quarantines_orphan(self):
        finished = self.finish(self.submit("finished"), [self.answer("Done.")])
        live = self.submit()
        # Simulate abrupt owner exit without delivering any terminal event.
        self.sessions.execution.close()
        second = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(second.close)
        self.assertEqual(second.external_work.get("test", "finished"), finished)
        orphan = second.external_work.submit("test", "job", {}, "Compute.")
        self.assertEqual(orphan["state"], "uncertain")
        self.assertEqual(orphan["turn_id"], live["turn_id"])
        self.assertFalse(any(m in {"thread/start", "turn/start"} for m, _ in second.rpc.calls))

    def test_http_authentication_cli_and_connector_configuration(self):
        server = make_ui_server(self.store, port=0, browser_sessions=self.sessions)
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        path = "/api/external-work"
        url = f"http://127.0.0.1:{server.server_port}"

        def request(method, path, body=None, **headers):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
            try:
                connection.request(method, path, json.dumps(body) if body is not None else None,
                                   {"Content-Type": "application/json", **headers})
                reply = connection.getresponse()
                return reply.status, json.load(reply)
            finally:
                connection.close()

        def command(*args):
            output = io.StringIO()
            with redirect_stdout(output), redirect_stderr(output):
                self.assertEqual(cli.main(["--db", str(self.store.database_path), *args]), 0, output.getvalue())
            return json.loads(output.getvalue())

        self.assertEqual(request("GET", path)[0], 403)
        receipt = command("external-work", "provision-token")
        self.assertEqual(set(receipt), {"token_file"})
        file = token_file(self.store.database_path)
        token = read_token(file).decode()
        self.assertEqual(receipt, provision_token(self.store.database_path))
        self.assertEqual(read_token(file).decode(), token)
        auth = {"Authorization": "Bearer " + token}
        job = {"source": "app:test", "request_id": "job", "prompt": "Compute.", "settings": {}}
        self.assertEqual(request("POST", path, job)[0], 401)
        self.assertEqual(request("POST", path, job, **auth, Origin="https://example.org")[0], 403)
        self.assertEqual(request("POST", path, job, **auth, **{"X-Forwarded-For": "127.0.0.1"})[0], 403)
        self.assertEqual(request("POST", path, job, **auth)[0], 200)
        self.assertEqual(request("POST", path, {**job, "prompt": "Other"}, **auth)[0], 409)
        self.assertEqual(request("GET", path + "/app%3Atest/job", **auth)[1]["state"], "running")
        self.assertEqual(request("GET", path + "/test/missing", **auth)[0], 404)
        self.assertEqual(command("external-work", "--url", url, "list", "--source", "app:test")["data"][0]["source"], "app:test")
        self.assertEqual(request("POST", path + "/app:test/job/cancel", {}, **auth)[0], 200)
        self.assertEqual(request("GET", "/api/browser/integrations/stoic")[0], 403)
        self.assertFalse(command("integration", "--url", url, "stoic", "status")["enabled"])
        self.assertEqual(command("integration", "--url", url, "stoic", "configure", "--settings",
                                 '{"max_concurrent_runs":3}')["max_concurrent_runs"], 3)
        os.chmod(file, 0o644)
        self.assertEqual(request("GET", path, **auth)[0], 403)
        self.assertEqual(sum(m == "turn/start" for m, _ in self.sessions.rpc.calls), 1)
