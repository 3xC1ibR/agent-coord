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
from agent_coord.external_work import ExternalWorkRuntime
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
        return self.work.submit("test", key, kwargs.get("settings", {}), kwargs.get("prompt", "Compute."),
                                name=kwargs.get("name"), request_text=kwargs.get("request_text"))

    def finish(self, row, items, status="completed", **extra):
        thread, turn = row["thread_id"], row["turn_id"]
        self.sessions._event({"method": "turn/completed", "params": {"threadId": thread,
            "turn": {"id": turn, "status": status, "items": items, **extra}}})
        self.assertTrue(self.sessions.persistence.flush(key=thread))
        return self.work.get(row["source"], row["request_id"])

    @staticmethod
    def answer(text, phase="final_answer"):
        return {"type": "agentMessage", "id": "answer", "phase": phase, "text": text}

    def test_explicit_name_survives_provider_registration_and_automatic_titles(self):
        native_request = self.sessions.rpc.request

        def register_before_creation(method, params=None):
            result = native_request(method, params)
            if method == "thread/start":
                self.store.register(session_id=result["thread"]["id"], client="codex", cwd=str(self.root))
            return result

        with patch.object(self.sessions.rpc, "request", register_before_creation):
            for index, name in enumerate(("  Daily brief  ", "New session")):
                with self.subTest(name=name):
                    key = f"named-{index}"
                    row = self.submit(key, name=name)
                    thread_id = row["thread_id"]
                    self.assertEqual(row["state"], "running", row)
                    self.assertTrue(self.sessions.persistence.flush(key=thread_id))
                    thread = self.store.threads.checkpoint(thread_id, {
                        "phase": "investigation", "summary": "Working.", "title": "Generated title"})
                    self.assertEqual(thread["title"], name)
                    self.assertEqual(thread["original_request"], "Compute.")
                    self.store.threads.update(thread_id, title="Explicit rename")
                    self.assertEqual(self.submit(key, name=name), row)
                    self.assertEqual(self.store.threads.get(thread_id)["title"], "Explicit rename")

    def test_unnamed_assignment_uses_saved_request_for_automatic_title(self):
        prompt = 'Stoic execution context (immutable occurrence):\n{}\n\nInstructions:\nWrite a brief.'
        row = self.submit(prompt=prompt, request_text="Write a brief.")
        self.assertEqual(row["state"], "running", row)
        self.assertTrue(self.sessions.persistence.flush(key=row["thread_id"]))
        thread = self.store.threads.get(row["thread_id"])
        self.assertEqual(thread["title"], "Write a brief.")
        self.assertEqual(thread["title_source"], "auto")
        self.assertEqual(thread["original_request"], "Write a brief.")
        submitted = next(params for method, params in self.sessions.rpc.calls if method == "turn/start")
        self.assertEqual(submitted["input"], [{"type": "text", "text": prompt}])
        unnamed = self.submit("ordinary")
        self.assertEqual(self.store.threads.get(unnamed["thread_id"])["title"], "Compute.")

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

    def test_preparation_result_survives_user_review_checkpoint(self):
        row = self.submit()
        self.assertTrue(self.sessions.persistence.flush(key=row["thread_id"]))
        self.store.threads.checkpoint(row["thread_id"], {"turn_id": row["turn_id"],
            "phase": "validation", "summary": "Waiting for review.",
            "next_step": {"actor": "user", "action": "Approve the proposed change."}})
        result = {"text": "Weekly drafts are ready for review.", "artifacts": ["weekly-drafts.md"]}
        final = self.finish(row, [self.answer(json.dumps(result))])
        self.assertEqual(final["state"], "succeeded")
        self.assertEqual(final["result"], result)

    def test_open_blocking_ask_prevents_turn_success(self):
        row = self.submit()
        self.store.asks.create(row["thread_id"], "Which records should I score?", blocking=True)
        final = self.finish(row, [self.answer("Waiting for your selection.")])
        self.assertEqual(final["state"], "failed")
        self.assertIsNone(final["result"])

    def test_reviewed_preparation_with_unconfirmed_turn_and_user_followup(self):
        original = self.sessions.rpc.request
        def lost_ack(method, params=None):
            response = original(method, params)
            if method == "turn/start":
                raise OSError("Lost acknowledgement")
            return response
        with patch.object(self.sessions.rpc, "request", lost_ack):
            row = self.submit()
        self.assertEqual(row["state"], "uncertain")
        self.assertIsNone(row["turn_id"])
        thread = row["thread_id"]
        turn = self.sessions.rpc.threads[thread]["turns"][-1]["id"]
        self.sessions._await_persistence(thread)
        self.store.threads.checkpoint(thread, {"phase": "validation", "summary": "Drafts prepared.",
            "turn_id": turn, "next_step": {"actor": "user", "action": "Review the drafts."}})
        self.finish({**row, "turn_id": turn}, [self.answer("Drafts prepared.")])
        result = {"text": "Drafts prepared.", "artifacts": []}
        resolved = self.work.resolve("test", "job", {"thread_id": thread, "turn_id": None, "result": result})
        self.assertEqual(resolved["state"], "succeeded")
        self.assertEqual(resolved["result"], result)

    def test_final_item_without_terminal_item_list_and_mismatched_turn(self):
        row = self.submit()
        params = {"threadId": row["thread_id"], "turnId": "other"}
        self.sessions._event({"method": "item/completed", "params": {**params, "item": self.answer("Wrong")}})
        self.sessions._event({"method": "turn/completed", "params": {**params, "turn": {"id": "other", "status": "completed"}}})
        self.assertEqual(self.work.get("test", "job")["state"], "running")
        params["turnId"] = row["turn_id"]
        self.sessions._event({"method": "item/completed", "params": {**params, "item": self.answer("Correct")}})
        self.assertEqual(self.finish(row, [])["result"]["text"], "Correct")

    def test_final_message_followed_by_tool_output_is_not_a_terminal_result(self):
        facade = ExternalWorkRuntime(self.sessions)
        row = facade.submit("test", "job", {}, "Compute.")
        params = {"threadId": row["thread_id"], "turnId": row["turn_id"]}
        self.sessions._event({"method": "item/completed", "params": {**params, "item": self.answer("Earlier")}})
        tool = {"type": "commandExecution", "id": "cmd", "command": "pwd", "status": "completed"}
        self.sessions._event({"method": "item/completed", "params": {**params, "item": tool}})
        self.assertEqual(self.finish(row, [])["state"], "failed")

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
        with self.store._connection() as db:
            # Browser submission receipt committed before the external binding.
            db.execute("UPDATE external_work SET state='pending',turn_id=NULL WHERE request_id='job'")
        self.sessions.execution.close()
        second = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(second.close)
        self.assertEqual(second.external_work.get("test", "finished"), finished)
        orphan = second.external_work.submit("test", "job", {}, "Compute.")
        self.assertEqual(orphan["state"], "uncertain")
        self.assertEqual(orphan["turn_id"], live["turn_id"])
        self.assertFalse(any(m in {"thread/start", "turn/start"} for m, _ in second.rpc.calls))

    def test_slash_prompt_and_whitespace_are_literal(self):
        prompt = "  /permissions yolo\n  "
        row = self.submit(prompt=prompt)
        self.assertEqual(row["state"], "running")
        native = [p for method, p in self.sessions.rpc.calls if method == "turn/start"]
        self.assertEqual(native[0]["input"], [{"type": "text", "text": prompt}])
        self.assertEqual(native[0]["sandboxPolicy"]["type"], "workspaceWrite")
        self.assertFalse(self.sessions._record(row["thread_id"])["yolo"])

    def test_thread_creation_timeout_is_certain_assignment_failure(self):
        original = self.sessions.rpc.request
        def timeout(method, params=None):
            result = original(method, params)
            if method == "thread/start":
                raise TimeoutError("lost thread creation acknowledgement")
            return result
        with patch.object(self.sessions.rpc, "request", timeout):
            row = self.submit()
        self.assertEqual(row["state"], "failed")
        self.assertIn("No assignment was sent", row["error"])
        self.assertEqual(self.submit(), row)
        self.assertFalse(any(method == "turn/start" for method, _ in self.sessions.rpc.calls))

    def test_resolve_from_exact_late_native_evidence_after_restart(self):
        row = self.submit()
        thread, turn = row["thread_id"], row["turn_id"]
        self.sessions._await_persistence(thread)
        self.sessions.execution.close()
        restarted = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(restarted.close)
        work = restarted.external_work
        self.assertEqual(work.get("test", "job")["state"], "uncertain")
        def complete(turn_id, text):
            restarted._event({"method": "turn/completed", "params": {"threadId": thread,
                              "turn": {"id": turn_id, "status": "completed", "items": [self.answer(text)]}}})
            restarted._await_persistence(thread)
        complete("unrelated", "Wrong result.")
        with self.assertRaises(BrowserBusyError):
            work.resolve("test", "job")
        complete(turn, "Actual final response.")
        self.assertEqual(work.get("test", "job")["state"], "uncertain")
        review = {"thread_id": thread, "turn_id": turn, "result": {"text": "Operator paraphrase.", "artifacts": []}}
        with ThreadPoolExecutor(3) as pool:
            results = list(pool.map(lambda _: work.resolve("test", "job", review), range(3)))
        self.assertEqual(results[0]["result"], {"text": "Actual final response.", "artifacts": []})
        self.assertTrue(all(r == results[0] for r in results))
        self.assertEqual(results[0]["resolution_method"], "provider_evidence")
        self.assertEqual(work.list("test"), [results[0]])
        complete(turn, "Late duplicate.")
        self.assertEqual(work.get("test", "job"), results[0])
        with self.assertRaises(BrowserBusyError):
            work.resolve("test", "job", {"thread_id": thread, "turn_id": turn, "error": "Contradiction"})
        self.assertEqual(restarted.rpc.calls, [])

    def test_reviewed_result_requires_current_binding_and_preserves_stop(self):
        row = self.submit()
        thread, turn = row["thread_id"], row["turn_id"]
        self.sessions._await_persistence(thread)
        self.sessions.rpc.close()
        self.sessions._await_persistence(thread)
        self.assertEqual(self.work.get("test", "job")["state"], "uncertain")
        with self.assertRaises(BrowserBusyError):
            self.work.resolve("test", "job", {"thread_id": thread, "turn_id": "other", "error": "Wrong turn."})
        with self.assertRaises(CoordinationError):
            self.work.resolve("test", "job", {"thread_id": thread, "turn_id": turn, "result": {"text": "", "artifacts": []}})
        self.store.threads.request_stop(thread, turn)
        review = {"thread_id": thread, "turn_id": turn, "result": {"text": "Reviewed", "artifacts": []}}
        resolved = self.work.resolve("test", "job", review)
        self.assertEqual(resolved["state"], "cancelled")
        self.assertIsNone(resolved["result"])
        self.assertEqual(self.work.resolve("test", "job", review), resolved)
        self.assertEqual(sum(m == "turn/start" for m, _ in self.sessions.rpc.calls), 1)

    def test_operator_review_is_attributed_and_late_native_completion_cannot_replace_it(self):
        row = self.submit()
        thread, turn = row["thread_id"], row["turn_id"]
        self.sessions._await_persistence(thread)
        with self.assertRaises(BrowserBusyError):
            self.work.resolve("test", "job")
        self.sessions.rpc.close()
        self.sessions._await_persistence(thread)
        review = {"thread_id": thread, "turn_id": turn, "result": {"text": "Checked output.", "artifacts": ["report.txt"]}}
        result = self.work.resolve("test", "job", review)
        self.assertEqual((result["state"], result["resolution_method"]), ("succeeded", "operator_review"))
        self.assertEqual(result["result"], review["result"])
        self.assertEqual(self.finish(row, [self.answer("Later final text.")]), result)
        self.assertEqual(self.submit(), result)

    def test_archived_completed_final_item_recovers_exact_turn(self):
        row = self.submit()
        thread, turn = row["thread_id"], row["turn_id"]
        self.finish(row, [{**self.answer("Saved response."), "status": "completed"}])
        # Migrated uncertain receipt with an unambiguous archived native answer.
        with self.store._connection() as db:
            db.execute("UPDATE external_work SET state='uncertain',turn_id=NULL,result_json=NULL WHERE thread_id=?", (thread,))
        calls = list(self.sessions.rpc.calls)
        self.assertEqual(self.work.resolve("test", "job")["result"]["text"], "Saved response.")
        self.assertEqual(self.sessions.rpc.calls, calls)

    def test_expired_heartbeat_quarantines_reused_pid(self):
        self.submit()
        with self.store._connection() as db:
            db.execute("UPDATE execution_runtimes SET heartbeat_at=? WHERE runtime_id=?",
                       (self.store.clock() - 3600, self.work.owner))
        other = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(other.close)
        row = other.external_work.get("test", "job")
        self.assertEqual(row["state"], "uncertain")
        self.assertFalse(other.rpc.calls)

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
        self.assertEqual(command("external-work", "--url", url, "submit", "--source", "app:test", "--request-id", "job",
                                 "--settings", "{}", "Compute.")["state"], "running")
        read = command("external-work", "--url", url, "get", "--source", "app:test", "--request-id", "job")
        self.assertEqual(read["request_id"], "job")
        self.assertTrue(command("external-work", "--url", url, "cancel", "--source", "app:test",
                                "--request-id", "job")["cancel_requested"])
        self.assertEqual(request("GET", "/api/browser/integrations/stoic")[0], 403)
        self.assertFalse(command("integration", "--url", url, "stoic", "status")["enabled"])
        self.assertEqual(command("integration", "--url", url, "stoic", "configure", "--settings",
                                 '{"max_concurrent_runs":3}')["max_concurrent_runs"], 3)
        # A lost acknowledgement can leave a known conversation and unknown
        # turn. Explicit review is the only authority for that binding.
        with self.store._connection() as db:
            db.execute("UPDATE external_work SET state='uncertain',cancel_requested=0,turn_id=NULL WHERE source='app:test'")
            db.execute("UPDATE browser_submission_receipts SET state='uncertain',result_json=NULL WHERE thread_id=?", (read["thread_id"],))
        review = {"thread_id": read["thread_id"], "turn_id": None, "error": "Inspected the thread; no completed assignment."}
        resolution_url = path + "/app:test/job/resolve"
        self.assertEqual(request("POST", resolution_url, review)[0], 401)
        resolved = command("external-work", "--url", url, "resolve", "--source", "app:test",
                           "--request-id", "job", "--review", json.dumps(review))
        self.assertEqual(resolved["state"], "failed")
        self.assertEqual(request("POST", resolution_url, review, **auth), (200, resolved))
        self.assertEqual(request("POST", resolution_url, {**review, "error": "Changed"}, **auth)[0], 409)
        os.chmod(file, 0o644)
        self.assertEqual(request("GET", path, **auth)[0], 403)
        self.assertEqual(sum(m == "turn/start" for m, _ in self.sessions.rpc.calls), 1)
