"""Role-routed Stoic runs through the real execution engine, with Stoic and the provider simulated."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from test_codex_app_server import FakeCodex
from test_stoic_integration import FakeStoic
from agent_coord import workspace_roles
from agent_coord.codex_app_server import BrowserSessions
from agent_coord.stoic_integration import StoicIntegration, StoicRequestError
from agent_coord.store import CoordinationStore
from agent_coord.supervision import USER

ROUTE = {"role": "outcomes"}
LEGACY_RECEIPT = ["runner_id", "claim_token", "receipt_id", "status", "result", "error"]


class RoutedStoic(FakeStoic):
    """The simulated Stoic API with the compact definition list."""
    def __init__(self):
        super().__init__()
        self.definitions = []
        self.definition_status = None

    def request(self, method, path, body=None):
        if path.startswith("/api/execution/definitions?"):
            self.calls.append((method, path, body))
            if self.definition_status:
                raise StoicRequestError("Definition list unavailable.", self.definition_status)
            query = parse_qs(urlsplit(path).query)
            assert query["routed"] == ["true"], query
            offset = int(query["offset"][0])
            return {"definitions": self.definitions[offset:offset + 100]}
        return super().request(method, path, body)


class StoicRoutingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = CoordinationStore(self.root / "state.sqlite3")
        self.sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(self.sessions.close)
        self.work = self.sessions.external_work
        self.remote = RoutedStoic()
        self.settings = {"client": "codex", "model": "available-model", "effort": "high",
                         "permission_mode": "full-access", "cwd": str(self.root)}
        self.remote.runs = {}
        self.connector = self.restart()
        self.holder_mode = None

    def restart(self):
        connector = StoicIntegration(self.work, client_factory=lambda *_: self.remote)
        self.addCleanup(connector.close)
        return connector

    def enable(self, **settings):
        modes = ["full-access"] + ([self.holder_mode] if self.holder_mode else [])
        self.connector.configure({"enabled": True, "allowed_roots": [str(self.root)],
                                  "allowed_permission_modes": {"codex": modes}, **settings})

    def offer(self, run_id, route=ROUTE, **fields):
        run = {"id": run_id, "name": "Daily outcomes", "prompt": "Review the outcomes.", "settings": self.settings,
               "source": "job", "source_id": "job-1", **fields}
        if route is not None:
            run["route"] = route
        self.remote.runs[run_id] = run
        self.connector.next_scan = 0

    def synchronize(self, times=2):
        for _ in range(times):
            self.connector.process_once()
            self.assertIsNone(self.connector.last_error, self.connector.last_error)

    def conversation(self):
        thread = self.sessions.create({})["session"]["thread_id"]
        self.holder_mode = self.sessions._record(thread)["permission_mode"]
        return thread

    def role(self, holder=None, name="outcomes"):
        role = workspace_roles.create_role(self.store, workspace=str(self.root), name=name, requester_kind=USER)
        if holder:
            self.appoint(role, holder)
        return role

    def appoint(self, role, holder):
        workspace_roles.appoint(self.store, role_id=role["role_id"], holder=holder, requester_kind=USER,
                                workspace=str(self.root))

    def local(self, run_id):
        (source,) = {row["source"] for row in self.work.list()}
        return self.work.get(source, run_id)

    def complete(self, thread, turn, text="Outcomes reviewed."):
        self.sessions._event({"method": "turn/completed", "params": {"threadId": thread, "turn": {
            "id": turn, "status": "completed",
            "items": [{"type": "agentMessage", "id": "answer", "phase": "final_answer", "text": text}]}}})
        self.assertTrue(self.sessions.persistence.flush(key=thread))

    def turns(self, thread=None):
        return [params for name, params in self.sessions.rpc.calls
                if name == "turn/start" and (thread is None or params["threadId"] == thread)]

    def heartbeats(self, run_id):
        return [body for _, path, body in self.remote.calls if path.endswith(f"/{run_id}/heartbeat")]

    def notices(self, recipient):
        with self.store.reading(), self.store._connection() as db:
            return [json.loads(row[0]) for row in db.execute("""SELECT body FROM messages WHERE recipient_session_id=?
                AND thread_id LIKE 'stoic-role-coverage:%' ORDER BY id""", (recipient,))]

    def definition(self, role="outcomes", identity="job-1"):
        return {"source": "job", "id": identity, "name": "Daily outcomes", "cwd": str(self.root),
                "route": {"role": role}}

    def test_run_without_route_uses_the_unchanged_submission_and_receipt(self):
        holder = self.conversation()
        self.role(holder)
        self.enable()
        self.offer("plain", route=None)
        with patch.object(self.work, "submit", wraps=self.work.submit) as submit, \
                patch.object(self.work, "register_holder_policy") as register:
            self.synchronize()
        (call,) = submit.call_args_list
        self.assertEqual(call.args[1:], ("plain", self.settings, "Review the outcomes."))
        self.assertEqual(call.kwargs, {"name": "Daily outcomes", "request_text": None})
        register.assert_not_called()
        local = self.local("plain")
        self.assertNotIn("delivery", local)
        self.assertNotEqual(local["thread_id"], holder)
        self.assertEqual(self.turns(holder), [])
        self.complete(local["thread_id"], local["turn_id"])
        self.synchronize(1)
        receipt = self.remote.results["plain"]
        self.assertEqual(list(receipt), LEGACY_RECEIPT)
        self.assertEqual(receipt["result"], {"text": "Outcomes reviewed.", "artifacts": []})
        self.assertEqual(self.connector.status()["uncovered_roles"], [])

    def test_idle_holder_receives_the_run_and_its_final_message_is_published(self):
        holder = self.conversation()
        self.role(holder)
        self.enable()
        self.offer("routed", occurrence_id="today", scheduled_at="2026-11-22T15:00:00Z")
        threads = sum(name == "thread/start" for name, _ in self.sessions.rpc.calls)
        self.synchronize()
        local = self.local("routed")
        self.assertEqual([local["state"], local["thread_id"]], ["running", holder])
        self.assertEqual(sum(name == "thread/start" for name, _ in self.sessions.rpc.calls), threads)
        (turn,) = self.turns(holder)
        text = turn["input"][0]["text"]
        # The occurrence-context envelope precedes the unchanged instructions.
        self.assertTrue(text.startswith("Stoic execution context (immutable occurrence):\n"))
        self.assertTrue(text.endswith("\n\nInstructions:\nReview the outcomes."))
        self.assertEqual(json.loads(text.split("\n")[1])["scheduled_at"], "2026-11-22T15:00:00Z")
        self.synchronize(1)
        self.assertEqual({key: self.heartbeats("routed")[-1][key] for key in ("thread_id", "turn_id")},
                         {"thread_id": holder, "turn_id": local["turn_id"]})
        self.complete(holder, local["turn_id"], "The holder's final message.")
        self.remote.lose_result_response = True
        self.connector.process_once()
        self.synchronize(1)
        receipt = self.remote.results["routed"]
        self.assertEqual(list(receipt), LEGACY_RECEIPT + ["delivery"])
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(receipt["result"], {"text": "The holder's final message.", "artifacts": []})
        self.assertEqual(receipt["delivery"], {"route": ROUTE, "outcome": "holder", "thread_id": holder})
        # The lost response repeated one identical receipt.
        self.assertEqual(len(self.remote.receipts), 1)
        self.assertEqual(self.connector.status()["runs"], {"published": 1})

    def test_busy_holder_queues_the_run_without_consuming_execution_capacity(self):
        holder = self.conversation()
        self.role(holder)
        self.enable(max_concurrent_runs=1)
        user_turn = self.sessions.send(holder, {"message": "Other work."})["turn"]["id"]
        self.offer("queued")
        self.synchronize()
        waiting = self.local("queued")
        self.assertEqual([waiting["state"], waiting["thread_id"]], ["pending", None])
        self.assertEqual(waiting["delivery"]["outcome"], "waiting")
        self.synchronize(1)
        self.assertEqual({(beat["thread_id"], beat["turn_id"]) for beat in self.heartbeats("queued")}, {(None, None)})
        self.assertEqual(self.remote.results, {})
        # A run that only waits for its holder leaves room for other work.
        self.offer("plain", route=None)
        self.synchronize()
        self.assertEqual(self.local("plain")["state"], "running")
        self.assertEqual(len(self.turns(holder)), 1)
        self.complete(holder, user_turn, "Unrelated answer.")
        self.work.advance_routes()
        delivered = self.local("queued")
        self.assertEqual([delivered["state"], delivered["thread_id"]], ["running", holder])
        self.complete(holder, delivered["turn_id"], "Queued result.")
        self.synchronize(1)
        receipt = self.remote.results["queued"]
        self.assertEqual(receipt["result"]["text"], "Queued result.")
        self.assertEqual(receipt["delivery"]["outcome"], "holder")

    def assert_fallback(self, run_id, reason):
        local = self.local(run_id)
        self.assertEqual(local["state"], "running", local)
        started = next(params for name, params in self.sessions.rpc.calls
                       if name == "turn/start" and params["threadId"] == local["thread_id"])
        # The fallback conversation uses the run's own settings.
        self.assertEqual([started["model"], started["effort"]], ["available-model", "high"])
        self.assertEqual(started["sandboxPolicy"]["type"], "dangerFullAccess")
        self.assertTrue(self.store.threads.get(local["thread_id"])["title"].startswith("Daily outcomes"))
        self.complete(local["thread_id"], local["turn_id"], "Fallback result.")
        self.synchronize(1)
        receipt = self.remote.results[run_id]
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(receipt["result"]["text"], "Fallback result.")
        self.assertEqual(receipt["delivery"], {"route": ROUTE, "outcome": "fallback", "reason": reason})
        return local

    def test_unheld_role_falls_back_and_the_result_records_why(self):
        self.role()
        self.enable()
        self.offer("vacant")
        self.synchronize()
        self.assert_fallback("vacant", "Role 'outcomes' has no holder.")

    def test_closed_holder_falls_back_and_the_result_records_why(self):
        holder = self.conversation()
        self.role(holder)
        self.enable()
        self.store.threads.update(holder, attention="archived")
        self.offer("closed")
        self.synchronize()
        local = self.assert_fallback("closed", "The holder of role 'outcomes' is closed.")
        self.assertNotEqual(local["thread_id"], holder)
        self.assertEqual(self.turns(holder), [])

    def test_role_transfer_between_two_runs_sends_the_second_to_the_successor(self):
        first_holder, successor = self.conversation(), self.conversation()
        role = self.role(first_holder)
        self.enable()
        self.offer("monday")
        self.synchronize()
        monday = self.local("monday")
        self.assertEqual(monday["thread_id"], first_holder)
        self.complete(first_holder, monday["turn_id"], "Monday result.")
        self.synchronize(1)
        self.appoint(role, successor)
        self.offer("tuesday")
        self.synchronize()
        tuesday = self.local("tuesday")
        self.assertEqual(tuesday["thread_id"], successor)
        self.complete(successor, tuesday["turn_id"], "Tuesday result.")
        self.synchronize(1)
        self.assertEqual(self.remote.results["monday"]["delivery"]["thread_id"], first_holder)
        self.assertEqual(self.remote.results["tuesday"]["delivery"]["thread_id"], successor)
        self.assertEqual(self.remote.results["tuesday"]["result"]["text"], "Tuesday result.")
        self.assertEqual([len(self.turns(first_holder)), len(self.turns(successor))], [1, 1])

    def test_holder_outside_the_local_policy_is_not_given_the_run(self):
        holder = self.conversation()
        self.role(holder)
        self.connector.configure({"enabled": True, "allowed_roots": [str(self.root)],
                                  "allowed_permission_modes": {"codex": ["full-access"]}})
        self.assertNotEqual(self.holder_mode, "full-access")
        self.offer("guarded")
        self.synchronize()
        local = self.local("guarded")
        self.assertNotEqual(local["thread_id"], holder)
        self.assertEqual(local["delivery"]["outcome"], "fallback")
        self.assertIn("outside the local Stoic execution policy", local["delivery"]["reason"])
        self.assertEqual(self.turns(holder), [])

    def test_waiting_run_is_delivered_after_the_adapter_and_runtime_restart(self):
        holder = self.conversation()
        self.role(holder)
        self.enable()
        user_turn = self.sessions.send(holder, {"message": "Other work."})["turn"]["id"]
        self.offer("survivor")
        self.synchronize()
        self.assertEqual(self.local("survivor")["state"], "pending")
        with self.store._connection() as db:
            db.execute("UPDATE external_work SET owner='ended-runtime'")
        self.work.holder_policies.clear()
        self.connector.close()
        self.connector = self.restart()
        self.complete(holder, user_turn)
        # Reading the receipt lets the surviving runtime take over the waiting assignment.
        self.assertEqual(self.local("survivor")["state"], "pending")
        self.work.advance_routes()
        self.assertEqual(self.local("survivor")["delivery"]["reason"],
                         "Waiting for the source to restore its delivery policy.")
        self.synchronize(1)
        self.work.advance_routes()
        delivered = self.local("survivor")
        self.assertEqual([delivered["state"], delivered["thread_id"]], ["running", holder])

    def test_uncovered_role_raises_one_notice_until_the_gap_closes(self):
        recipient = self.conversation()
        self.enable(notice_session_id=recipient)
        # A definition whose directory local policy denies belongs to another runner.
        elsewhere = {**self.definition("Remote lead", "job-9"), "cwd": "/another/machine/project"}
        self.remote.definitions = [self.definition(), self.definition(identity="job-2"),
                                   self.definition("Reviewer", "job-3"), elsewhere]
        self.synchronize(1)
        notices = self.notices(recipient)
        self.assertEqual([notice["role"] for notice in notices], ["outcomes", "Reviewer"])
        self.assertEqual(notices[0]["event"], "stoic_role_uncovered")
        self.assertEqual([item["id"] for item in notices[0]["definitions"]], ["job-1", "job-2"])
        self.assertIn("does not exist", notices[0]["reason"])
        uncovered = self.connector.status()["uncovered_roles"]
        self.assertEqual([gap["role"] for gap in uncovered], ["outcomes", "Reviewer"])
        self.assertTrue(all(gap["notified_at"] for gap in uncovered))
        # The same gap on later days, and after a restart, raises nothing further.
        for _ in range(3):
            self.connector.next_coverage_check = 0
            self.synchronize(1)
        self.connector.close()
        self.connector = self.restart()
        self.synchronize(1)
        self.assertEqual(len(self.notices(recipient)), 2)
        calls = [path for _, path, _ in self.remote.calls if path.startswith("/api/execution/definitions?")]
        self.assertEqual(len(calls), 5)
        # Within a day the check is not repeated.
        self.synchronize(3)
        self.assertEqual(len([path for _, path, _ in self.remote.calls
                              if path.startswith("/api/execution/definitions?")]), 5)
        holder = self.conversation()
        role = self.role(holder)
        self.connector.next_coverage_check = 0
        self.synchronize(1)
        self.assertEqual([gap["role"] for gap in self.connector.status()["uncovered_roles"]], ["Reviewer"])
        # A new gap for the same role is announced again, once.
        self.appoint(role, None)
        for _ in range(2):
            self.connector.next_coverage_check = 0
            self.synchronize(1)
        notices = self.notices(recipient)
        self.assertEqual([notice["role"] for notice in notices], ["outcomes", "Reviewer", "outcomes"])
        self.assertEqual(notices[-1]["reason"], "Role 'outcomes' has no holder.")

    def test_fallback_announces_the_gap_once_before_the_daily_check(self):
        recipient = self.conversation()
        self.role()
        self.enable(notice_session_id=recipient)
        self.synchronize(1)
        self.assertEqual(self.notices(recipient), [])
        self.offer("first")
        self.remote.definitions = [self.definition()]
        self.synchronize(3)
        self.offer("second")
        self.synchronize(3)
        self.connector.next_coverage_check = 0
        self.synchronize(1)
        notices = self.notices(recipient)
        self.assertEqual(len(notices), 1)
        self.assertEqual([notices[0]["role"], notices[0]["reason"]], ["outcomes", "Role 'outcomes' has no holder."])
        self.assertEqual(self.local("second")["delivery"]["outcome"], "fallback")

    def test_notice_waits_for_a_recipient_and_is_then_delivered_once(self):
        self.enable()
        self.remote.definitions = [self.definition()]
        self.synchronize(1)
        (gap,) = self.connector.status()["uncovered_roles"]
        self.assertIsNone(gap["notified_at"])
        recipient = self.conversation()
        with self.assertRaisesRegex(Exception, "known conversation"):
            self.connector.configure({"notice_session_id": "missing-conversation"})
        self.connector.configure({"notice_session_id": recipient})
        self.assertEqual(self.connector.status()["notice_session_id"], recipient)
        self.synchronize(2)
        self.assertEqual(len(self.notices(recipient)), 1)
        self.assertIsNotNone(self.connector.status()["uncovered_roles"][0]["notified_at"])

    def test_stoic_release_without_the_definition_list_does_not_disturb_runs(self):
        holder = self.conversation()
        self.role(holder)
        self.enable()
        self.remote.definition_status = 404
        self.offer("routed")
        self.synchronize(3)
        self.assertEqual(self.local("routed")["thread_id"], holder)
        self.assertIsNone(self.connector.status()["route_coverage_checked_at"])
        self.assertEqual(len([path for _, path, _ in self.remote.calls
                              if path.startswith("/api/execution/definitions?")]), 1)
        self.remote.definition_status = 503
        self.connector.next_coverage_check = 0
        self.synchronize(1)
        self.assertIsNone(self.connector.last_error)


if __name__ == "__main__":
    unittest.main()
