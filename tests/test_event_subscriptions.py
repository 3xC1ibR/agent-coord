from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))
from agent_coord.event_subscriptions import EventSubscriptions
from agent_coord.store import CoordinationError, CoordinationStore


class EventSubscriptionsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = 1700000000.0
        self.store = CoordinationStore(self.root / "state.sqlite3", clock=lambda: self.now)
        for session in ("owner", "other", "a", "b"):
            self.store.register(session_id=session, client="codex", cwd=str(self.root))
        self.events = EventSubscriptions(self.store, cwd=str(self.root))
        self.addCleanup(self.events.close)
        self.turn("a", "a1")
        self.turn("b", "b1")

    def turn(self, session, turn):
        self.store.threads.start_turn(session, turn_id=turn)

    def finish(self, session, turn=None, status="completed"):
        self.store.threads.finish_turn(session, turn_id=turn, status=status)

    def messages(self):
        with self.store._connection() as db:
            return [dict(row) for row in db.execute("SELECT * FROM messages WHERE thread_id LIKE 'event:%' ORDER BY id")]

    def test_finish_before_registration_retained_turn_and_notification_claim(self):
        self.finish("a", "a1")
        with self.store._connection() as db:
            db.execute("UPDATE turn_completions SET notified_at = ?", (self.now,))
        subscription = self.events.create("owner", sessions=["a"], continuation="inspect result")
        self.events.process_once()
        result = self.events.get("owner", subscription["id"])
        self.assertEqual(result["state"], "fired")
        self.assertEqual(result["items"][0]["turn_id"], "a1")
        message = self.messages()[0]
        self.assertEqual((message["classification"], message["reply_required"]), ("action_required", 0))
        self.assertEqual((message["sender_session_id"], message["recipient_session_id"]), ("owner", "owner"))
        body = json.loads(message["body"])
        self.assertEqual(body["continuation"], "inspect result")
        self.assertEqual(body["outcomes"][0]["result_ref"], {"session_id": "a", "turn_id": "a1"})
        self.assertIn("not assignment completion", body["meaning"])

    def test_registration_captures_turn_not_future_assignment(self):
        subscription = self.events.create("owner", sessions=["a"])
        self.assertEqual(self.events.process_once(), [])
        self.finish("a", "a1")
        self.turn("a", "a2")
        self.events.process_once()
        self.assertEqual(self.events.get("owner", subscription["id"])["items"][0]["outcome"]["turn_id"], "a1")

    def test_explicit_old_turn_matches_after_new_turn_begins(self):
        self.finish("a", "a1")
        self.turn("a", "a2")
        old = self.events.create("owner", turns=[{"session_id": "a", "turn_id": "a1"}])
        current = self.events.create("owner", sessions=["a"])
        self.events.process_once()
        self.assertEqual(self.events.get("owner", old["id"])["state"], "fired")
        self.assertEqual(self.events.get("owner", current["id"])["state"], "pending")

    def test_initial_message_waits_for_its_delivery_turn_and_ignores_unrelated_turns(self):
        message = self.store.send_message(sender_session_id="owner", recipient_session_id="a",
                                          body="Do the assignment", reply_required=False)
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM messages WHERE id = ?", (message["id"],)).fetchone()
            subscription_id = self.events.create_for_message(db, row)
        self.finish("a", "a1")
        self.assertEqual(self.events.process_once(), [])
        self.turn("a", "a2")
        self.store.touch("a", turn_active=True)
        self.store.inbox("a")
        self.finish("a", "a2")
        self.turn("a", "a3")
        restarted = EventSubscriptions(self.store)
        restarted.process_once()
        result = restarted.get("owner", subscription_id)
        self.assertEqual(result["items"][0]["turn_id"], "a2")
        self.assertEqual(result["items"][0]["outcome"]["assignment_message_id"], message["id"])
        self.assertIsNone(result["items"][0]["outcome"]["context_lifecycle"])
        self.finish("a", "a3")
        restarted.process_once()
        self.assertEqual(len(self.messages()), 1)

    def test_all_counts_terminal_failure_and_interruption(self):
        subscription = self.events.create("owner", sessions=["a", "b"])
        self.finish("a", status="failed")
        self.assertEqual(self.events.process_once(), [])
        self.finish("b", status="interrupted")
        self.events.process_once()
        result = self.events.get("owner", subscription["id"])
        self.assertEqual(result["state"], "fired")
        self.assertEqual({item["outcome"]["state"] for item in result["items"]}, {"failed", "interrupted"})
        self.assertEqual(len(self.messages()), 1)

    def test_any_fires_one_group_even_when_all_targets_finished(self):
        subscription = self.events.create("owner", sessions=["a", "b"], mode="any")
        self.finish("a")
        self.finish("b")
        for _ in range(3):
            self.events.process_once()
        self.assertEqual(len(self.messages()), 1)
        self.assertEqual(len(json.loads(self.messages()[0]["body"])["outcomes"]), 1)
        self.assertEqual(self.events.get("owner", subscription["id"])["state"], "fired")

    def test_any_delayed_scan_reports_first_finisher(self):
        self.events.create("owner", sessions=["a", "b"], mode="any")
        self.finish("b")
        self.now += 1
        self.finish("a")
        self.events.process_once()
        outcome = json.loads(self.messages()[0]["body"])["outcomes"][0]
        self.assertEqual(outcome["session_id"], "b")

    def test_each_fires_once_per_unique_target(self):
        subscription = self.events.create("owner", sessions=["a", "a", "b"],
                                          turns=[{"session_id": "a", "turn_id": "a1"}], mode="each")
        self.finish("a")
        self.events.process_once()
        self.assertEqual(self.events.get("owner", subscription["id"])["state"], "pending")
        self.finish("b")
        self.events.process_once()
        self.events.process_once()
        result = self.events.get("owner", subscription["id"])
        self.assertEqual(result["state"], "fired")
        self.assertEqual(len(result["message_ids"]), 2)
        self.assertEqual(len({message["thread_id"] for message in self.messages()}), 2)

    def test_restart_retains_matches_and_receipts(self):
        subscription = self.events.create("owner", sessions=["a", "b"])
        self.finish("a")
        self.events.process_once()
        restarted_store = CoordinationStore(self.store.database_path, clock=lambda: self.now)
        restarted = EventSubscriptions(restarted_store)
        self.addCleanup(restarted.close)
        self.finish("b")
        restarted.process_once()
        EventSubscriptions(restarted_store).process_once()
        self.assertEqual(len(restarted.get("owner", subscription["id"])["message_ids"]), 1)
        self.assertEqual(len(self.messages()), 1)

    def test_concurrent_processors_deduplicate(self):
        self.events.create("owner", sessions=["a", "b"], mode="each")
        self.finish("a")
        self.finish("b")
        workers = [EventSubscriptions(self.store) for _ in range(4)]
        barrier = threading.Barrier(4)
        def process(worker):
            barrier.wait(timeout=5)
            return worker.process_once()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(process, workers))
        self.assertEqual(sum(map(len, results)), 2)
        self.assertEqual(len(self.messages()), 2)

    def test_concurrent_registration_and_completion_do_not_lose_event(self):
        barrier = threading.Barrier(2)
        def register():
            barrier.wait(timeout=5)
            return self.events.create("owner", sessions=["a"])
        def complete():
            barrier.wait(timeout=5)
            self.finish("a", "a1")
        with ThreadPoolExecutor(max_workers=2) as pool:
            registering = pool.submit(register)
            completing = pool.submit(complete)
            subscription = registering.result(timeout=10)
            completing.result(timeout=10)
        self.events.process_once()
        self.assertEqual(self.events.get("owner", subscription["id"])["state"], "fired")
        self.assertEqual(len(self.messages()), 1)

    def test_worker_starts_once_and_closes(self):
        scanned = threading.Event()
        with patch.object(self.events, "process_once", side_effect=lambda: scanned.set()):
            self.events.start()
            worker = self.events.worker
            self.events.start()
            self.assertIs(self.events.worker, worker)
            self.assertTrue(scanned.wait(timeout=5))
            self.events.close()
            self.assertFalse(worker.is_alive())

    def test_message_and_receipt_rollback_together(self):
        subscription = self.events.create("owner", sessions=["a"])
        self.finish("a")
        original = self.store._insert_message
        def fail(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("simulated interruption after insert")
        with patch.object(self.store, "_insert_message", side_effect=fail):
            with self.assertRaises(RuntimeError):
                self.events.process_once()
        self.assertEqual(self.messages(), [])
        self.assertEqual(self.events.get("owner", subscription["id"])["state"], "pending")
        self.events.process_once()
        self.assertEqual(len(self.messages()), 1)

    def test_timeout_persists_and_fires_once(self):
        subscription = self.events.create("owner", sessions=["a"], timeout=5)
        self.now += 5
        self.events.process_once()
        self.finish("a")
        self.events.process_once()
        self.assertEqual(self.events.get("owner", subscription["id"])["state"], "timed_out")
        self.assertEqual(json.loads(self.messages()[0]["body"])["event"], "subscription_timeout")
        self.assertEqual(len(self.messages()), 1)

    def test_completion_after_deadline_does_not_win_delayed_scan(self):
        subscription = self.events.create("owner", sessions=["a"], timeout=5)
        self.now += 6
        self.finish("a")
        self.events.process_once()
        self.assertEqual(self.events.get("owner", subscription["id"])["state"], "timed_out")

    def test_each_timeout_fires_for_unmatched_remainder_only_once(self):
        subscription = self.events.create("owner", sessions=["a", "b"], mode="each", timeout=5)
        self.finish("a")
        self.events.process_once()
        self.now += 5
        self.events.process_once()
        self.events.process_once()
        self.assertEqual(self.events.get("owner", subscription["id"])["state"], "timed_out")
        self.assertEqual(len(self.messages()), 2)

    def test_cancel_pending_and_already_generated_undelivered_messages(self):
        pending = self.events.create("owner", sessions=["b"])
        self.events.cancel("owner", pending["id"])
        fired = self.events.create("owner", sessions=["a"])
        self.finish("a")
        self.events.process_once()
        self.events.cancel("owner", fired["id"])
        self.finish("b")
        self.events.process_once()
        self.assertEqual(len(self.messages()), 1)
        message = self.messages()[0]
        self.assertEqual(message["suppressed_at"], self.now)
        self.assertEqual(message["delivered_at"], self.now)

    def test_cancellation_cannot_recall_delivered_message(self):
        subscription = self.events.create("owner", sessions=["a"])
        self.finish("a")
        self.events.process_once()
        with self.store._connection() as db:
            db.execute("UPDATE messages SET delivered_at = ?", (self.now,))
        self.events.cancel("owner", subscription["id"])
        self.assertIsNone(self.messages()[0]["suppressed_at"])

    def test_archived_owner_defers_delivery_until_reopened(self):
        subscription = self.events.create("owner", sessions=["a"], timeout=5)
        self.store.threads.update("owner", attention="archived")
        self.finish("a")
        self.events.process_once()
        self.assertEqual(self.messages(), [])
        self.assertEqual(self.store.threads.get("owner")["attention"], "archived")
        self.now += 10
        self.store.threads.update("owner", attention="now")
        self.events.process_once()
        self.assertEqual(self.events.get("owner", subscription["id"])["state"], "fired")
        self.assertEqual(len(self.messages()), 1)

    def test_archived_owner_timeout_and_cancel(self):
        subscription = self.events.create("owner", sessions=["a"], timeout=5)
        self.store.threads.update("owner", attention="archived")
        self.now += 5
        self.events.process_once()
        self.events.cancel("owner", subscription["id"])
        self.store.threads.update("owner", attention="now")
        self.events.process_once()
        self.assertEqual(self.messages(), [])

    def test_event_delivery_preserves_stop_pause(self):
        from agent_coord.browser_wake import BrowserInboxWake
        wake = BrowserInboxWake(SimpleNamespace(store=self.store))
        wake.pause("owner", "Stopped by user")
        self.events.create("owner", sessions=["a"])
        self.finish("a")
        self.events.process_once()
        with self.store._connection() as db:
            pause = db.execute("SELECT paused, error FROM browser_inbox_wake WHERE thread_id = 'owner'").fetchone()
        self.assertEqual((pause["paused"], pause["error"]), (1, "Stopped by user"))
        self.assertEqual(len(self.messages()), 1)

    def test_owner_and_workspace_isolation(self):
        subscription = self.events.create("owner", sessions=["a"])
        for operation in (self.events.get, self.events.cancel):
            with self.assertRaises(CoordinationError):
                operation("other", subscription["id"])
        self.assertEqual(self.events.list("other"), [])
        elsewhere = EventSubscriptions(self.store, cwd=str(self.root / "elsewhere"))
        self.finish("a")
        self.assertEqual(elsewhere.process_once(), [])
        with self.assertRaises(CoordinationError):
            elsewhere.get("owner", subscription["id"])

    def test_idempotent_create_does_not_recapture_new_turn(self):
        first = self.events.create("owner", sessions=["a"], subscription_id="retry")
        self.turn("a", "a2")
        second = self.events.create("owner", sessions=["a"], subscription_id="retry")
        self.assertEqual(first, second)
        with self.assertRaises(CoordinationError):
            self.events.create("owner", sessions=["b"], subscription_id="retry")

    def test_reject_invalid_registration(self):
        invalid = [{}, {"sessions": []}, {"sessions": "a"}, {"sessions": ["owner"]},
                   {"sessions": ["other"]}, {"sessions": ["missing"]},
                   {"turns": [{"session_id": "owner", "turn_id": "x"}]},
                   {"turns": [{"session_id": "a"}]}, {"sessions": ["a"], "mode": "forever"},
                   {"sessions": ["a"], "continuation": "x" * 16001}]
        invalid.extend({"sessions": ["a"], "timeout": value} for value in (0, -1, float("inf"), float("nan"), True, "5", 10 ** 1000))
        invalid.append({"sessions": ["a"], "subscription_id": ""})
        for kwargs in invalid:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(CoordinationError):
                    self.events.create("owner", **kwargs)
        self.assertEqual(self.events.list("owner"), [])

    def job(self, job_id, *, owner="owner", state="queued"):
        from agent_coord.jobs import ManagedJobs
        jobs = ManagedJobs(self.store)
        job = jobs.create(owner, ["printf", "result"], job_id=job_id)
        if state != "queued":
            with self.store._connection() as db:
                db.execute("UPDATE jobs SET state = ?, exit_code = ?, error = ? WHERE id = ?",
                           (state, 0 if state == "completed" else 1, "failure detail" if state == "failed" else None, job["id"]))
        return job

    def test_job_terminal_states_and_bounded_results(self):
        for state in ("completed", "failed", "cancelled", "timed_out", "unknown"):
            with self.subTest(state=state):
                job = self.job("job-" + state, state=state)
                subscription = self.events.create("owner", jobs=[job["id"]])
                self.events.process_once()
                result = self.events.get("owner", subscription["id"])
                self.assertEqual(result["state"], "fired")
                self.assertEqual(result["items"][0]["outcome"]["state"], state)
                self.assertEqual(result["items"][0]["outcome"]["result_ref"], {"job_id": job["id"]})
        self.assertEqual(len(self.messages()), 5)

    def test_job_owner_only_and_missing_jobs(self):
        self.job("private", owner="other")
        for job_id in ("private", "missing"):
            with self.subTest(job=job_id):
                with self.assertRaises(CoordinationError):
                    self.events.create("owner", jobs=[job_id])
        self.assertEqual(self.events.list("owner"), [])

    def test_mixed_turn_and_job_all_and_restart(self):
        self.job("command")
        subscription = self.events.create("owner", sessions=["a"], jobs=["command"])
        self.finish("a", status="interrupted")
        self.assertEqual(self.events.process_once(), [])
        restarted = EventSubscriptions(self.store)
        self.addCleanup(restarted.close)
        with self.store._connection() as db:
            db.execute("UPDATE jobs SET state = 'failed', exit_code = 3, error = ? WHERE id = 'command'", ("x" * 10000,))
        restarted.process_once()
        self.assertEqual(restarted.get("owner", subscription["id"])["state"], "fired")
        outcomes = json.loads(self.messages()[0]["body"])["outcomes"]
        job = next(item for item in outcomes if item["kind"] == "job")
        self.assertEqual(job["exit_code"], 3)
        self.assertEqual(len(job["error"]), 2000)

    def test_any_mixed_targets_uses_job_finish_time(self):
        self.job("first-job")
        self.events.create("owner", sessions=["a"], jobs=["first-job"], mode="any")
        with self.store._connection() as db:
            db.execute("UPDATE jobs SET state = 'completed', finished_at = ? WHERE id = 'first-job'", (self.now,))
        self.now += 1
        self.finish("a")
        self.events.process_once()
        outcome = json.loads(self.messages()[0]["body"])["outcomes"][0]
        self.assertEqual(outcome["kind"], "job")
        self.assertEqual(outcome["finished_at"], self.now - 1)

    def test_job_completion_after_deadline_times_out(self):
        self.job("late-job")
        subscription = self.events.create("owner", jobs=["late-job"], timeout=5)
        self.now += 6
        with self.store._connection() as db:
            db.execute("UPDATE jobs SET state = 'completed', finished_at = ? WHERE id = 'late-job'", (self.now,))
        self.events.process_once()
        self.assertEqual(self.events.get("owner", subscription["id"])["state"], "timed_out")


if __name__ == "__main__":
    unittest.main()
