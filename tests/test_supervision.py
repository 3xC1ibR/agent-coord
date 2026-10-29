from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))

from agent_coord import supervision
from agent_coord.event_subscriptions import EventSubscriptions
from agent_coord.store import CoordinationError, CoordinationStore
from test_subagent_close import attach


class SupervisionTransferTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = CoordinationStore(self.root / "coord.sqlite3")
        for session in ("lead", "successor", "other", "c1", "c2", "c3", "g1"):
            self.store.register(session_id=session, client="codex", cwd=str(self.root))
        for child in ("c1", "c2", "c3"):
            attach(self.store, "lead", child)
        attach(self.store, "c1", "g1")
        self.requests = 0

    def transfer(self, threads, *, source="lead", target="successor", requester=None, request_id=None, **kwargs):
        self.requests += 1
        # The default is a handoff initiated by the current supervisor.
        return supervision.transfer(self.store, requester=requester or source, from_session=source,
            to_session=target, thread_ids=threads, request_id=request_id or f"transfer-{self.requests}",
            reason="The lead's context is exhausted.", workspace=kwargs.pop("workspace", str(self.root)), **kwargs)

    def db_value(self, query, *values):
        with self.store._connection() as db:
            return [tuple(row) for row in db.execute(query, values).fetchall()]

    def supervisor(self, thread):
        with self.store._connection() as db:
            return supervision.effective_supervisor(db, thread)

    def test_selected_idle_workers_move_together_and_keep_creator_provenance(self):
        result = self.transfer(["c1", "c2"])
        self.assertFalse(result["replayed"])
        self.assertEqual([agent["thread_id"] for agent in result["agents"]], ["c1", "c2"])
        self.assertEqual([self.supervisor(t) for t in ("c1", "c2", "c3", "g1")], ["successor", "successor", "lead", "c1"])
        with self.store._connection() as db:
            self.assertEqual(supervision.creator(db, "c1"), "lead")
            self.assertTrue(supervision.authorizes(db, "successor", "c1"))
            self.assertFalse(supervision.authorizes(db, "lead", "c1"))
            self.assertTrue(supervision.authorizes(db, "lead", "c3"))
            self.assertTrue(supervision.retired_supervisor(db, "lead", "c1"))
            self.assertFalse(supervision.retired_supervisor(db, "lead", "c3"))
        record = supervision.history(self.store, "c1")
        self.assertEqual((record["created_by"], record["supervisor"]), ("lead", "successor"))
        self.assertEqual([t["from_session_id"] for t in record["transfers"]], ["lead"])
        # The retired lead is retained; its own record and other workers are unchanged.
        self.assertIsNone(self.store.get_session("lead")["ended_at"])

    def test_live_supervisor_cannot_be_adopted_from_or_bypassed_by_a_bystander(self):
        with self.assertRaisesRegex(CoordinationError, "has not ended or been closed"):
            self.transfer(["c1"], requester="successor")
        # Idle or stale presence is not retirement.
        with self.store._connection() as db:
            db.execute("UPDATE sessions SET last_seen_at = 0, activity = 'idle' WHERE session_id = 'lead'")
        with self.assertRaisesRegex(CoordinationError, "has not ended or been closed"):
            self.transfer(["c1"], requester="successor")
        with self.assertRaisesRegex(CoordinationError, "Only the current supervisor"):
            self.transfer(["c1"], requester="other")
        self.assertEqual(self.supervisor("c1"), "lead")
        self.assertEqual(self.db_value("SELECT COUNT(*) FROM supervision_transfers"), [(0,)])

    def test_current_supervisor_hands_off_while_live_and_mid_turn(self):
        self.store.touch("lead", turn_active=True)
        result = self.transfer(["c1"])
        self.assertEqual((result["mode"], self.supervisor("c1")), ("handoff", "successor"))
        self.assertEqual(supervision.history(self.store, "c1")["transfers"][0]["requester_session_id"], "lead")

    def test_successor_adopts_from_an_ended_or_closed_supervisor_with_a_non_waking_notice(self):
        self.store.end_session("lead")
        result = self.transfer(["c1"], requester="successor")
        self.assertEqual(result["mode"], "adoption")
        self.store.threads.ensure("successor")
        self.store.threads.update("successor", attention="archived")
        self.store.register(session_id="heir", client="codex", cwd=str(self.root))
        self.transfer(["c1"], source="successor", target="heir", requester="heir")
        self.assertEqual(self.supervisor("c1"), "heir")
        # The closed supervisor's notice is informational: undelivered, unclaimed, and it stays closed.
        notices = self.db_value("""SELECT classification, reply_required, delivered_at FROM messages
            WHERE recipient_session_id = 'successor' AND thread_id LIKE 'supervision-transfer:%'""")
        self.assertEqual(notices, [("informational", 0, None)])
        self.assertEqual(self.db_value("SELECT attention FROM work_threads WHERE thread_id = 'successor'"), [("archived",)])
        self.assertEqual(self.db_value("SELECT COUNT(*) FROM message_wake_attempts"), [(0,)])
        self.store.end_session("heir")
        with self.store._connection() as db:
            db.execute("UPDATE sessions SET turn_active = 1 WHERE session_id = 'heir'")
        with self.assertRaisesRegex(CoordinationError, "still has an active turn"):
            self.transfer(["c1"], source="heir", target="other", requester="other")

    def test_sidecar_cannot_adopt_its_watched_agent_and_bypass_watch_limits(self):
        from agent_coord.sidecars import Sidecars
        Sidecars(self.store)
        with self.store._connection() as db:
            columns = [row[1] for row in db.execute("PRAGMA table_info(sidecar_watches)")]
            values = {"id": "watch", "sidecar_id": "successor", "target_id": "c1", "state": "active"}
            for column in columns:
                values.setdefault(column, 0)
            db.execute("INSERT INTO sidecar_watches (%s) VALUES (%s)" % (",".join(values), ",".join("?" * len(values))),
                       list(values.values()))
        self.store.end_session("lead")
        with self.assertRaisesRegex(CoordinationError, "sidecar report"):
            self.transfer(["c1"], requester="successor")
        self.assertEqual(self.supervisor("c1"), "lead")
        self.assertEqual(self.db_value("SELECT COUNT(*) FROM messages WHERE thread_id LIKE 'supervision-transfer:%'"), [(0,)])

    def test_notices_list_moved_agents_and_rerouted_replies_for_every_other_party(self):
        assignment = self.store.send_message(sender_session_id="lead", recipient_session_id="c1",
                                             body="Assignment", reply_required=True)
        self.store.inbox("c1")
        self.store.end_session("lead")
        result = self.transfer(["c1", "c2"], requester="successor")
        rows = self.db_value("""SELECT recipient_session_id, sender_session_id, classification, body FROM messages
            WHERE id IN (%s) ORDER BY id""" % ",".join("?" * len(result["notice_message_ids"])),
            *result["notice_message_ids"])
        self.assertEqual([row[0] for row in rows], ["lead", "c1", "c2"])
        self.assertTrue(all(row[1] == "successor" and row[2] == "informational" for row in rows))
        body = json.loads(rows[0][3])
        self.assertEqual((body["event"], body["mode"], body["thread_ids"]), ("supervision_transferred", "adoption", ["c1", "c2"]))
        self.assertEqual(body["rerouted_reply_message_ids"], {"c1": [assignment["id"]]})
        self.assertEqual(self.transfer(["c1", "c2"], requester="successor", request_id="transfer-1")["notice_message_ids"],
                         result["notice_message_ids"])
        self.assertEqual(self.db_value("SELECT COUNT(*) FROM messages WHERE thread_id LIKE 'supervision-transfer:%'"), [(3,)])

    def test_one_unsafe_worker_refuses_the_whole_selection(self):
        self.store.touch("c2", turn_active=True)
        with self.assertRaisesRegex(CoordinationError, "c2: a turn is active"):
            self.transfer(["c1", "c2"])
        self.assertEqual(self.supervisor("c1"), "lead")
        self.assertEqual(self.db_value("SELECT COUNT(*) FROM agent_supervisors")[0][0], 0)
        self.assertEqual(self.db_value("SELECT turn_active FROM sessions WHERE session_id = 'c2'")[0][0], 1)

    def test_failure_after_writes_rolls_back_every_record(self):
        EventSubscriptions(self.store, str(self.root))
        with self.store._connection() as db:
            db.execute("""INSERT INTO event_subscriptions (id, owner_session_id, cwd, mode, continuation, state,
                request_json, created_at, updated_at) VALUES ('watch-c1', 'lead', ?, 'all', '', 'pending', '{}', 0, 0)""",
                (str(self.root),))
            db.execute("INSERT INTO event_subscription_items (subscription_id, id, kind, session_id) VALUES ('watch-c1', '1', 'session', 'c1')")
        with patch.object(supervision, "_handover", side_effect=RuntimeError("interrupted")):
            with self.assertRaises(RuntimeError):
                self.transfer(["c1", "c2"])
        self.assertEqual(self.db_value("SELECT COUNT(*) FROM agent_supervisors"), [(0,)])
        self.assertEqual(self.db_value("SELECT COUNT(*) FROM supervision_transfers"), [(0,)])
        self.assertEqual(self.db_value("SELECT owner_session_id FROM event_subscriptions"), [("lead",)])

    def test_transfer_never_resumes_paused_work_or_delivers_queued_requests(self):
        with self.store._connection() as db:
            db.execute("""CREATE TABLE browser_inbox_wake (thread_id TEXT PRIMARY KEY, paused INTEGER NOT NULL DEFAULT 0,
                dispatching INTEGER NOT NULL DEFAULT 0, error TEXT, pause_kind TEXT, recovery_turn_id TEXT)""")
            db.execute("INSERT INTO browser_inbox_wake (thread_id, paused, pause_kind) VALUES ('c1', 1, 'user_stop')")
            db.execute("INSERT INTO browser_inbox_wake (thread_id, paused, pause_kind) VALUES ('c2', 1, 'unknown')")
        queued = self.store.send_message(sender_session_id="other", recipient_session_id="c1", body="Queued work")
        messages = self.db_value("SELECT COUNT(*) FROM messages")[0][0]
        result = self.transfer(["c1", "c2"])
        self.assertEqual([(a["paused"], a["pause_kind"]) for a in result["agents"]],
                         [(True, "user_stop"), (True, "unknown")])
        self.assertEqual(self.db_value("SELECT paused, pause_kind FROM browser_inbox_wake ORDER BY thread_id"),
                         [(1, "user_stop"), (1, "unknown")])
        # Only informational notices were added; none is delivered or claimed for a wake.
        self.assertEqual(self.db_value("SELECT COUNT(*) FROM messages")[0][0], messages + len(result["notice_message_ids"]))
        self.assertEqual(self.db_value("""SELECT DISTINCT classification, delivered_at FROM messages
            WHERE thread_id LIKE 'supervision-transfer:%'"""), [("informational", None)])
        self.assertEqual(self.db_value("SELECT COUNT(*) FROM message_wake_attempts"), [(0,)])
        self.assertIsNone(self.db_value("SELECT delivered_at FROM messages WHERE id = ?", queued["id"])[0][0])
        self.assertEqual(self.db_value("SELECT turn_active FROM sessions WHERE session_id IN ('c1', 'c2')"), [(0,), (0,)])

    def test_undelivered_request_from_retired_lead_or_dispatching_wake_blocks_transfer(self):
        self.store.send_message(sender_session_id="lead", recipient_session_id="c1", body="Stale assignment")
        with self.assertRaisesRegex(CoordinationError, "undelivered request from the retired supervisor"):
            self.transfer(["c1"])
        with self.store._connection() as db:
            db.execute("CREATE TABLE browser_inbox_wake (thread_id TEXT PRIMARY KEY, paused INTEGER, dispatching INTEGER, pause_kind TEXT)")
            db.execute("INSERT INTO browser_inbox_wake VALUES ('c2', 0, 1, NULL)")
        with self.assertRaisesRegex(CoordinationError, "inbox wake is dispatching"):
            self.transfer(["c2"])
        self.assertEqual(self.supervisor("c1"), "lead")
        self.assertEqual(self.supervisor("c2"), "lead")

    def test_stale_and_concurrent_transfers_are_rejected_and_request_ids_replay(self):
        first = self.transfer(["c1"], request_id="adopt-c1")
        with self.assertRaisesRegex(CoordinationError, "supervised by successor, not lead"):
            self.transfer(["c1"], target="other")
        replay = self.transfer(["c1"], request_id="adopt-c1")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["transfer_id"], first["transfer_id"])
        with self.assertRaisesRegex(CoordinationError, "already used for a different transfer"):
            self.transfer(["c2"], request_id="adopt-c1")
        # A later succession moves from the current supervisor; earlier leads stay retired.
        self.transfer(["c1"], source="successor", target="other")
        self.assertEqual(self.supervisor("c1"), "other")
        with self.store._connection() as db:
            self.assertTrue(supervision.retired_supervisor(db, "lead", "c1"))
            self.assertTrue(supervision.retired_supervisor(db, "successor", "c1"))
        self.assertEqual(len(supervision.history(self.store, "c1")["transfers"]), 2)

    def test_authority_selection_cycle_and_workspace_checks(self):
        with self.assertRaisesRegex(CoordinationError, "Only the current supervisor"):
            self.transfer(["c1"], requester="other")
        with self.assertRaisesRegex(CoordinationError, "cannot be one of the transferred"):
            self.transfer(["c1", "successor"])
        with self.assertRaisesRegex(CoordinationError, "distinct"):
            self.transfer(["c1", "c1"])
        with self.assertRaisesRegex(CoordinationError, "supervised by c1, not lead"):
            self.transfer(["g1"])
        # g1 descends from c1, so g1 adopting c1 would make c1 its own ancestor.
        with self.assertRaisesRegex(CoordinationError, "cycle"):
            self.transfer(["c1"], target="g1")
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        self.store.register(session_id="outside", client="codex", cwd=outside.name)
        with self.assertRaisesRegex(CoordinationError, "Successor 'outside' was not found in this workspace"):
            self.transfer(["c1"], target="outside")
        self.assertEqual(self.db_value("SELECT COUNT(*) FROM supervision_transfers")[0][0], 0)

    def test_reports_to_the_retired_lead_route_to_the_successor(self):
        assignment = self.store.send_message(sender_session_id="lead", recipient_session_id="c1",
                                             body="Assignment", reply_required=True)
        with self.store._connection() as db:
            db.execute("UPDATE messages SET delivered_at = 1 WHERE id = ?", (assignment["id"],))
        result = self.transfer(["c1"])
        self.assertEqual(result["agents"][0]["outstanding_reply_message_ids"], [assignment["id"]])
        with self.store._connection() as db:
            self.assertEqual(supervision.report_recipient(db, "c1", "lead"), "successor")
            self.assertEqual(supervision.report_recipient(db, "c3", "lead"), "lead")
            self.assertEqual(supervision.report_recipient(db, "c1", "other"), "other")

    def test_store_routes_worker_replies_and_refuses_retired_lead_directives(self):
        assignment = self.store.send_message(sender_session_id="lead", recipient_session_id="c1",
                                             body="Assignment", reply_required=True)
        self.store.inbox("c1")
        self.transfer(["c1"])
        reply = self.store.reply_message(sender_session_id="c1", message_id=assignment["id"], body="Result")
        self.assertEqual(reply["recipient_session_id"], "successor")
        self.assertEqual(reply["in_reply_to"], assignment["id"])
        self.assertEqual([m["body"] for m in self.store.inbox("successor", mark_delivered=False)
                          if m["classification"] != "informational"], ["Result"])
        self.assertEqual(self.store.inbox("lead", mark_delivered=False), [])
        with self.assertRaisesRegex(CoordinationError, "supervised by successor"):
            self.store.send_message(sender_session_id="lead", recipient_session_id="c1", body="Stale directive")
        self.store.send_message(sender_session_id="lead", recipient_session_id="c1", body="Context note",
                                classification="informational", reply_required=False)
        self.store.send_message(sender_session_id="lead", recipient_session_id="c3", body="Still mine")
        self.store.send_message(sender_session_id="successor", recipient_session_id="c1", body="Next step")

    def test_pending_subscriptions_move_only_when_fully_selected(self):
        EventSubscriptions(self.store, str(self.root))
        with self.store._connection() as db:
            for identifier, targets in (("watch-c1", ["c1"]), ("watch-mixed", ["c2", "c3"])):
                db.execute("""INSERT INTO event_subscriptions (id, owner_session_id, cwd, mode, continuation, state,
                    request_json, created_at, updated_at) VALUES (?, 'lead', ?, 'all', '', 'pending', '{}', 0, 0)""",
                    (identifier, str(self.root)))
                for index, target in enumerate(targets):
                    db.execute("""INSERT INTO event_subscription_items (subscription_id, id, kind, session_id)
                        VALUES (?, ?, 'session', ?)""", (identifier, str(index), target))
        with self.assertRaisesRegex(CoordinationError, "watch-mixed also watches agents outside"):
            self.transfer(["c1", "c2"])
        self.assertEqual(self.db_value("SELECT owner_session_id FROM event_subscriptions ORDER BY id"), [("lead",), ("lead",)])
        result = self.transfer(["c1"])
        self.assertEqual(result["moved_subscription_ids"], ["watch-c1"])
        self.assertEqual(self.db_value("SELECT id, owner_session_id FROM event_subscriptions ORDER BY id"),
                         [("watch-c1", "successor"), ("watch-mixed", "lead")])

    def test_readers_tolerate_a_database_without_supervision_tables(self):
        with self.store._connection() as db:
            self.assertEqual(supervision.overrides(db), {})
            self.assertEqual(supervision.effective_supervisor(db, "c1"), "lead")
            self.assertFalse(supervision.retired_supervisor(db, "lead", "c1"))
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name = 'agent_supervisors'").fetchone())


if __name__ == "__main__":
    unittest.main()
