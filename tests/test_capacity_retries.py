"""Capacity retry probes use deterministic providers and isolated SQLite only."""
import copy
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from test_codex_app_server import BrowserSessions, CoordinationStore, FakeCodex
from agent_coord.capacity_retries import CapacityRetries, retry_delay, safe_capacity_failure, retained_context
from agent_coord.codex_app_server import BrowserBusyError
from agent_coord.store import CoordinationError
from agent_coord.hook import _actionable_context


class CapacityCodex(FakeCodex):
    def __init__(self, store, callback):
        super().__init__(store, callback)
        self.sequence = 0
        self.reject_immediately = False
        self.fail_revert = self.fail_after_accept = False
        self.hook = None

    def request(self, method, params=None):
        if method == "thread/revert":
            self.calls.append((method, copy.deepcopy(params)))
            if self.fail_revert:
                raise RuntimeError("connection lost during revert")
            turns = self.threads[params["threadId"]]["turns"]
            index = next(i for i, t in enumerate(turns) if t["id"] == params["beforeTurnId"])
            del turns[index:]
            # Current protocol returns metadata only, never retained turns.
            thread = copy.deepcopy(self.threads[params["threadId"]])
            thread["turns"] = []
            return {"thread": thread}
        if method == "turn/start":
            self.calls.append((method, copy.deepcopy(params)))
            self.sequence += 1
            tid = params["threadId"]
            turn = {"id": "capacity-turn-" + str(self.sequence), "status": "inProgress", "items": [
                {"id": "input-" + str(self.sequence), "type": "userMessage", "content": copy.deepcopy(params["input"])}]}
            self.threads[tid]["turns"].append(turn)
            self.callback({"method": "turn/started", "params": {"threadId": tid, "turn": copy.deepcopy(turn)}})
            if self.hook:
                self.hook(tid)
            if self.reject_immediately:
                self.finish(tid, "failed")
            if self.fail_after_accept:
                raise RuntimeError("response timed out after acceptance")
            return {"turn": copy.deepcopy(turn)}
        return super().request(method, params)

    def finish(self, tid, status="completed", *, error=None, extra=None, termination=None, thin=False):
        turn = self.threads[tid]["turns"][-1]
        turn["status"] = status
        if status == "failed":
            turn["error"] = error or {"message": "Selected model is at capacity.", "codexErrorInfo": "serverOverloaded"}
        if extra:
            turn["items"].append(extra)
            self.callback({"method": "item/completed", "params": {"threadId": tid, "turnId": turn["id"], "item": extra}})
        if termination:
            turn["termination"] = termination
        event = copy.deepcopy(turn)
        if thin:
            event["items"] = []
        self.callback({"method": "turn/completed", "params": {"threadId": tid, "turn": event}})


class CapacityRetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = 1700000000.
        self.store = CoordinationStore(self.root / "coord.sqlite3", clock=lambda: self.now)
        self.sessions = BrowserSessions(self.store, str(self.root), rpc_factory=CapacityCodex)
        self.addCleanup(self.sessions.close)
        self.retry = self.sessions.capacity_retries
        self.retry.jitter = lambda: 0
        self.tid = self.sessions.create({"model": "available-model", "effort": "high", "yolo": True})["session"]["thread_id"]
        self.rpc = self.sessions.rpc
        self.store.register(session_id="owner", client="codex", cwd=str(self.root))

    def flush(self):
        self.assertTrue(self.sessions.persistence.flush())

    def start(self):
        self.sessions.send(self.tid, {"message": "Perform the original task"})
        self.flush()

    def reject(self, **kwargs):
        self.rpc.finish(self.tid, "failed", **kwargs)
        self.flush()

    def due(self):
        state = self.retry.snapshot(self.tid)
        self.now = state["next_at"]
        self.retry.process_once()
        self.flush()

    def completions(self):
        with self.store._connection() as db:
            return [dict(r) for r in db.execute("SELECT * FROM turn_completions WHERE thread_id=?", (self.tid,))]

    def test_transient_success_same_request_one_native_and_mirror_prompt(self):
        self.start()
        original = next(p for m, p in self.rpc.calls if m == "turn/start")
        old = self.rpc.threads[self.tid]["turns"][-1]["id"]
        self.reject()
        self.assertEqual(self.retry.snapshot(self.tid)["state"], "waiting")
        self.assertEqual(self.completions(), [])
        self.assertFalse(self.sessions.work_thread(self.tid)["needs_attention"])
        self.retry.process_once()
        self.assertEqual(self.rpc.sequence, 1, "not before due time")
        self.due()
        self.assertEqual([p for m, p in self.rpc.calls if m == "turn/start"], [original, original])
        native = self.rpc.threads[self.tid]["turns"]
        self.assertEqual(len(native), 1)
        self.assertNotEqual(native[0]["id"], old)
        self.assertEqual(len(self.sessions.read(self.tid)["thread"]["turns"]), 1)
        self.rpc.finish(self.tid)
        self.flush()
        self.assertIsNone(self.retry.snapshot(self.tid))
        self.assertEqual([r["status"] for r in self.completions()], ["completed"])
        with self.store._connection() as db:
            self.assertEqual(db.execute("SELECT paused FROM browser_inbox_wake WHERE thread_id=?", (self.tid,)).fetchone()[0], 0)

    def test_metadata_only_revert_response_preserves_earlier_native_history(self):
        self.start()
        earlier = self.rpc.threads[self.tid]["turns"][-1]["id"]
        self.rpc.finish(self.tid); self.flush()
        self.start(); self.reject(); self.due()
        native = self.rpc.threads[self.tid]["turns"]
        self.assertEqual(len(native), 2)
        self.assertEqual(native[0]["id"], earlier)
        self.assertEqual(self.rpc.sequence, 3)
        calls = [method for method, _ in self.rpc.calls]
        reverted = calls.index("thread/revert")
        self.assertEqual(calls[reverted:reverted+3], ["thread/revert", "thread/read", "turn/start"])

    def test_noop_revert_metadata_cannot_authorize_a_duplicate_prompt(self):
        self.start(); self.reject()
        original = self.rpc.request
        def no_op(method, params=None):
            if method == "thread/revert":
                self.rpc.calls.append((method, copy.deepcopy(params)))
                return {"thread": {"id": self.tid, "turns": []}}
            return original(method, params)
        self.rpc.request = no_op
        self.due()
        self.assertEqual(self.retry.snapshot(self.tid)["state"], "uncertain")
        self.assertEqual(self.rpc.sequence, 1)
        self.assertEqual(len(self.rpc.threads[self.tid]["turns"]), 1)

    def test_overrevert_or_postrevert_read_failure_never_submits(self):
        for failure in ("overrevert", "read_failure"):
            with self.subTest(failure=failure):
                self.start(); self.rpc.finish(self.tid); self.flush()
                self.start(); self.reject()
                original = self.rpc.request
                reverted = False
                def broken(method, params=None):
                    nonlocal reverted
                    if method == "thread/read" and reverted and failure == "read_failure":
                        raise RuntimeError("native history unavailable after revert")
                    result = original(method, params)
                    if method == "thread/revert":
                        reverted = True
                        if failure == "overrevert":
                            self.rpc.threads[self.tid]["turns"] = []
                    return result
                self.rpc.request = broken
                count = self.rpc.sequence
                self.due()
                self.assertEqual(self.retry.snapshot(self.tid)["state"], "uncertain")
                self.assertEqual(self.rpc.sequence, count)
                self.rpc.request = original

    def test_rejected_user_input_preserves_retry_and_original_completion(self):
        self.start(); self.reject()
        for body in ({"message": "Steer", "expectedTurnId": "stale"},
                     {"message": ""}, {"message": "/model nonexistent"}):
            with self.subTest(body=body):
                with self.assertRaises(CoordinationError):
                    self.sessions.send(self.tid, body)
                self.assertEqual(self.retry.snapshot(self.tid)["state"], "waiting")
                self.assertEqual(self.completions(), [])
        self.due()
        self.assertEqual(self.rpc.sequence, 2)

    def test_rejected_queue_input_keeps_waiting_retry(self):
        self.start(); self.reject()
        with patch.object(self.sessions, "read", side_effect=RuntimeError("read failed")):
            with self.assertRaisesRegex(RuntimeError, "read failed"):
                self.sessions.queue.enqueue(self.tid, {"message": "Queue rejected"})
        self.assertEqual(self.retry.snapshot(self.tid)["state"], "waiting")
        with self.store._connection() as db:
            db.execute("""CREATE TRIGGER reject_queue BEFORE INSERT ON browser_message_queue
                BEGIN SELECT RAISE(ABORT,'queue acceptance failed'); END""")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "queue acceptance failed"):
            self.sessions.queue.enqueue(self.tid, {"message": "Queue rejected"})
        self.assertEqual(self.retry.snapshot(self.tid)["state"], "waiting")
        self.assertEqual(self.completions(), [])
        self.due()
        self.assertEqual(self.rpc.sequence, 2)

    def test_provider_lookup_failure_before_submit_keeps_pending_request(self):
        self.start(); self.reject()
        with patch.object(self.sessions, "_rpc", side_effect=RuntimeError("provider lookup failed")):
            with self.assertRaisesRegex(RuntimeError, "provider lookup failed"):
                self.sessions.send(self.tid, {"message": "Rejected before submission"})
        self.assertEqual(self.retry.snapshot(self.tid)["state"], "waiting")
        self.assertNotIn(self.tid, self.sessions.active)
        self.due()
        self.assertEqual(self.rpc.sequence, 2)

    def test_finite_four_retries_then_one_failure(self):
        self.start()
        for attempt in range(1, 5):
            self.reject()
            self.assertEqual(self.retry.snapshot(self.tid)["attempt"], attempt)
            self.assertEqual(self.completions(), [])
            self.due()
        self.reject()
        self.assertEqual(self.retry.snapshot(self.tid)["state"], "exhausted")
        self.assertEqual(self.rpc.sequence, 5)
        self.assertEqual([r["status"] for r in self.completions()], ["failed"])
        self.retry.process_once()
        self.assertEqual(self.rpc.sequence, 5)

    def test_jitter_backoff_and_typed_provider_delay(self):
        self.assertEqual(retry_delay({}, 1, lambda: 0), 5)
        self.assertEqual(retry_delay({}, 4, lambda: 1), 50)
        self.assertEqual(retry_delay({"additionalDetails": {"retryAfterMs": 20000}}, 1, lambda: 0), 20)
        self.start()
        self.reject(error={"message": "capacity", "codexErrorInfo": "serverOverloaded", "retryAfter": 301})
        self.assertIsNone(self.retry.snapshot(self.tid))
        self.assertEqual(len(self.completions()), 1)

    def test_negative_controls_never_retry(self):
        for error, extra, termination in [
            ({"message": "Selected model is at capacity"}, None, None),
            ({"message": "quota", "codexErrorInfo": "usageLimitExceeded"}, None, None),
            ({"message": "auth", "codexErrorInfo": "unauthorized"}, None, None),
            (None, {"id": "assistant", "type": "agentMessage", "text": "Started"}, None),
            (None, {"id": "tool", "type": "commandExecution", "command": "side effect"}, None),
            (None, {"id": "reason", "type": "reasoning", "summary": []}, None),
            (None, None, {"origin": "user"}),
            (None, None, {"execution": "unknown"}),
        ]:
            with self.subTest(error=error, extra=extra, termination=termination):
                self.start()
                self.reject(error=error, extra=extra, termination=termination, thin=bool(extra))
                self.assertIsNone(self.retry.snapshot(self.tid))

    def test_stop_cancels_timer_and_preserves_pause(self):
        self.start(); self.reject()
        self.sessions.interrupt(self.tid)
        self.now += 400
        self.retry.process_once()
        self.assertEqual(self.rpc.sequence, 1)
        with self.store._connection() as db:
            self.assertEqual(db.execute("SELECT pause_kind FROM browser_inbox_wake WHERE thread_id=?", (self.tid,)).fetchone()[0], "stop")

    def test_new_user_input_supersedes_without_old_replay(self):
        self.start(); self.reject()
        self.sessions.send(self.tid, {"message": "Do a different task"})
        self.flush()
        self.now += 400
        self.retry.process_once()
        self.assertEqual(self.rpc.sequence, 2)
        self.assertFalse(any(m == "thread/revert" for m, _ in self.rpc.calls))

    def test_automatic_wake_cannot_cancel_or_bypass_capacity_pause(self):
        self.start(); self.reject()
        with self.assertRaises(BrowserBusyError):
            self.sessions.send(self.tid, {"message": "Another automatic wake"}, automatic=True)
        self.assertEqual(self.retry.snapshot(self.tid)["state"], "waiting")
        self.assertEqual(self.rpc.sequence, 1)

    def test_close_cancels_and_stays_archived(self):
        self.start(); self.reject()
        self.sessions.close_work_thread(self.tid)
        self.now += 400
        self.retry.process_once()
        self.assertEqual(self.rpc.sequence, 1)
        self.assertEqual(self.store.threads.get(self.tid)["attention"], "archived")

    def test_queued_input_cancels(self):
        self.start(); self.reject()
        self.sessions.queue.enqueue(self.tid, {"message": "Next request"})
        self.now += 400
        self.retry.process_once()
        self.assertEqual(self.rpc.sequence, 1)

    def test_archive_and_deleted_thread_cannot_retry(self):
        self.start(); self.reject()
        self.sessions.update(self.tid, {"archived": True})
        self.assertIsNone(self.retry.snapshot(self.tid))
        self.now += 400
        self.retry.process_once()
        self.assertEqual(self.rpc.sequence, 1)
        self.sessions.update(self.tid, {"archived": False})
        self.start(); self.reject()
        self.now = self.retry.snapshot(self.tid)["next_at"]
        with self.store._connection() as db:
            # No delete API exists today; simulate an externally removed thread.
            db.execute("PRAGMA foreign_keys=OFF")
            db.execute("DELETE FROM work_threads WHERE thread_id=?", (self.tid,))
        self.retry.process_once()
        self.assertEqual(self.rpc.sequence, 2)

    def test_deadline_expiry_reports_one_failure_without_resend(self):
        self.start(); self.reject()
        self.now += 301
        self.retry.process_once(); self.retry.process_once()
        self.assertEqual(self.retry.snapshot(self.tid)["state"], "exhausted")
        self.assertEqual(self.rpc.sequence, 1)
        self.assertEqual([r["status"] for r in self.completions()], ["failed"])

    def test_missing_native_history_never_uses_display_mirror_as_proof(self):
        self.start(); self.reject()
        self.rpc.no_history = True
        self.due()
        self.assertEqual(self.retry.snapshot(self.tid)["state"], "uncertain")
        self.assertEqual(self.rpc.sequence, 1)
        self.assertTrue(self.sessions.work_thread(self.tid)["needs_attention"])

    def test_initial_timeout_after_acceptance_never_creates_retry(self):
        self.rpc.fail_after_accept = True
        with self.assertRaisesRegex(RuntimeError, "timed out"):
            self.start()
        self.flush()
        self.assertIsNone(self.retry.snapshot(self.tid))
        self.now += 400
        self.retry.process_once()
        self.assertEqual(self.rpc.sequence, 1)

    def test_steered_request_is_never_replayed_from_original_input(self):
        self.start()
        self.sessions.send(self.tid, {"message": "Add this correction"})
        self.reject()
        self.assertIsNone(self.retry.snapshot(self.tid))

    def test_actual_runtime_restart_keeps_waiting_retry_and_fires_once(self):
        self.start(); self.reject()
        native = copy.deepcopy(self.rpc.threads)
        settings = copy.deepcopy(self.rpc.settings)
        sequence = self.rpc.sequence
        self.sessions.runtime_handover = True
        self.sessions.close()
        def factory(store, callback):
            rpc = CapacityCodex(store, callback)
            rpc.threads, rpc.settings, rpc.sequence = copy.deepcopy(native), copy.deepcopy(settings), sequence
            return rpc
        self.sessions = BrowserSessions(self.store, str(self.root), rpc_factory=factory)
        self.addCleanup(self.sessions.close)
        self.retry = self.sessions.capacity_retries
        self.retry.jitter = lambda: 0
        self.due()
        self.rpc = self.sessions.rpc
        self.retry.process_once()
        self.assertEqual(self.rpc.sequence, 2)
        self.rpc.finish(self.tid); self.flush()
        self.assertEqual([r["status"] for r in self.completions()], ["completed"])

    def test_changed_settings_cancel_instead_of_substitution(self):
        self.start(); self.reject()
        self.sessions.update(self.tid, {"effort": "medium"})
        self.due()
        self.assertEqual(self.rpc.sequence, 1)
        self.assertIsNone(self.retry.snapshot(self.tid))

    def test_historical_and_stopped_failures_never_schedule(self):
        for kind in ("stop", "unknown", "failure", "pre_submit"):
            with self.subTest(kind=kind):
                self.start()
                self.sessions.inbox_wake.pause(self.tid, "Retain guard", kind=kind)
                self.reject()
                self.assertIsNone(self.retry.snapshot(self.tid))
        self.start()
        with self.store._connection() as db:
            db.execute("DELETE FROM capacity_turn_requests")
        self.reject()
        self.assertIsNone(self.retry.snapshot(self.tid))

    def test_duplicate_completion_and_two_workers_claim_once(self):
        self.start(); self.reject()
        self.reject()
        self.now = self.retry.snapshot(self.tid)["next_at"]
        other = CapacityRetries(self.sessions, jitter=lambda: 0)
        with ThreadPoolExecutor(2) as pool:
            list(pool.map(lambda worker: worker.process_once(), [self.retry, other]))
        self.flush()
        self.assertEqual(self.rpc.sequence, 2)

    def test_restart_waiting_once_and_claimed_delivery_never_replayed(self):
        self.start(); self.reject()
        restarted = CapacityRetries(self.sessions, jitter=lambda: 0)
        self.now = restarted.snapshot(self.tid)["next_at"]
        restarted.process_once(); restarted.process_once(); self.flush()
        self.assertEqual(self.rpc.sequence, 2)
        self.reject()
        with self.store._connection() as db:
            db.execute("UPDATE capacity_retries SET state='submitting'")
        restarted = CapacityRetries(self.sessions)
        self.now += 400
        restarted.process_once()
        self.assertEqual(restarted.snapshot(self.tid)["state"], "uncertain")
        self.assertEqual(self.rpc.sequence, 2)

    def test_revert_unknown_or_timeout_after_accept_never_resubmitted(self):
        for where in ("fail_revert", "fail_after_accept"):
            with self.subTest(where=where):
                self.start(); self.reject()
                setattr(self.rpc, where, True)
                self.due()
                self.assertEqual(self.retry.snapshot(self.tid)["state"], "uncertain")
                count = self.rpc.sequence
                self.now += 400
                self.retry.process_once()
                self.assertEqual(self.rpc.sequence, count)
                setattr(self.rpc, where, False)

    def test_fast_rejection_before_response_still_schedules(self):
        self.rpc.reject_immediately = True
        self.start()
        self.assertEqual(self.retry.snapshot(self.tid)["state"], "waiting")
        self.due()
        self.assertEqual(self.retry.snapshot(self.tid)["state"], "waiting")
        self.assertEqual(self.rpc.sequence, 2)
        self.assertEqual(self.completions(), [])

    def test_stop_wins_send_lock_before_due_worker(self):
        self.start(); self.reject()
        self.now = self.retry.snapshot(self.tid)["next_at"]
        with self.sessions._thread_lock(self.tid):
            with ThreadPoolExecutor(1) as pool:
                future = pool.submit(self.retry.process_once)
                self.sessions.interrupt(self.tid)
                # Release before joining the waiting worker.
                self.sessions._thread_lock(self.tid).release()
                try:
                    future.result(3)
                finally:
                    self.sessions._thread_lock(self.tid).acquire()
        self.assertEqual(self.rpc.sequence, 1)

    def test_runtime_admission_pause_blocks_retry_then_resumes_once(self):
        self.start(); self.reject()
        self.now = self.retry.snapshot(self.tid)["next_at"]
        self.sessions.admission.pause()
        self.retry.process_once()
        self.assertEqual(self.rpc.sequence, 1)
        self.sessions.admission.resume()
        self.retry.process_once(); self.flush()
        self.assertEqual(self.rpc.sequence, 2)

    def test_pending_status_reads_do_not_emit_an_endless_completion_refresh_loop(self):
        self.start(); self.reject()
        self.sessions.events.clear()
        for _ in range(4):
            self.sessions.read(self.tid)
        self.assertEqual([e for e in self.sessions.events if e["method"] == "turn/completed"], [])

    def test_new_input_supersedes_uncertain_retry_status_without_replay(self):
        self.start(); self.reject()
        self.rpc.fail_revert = True
        self.due()
        self.assertTrue(self.sessions.work_thread(self.tid)["needs_attention"])
        self.rpc.fail_revert = False
        self.sessions.send(self.tid, {"message": "Fresh instructions after review"}); self.flush()
        self.assertIsNone(self.retry.snapshot(self.tid))
        self.assertFalse(self.sessions.work_thread(self.tid)["needs_attention"])

    def test_inbox_delivery_receipts_wake_and_subscription_follow_final_turn_once(self):
        message = self.store.send_message(sender_session_id="owner", recipient_session_id=self.tid,
                                          body="Handle the assignment", reply_required=False)
        with self.store._connection() as db:
            row = db.execute("SELECT * FROM messages WHERE id=?", (message["id"],)).fetchone()
            self.sessions.event_subscriptions.create_for_message(db, row)
        self.sessions.inbox_wake.process_once(); self.flush()
        _actionable_context(self.store, self.tid)
        with self.store._connection() as db:
            receipt = dict(db.execute("SELECT * FROM messages WHERE id=?", (message["id"],)).fetchone())
        self.reject()
        self.sessions.event_subscriptions.process_once()
        self.assertEqual(self.completions(), [])
        self.rpc.hook = lambda tid: self.assertIn("Handle the assignment", _actionable_context(self.store, tid))
        self.due()
        self.assertEqual([m["id"] for m in retained_context(self.store, self.tid)], [message["id"]])
        self.rpc.finish(self.tid); self.flush()
        self.sessions.event_subscriptions.process_once(); self.sessions.event_subscriptions.process_once()
        with self.store._connection() as db:
            after = dict(db.execute("SELECT * FROM messages WHERE id=?", (message["id"],)).fetchone())
            self.assertEqual(db.execute("SELECT COUNT(*) FROM message_wake_attempts WHERE message_id=?", (message["id"],)).fetchone()[0], 1)
            outcomes = list(db.execute("SELECT * FROM event_subscription_items WHERE assignment_message_id=?", (message["id"],)))
        self.assertEqual(after["delivered_at"], receipt["delivered_at"])
        self.assertEqual(after["recipient_turn_id"], self.rpc.threads[self.tid]["turns"][-1]["id"])
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0]["state"], "matched")
        subscription = self.sessions.event_subscriptions.get("owner", outcomes[0]["subscription_id"])
        self.assertEqual(subscription["state"], "fired")
        self.assertEqual(len(subscription["message_ids"]), 1)
        self.assertEqual(len(self.completions()), 1)
