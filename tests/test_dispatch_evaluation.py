from __future__ import annotations

import copy
import json
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))
import test_dispatch_runtime as runtime_tests
from agent_coord import cli
from agent_coord.attention import ClassificationError
from agent_coord.dispatch_evaluation import evaluate, label_request, lifecycle_timings, read_records, replay_record
from agent_coord.dispatch_routing import RoutingPolicy, route
from agent_coord.store import CoordinationError


class EvaluationTests(unittest.TestCase):
    setUp = runtime_tests.DispatchRuntimeTests.setUp
    create = runtime_tests.DispatchRuntimeTests.create
    submit = runtime_tests.DispatchRuntimeTests.submit
    drain = runtime_tests.DispatchRuntimeTests.drain
    finish = runtime_tests.DispatchRuntimeTests.finish

    def captured(self, key="one", *, confidence=.62, winning=.66, crash=False):
        identity = self.submit(key=key)

        def classifier(state, config, policy):
            # The evidence must survive even if the process dies inside the network call.
            with self.store._connection() as db:
                saved = json.loads(db.execute("SELECT evidence_json FROM dispatch_attempts WHERE request_id=?", (identity,)).fetchone()[0])
            self.assertEqual(saved["input"]["state"], state)
            self.assertNotIn("retrieval", state)  # logging must not change the Jev prompt
            if crash:
                raise RuntimeError("Simulated termination during classification")
            chosen = next(c["key"] for c in state["candidates"] if c["thread_id"] == self.vendor)
            probabilities = {c["key"]: 0.0 for c in state["candidates"]}
            probabilities.update(new=0., dispatcher=1 - winning)
            probabilities[chosen] = winning
            return {"type": "choice", "choice": chosen, "confidence": confidence, "probabilities": probabilities}

        def actual(snapshot, request, config, **kwargs):
            return route(snapshot, request, config, classifier=classifier, **kwargs)

        with patch("agent_coord.dispatch_runtime.route", side_effect=actual):
            self.assertTrue(self.runtime.process_once())
        return identity

    def records(self, **kwargs):
        return read_records(self.store.database_path, self.dispatcher, **kwargs)

    def test_stage_timings_survive_without_changing_classifier_input(self):
        self.captured()
        self.drain(self.dispatcher)
        report = evaluate(self.store.database_path, self.dispatcher)
        record = report["records"][0]
        expected = {"request_context", "index_refresh", "snapshot", "configuration",
                    "ranking", "evidence_persist", "classifier", "decision"}
        self.assertEqual(set(record["evidence"]["timings_ms"]), expected)
        self.assertTrue(all(t >= 0 for t in record["evidence"]["timings_ms"].values()))
        self.assertNotIn("timings_ms", record["evidence"]["input"])
        self.assertEqual(record["attempts"][0]["evidence"], record["evidence"])
        for name in expected | {"intake_wait", "queue_wait", "provider_submit", "intake_to_accept"}:
            self.assertEqual(report["summary"]["stage_latency_ms"][name]["count"], 1)
        events = record["outcome"]["events"]
        self.assertTrue(all(e["transition_at"] for e in events))
        self.assertEqual([e["transition_at"] for e in events], sorted(e["transition_at"] for e in events))

    def test_direct_bypass_does_not_refresh_or_classify(self):
        self.submit(askDispatcher=True)
        with patch("agent_coord.dispatch_runtime.sync_messages", side_effect=AssertionError("Refreshed bypass")):
            self.runtime.process_once()
        evidence = self.records()["records"][0]["evidence"]
        self.assertEqual(set(evidence["timings_ms"]), {"request_context"})
        self.assertNotIn("input", evidence)

    def test_lifecycle_intervals_use_transition_times_and_skip_ambiguous_or_old_events(self):
        record = {"created_at": 100, "outcome": {"events": [
            {"state": "routing", "transition_at": 101},
            {"state": "routing", "transition_at": 103},
            {"state": "queued", "transition_at": 104},
            {"state": "sending", "transition_at": 110},
            {"state": "accepted", "transition_at": 111}]}}
        self.assertEqual(lifecycle_timings(record), {"intake_wait": 1000, "queue_wait": 6000,
                                                   "provider_submit": 1000, "intake_to_accept": 11000})
        record["outcome"]["events"].append({"state": "sending", "transition_at": 112})
        self.assertNotIn("provider_submit", lifecycle_timings(record))
        self.assertNotIn("queue_wait", lifecycle_timings(record))
        for e in record["outcome"]["events"]:
            del e["transition_at"]
        self.assertEqual(lifecycle_timings(record), {})

    def test_complete_input_and_all_ranks_survive_history_changes(self):
        for n in range(10):
            self.create("Other topic " + str(n))
        self.captured()
        record = self.records()["records"][0]
        evidence = record["evidence"]
        self.assertEqual(len(evidence["input"]["state"]["candidates"]), 8)
        self.assertEqual(len(evidence["retrieval"]), 11)
        self.assertEqual(sum(r["selected"] for r in evidence["retrieval"]), 8)
        self.assertEqual(len(evidence["input_sha256"]), 64)
        self.assertEqual(record["attempts"][0]["evidence"], evidence)
        with self.store._connection() as db:
            db.execute("UPDATE work_threads SET title='Changed after routing' WHERE thread_id=?", (self.vendor,))
        self.assertEqual(self.records()["records"][0]["evidence"], evidence)

    def test_unfinished_attempt_is_preserved_and_retry_appends(self):
        identity = self.captured(crash=True)
        first = self.records()["records"][0]
        self.assertEqual(first["outcome"]["state"], "failed")
        self.assertEqual(replay_record(first)["reason"], "classifier_answer_not_recorded")
        self.runtime.update(identity, "pending")
        self.captured()  # same intake key, second planning attempt
        record = self.records()["records"][0]
        self.assertEqual(len(record["attempts"]), 2)
        self.assertNotIn("answer", record["attempts"][0]["evidence"])
        self.assertEqual(replay_record(record)["status"], "replayed")

    def test_threshold_replay_uses_saved_answer_without_any_provider_or_store_writes(self):
        self.captured()
        before = self.sessions.rpc.calls[:]
        with self.store._connection() as db:
            dump = list(db.iterdump())
        with patch("agent_coord.dispatch_evaluation.classify", side_effect=AssertionError("Network called")):
            baseline = evaluate(self.store.database_path, self.dispatcher)
            changed = evaluate(self.store.database_path, self.dispatcher, overrides={"min_confidence": .6, "min_probability": .65})
        self.assertEqual(baseline["summary"]["fallback"], 1)
        self.assertEqual(changed["summary"]["automatic"], 1)
        self.assertEqual(changed["records"][0]["replay"]["decision"]["thread_id"], self.vendor)
        self.assertEqual(changed["delivered_messages"], 0)
        self.assertEqual(self.sessions.rpc.calls, before)
        with self.store._connection() as db:
            self.assertEqual(list(db.iterdump()), dump)

    def test_live_replay_uses_saved_question_or_explicit_prompt_override(self):
        self.captured()
        record = self.records()["records"][0]
        question = record["evidence"]["input"]["questions"]["destination"]
        question["instructions"] = "Historical instructions from an earlier version"
        record["attempts"][0]["evidence"] = copy.deepcopy(record["evidence"])
        original = copy.deepcopy(record)
        calls = []
        def classifier(state, config, policy, **kwargs):
            calls.append((state, policy, kwargs["question"]))
            return record["evidence"]["answer"]
        replay_record(record, live=True, classifier=classifier)
        self.assertEqual(calls[-1][2], question)
        result = replay_record(record, live=True, classifier=classifier, instructions="Experimental prompt", overrides={"model": "jev-experiment"})
        self.assertEqual(calls[-1][2]["instructions"], "Experimental prompt")
        self.assertEqual(result["evaluated_input"]["model"], "jev-experiment")
        self.assertEqual(record, original)

    def test_live_error_does_not_replace_recorded_evidence(self):
        self.captured()
        original = self.records()
        with patch("agent_coord.dispatch_evaluation.load_config", return_value={"enabled": True}), patch(
                "agent_coord.dispatch_evaluation.classify", side_effect=ClassificationError("request_failed")):
            result = evaluate(self.store.database_path, self.dispatcher, live=True)
        self.assertEqual(result["summary"]["errors"], 1)
        self.assertEqual(self.records(), original)

    def test_labels_are_append_only_and_separate_user_from_review(self):
        self.captured()
        before = self.sessions.rpc.calls[:]
        label_request(self.store.database_path, self.dispatcher, "one", "new", source="review")
        label_request(self.store.database_path, self.dispatcher, "one", "reuse", target_id=self.vendor, source="user", note="User confirmed")
        label_request(self.store.database_path, self.dispatcher, "one", "dispatcher", source="review", note="Corrected review")
        report = evaluate(self.store.database_path, self.dispatcher)
        self.assertEqual(len(report["records"][0]["labels"]), 3)
        self.assertEqual(report["summary"]["labels"]["user"]["correct"], 0)
        self.assertEqual(report["summary"]["labels"]["user"]["retrieval_hits"], 1)
        self.assertEqual(report["summary"]["labels"]["review"]["correct"], 1)
        self.assertEqual(self.sessions.rpc.calls, before)
        report = evaluate(self.store.database_path, self.dispatcher, overrides={"min_confidence": .6, "min_probability": .65})
        self.assertEqual(report["summary"]["labels"]["user"]["correct"], 1)
        self.assertEqual(report["summary"]["labels"]["review"]["wrong_automatic"], 1)

    def test_retrieval_miss_keeps_expected_rank(self):
        for n in range(10):
            self.create("Unrelated " + str(n))
        self.captured()
        last = self.records()["records"][0]["evidence"]["retrieval"][-1]
        label_request(self.store.database_path, self.dispatcher, "one", "reuse", target_id=last["thread_id"], source="review")
        label = evaluate(self.store.database_path, self.dispatcher)["records"][0]["replay"]["labels"]["review"]
        self.assertFalse(label["retrieval_hit"])
        self.assertEqual(label["expected_retrieval_rank"], 11)

    def test_labels_validate_request_and_destination_without_delivery(self):
        self.captured()
        for request_id, action, target in [("missing", "new", None), ("one", "reuse", None),
                                            ("one", "reuse", "absent"), ("one", "reuse", self.dispatcher), ("one", "new", self.vendor)]:
            with self.assertRaises(CoordinationError):
                label_request(self.store.database_path, self.dispatcher, request_id, action, target_id=target)
        self.assertEqual(self.records()["records"][0]["labels"], [])

    def test_llm_handoffs_match_provider_turn_but_are_not_labels(self):
        self.captured(); self.drain(self.dispatcher)
        record = self.records()["records"][0]
        turn = record["outcome"]["turn_id"]
        self.store.send_message(sender_session_id=self.dispatcher, recipient_session_id=self.vendor,
                                classification="action_required", reply_required=False, body="Status question")
        with self.store._connection() as db:
            db.execute("UPDATE messages SET sender_turn_id=? WHERE sender_session_id=?", (turn, self.dispatcher))
        self.finish(self.dispatcher)
        result = evaluate(self.store.database_path, self.dispatcher)
        self.assertEqual(result["summary"]["llm_agreements"], 1)
        self.assertEqual(result["summary"]["labels"]["user"]["count"], 0)
        self.assertEqual(result["records"][0]["outcome"]["provider_completion"]["status"], "completed")
        # A second request in the same provider turn must not inherit a confident handoff attribution.
        second = self.submit(key="two")
        self.runtime.update(second, "accepted", target_id=self.dispatcher, turn_id=turn)
        result = evaluate(self.store.database_path, self.dispatcher)
        self.assertEqual(result["records"][0]["outcome"]["handoff_attribution"], "shared_turn")
        self.assertEqual(result["summary"]["llm_comparisons"], 0)

    def test_pagination_and_legacy_evidence_are_explicit(self):
        first = self.captured()
        self.captured(key="two")
        with self.store._connection() as db:
            db.execute("DELETE FROM dispatch_attempts WHERE request_id=?", (first,))
            db.execute("UPDATE dispatch_requests SET evidence_json=? WHERE id=?", (json.dumps({"candidates": [self.vendor], "answer": {}}), first))
        page = evaluate(self.store.database_path, self.dispatcher, limit=1)
        self.assertEqual(page["next_cursor"], first)
        self.assertEqual(page["summary"]["unavailable"], 1)
        self.assertEqual(len(evaluate(self.store.database_path, self.dispatcher, after=first)["records"]), 1)

    def test_cli_evaluation_avoids_store_initialization_and_defaults_to_offline(self):
        self.captured()
        args = cli._parser().parse_args(["--db", str(self.store.database_path), "thread", "dispatch-evaluate", "--session-id", self.dispatcher])
        with patch("agent_coord.cli.CoordinationStore", side_effect=AssertionError("Store initialized")), patch(
                "agent_coord.dispatch_evaluation.classify", side_effect=AssertionError("Network called")):
            self.assertEqual(cli.run(args)["summary"]["replayed"], 1)

    def test_empty_database_and_invalid_flags_never_create_data(self):
        path = self.root / "empty.sqlite3"
        sqlite3.connect(path).close()
        before = path.read_bytes()
        self.assertEqual(evaluate(path, "dispatcher")["summary"]["records"], 0)
        self.assertEqual(path.read_bytes(), before)
        for overrides in ({"name": "baseline"}, {"model": "jev-experiment"}):
            with self.assertRaises(CoordinationError):
                evaluate(path, "dispatcher", overrides=overrides)
        with self.assertRaises(ValueError):
            evaluate(path, "dispatcher", overrides={"min_confidence": 1.1})
        with self.assertRaises(sqlite3.OperationalError):
            evaluate(self.root / "missing.sqlite3", "dispatcher")
        self.assertFalse((self.root / "missing.sqlite3").exists())

    def test_direct_request_is_recorded_without_classifier_input(self):
        self.submit(askDispatcher=True)
        with patch("agent_coord.dispatch_runtime.route", side_effect=AssertionError("Jev called")):
            self.runtime.process_once()
        record = self.records()["records"][0]
        self.assertEqual(record["evidence"]["reason"], "direct_request")
        self.assertEqual(replay_record(record)["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
