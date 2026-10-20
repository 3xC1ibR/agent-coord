from __future__ import annotations

import copy
import json
import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))

from agent_coord.attention import ClassificationError
from agent_coord.dispatch_history import RoutingHistory
from agent_coord.dispatch_routing import RoutingPolicy, build_state, classify, decide, question_for, route
from agent_coord.dispatch_snapshot import read_snapshot
from agent_coord.conversation_search import sync_messages


def thread(tid, title, **kwargs):
    return {"thread_id": tid, "title": title, "cwd": "/project", "summary": title,
            "history_status": "saved", "last_work_at": 1000, "messages": [], **kwargs}


def request(message="Add multiple vendors to the onboarding form", **kwargs):
    return {"request_id": "request-1", "dispatcher_id": "entry", "message": message,
            "cwd": "/project", **kwargs}


def answer(state, choice, confidence=.98, winning=.98):
    keys = question_for(state, RoutingPolicy())["criteria"]
    return {"type": "choice", "choice": choice, "confidence": confidence,
            "probabilities": {k: winning if k == choice else (1 - winning) / (len(keys) - 1) for k in keys}}


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = {"captured_at": 10000, "threads": [
            thread("vendor", "Vendor onboarding form"), thread("helm", "Helm chart", last_work_at=1),
            thread("other", "Vendor form", cwd="/other"), thread("entry", "Vendor dispatcher")]}

    def state(self, **kwargs):
        return build_state(self.snapshot, request(**kwargs))

    def choose(self, state, tid):
        return next(c["key"] for c in state["candidates"] if c["thread_id"] == tid)

    def test_workspace_and_dispatcher_are_filtered_without_mutation(self):
        original = copy.deepcopy(self.snapshot)
        state = self.state()
        self.assertEqual({c["thread_id"] for c in state["candidates"]}, {"vendor", "helm"})
        self.assertEqual(self.snapshot, original)

    def test_old_closed_thread_remains_eligible(self):
        self.snapshot["threads"][1]["attention"] = "archived"
        state = self.state(message="Change the Helm chart probes")
        self.assertEqual(state["candidates"][0]["thread_id"], "helm")
        result = decide(state, answer(state, self.choose(state, "helm")))
        self.assertEqual((result["action"], result["delivery"]), ("reuse", "ready"))

    def test_recent_assignment_beats_stale_checkpoint(self):
        self.snapshot["threads"][0].update(title="UI work", summary="Changed sidebar", running=True)
        state = self.state(recent_routes=[{"request_id": "earlier", "thread_id": "vendor",
                                          "message": "Create vendor onboarding form", "status": "queued"}])
        self.assertEqual(state["candidates"][0]["thread_id"], "vendor")
        self.assertIn("vendor", state["candidates"][0]["recent_routes"][0]["text"])
        result = decide(state, answer(state, self.choose(state, "vendor")))
        self.assertEqual((result["action"], result["delivery"]), ("reuse", "queue"))

    def test_creation_in_flight_keeps_same_destination(self):
        state = self.state(recent_routes=[{"creation_request_id": "create-1", "cwd": "/project",
                                          "message": "Vendor onboarding", "status": "creating"}])
        key = self.choose(state, "pending:create-1")
        result = decide(state, answer(state, key))
        self.assertEqual(result["creation_request_id"], "create-1")
        self.assertEqual(result["delivery"], "wait_for_creation")

    def test_cancelled_pending_creation_is_not_a_candidate(self):
        state = self.state(recent_routes=[{"creation_request_id": "create-1", "cwd": "/project",
                                          "message": "Vendor onboarding", "status": "cancelled"}])
        self.assertNotIn("pending:create-1", {c["thread_id"] for c in state["candidates"]})

    def test_references_survive_shortlist_but_not_workspace_filter(self):
        state = build_state(self.snapshot, request(referenced_thread_ids=["helm"]), RoutingPolicy(candidate_limit=1))
        self.assertEqual(state["candidates"][0]["thread_id"], "helm")
        state = self.state(referenced_thread_ids=["other"])
        result = decide(state, answer(state, "new"))
        self.assertEqual(result["reason"], "missing_reference")

    def test_paused_failed_and_closing_do_not_become_new_work(self):
        for fields in ({"wake_paused": True}, {"pending_close": True}, {"last_turn_status": "failed"}):
            with self.subTest(fields=fields):
                snapshot = copy.deepcopy(self.snapshot)
                snapshot["threads"][0].update(fields)
                state = build_state(snapshot, request())
                result = decide(state, answer(state, self.choose(state, "vendor")))
                self.assertEqual(result["action"], "dispatcher")
                self.assertEqual(result["suggested_thread_id"], "vendor")

    def test_user_queue_has_priority(self):
        self.snapshot["threads"][0]["user_queue"] = 1
        state = self.state()
        self.assertEqual(decide(state, answer(state, self.choose(state, "vendor")))["delivery"], "queue")

    def test_low_confidence_probability_and_margin_each_defer(self):
        state = self.state()
        for policy, values in ((RoutingPolicy(), {"confidence": .1}),
                               (RoutingPolicy(), {"winning": .6}),
                               (RoutingPolicy(min_probability=.1, min_margin=.9), {"winning": .8})):
            with self.subTest(values=values):
                self.assertEqual(decide(state, answer(state, "new", **values), policy)["reason"], "low_confidence")

    def test_new_is_supported_but_not_in_place_of_explicit_target(self):
        state = self.state(message="Design a bird identification game")
        self.assertEqual(decide(state, answer(state, "new"))["action"], "new")
        state = self.state(target_thread_id="vendor")
        self.assertEqual(decide(state, answer(state, "new"))["reason"], "explicit_target_disagreement")
        self.assertEqual(decide(state, answer(state, self.choose(state, "helm")))["reason"], "explicit_target_disagreement")

    def test_missing_workspace_truncated_context_and_request_defer(self):
        for kwargs, reason in (({"cwd": None}, "workspace_unresolved"),
                               ({"message": "x" * 6001}, "incomplete_request_context"),
                               ({"context": [{"text": "x" * 1700}]}, "incomplete_request_context")):
            state = self.state(**kwargs)
            self.assertEqual(decide(state, answer(state, "new"))["reason"], reason)

    def test_incomplete_discovery_does_not_mean_no_suitable_thread(self):
        self.snapshot["threads"] = [thread(str(i), "Saved title", history_status="missing") for i in range(5)]
        state = self.state()
        self.assertEqual(decide(state, answer(state, "new"))["reason"], "incomplete_discovery")

    def test_text_only_router_defers_unseen_attachments(self):
        state = self.state(message="Fix the issue in this screenshot", attachments=[{"path": "/image.png"}])
        self.assertEqual(decide(state, answer(state, "new"))["reason"], "attachment_context_required")

    def test_network_failure_falls_back_without_delivery(self):
        classifier = Mock(side_effect=ClassificationError("request_failed"))
        result = route(self.snapshot, request(), classifier=classifier)
        self.assertEqual(result["decision"]["action"], "dispatcher")
        self.assertEqual(result["decision"]["reason"], "request_failed")
        self.assertGreaterEqual(result["evidence"]["timings_ms"]["classifier"], 0)
        self.assertNotIn("decision", result["evidence"]["timings_ms"])

    def test_relevant_history_can_outlive_recent_topic(self):
        self.snapshot["threads"][0]["messages"] = [
            {"role": "user", "text": "The vendor onboarding form must validate tax identifiers"},
            {"role": "assistant", "text": "Agreed"},
            {"role": "user", "text": "Now adjust the sidebar color"}]
        state = self.state(message="Change vendor tax identifier validation")
        candidate = next(c for c in state["candidates"] if c["thread_id"] == "vendor")
        self.assertIn("tax identifiers", candidate["relevant_messages"][0]["text"])
        self.assertIn("sidebar", candidate["latest_user_message"]["text"])

    def test_context_lifecycle_is_included_in_candidate_state_and_retrieval_score(self):
        self.snapshot["threads"][1]["context_lifecycle"] = {
            "checkpoint_id": 42,
            "stale": False,
            "recommendation": "reuse",
            "reason": "Retained debugging constraints help with related fixes.",
            "reuse_for": ["Canary rollout healthchecks"],
            "fresh_for": ["Independent security audit"],
            "handover": {"confirmed_facts": ["Secret"], "constraints": [], "tried_and_failed": [], "open_questions": []},
        }
        state = self.state(message="Adjust canary rollout healthchecks")
        self.assertEqual(state["candidates"][0]["thread_id"], "helm")
        lc = state["candidates"][0]["context_lifecycle"]
        self.assertEqual(lc, {
            "checkpoint_id": 42,
            "stale": False,
            "recommendation": "reuse",
            "reason": "Retained debugging constraints help with related fixes.",
            "reuse_for": ["Canary rollout healthchecks"],
            "fresh_for": ["Independent security audit"],
        })
        self.assertNotIn("handover", lc)
        self.assertNotIn("context_lifecycle", state["candidates"][1])
        self.assertIn("context_lifecycle", question_for(state, RoutingPolicy())["instructions"])

    def test_invalid_policy_rejected(self):
        for kw in ({"candidate_limit": 0}, {"min_confidence": float("nan")}, {"timeout": -1}, {"name": "unknown"}):
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                RoutingPolicy(**kw)


class RoutingAPITests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.key = Path(directory.name) / "key"
        self.key.write_text("routing-test-secret")
        self.config = {"api_key_file": str(self.key)}
        self.state = build_state({"captured_at": 10, "threads": []}, request(message="routing-test-secret"))
        self.connection = Mock()
        self.connection.getresponse.return_value.status = 200

    def call(self, value=None):
        payload = {"model": "jev-1.13.0", "answers": {"destination": value or answer(self.state, "new")},
                   "usage": {"input_tokens": 1, "output_tokens": 1}}
        self.connection.getresponse.return_value.read.return_value = json.dumps(payload).encode()
        with patch("agent_coord.dispatch_routing.http.client.HTTPSConnection", return_value=self.connection):
            return classify(self.state, self.config)

    def test_fixed_endpoint_redaction_and_response_limit(self):
        self.assertEqual(self.call()["choice"], "new")
        args, kwargs = self.connection.request.call_args
        self.assertEqual(args, ("POST", "/v1/systemone"))
        self.assertNotIn(b"routing-test-secret", kwargs["body"])
        self.connection.getresponse.return_value.read.assert_called_once_with(65537)
        self.connection.close.assert_called_once()

    def test_invalid_choice_probabilities_and_confidence_fail_closed(self):
        for key, value in (("choice", "invented-thread"), ("confidence", True),
                           ("confidence", float("nan")), ("probabilities", {"new": 2})):
            a = answer(self.state, "new")
            a[key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ClassificationError, "invalid_response"):
                self.call(a)

    def test_errors_are_content_free(self):
        self.connection.request.side_effect = OSError("routing-test-secret")
        with self.assertRaisesRegex(ClassificationError, "^request_failed$"):
            self.call()
        self.connection.request.side_effect = None
        self.connection.getresponse.return_value.status = 302
        with self.assertRaisesRegex(ClassificationError, "^http_302$"):
            self.call()


class HistoryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "routing.sqlite3"
        self.history = RoutingHistory(self.path)

    def test_retries_and_concurrent_decisions_have_one_winner(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda i: self.history.record(request(), {"action": "reuse", "thread_id": str(i)}), range(8)))
        self.assertEqual(len({r["id"] for r in results}), 1)
        self.assertEqual(len({r["decision"]["thread_id"] for r in results}), 1)
        self.assertEqual(sum(not r["replayed"] for r in results), 1)
        with self.assertRaises(ValueError):
            self.history.record(request(message="Different instruction"), {"action": "new"})

    def test_catchup_is_lazy_durable_and_does_not_skip_later_events(self):
        for i in range(3):
            self.history.record(request(request_id=str(i)), {"action": "new"})
        batch = self.history.catch_up("entry", limit=2)
        self.assertTrue(batch["has_more"])
        self.assertEqual(len(self.history.catch_up("entry")["events"]), 3)
        self.history.confirm_seen("entry", batch["through_id"])
        reopened = RoutingHistory(self.path)
        self.assertEqual(len(reopened.catch_up("entry")["events"]), 1)
        self.assertEqual(reopened.catch_up("other")["events"], [])
        with self.assertRaises(ValueError):
            reopened.confirm_seen("other", batch["through_id"])

    def test_preview_retries_do_not_call_jev_or_duplicate_work(self):
        snapshot = {"captured_at": 10, "threads": []}
        classifier = Mock(side_effect=lambda state, *_: answer(state, "new"))
        first = self.history.plan(snapshot, request(), classifier=classifier)
        second = self.history.plan(snapshot, request(), classifier=classifier)
        classifier.assert_called_once()
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["decision"], second["decision"])
        self.assertTrue(first["decision"]["creation_request_id"].startswith("dispatch-"))

    def test_followup_automatically_sees_pending_destination_without_llm_catchup(self):
        snapshot = {"captured_at": 10, "threads": []}
        first = self.history.plan(snapshot, request(), classifier=lambda state, *_: answer(state, "new"))
        second = self.history.plan(snapshot, request(message="Add a second vendor", request_id="followup"),
                                   classifier=lambda state, *_: answer(state, state["candidates"][0]["key"]))
        self.assertEqual(second["decision"]["delivery"], "wait_for_creation")
        self.assertEqual(second["decision"]["creation_request_id"], first["decision"]["creation_request_id"])
        self.assertEqual(len(self.history.catch_up("entry")["events"]), 2)
        self.assertEqual(len(self.history.catch_up("entry")["events"]), 2)

    def test_preview_id_cannot_change_input(self):
        snapshot = {"captured_at": 10, "threads": []}
        self.history.record(request(), {"action": "new"})
        classifier = Mock()
        with self.assertRaises(ValueError):
            self.history.plan(snapshot, request(message="Changed"), classifier=classifier)
        classifier.assert_not_called()

    def test_live_app_database_cannot_be_used_as_history_store(self):
        app = self.path.parent / "app.sqlite3"
        with closing(sqlite3.connect(app)) as db:
            db.execute("CREATE TABLE sessions(id TEXT)")
            db.commit()
        before = app.read_bytes()
        with self.assertRaisesRegex(ValueError, "separate database"):
            RoutingHistory(app)
        self.assertEqual(app.read_bytes(), before)


class SnapshotTests(unittest.TestCase):
    def test_snapshot_is_read_only_and_never_projects_tool_or_setup_text(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app.sqlite3"
            with closing(sqlite3.connect(path)) as db:
                db.executescript("""CREATE TABLE browser_sessions(thread_id TEXT);
                    CREATE TABLE sessions(session_id TEXT,cwd TEXT,client TEXT,turn_active INTEGER,write_scope_json TEXT);
                    CREATE TABLE work_threads(thread_id TEXT,title TEXT,original_request TEXT,attention TEXT,created_at REAL);
                    CREATE TABLE thread_checkpoints(id INTEGER,thread_id TEXT,summary TEXT,phase TEXT,created_at REAL);
                    CREATE TABLE browser_history(thread_id TEXT,history_json TEXT);
                    INSERT INTO browser_sessions VALUES ('vendor');
                    INSERT INTO sessions VALUES ('vendor','/project','codex',1,'[]');
                    INSERT INTO work_threads VALUES ('vendor','Vendor form','Build form','now',1);
                """)
                history = {"turns": [{"id": "t", "items": [
                    {"type": "userMessage", "content": [{"type": "text", "text": "# AGENTS.md private setup"}]},
                    {"type": "userMessage", "content": [{"type": "text", "text": "Support multiple vendors"}]},
                    {"type": "commandExecution", "text": "private command output"}]}]}
                db.execute("INSERT INTO browser_history VALUES (?,?)", ("vendor", json.dumps(history)))
                db.commit()
            before = path.read_bytes()
            result = read_snapshot(path, now=10)
            self.assertTrue(result["read_only"])
            self.assertEqual([m["text"] for m in result["threads"][0]["messages"]], ["Support multiple vendors"])
            self.assertEqual(path.read_bytes(), before)

    def test_snapshot_reads_context_lifecycle_and_staleness(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app.sqlite3"
            report = {
                "recommendation": "fresh",
                "reason": "Implementation complete; independent review should start clean.",
                "reuse_for": ["Small follow-up tweaks"],
                "fresh_for": ["Independent code review"],
                "handover": {"confirmed_facts": ["Done"], "constraints": [], "tried_and_failed": [], "open_questions": []},
            }
            with closing(sqlite3.connect(path)) as db:
                db.executescript("""CREATE TABLE browser_sessions(thread_id TEXT);
                    CREATE TABLE sessions(session_id TEXT,cwd TEXT,client TEXT,turn_active INTEGER,write_scope_json TEXT);
                    CREATE TABLE work_threads(thread_id TEXT,title TEXT,original_request TEXT,attention TEXT,created_at REAL,turn_key TEXT);
                    CREATE TABLE thread_checkpoints(id INTEGER,thread_id TEXT,summary TEXT,phase TEXT,created_at REAL,context_lifecycle_json TEXT,context_lifecycle_turn_key TEXT);
                    INSERT INTO browser_sessions VALUES ('vendor'),('stale_vendor');
                    INSERT INTO sessions VALUES ('vendor','/project','codex',0,'[]'),('stale_vendor','/project','codex',0,'[]');
                    INSERT INTO work_threads VALUES ('vendor','Vendor form','Build form','now',1,'turn-1'),('stale_vendor','Stale vendor','Build','now',1,'turn-2');
                """)
                db.execute("INSERT INTO thread_checkpoints VALUES (10,'vendor','Done','finished',2,?,'turn-1')", (json.dumps(report),))
                db.execute("INSERT INTO thread_checkpoints VALUES (11,'stale_vendor','Done','finished',2,?,'turn-1')", (json.dumps(report),))
                db.commit()
            snap = read_snapshot(path, now=10)
            by_id = {t["thread_id"]: t for t in snap["threads"]}
            self.assertEqual(by_id["vendor"]["context_lifecycle"], {
                "checkpoint_id": 10,
                "stale": False,
                "recommendation": "fresh",
                "reason": "Implementation complete; independent review should start clean.",
                "reuse_for": ["Small follow-up tweaks"],
                "fresh_for": ["Independent code review"],
            })
            self.assertTrue(by_id["stale_vendor"]["context_lifecycle"]["stale"])


class IndexedSnapshotTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "app.sqlite3"
        self.db = sqlite3.connect(self.path)
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""CREATE TABLE browser_sessions(thread_id TEXT PRIMARY KEY,cwd TEXT);
            CREATE TABLE sessions(session_id TEXT,cwd TEXT,client TEXT,turn_active INTEGER,write_scope_json TEXT);
            CREATE TABLE work_threads(thread_id TEXT,title TEXT,original_request TEXT,attention TEXT,created_at REAL);
            CREATE TABLE thread_checkpoints(id INTEGER,thread_id TEXT,summary TEXT,phase TEXT,created_at REAL);
            CREATE TABLE browser_history(thread_id TEXT PRIMARY KEY,history_json TEXT);
            CREATE TABLE browser_inbox_wake(thread_id TEXT,paused INTEGER);
            INSERT INTO browser_sessions VALUES ('vendor','/project'),('other','/other');
            INSERT INTO sessions VALUES ('vendor','/old','codex',1,'[]'),('other','/old','codex',0,'[]');
            INSERT INTO work_threads VALUES ('vendor','Vendor form','Build form','now',1),('other','Other form','','now',1);
            INSERT INTO browser_inbox_wake VALUES ('vendor',0);
        """)
        self.save("vendor", {"turns": [{"id": "t", "items": [
            {"id": "duplicate", "type": "agentMessage", "text": str(i) + "x" * 6000}
            for i in range(203)]}]})
        self.save("other", {"turns": [{"items": [{"type": "agentMessage", "text": "other workspace"}]}]})

    def save(self, tid, history):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO browser_history VALUES (?,?)", (tid, json.dumps(history)))

    def snapshot(self, **kwargs):
        return read_snapshot(self.path, now=10, **kwargs)

    def test_cold_warm_duplicate_ids_bounds_workspace_and_read_only_parity(self):
        cold = self.snapshot()
        with self.db:
            sync_messages(self.db, cwd="/project")
        self.assertEqual([r[0] for r in self.db.execute("SELECT thread_id FROM conversation_search_routing")], ["vendor"])
        before = self.path.read_bytes()
        self.assertEqual(self.snapshot(), cold)
        self.assertEqual(self.path.read_bytes(), before)
        selected = self.snapshot(cwd="/project")
        self.assertEqual(selected["threads"], [cold["threads"][1]])
        history = selected["threads"][0]
        self.assertEqual(len(history["messages"]), 200)
        self.assertTrue(history["history_truncated"])
        self.assertTrue(history["messages"][0]["text"].startswith("3x"))
        self.assertEqual(len(history["messages"][0]["text"]), 6000)
        self.assertTrue(history["messages"][0]["truncated"])
        with patch("agent_coord.conversation_search.messages", side_effect=AssertionError("Decoded unchanged history")):
            with self.db:
                sync_messages(self.db, cwd="/project")
            self.snapshot(cwd="/project")

    def test_updates_deletions_and_old_index_writers_cannot_leave_stale_routing(self):
        with self.db:
            sync_messages(self.db)
        self.save("vendor", {"historyUnavailable": True, "turns": []})
        # Older search code can drain its dirty queue, but cannot restore the
        # separately invalidated routing projection.
        with self.db:
            self.db.execute("DELETE FROM conversation_search_dirty")
            self.db.execute("UPDATE browser_inbox_wake SET paused=1 WHERE thread_id='vendor'")
        cold = self.snapshot(cwd="/project")
        self.assertEqual(cold["threads"][0]["history_status"], "partial")
        self.assertTrue(cold["threads"][0]["wake_paused"])
        with self.db:
            sync_messages(self.db, cwd="/project")
        self.assertEqual(self.snapshot(cwd="/project"), cold)
        with self.db:
            self.db.execute("DELETE FROM browser_history WHERE thread_id='vendor'")
        self.assertEqual(self.snapshot(cwd="/project")["threads"][0]["history_status"], "missing")

    def test_malformed_histories_preserve_fallback_semantics(self):
        for value in (None, [], {"turns": None}, {"turns": "odd"}, {"turns": 3}, {"turns": [{"items": 4}]}):
            with self.subTest(value=value):
                self.save("vendor", value)
                cold = self.snapshot(cwd="/project")
                with self.db:
                    sync_messages(self.db, cwd="/project")
                self.assertEqual(self.snapshot(cwd="/project"), cold)

    def test_legacy_search_tables_are_migrated_without_changing_their_shape(self):
        with self.db:
            self.db.execute("CREATE TABLE conversation_search_histories(thread_id TEXT PRIMARY KEY,status TEXT NOT NULL)")
            self.db.execute("INSERT INTO conversation_search_histories VALUES ('vendor','saved')")
            sync_messages(self.db)
            # This is how older installed runtimes write their projection.
            self.db.execute("INSERT OR REPLACE INTO conversation_search_histories VALUES ('vendor','saved')")
        self.assertEqual(self.snapshot()["threads"][1]["history_status"], "saved")


if __name__ == "__main__":
    unittest.main()
