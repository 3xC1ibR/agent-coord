from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))

from agent_coord.store import CoordinationError, CoordinationStore
from agent_coord.thread_usage import backfill_codex


def counts(total):
    return {"input_tokens": total - 2, "output_tokens": 2, "total_tokens": total}


class ThreadUsageTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.path = self.root / "state.sqlite3"
        self.store = CoordinationStore(self.path)
        self.store.register(session_id="one", client="codex", cwd=str(self.root))
        self.usage = self.store.usage

    def snapshot(self, total, **kwargs):
        return self.usage.record_snapshot("one", provider="codex", native_id="native",
                                         source_key="cumulative", totals=counts(total), **kwargs)

    def delta(self, event_id, total, **kwargs):
        return self.usage.record_delta("one", provider="codex", native_id="native",
                                      event_id=event_id, totals=counts(total), **kwargs)

    def test_snapshots_replay_out_of_order_restart_and_regression(self):
        self.snapshot(100, observed_at=10, coverage="complete")
        self.snapshot(200, observed_at=20, coverage="complete")
        self.snapshot(100, observed_at=10, coverage="complete")
        self.snapshot(200, observed_at=20, coverage="complete")
        recovered = CoordinationStore(self.path).usage.summary("one")
        self.assertEqual(recovered["totals"], counts(200))
        self.assertEqual(recovered["status"], "available")
        self.snapshot(10, observed_at=30, coverage="complete")
        self.assertEqual(self.usage.summary("one")["totals"], counts(200))
        self.assertEqual(self.usage.summary("one")["status"], "partial")

    def test_delayed_live_snapshot_can_advance_a_later_observed_backfill(self):
        self.snapshot(100, observed_at=30, coverage="complete")
        self.snapshot(200, observed_at=20, coverage="complete")
        self.assertEqual(self.usage.summary("one")["totals"], counts(200))
        self.assertEqual(self.usage.summary("one")["observed_at"], "1970-01-01T00:00:30+00:00")

    def test_deltas_deduplicate_after_crash_and_concurrent_replay(self):
        def record(_):
            self.delta("event-a", 10, model="model-a")
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(record, range(12)))
        self.store = CoordinationStore(self.path)
        self.usage = self.store.usage
        self.delta("event-a", 10, model="model-a")
        self.delta("event-a", 10, model="different-current-model")
        self.delta("event-b", 20, model="model-b")
        result = self.usage.summary("one")
        self.assertEqual(result["totals"]["total_tokens"], 30)
        self.assertEqual({m["model"]: m["totals"]["total_tokens"] for m in result["models"]},
                         {"model-a": 10, "model-b": 20})

    def test_alternate_sources_scopes_and_native_ids_are_not_added(self):
        self.snapshot(100, coverage="complete")
        self.delta("a", 50, source_key="events", coverage="complete")
        self.snapshot(120, scope="including_native_children", coverage="partial")
        self.usage.record_snapshot("one", provider="codex", native_id="other", source_key="other",
                                   totals=counts(60), coverage="complete")
        summary = self.usage.summary("one")
        self.assertEqual(summary["totals"]["total_tokens"], 120)
        self.assertEqual(summary["scope"], "including_native_children")
        self.assertEqual(summary["status"], "partial")

    def test_model_snapshots_sum_buckets_but_never_add_to_aggregate(self):
        self.snapshot(20, model="a")
        self.snapshot(30, model="b")
        self.snapshot(25, model="a")
        self.assertEqual(self.usage.summary("one")["totals"]["total_tokens"], 55)
        self.snapshot(100)
        self.assertEqual(self.usage.summary("one")["totals"]["total_tokens"], 100)
        self.assertEqual(len(self.usage.summary("one")["models"]), 2)

    def test_atomic_replacement_removes_disappearing_models_and_rejects_stale_scan(self):
        def replace(models, stamp):
            self.usage.replace_snapshots("one", provider="codex", native_id="native", source_key="log",
                                         models=models, coverage="complete", observed_at=stamp)
        replace([{"model": "a", "totals": counts(20)}, {"model": "b", "totals": counts(30)}], 10)
        replace([{"model": "b", "totals": counts(40)}], 20)
        replace([{"model": "a", "totals": counts(100)}], 15)
        summary = self.usage.summary("one")
        self.assertEqual(summary["totals"]["total_tokens"], 40)
        self.assertEqual([m["model"] for m in summary["models"]], ["b"])
        # Invalid bucket is rejected before deleting the good stream.
        with self.assertRaises(CoordinationError):
            replace([{"model": "bad", "totals": {"input_tokens": -1}}], 30)
        self.assertEqual(self.usage.summary("one")["totals"]["total_tokens"], 40)
        replace([], 40)
        replace([{"model": "a", "totals": counts(500)}], 35)
        self.assertEqual(self.usage.summary("one")["totals"], {})

    def test_unknown_counts_are_omitted_including_partial_model_sum(self):
        self.usage.record_snapshot("one", provider="codex", native_id="native", source_key="models",
                                   totals={"output_tokens": 4}, model="a")
        self.usage.record_snapshot("one", provider="codex", native_id="native", source_key="models",
                                   totals={"input_tokens": 10, "output_tokens": 6}, model="b")
        self.assertEqual(self.usage.summary("one")["totals"], {"output_tokens": 10})
        self.assertEqual(self.usage.summary("one")["status"], "partial")

    def test_validation_rejects_invalid_counts_and_raw_transcript_content(self):
        for bad in (-1, 1.5, True, "12", float("nan"), 2**63):
            with self.subTest(bad=bad), self.assertRaises(CoordinationError):
                self.usage.record_delta("one", provider="codex", native_id="native", event_id="a",
                                        totals={"input_tokens": bad})
        with self.assertRaises(CoordinationError):
            self.snapshot(10, raw={"message": "secret transcript content"})
        self.assertEqual(self.usage.summary("one")["totals"], {})

    def test_context_is_separate_latest_observation_and_not_consumption(self):
        self.usage.record_context("one", provider="codex", native_id="native", used=300, limit=1000, observed_at=20)
        self.usage.record_context("one", provider="codex", native_id="native", used=100, limit=1000, observed_at=10)
        summary = self.usage.summary("one")
        self.assertEqual(summary["status"], "unavailable")
        self.assertEqual(summary["totals"], {})
        self.assertEqual(summary["context"], {"used_tokens": 300, "limit_tokens": 1000})

    def test_fork_boundary_is_frozen_and_missing_fields_not_invented(self):
        self.usage.set_baseline("one", provider="codex", native_id="native", totals={"total_tokens": 100})
        self.snapshot(140)
        self.usage.set_baseline("one", provider="codex", native_id="native", totals={"total_tokens": 120})
        self.assertEqual(self.usage.summary("one")["totals"], {"total_tokens": 40})
        self.assertEqual(self.usage.raw_snapshot("one", provider="codex", native_id="native"), counts(140))

    def test_unknown_fork_baseline_hides_inherited_snapshot_but_not_direct_deltas(self):
        self.usage.set_baseline("one", provider="codex", native_id="native")
        self.snapshot(1000)
        self.assertEqual(self.usage.summary("one")["totals"], {})
        self.delta("child-request", 20)
        self.assertEqual(self.usage.summary("one")["totals"]["total_tokens"], 20)

    def test_provider_excluded_baseline_preserves_model_buckets(self):
        self.usage.set_baseline("one", provider="codex", native_id="native", totals={})
        self.snapshot(20, model="child-model")
        self.assertEqual(self.usage.summary("one")["totals"], counts(20))
        self.assertEqual(self.usage.summary("one")["models"][0]["model"], "child-model")

    def test_verified_native_reset_periods_add_lifetime_usage_and_replay_safely(self):
        self.snapshot(500, coverage="complete")
        self.usage.start_epoch("one", provider="codex", native_id="reset-native", previous_native_id="native")
        self.usage.record_snapshot("one", provider="codex", native_id="reset-native", source_key="cumulative",
                                   totals=counts(100), coverage="complete")
        self.assertEqual(self.usage.summary("one")["totals"]["total_tokens"], 600)
        self.assertEqual(self.usage.summary("one")["status"], "available")
        self.usage = CoordinationStore(self.path).usage
        self.usage.start_epoch("one", provider="codex", native_id="reset-native", previous_native_id="native")
        self.usage.record_snapshot("one", provider="codex", native_id="reset-native", source_key="cumulative",
                                   totals=counts(120), coverage="complete")
        self.assertEqual(self.usage.summary("one")["totals"]["total_tokens"], 620)
        # A higher overlapping delta stream replaces that native's snapshot only.
        self.usage.record_delta("one", provider="codex", native_id="reset-native", event_id="b",
                                totals=counts(140), coverage="partial")
        self.assertEqual(self.usage.summary("one")["totals"]["total_tokens"], 640)
        with self.assertRaises(CoordinationError):
            self.usage.start_epoch("one", provider="codex", native_id="branch", previous_native_id="native")

    def test_reset_period_preserves_old_fork_baseline_and_excludes_new_inheritance(self):
        self.usage.set_baseline("one", provider="codex", native_id="native", totals={"total_tokens": 1000})
        self.snapshot(1500)
        self.usage.start_epoch("one", provider="codex", native_id="reset", previous_native_id="native")
        self.usage.record_snapshot("one", provider="codex", native_id="reset", source_key="cumulative", totals=counts(100))
        self.assertEqual(self.usage.summary("one")["totals"], {"total_tokens": 600})

    def test_unverified_native_aliases_never_claim_complete_lifetime_totals(self):
        self.snapshot(500, coverage="complete")
        self.usage.record_snapshot("one", provider="codex", native_id="maybe-alias", source_key="cumulative",
                                   totals=counts(100), coverage="complete")
        summary = self.usage.summary("one")
        self.assertEqual(summary["totals"]["total_tokens"], 500)
        self.assertEqual(summary["status"], "partial")
        self.assertIn("one identity only", summary["note"])

    def transcript(self, events, identity="one"):
        path = self.root / "rollout.jsonl"
        with path.open("w") as stream:
            stream.write(json.dumps({"type": "session_meta", "payload": {"id": identity}}) + "\n")
            for event in events:
                stream.write(json.dumps(event) + "\n")
        return path

    def token_event(self, total):
        return {"type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": counts(total), "last_token_usage": counts(10), "model_context_window": 1000}}}

    def test_backfill_is_idempotent_bounded_and_identity_guarded(self):
        path = self.transcript([self.token_event(30), self.token_event(60), self.token_event(60)])
        for _ in range(2):
            summary = backfill_codex(self.store, "one", path)
            self.assertEqual(summary["totals"], counts(60))
            self.assertEqual(summary["status"], "available")
        foreign = self.transcript([self.token_event(1000)], identity="another-thread")
        summary = backfill_codex(self.store, "one", foreign)
        self.assertEqual(summary["totals"], counts(60))
        self.assertEqual(summary["status"], "partial")
        with patch("agent_coord.thread_usage.MAX_TRANSCRIPT_BYTES", 1):
            self.assertEqual(backfill_codex(self.store, "one", path)["totals"], counts(60))

    def test_truncated_transcript_keeps_observed_usage_and_marks_partial(self):
        path = self.transcript([self.token_event(60)])
        with path.open("a") as stream:
            stream.write('{"secret-user-text":')
        summary = backfill_codex(self.store, "one", path)
        self.assertEqual(summary["totals"], counts(60))
        self.assertEqual(summary["status"], "partial")
        with self.store._connection() as db:
            rows = db.execute("SELECT * FROM thread_usage_samples").fetchall()
            self.assertNotIn("secret-user-text", str([tuple(row) for row in rows]))

    def test_missing_telemetry_is_unavailable_and_durable(self):
        path = self.transcript([{"type": "event_msg", "payload": {"type": "message", "text": "private"}}])
        self.assertEqual(backfill_codex(self.store, "one", path)["status"], "unavailable")
        summary = CoordinationStore(self.path).usage.summary("one")
        self.assertIsNotNone(summary["observed_at"])
        self.assertNotIn("private", json.dumps(summary))

    def test_historical_fork_without_boundary_is_unavailable(self):
        path = self.root / "fork.jsonl"
        path.write_text(json.dumps({"type": "session_meta", "payload": {"id": "one", "forked_from_id": "parent"}})
                        + "\n" + json.dumps(self.token_event(10000)) + "\n")
        self.assertEqual(backfill_codex(self.store, "one", path)["totals"], {})

    def test_historical_fork_uses_exact_parent_prefix_not_later_requests(self):
        parent_id = "11111111-1111-4111-8111-111111111111"
        directory = self.root / "sessions"
        directory.mkdir()
        parent = directory / ("rollout-" + parent_id + ".jsonl")
        events = [{"type": "session_meta", "payload": {"id": parent_id}, "ordinal": 0},
                  {**self.token_event(1000), "ordinal": 1},
                  {"type": "event_msg", "payload": {"type": "task_complete"}, "ordinal": 2}]
        prefix = b"".join(json.dumps(event).encode()+b"\n" for event in events)
        parent.write_bytes(prefix + json.dumps({**self.token_event(5000), "ordinal": 3}).encode()+b"\n")
        path = self.root / "child.jsonl"
        metadata = {"id": "one", "forked_from_id": parent_id, "forked_from_ordinal_exclusive": 3,
                    "history_base": {"thread_id": parent_id, "end_ordinal_exclusive": 3,
                                     "end_byte_offset": len(prefix)}}
        path.write_text(json.dumps({"type": "session_meta", "payload": metadata, "ordinal": 3})
                        + "\n" + json.dumps({**self.token_event(1200), "ordinal": 4}) + "\n")
        with patch.dict("os.environ", {"CODEX_HOME": str(self.root)}):
            summary = backfill_codex(self.store, "one", path)
        self.assertEqual(summary["totals"]["total_tokens"], 200)
        self.assertEqual(CoordinationStore(self.path).usage.summary("one")["totals"]["total_tokens"], 200)

    def test_historical_fork_rejects_rewritten_parent_prefix(self):
        parent_id = "11111111-1111-4111-8111-111111111111"
        directory = self.root / "sessions"
        directory.mkdir()
        parent = directory / ("rollout-" + parent_id + ".jsonl")
        # Ordinal 1 was lost even though the final ordinal and offset exist.
        prefix = b"".join(json.dumps(event).encode()+b"\n" for event in [
            {"type": "session_meta", "payload": {"id": parent_id}, "ordinal": 0},
            {**self.token_event(500), "ordinal": 2}])
        parent.write_bytes(prefix)
        path = self.root / "child.jsonl"
        metadata = {"id": "one", "forked_from_id": parent_id, "forked_from_ordinal_exclusive": 3,
                    "history_base": {"thread_id": parent_id, "end_ordinal_exclusive": 3,
                                     "end_byte_offset": len(prefix)}}
        path.write_text(json.dumps({"type": "session_meta", "payload": metadata})
                        + "\n" + json.dumps(self.token_event(1200)) + "\n")
        with patch.dict("os.environ", {"CODEX_HOME": str(self.root)}):
            self.assertEqual(backfill_codex(self.store, "one", path)["totals"], {})

    def test_codex_native_index_is_read_only_and_rollout_identity_is_checked(self):
        thread_id = "11111111-1111-4111-8111-111111111111"
        self.store.register(session_id=thread_id, client="codex", cwd=str(self.root))
        path = self.transcript([self.token_event(80)], identity=thread_id)
        index = self.root / "state_5.sqlite"
        connection = sqlite3.connect(index)
        try:
            connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY,rollout_path TEXT)")
            connection.execute("INSERT INTO threads VALUES (?,?)", (thread_id, str(path)))
            connection.commit()
        finally:
            connection.close()
        before = index.read_bytes()
        with patch.dict("os.environ", {"CODEX_HOME": str(self.root)}):
            self.assertEqual(backfill_codex(self.store, thread_id)["totals"]["total_tokens"], 80)
        self.assertEqual(index.read_bytes(), before)
        self.transcript([self.token_event(2000)], identity="foreign")
        with patch.dict("os.environ", {"CODEX_HOME": str(self.root)}):
            self.assertEqual(backfill_codex(self.store, thread_id)["totals"]["total_tokens"], 80)


if __name__ == "__main__":
    unittest.main()
