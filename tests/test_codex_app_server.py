from __future__ import annotations

import copy
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

PLUGIN_SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"
sys.path.insert(0, str(PLUGIN_SCRIPTS))

from agent_coord.codex_app_server import BrowserBusyError, BrowserSessions, CodexRPC
from agent_coord.store import CoordinationError, CoordinationStore


EMPTY_ROLLOUT_ERROR = (
    "failed to read thread: thread-store internal error: failed to read session metadata "
    "/sessions/rollout.jsonl: rollout at /sessions/rollout.jsonl is empty"
)


class FakeCodex:
    def __init__(self, store, callback):
        self.callback = callback
        self.threads = {}
        self.settings = {}
        self.calls = []
        self.writes = []
        self.fail_turn = False
        self.fail_steer = False
        self.fast_turn = False
        self.no_history = False
        self.empty_rollout_reads = {}
        self.closed = False

    def request(self, method, params=None):
        params = params or {}
        self.calls.append((method, params))
        thread_id = params.get("threadId")
        if method == "model/list":
            return {"data": [
                {"model": "available-model", "isDefault": True, "serviceTiers": [{"id": "priority"}, {"id": "ultrafast"}], "defaultReasoningEffort": "medium", "supportedReasoningEfforts": [{"reasoningEffort": "medium"}, {"reasoningEffort": "high"}]},
                {"model": "other-model", "defaultReasoningEffort": "low", "supportedReasoningEfforts": [{"reasoningEffort": "low"}]},
            ]}
        if method == "thread/start":
            thread_id = "thread-" + str(len(self.threads) + 1)
            self.threads[thread_id] = {"id": thread_id, "cwd": params["cwd"], "turns": []}
        if method == "thread/fork":
            parent = thread_id
            thread_id = "thread-" + str(len(self.threads) + 1)
            self.threads[thread_id] = {**copy.deepcopy(self.threads[parent]), "id": thread_id,
                                       "sessionId": parent, "forkedFromId": parent}
            self.settings[thread_id] = copy.deepcopy(self.settings.get(parent, {"model": "available-model", "reasoningEffort": "medium"}))
        if method in {"thread/start", "thread/fork", "thread/read", "thread/resume"}:
            if method == "thread/read" and self.empty_rollout_reads.get(params.get("includeTurns"), 0):
                self.empty_rollout_reads[params.get("includeTurns")] -= 1
                raise CoordinationError(EMPTY_ROLLOUT_ERROR)
            if self.no_history and method == "thread/read" and params.get("includeTurns"):
                raise CoordinationError("list_turns is not supported yet")
            if thread_id not in self.threads:
                raise CoordinationError("Thread not found.")
            thread = copy.deepcopy(self.threads[thread_id])
            if params.get("excludeTurns") or (method == "thread/read" and not params.get("includeTurns")):
                thread["turns"] = []
            if method in {"thread/start", "thread/fork", "thread/resume"}:
                settings = self.settings.setdefault(thread_id, {"model": "available-model", "reasoningEffort": "medium"})
                settings["model"] = params.get("model", settings["model"])
                settings["reasoningEffort"] = params.get("config", {}).get("model_reasoning_effort", settings["reasoningEffort"])
                if "serviceTier" in params:
                    settings["serviceTier"] = "priority" if params["serviceTier"] == "fast" else params["serviceTier"]
                return {"thread": thread, **settings}
            return {"thread": thread}
        if method == "turn/start":
            if self.fail_turn:
                raise CoordinationError("Provider unavailable.")
            turn = {"id": "turn-" + str(len(self.threads[thread_id]["turns"])), "status": "inProgress", "items": [{"id": "user", "type": "userMessage", "content": params["input"]}]}
            self.threads[thread_id]["turns"].append(turn)
            self.callback({"method": "turn/started", "params": {"threadId": thread_id, "turn": turn}})
            if self.fast_turn:
                turn["status"] = "completed"
                self.callback({"method": "turn/completed", "params": {"threadId": thread_id, "turn": turn}})
            return {"turn": copy.deepcopy(turn)}
        if method == "turn/steer":
            if self.fail_steer:
                raise CoordinationError("Steering unavailable.")
            turn = self.threads[thread_id]["turns"][-1]
            if turn["status"] != "inProgress" or turn["id"] != params["expectedTurnId"]:
                raise CoordinationError("Active turn mismatch.")
            item = {"id": "steer-" + str(len(turn["items"])), "type": "userMessage", "content": params["input"]}
            turn["items"].append(item)
            self.callback({"method": "item/completed", "params": {"threadId": thread_id, "turnId": turn["id"], "item": item}})
            return {"turnId": turn["id"]}
        if method == "turn/interrupt":
            turn = self.threads[thread_id]["turns"][-1]
            if params.get("turnId") and (turn["status"] != "inProgress" or turn["id"] != params["turnId"]):
                raise CoordinationError(f"expected active turn id {turn['id']} but found {params['turnId']}")
            turn["status"] = "interrupted"
            self.callback({"method": "turn/completed", "params": {"threadId": thread_id, "turn": turn}})
        return {}

    def write(self, message):
        self.writes.append(message)

    def close(self):
        self.closed = True
        self.callback({"method": "bridge/disconnected", "params": {}})


class BrowserSessionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = CoordinationStore(self.root / "coord.sqlite3")
        self.sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(self.sessions.close)

    def create(self, **values):
        return self.sessions.create({"name": "Feature discussion", **values})["session"]["thread_id"]

    def usage_event(self, thread_id, total, **kwargs):
        self.sessions._event({"method": "thread/tokenUsage/updated", "params": {
            "threadId": thread_id, "tokenUsage": {"total": {"inputTokens": total-10,
            "outputTokens": 10, "totalTokens": total}, "last": {"totalTokens": 20},
            "modelContextWindow": 1000}, **kwargs}})

    def test_startup_skips_a_browser_session_whose_creation_was_interrupted(self):
        # Observed live: a create hit "database is locked" after saving the
        # conversation row, and every later runtime start would have failed.
        with self.store._connection() as db:
            db.execute("""INSERT INTO browser_sessions (thread_id, cwd, name, created_at, updated_at, client)
                VALUES ('interrupted', ?, 'Interrupted', 1, 1, 'codex')""", (str(self.root),))
        with self.assertLogs("agent_coord.codex_app_server", "WARNING"):
            restarted = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.list_sessions(), [])

    def test_interrupted_creation_does_not_leave_an_unregistered_conversation(self):
        with patch.object(self.store, "register", side_effect=sqlite3.OperationalError("database is locked")):
            with self.assertRaises(sqlite3.OperationalError):
                self.create()
        with self.store._connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM browser_sessions").fetchone()[0], 0)

    def test_idle_send_does_not_read_the_transcript(self):
        # Acceptance must not wait for a whole-history read: an idle loaded
        # conversation has no missed turn to reconcile.
        thread_id = self.create()
        self.sessions.rpc.calls.clear()
        self.sessions.send(thread_id, {"message": "Start"})
        methods = [method for method, _ in self.sessions.rpc.calls]
        self.assertNotIn("thread/read", methods)
        self.assertEqual(methods.count("turn/start"), 1)

    def test_send_reconciles_a_missed_completion_before_choosing_steer(self):
        thread_id = self.create()
        self.sessions.send(thread_id, {"message": "First"})
        self.sessions.rpc.threads[thread_id]["turns"][-1]["status"] = "completed"  # Completion event lost.
        self.sessions.rpc.calls.clear()
        self.sessions.send(thread_id, {"message": "Second"})
        methods = [method for method, _ in self.sessions.rpc.calls]
        self.assertIn("thread/read", methods)
        self.assertNotIn("turn/steer", methods)
        self.assertEqual(methods.count("turn/start"), 1)

    def idle_conversations(self, count):
        threads = [self.create() for _ in range(count)]
        with self.sessions.lock:
            for index, thread_id in enumerate(threads):
                self.sessions.last_used[thread_id] = 1000.0 + index  # Oldest first.
        return threads

    def test_idle_conversations_beyond_the_warm_set_are_released_and_reopen_normally(self):
        threads = self.idle_conversations(9)
        released = self.sessions.release_idle_conversations(now=10_000.0)
        self.assertEqual(released, threads[2::-1], "the six most recent stay warm; older ones go, oldest last")
        self.assertTrue(set(threads[3:]) <= self.sessions.loaded)
        self.assertFalse(set(threads[:3]) & self.sessions.loaded)
        self.assertIsNotNone(self.store.get_session(threads[0])["ended_at"], "shown as saved, as after a restart")
        self.assertIn(("thread/unsubscribe", {"threadId": threads[0]}), self.sessions.rpc.calls)
        self.assertEqual(self.sessions.work_thread(threads[0])["status"], "saved")
        self.sessions.read(threads[0])
        self.assertIn(threads[0], self.sessions.loaded)
        self.sessions.send(threads[1], {"message": "Continue"})
        self.assertIn(threads[1], self.sessions.active)

    def test_recently_used_conversations_wait_for_the_idle_period(self):
        threads = self.idle_conversations(8)
        self.assertEqual(self.sessions.release_idle_conversations(now=1300.0), [])
        self.assertEqual(self.sessions.release_idle_conversations(now=1601.0), [threads[1], threads[0]])

    def test_busy_conversations_are_never_released(self):
        threads = self.idle_conversations(18)  # The seven most recent fill the warm set.
        running, approval, unsaved, queued, wake, waiting, review, paused, stopped_queue = threads[:9]
        self.sessions.send(running, {"message": "Work"})
        with self.sessions.lock:
            self.sessions.requests["approval"] = {"params": {"threadId": approval}}
            self.sessions.dirty_histories.add(unsaved)
        with self.store._connection() as db:
            db.execute("""INSERT INTO browser_message_queue (id, thread_id, message, state, error, owner, created_at)
                VALUES ('q1', ?, 'Next', 'queued', NULL, ?, 1)""", (queued, self.sessions.queue.owner))
            db.execute("""INSERT INTO browser_message_queue (id, thread_id, message, state, error, owner, created_at)
                VALUES ('q2', ?, 'Later', 'paused', 'Stopped', ?, 1)""", (stopped_queue, self.sessions.queue.owner))
        self.store.register(session_id="sender", client="codex", cwd=str(self.root))
        claimed = self.store.send_message(sender_session_id="sender", recipient_session_id=wake, body="Wake")
        with self.store._connection() as db:
            db.execute("INSERT INTO message_wake_attempts (message_id, session_id, attempted_at, outcome) VALUES (?, ?, 1, 'claimed')",
                       (claimed["id"], wake))
            db.execute("""INSERT INTO review_runs (id, owner_id, request_id, request_json, packet_json, cwd, standard, state,
                phase, created_at, deadline) VALUES ('r1', ?, 'req', '{}', '{}', ?, 'strict', 'active', 'draft', 1, 9999999999)""",
                       (review, str(self.root)))
        self.store.send_message(sender_session_id="sender", recipient_session_id=waiting, body="Please look")
        self.store.send_message(sender_session_id="sender", recipient_session_id=paused, body="Paused wake")
        self.sessions.inbox_wake.pause(paused, "You stopped the turn.", kind="stop")
        scoped, delegated = threads[9:11]
        self.store.begin_work(session_id=scoped, scopes=["src/feature.py"])
        delegation = self.store.create_delegation(parent_session_id=running, cwd=str(self.root), scopes=["child.txt"],
                                                  instructions="Child work", mode="reviewed")
        self.store.attach_delegation(delegation["delegation_id"], delegated)
        released = self.sessions.release_idle_conversations(now=10_000.0)
        for thread_id in (running, approval, unsaved, queued, wake, waiting, review):
            self.assertNotIn(thread_id, released, thread_id)
            self.assertIn(thread_id, self.sessions.loaded)
        for thread_id in (scoped, delegated):
            self.assertNotIn(thread_id, released, thread_id)
        self.assertIn(paused, released, "a stopped inbox keeps its pause, not its process")
        self.assertIn(stopped_queue, released, "a paused queue waits for the user, not for a loaded provider")
        with self.store._connection() as db:
            self.assertEqual(db.execute("SELECT paused FROM browser_inbox_wake WHERE thread_id = ?", (paused,)).fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT state FROM browser_message_queue WHERE id = 'q2'").fetchone()[0], "paused")

    def test_release_rechecks_use_and_new_work_after_selection(self):
        threads = self.idle_conversations(10)
        reopened, queued_later, scoped_later = threads[0], threads[1], threads[2]
        selection = self.sessions._durably_busy
        reads = []
        def busy_after_selection(thread_ids):
            result = selection(thread_ids)
            if not reads:  # The selection-time read; then the user and the queue act.
                reads.append(list(thread_ids))
                with self.sessions.lock:
                    self.sessions.last_used[reopened] = 9_999.0
                with self.store._connection() as db:
                    db.execute("""INSERT INTO browser_message_queue (id, thread_id, message, state, error, owner, created_at)
                        VALUES ('late', ?, 'Next', 'queued', NULL, ?, 1)""", (queued_later, self.sessions.queue.owner))
                self.store.begin_work(session_id=scoped_later, scopes=["src/late.py"])
            return result
        with patch.object(self.sessions, "_durably_busy", side_effect=busy_after_selection):
            released = self.sessions.release_idle_conversations(now=10_000.0)
        self.assertEqual(released, [threads[3]])
        self.assertTrue({reopened, queued_later, scoped_later} <= self.sessions.loaded)
        self.assertIsNone(self.store.get_session(scoped_later)["ended_at"], "a newly scoped agent keeps its session")

    def test_release_skips_a_conversation_in_use(self):
        threads = self.idle_conversations(7)
        lock = self.sessions._thread_lock(threads[0])
        with lock:
            result = []
            worker = threading.Thread(target=lambda: result.extend(self.sessions.release_idle_conversations(now=10_000.0)))
            worker.start(); worker.join(5)
        self.assertEqual(result, [])
        self.assertIn(threads[0], self.sessions.loaded)
        self.assertEqual(self.sessions.release_idle_conversations(now=10_000.0), [threads[0]])

    def test_prewarm_starts_only_idle_claude_conversations(self):
        codex = self.create()
        self.assertEqual(self.sessions.prewarm(codex), {"started": False})

    def test_concurrent_thread_list_reads_share_one_build(self):
        self.create()
        builds, release = [], threading.Event()
        original = self.sessions.list_work_threads
        def slow_list(**options):
            builds.append(options)
            release.wait(5)
            return original(**options)
        self.sessions.list_work_threads = slow_list
        with ThreadPoolExecutor(6) as pool:
            results = [pool.submit(self.sessions.work_thread_inventory) for _ in range(6)]
            while not builds:
                threading.Event().wait(.01)
            release.set()
            bodies = {future.result(5) for future in results}
        self.assertEqual(len(builds), 1)
        self.assertEqual(len(bodies), 1)
        self.assertEqual(len(json.loads(bodies.pop())["data"]), 1)

    def test_thread_list_reuse_ends_at_changes_and_after_its_window(self):
        first = self.create()
        builds = []
        original = self.sessions.list_work_threads
        self.sessions.list_work_threads = lambda **options: builds.append(options) or original(**options)
        self.sessions.work_thread_inventory()
        self.sessions._publish("item/agentMessage/delta", {"threadId": first, "delta": "x"})
        self.sessions._publish("thread/tokenUsage/updated", {"threadId": first})
        self.sessions.work_thread_inventory()
        self.assertEqual(len(builds), 1, "conversation-only events keep the shared list")
        self.sessions.work_thread_inventory(archived=True)
        self.assertEqual(len(builds), 2, "open and closed lists are separate")
        self.sessions._publish("turn/completed", {"threadId": first})
        self.sessions.work_thread_inventory()
        self.assertEqual(len(builds), 3)
        self.sessions.inventory_changed()
        self.sessions.work_thread_inventory()
        self.assertEqual(len(builds), 4)
        with patch("agent_coord.codex_app_server.INVENTORY_REUSE_SECONDS", 0):
            self.sessions.work_thread_inventory()
        self.assertEqual(len(builds), 5, "external writes appear once reuse expires")

    def test_conversation_projections_share_creators_but_decisions_read_current_ones(self):
        thread_id = self.create()
        scans = []
        with patch("agent_coord.codex_app_server.app_agent_creators", side_effect=lambda db: scans.append(1) or {}):
            for _ in range(3):
                self.sessions.work_thread(thread_id)
            self.assertEqual(len(scans), 1, "opening a conversation repeatedly scans app agents once")
            self.sessions._app_agent_creators()
            self.assertEqual(len(scans), 2, "an authoritative read is never shared")
            self.sessions.inventory_changed()
            self.sessions.work_thread(thread_id)
            self.assertEqual(len(scans), 3)

    def test_failed_thread_list_build_is_not_reused(self):
        self.create()
        original, failures = self.sessions.list_work_threads, [CoordinationError("busy")]
        def flaky(**options):
            if failures:
                raise failures.pop()
            return original(**options)
        self.sessions.list_work_threads = flaky
        with self.assertRaisesRegex(CoordinationError, "busy"):
            self.sessions.work_thread_inventory()
        self.assertEqual(len(json.loads(self.sessions.work_thread_inventory())["data"]), 1)

    def test_usage_snapshot_replay_is_durable_and_emits_detail_event(self):
        thread_id = self.create()
        self.usage_event(thread_id, 100)
        self.usage_event(thread_id, 100)
        self.usage_event(thread_id, 200)
        summary = CoordinationStore(self.store.database_path).usage.summary(thread_id)
        self.assertEqual(summary["totals"]["total_tokens"], 200)
        self.assertEqual(summary["context"], {"used_tokens": 20, "limit_tokens": 1000})
        self.assertEqual(summary["models"], [])  # Current setting isn't historical attribution.
        self.assertEqual(self.sessions.work_thread(thread_id)["usage"], summary)
        emitted = [event for event in self.sessions.events if event["method"] == "thread/usage/updated"]
        self.assertEqual(emitted[-1]["params"]["usage"], summary)

    def test_usage_failure_never_breaks_turn_completion(self):
        thread_id = self.create()
        self.sessions.send(thread_id, {"message": "Continue"})
        with patch.object(self.store.usage, "record_snapshot", side_effect=ValueError("invalid telemetry")):
            with self.assertLogs("agent_coord.browser_persistence", level="ERROR"):
                self.usage_event(thread_id, 100)
        self.sessions.interrupt(thread_id)
        self.assertNotIn(thread_id, self.sessions.active)
        self.assertFalse(self.store.get_session(thread_id)["turn_active"])

    def test_usage_busy_retry_does_not_replay_provider_work(self):
        thread_id = self.create()
        original = self.store.usage.record_snapshot
        calls = []
        def busy_once(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise sqlite3.OperationalError("database is locked")
            return original(*args, **kwargs)
        with patch.object(self.store.usage, "record_snapshot", side_effect=busy_once):
            self.usage_event(thread_id, 100)
            self.usage_event(thread_id, 200)
            self.assertTrue(self.sessions.persistence.flush(timeout=3))
        self.assertEqual(self.store.usage.summary(thread_id)["totals"]["total_tokens"], 200)
        self.assertFalse(any(method == "turn/start" for method, _ in self.sessions.rpc.calls))

    def test_usage_fork_uses_frozen_raw_baseline_not_parent_later_total(self):
        parent = self.create()
        self.usage_event(parent, 1000)
        child = self.sessions.fork_work_thread(parent)["thread_id"]
        self.usage_event(parent, 2000)
        self.usage_event(child, 1100, sessionId=parent)
        self.assertEqual(self.store.usage.summary(child)["totals"]["total_tokens"], 100)
        self.assertEqual(self.store.usage.summary(parent)["totals"]["total_tokens"], 2000)
        # A nested fork inherits the child's raw 1100, not its attributed 100.
        grandchild = self.sessions.fork_work_thread(child)["thread_id"]
        self.usage_event(grandchild, 1140)
        self.assertEqual(self.store.usage.summary(grandchild)["totals"]["total_tokens"], 40)
        self.assertEqual(CoordinationStore(self.store.database_path).usage.summary(child)["totals"]["total_tokens"], 100)

    def test_fork_waits_for_deferred_final_usage_before_freezing_baseline(self):
        parent = self.create()
        self.sessions.rpc.fast_turn = True
        self.sessions.send(parent, {"message": "Finish before the fork"})
        self.usage_event(parent, 1000)
        original = self.store.usage.record_snapshot
        release = threading.Event()
        flush_entered = threading.Event()
        original_flush = self.sessions.persistence.flush

        def deferred(*args, **kwargs):
            if not release.is_set():
                raise sqlite3.OperationalError("database is locked")
            return original(*args, **kwargs)

        def flush(*args, **kwargs):
            flush_entered.set()
            return original_flush(*args, **kwargs)

        with patch.object(self.store.usage, "record_snapshot", side_effect=deferred):
            self.usage_event(parent, 2000)
            self.assertEqual(self.store.usage.raw_snapshot(parent, provider="codex", native_id=parent)["total_tokens"], 1000)
            try:
                with patch.object(self.sessions.persistence, "flush", side_effect=flush):
                    with ThreadPoolExecutor(max_workers=1) as executor:
                        fork = executor.submit(self.sessions.fork_work_thread, parent)
                        self.assertTrue(flush_entered.wait(timeout=2))
                        release.set()
                        child = fork.result(timeout=3)["thread_id"]
            finally:
                release.set()
        self.usage_event(child, 2100)
        self.assertEqual(self.store.usage.summary(child)["totals"]["total_tokens"], 100)
        self.assertEqual(self.store.usage.summary(parent)["totals"]["total_tokens"], 2000)

    def test_fork_leaves_baseline_unknown_if_deferred_usage_cannot_drain(self):
        parent = self.create()
        self.usage_event(parent, 1000)
        original = self.store.usage.record_snapshot
        release = threading.Event()
        original_flush = self.sessions.persistence.flush

        def deferred(*args, **kwargs):
            if not release.is_set():
                raise sqlite3.OperationalError("database is locked")
            return original(*args, **kwargs)

        def short_wait(*, timeout):
            self.assertEqual(timeout, 1)
            return original_flush(timeout=0.02)

        with patch.object(self.store.usage, "record_snapshot", side_effect=deferred):
            self.usage_event(parent, 2000)
            try:
                with patch.object(self.sessions.persistence, "flush", side_effect=short_wait):
                    child = self.sessions.fork_work_thread(parent)["thread_id"]
                # The native fork still succeeds, but stale 1000 cannot become
                # an immutable baseline while final 2000 remains deferred.
                with self.store._connection() as db:
                    baseline = db.execute("SELECT totals_json FROM thread_usage_baselines WHERE thread_id=?", (child,)).fetchone()
                self.assertIsNone(baseline["totals_json"])
            finally:
                release.set()
            self.assertTrue(original_flush(timeout=3))
        self.usage_event(child, 2100)
        self.assertEqual(self.store.usage.summary(parent)["totals"]["total_tokens"], 2000)
        self.assertEqual(self.store.usage.summary(child)["status"], "unavailable")
        self.assertEqual(self.store.usage.summary(child)["totals"], {})

    def test_overview_never_queries_usage_or_transcripts_detail_recovers_once(self):
        thread_id = self.create()
        with patch.object(self.store.usage, "summary", wraps=self.store.usage.summary) as summary:
            with patch.object(self.store.usage, "backfill", wraps=self.store.usage.backfill) as backfill:
                self.sessions.list_work_threads()
                summary.assert_not_called()
                backfill.assert_not_called()
                self.sessions.work_thread(thread_id)
                self.sessions.work_thread(thread_id)
                self.assertEqual(backfill.call_count, 1)
        # Durable unavailable note prevents the next process from rescanning.
        other = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(other.close)
        with patch.object(self.store.usage, "backfill") as backfill:
            other.work_thread(thread_id)
            backfill.assert_not_called()

    def test_resolved_defaults_are_visible_and_restored(self):
        thread_id = self.create()
        record = self.sessions.read(thread_id)["session"]
        self.assertEqual((record["model"], record["effort"]), ("available-model", "medium"))
        self.assertFalse(record["yolo"])
        # Old records with no saved settings get Codex's effective values on resume.
        with self.store._connection() as db:
            db.execute("UPDATE browser_sessions SET model = NULL, effort = NULL WHERE thread_id = ?", (thread_id,))
        self.sessions.loaded.discard(thread_id)
        record = self.sessions.read(thread_id)["session"]
        self.assertEqual((record["model"], record["effort"]), ("available-model", "medium"))

    def test_fork_copies_history_and_settings_but_starts_independent_state(self):
        parent = self.create(yolo=True, model="available-model", effort="high")
        self.sessions.rpc.fast_turn = True
        self.sessions.send(parent, {"message": "Explore the original approach"})
        project = self.store.threads.organization.create_project("Fork project")
        self.store.threads.update(parent, attention="later", pinned=True, project_id=project["id"], repository_id=None)
        self.store.threads.checkpoint(parent, {"phase": "planning", "summary": "Original plan",
                                             "next_action": "Review", "next_actor": "user"})
        self.store.begin_work(session_id=parent, scopes=["src/**"])
        before = self.store.threads.get(parent, history=True)
        original_history = copy.deepcopy(self.sessions.rpc.threads[parent])
        fork = self.sessions.fork_work_thread(parent)
        child = fork["thread_id"]
        self.assertNotEqual(child, parent)
        self.assertEqual(fork["forked_from"], {"thread_id": parent, "title": before["title"]})
        self.assertEqual(fork["forked_from_turn_id"], "turn-0")
        self.assertEqual(fork["project_id"], project["id"])
        self.assertEqual(fork["project_name"], "Fork project")
        self.assertIsNone(fork["repository_id"])
        self.assertEqual(fork["attention"], "now")
        self.assertFalse(fork["pinned"])
        self.assertIsNone(fork["checkpoint"])
        self.assertIsNone(fork["turn_completion"])
        self.assertFalse(fork["unread"])
        self.assertEqual(fork["original_request"], before["original_request"])
        self.assertEqual(self.store.get_session(child)["write_scope"], [])
        self.assertIsNone(self.store.get_session(child)["bead_id"])
        self.assertEqual(self.sessions.queue.list(child), [])
        self.assertEqual(self.sessions.pending_requests(child), [])
        self.assertEqual(self.store.threads.get(parent, history=True), before)
        self.assertEqual(self.sessions.rpc.threads[parent], original_history)
        self.assertEqual({t["thread_id"] for t in self.sessions.list_work_threads()}, {parent, child})
        detail = self.sessions.read(child)
        self.assertEqual(detail["thread"]["turns"], original_history["turns"])
        self.assertEqual((detail["session"]["model"], detail["session"]["effort"], detail["session"]["yolo"]),
                         ("available-model", "high", 1))
        resume = next(p for m, p in self.sessions.rpc.calls if m == "thread/resume")
        self.assertEqual(resume["threadId"], child)
        self.assertIn("agent-coord checkpoint --json", resume["developerInstructions"])
        self.assertNotIn("checkpoint --session-id " + parent, resume["developerInstructions"])
        self.assertEqual(resume["sandbox"], "danger-full-access")
        self.assertEqual(sum(m == "turn/start" for m, _ in self.sessions.rpc.calls), 1)
        self.sessions.send(child, {"message": "Try another approach"})
        self.assertEqual(self.sessions.rpc.threads[parent], original_history)
        self.assertEqual(self.store.threads.get(parent, history=True), before)

    def test_fork_survives_restart_and_resume_failure_without_another_fork(self):
        parent = self.create()
        group = self.store.threads.organization.create_project("Source group")
        self.store.threads.update(parent, project_id=group["id"])
        self.sessions.rpc.fast_turn = True
        self.sessions.send(parent, {"message": "Original"})
        child = self.sessions.fork_work_thread(parent)["thread_id"]
        with patch.object(self.sessions.rpc, "request", side_effect=CoordinationError("Connection lost")):
            with self.assertRaisesRegex(CoordinationError, "Connection lost"):
                self.sessions.read(child)
        self.sessions.close()
        reopened = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(reopened.close)
        reopened.rpc.threads = self.sessions.rpc.threads
        detail = reopened.read(child)
        self.assertEqual(detail["work_thread"]["forked_from_thread_id"], parent)
        self.assertEqual(detail["work_thread"]["project_id"], group["id"])
        self.assertEqual(len(detail["thread"]["turns"]), 1)
        self.assertFalse(detail["work_thread"]["unread"])
        self.assertFalse(any(m == "thread/fork" for m, _ in reopened.rpc.calls))
        grandchild = reopened.fork_work_thread(child)
        self.assertEqual(grandchild["project_id"], group["id"])
        self.assertEqual(grandchild["forked_from"]["thread_id"], child)
        self.store.threads.update(child, title="Renamed fork", attention="archived")
        forks = reopened.work_thread(parent)["forked_sessions"]
        self.assertEqual([(f["thread_id"], f["title"], f["forked_from_turn_id"]) for f in forks],
                         [(child, "Renamed fork", "turn-0")])
        self.assertEqual(reopened.work_thread(child)["forked_sessions"][0]["thread_id"], grandchild["thread_id"])
        listed = {t["thread_id"]: t for t in reopened.list_work_threads()}
        self.assertEqual(listed[parent]["forked_sessions"], forks)
        self.store.threads.update(parent, project_id=None)
        self.assertEqual(reopened.work_thread(child)["project_id"], group["id"])

    def test_fork_without_group_remains_ungrouped(self):
        parent = self.create()
        child = self.sessions.fork_work_thread(parent)["thread_id"]
        self.assertIsNone(self.sessions.read(child)["work_thread"]["project_id"])

    def test_fork_rejects_busy_closed_claude_and_online_terminal_sources(self):
        parent = self.create()
        self.sessions.send(parent, {"message": "Working"})
        self.assertFalse(self.sessions.work_thread(parent)["can_fork"])
        with self.assertRaises(BrowserBusyError):
            self.sessions.fork_work_thread(parent)
        self.sessions.interrupt(parent)
        self.sessions.requests["pending"] = {"params": {"threadId": parent}}
        with self.assertRaises(BrowserBusyError):
            self.sessions.fork_work_thread(parent)
        self.sessions.requests.clear()
        self.store.threads.update(parent, attention="archived")
        with self.assertRaises(CoordinationError):
            self.sessions.fork_work_thread(parent)
        for client in ("claude", "codex"):
            self.store.register(session_id=client, client=client, cwd=str(self.root))
            with self.assertRaises(CoordinationError):
                self.sessions.fork_work_thread(client)
        self.assertFalse(any(m == "thread/fork" for m, _ in self.sessions.rpc.calls))

    def test_fork_rechecks_native_activity_and_rpc_failure_leaves_parent_intact(self):
        parent = self.create()
        before = self.store.threads.get(parent, history=True)
        self.sessions.rpc.threads[parent]["status"] = {"type": "active"}
        with self.assertRaises(BrowserBusyError):
            self.sessions.fork_work_thread(parent)
        self.sessions.rpc.threads[parent]["status"] = {"type": "idle"}
        request = self.sessions.rpc.request
        def fail_fork(method, params=None):
            if method == "thread/fork":
                raise CoordinationError("Fork unavailable")
            return request(method, params)
        with patch.object(self.sessions.rpc, "request", side_effect=fail_fork):
            with self.assertRaisesRegex(CoordinationError, "Fork unavailable"):
                self.sessions.fork_work_thread(parent)
        self.assertEqual([t["thread_id"] for t in self.sessions.list_work_threads()], [parent])
        self.assertEqual(self.store.threads.get(parent, history=True), before)

    def test_saved_terminal_can_fork_without_resuming_parent(self):
        self.store.register(session_id="terminal", client="codex", cwd=str(self.root))
        self.store.end_session("terminal")
        self.sessions.rpc.threads["terminal"] = {"id": "terminal", "turns": []}
        child = self.sessions.fork_work_thread("terminal")["thread_id"]
        self.assertTrue(self.sessions.work_thread(child)["browser_session"])
        self.assertFalse(self.sessions.work_thread("terminal")["browser_session"])
        self.assertEqual(self.store.get_session("terminal")["presence"], "offline")
        self.assertFalse(any(m == "thread/resume" for m, _ in self.sessions.rpc.calls))
        params = next(p for m, p in self.sessions.rpc.calls if m == "thread/fork")
        self.assertEqual(params["sandbox"], "workspace-write")
        self.assertEqual(params["approvalPolicy"], "on-request")

    def test_fork_is_workspace_scoped(self):
        parent = self.create()
        subdir = self.root / "subdir"
        subdir.mkdir()
        filtered = BrowserSessions(self.store, str(subdir), rpc_factory=FakeCodex)
        self.addCleanup(filtered.close)
        with self.assertRaisesRegex(CoordinationError, "workspace"):
            filtered.fork_work_thread(parent)
        self.assertEqual(filtered.rpc.calls, [])

    def test_slash_commands_change_next_turn_without_sending_prompt(self):
        thread_id = self.create()
        self.assertIn("other-model", self.sessions.send(thread_id, {"message": "/model"})["command"]["message"])
        self.assertIn("high", self.sessions.send(thread_id, {"message": "/effort"})["command"]["message"])
        self.assertIn("/model", self.sessions.send(thread_id, {"message": "/help"})["command"]["message"])
        self.sessions.send(thread_id, {"message": "/effort high"})
        switched = self.sessions.send(thread_id, {"message": "/model other-model"})
        self.assertEqual((switched["session"]["model"], switched["session"]["effort"]), ("other-model", "low"))
        self.assertFalse(any(method == "turn/start" for method, _ in self.sessions.rpc.calls))
        self.assertFalse(self.store.threads.get(thread_id)["original_request"])
        self.sessions.send(thread_id, {"message": "Real prompt"})
        turn = self.sessions.rpc.calls[-1][1]
        self.assertEqual((turn["model"], turn["effort"]), ("other-model", "low"))
        self.assertEqual(turn["input"][0]["text"], "Real prompt")

    def test_model_settings_persist_across_restart_and_reject_invalid_or_busy_changes(self):
        thread_id = self.create()
        for message in ("/model missing", "/effort ultra", "/model other-model high", "/effort high extra", "/model other-model low extra"):
            with self.assertRaises(CoordinationError):
                self.sessions.send(thread_id, {"message": message})
        self.assertEqual(self.sessions._record(thread_id)["model"], "available-model")
        self.sessions.send(thread_id, {"message": "/model other-model low"})
        self.sessions.close()
        reopened = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(reopened.close)
        reopened.rpc.threads = self.sessions.rpc.threads
        record = reopened.read(thread_id)["session"]
        self.assertEqual((record["model"], record["effort"]), ("other-model", "low"))
        reopened.send(thread_id, {"message": "Start work"})
        with self.assertRaises(BrowserBusyError):
            reopened.send(thread_id, {"message": "/model available-model"})
        with self.assertRaises(BrowserBusyError):
            reopened.update(thread_id, {"effort": "low"})
        self.assertEqual(reopened._record(thread_id)["model"], "other-model")

    def test_speed_commands_toggle_without_prompt_and_apply_to_turns(self):
        thread_id = self.create()
        for command, tier in [("/fast", "fast"), ("/fast", "default"),
                              ("/ultrafast on", "ultrafast"), ("/fast on", "fast"),
                              ("/utrafast", "ultrafast"), ("/ultrafast off", "default")]:
            result = self.sessions.send(thread_id, {"message": command})
            self.assertEqual(result["session"]["service_tier"], tier)
            self.assertIn(tier, result["command"]["message"])
        self.assertFalse(any(method == "turn/start" for method, _ in self.sessions.rpc.calls))
        self.assertFalse(self.store.threads.get(thread_id)["original_request"])
        self.sessions.send(thread_id, {"message": "/fast on"})
        result = self.sessions.send(thread_id, {"message": "/ultrafast status"})
        self.assertEqual(result["session"]["service_tier"], "fast")
        self.sessions.update(thread_id, {"service_tier": None})
        self.assertEqual(self.sessions._record(thread_id)["service_tier"], "default")
        self.sessions.send(thread_id, {"message": "/fast on"})
        self.sessions.send(thread_id, {"message": "Work"})
        self.assertEqual(self.sessions.rpc.calls[-1][1]["serviceTier"], "fast")
        with self.assertRaises(BrowserBusyError):
            self.sessions.send(thread_id, {"message": "/ultrafast"})
        self.assertEqual(self.sessions._record(thread_id)["service_tier"], "fast")

    def test_speed_settings_survive_restart_fork_and_explicit_disable(self):
        thread_id = self.create(service_tier="ultrafast")
        self.assertEqual(self.sessions.rpc.calls[-1][1].get("serviceTier"), "ultrafast")
        self.sessions.close()
        reopened = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(reopened.close)
        reopened.rpc.threads = self.sessions.rpc.threads
        record = reopened.read(thread_id)["session"]
        self.assertEqual(record["service_tier"], "ultrafast")
        resume = next(params for method, params in reopened.rpc.calls if method == "thread/resume")
        self.assertEqual(resume["serviceTier"], "ultrafast")
        child = reopened.fork_work_thread(thread_id)["thread_id"]
        self.assertEqual(reopened._record(child)["service_tier"], "ultrafast")
        reopened.send(thread_id, {"message": "/fast on"})
        fast_child = reopened.fork_work_thread(thread_id)["thread_id"]
        self.assertEqual(reopened._record(fast_child)["service_tier"], "fast")
        reopened.send(fast_child, {"message": "/fast"})
        self.assertEqual(reopened._record(fast_child)["service_tier"], "default")
        reopened.send(child, {"message": "/fast off"})
        reopened.send(child, {"message": "Work"})
        self.assertEqual(reopened.rpc.calls[-1][1]["serviceTier"], "default")
        self.assertEqual(reopened._record(thread_id)["service_tier"], "fast")

    def test_speed_modes_reject_invalid_values_and_other_providers(self):
        thread_id = self.create()
        for command in ("/fast yes", "/ultrafast on extra", "/utrafast invalid"):
            with self.assertRaises(CoordinationError):
                self.sessions.send(thread_id, {"message": command})
        for tier in ("invalid", True, [], 5):
            with self.assertRaisesRegex(CoordinationError, "Service tier"):
                self.sessions.update(thread_id, {"service_tier": tier})
            with self.assertRaises(CoordinationError):
                self.create(service_tier=tier)
        with self.store._connection() as db:
            db.execute("UPDATE browser_sessions SET client='claude' WHERE thread_id=?", (thread_id,))
        for command in ("/fast", "/ultrafast", "/utrafast"):
            with self.assertRaisesRegex(CoordinationError, "only available for Codex"):
                self.sessions._command(thread_id, command)
        with self.assertRaisesRegex(CoordinationError, "only available for Codex"):
            self.sessions._creation_settings({"cwd": str(self.root), "client": "claude", "service_tier": "fast"})

    def test_unavailable_speed_modes_never_confirm_or_save_a_silent_fallback(self):
        thread_id = self.create()
        catalog = [{"model": "available-model", "isDefault": True,
                    "serviceTiers": [{"id": "priority"}], "additionalSpeedTiers": ["fast"],
                    "supportedReasoningEfforts": [{"reasoningEffort": "medium"}]}]
        with patch.object(self.sessions, "models", return_value=catalog):
            self.sessions.send(thread_id, {"message": "/fast on"})
            for command in ("/ultrafast", "/utrafast on"):
                with self.assertRaisesRegex(CoordinationError, "does not advertise ultrafast"):
                    self.sessions.send(thread_id, {"message": command})
            self.assertEqual(self.sessions._record(thread_id)["service_tier"], "fast")
            with self.assertRaisesRegex(CoordinationError, "does not advertise ultrafast"):
                self.create(service_tier="ultrafast")
        self.assertFalse(any(method == "turn/start" for method, _ in self.sessions.rpc.calls))

    def test_yolo_is_explicit_persists_and_applies_to_start_resume_and_turn(self):
        for invalid in ("true", 1, None):
            with self.assertRaisesRegex(CoordinationError, "YOLO"):
                self.create(yolo=invalid)
        thread_id = self.create(yolo=True)
        options = next(params for method, params in reversed(self.sessions.rpc.calls) if method == "thread/start")
        self.assertEqual(options["approvalPolicy"], "never")
        self.assertEqual(options["sandbox"], "danger-full-access")
        self.sessions.close()
        reopened = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(reopened.close)
        reopened.rpc.threads = self.sessions.rpc.threads
        self.assertTrue(reopened.read(thread_id)["session"]["yolo"])
        options = next(params for method, params in reopened.rpc.calls if method == "thread/resume")
        self.assertEqual(options["approvalPolicy"], "never")
        self.assertEqual(options["sandbox"], "danger-full-access")
        reopened.send(thread_id, {"message": "Work"})
        options = reopened.rpc.calls[-1][1]
        self.assertEqual(options["approvalPolicy"], "never")
        self.assertEqual(options["sandboxPolicy"], {"type": "dangerFullAccess"})
        with self.assertRaises(BrowserBusyError):
            reopened.update(thread_id, {"yolo": False})
        reopened.interrupt(thread_id)
        self.assertFalse(reopened.update(thread_id, {"yolo": False})["yolo"])
        reopened.send(thread_id, {"message": "Safer work"})
        options = reopened.rpc.calls[-1][1]
        self.assertEqual(options["approvalPolicy"], "on-request")
        self.assertEqual(options["sandboxPolicy"], {
            "type": "workspaceWrite", "writableRoots": [str(self.root)], "networkAccess": False,
        })

    def test_yolo_can_be_enabled_on_an_existing_idle_session(self):
        thread_id = self.create()
        for invalid in ("true", 1, None):
            with self.assertRaisesRegex(CoordinationError, "YOLO"):
                self.sessions.update(thread_id, {"yolo": invalid})
        self.assertFalse(self.sessions._record(thread_id)["yolo"])
        self.assertTrue(self.sessions.update(thread_id, {"yolo": True})["yolo"])
        self.sessions.rpc.fast_turn = True
        self.sessions.send(thread_id, {"message": "Work"})
        options = self.sessions.rpc.calls[-1][1]
        self.assertEqual(options["approvalPolicy"], "never")
        self.assertEqual(options["sandboxPolicy"], {"type": "dangerFullAccess"})
        self.sessions.close()
        reopened = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(reopened.close)
        reopened.rpc.threads = self.sessions.rpc.threads
        self.assertTrue(reopened.read(thread_id)["session"]["yolo"])
        options = next(params for method, params in reopened.rpc.calls if method == "thread/resume")
        self.assertEqual(options["approvalPolicy"], "never")
        self.assertEqual(options["sandbox"], "danger-full-access")
        reopened.update(thread_id, {"archived": True})
        with self.assertRaisesRegex(CoordinationError, "Restore"):
            reopened.update(thread_id, {"yolo": False})

    def test_existing_database_migrates_yolo_to_off(self):
        self.sessions.close()
        with self.store._connection() as db:
            db.execute("DROP TABLE browser_sessions")
            db.execute("CREATE TABLE browser_sessions (thread_id TEXT PRIMARY KEY, cwd TEXT NOT NULL, name TEXT NOT NULL, model TEXT, effort TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL, archived INTEGER NOT NULL DEFAULT 0)")
        for _ in range(2):
            reopened = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
            try:
                tid = reopened.create({"name": "Migrated"})["session"]["thread_id"]
                self.assertFalse(reopened._record(tid)["yolo"])
                self.assertIsNone(reopened._record(tid)["service_tier"])
            finally:
                reopened.close()
            with self.store._connection() as db:
                db.execute("DELETE FROM browser_sessions")

    def test_independent_session_registers_without_bead_and_preserves_settings(self):
        thread_id = self.create(model="available-model", effort="high")
        session = self.store.get_session(thread_id)
        self.assertIsNone(session["bead_id"])
        self.assertEqual(session["write_scope"], [])
        self.assertEqual(session["name"], "Feature discussion")
        options = self.sessions.rpc.calls[-1][1]
        self.assertEqual(options["approvalPolicy"], "on-request")
        self.assertEqual(options["approvalsReviewer"], "user")
        self.assertEqual(options["sandbox"], "workspace-write")
        self.assertEqual(options["config"]["model_reasoning_effort"], "high")

    def test_history_and_names_survive_service_restart(self):
        thread_id = self.create()
        self.sessions.rpc.fast_turn = True
        self.sessions.send(thread_id, {"message": "Continue the project."})
        self.sessions.update(thread_id, {"name": "Project"})
        self.sessions.close()
        reopened = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(reopened.close)
        reopened.rpc.threads = self.sessions.rpc.threads
        self.assertEqual(reopened.list_sessions()[0]["status"], "saved")
        result = reopened.read(thread_id)
        self.assertEqual(result["session"]["name"], "Project")
        self.assertEqual(result["thread"]["turns"][0]["items"][0]["content"][0]["text"], "Continue the project.")
        self.assertEqual(reopened.rpc.calls[0][0], "thread/resume")

    def test_parallel_sessions_steer_without_starting_duplicate_turns(self):
        first, second = self.create(), self.create()
        self.sessions.send(first, {"message": "First"})
        self.sessions.send(second, {"message": "Second"})
        result = self.sessions.send(first, {"message": "Focus on the UI"})
        self.assertEqual(result, {"turnId": self.sessions.active[first]})
        self.assertEqual(len(self.sessions.rpc.threads[first]["turns"]), 1)
        self.assertEqual(len(self.sessions.active), 2)
        self.sessions.interrupt(first)
        self.assertNotIn(first, self.sessions.active)
        self.assertIn(second, self.sessions.active)

    def test_stop_targets_latest_turn_despite_old_in_progress_history(self):
        thread_id = self.create()
        turn = self.sessions.send(thread_id, {"message": "Current work"}, start_only=True)["turn"]
        self.sessions.rpc.threads[thread_id]["turns"].insert(0, {"id": "stale", "status": "inProgress", "items": []})
        self.assertEqual(self.sessions.read(thread_id)["activeTurn"], turn["id"])
        self.sessions.interrupt(thread_id)
        self.assertEqual(self.sessions.rpc.calls[-1], ("turn/interrupt", {"threadId": thread_id, "turnId": turn["id"]}))
        self.assertFalse(self.sessions.read(thread_id)["running"])

    def test_old_in_progress_history_does_not_resurrect_after_latest_completion(self):
        thread_id = self.create()
        self.sessions.rpc.threads[thread_id]["turns"] = [
            {"id": "stale", "status": "inProgress", "items": []},
            {"id": "finished", "status": "completed", "items": []},
        ]
        self.assertFalse(self.sessions.read(thread_id)["running"])
        self.sessions.rpc.fast_turn = True
        self.sessions.send(thread_id, {"message": "Next work"}, start_only=True)
        self.assertEqual(len(self.sessions.rpc.threads[thread_id]["turns"]), 3)

    def test_stop_uses_current_turn_with_separate_stored_history_reader(self):
        thread_id = self.create()
        turn = self.sessions.send(thread_id, {"message": "Current work"})["turn"]
        self.sessions.rpc.no_history = True
        self.history_reader(thread_id, [
            {"id": "stale", "status": "inProgress", "items": []}, turn,
        ])
        self.assertEqual(self.sessions.read(thread_id)["activeTurn"], turn["id"])
        self.sessions.interrupt(thread_id)
        self.assertFalse(self.sessions.read(thread_id)["running"])

    def test_read_keeps_new_turn_started_while_history_response_is_in_flight(self):
        thread_id = self.create()
        old = self.sessions.send(thread_id, {"message": "Old work"})["turn"]
        original = self.sessions.rpc.request

        def request(method, params=None):
            result = original(method, params)
            if method == "thread/read":
                original("turn/interrupt", {"threadId": thread_id, "turnId": old["id"]})
                original("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": "New work"}]})
            return result

        with patch.object(self.sessions.rpc, "request", side_effect=request):
            detail = self.sessions.read(thread_id)
        current = self.sessions.rpc.threads[thread_id]["turns"][-1]["id"]
        self.assertEqual(detail["activeTurn"], current)
        self.sessions.interrupt(thread_id)

    def test_read_does_not_resurrect_turn_completed_during_history_request(self):
        thread_id = self.create()
        turn = self.sessions.send(thread_id, {"message": "Work"})["turn"]
        original = self.sessions.rpc.request

        def request(method, params=None):
            result = original(method, params)
            if method == "thread/read":
                original("turn/interrupt", {"threadId": thread_id, "turnId": turn["id"]})
            return result

        with patch.object(self.sessions.rpc, "request", side_effect=request):
            self.assertFalse(self.sessions.read(thread_id)["running"])

    def test_incomplete_history_does_not_replace_streamed_active_turn(self):
        thread_id = self.create()
        turn = self.sessions.send(thread_id, {"message": "Current work"})["turn"]
        stale = {"id": thread_id, "status": {"type": "active"}, "turns": [
            {"id": "stale", "status": "inProgress", "items": []},
        ]}
        with patch.object(self.sessions, "_read_thread", return_value=stale):
            self.assertEqual(self.sessions.read(thread_id)["activeTurn"], turn["id"])

    def test_steering_uses_active_turn_and_preserves_history_and_original_request(self):
        thread_id = self.create()
        self.sessions.rpc.no_history = True
        turn = self.sessions.send(thread_id, {"message": "Original request"})["turn"]
        result = self.sessions.send(thread_id, {"message": "New direction", "expectedTurnId": turn["id"]})
        self.assertEqual(result, {"turnId": turn["id"]})
        self.assertEqual(self.sessions.rpc.calls[-1], ("turn/steer", {
            "threadId": thread_id, "expectedTurnId": turn["id"], "input": [{"type": "text", "text": "New direction"}],
        }))
        self.assertEqual(self.store.threads.get(thread_id)["original_request"], "Original request")
        detail = self.sessions.read(thread_id)
        self.assertTrue(detail["running"])
        self.assertEqual(len(detail["thread"]["turns"]), 1)
        self.assertEqual(detail["thread"]["turns"][0]["items"][-1]["content"][0]["text"], "New direction")
        with self.store._connection() as db:
            history = json.loads(db.execute("SELECT history_json FROM browser_history WHERE thread_id = ?", (thread_id,)).fetchone()[0])
        self.assertEqual(history["turns"][0]["items"][-1]["content"][0]["text"], "New direction")

    def test_stale_steering_does_not_start_or_target_another_turn(self):
        thread_id = self.create()
        turn = self.sessions.send(thread_id, {"message": "Start"})["turn"]
        self.sessions.interrupt(thread_id)
        with self.assertRaisesRegex(BrowserBusyError, "changed or finished"):
            self.sessions.send(thread_id, {"message": "Too late", "expectedTurnId": turn["id"]})
        self.assertEqual(len(self.sessions.rpc.threads[thread_id]["turns"]), 1)
        self.sessions.send(thread_id, {"message": "Next turn"})
        with self.assertRaisesRegex(BrowserBusyError, "changed or finished"):
            self.sessions.send(thread_id, {"message": "Wrong turn", "expectedTurnId": turn["id"]})
        self.assertFalse(any(method == "turn/steer" for method, _ in self.sessions.rpc.calls))

    def test_failed_steering_preserves_active_turn_and_can_retry(self):
        thread_id = self.create()
        turn = self.sessions.send(thread_id, {"message": "Start"})["turn"]
        self.sessions.rpc.fail_steer = True
        with self.assertRaisesRegex(CoordinationError, "Steering unavailable"):
            self.sessions.send(thread_id, {"message": "Adjust"})
        self.assertEqual(self.sessions.active[thread_id], turn["id"])
        self.assertEqual(len(self.sessions.rpc.threads[thread_id]["turns"][0]["items"]), 1)
        self.sessions.rpc.fail_steer = False
        self.assertEqual(self.sessions.send(thread_id, {"message": "Adjust"}), {"turnId": turn["id"]})

    def test_steering_rejects_invalid_expected_turn_ids(self):
        thread_id = self.create()
        for value in (None, "", [], 1):
            with self.assertRaisesRegex(CoordinationError, "Expected turn ID"):
                self.sessions.send(thread_id, {"message": "Adjust", "expectedTurnId": value})
        self.assertFalse(any(method in {"turn/start", "turn/steer"} for method, _ in self.sessions.rpc.calls))

    def test_completion_during_steering_does_not_restart_the_turn(self):
        thread_id = self.create()
        turn = self.sessions.send(thread_id, {"message": "Start"})["turn"]
        request = self.sessions.rpc.request

        def complete_before_steer(method, params=None):
            if method == "turn/steer":
                request("turn/interrupt", {"threadId": thread_id, "turnId": turn["id"]})
            return request(method, params)

        self.sessions.rpc.request = complete_before_steer
        with self.assertRaisesRegex(CoordinationError, "Active turn mismatch"):
            self.sessions.send(thread_id, {"message": "Too late", "expectedTurnId": turn["id"]})
        self.assertNotIn(thread_id, self.sessions.active)
        self.assertEqual(len(self.sessions.rpc.threads[thread_id]["turns"]), 1)

    def test_streamed_history_survives_when_codex_cannot_query_stored_turns(self):
        thread_id = self.create()
        self.sessions.rpc.no_history = True
        self.assertEqual(self.sessions.read(thread_id)["thread"]["turns"], [])
        turn = self.sessions.send(thread_id, {"message": "Remember this"})["turn"]
        params = {"threadId": thread_id, "turnId": turn["id"]}
        self.sessions._event({"method": "item/started", "params": {**params, "item": {"id": "answer", "type": "agentMessage", "text": ""}}})
        for text in ["Hello", " again"]:
            self.sessions._event({"method": "item/agentMessage/delta", "params": {**params, "itemId": "answer", "delta": text}})
        self.sessions._event({"method": "turn/completed", "params": {"threadId": thread_id, "turn": {"id": turn["id"], "status": "completed", "items": []}}})
        self.assertEqual(self.sessions.read(thread_id)["thread"]["turns"][0]["items"][-1]["text"], "Hello again")
        self.sessions.close()
        reopened = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(reopened.close)
        reopened.rpc.threads = self.sessions.rpc.threads
        reopened.rpc.no_history = True
        thread = reopened.read(thread_id)["thread"]
        self.assertEqual(thread["turns"][0]["items"][-1]["text"], "Hello again")
        self.assertEqual(thread["turns"][0]["status"], "completed")

    def history_reader(self, thread_id, turns):
        reader = FakeCodex(self.store, lambda event: None)
        reader.threads[thread_id] = {"id": thread_id, "turns": turns, "status": {"type": "notLoaded"}}
        self.sessions.history_rpc = reader
        return reader

    def test_first_message_read_recovers_when_rollout_metadata_is_delayed(self):
        thread_id = self.create()
        turn = self.sessions.send(thread_id, {"message": "First message"})["turn"]
        self.sessions.rpc.empty_rollout_reads = {True: 2}
        with patch("agent_coord.codex_app_server.time.sleep") as sleep:
            result = self.sessions.read(thread_id)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(result["thread"]["turns"], [turn])
        self.assertEqual(result["activeTurn"], turn["id"])
        self.assertTrue(result["running"])
        self.assertFalse(result["thread"]["historyUnavailable"])
        self.assertEqual(sum(method == "turn/start" for method, _ in self.sessions.rpc.calls), 1)

    def test_summary_read_retries_empty_rollout_with_unsupported_loaded_history(self):
        thread_id = self.create()
        turn = self.sessions.send(thread_id, {"message": "First message"})["turn"]
        self.sessions.rpc.no_history = True
        self.sessions.rpc.empty_rollout_reads = {False: 1}
        self.history_reader(thread_id, [turn])
        with patch("agent_coord.codex_app_server.time.sleep") as sleep:
            result = self.sessions.read(thread_id)
        self.assertEqual(sleep.call_count, 1)
        self.assertEqual(result["thread"]["turns"], [turn])
        self.assertTrue(result["running"])
        self.assertFalse(result["thread"]["historyUnavailable"])

    def test_separate_history_reader_retries_delayed_metadata(self):
        thread_id = self.create()
        turn = self.sessions.send(thread_id, {"message": "First message"})["turn"]
        self.sessions.rpc.no_history = True
        reader = self.history_reader(thread_id, [turn])
        reader.empty_rollout_reads = {True: 2}
        with patch("agent_coord.codex_app_server.time.sleep") as sleep:
            result = self.sessions.read(thread_id)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(result["thread"]["turns"], [turn])
        self.assertFalse(result["thread"]["historyUnavailable"])
        self.assertFalse(reader.closed)

    def test_persistently_empty_rollout_still_fails_without_losing_the_active_turn(self):
        thread_id = self.create()
        turn = self.sessions.send(thread_id, {"message": "First message"})["turn"]
        self.sessions.rpc.empty_rollout_reads = {True: 100}
        with patch("agent_coord.codex_app_server.time.sleep") as sleep:
            with self.assertRaises(CoordinationError) as caught:
                self.sessions.read(thread_id)
        self.assertEqual(str(caught.exception), EMPTY_ROLLOUT_ERROR)
        self.assertGreater(sleep.call_count, 0)
        self.assertLessEqual(sum(call.args[0] for call in sleep.call_args_list), 1)
        self.assertEqual(self.sessions.active[thread_id], turn["id"])
        self.assertEqual(self.sessions._history(thread_id)["turns"], [turn])
        self.sessions.rpc.empty_rollout_reads.clear()
        self.assertEqual(self.sessions.read(thread_id)["thread"]["turns"], [turn])
        self.assertEqual(sum(method == "turn/start" for method, _ in self.sessions.rpc.calls), 1)

    def test_thread_read_does_not_retry_unrelated_storage_or_transport_errors(self):
        thread_id = self.create()
        for error in ("Thread not found.", "Connection to Codex app-server was lost.",
                      EMPTY_ROLLOUT_ERROR.replace("is empty", "contains invalid JSON"),
                      "failed to read thread: permission denied"):
            with self.subTest(error=error):
                with patch.object(self.sessions.rpc, "request", side_effect=CoordinationError(error)) as request:
                    with patch("agent_coord.codex_app_server.time.sleep") as sleep:
                        with self.assertRaises(CoordinationError) as caught:
                            self.sessions.read(thread_id)
                self.assertEqual(str(caught.exception), error)
                self.assertEqual(request.call_count, 1)
                sleep.assert_not_called()

    def test_resumed_terminal_history_is_recovered_without_resuming_the_history_reader(self):
        thread_id = "terminal"
        self.store.register(session_id=thread_id, client="codex", cwd=str(self.root))
        self.store.end_session(thread_id)
        turns = [{"id": "old-turn", "status": "completed", "items": [
            {"id": "user", "type": "userMessage", "content": [{"type": "text", "text": "Original request"}]},
            {"id": "answer", "type": "agentMessage", "text": "Saved answer"}]}]
        self.sessions.rpc.threads[thread_id] = {"id": thread_id, "turns": turns, "status": {"type": "idle"}}
        self.sessions.rpc.no_history = True
        reader = self.history_reader(thread_id, turns)
        self.sessions.resume_work_thread(thread_id)
        result = self.sessions.read(thread_id)
        self.assertEqual(result["thread"]["turns"], turns)
        self.assertFalse(result["thread"]["historyUnavailable"])
        self.assertEqual(result["thread"]["status"], {"type": "idle"})
        self.assertEqual(reader.calls, [("thread/read", {"threadId": thread_id, "includeTurns": True})])
        with self.store._connection() as db:
            saved = json.loads(db.execute("SELECT history_json FROM browser_history WHERE thread_id = ?", (thread_id,)).fetchone()[0])
        self.assertEqual(saved["turns"], turns)
        self.sessions.close()
        self.assertTrue(reader.closed)

    def test_one_unsupported_thread_does_not_disable_history_for_other_threads(self):
        first, second = self.create(), self.create()
        self.sessions.rpc.no_history = True
        self.assertTrue(self.sessions.read(first)["thread"]["historyUnavailable"])
        self.sessions.rpc.no_history = False
        turns = [{"id": "stored", "status": "completed", "items": [
            {"id": "answer", "type": "agentMessage", "text": "Other thread history"}]}]
        self.sessions.rpc.threads[second]["turns"] = turns
        result = self.sessions.read(second)["thread"]
        self.assertEqual(result["turns"], turns)
        self.assertFalse(result["historyUnavailable"])

    def test_unchanged_large_history_reads_skip_persistence_and_keep_response_isolated(self):
        thread_id = self.create()
        turns = [{"id": "saved", "status": "completed", "items": [
            {"id": "answer", "type": "agentMessage", "text": "long transcript " * 100000}]}]
        self.sessions.rpc.threads[thread_id].update(turns=turns, status={"type": "idle"})
        self.sessions.read(thread_id)
        with patch.object(self.sessions, "_save_history", wraps=self.sessions._save_history) as save:
            result = self.sessions.read(thread_id)
            self.sessions.read(thread_id)
            save.assert_not_called()
        result["thread"]["turns"][0]["items"][0]["text"] = "caller edit"
        self.assertEqual(self.sessions._history(thread_id)["turns"], turns)
        self.sessions.rpc.threads[thread_id]["turns"][0]["items"][0]["text"] = "changed answer"
        with patch.object(self.sessions, "_save_history", wraps=self.sessions._save_history) as save:
            self.sessions.read(thread_id)
            save.assert_called_once_with(thread_id)
        with self.store._connection() as db:
            persisted = json.loads(db.execute("SELECT history_json FROM browser_history WHERE thread_id = ?", (thread_id,)).fetchone()[0])
        self.assertEqual(persisted["turns"][0]["items"][0]["text"], "changed answer")

    def test_read_flushes_unsaved_stream_deltas_even_when_provider_matches_mirror(self):
        thread_id = self.create()
        turns = [{"id": "live", "status": "inProgress", "items": [
            {"id": "answer", "type": "agentMessage", "text": "Partial"}]}]
        self.sessions.rpc.threads[thread_id].update(turns=turns, status={"type": "active"})
        self.sessions.read(thread_id)
        self.sessions._event({"method": "item/agentMessage/delta", "params": {
            "threadId": thread_id, "turnId": "live", "itemId": "answer", "delta": " update"}})
        self.sessions.rpc.threads[thread_id]["turns"][0]["items"][0]["text"] = "Partial update"
        with patch.object(self.sessions, "_save_history", wraps=self.sessions._save_history) as save:
            self.sessions.read(thread_id)
            save.assert_called_once_with(thread_id)
            self.sessions.read(thread_id)
            save.assert_called_once_with(thread_id)
        with self.store._connection() as db:
            persisted = json.loads(db.execute("SELECT history_json FROM browser_history WHERE thread_id = ?", (thread_id,)).fetchone()[0])
        self.assertEqual(persisted["turns"][0]["items"][0]["text"], "Partial update")

    def test_failed_history_read_keeps_cached_messages_and_can_recover(self):
        thread_id = self.create()
        turns = [{"id": "cached", "status": "completed", "items": [
            {"id": "answer", "type": "agentMessage", "text": "Cached answer"}]}]
        self.sessions._remember_thread({"id": thread_id, "turns": turns})
        self.sessions.rpc.no_history = True
        failed = self.history_reader(thread_id, turns)
        failed.no_history = True
        result = self.sessions.read(thread_id)["thread"]
        self.assertTrue(result["historyUnavailable"])
        self.assertEqual(result["turns"], turns)
        self.assertTrue(failed.closed)
        self.assertIsNone(self.sessions.history_rpc)
        self.assertFalse(self.sessions.rpc.closed)
        reader = self.history_reader(thread_id, turns)
        recovered = self.sessions.read(thread_id)["thread"]
        self.assertFalse(recovered["historyUnavailable"])
        self.assertEqual(recovered["turns"], turns)
        self.assertFalse(reader.closed)

    def test_stored_history_does_not_overwrite_live_deltas_or_running_state(self):
        thread_id = self.create()
        turn = self.sessions.send(thread_id, {"message": "Continue"})["turn"]
        older = {"id": "older", "status": "completed", "items": [
            {"id": "old-answer", "type": "agentMessage", "text": "Before browser resume"}]}
        persisted = copy.deepcopy(turn)
        persisted["items"].append({"id": "answer", "type": "agentMessage", "text": "Partial"})
        self.sessions.rpc.threads[thread_id]["status"] = {"type": "active"}
        self.sessions.rpc.no_history = True
        reader = self.history_reader(thread_id, [older, persisted])
        params = {"threadId": thread_id, "turnId": turn["id"]}
        self.sessions._event({"method": "item/started", "params": {**params, "item": {"id": "answer", "type": "agentMessage", "text": "Partial"}}})
        self.sessions._event({"method": "item/agentMessage/delta", "params": {**params, "itemId": "answer", "delta": " live reply"}})
        result = self.sessions.read(thread_id)
        self.assertEqual([item["id"] for item in result["thread"]["turns"]], ["older", turn["id"]])
        self.assertEqual(result["thread"]["turns"][-1]["items"][-1]["text"], "Partial live reply")
        self.assertEqual(result["activeTurn"], turn["id"])
        self.assertTrue(result["running"])
        self.sessions._event({"method": "turn/completed", "params": {"threadId": thread_id, "turn": {"id": turn["id"], "status": "completed", "items": []}}})
        self.sessions.rpc.threads[thread_id]["status"] = {"type": "idle"}
        # Persisted history still has an in-progress snapshot after completion.
        result = self.sessions.read(thread_id)
        self.assertEqual(result["thread"]["turns"][-1]["status"], "completed")
        self.assertFalse(result["running"])
        self.assertTrue(all(method == "thread/read" for method, _ in reader.calls))

    def test_archived_history_recovery_does_not_resume_or_reopen_thread(self):
        thread_id = self.create()
        self.sessions.update(thread_id, {"archived": True})
        self.sessions.rpc.no_history = True
        turns = [{"id": "old", "status": "completed", "items": [
            {"id": "answer", "type": "agentMessage", "text": "Archived answer"}]}]
        reader = self.history_reader(thread_id, turns)
        self.sessions.rpc.calls.clear()
        result = self.sessions.read(thread_id)
        self.assertEqual(result["thread"]["turns"], turns)
        self.assertEqual(result["work_thread"]["attention"], "archived")
        self.assertTrue(all(method == "thread/read" for method, _ in self.sessions.rpc.calls + reader.calls))

    def test_fast_turn_completion_is_not_overwritten_by_start_response(self):
        thread_id = self.create()
        self.sessions.rpc.fast_turn = True
        self.sessions.send(thread_id, {"message": "Instant response"})
        self.assertNotIn(thread_id, self.sessions.active)
        self.assertFalse(self.store.get_session(thread_id)["turn_active"])

    def test_failed_turn_can_be_retried(self):
        thread_id = self.create()
        self.sessions.rpc.fail_turn = True
        with self.assertRaisesRegex(CoordinationError, "Provider unavailable"):
            self.sessions.send(thread_id, {"message": "Test"})
        self.sessions.rpc.fail_turn = False
        self.sessions.send(thread_id, {"message": "Retry"})
        self.assertIn(thread_id, self.sessions.active)

    def test_scope_state_survives_turn_completion(self):
        thread_id = self.create()
        self.store.begin_work(session_id=thread_id, scopes=["src/**"])
        self.sessions.rpc.fast_turn = True
        self.sessions.send(thread_id, {"message": "Keep work reserved"})
        state = self.store.get_session(thread_id)
        self.assertEqual(state["activity"], "waiting")
        self.assertEqual(state["write_scope"], ["src/**"])

    def test_archive_stop_restore_lifecycle(self):
        thread_id = self.create()
        self.sessions.send(thread_id, {"message": "Work"})
        with self.assertRaisesRegex(BrowserBusyError, "Stop"):
            self.sessions.update(thread_id, {"archived": True})
        self.sessions.interrupt(thread_id)
        self.sessions.update(thread_id, {"archived": True})
        self.assertEqual(self.sessions.list_sessions(), [])
        self.assertEqual(len(self.sessions.list_sessions(archived=True)), 1)
        self.assertEqual(self.store.get_session(thread_id)["presence"], "offline")
        with self.assertRaisesRegex(CoordinationError, "Reopen"):
            self.sessions.send(thread_id, {"message": "Cannot send"})
        self.sessions.update(thread_id, {"archived": False})
        self.sessions.send(thread_id, {"message": "Continue"})

    def test_repository_filter_applies_to_read_mutations_and_creation(self):
        thread_id = self.create()
        other = self.root / "other"
        other.mkdir()
        isolated = BrowserSessions(self.store, str(other), rpc_factory=FakeCodex)
        self.addCleanup(isolated.close)
        self.assertEqual(isolated.list_sessions(), [])
        for operation in [lambda: isolated.read(thread_id), lambda: isolated.send(thread_id, {"message": "No"}), lambda: isolated.update(thread_id, {"archived": True}), lambda: isolated.create({"cwd": str(self.root)})]:
            with self.assertRaises(CoordinationError):
                operation()
        self.assertEqual(isolated.rpc.calls, [])

    def test_invalid_model_effort_or_directory_never_creates_a_thread(self):
        for values in [{"model": "unknown"}, {"effort": "invalid"}, {"model": "available-model", "effort": "invalid"}, {"cwd": str(self.root / "missing")}]:
            with self.assertRaises(CoordinationError):
                self.create(**values)
        self.assertFalse(any(call[0] == "thread/start" for call in self.sessions.rpc.calls))

    def test_explicit_effort_with_inherited_model_creates_and_sends_on_first_attempt(self):
        request = self.sessions.rpc.request
        def configured(method, params=None):
            if method == "config/read":
                self.assertEqual(params, {"cwd": str(self.root), "includeLayers": False})
                return {"config": {"model": "other-model", "model_reasoning_effort": "low"}}
            return request(method, params)
        with patch.object(self.sessions.rpc, "request", side_effect=configured):
            thread_id = self.create(model=None, effort="low")
            self.sessions.send(thread_id, {"message": "First Enter"})
        start = next(params for method, params in self.sessions.rpc.calls if method == "thread/start")
        self.assertEqual(start["model"], "other-model", "Use the configured model, not the catalog default")
        self.assertEqual(start["config"]["model_reasoning_effort"], "low")
        self.assertEqual(sum(method == "thread/start" for method, _ in self.sessions.rpc.calls), 1)
        self.assertEqual(sum(method == "turn/start" for method, _ in self.sessions.rpc.calls), 1)

    def test_inherited_model_effort_uses_catalog_default_only_when_configuration_is_absent(self):
        thread_id = self.create(model=None, effort="high")
        self.assertEqual(self.sessions._record(thread_id)["model"], "available-model")
        self.assertEqual(self.sessions._record(thread_id)["effort"], "high")

    def test_inherited_model_keeps_unavailable_and_unsupported_effort_errors(self):
        request = self.sessions.rpc.request
        for model, error in [("unavailable", "Choose an available model"), ("other-model", "Reasoning effort is not supported")]:
            with self.subTest(model=model), patch.object(self.sessions.rpc, "request", side_effect=lambda method, params=None:
                    {"config": {"model": model}} if method == "config/read" else request(method, params)):
                with self.assertRaisesRegex(CoordinationError, error):
                    self.create(model=None, effort="high")
        self.assertFalse(any(method == "thread/start" for method, _ in self.sessions.rpc.calls))

    def test_inherited_model_configuration_failure_does_not_create_or_silently_fall_back(self):
        request = self.sessions.rpc.request
        def failed(method, params=None):
            if method == "config/read":
                raise CoordinationError("Configuration unavailable")
            return request(method, params)
        with patch.object(self.sessions.rpc, "request", side_effect=failed):
            with self.assertRaisesRegex(CoordinationError, "Configuration unavailable"):
                self.create(model=None, effort="high")
            explicit = self.create(model="available-model", effort="high")
        self.assertEqual(self.sessions._record(explicit)["model"], "available-model")
        self.assertEqual(sum(method == "thread/start" for method, _ in self.sessions.rpc.calls), 1)

    def test_parent_workspace_can_create_read_and_list_child_repository_sessions(self):
        repo = self.root / "repo"
        (repo / ".git").mkdir(parents=True)
        thread_id = self.create(cwd=str(repo))
        self.assertEqual(self.sessions.rpc.calls[-1][1]["cwd"], str(repo))
        self.assertEqual(self.sessions.read(thread_id)["session"]["cwd"], str(repo))
        self.assertEqual([item["thread_id"] for item in self.sessions.list_sessions()], [thread_id])
        self.assertEqual([item["thread_id"] for item in self.sessions.list_work_threads()], [thread_id])
        self.sessions.send(thread_id, {"message": "Work inside the selected repository."})
        self.sessions.interrupt(thread_id)
        self.sessions.update_work_thread(thread_id, {"attention": "later"})
        self.assertEqual(self.sessions.work_thread(thread_id)["attention"], "later")

    def test_workspace_choices_include_previous_workspaces_without_starting_codex(self):
        nested = self.root / "group/project"
        nested.mkdir(parents=True)
        self.store.register(session_id="previous", client="codex", cwd=str(nested))
        self.store.end_session("previous")
        choices = self.sessions.list_workspaces()
        self.assertIn(str(nested), [item["cwd"] for item in choices])
        self.assertEqual(self.sessions.rpc.calls, [])

    def test_creation_rejects_paths_and_symlinks_outside_the_workspace(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        outside = Path(temporary.name).resolve()
        (self.root / "escape").symlink_to(outside, target_is_directory=True)
        for path in (outside, self.root / ".." / outside.name, self.root / "escape"):
            with self.subTest(path=path), self.assertRaises(CoordinationError):
                self.create(cwd=str(path))
        self.assertEqual(self.sessions.rpc.calls, [])

    def test_repository_filter_preserves_sessions_in_linked_worktrees(self):
        repo = self.root / "repo"
        gitdir = repo / ".git/worktrees/feature"
        gitdir.mkdir(parents=True)
        (gitdir / "commondir").write_text("../..")
        worktree = self.root / "feature"
        worktree.mkdir()
        (worktree / ".git").write_text("gitdir: " + str(gitdir))
        first = self.create(cwd=str(repo))
        second = self.create(cwd=str(worktree))
        filtered = BrowserSessions(self.store, str(repo), rpc_factory=FakeCodex)
        self.addCleanup(filtered.close)
        filtered.rpc.threads = self.sessions.rpc.threads
        self.assertEqual({item["thread_id"] for item in filtered.list_sessions()}, {first, second})
        self.assertEqual({item["thread_id"] for item in filtered.list_work_threads()}, {first, second})
        self.assertEqual(filtered.read(second)["session"]["cwd"], str(worktree))

    def pending(self, thread_id, method, **params):
        self.sessions._event({"id": 42, "method": method, "params": {"threadId": thread_id, **params}})
        return self.sessions.pending_requests(thread_id)[0]["key"]

    def test_approvals_are_scoped_and_only_answered_once(self):
        first, second = self.create(), self.create()
        key = self.pending(first, "item/commandExecution/requestApproval", command="touch project.txt")
        with self.assertRaises(CoordinationError):
            self.sessions.answer(second, key, {"decision": "accept"})
        with self.assertRaises(CoordinationError):
            self.sessions.answer(first, key, {"decision": "arbitrary"})
        self.sessions.answer(first, key, {"decision": "decline"})
        self.assertEqual(self.sessions.rpc.writes[-1], {"id": 42, "result": {"decision": "decline"}})
        with self.assertRaises(CoordinationError):
            self.sessions.answer(first, key, {"decision": "accept"})

    def test_permissions_never_grant_more_than_requested(self):
        thread_id = self.create()
        permissions = {"network": {"enabled": True}}
        key = self.pending(thread_id, "item/permissions/requestApproval", permissions=permissions)
        self.sessions.answer(thread_id, key, {"decision": "accept", "permissions": {"fileSystem": "everything"}})
        self.assertEqual(self.sessions.rpc.writes[-1]["result"], {"permissions": permissions, "scope": "turn"})

    def test_question_answers_and_unknown_request_cancellation(self):
        thread_id = self.create()
        key = self.pending(thread_id, "item/tool/requestUserInput", questions=[{"id": "choice", "question": "Which?"}])
        with self.assertRaises(CoordinationError):
            self.sessions.answer(thread_id, key, {"answers": {}})
        self.sessions.answer(thread_id, key, {"answers": {"choice": {"answers": ["First"]}}})
        self.assertEqual(self.sessions.rpc.writes[-1]["result"]["answers"]["choice"]["answers"], ["First"])
        key = self.pending(thread_id, "future/request")
        self.sessions.answer(thread_id, key, {"decision": "cancel"})
        self.assertEqual(self.sessions.rpc.writes[-1]["error"]["code"], -32601)

    def test_event_replay_reports_gaps_and_close_unblocks_waiter(self):
        self.create()
        for index in range(1505):
            self.sessions._publish("progress", {"index": index})
        result = self.sessions.events_after(0, timeout=0)
        self.assertTrue(result["reset"])
        self.assertEqual(len(result["events"]), 1500)
        with ThreadPoolExecutor() as executor:
            waiter = executor.submit(self.sessions.events_after, result["seq"], 5)
            self.sessions.close()
            self.assertTrue(all(event["method"] == "bridge/disconnected" for event in waiter.result(timeout=2)["events"]))

    def test_browser_launch_supplies_checkpoint_instructions_and_captures_intent(self):
        thread_id = self.create()
        options = next(params for method, params in self.sessions.rpc.calls if method == "thread/start")
        self.assertIn("agent-coord checkpoint --json", options["developerInstructions"])
        self.assertNotIn(" --db ", options["developerInstructions"])
        self.sessions.rpc.fast_turn = True
        self.sessions.send(thread_id, {"message": "Investigate database performance."})
        self.sessions.send(thread_id, {"message": "Consider another option."})
        self.assertEqual(self.sessions.work_thread(thread_id)["original_request"], "Investigate database performance.")

    def test_later_and_checkpoint_survive_server_restart_and_archive_restore(self):
        thread_id = self.create()
        self.sessions.checkpoint_work_thread(thread_id, {"phase": "investigation", "summary": "Review the options.", "next_action": "Choose an approach.", "next_actor": "user"})
        self.sessions.update_work_thread(thread_id, {"attention": "later"})
        self.sessions.close()
        reopened = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(reopened.close)
        reopened.rpc.threads = self.sessions.rpc.threads
        thread = reopened.list_work_threads()[0]
        self.assertEqual(thread["attention"], "later")
        self.assertEqual(thread["checkpoint"]["author"], "user")
        self.assertFalse(thread["needs_attention"])
        self.assertEqual(thread["response_state"], "input")
        reopened.update_work_thread(thread_id, {"attention": "archived"})
        self.assertEqual(reopened.list_work_threads(), [])
        with self.assertRaisesRegex(CoordinationError, "Reopen"):
            reopened.send(thread_id, {"message": "Work"})
        self.assertTrue(reopened.update_work_thread(thread_id, {"attention": "now"})["needs_attention"])
        reopened.send(thread_id, {"message": "Continue."})

    def test_terminal_threads_are_visible_without_starting_codex_and_only_resume_offline(self):
        self.store.register(session_id="terminal", client="codex", cwd=str(self.root))
        self.store.threads.start_turn("terminal", prompt="Investigate something.")
        thread = self.sessions.list_work_threads()[0]
        self.assertFalse(thread["browser_session"])
        self.assertFalse(thread["can_resume"])
        self.assertEqual(self.sessions.rpc.calls, [])
        with self.assertRaises(CoordinationError):
            self.sessions.resume_work_thread("terminal")
        self.store.end_session("terminal")
        self.sessions.rpc.threads["terminal"] = {"id": "terminal", "turns": []}
        self.assertTrue(self.sessions.resume_work_thread("terminal")["browser_session"])
        self.assertEqual(self.sessions.read("terminal")["work_thread"]["original_request"], "Investigate something.")

    def test_global_thread_list_covers_multiple_projects_and_isolated_mutations_fail(self):
        first = self.create()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        other = Path(temporary.name).resolve()
        self.store.register(session_id="other-session", client="claude", cwd=str(other))
        global_sessions = BrowserSessions(self.store, rpc_factory=FakeCodex)
        self.addCleanup(global_sessions.close)
        self.assertEqual({t["thread_id"] for t in global_sessions.list_work_threads()}, {first, "other-session"})
        self.assertEqual([t["thread_id"] for t in self.sessions.list_work_threads()], [first])
        for operation in [lambda: self.sessions.work_thread("other-session"),
                          lambda: self.sessions.update_work_thread("other-session", {"attention": "later"}),
                          lambda: self.sessions.link_work_thread("other-session", {"kind": "issue", "target": "one"}),
                          lambda: self.sessions.checkpoint_work_thread("other-session", {"phase": "discussion", "summary": "No."})]:
            with self.assertRaises(CoordinationError):
                operation()

    def test_existing_browser_history_backfills_original_request(self):
        thread_id = self.create()
        self.sessions.rpc.fast_turn = True
        self.sessions.send(thread_id, {"message": "The earlier request."})
        with self.store._connection() as db:
            db.execute("DELETE FROM turn_completions WHERE thread_id = ?", (thread_id,))
            db.execute("DELETE FROM work_threads WHERE thread_id = ?", (thread_id,))
        reopened = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.work_thread(thread_id)["original_request"], "The earlier request.")

    def test_delegated_children_remain_under_the_parent_thread(self):
        parent = self.create()
        self.store.create_delegation(parent_session_id=parent, cwd=str(self.root), bead_id="work-a", scopes=["src/**"], instructions="Investigate a subproblem.", mode="reviewed", delegation_id="delegated")
        self.store.register(session_id="child", client="codex", cwd=str(self.root))
        self.store.attach_delegation("delegated", "child")
        self.assertEqual([t["thread_id"] for t in self.sessions.list_work_threads()], [parent])
        self.assertEqual([t["thread_id"] for t in self.sessions.work_thread(parent)["children"]], ["child"])


class DatabaseContentionTests(unittest.TestCase):
    setUp = BrowserSessionTests.setUp
    create = BrowserSessionTests.create

    @contextmanager
    def database_locked(self):
        connect = self.store._connect

        def short_timeout():
            connection = connect()
            connection.execute("PRAGMA busy_timeout = 20")
            return connection

        blocker = connect()
        try:
            blocker.execute("BEGIN IMMEDIATE")
            with patch.object(self.store, "_connect", short_timeout):
                yield
        finally:
            blocker.rollback()
            blocker.close()

    def test_locked_history_does_not_disconnect_rpc_or_lose_completion(self):
        identity = self.create()
        turn = self.sessions.send(identity, {"message": "Start"})["turn"]["id"]
        client = CodexRPC(self.store, self.sessions._event, command=[sys.executable, "-u", "-c", '''
import json,sys
for line in sys.stdin:
    message=json.loads(line)
    if 'id' not in message: continue
    if message['method']=='emit': print(json.dumps(message['params']),flush=True)
    print(json.dumps({'id':message['id'],'result':{'ok':True}}),flush=True)
'''])
        self.addCleanup(client.close)
        client.start()
        calls_before = list(self.sessions.rpc.calls)
        with self.database_locked():
            item = {"id": "answer", "type": "agentMessage", "text": "Saved once"}
            client.request("emit", {"method": "item/completed", "params": {
                "threadId": identity, "turnId": turn, "item": item}}, timeout=2)
            client.request("emit", {"method": "turn/completed", "params": {
                "threadId": identity, "turn": {"id": turn, "status": "completed"}}}, timeout=2)
            self.assertEqual(client.request("still-responsive", timeout=2), {"ok": True})
            self.assertTrue(client.ready)
            self.assertIsNone(client.process.poll())
            self.assertNotIn(identity, self.sessions.active)
            self.assertFalse(self.sessions.persistence.flush(timeout=.02))
        self.assertTrue(self.sessions.persistence.flush(timeout=3))
        with self.store._connection() as db:
            history = json.loads(db.execute("SELECT history_json FROM browser_history WHERE thread_id=?", (identity,)).fetchone()[0])
            completions = db.execute("SELECT status FROM turn_completions WHERE thread_id=?", (identity,)).fetchall()
        self.assertEqual([row[0] for row in completions], ["completed"])
        self.assertEqual([i["text"] for i in history["turns"][0]["items"] if i["type"] == "agentMessage"], ["Saved once"])
        self.assertFalse(self.store.get_session(identity)["turn_active"])
        self.assertNotIn(identity, self.sessions.dirty_histories)
        self.assertEqual(self.sessions.rpc.calls, calls_before)  # No provider replay.

    def test_disconnect_clears_runtime_state_before_database_recovers(self):
        identity = self.create()
        self.sessions.send(identity, {"message": "Start"})
        other = self.create()
        with self.store._connection() as db:
            db.execute("UPDATE browser_sessions SET client='claude' WHERE thread_id=?", (other,))
        self.sessions.loaded_clients[other] = "claude"
        self.sessions.active[other] = "other-turn"
        self.sessions.requests = {"codex": {"params": {"threadId": identity}},
                                  "claude": {"params": {"threadId": other}}}
        self.sessions.queue.enqueue(identity, {"message": "Do not send automatically"})
        with self.database_locked():
            self.sessions._event({"method": "bridge/disconnected", "params": {}})
            self.assertNotIn(identity, self.sessions.active)
            self.assertNotIn(identity, self.sessions.loaded)
            self.assertIn(other, self.sessions.active)
            self.assertIn(other, self.sessions.loaded)
            self.assertEqual(set(self.sessions.requests), {"claude"})
            self.assertEqual(self.sessions.events[-1]["method"], "bridge/disconnected")
        self.assertTrue(self.sessions.persistence.flush(timeout=3))
        self.assertIsNotNone(self.store.get_session(identity)["ended_at"])
        self.assertIsNone(self.store.get_session(other)["ended_at"])
        self.assertEqual(self.sessions.queue.list(identity)[0]["state"], "paused")

    def test_next_prompt_waits_for_deferred_completion_without_resubmission(self):
        identity = self.create()
        first = self.sessions.send(identity, {"message": "First"})["turn"]["id"]
        with ThreadPoolExecutor(max_workers=1) as executor:
            with self.database_locked():
                turn = self.sessions.rpc.threads[identity]["turns"][-1]
                turn["status"] = "completed"
                self.sessions._event({"method": "turn/completed", "params": {
                    "threadId": identity, "turn": turn}})
                sending = executor.submit(self.sessions.send, identity, {"message": "Second"})
                with self.assertRaises(FutureTimeoutError):
                    sending.result(timeout=.03)
                self.assertEqual(len(self.sessions.rpc.threads[identity]["turns"]), 1)
            second = sending.result(timeout=3)["turn"]["id"]
        self.assertNotEqual(first, second)
        self.assertEqual(len(self.sessions.rpc.threads[identity]["turns"]), 2)
        self.assertEqual(self.sessions.active[identity], second)
        with self.store._connection() as db:
            self.assertEqual(db.execute("SELECT turn_id FROM work_threads WHERE thread_id=?", (identity,)).fetchone()[0], second)
            self.assertEqual(db.execute("SELECT turn_key FROM turn_completions WHERE thread_id=?", (identity,)).fetchone()[0], first)

    def test_turn_bookkeeping_retries_in_order_without_reapplying_deltas(self):
        identity = self.create()
        with self.database_locked():
            for turn in ("first", "second"):
                self.sessions._event({"method": "turn/started", "params": {
                    "threadId": identity, "turn": {"id": turn, "items": []}}})
                self.sessions._event({"method": "item/started", "params": {
                    "threadId": identity, "turnId": turn,
                    "item": {"id": turn + "-answer", "type": "agentMessage", "text": ""}}})
                self.sessions._event({"method": "item/agentMessage/delta", "params": {
                    "threadId": identity, "turnId": turn, "itemId": turn + "-answer", "delta": "once"}})
                self.sessions._event({"method": "turn/completed", "params": {
                    "threadId": identity, "turn": {"id": turn, "status": "completed"}}})
        self.assertTrue(self.sessions.persistence.flush(timeout=3))
        with self.store._connection() as db:
            completions = db.execute("SELECT turn_key FROM turn_completions WHERE thread_id=? ORDER BY id", (identity,)).fetchall()
            history = json.loads(db.execute("SELECT history_json FROM browser_history WHERE thread_id=?", (identity,)).fetchone()[0])
        self.assertEqual([r[0] for r in completions], ["first", "second"])
        self.assertEqual([t["items"][0]["text"] for t in history["turns"]], ["once", "once"])


class RPCProcessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = CoordinationStore(Path(temporary.name) / "state.sqlite3")

    def client(self, script):
        events = []
        client = CodexRPC(self.store, events.append, command=[sys.executable, "-u", "-c", script])
        self.addCleanup(client.close)
        return client, events

    def test_process_does_not_inherit_parent_identity(self):
        client, _ = self.client('''
import sys,json,os
for line in sys.stdin:
 m=json.loads(line)
 if 'id' in m:
  print(json.dumps({'id':m['id'],'result':{k:os.environ.get(k) for k in ('AGENT_COORD_SESSION_ID','CODEX_THREAD_ID','CLAUDE_ENV_FILE','PATH','AGENT_COORD_DB')}}),flush=True)
''')
        with patch.dict("os.environ", {"AGENT_COORD_SESSION_ID": "parent", "CODEX_THREAD_ID": "parent", "CLAUDE_ENV_FILE": "/parent/file"}):
            result = client.request("environment")
        self.assertIsNone(result["AGENT_COORD_SESSION_ID"])
        self.assertIsNone(result["CODEX_THREAD_ID"])
        self.assertIsNone(result["CLAUDE_ENV_FILE"])
        self.assertEqual(result["AGENT_COORD_DB"], str(self.store.database_path))
        self.assertTrue(Path(result["PATH"].split(":")[0], "agent-coord").is_file())

    def test_process_interleaves_server_requests_and_client_responses(self):
        client, events = self.client('''
import sys,json
for line in sys.stdin:
 m=json.loads(line)
 if 'id' not in m: continue
 if m.get('method') != 'initialize':
  print(json.dumps({'id':'approval','method':'item/fileChange/requestApproval','params':{'threadId':'t'}}),flush=True)
 print(json.dumps({'id':m['id'],'result':{'method':m.get('method')}}),flush=True)
''')
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda method: client.request(method), ["first", "second"]))
        self.assertEqual({r["method"] for r in results}, {"first", "second"})
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["id"], "approval")

    def test_exit_fails_pending_requests_without_waiting_for_timeout(self):
        client, _ = self.client('''
import sys,json
for line in sys.stdin:
 m=json.loads(line)
 if m.get('method')=='initialize': print(json.dumps({'id':m['id'],'result':{}}),flush=True)
 elif m.get('method')=='die': sys.exit(1)
''')
        with self.assertRaisesRegex(CoordinationError, "disconnected"):
            client.request("die", timeout=2)

    def test_timeout_cleans_pending_requests_and_close_reaps_child(self):
        client, _ = self.client('''
import sys,json
for line in sys.stdin:
 m=json.loads(line)
 if m.get('method')=='initialize': print(json.dumps({'id':m['id'],'result':{}}),flush=True)
''')
        with self.assertRaisesRegex(CoordinationError, "did not respond"):
            client.request("ignored", timeout=.03)
        self.assertEqual(client.pending, {})
        client.close()
        self.assertIsNotNone(client.process.poll())
        self.assertTrue(all(not reader.is_alive() for reader in client.readers))


if __name__ == "__main__":
    unittest.main()


class ToolActivityTests(unittest.TestCase):
    """Streamed Codex items record what a browser thread is doing right now."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = CoordinationStore(self.root / "state.sqlite3")
        self.sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(self.sessions.close)

    def test_command_and_file_items_become_the_thread_activity(self):
        thread_id = self.sessions.create({"name": "Worker"})["session"]["thread_id"]
        self.assertIsNone(self.sessions.work_thread(thread_id, history=False)["last_tool"])
        turn = self.sessions.send(thread_id, {"message": "Run the suite"})["turn"]
        params = {"threadId": thread_id, "turnId": turn["id"]}
        self.sessions._event({"method": "item/started", "params": {**params, "item": {
            "id": "cmd-1", "type": "commandExecution", "command": "python3 -m unittest  -q", "status": "inProgress"}}})
        tool = self.sessions.work_thread(thread_id, history=False)["last_tool"]
        self.assertEqual((tool["name"], tool["kind"], tool["label"]), ("commandExecution", "run", "python3 -m unittest -q"))
        self.sessions._event({"method": "item/completed", "params": {**params, "item": {
            "id": "cmd-1", "type": "commandExecution", "command": "python3 -m unittest  -q", "status": "completed"}}})
        self.sessions._event({"method": "item/started", "params": {**params, "item": {
            "id": "patch-1", "type": "fileChange", "changes": [{"path": "web/app.js", "kind": "update"}, {"path": "web/styles.css", "kind": "update"}]}}})
        tool = self.store.get_session(thread_id)["last_tool"]
        self.assertEqual((tool["kind"], tool["label"]), ("edit", "web/app.js, web/styles.css"))
        # Messages and reasoning are not activity; the last tool stays.
        self.sessions._event({"method": "item/started", "params": {**params, "item": {"id": "answer", "type": "agentMessage", "text": "Done"}}})
        self.assertEqual(self.store.get_session(thread_id)["last_tool"]["label"], "web/app.js, web/styles.css")
