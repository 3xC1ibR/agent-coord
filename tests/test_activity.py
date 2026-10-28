"""Activity evidence remains local, scoped, reproducible, and human-attributed."""
import json
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))

from agent_coord.activity import ActivityStore, METRICS
from agent_coord.app_control import AppControl
from agent_coord.store import CoordinationError, CoordinationStore


class ActivityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.base = 1_700_000_000.0
        self.now = self.base
        self.store = CoordinationStore(self.root / "state.sqlite3", clock=lambda: self.now)
        self.activity = ActivityStore(self.store)
        self.register("one")
        self.now += 1000

    def register(self, thread_id, *, cwd=None, project_cwd=None):
        self.store.register(session_id=thread_id, client="codex", cwd=str(cwd or self.root),
                            project_cwd=project_cwd, name="Thread " + thread_id)

    def event(self, kind, second, thread_id="one", **kwargs):
        return self.activity.record(kind, thread_id, occurred_at=self.base + second, **kwargs)

    def query(self, **kwargs):
        return self.activity.query(**{"start": self.base, "end": self.now, **kwargs})

    def test_additive_initialization_settings_and_no_backfill(self):
        self.store.threads.capture_request("one", "private original prompt")
        self.assertEqual(self.activity.settings(), {"enabled": True, "recording_started_at": self.base})
        self.assertEqual(ActivityStore(self.store).settings(), self.activity.settings())
        result = self.query()
        self.assertEqual(result["events"], [])
        self.assertEqual(result["totals"], dict.fromkeys(METRICS, 0))
        self.assertTrue(result["coverage"]["complete"])
        self.assertIsNone(result["coverage"]["first_event_at"])
        self.assertIn("No historical silent-view backfill", result["coverage"]["note"])
        self.assertFalse(self.activity.query()["coverage"]["complete"])
        self.assertEqual(self.store.threads.get("one")["original_request"], "private original prompt")

    def test_defaults_receipt_and_separate_receive_time(self):
        receipt = self.activity.record("message_submitted", "one")
        self.assertEqual(receipt["occurred_at"], self.now)
        self.assertEqual(receipt["received_at"], self.now)
        self.assertEqual(receipt["status"], "recorded")
        receipt = self.event("message_submitted", 20)
        self.assertEqual(receipt["occurred_at"], self.base + 20)
        self.assertEqual(receipt["received_at"], self.now)
        self.now += 1
        result = self.activity.query()
        self.assertEqual(result["start"], self.now - 86400)
        self.assertEqual(result["end"], self.now)
        self.assertEqual(result["as_of"], self.now)
        self.assertEqual(result["totals"]["messages"], 2)

    def test_idempotency_omitted_time_survives_reassignment_and_pause(self):
        first = self.activity.record("message_submitted", "one", event_id="stable", details={"count": 1})
        self.now += 100
        self.store.threads.update("one", title="Changed", domain="Backend")
        self.activity.configure(False)
        again = self.activity.record("message_submitted", "one", event_id="stable", details={"count": 1})
        self.assertEqual(again, {**first, "status": "duplicate"})
        self.assertEqual(self.query()["events"][0]["snapshot"]["title"], "Thread one")
        for change in ({"kind": "decision_submitted"}, {"actor": "agent"}, {"source": "cli"},
                       {"occurred_at": self.now}, {"window_id": "other"}, {"details": {"count": 2}}):
            with self.subTest(change=change), self.assertRaises(CoordinationError):
                self.activity.record(**{"kind": "message_submitted", "thread_id": "one",
                                        "event_id": "stable", "details": {"count": 1}, **change})

    def test_concurrent_retries_have_one_winner(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            receipts = list(pool.map(lambda _: self.event("message_submitted", 10, event_id="retry"), range(12)))
        self.assertEqual(sum(r["status"] == "recorded" for r in receipts), 1)
        self.assertEqual(len({r["sequence"] for r in receipts}), 1)
        self.assertEqual(self.query()["totals"]["messages"], 1)

    def test_invalid_records_are_atomic(self):
        invalid = [{"kind": []}, {"kind": "unknown"}, {"actor": "robot"}, {"actor": {}},
                   {"thread_id": "missing"}, {"thread_id": ""}, {"event_id": ""},
                   {"source": "s" * 65}, {"visit_id": "\0"}, {"turn_id": 3},
                   {"window_id": "w" * 201}, {"occurred_at": float("nan")},
                   {"occurred_at": float("inf")}, {"occurred_at": True}, {"occurred_at": "123"},
                   {"occurred_at": 10 ** 1000}, {"details": []}, {"details": {"bad": float("inf")}},
                   {"details": {"body": "secret"}}, {"details": {"nested": {"prompt_text": "secret"}}},
                   {"details": {"x": "a" * 1025}}, {"details": {str(i): "a" * 1024 for i in range(9)}},
                   {"details": {"x": {"x": {"x": {"x": {"x": 1}}}}}}]
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(CoordinationError):
                self.activity.record(**{"kind": "message_submitted", "thread_id": "one", **values})
        self.assertEqual(self.query()["events"], [])

    def test_prompt_bodies_are_not_copied(self):
        self.store.threads.capture_request("one", "secret request body")
        self.event("message_submitted", 10, details={"queued": True, "attachment_count": 2})
        data = json.dumps(self.query())
        self.assertNotIn("secret request body", data)
        self.assertIn("attachment_count", data)

    def test_range_is_half_open_and_future_end_capped(self):
        for second in (0, 1, 2, 3, 1000, 1001):
            self.event("message_submitted", second)
        result = self.query(start=self.base + 1, end=self.base + 3)
        self.assertEqual([e["occurred_at"] - self.base for e in result["events"]], [1, 2])
        result = self.query(end=self.now + 1000)
        self.assertEqual(result["end"], self.now)
        self.assertEqual(result["totals"]["messages"], 4)

    def test_epoch_times_represent_timezone_and_dst_unambiguously(self):
        first = datetime.fromisoformat("2023-11-05T01:30:00-05:00").timestamp()
        second = datetime.fromisoformat("2023-11-05T01:30:00-06:00").timestamp()
        self.assertEqual(second - first, 3600)
        self.activity.record("message_submitted", "one", occurred_at=first)
        self.activity.record("message_submitted", "one", occurred_at=second)
        result = self.activity.query(start=first, end=second)
        self.assertEqual(result["totals"]["messages"], 1)
        self.assertEqual(result["events"][0]["occurred_at"], first)

    def test_visits_pair_by_thread_window_and_visit(self):
        self.register("two")
        for second, window in ((10, "a"), (11, "b")):
            self.event("visit_started", second, window_id=window, visit_id="same")
        self.event("message_submitted", 12, window_id="a", visit_id="same")
        self.event("message_submitted", 13, actor="agent", window_id="b", visit_id="same")
        self.event("visit_ended", 14, window_id="a", visit_id="same")
        self.event("visit_ended", 15, window_id="b", visit_id="same")
        self.event("visit_started", 16, "two", window_id="a", visit_id="same")
        self.event("visit_started", 17)  # Missing visit identity is explicitly incomplete.
        self.event("decision_submitted", 18)
        result = self.query()
        self.assertEqual(result["totals"], dict(visits=4, return_visits=2, visits_without_message=1,
                                               messages=1, decisions=1, threads=2, incomplete_visits=2))

    def test_incomplete_visit_does_not_become_silent_before_end(self):
        self.event("visit_started", 10, visit_id="v")
        self.event("message_submitted", 11, actor="system", visit_id="v")
        self.event("visit_ended", 20, actor="system", visit_id="v")
        self.event("visit_ended", 30, visit_id="v")
        for end, incomplete, silent in ((20, 1, 0), (30, 1, 0), (31, 0, 1)):
            totals = self.query(end=self.base + end)["totals"]
            self.assertEqual(totals["incomplete_visits"], incomplete)
            self.assertEqual(totals["visits_without_message"], silent)
        # A completion in range does not invent a start from before the range.
        self.assertEqual(self.query(start=self.base + 11)["totals"]["visits"], 0)

    def test_revisit_is_subsequent_start_in_selected_range(self):
        for second in (1, 20, 30):
            self.event("visit_started", second, visit_id=str(second))
        self.assertEqual(self.query()["totals"]["return_visits"], 2)
        self.assertEqual(self.query(start=self.base + 20)["totals"]["return_visits"], 1)

    def test_out_of_order_delivery_and_tied_times(self):
        self.event("visit_ended", 20, visit_id="v")
        self.event("message_submitted", 10, visit_id="v")
        self.event("visit_started", 10, visit_id="v")
        result = self.query()
        self.assertEqual(result["totals"]["visits_without_message"], 0)
        self.assertEqual(result["totals"]["incomplete_visits"], 0)
        self.assertEqual([e["kind"] for e in result["events"]], ["message_submitted", "visit_started", "visit_ended"])

    def test_explicit_visit_association_handles_browser_server_clock_skew(self):
        for visit, message_time in (("before", 9), ("after", 21)):
            self.event("visit_started", 10, visit_id=visit, window_id="window")
            self.event("visit_ended", 20, visit_id=visit, window_id="window")
            self.event("message_submitted", message_time, visit_id=visit, window_id="window")
        result = self.query(start=self.base + 10)
        self.assertEqual(result["totals"]["visits_without_message"], 0)
        self.assertEqual(result["totals"]["messages"], 1)  # Earlier association is not a range message.
        cutoff = self.query(start=self.base + 10, end=self.base + 21)
        self.assertEqual(cutoff["totals"]["visits_without_message"], 1)
        self.assertEqual(cutoff["totals"]["messages"], 0)

    def test_current_organization_with_immutable_snapshots(self):
        group = self.store.threads.organization.create_project("Old group", str(self.root))
        self.store.threads.update("one", domain="web", project_id=group["id"])
        self.event("visit_started", 10, visit_id="v")
        new = self.root / "new"
        new.mkdir()
        new_group = self.store.threads.organization.create_project("New group", str(new))
        with self.store._connection() as db:
            db.execute("UPDATE work_threads SET project_cwd=? WHERE thread_id='one'", (str(new),))
        self.store.threads.update("one", title="New title", domain="backend", project_id=new_group["id"])
        self.event("visit_ended", 20, visit_id="v")
        result = self.query(project=str(new), domain="Backend", group=new_group["id"])
        self.assertEqual(result["totals"]["visits_without_message"], 1)
        row = result["groups"][0]
        self.assertEqual((row["title"], row["project"], row["domain"], row["group_name"]),
                         ("New title", str(new), "backend", "New group"))
        snapshot = result["events"][0]["snapshot"]
        self.assertEqual((snapshot["title"], snapshot["project_cwd"], snapshot["domain"], snapshot["group_id"]),
                         ("Thread one", str(self.root), "web", group["id"]))
        self.assertEqual(self.query(project=str(self.root))["totals"]["visits"], 0)
        self.assertEqual(self.query(domain="web")["events"], [])
        self.assertEqual(self.query(group=group["id"])["events"], [])

    def test_group_rows_reconcile_and_empty_domain_group_filters(self):
        self.register("two")
        self.register("three", cwd=self.root / "child")
        self.store.threads.update("two", domain="backend")
        self.event("message_submitted", 1)
        self.event("decision_submitted", 2, "two")
        self.event("visit_started", 3, "three", visit_id="v")
        for group_by in ("thread", "project", "domain", "group"):
            result = self.query(group_by=group_by, limit=1)
            self.assertEqual(len(result["events"]), 1)
            for metric in METRICS:
                self.assertEqual(sum(g[metric] for g in result["groups"]), result["totals"][metric])
            self.assertEqual(result["totals"]["threads"], 3)
        self.assertEqual(self.query(domain="")["totals"]["threads"], 2)
        self.assertEqual(self.query(group="")["totals"]["threads"], 3)
        self.assertEqual(self.query(thread_id="two")["totals"]["decisions"], 1)

    def test_workspace_boundary_descendants_siblings_and_move(self):
        child = self.root / "child"
        outsider = self.root.with_name(self.root.name + "-sibling")
        self.register("child", cwd=child)
        self.register("outside", cwd=outsider)
        for thread_id in ("one", "child", "outside"):
            self.event("message_submitted", 10, thread_id)
        result = self.query(allowed_cwd=str(self.root))
        self.assertEqual({e["thread_id"] for e in result["events"]}, {"one", "child"})
        self.assertEqual(self.query(allowed_cwd=str(self.root), thread_id="outside")["events"], [])
        self.store.register(session_id="outside", client="codex", cwd=str(child))
        with self.store._connection() as db:
            db.execute("UPDATE work_threads SET project_cwd=? WHERE thread_id='outside'", (str(child),))
        self.event("message_submitted", 11, "outside")
        result = self.query(allowed_cwd=str(self.root))
        self.assertEqual(len([e for e in result["events"] if e["thread_id"] == "outside"]), 1)
        self.assertNotIn(str(outsider), json.dumps(result))

    def test_workspace_boundary_allows_linked_worktree(self):
        repository = self.root / "repo"
        marker = repository / ".git/worktrees/other"
        marker.mkdir(parents=True)
        other = self.root / "other"
        other.mkdir()
        (other / ".git").write_text("gitdir: " + str(marker))
        (marker / "commondir").write_text("../..")
        self.register("worktree", cwd=other)
        self.event("message_submitted", 10, "worktree")
        self.assertEqual(self.query(allowed_cwd=str(repository))["totals"]["messages"], 1)

    def test_cursor_freezes_cutoff_and_high_water_with_full_summary(self):
        for second in (20, 10, 10, 30, 50):
            self.event("message_submitted", second)
        first = self.activity.query(limit=2)
        self.assertEqual(first["totals"]["messages"], 5)
        self.event("message_submitted", 15)  # Late evidence with an older timestamp.
        self.now += 500
        self.event("message_submitted", 1500)
        pages = first["events"][:]
        cursor = first["next_cursor"]
        while cursor:
            page = self.activity.query(limit=2, cursor=cursor)
            self.assertEqual(page["totals"], first["totals"])
            self.assertEqual(page["groups"], first["groups"])
            self.assertEqual(page["as_of"], first["as_of"])
            self.assertEqual(page["coverage"], first["coverage"])
            pages.extend(page["events"])
            cursor = page["next_cursor"]
        self.assertEqual([e["occurred_at"] - self.base for e in pages], [10, 10, 20, 30, 50])
        self.assertEqual(len({e["event_id"] for e in pages}), 5)

    def test_cursor_filter_binding_tampering_and_clear(self):
        for second in (1, 2, 3):
            self.event("message_submitted", second)
        first = self.query(limit=1)
        for change in ({"project": str(self.root)}, {"domain": ""}, {"group": ""},
                       {"thread_id": "one"}, {"allowed_cwd": str(self.root)}, {"group_by": "domain"},
                       {"start": self.base + 1}, {"end": self.now - 1}):
            with self.subTest(change=change), self.assertRaises(CoordinationError):
                self.query(cursor=first["next_cursor"], **change)
        with self.assertRaises(CoordinationError):
            self.query(cursor=first["next_cursor"] + "x")
        self.activity.clear(before=self.base)
        with self.assertRaisesRegex(CoordinationError, "cleared"):
            self.query(cursor=first["next_cursor"])

    def test_cursor_rejects_changed_organization(self):
        self.event("message_submitted", 1)
        self.event("message_submitted", 2)
        first = self.query(limit=1)
        self.store.threads.update("one", domain="web")
        with self.assertRaisesRegex(CoordinationError, "organization changed"):
            self.query(cursor=first["next_cursor"])

    def test_cursor_tolerates_unrelated_new_threads_and_rejects_pause(self):
        self.event("message_submitted", 1)
        self.event("message_submitted", 2)
        first = self.query(limit=1)
        self.register("new-agent")
        self.event("message_submitted", 1, "new-agent", actor="agent")
        page = self.query(cursor=first["next_cursor"])
        self.assertEqual(page["totals"], first["totals"])
        self.activity.configure(False)
        with self.assertRaisesRegex(CoordinationError, "settings changed"):
            self.query(cursor=first["next_cursor"])

    def test_invalid_queries(self):
        for values in ({"start": float("nan")}, {"end": float("inf")}, {"start": "yesterday"},
                       {"end": True}, {"start": self.now}, {"start": self.now + 1},
                       {"limit": 0}, {"limit": 501}, {"limit": True}, {"limit": 1.0},
                       {"project": "relative"}, {"domain": []}, {"domain": "d" * 65},
                       {"thread_id": ""}, {"group": []}, {"group_by": "week"}, {"group_by": []},
                       {"cursor": "bad"}, {"allowed_cwd": ""}):
            with self.subTest(values=values), self.assertRaises(CoordinationError):
                self.query(**values)

    def test_pause_resume_and_clear_coverage(self):
        self.event("message_submitted", 10)
        self.activity.configure(False)
        self.activity.configure(False)  # Idempotent configuration.
        self.assertEqual(self.event("message_submitted", 20)["status"], "skipped")
        self.now += 100
        self.activity.configure(True)
        self.event("message_submitted", 1100)
        self.now += 1
        result = self.query()
        self.assertEqual(result["totals"]["messages"], 2)
        self.assertFalse(result["coverage"]["complete"])
        self.assertEqual(result["coverage"]["paused_intervals"], [{"start": self.base + 1000, "end": self.base + 1100}])
        self.assertEqual(self.activity.clear(before=self.base + 10)["deleted"], 0)
        self.assertEqual(self.activity.clear(before=self.base + 11)["deleted"], 1)
        self.assertEqual(self.query()["coverage"]["first_event_at"], self.base + 1100)
        self.assertEqual(self.activity.clear()["deleted"], 1)
        result = self.query()
        self.assertFalse(result["coverage"]["complete"])
        self.assertIsNone(result["coverage"]["first_event_at"])
        self.assertEqual(result["coverage"]["history_cleared_at"], self.now)
        self.assertEqual(self.activity.settings()["recording_started_at"], self.base)
        for bad in (0, 1, None, "true"):
            with self.assertRaises(CoordinationError):
                self.activity.configure(bad)
        with self.assertRaises(CoordinationError):
            self.activity.clear(before=float("nan"))

    def test_buffered_visits_cannot_restore_cleared_history(self):
        for kind in ("visit_started", "visit_ended"):
            self.event(kind, 10, event_id=kind, source="navigation", visit_id="old", window_id="window")
        self.assertEqual(self.activity.clear()["deleted"], 2)
        # Covers retry of an accepted event and first upload from an offline window.
        for event_id, kind in (("visit_started", "visit_started"), ("visit_ended", "visit_ended"),
                               ("offline-window", "visit_started")):
            receipt = self.event(kind, 10, event_id=event_id, source="navigation", visit_id="old", window_id="window")
            self.assertEqual(receipt, {"status": "skipped", "reason": "history_cleared", "event_id": event_id})
        reopened = ActivityStore(CoordinationStore(self.store.database_path, clock=lambda: self.now))
        self.assertEqual(reopened.record("visit_started", "one", event_id="visit_started",
                                         occurred_at=self.base + 10, source="navigation",
                                         visit_id="old", window_id="window")["reason"], "history_cleared")
        self.assertEqual(self.query()["events"], [])
        self.assertEqual(self.activity.record("visit_started", "one", event_id="fresh", source="focus")["status"], "recorded")

    def test_full_clear_drops_exact_retries_even_with_equal_or_future_client_time(self):
        for second in (1000, 1020):
            self.event("visit_started", second, event_id="visit-" + str(second), source="focus")
        self.assertEqual(self.activity.clear()["deleted"], 2)
        self.now += 100
        for second in (1000, 1020):
            receipt = self.event("visit_started", second, event_id="visit-" + str(second), source="focus")
            self.assertEqual(receipt["reason"], "history_cleared")
        # Only one-way keyed IDs and reasons survive, not the cleared evidence.
        with self.store._connection() as db:
            retained = [dict(row) for row in db.execute("SELECT * FROM activity_discarded_visits")]
            self.assertEqual(db.execute("SELECT COUNT(*) FROM activity_events").fetchone()[0], 0)
        self.assertEqual(len(retained), 2)
        self.assertNotIn("visit-1000", json.dumps(retained))
        self.assertNotIn("thread_id", json.dumps(retained))
        self.assertNotIn("occurred_at", json.dumps(retained))
        self.assertEqual(self.activity.record("visit_started", "one", event_id="new-visit")["status"], "recorded")

    def test_partial_clear_keeps_exclusive_boundary_and_monotonic_cutoff(self):
        self.event("visit_started", 10, event_id="deleted")
        survivor = self.event("visit_started", 20, event_id="retained")
        self.assertEqual(self.activity.clear(before=self.base + 20)["deleted"], 1)
        self.assertEqual(self.event("visit_started", 20, event_id="retained"), {**survivor, "status": "duplicate"})
        self.activity.clear(before=self.base + 5)
        self.assertEqual(self.event("visit_ended", 19, event_id="late")["reason"], "history_cleared")
        self.assertEqual(self.event("visit_ended", 20, event_id="boundary")["status"], "recorded")
        self.assertEqual(self.event("visit_started", 21, event_id="later")["status"], "recorded")

    def test_paused_observations_arriving_after_resume_use_occurrence_time(self):
        self.activity.configure(False)  # Paused at +1000, resumed at +1100.
        self.now += 100
        self.activity.configure(True)
        self.now += 100
        for second in (1000, 1050, 1099.99):
            for kind in ("visit_started", "visit_ended"):
                receipt = self.event(kind, second, source="navigation")
                self.assertEqual(receipt["reason"], "recording_paused_at_occurrence")
        for second in (999.99, 1100, 1150):
            self.assertEqual(self.event("visit_started", second, source="focus")["status"], "recorded")
        self.assertEqual(self.query()["totals"]["visits"], 3)

    def test_multiple_pauses_and_delayed_uploads_survive_restart(self):
        self.activity.configure(False)
        self.now += 100
        self.activity.configure(True)
        self.now += 100
        self.activity.configure(False)
        self.now += 100
        self.activity.configure(True)
        self.activity = ActivityStore(CoordinationStore(self.store.database_path, clock=lambda: self.now))
        for second in (1050, 1250):
            self.assertEqual(self.event("visit_started", second)["reason"], "recording_paused_at_occurrence")
        self.assertEqual(self.event("visit_started", 1150)["status"], "recorded")

    def test_rejected_visit_retries_stay_skipped_and_retained_duplicates_stay_idempotent(self):
        accepted = self.event("visit_started", 10, event_id="accepted")
        self.activity.configure(False)
        rejected = self.activity.record("visit_started", "one", event_id="paused-no-time")
        ahead = self.event("visit_ended", 1500, event_id="paused-clock-ahead")
        self.assertEqual(rejected["reason"], "recording_paused")
        self.assertEqual(ahead["reason"], "recording_paused")
        self.assertEqual(self.event("visit_started", 10, event_id="accepted"), {**accepted, "status": "duplicate"})
        self.now += 100
        self.activity.configure(True)
        self.assertEqual(self.activity.record("visit_started", "one", event_id="paused-no-time"), rejected)
        self.assertEqual(self.event("visit_ended", 1500, event_id="paused-clock-ahead"), ahead)
        with self.assertRaisesRegex(CoordinationError, "different activity"):
            self.event("visit_started", 11, event_id="accepted")
        self.assertEqual(self.activity.record("visit_started", "one", event_id="fresh")["status"], "recorded")

    def test_visits_before_recording_start_skip_without_changing_backend_acceptance(self):
        receipt = self.event("visit_started", -1, source="navigation")
        self.assertEqual(receipt["reason"], "before_recording_started")
        self.activity.configure(False)
        self.now += 100
        self.activity.configure(True)
        self.activity.clear()
        for kind in ("message_submitted", "decision_submitted", "organization_changed"):
            for second in (-1, 1050):
                self.assertEqual(self.event(kind, second)["status"], "recorded")
            current = self.activity.record(kind, "one")
            self.assertEqual(current["status"], "recorded")
            self.assertEqual(current["occurred_at"], self.now)

    def creator(self, child, parent="one", mode="orchestration"):
        AppControl(self.store)
        with self.store._connection() as db:
            db.execute("""INSERT INTO app_control_requests
                (request_id, operation, sender_session_id, cwd, thread_id, payload_json, status, created_at, updated_at)
                VALUES (?, 'create', ?, ?, ?, ?, 'completed', ?, ?)""",
                ("create-" + child, parent, str(self.root), child, json.dumps({"mode": mode}), self.base, self.base))

    def test_25_direct_prompts_vs_one_prompt_and_25_agents(self):
        for n in range(25):
            self.event("message_submitted", n, event_id="direct-" + str(n))
        self.assertEqual(self.query()["totals"]["messages"], 25)
        self.activity.clear()
        self.base = self.now
        self.now += 1000
        self.event("message_submitted", 100)
        for n in range(25):
            child = "agent-" + str(n)
            self.register(child)
            self.creator(child)
            self.event("message_submitted", 101 + n, child, actor="agent", source="coordination")
            self.event("visit_started", 150 + n, child, actor="system", source="review")
        result = self.query()
        self.assertEqual(result["totals"]["messages"], 1)
        self.assertEqual(result["totals"]["threads"], 1)
        self.assertEqual(result["totals"]["visits"], 0)
        self.assertEqual(len(result["events"]), 51)
        children = [e for e in result["events"] if e["actor"] == "agent"]
        self.assertEqual(len(children), 25)
        self.assertTrue(all(e["snapshot"]["created_by_thread_id"] == "one" for e in children))
        self.assertTrue(all(e["snapshot"]["creation_mode"] == "orchestration" for e in children))

    def test_creator_outside_workspace_is_redacted(self):
        outsider = self.root.with_name(self.root.name + "-outside")
        self.register("private-parent", cwd=outsider)
        self.creator("one", parent="private-parent")
        self.event("message_submitted", 1)
        data = self.query(allowed_cwd=str(self.root))
        self.assertNotIn(str(outsider), json.dumps(data))
        self.assertNotIn("private-parent", json.dumps(data))
        self.assertTrue(data["events"][0]["snapshot"]["creator_outside_workspace"])


if __name__ == "__main__":
    unittest.main()
