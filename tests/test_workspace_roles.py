from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))

from agent_coord import supervision, workspace_roles
from agent_coord.app_agent_failures import notify_agent_failure
from agent_coord.store import CoordinationError, CoordinationStore
from test_subagent_close import attach


class WorkspaceRoleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = CoordinationStore(self.root / "coord.sqlite3")
        for session in ("lead", "next", "other", "c1", "c2", "g1"):
            self.store.register(session_id=session, client="codex", cwd=str(self.root))
            self.store.threads.ensure(session)
        attach(self.store, "lead", "c1")
        attach(self.store, "lead", "c2")
        attach(self.store, "c1", "g1")

    def role(self, name="Field lead", holder=None):
        role = workspace_roles.create_role(self.store, workspace=str(self.root), name=name, requester_kind="user")
        if holder:
            workspace_roles.appoint(self.store, role_id=role["role_id"], holder=holder, requester_kind="user",
                                    workspace=str(self.root))
        return role["role_id"]

    def appoint(self, role_id, holder, **kwargs):
        kwargs.setdefault("requester_kind", "user")
        return workspace_roles.appoint(self.store, role_id=role_id, holder=holder, workspace=str(self.root), **kwargs)

    def process(self):
        return workspace_roles.process(self.store, str(self.root))

    def supervisor(self, thread):
        with self.store._connection() as db:
            return supervision.effective_supervisor(db, thread)

    def by_user(self, thread):
        with self.store._connection() as db:
            return supervision.supervised_by_user(db, thread)

    def rows(self, query, *values):
        with self.store._connection() as db:
            return [tuple(row) for row in db.execute(query, values)]

    def test_appointment_moves_idle_agents_now_and_busy_agents_when_their_turn_ends(self):
        role = self.role(holder="lead")
        self.store.touch("c2", turn_active=True)
        result = self.appoint(role, "next")
        self.assertEqual((result["moved_thread_ids"], result["deferred_thread_ids"]), (["c1"], ["c2"]))
        self.assertEqual([self.supervisor(t) for t in ("c1", "c2", "g1")], ["next", "lead", "c1"])
        with self.store._connection() as db:
            self.assertEqual(supervision.creator(db, "c1"), "lead")
        self.assertEqual(self.process(), 0)
        self.assertEqual(self.rows("SELECT thread_id, blocker FROM supervision_pending_moves"), [("c2", "a turn is active")])
        self.store.touch("c2", turn_active=False)
        self.assertEqual(self.process(), 1)
        self.assertEqual(self.supervisor("c2"), "next")
        self.assertEqual(self.rows("SELECT COUNT(*) FROM supervision_pending_moves"), [(0,)])
        notices = self.rows("""SELECT recipient_session_id, classification FROM messages
            WHERE thread_id LIKE 'supervision-transfer:%' ORDER BY id""")
        self.assertEqual({row[1] for row in notices}, {"informational"})
        self.assertEqual({row[0] for row in notices}, {"lead", "next", "c1", "c2"})
        listed = workspace_roles.list_roles(self.store, str(self.root))["data"]
        self.assertEqual([(r["name"], r["holder_session_id"], r["agent_count"]) for r in listed], [("Field lead", "next", 2)])

    def test_only_the_user_or_holder_changes_a_holder_and_a_session_holds_one_role(self):
        created = workspace_roles.create_role(self.store, workspace=str(self.root), name="Chief of staff",
                                              requester_kind="session", requester_session="lead")
        role = created["role_id"]
        self.assertEqual(created["holder_session_id"], "lead")
        with self.assertRaisesRegex(CoordinationError, "Only the user or the role's current holder"):
            self.appoint(role, "other", requester_kind="session", requester_session="other")
        handoff = self.appoint(role, "next", requester_kind="session", requester_session="lead")
        self.assertEqual((handoff["holder_session_id"], sorted(handoff["moved_thread_ids"])), ("next", ["c1", "c2"]))
        with self.assertRaisesRegex(CoordinationError, "already exists"):
            self.role("chief OF staff")
        second = self.role("Reviewer")
        self.appoint(second, "next")
        held = {r["name"]: r["holder_session_id"] for r in workspace_roles.list_roles(self.store, str(self.root))["data"]}
        self.assertEqual(held, {"Chief of staff": None, "Reviewer": "next"})
        self.store.threads.update("other", attention="archived")
        with self.assertRaisesRegex(CoordinationError, "closed or ended session cannot hold a role"):
            self.appoint(second, "other")
        with self.assertRaisesRegex(CoordinationError, "Vacate the role before deleting it"):
            workspace_roles.delete_role(self.store, role_id=second, workspace=str(self.root))
        self.assertTrue(workspace_roles.delete_role(self.store, role_id=role, workspace=str(self.root))["deleted"])

    def test_closing_a_holder_passes_agents_to_the_user_until_the_next_appointment(self):
        role = self.role(holder="lead")
        assignment = self.store.send_message(sender_session_id="lead", recipient_session_id="c1",
                                             body="Assignment", reply_required=True)
        self.store.inbox("c1")
        self.store.touch("c2", turn_active=True)
        self.store.threads.update("lead", attention="archived")
        self.assertEqual(self.process(), 1)
        self.assertIsNone(workspace_roles.list_roles(self.store, str(self.root))["data"][0]["holder_session_id"])
        self.assertTrue(self.by_user("c1"))
        self.assertIsNone(self.supervisor("c1"))
        self.assertEqual(self.supervisor("g1"), "c1")
        self.assertEqual(self.rows("SELECT thread_id, to_kind, role_id FROM supervision_pending_moves"), [("c2", "user", role)])
        # A report to the closed lead stays on record without reopening or waking it.
        reply = self.store.reply_message(sender_session_id="c1", message_id=assignment["id"], body="Result")
        self.assertEqual((reply["recipient_session_id"], reply["classification"]), ("lead", "informational"))
        self.assertEqual(self.store.threads.get("lead")["attention"], "archived")
        notify_agent_failure(self.store, "c1", event_key=["turn", "t1"], stage="turn", error="Crashed", turn_id="t1")
        self.assertEqual(self.rows("SELECT COUNT(*) FROM messages WHERE body LIKE '%app_agent_failed%'"), [(0,)])
        self.store.touch("c2", turn_active=False)
        self.process()
        self.assertTrue(self.by_user("c2"))
        result = self.appoint(role, "next")
        self.assertEqual(sorted(result["moved_thread_ids"]), ["c1", "c2"])
        self.assertEqual([self.supervisor(t) for t in ("c1", "c2")], ["next", "next"])
        self.assertFalse(self.by_user("c1"))
        record = supervision.history(self.store, "c1")
        self.assertEqual([(t["mode"], t["to_kind"]) for t in record["transfers"]], [("succession", "user"), ("appointment", "session")])

    def test_succession_without_a_role_goes_to_the_user_and_reopening_first_cancels_it(self):
        self.store.threads.update("lead", attention="archived")
        self.store.threads.update("lead", attention="now")
        self.assertEqual(self.process(), 0)
        self.assertEqual([self.supervisor(t) for t in ("c1", "c2")], ["lead", "lead"])
        self.store.threads.update("c1", attention="archived")
        self.process()
        self.assertTrue(self.by_user("g1"))
        self.assertEqual(self.rows("SELECT role_id FROM agent_supervisors WHERE thread_id = 'g1'"), [(None,)])
        # A closed agent is not passed on.
        self.store.threads.update("c2", attention="archived")
        self.store.threads.update("lead", attention="archived")
        self.process()
        self.assertEqual(self.rows("SELECT COUNT(*) FROM agent_supervisors WHERE thread_id = 'c2'"), [(0,)])

    def test_an_idle_unloaded_app_conversation_is_not_retired(self):
        with self.store._connection() as db:
            db.execute("CREATE TABLE IF NOT EXISTS browser_sessions (thread_id TEXT PRIMARY KEY)")
            db.execute("INSERT INTO browser_sessions VALUES ('lead')")
        self.store.end_session("lead")
        self.assertEqual(self.process(), 0)
        self.assertEqual(self.supervisor("c1"), "lead")
        with self.assertRaisesRegex(CoordinationError, "has not ended or been closed"):
            supervision.transfer(self.store, requester="next", from_session="lead", to_session="next",
                                 thread_ids=["c1"], request_id="adopt", reason="Replace", workspace=str(self.root))
        # A terminal session that ended is retired.
        self.store.end_session("c1")
        self.process()
        self.assertTrue(self.by_user("g1"))
        self.store.threads.update("lead", attention="archived")
        result = supervision.transfer(self.store, requester="next", from_session="lead", to_session="next",
                                      thread_ids=["c1"], request_id="adopt", reason="Replace", workspace=str(self.root))
        self.assertEqual(result["mode"], "adoption")

    def test_display_fields_follow_the_current_supervisor_and_name_the_held_role(self):
        role = self.role(holder="lead")
        with self.store._connection() as db:
            fields = supervision.display_fields("c1", {"thread_id": "lead", "mode": "orchestration"}, supervision.relations(db))
            holder = supervision.display_fields("lead", None, supervision.relations(db))
        self.assertEqual((fields["supervisor_thread_id"], fields["supervised_by_user"]), ("lead", False))
        self.assertEqual(holder["role"], {"role_id": role, "name": "Field lead"})
        self.store.threads.update("lead", attention="archived")
        self.process()
        with self.store._connection() as db:
            fields = supervision.display_fields("c1", {"thread_id": "lead", "mode": "orchestration"}, supervision.relations(db))
        self.assertEqual((fields["supervisor_thread_id"], fields["supervised_by_user"]), (None, True))

    def test_notices_never_wake_the_retired_supervisor(self):
        self.role(holder="lead")
        self.store.threads.update("lead", attention="archived")
        self.process()
        rows = self.rows("""SELECT sender_session_id, classification, delivered_at, body FROM messages
            WHERE recipient_session_id = 'lead' AND thread_id LIKE 'supervision-transfer:%'""")
        self.assertEqual([(r[0], r[1], r[2]) for r in rows], [("lead", "informational", None)])
        self.assertEqual(json.loads(rows[0][3])["to_session_id"], None)
        self.assertEqual(self.store.threads.get("lead")["attention"], "archived")


    def test_a_busy_workers_report_never_reopens_its_closed_lead_and_reopening_keeps_it(self):
        assignment = self.store.send_message(sender_session_id="lead", recipient_session_id="c2",
                                             body="Assignment", reply_required=True)
        self.store.inbox("c2")
        self.store.touch("c2", turn_active=True)
        self.store.threads.update("lead", attention="archived")
        self.process()
        reply = self.store.reply_message(sender_session_id="c2", message_id=assignment["id"], body="Result")
        self.assertEqual(reply["classification"], "informational")
        self.assertEqual(self.store.threads.get("lead")["attention"], "archived")
        notify_agent_failure(self.store, "c2", event_key=["turn", "t1"], stage="turn", error="Crashed", turn_id="t1")
        self.assertEqual(self.rows("SELECT COUNT(*) FROM messages WHERE body LIKE '%app_agent_failed%'"), [(0,)])
        # Reopening the lead before the worker becomes idle keeps the worker with it.
        self.store.threads.update("lead", attention="now")
        self.store.touch("c2", turn_active=False)
        self.process()
        self.assertEqual(self.supervisor("c2"), "lead")
        self.assertEqual(self.rows("SELECT COUNT(*) FROM supervision_pending_moves"), [(0,)])

    def test_closing_the_previous_holder_keeps_a_busy_agent_bound_for_the_new_holder(self):
        role = self.role(holder="lead")
        self.store.touch("c2", turn_active=True)
        self.appoint(role, "next")
        self.store.threads.update("lead", attention="archived")
        self.process()
        self.assertEqual(self.rows("SELECT to_kind, to_session_id, role_id FROM supervision_pending_moves"), [("session", "next", role)])
        self.store.touch("c2", turn_active=False)
        self.process()
        self.assertEqual(self.supervisor("c2"), "next")

    def test_the_user_assigns_a_supervisor_to_an_agent_without_a_role(self):
        self.store.threads.update("c1", attention="archived")
        self.process()
        self.assertTrue(self.by_user("g1"))
        with self.assertRaisesRegex(CoordinationError, "has no supervisor to replace"):
            workspace_roles.assign_supervisor(self.store, thread_id="lead", holder="c2", workspace=str(self.root))
        attach(self.store, "c2", "next")
        with self.assertRaisesRegex(CoordinationError, "cannot be supervised by one of its own agents"):
            workspace_roles.assign_supervisor(self.store, thread_id="c2", holder="next", workspace=str(self.root))
        self.store.register(session_id="third", client="codex", cwd=str(self.root))
        self.store.threads.ensure("third")
        result = workspace_roles.assign_supervisor(self.store, thread_id="g1", holder="third", workspace=str(self.root))
        self.assertEqual((result["moved_thread_ids"], self.supervisor("g1")), (["g1"], "third"))
        self.store.touch("c2", turn_active=True)
        busy = workspace_roles.assign_supervisor(self.store, thread_id="c2", holder="third", workspace=str(self.root))
        self.assertEqual(busy["deferred_thread_ids"], ["c2"])

    def test_closures_before_the_first_scan_or_this_feature_still_pass_agents_on(self):
        with self.store._connection() as db:
            db.execute("UPDATE work_threads SET attention = 'archived' WHERE thread_id = 'lead'")
        fresh = CoordinationStore(self.root / "coord.sqlite3")
        self.assertEqual(workspace_roles.process(fresh, str(self.root)), 1)
        with fresh._connection() as db:
            self.assertTrue(supervision.supervised_by_user(db, "c1"))
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        store = CoordinationStore(Path(other.name) / "coord.sqlite3")
        for session in ("boss", "worker"):
            store.register(session_id=session, client="codex", cwd=other.name)
            store.threads.ensure(session)
        attach(store, "boss", "worker")
        store.threads.update("boss", attention="archived")
        store._workspace_roles_swept = True  # Only the recorded closure can trigger succession.
        self.assertEqual(workspace_roles.process(store, other.name), 1)

    def test_retirement_discovery_does_not_reserve_the_writer(self):
        workspace_roles._prepare(self.store)
        retired = supervision.retired
        checked = []

        def discover(db, session_id):
            if not checked:
                completed = threading.Event()

                def write():
                    self.store.touch("other")
                    completed.set()

                worker = threading.Thread(target=write)
                worker.start()
                checked.append(completed.wait(1))
                worker.join(1)
            return retired(db, session_id)

        with patch.object(supervision, "retired", side_effect=discover):
            workspace_roles._sweep(self.store, str(self.root))
        self.assertEqual(checked, [True])

    def test_changing_a_holders_role_vacates_its_former_role_and_keeps_its_agents(self):
        first = self.role("Lead", holder="lead")
        second = self.role("Reviewer")
        self.appoint(second, "lead")
        listed = {r["name"]: r for r in workspace_roles.list_roles(self.store, str(self.root))["data"]}
        self.assertEqual((listed["Lead"]["holder_session_id"], listed["Reviewer"]["holder_session_id"]), (None, "lead"))
        self.assertEqual([self.supervisor(t) for t in ("c1", "c2")], ["lead", "lead"])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM supervision_transfers WHERE mode = 'appointment'"), [(0,)])
        # The agents now belong to the second role; vacating it leaves them waiting with the user only once.
        self.store.threads.update("lead", attention="archived")
        self.process()
        self.assertEqual(self.rows("SELECT DISTINCT role_id FROM agent_supervisors WHERE supervisor_kind = 'user'"), [(second,)])
        before = self.rows("SELECT COUNT(*) FROM supervision_transfers")
        self.appoint(first, "next")
        self.appoint(first, None)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM supervision_transfers"), before)

    def test_role_names_are_unique_across_a_workspace_and_its_folders(self):
        self.role("Lead")
        nested = self.root / "service"
        nested.mkdir()
        with self.assertRaisesRegex(CoordinationError, "already exists"):
            workspace_roles.create_role(self.store, workspace=str(nested), name="lead", requester_kind="user")


    def test_a_report_sent_before_the_first_scan_does_not_reopen_the_closed_lead(self):
        assignment = self.store.send_message(sender_session_id="lead", recipient_session_id="c2",
                                             body="Assignment", reply_required=True)
        self.store.inbox("c2")
        self.store.threads.update("lead", attention="archived")
        reply = self.store.reply_message(sender_session_id="c2", message_id=assignment["id"], body="Result")
        self.assertEqual(reply["classification"], "informational")
        self.assertEqual(self.store.threads.get("lead")["attention"], "archived")

    def test_the_users_choice_survives_the_previous_supervisor_closing(self):
        self.store.touch("c2", turn_active=True)
        result = workspace_roles.assign_supervisor(self.store, thread_id="c2", holder="other", workspace=str(self.root))
        self.assertEqual(result["deferred_thread_ids"], ["c2"])
        self.store.threads.update("lead", attention="archived")
        self.process()
        self.store.touch("c2", turn_active=False)
        self.process()
        self.assertEqual(self.supervisor("c2"), "other")

    def test_the_users_choice_replaces_a_move_waiting_for_a_role(self):
        role = self.role(holder="lead")
        self.store.touch("c2", turn_active=True)
        self.appoint(role, "next")
        result = workspace_roles.assign_supervisor(self.store, thread_id="c2", holder="other", workspace=str(self.root))
        self.assertEqual(result["deferred_thread_ids"], ["c2"])
        self.assertEqual(self.rows("SELECT to_session_id, role_id FROM supervision_pending_moves"), [("other", None)])
        self.store.touch("c2", turn_active=False)
        self.process()
        self.assertEqual(self.supervisor("c2"), "other")

    def test_a_holder_cannot_strip_another_session_of_its_role(self):
        mine = workspace_roles.create_role(self.store, workspace=str(self.root), name="Reviewer",
                                           requester_kind="session", requester_session="other")["role_id"]
        lead = workspace_roles.create_role(self.store, workspace=str(self.root), name="Lead",
                                           requester_kind="session", requester_session="lead")["role_id"]
        with self.assertRaisesRegex(CoordinationError, "already holds another role"):
            self.appoint(lead, "other", requester_kind="session", requester_session="lead")
        held = {r["name"]: r["holder_session_id"] for r in workspace_roles.list_roles(self.store, str(self.root))["data"]}
        self.assertEqual(held, {"Lead": "lead", "Reviewer": "other"})
        self.assertNotEqual(mine, lead)


if __name__ == "__main__":
    unittest.main()
