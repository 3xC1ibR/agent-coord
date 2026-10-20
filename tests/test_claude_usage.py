from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from test_browser_providers import CoordinationStore
from agent_coord.claude_code import ClaudeRPC
from agent_coord.claude_usage import ClaudeUsage, backfill_usage, normalize_usage


def usage(input=10, output=5, **extra):
    return {"input_tokens": input, "output_tokens": output,
            "cache_read_input_tokens": 20, "cache_creation_input_tokens": 2, **extra}


def model_usage(input=10, output=5):
    return {"inputTokens": input, "outputTokens": output,
            "cacheReadInputTokens": 20, "cacheCreationInputTokens": 2}


class ClaudeUsageTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = CoordinationStore(self.root / "state.sqlite3")
        self.store.register(session_id="thread", client="claude", cwd=str(self.root))
        self.events = []
        self.rpc = ClaudeRPC(self.store, self.events.append)
        self.collector = self.rpc.usage

    def result(self, uuid="result", **values):
        return {"type": "result", "subtype": "success", "session_id": "thread", "uuid": uuid,
                "usage": usage(), **values}

    def write(self, filename, entries):
        path = self.root / filename
        path.write_text("".join(json.dumps(e) + "\n" for e in entries))
        return path

    def assistant(self, mid, model="opus", **values):
        return {"type": "assistant", "sessionId": "thread", "message": {
            "id": mid, "model": model, "usage": usage(**values), "content": "PRIVATE"}}

    def fork(self, entries):
        self.store.register(session_id="parent", client="claude", cwd=str(self.root))
        path = self.write("fork.jsonl", entries)
        with self.store._connection() as db:
            db.execute("INSERT INTO claude_sessions(thread_id,fork_path) VALUES (?,?)", ("thread", str(path)))
            db.execute("UPDATE work_threads SET forked_from_thread_id='parent' WHERE thread_id='thread'")
        return path

    def samples(self):
        with self.store._connection() as db:
            return [dict(row) for row in db.execute("SELECT * FROM thread_usage_samples WHERE thread_id='thread'")]

    def test_normalization_preserves_unknown_and_thinking_subset(self):
        self.assertEqual(normalize_usage({"input_tokens": 3}), {})
        counted = normalize_usage(usage(output_tokens_details={"thinking_tokens": 3}))
        self.assertEqual(counted["input_tokens"], 32)
        self.assertEqual(counted["total_tokens"], 37)
        self.assertEqual(counted["reasoning_output_tokens"], 3)
        self.assertNotIn("output_tokens", normalize_usage(usage(output=True)))

    def test_capture_before_ui_filters_and_durable_replay_after_restart(self):
        event = self.result(result="PRIVATE")
        self.rpc._message("thread", event)  # No active display turn.
        resumed = CoordinationStore(self.root / "state.sqlite3")
        ClaudeUsage(resumed).capture("thread", event)
        summary = resumed.usage.summary("thread")
        self.assertEqual(summary["totals"]["total_tokens"], 37)
        self.assertEqual(len(self.samples()), 1)
        self.assertEqual(self.events[-1]["method"], "thread/usage/updated")
        self.assertNotIn("PRIVATE", json.dumps(self.samples()))

    def test_background_result_does_not_complete_unrelated_ui_turn(self):
        self.rpc.turns["thread"] = {"id": "ui-turn", "items": [{"id": "ui-prompt"}], "status": "inProgress"}
        self.rpc._message("thread", self.result(user_message_uuids=["background-prompt"]))
        self.assertIn("thread", self.rpc.turns)
        self.assertEqual(self.store.usage.summary("thread")["totals"]["output_tokens"], 5)

    def test_assistant_placeholders_and_nested_results_are_not_added(self):
        self.collector.capture("thread", self.assistant("step"))
        self.collector.capture("thread", self.result(parent_tool_use_id="native-child"))
        nested_reset = str(uuid.uuid4())
        self.collector.capture("thread", {"type": "conversation_reset", "new_conversation_id": nested_reset,
            "parent_tool_use_id": "native-child"})
        self.assertEqual(self.samples(), [])
        self.assertIsNone(self.store.usage.epoch_position("thread", provider="claude", native_id=nested_reset))

    def test_direct_and_child_inclusive_are_alternatives_with_real_models(self):
        self.collector.capture("thread", self.result(modelUsage={"opus": model_usage(30, 10), "haiku": model_usage(2, 1)}))
        self.collector.capture("thread", self.result("second", modelUsage={"opus": model_usage(40, 15), "haiku": model_usage(2, 1)}))
        summary = self.store.usage.summary("thread")
        self.assertEqual(summary["scope"], "including_native_children")
        self.assertEqual(summary["totals"]["total_tokens"], 102)
        self.assertEqual({m["model"] for m in summary["models"]}, {"opus", "haiku"})

    def test_fork_baseline_subtracted_per_model_not_from_direct_deltas(self):
        self.fork([{"type": "cost-state", "modelUsage": {"opus": model_usage(100, 20)}}])
        self.collector.capture("thread", self.result(usage=usage(2, 3),
            modelUsage={"opus": model_usage(110, 30), "haiku": model_usage(1, 1)}))
        summary = self.store.usage.summary("thread")
        self.assertEqual(summary["totals"]["total_tokens"], 44)
        direct = next(json.loads(r["totals_json"]) for r in self.samples() if r["scope"] == "direct")
        self.assertEqual(direct["total_tokens"], 27)
        opus = next(m for m in summary["models"] if m["model"] == "opus")
        self.assertEqual(opus["totals"]["total_tokens"], 20)

    def test_missing_fork_baseline_keeps_only_direct_observed_turns(self):
        self.fork([self.assistant("inherited", input=100)])
        self.collector.capture("thread", self.result(modelUsage={"opus": model_usage(1000, 100)}))
        summary = self.store.usage.summary("thread")
        self.assertEqual(summary["scope"], "direct")
        self.assertEqual(summary["totals"]["total_tokens"], 37)
        self.assertIn("baseline", summary["note"])

    def test_crash_zero_never_erases_previous_counts_and_missing_id_not_summed(self):
        self.collector.capture("thread", self.result(modelUsage={"opus": model_usage()}))
        self.collector.capture("thread", self.result("crash", subtype="error_during_execution",
            usage=usage(0, 0, cache_read_input_tokens=0, cache_creation_input_tokens=0),
            modelUsage={"opus": {"inputTokens": 0, "outputTokens": 0, "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0}}))
        self.collector.capture("thread", self.result(uuid=None))
        self.assertEqual(self.store.usage.summary("thread")["totals"]["total_tokens"], 37)
        self.assertEqual(len([r for r in self.samples() if r["kind"] == "delta"]), 1)

    def test_transcript_dedup_and_canonical_rewrite_remove_old_model(self):
        path = self.write("transcript.jsonl", [self.assistant("one"), self.assistant("one"), self.assistant("two", "haiku")])
        self.assertEqual(backfill_usage(self.store, "thread", path)["totals"]["total_tokens"], 74)
        path.write_text(json.dumps(self.assistant("one")) + "\n{unfinished")
        summary = backfill_usage(self.store, "thread", path)
        self.assertEqual(summary["totals"]["total_tokens"], 37)
        self.assertEqual([m["model"] for m in summary["models"]], ["opus"])

    def test_fork_transcript_excludes_copied_ids_even_when_native_rewrites_session(self):
        inherited = self.assistant("copied", input=100)
        self.fork([inherited])
        path = self.write("child.jsonl", [inherited, self.assistant("new")])
        summary = backfill_usage(self.store, "thread", path)
        self.assertEqual(summary["totals"]["total_tokens"], 37)

    def test_unknown_model_and_missing_counters_are_not_invented(self):
        a = self.assistant("known")
        b = self.assistant("unknown", None)
        b["message"]["usage"].pop("output_tokens")
        summary = backfill_usage(self.store, "thread", self.write("mixed.jsonl", [a, b]))
        self.assertEqual(summary["totals"]["input_tokens"], 64)
        self.assertNotIn("total_tokens", summary["totals"])
        self.assertNotIn("output_tokens", summary["totals"])

    def test_native_identity_mapping_backfill_and_app_event_identity(self):
        identity = str(uuid.uuid4())
        with self.store._connection() as db:
            db.execute("INSERT INTO claude_sessions(thread_id,native_id) VALUES (?,?)", ("thread", identity))
        config = self.root / "claude"
        folder = config / "projects/workspace"
        folder.mkdir(parents=True)
        message = self.assistant("own")
        message["sessionId"] = identity
        (folder / f"{identity}.jsonl").write_text(json.dumps(message) + "\n")
        with patch.dict("os.environ", {"CLAUDE_CONFIG_DIR": str(config)}):
            summary = backfill_usage(self.store, "thread")
        self.assertEqual(summary["totals"]["total_tokens"], 37)
        self.assertEqual({r["native_id"] for r in self.samples()}, {identity})
        self.collector.capture("thread", self.result(session_id=identity))
        self.assertEqual(self.events[-1]["params"]["threadId"], "thread")

    def test_wrong_transcript_and_unidentified_messages_are_not_attributed(self):
        wrong = self.assistant("wrong")
        wrong["sessionId"] = "other"
        no_id = self.assistant("unidentified")
        no_id.pop("sessionId")
        summary = backfill_usage(self.store, "thread", self.write("foreign.jsonl", [wrong, no_id]))
        self.assertEqual(summary["totals"], {})

    def test_native_reset_identity_is_durable_without_changing_app_id(self):
        with self.store._connection() as db:
            db.execute("INSERT INTO claude_sessions(thread_id,started) VALUES ('thread',1)")
        self.rpc._message("thread", {"type": "system", "subtype": "init", "session_id": "reset-native"})
        with self.store._connection() as db:
            self.assertEqual(db.execute("SELECT native_id FROM claude_sessions WHERE thread_id='thread'").fetchone()[0], "reset-native")
        self.collector.capture("thread", self.result(session_id="reset-native"))
        self.assertEqual(self.events[-1]["params"]["threadId"], "thread")

    def test_explicit_native_reset_sums_disjoint_epochs_and_survives_restart(self):
        new_id = str(uuid.uuid4())
        with self.store._connection() as db:
            db.execute("INSERT INTO claude_sessions(thread_id,native_id) VALUES ('thread','thread')")
        def models(tokens):
            return {"opus": {"inputTokens": tokens, "outputTokens": 0, "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0}}
        self.collector.capture("thread", self.result(modelUsage=models(500)))
        reset = {"type": "conversation_reset", "new_conversation_id": new_id, "trigger": "clear"}
        self.rpc._message("thread", reset)
        self.rpc._message("thread", reset)  # Replayed reset is idempotent.
        self.collector.capture("thread", self.result("new-period", session_id=new_id, modelUsage=models(100)))
        self.assertEqual(self.store.usage.summary("thread")["totals"]["total_tokens"], 600)
        resumed = CoordinationStore(self.root / "state.sqlite3")
        ClaudeUsage(resumed).capture("thread", self.result("new-period", session_id=new_id, modelUsage=models(100)))
        self.assertEqual(resumed.usage.summary("thread")["totals"]["total_tokens"], 600)

    def test_identity_alias_without_reset_is_never_summed_as_fresh_epoch(self):
        with self.store._connection() as db:
            db.execute("INSERT INTO claude_sessions(thread_id,native_id) VALUES ('thread','thread')")
        def models(tokens):
            return {"opus": {"inputTokens": tokens, "outputTokens": 0, "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0}}
        self.collector.capture("thread", self.result(modelUsage=models(500)))
        self.collector.capture("thread", self.result("alias", session_id="possible-alias", modelUsage=models(100)))
        summary = self.store.usage.summary("thread")
        self.assertEqual(summary["totals"]["total_tokens"], 500)
        self.assertEqual(summary["status"], "partial")
        self.assertIn("one identity", summary["note"])

    def test_fork_baseline_not_reapplied_after_verified_reset(self):
        new_id = str(uuid.uuid4())
        self.fork([{"type": "cost-state", "modelUsage": {"opus": model_usage(1000, 20)}}])
        self.collector.capture("thread", {"type": "conversation_reset", "new_conversation_id": new_id, "trigger": "clear"})
        models = {"opus": {"inputTokens": 100, "outputTokens": 0, "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0}}
        self.collector.capture("thread", self.result(session_id=new_id, modelUsage=models))
        self.assertEqual(self.store.usage.summary("thread")["totals"]["total_tokens"], 100)

    def test_bad_telemetry_and_storage_failure_never_break_display(self):
        for event in [self.result(usage=[]), self.result(modelUsage=[1]), self.result(usage=usage(-1, -2))]:
            self.collector.capture("thread", event)
        with patch.object(self.store.usage, "record_delta", side_effect=RuntimeError("disk")):
            self.rpc._message("thread", self.result())
        self.assertNotIn("thread", self.rpc.turns)
