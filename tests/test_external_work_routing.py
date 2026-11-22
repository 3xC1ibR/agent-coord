"""Role-routed external work: delivery to a role holder, waiting, and fallback.

An assignment without a route must behave exactly as it did before routing
existed; the first tests pin that behavior down to the stored bytes.
"""
from contextlib import redirect_stderr, redirect_stdout
import copy
import hashlib
import http.client
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from test_codex_app_server import FakeCodex
from agent_coord import cli, workspace_roles
from agent_coord.codex_app_server import BrowserBusyError, BrowserSessions
from agent_coord.external_work import ExternalWorkRuntime, WAITING_REASON
from agent_coord.external_work_control import provision_token, read_token, token_file
from agent_coord.store import CoordinationError, CoordinationStore
from agent_coord.supervision import USER
from agent_coord.ui import make_ui_server

LEGACY_RECEIPT = ["source", "request_id", "state", "thread_id", "turn_id", "error", "created_at", "updated_at",
                  "result", "cancel_requested"]
LEGACY_TABLE = """CREATE TABLE external_work (
    source TEXT NOT NULL, request_id TEXT NOT NULL, digest TEXT NOT NULL, input_json TEXT NOT NULL,
    owner TEXT NOT NULL, state TEXT NOT NULL, thread_id TEXT UNIQUE, turn_id TEXT, attempted INTEGER DEFAULT 0,
    cancel_requested INTEGER DEFAULT 0, result_json TEXT, error TEXT, created_at REAL NOT NULL,
    updated_at REAL NOT NULL, PRIMARY KEY(source, request_id))"""
ROUTE = {"role": "outcomes"}


class RoutingFixture(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.store = CoordinationStore(self.root / "state.sqlite3")
        self.sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(self.sessions.close)
        self.work = self.sessions.external_work

    def conversation(self, **settings):
        return self.sessions.create(settings)["session"]["thread_id"]

    def role(self, holder=None, name="outcomes"):
        role = workspace_roles.create_role(self.store, workspace=str(self.root), name=name, requester_kind=USER)
        if holder:
            self.appoint(role, holder)
        return role

    def appoint(self, role, holder):
        return workspace_roles.appoint(self.store, role_id=role["role_id"], holder=holder, requester_kind=USER,
                                       workspace=str(self.root))

    def submit(self, key, prompt="Compute.", route=ROUTE, settings=None, **kwargs):
        return self.work.submit("test", key, {"cwd": str(self.root)} if settings is None else settings, prompt,
                                route=route, **kwargs)

    def calls(self, method, thread=None):
        return [params for name, params in self.sessions.rpc.calls
                if name == method and (thread is None or params.get("threadId") == thread)]

    def complete(self, thread, turn, text="Done.", status="completed"):
        self.sessions._event({"method": "turn/completed", "params": {"threadId": thread, "turn": {
            "id": turn, "status": status,
            "items": [{"type": "agentMessage", "id": "answer", "phase": "final_answer", "text": text}]}}})
        self.assertTrue(self.sessions.persistence.flush(key=thread))

    def occupy(self, thread, message="Other work."):
        """Start an ordinary user turn in a conversation and return its turn ID."""
        return self.sessions.send(thread, {"message": message})["turn"]["id"]

    def row(self, key):
        with self.store.reading(), self.store._connection() as db:
            return dict(db.execute("SELECT * FROM external_work WHERE source='test' AND request_id=?", (key,)).fetchone())


class UnroutedBehaviorTests(RoutingFixture):
    def test_assignment_without_route_keeps_stored_input_receipt_and_provider_calls(self):
        settings = {"client": "codex", "model": "available-model", "effort": "high", "cwd": str(self.root)}
        prompt = "Stoic execution context (immutable occurrence):\n{}\n\nInstructions:\nWrite a brief."
        # A held role in the same workspace must not attract an assignment that names no route.
        holder = self.conversation()
        self.role(holder)
        before = len(self.sessions.rpc.calls)
        receipt = self.work.submit("test", "plain", settings, prompt, name="Daily brief")
        self.assertEqual(list(receipt), LEGACY_RECEIPT)
        self.assertEqual(receipt["state"], "running")
        self.assertNotEqual(receipt["thread_id"], holder)
        stored = self.row("plain")
        encoded = json.dumps({"settings": settings, "prompt": prompt, "name": "Daily brief"},
                             sort_keys=True, separators=(",", ":"))
        self.assertEqual(stored["input_json"], encoded)
        self.assertEqual(stored["digest"], hashlib.sha256(encoded.encode()).hexdigest())
        for column in ("route_json", "delivery_json", "holder_policy", "ask_floor", "submission_id"):
            self.assertIsNone(stored[column], column)
        methods = [name for name, _ in self.sessions.rpc.calls[before:]]
        self.assertEqual(methods.count("thread/start"), 1)
        self.assertEqual(methods.count("turn/start"), 1)
        started = self.calls("turn/start", receipt["thread_id"])[0]
        self.assertEqual(started["input"], [{"type": "text", "text": prompt}])
        self.assertEqual([started[key] for key in ("model", "effort")], ["available-model", "high"])
        self.assertEqual(self.calls("turn/start", holder), [])
        self.assertEqual(self.store.threads.get(receipt["thread_id"])["title"], "Daily brief")
        self.assertEqual(self.work.submit("test", "plain", settings, prompt, name="Daily brief"), receipt)
        self.complete(receipt["thread_id"], receipt["turn_id"], "Brief ready.")
        finished = self.work.get("test", "plain")
        self.assertEqual(list(finished), LEGACY_RECEIPT)
        self.assertEqual(finished["result"], {"text": "Brief ready.", "artifacts": []})
        self.assertIsNone(self.work.routing["worker"])

    def test_existing_database_keeps_assignments_and_loses_the_single_conversation_constraint(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        inputs = json.dumps({"settings": {}, "prompt": "Compute.", "name": None}, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(inputs.encode()).hexdigest()
        store = CoordinationStore(root / "state.sqlite3")
        with store._connection() as db:
            db.execute(LEGACY_TABLE)
            for name in ("evidence_state", "evidence_result_json", "evidence_error", "resolution_digest", "resolution_method"):
                db.execute(f"ALTER TABLE external_work ADD COLUMN {name} TEXT")
            db.execute("""INSERT INTO external_work (source,request_id,digest,input_json,owner,state,thread_id,turn_id,
                attempted,result_json,created_at,updated_at,resolution_method) VALUES
                ('test','old',?,?,'earlier-runtime','succeeded','thread-old','turn-old',1,?,10.0,11.0,'provider_evidence')""",
                (digest, inputs, json.dumps({"text": "Kept.", "artifacts": []})))
        sessions = BrowserSessions(store, str(root), rpc_factory=FakeCodex)
        self.addCleanup(sessions.close)
        work = sessions.external_work
        kept = work.get("test", "old")
        self.assertEqual(kept, {"source": "test", "request_id": "old", "state": "succeeded", "thread_id": "thread-old",
                                "turn_id": "turn-old", "error": None, "created_at": 10.0, "updated_at": 11.0,
                                "result": {"text": "Kept.", "artifacts": []}, "cancel_requested": False,
                                "resolution_method": "provider_evidence"})
        # The saved identity still answers an identical retry without launching anything.
        self.assertEqual(work.submit("test", "old", {}, "Compute."), kept)
        self.assertFalse(any(name == "thread/start" for name, _ in sessions.rpc.calls))
        with store.reading(), store._connection() as db:
            indexes = {row["name"]: row["origin"] for row in db.execute("PRAGMA index_list(external_work)")}
            tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'external_work%' AND type='table'")]
        self.assertEqual(tables, ["external_work"])
        self.assertNotIn("u", indexes.values())
        self.assertLessEqual({"external_work_order", "external_work_source", "external_work_active_owner",
                              "external_work_thread"}, set(indexes))
        with store._connection() as db:
            db.execute("""INSERT INTO external_work (source,request_id,digest,input_json,owner,state,thread_id,
                created_at,updated_at) VALUES ('test','second','d','{}','earlier-runtime','failed','thread-old',12.0,12.0)""")
        # Reopening the migrated database is a no-op.
        again = ExternalWorkRuntime(sessions)
        self.assertEqual(again.get("test", "old"), kept)

    def test_route_must_be_an_object_and_is_part_of_the_assignment_identity(self):
        for invalid in ("outcomes", [], {}, 7, {"role": "x" * 3000}):
            with self.subTest(route=invalid), self.assertRaises(CoordinationError):
                self.submit("invalid", route=invalid)
        self.assertIsNone(self.work.get("test", "invalid"))
        holder = self.conversation()
        self.role(holder)
        first = self.submit("identity")
        self.assertEqual(self.submit("identity"), first)
        for changed in ({"role": "another"}, None):
            with self.subTest(route=changed), self.assertRaises(BrowserBusyError):
                self.submit("identity", route=changed)
        self.assertEqual(len(self.calls("turn/start")), 1)


class RoleDeliveryTests(RoutingFixture):
    def test_idle_holder_receives_the_assignment_as_a_new_turn_and_its_final_message_is_the_result(self):
        holder = self.conversation(model="other-model", effort="low")
        self.store.threads.update(holder, title="Outcomes desk")
        role = self.role(holder)
        threads = len(self.calls("thread/start"))
        prompt = "Stoic execution context (immutable occurrence):\n{}\n\nInstructions:\n  Check the outcomes.\n"
        # The assignment's own settings describe a different provider selection; the holder keeps its own.
        receipt = self.submit("first", prompt, settings={"cwd": str(self.root), "model": "available-model",
                                                         "effort": "high"}, name="Daily outcomes")
        self.assertEqual(receipt["state"], "running")
        self.assertEqual(receipt["thread_id"], holder)
        self.assertEqual(receipt["delivery"], {"route": ROUTE, "outcome": "holder", "role_id": role["role_id"],
                                               "thread_id": holder})
        self.assertEqual(len(self.calls("thread/start")), threads)
        (turn,) = self.calls("turn/start", holder)
        self.assertEqual(turn["input"], [{"type": "text", "text": prompt}])
        self.assertEqual([turn["model"], turn["effort"]], ["other-model", "low"])
        self.assertEqual(self.store.threads.get(holder)["title"], "Outcomes desk")
        self.complete(holder, receipt["turn_id"], '{"text":"Outcomes reviewed.","artifacts":["outcomes.md"]}')
        done = self.work.get("test", "first")
        self.assertEqual(done["state"], "succeeded")
        self.assertEqual(done["result"], {"text": "Outcomes reviewed.", "artifacts": ["outcomes.md"]})
        self.assertEqual(done["delivery"]["outcome"], "holder")
        # The next run is another turn in the same conversation.
        second = self.submit("second", "Check again.")
        self.assertEqual([second["state"], second["thread_id"]], ["running", holder])
        self.assertNotEqual(second["turn_id"], receipt["turn_id"])
        self.assertEqual(len(self.calls("thread/start")), threads)
        self.complete(holder, second["turn_id"], "Second result.")
        self.assertEqual(self.work.get("test", "second")["result"]["text"], "Second result.")
        self.assertEqual(self.work.get("test", "first")["result"]["text"], "Outcomes reviewed.")
        self.assertEqual(self.submit("first", prompt, settings={"cwd": str(self.root), "model": "available-model",
                                                                "effort": "high"}, name="Daily outcomes"), done)
        self.assertEqual(len(self.calls("turn/start", holder)), 2)

    def test_role_name_matches_without_regard_to_case_or_spacing(self):
        holder = self.conversation()
        self.role(holder, name="Outcomes Lead")
        receipt = self.submit("spaced", route={"role": "  outcomes   lead "})
        self.assertEqual([receipt["state"], receipt["thread_id"]], ["running", holder])

    def test_busy_holder_receives_the_assignment_after_its_current_turn_in_submission_order(self):
        holder = self.conversation()
        self.role(holder)
        user_turn = self.occupy(holder)
        first = self.submit("first", "First run.")
        self.assertEqual(first["state"], "pending")
        self.assertIsNone(first["thread_id"])
        self.assertIsNone(first["turn_id"])
        self.assertEqual(first["delivery"], {"route": ROUTE, "outcome": "waiting", "reason": WAITING_REASON,
                                             "thread_id": holder})
        second = self.submit("second", "Second run.")
        self.assertEqual(second["state"], "pending")
        self.assertEqual(self.work.advance_routes(), 2)
        self.assertEqual(len(self.calls("turn/start", holder)), 1)
        self.assertEqual(len(self.calls("thread/start")), 1)
        self.assertEqual(self.submit("first", "First run."), first)
        self.complete(holder, user_turn)
        self.work.advance_routes()
        first = self.work.get("test", "first")
        self.assertEqual([first["state"], first["thread_id"]], ["running", holder])
        self.assertEqual(self.work.get("test", "second")["state"], "pending")
        started = self.calls("turn/start", holder)
        self.assertEqual(started[-1]["input"], [{"type": "text", "text": "First run."}])
        self.complete(holder, first["turn_id"], "First result.")
        self.assertEqual(self.work.advance_routes(), 1)
        second = self.work.get("test", "second")
        self.assertEqual([second["state"], second["thread_id"]], ["running", holder])
        self.complete(holder, second["turn_id"], "Second result.")
        self.assertEqual(self.work.get("test", "first")["result"]["text"], "First result.")
        self.assertEqual(self.work.get("test", "second")["result"]["text"], "Second result.")
        self.assertEqual(self.work.advance_routes(), 0)
        self.assertEqual([params["input"][0]["text"] for params in self.calls("turn/start", holder)],
                         ["Other work.", "First run.", "Second run."])
        self.assertEqual(len(self.calls("turn/steer")), 0)

    def test_assignment_waits_behind_messages_the_user_queued_for_the_holder(self):
        holder = self.conversation()
        self.role(holder)
        user_turn = self.occupy(holder)
        # Hold the queue's own dispatch so the order of the two deliveries is observable.
        with patch.object(self.sessions.queue, "wake"):
            self.sessions.queue.enqueue(holder, {"message": "Queued follow-up."})
            waiting = self.submit("queued", "Routed run.")
            self.assertEqual(waiting["state"], "pending")
            self.complete(holder, user_turn)
            self.work.advance_routes()
            self.assertEqual(self.work.get("test", "queued")["state"], "pending")
            self.assertEqual(len(self.calls("turn/start", holder)), 1)
            (item,) = self.sessions.queue.list(holder)
            self.sessions.queue.change(holder, {"action": "cancel", "id": item["id"]})
            self.work.advance_routes()
        delivered = self.work.get("test", "queued")
        self.assertEqual([delivered["state"], delivered["thread_id"]], ["running", holder])
        self.assertEqual(self.calls("turn/start", holder)[-1]["input"], [{"type": "text", "text": "Routed run."}])

    def test_waiting_assignment_can_be_cancelled_and_is_never_sent(self):
        holder = self.conversation()
        self.role(holder)
        user_turn = self.occupy(holder)
        self.submit("cancelled")
        cancelled = self.work.cancel("test", "cancelled")
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertEqual(cancelled["delivery"]["outcome"], "waiting")
        self.complete(holder, user_turn)
        self.assertEqual(self.work.advance_routes(), 0)
        self.assertEqual(len(self.calls("turn/start", holder)), 1)

    def test_provider_refusal_at_submission_returns_the_assignment_to_waiting(self):
        holder = self.conversation()
        self.role(holder)
        with patch.object(self.sessions, "send", side_effect=BrowserBusyError("A turn is already running.")):
            waiting = self.submit("raced")
        self.assertEqual(waiting["state"], "pending")
        self.assertIsNone(waiting["thread_id"])
        self.assertEqual(waiting["delivery"]["outcome"], "waiting")
        self.assertEqual(self.calls("turn/start", holder), [])
        self.work.advance_routes()
        delivered = self.work.get("test", "raced")
        self.assertEqual([delivered["state"], delivered["thread_id"]], ["running", holder])
        self.assertEqual(len(self.calls("turn/start", holder)), 1)

    def test_ambiguous_submission_to_the_holder_is_uncertain_and_never_replayed(self):
        holder = self.conversation()
        self.role(holder)
        self.sessions.rpc.fail_turn = True
        uncertain = self.submit("ambiguous")
        self.sessions.rpc.fail_turn = False
        self.assertEqual(uncertain["state"], "uncertain")
        self.assertEqual(uncertain["thread_id"], holder)
        self.assertIn("will not be replayed", uncertain["error"])
        self.assertEqual(self.work.advance_routes(), 0)
        self.assertEqual(self.submit("ambiguous"), uncertain)
        self.assertEqual(len(self.calls("turn/start", holder)), 1)
        self.assertEqual(len(self.calls("thread/start")), 1)


class FallbackTests(RoutingFixture):
    def assert_fallback(self, key, reason, **submit):
        """The fallback is the ordinary new-conversation launch with the assignment's settings."""
        settings = {"cwd": str(self.root), "model": "available-model", "effort": "high"}
        before = len(self.sessions.rpc.calls)
        receipt = self.submit(key, "Run the job.", settings=settings, name="Daily job", **submit)
        routed = copy.deepcopy(self.sessions.rpc.calls[before:])
        before = len(self.sessions.rpc.calls)
        plain = self.work.submit("test", key + "-plain", settings, "Run the job.", name="Daily job")
        unrouted = copy.deepcopy(self.sessions.rpc.calls[before:])
        self.assertEqual(receipt["state"], "running", receipt)
        self.assertEqual(receipt["delivery"], {"route": submit.get("route", ROUTE), "outcome": "fallback", "reason": reason})
        self.assertNotEqual(receipt["thread_id"], plain["thread_id"])

        def normalized(calls, thread):
            return [(name, json.loads(json.dumps(params).replace(thread, "THREAD"))) for name, params in calls
                    if name in {"thread/start", "turn/start"}]

        self.assertEqual(normalized(routed, receipt["thread_id"]), normalized(unrouted, plain["thread_id"]))
        self.assertEqual([name for name, _ in normalized(routed, receipt["thread_id"])], ["thread/start", "turn/start"])
        self.assertEqual(self.store.threads.get(receipt["thread_id"])["title"], "Daily job")
        self.complete(receipt["thread_id"], receipt["turn_id"], "Fallback result.")
        done = self.work.get("test", key)
        self.assertEqual([done["state"], done["result"]["text"]], ["succeeded", "Fallback result."])
        self.assertEqual(done["delivery"]["outcome"], "fallback")
        return done

    def test_role_without_a_holder_runs_in_a_new_conversation_and_records_why(self):
        self.role()
        self.assert_fallback("vacant", "Role 'outcomes' has no holder.")

    def test_unknown_role_runs_in_a_new_conversation_and_records_why(self):
        self.assert_fallback("unknown", f"Role 'outcomes' does not exist in the workspace of {self.root}.")

    def test_closed_holder_runs_in_a_new_conversation_and_receives_nothing(self):
        holder = self.conversation()
        self.role(holder)
        self.store.threads.update(holder, attention="archived")
        self.assert_fallback("closed", "The holder of role 'outcomes' is closed.")
        self.assertEqual(self.calls("turn/start", holder), [])

    def test_holder_closed_while_the_assignment_waits_falls_back_instead_of_waiting_forever(self):
        holder = self.conversation()
        self.role(holder)
        user_turn = self.occupy(holder)
        waiting = self.submit("late", "Run the job.")
        self.assertEqual(waiting["state"], "pending")
        self.complete(holder, user_turn)
        self.store.threads.update(holder, attention="archived")
        self.work.advance_routes()
        fallen = self.work.get("test", "late")
        self.assertEqual(fallen["state"], "running")
        self.assertNotEqual(fallen["thread_id"], holder)
        self.assertEqual(fallen["delivery"], {"route": ROUTE, "outcome": "fallback",
                                              "reason": "The holder of role 'outcomes' is closed."})
        self.assertEqual(len(self.calls("turn/start", holder)), 1)

    def test_terminal_session_holder_cannot_receive_a_turn(self):
        self.store.register(session_id="terminal-lead", client="codex", cwd=str(self.root))
        self.role("terminal-lead")
        self.assert_fallback("terminal", "The holder of role 'outcomes' is not a conversation this runtime can deliver to.")

    def test_unsupported_route_runs_in_a_new_conversation(self):
        self.assert_fallback("unsupported", "This runtime does not support the requested route.",
                             route={"conversation": "thread-9"})

    def test_holder_refused_by_the_source_policy_runs_in_a_new_conversation(self):
        holder = self.conversation()
        self.role(holder)
        seen = []

        def refuse(record):
            seen.append(record["thread_id"])
            raise CoordinationError("The role holder is outside the local execution policy.")

        self.assert_fallback("refused", "The role holder is outside the local execution policy.", holder_policy=refuse)
        self.assertEqual(seen, [holder])
        self.assertEqual(self.calls("turn/start", holder), [])
        accepted = self.work.submit("other", "accepted", {"cwd": str(self.root)}, "Compute.", route=ROUTE,
                                    holder_policy=lambda record: None)
        self.assertEqual([accepted["state"], accepted["thread_id"]], ["running", holder])

    def test_fallback_with_invalid_settings_fails_like_an_ordinary_assignment(self):
        self.role()
        failed = self.submit("invalid-settings", settings={"cwd": str(self.root), "model": "no-model"})
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(failed["delivery"]["outcome"], "fallback")
        self.assertEqual(self.calls("thread/start"), [])


class RoleTransferTests(RoutingFixture):
    def test_role_transferred_between_two_runs_sends_the_second_run_to_the_successor(self):
        first_holder, successor = self.conversation(), self.conversation()
        role = self.role(first_holder)
        first = self.submit("monday")
        self.assertEqual(first["thread_id"], first_holder)
        self.complete(first_holder, first["turn_id"], "Monday result.")
        self.appoint(role, successor)
        second = self.submit("tuesday")
        self.assertEqual([second["state"], second["thread_id"]], ["running", successor])
        self.assertEqual(second["delivery"]["thread_id"], successor)
        self.complete(successor, second["turn_id"], "Tuesday result.")
        self.assertEqual(self.work.get("test", "monday")["result"]["text"], "Monday result.")
        self.assertEqual(self.work.get("test", "tuesday")["result"]["text"], "Tuesday result.")
        self.assertEqual(len(self.calls("turn/start", first_holder)), 1)
        self.assertEqual(len(self.calls("turn/start", successor)), 1)
        self.assertEqual(len(self.calls("thread/start")), 2)

    def test_run_waiting_for_a_busy_holder_follows_the_role_to_its_successor(self):
        first_holder, successor = self.conversation(), self.conversation()
        role = self.role(first_holder)
        self.occupy(first_holder)
        waiting = self.submit("handover")
        self.assertEqual(waiting["delivery"]["thread_id"], first_holder)
        self.appoint(role, successor)
        self.work.advance_routes()
        delivered = self.work.get("test", "handover")
        self.assertEqual([delivered["state"], delivered["thread_id"]], ["running", successor])
        self.assertEqual(len(self.calls("turn/start", first_holder)), 1)

    def test_vacated_role_releases_a_waiting_run_to_the_fallback(self):
        holder = self.conversation()
        role = self.role(holder)
        self.occupy(holder)
        self.submit("vacated")
        self.appoint(role, None)
        self.work.advance_routes()
        fallen = self.work.get("test", "vacated")
        self.assertEqual(fallen["state"], "running")
        self.assertEqual(fallen["delivery"]["reason"], "Role 'outcomes' has no holder.")

    def test_nearest_workspace_role_is_chosen_for_the_working_directory(self):
        project = self.root / "project"
        project.mkdir()
        outer, inner = self.conversation(), self.sessions.create({"cwd": str(project)})["session"]["thread_id"]
        self.role(outer)
        with self.store._connection() as db:
            # A project can define its own role of the same name beneath the workspace role.
            workspace_roles.ensure_schema(db)
            db.execute("""INSERT INTO workspace_roles (role_id,workspace,name,holder_session_id,created_at,updated_at)
                VALUES ('inner-role',?,?,?,1,1)""", (str(project), "outcomes", inner))
        nested = self.submit("nested", settings={"cwd": str(project)})
        self.assertEqual(nested["thread_id"], inner)
        self.assertEqual(self.submit("outer")["thread_id"], outer)
        status = self.work.inspect_route(ROUTE, str(project))
        self.assertEqual([status["deliverable"], status["role_id"]], [True, "inner-role"])


class SharedConversationTests(RoutingFixture):
    def test_repeated_completion_of_an_earlier_run_does_not_disturb_the_current_run(self):
        holder = self.conversation()
        self.role(holder)
        first = self.submit("first")
        self.complete(holder, first["turn_id"], "First result.")
        second = self.submit("second")
        # A provider can repeat a terminal event after a reconnect.
        self.complete(holder, first["turn_id"], "Changed text.")
        self.assertEqual(self.work.get("test", "second")["state"], "running")
        self.assertEqual(self.work.get("test", "first")["result"]["text"], "First result.")
        self.complete(holder, second["turn_id"], "Second result.")
        self.assertEqual(self.work.get("test", "second")["result"]["text"], "Second result.")

    def test_turns_the_user_runs_in_the_holder_are_not_reported_as_results(self):
        holder = self.conversation()
        self.role(holder)
        first = self.submit("first")
        self.complete(holder, first["turn_id"], "Assigned result.")
        user_turn = self.occupy(holder)
        self.complete(holder, user_turn, "Unrelated answer.")
        self.assertEqual(self.work.get("test", "first")["result"]["text"], "Assigned result.")
        self.assertEqual(self.work.observers, {})

    def test_uncertain_run_keeps_its_evidence_watch_when_a_later_run_uses_the_conversation(self):
        holder = self.conversation()
        self.role(holder)
        first = self.submit("first")
        with patch.object(self.sessions, "interrupt", side_effect=CoordinationError("Stop was not confirmed.")):
            self.assertEqual(self.work.cancel("test", "first")["state"], "uncertain")
        # The provider never reported the first turn; the holder later becomes idle again.
        with self.sessions.lock:
            self.sessions.active.pop(holder, None)
        with patch.object(self.sessions, "read", return_value={"running": False, "queuedMessages": []}):
            second = self.submit("second")
        self.assertEqual([second["state"], second["thread_id"]], ["running", holder])
        self.assertIn((holder, first["turn_id"]), self.work.retained)
        self.complete(holder, second["turn_id"], "Second result.")
        self.assertEqual(self.work.get("test", "second")["result"]["text"], "Second result.")
        self.assertEqual(self.work.get("test", "first")["state"], "uncertain")
        self.complete(holder, first["turn_id"], "Late first result.")
        resolved = self.work.resolve("test", "first")
        self.assertEqual(resolved["state"], "cancelled")
        self.assertEqual(self.work.retained, {})

    def test_question_the_holder_asked_earlier_does_not_fail_the_assignment(self):
        holder = self.conversation()
        self.role(holder)

        def ask(question):
            with self.store._connection() as db:
                db.execute("""INSERT INTO asks (session_id,question,blocking,status,created_at,updated_at)
                    VALUES (?,?,1,'open',1,1)""", (holder, question))

        ask("Which report format?")
        first = self.submit("first")
        self.complete(holder, first["turn_id"], "Result despite the earlier question.")
        self.assertEqual(self.work.get("test", "first")["state"], "succeeded")
        second = self.submit("second")
        ask("May I delete the old report?")
        self.complete(holder, second["turn_id"], "I need an answer first.")
        failed = self.work.get("test", "second")
        self.assertEqual([failed["state"], failed["error"]], ["failed", "The assigned turn ended awaiting user input."])


class RestartTests(RoutingFixture):
    def abandon(self, key):
        """Make the assignment belong to a runtime that no longer exists."""
        with self.store._connection() as db:
            db.execute("UPDATE external_work SET owner='ended-runtime' WHERE source='test' AND request_id=?", (key,))

    def test_waiting_assignment_is_continued_by_the_next_runtime_without_operator_review(self):
        holder = self.conversation()
        self.role(holder)
        user_turn = self.occupy(holder)
        self.submit("waiting")
        self.abandon("waiting")
        self.assertEqual(self.work.get("test", "waiting")["state"], "pending")
        self.assertEqual(self.row("waiting")["owner"], self.work.owner)
        self.complete(holder, user_turn)
        self.work.advance_routes()
        delivered = self.work.get("test", "waiting")
        self.assertEqual([delivered["state"], delivered["thread_id"]], ["running", holder])

    def test_assignment_in_another_workspace_is_not_continued_here(self):
        elsewhere = tempfile.TemporaryDirectory()
        self.addCleanup(elsewhere.cleanup)
        holder = self.conversation()
        self.role(holder)
        self.occupy(holder)
        self.submit("foreign")
        with self.store._connection() as db:
            inputs = json.loads(db.execute("SELECT input_json FROM external_work WHERE request_id='foreign'").fetchone()[0])
            inputs["settings"]["cwd"] = str(Path(elsewhere.name).resolve())
            db.execute("UPDATE external_work SET input_json=? WHERE request_id='foreign'", (json.dumps(inputs),))
        self.abandon("foreign")
        self.assertEqual(self.work.get("test", "foreign")["state"], "uncertain")

    def test_started_assignment_of_an_ended_runtime_stays_uncertain(self):
        holder = self.conversation()
        self.role(holder)
        running = self.submit("running")
        self.abandon("running")
        uncertain = self.work.get("test", "running")
        self.assertEqual([uncertain["state"], uncertain["thread_id"], uncertain["turn_id"]],
                         ["uncertain", holder, running["turn_id"]])
        self.assertEqual(self.work.advance_routes(), 0)
        self.assertEqual(len(self.calls("turn/start", holder)), 1)

    def test_turn_of_an_ended_runtime_is_recovered_from_the_source_qualified_submission(self):
        holder = self.conversation()
        self.role(holder)
        running = self.submit("lost-binding")
        # The same request ID from another source shares the holder's conversation later.
        with self.store._connection() as db:
            db.execute("UPDATE external_work SET turn_id=NULL,state='pending' WHERE request_id='lost-binding'")
        self.abandon("lost-binding")
        recovered = self.work.get("test", "lost-binding")
        self.assertEqual([recovered["state"], recovered["turn_id"]], ["uncertain", running["turn_id"]])
        self.assertTrue(self.row("lost-binding")["submission_id"].startswith("external-work:"))

    def test_assignment_submitted_with_a_policy_waits_until_its_source_restores_the_policy(self):
        holder = self.conversation()
        self.role(holder)
        user_turn = self.occupy(holder)
        self.submit("guarded", holder_policy=lambda record: None)
        self.complete(holder, user_turn)
        self.abandon("guarded")
        self.work.get("test", "guarded")
        self.work.holder_policies.clear()
        self.work.advance_routes()
        waiting = self.work.get("test", "guarded")
        self.assertEqual(waiting["state"], "pending")
        self.assertEqual(waiting["delivery"]["reason"], "Waiting for the source to restore its delivery policy.")
        self.assertEqual(len(self.calls("turn/start", holder)), 1)
        self.work.register_holder_policy("test", lambda record: None)
        self.work.advance_routes()
        self.assertEqual(self.work.get("test", "guarded")["thread_id"], holder)

    def test_background_worker_delivers_a_waiting_assignment_and_then_stops(self):
        holder = self.conversation()
        self.role(holder)
        user_turn = self.occupy(holder)
        with patch("agent_coord.external_work.ROUTING_INTERVAL_SECONDS", 0.02):
            self.submit("background")
            worker = self.work.routing["worker"]
            self.assertIsNotNone(worker)
            self.complete(holder, user_turn)
            worker.join(timeout=10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.work.get("test", "background")["thread_id"], holder)
        self.assertIsNone(self.work.routing["worker"])


class RoutingInterfaceTests(RoutingFixture):
    def test_inspection_reports_coverage_without_delivering(self):
        self.assertEqual(self.work.inspect_route(ROUTE, str(self.root)), {
            "deliverable": False, "reason": f"Role 'outcomes' does not exist in the workspace of {self.root}.",
            "thread_id": None, "role_id": None, "role": "outcomes"})
        holder = self.conversation()
        role = self.role(holder)
        self.assertEqual(self.work.inspect_route(ROUTE, str(self.root)), {
            "deliverable": True, "reason": None, "thread_id": holder, "role_id": role["role_id"], "role": "outcomes"})

        def refuse(record):
            raise CoordinationError("Refused.")

        refused = self.work.inspect_route(ROUTE, str(self.root), holder_policy=refuse)
        self.assertEqual([refused["deliverable"], refused["reason"], refused["role_id"]], [False, "Refused.", role["role_id"]])
        self.appoint(role, None)
        vacant = self.work.inspect_route(ROUTE, str(self.root))
        self.assertEqual([vacant["deliverable"], vacant["role_id"]], [False, role["role_id"]])
        self.assertEqual(self.calls("turn/start"), [])

    def test_http_and_command_line_submission_accept_a_route(self):
        holder = self.conversation()
        self.role(holder)
        server = make_ui_server(self.store, port=0, browser_sessions=self.sessions)
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        provision_token(self.store.database_path)
        token = read_token(token_file(self.store.database_path)).decode()
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
        self.addCleanup(connection.close)
        connection.request("POST", "/api/external-work", json.dumps({
            "source": "http", "request_id": "routed", "settings": {"cwd": str(self.root)}, "prompt": "Compute.",
            "route": ROUTE}), {"Content-Type": "application/json", "Authorization": "Bearer " + token})
        reply = connection.getresponse()
        receipt = json.load(reply)
        self.assertEqual(reply.status, 200, receipt)
        self.assertEqual([receipt["thread_id"], receipt["delivery"]["outcome"]], [holder, "holder"])
        self.complete(holder, receipt["turn_id"])
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            code = cli.main(["--db", str(self.store.database_path), "external-work",
                             "--url", f"http://127.0.0.1:{server.server_port}", "submit", "--source", "cli",
                             "--request-id", "routed", "--settings", json.dumps({"cwd": str(self.root)}),
                             "--route", json.dumps(ROUTE), "Compute again."])
        self.assertEqual(code, 0, output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["thread_id"], holder)
        self.assertEqual(len(self.calls("turn/start", holder)), 2)


if __name__ == "__main__":
    unittest.main()
