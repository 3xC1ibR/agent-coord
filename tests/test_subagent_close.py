from __future__ import annotations

import unittest
from unittest.mock import patch

import test_codex_app_server
import test_ui
from agent_coord import supervision
from agent_coord.app_control import AppControl
from agent_coord.store import CoordinationError
from agent_coord.subagent_close import close_plan, close_with_subagents


def attach(store, parent, child, mode="orchestration"):
    control = AppControl(store)
    payload = {"cwd": store.get_session(parent)["cwd"], "name": "Child", "prompt": "Work"}
    if mode:
        payload["mode"] = mode
    request = control.request("create", parent, payload)
    control.claim(request["request_id"], "test")
    control.identified(request["request_id"], child)
    control.complete(request["request_id"], result={"thread_id": child})


class SubagentCloseTests(unittest.TestCase):
    setUp = test_codex_app_server.BrowserSessionTests.setUp
    create = test_codex_app_server.BrowserSessionTests.create

    def plan_ids(self, parent):
        return [item["thread_id"] for item in close_plan(self.sessions, parent)["subagents"]]

    def test_plan_includes_nested_orchestration_and_delegates_but_not_independent_agents(self):
        parent, child, nested, independent, closed = [self.create() for _ in range(5)]
        attach(self.store, parent, child)
        attach(self.store, child, nested)
        attach(self.store, parent, independent, mode=None)
        attach(self.store, parent, closed)
        self.sessions.close_work_thread(closed)
        self.store.register(session_id="delegate", client="codex", cwd=str(self.root))
        delegation = self.store.create_delegation(parent_session_id=parent, cwd=str(self.root),
            scopes=["file.py"], instructions="Work", mode="reviewed")
        self.store.attach_delegation(delegation["delegation_id"], "delegate")
        self.assertEqual(set(self.plan_ids(parent)), {child, nested, "delegate"})
        self.assertLess(self.plan_ids(parent).index(nested), self.plan_ids(parent).index(child))

    def test_adopted_agents_close_with_the_successor_not_the_retired_lead(self):
        lead, successor, child, nested, kept = [self.create() for _ in range(5)]
        attach(self.store, lead, child)
        attach(self.store, child, nested)
        attach(self.store, lead, kept)
        supervision.transfer(self.store, requester=lead, from_session=lead, to_session=successor,
                             thread_ids=[child], request_id="adopt", reason="Replace lead", workspace=str(self.root))
        self.assertEqual(self.plan_ids(lead), [kept])
        self.assertEqual(self.plan_ids(successor), [nested, child])

    def test_no_closes_only_parent_and_leaves_running_child_and_scope_untouched(self):
        parent, child = self.create(), self.create()
        attach(self.store, parent, child)
        self.sessions.send(child, {"message": "Working"})
        self.store.begin_work(session_id=child, scopes=["file.py"])
        close_with_subagents(self.sessions, parent, {})
        self.assertIn(child, self.sessions.active)
        self.assertEqual(self.store.get_session(child)["write_scope"], ["file.py"])
        self.assertEqual(self.sessions.work_thread(child)["attention"], "now")

    def test_yes_stops_running_and_pending_input_children_before_archiving_parent(self):
        parent, child, nested, unrelated = [self.create() for _ in range(4)]
        attach(self.store, parent, child)
        attach(self.store, child, nested)
        for thread in (child, nested, unrelated):
            self.sessions.send(thread, {"message": "Working"})
            self.assertTrue(self.sessions.persistence.flush(timeout=3, key=thread))
        self.sessions._event({"id": 77, "method": "item/commandExecution/requestApproval", "params": {"threadId": nested}})
        self.store.begin_work(session_id=child, scopes=["file.py"])
        self.assertTrue(all(item["running"] for item in close_plan(self.sessions, parent)["subagents"]))
        result = close_with_subagents(self.sessions, parent, {"subagent_thread_ids": [child, nested]})
        self.assertEqual(result["closed_thread_ids"], [nested, child, parent])
        self.assertEqual(result["subagent_close_errors"], [])
        self.assertEqual(self.store.get_session(child)["write_scope"], [])
        self.assertEqual(self.sessions.pending_requests(nested), [])
        self.assertIn(unrelated, self.sessions.active)
        for thread in (child, nested):
            self.assertEqual(self.sessions.read(thread)["thread"]["turns"][0]["status"], "interrupted")

    def test_failure_reports_partial_result_and_keeps_parent_and_failed_child_open(self):
        parent, first, failed = [self.create() for _ in range(3)]
        attach(self.store, parent, first)
        attach(self.store, parent, failed)
        original = self.sessions.close_work_thread
        def close(thread):
            if thread == failed:
                raise CoordinationError("The turn is still stopping. Retry Close when it has stopped.")
            return original(thread)
        with patch.object(self.sessions, "close_work_thread", side_effect=close):
            result = close_with_subagents(self.sessions, parent, {"subagent_thread_ids": [first, failed]})
        self.assertEqual(result["closed_thread_ids"], [first])
        self.assertEqual(result["attention"], "now")
        self.assertEqual(result["subagent_close_errors"][0]["thread_id"], failed)
        self.assertEqual(self.sessions.work_thread(failed)["attention"], "now")

    def test_changed_snapshot_and_unrelated_ids_are_rejected_before_stopping_anything(self):
        parent, child, other = [self.create() for _ in range(3)]
        attach(self.store, parent, child)
        for ids in ([other], [], [child, other], [child, child], "all"):
            with self.subTest(ids=ids), patch.object(self.sessions, "close_work_thread") as close:
                with self.assertRaises(CoordinationError):
                    close_with_subagents(self.sessions, parent, {"subagent_thread_ids": ids})
                close.assert_not_called()

    def test_new_child_during_shutdown_is_not_silently_closed(self):
        parent, child, late = [self.create() for _ in range(3)]
        attach(self.store, parent, child)
        original = self.sessions.close_work_thread
        def close(thread):
            result = original(thread)
            attach(self.store, parent, late)
            return result
        with patch.object(self.sessions, "close_work_thread", side_effect=close):
            result = close_with_subagents(self.sessions, parent, {"subagent_thread_ids": [child]})
        self.assertEqual(result["closed_thread_ids"], [child])
        self.assertEqual(self.sessions.work_thread(late)["attention"], "now")
        self.assertEqual(result["attention"], "now")
        self.assertIn("changed", result["subagent_close_errors"][0]["error"])

    def test_cycle_terminates_without_including_root(self):
        parent, child = self.create(), self.create()
        attach(self.store, parent, child)
        attach(self.store, child, parent)
        self.assertEqual(self.plan_ids(parent), [child])

    def test_workspace_boundary_is_validated_for_all_children_before_closing(self):
        parent, child = self.create(), self.create()
        attach(self.store, parent, child)
        outside = self.root.parent / "outside"
        self.store.register(session_id="outside", client="codex", cwd=str(outside))
        attach(self.store, parent, "outside")
        with patch.object(self.sessions, "close_work_thread") as close:
            with self.assertRaisesRegex(CoordinationError, "workspace"):
                close_with_subagents(self.sessions, parent, {"subagent_thread_ids": [child, "outside"]})
            close.assert_not_called()


class SubagentCloseHTTPTests(unittest.TestCase):
    setUp = test_ui.BrowserHTTPTests.setUp
    stop_server = test_ui.BrowserHTTPTests.stop_server
    request = test_ui.BrowserHTTPTests.request

    def test_plan_is_read_only_and_close_validates_fields_and_csrf(self):
        parent, child = [self.sessions.create({})["session"]["thread_id"] for _ in range(2)]
        attach(self.store, parent, child)
        path = "/api/browser/threads/" + parent
        calls = list(self.sessions.rpc.calls)
        status, plan = self.request(path + "/close-plan")
        self.assertEqual(status, 200)
        self.assertEqual([item["thread_id"] for item in plan["subagents"]], [child])
        self.assertEqual(self.sessions.rpc.calls, calls)
        body = {"subagent_thread_ids": [child]}
        self.assertEqual(self.request(path + "/close", body, {"X-Agent-Coord-Token": "wrong"})[0], 403)
        self.assertEqual(self.request(path + "/close", {"subagent_thread_ids": True})[0], 400)
        self.assertEqual(self.request(path + "/close", {"unknown": True})[0], 400)
        status, result = self.request(path + "/close", body)
        self.assertEqual(status, 200)
        self.assertEqual(result["closed_thread_ids"], [child, parent])
