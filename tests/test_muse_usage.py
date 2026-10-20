from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from test_browser_providers import CoordinationStore
from agent_coord.muse_code import MuseRPC
from agent_coord.muse_usage import MuseUsage, backfill_usage


class MuseUsageTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = CoordinationStore(self.root / "state.sqlite3")
        self.store.register(session_id="thread", client="muse", cwd=str(self.root))
        self.events = []
        self.rpc = MuseRPC(self.store, self.events.append)
        self.collector = self.rpc.usage
        self.rpc.native["thread"] = "native"
        self.generation = object()
        self.rpc.generations["thread"] = self.generation
        self.rpc.seen["thread"] = set()

    def params(self, cursor="opaque:z", **values):
        return {"sessionId": "native", "turnId": "finished-turn", "viewCursor": cursor,
                "sourceRange": {"first": cursor, "last": cursor}, "modelId": "muse-wonder-1.0",
                "promptTokens": 100, "totalTokens": 110,
                "usage": {"inputTokens": 100, "outputTokens": 10, "cachedTokens": 80,
                          "cacheReadTokens": 80, "cacheWriteTokens": 0, "reasoningTokens": 3},
                "cumulative": {"promptTokens": 100, "outputTokens": 10, "totalTokens": 110}, **values}

    def capture(self, params):
        self.collector.capture("thread", "native", "session/tokenUsage", params)

    def write(self, entries):
        path = self.root / "session.jsonl"
        path.write_text("".join(json.dumps(e) + "\n" for e in entries))
        return path

    def samples(self):
        with self.store._connection() as db:
            return [dict(r) for r in db.execute("SELECT * FROM thread_usage_samples")]

    def test_counted_once_prompt_and_replay_after_restart(self):
        event = self.params()
        self.capture(event)
        MuseUsage(CoordinationStore(self.root / "state.sqlite3")).capture("thread", "native", "session/tokenUsage", event)
        summary = self.store.usage.summary("thread")
        self.assertEqual(summary["totals"]["total_tokens"], 110)
        self.assertEqual(summary["scope"], "direct")
        self.assertEqual(len(self.samples()), 2)  # One delta, one alternate snapshot.
        self.assertEqual(self.events[-1]["method"], "thread/usage/updated")

    def test_capture_before_active_turn_filter_with_generation_and_native_guards(self):
        message = {"method": "session/tokenUsage", "params": self.params()}
        self.rpc._message("thread", object(), message)
        self.assertEqual(self.samples(), [])
        self.rpc._message("thread", self.generation, {**message, "params": self.params(sessionId="foreign")})
        self.assertEqual(self.samples(), [])
        self.rpc._message("thread", self.generation, message)
        self.assertEqual(self.store.usage.summary("thread")["totals"]["total_tokens"], 110)

    def test_opaque_cursor_is_not_lexically_sorted_and_model_switch_is_preserved(self):
        self.capture(self.params("z", cumulative={"promptTokens": 100, "outputTokens": 10, "totalTokens": 110}))
        self.capture(self.params("a", modelId="new-model", cumulative={"promptTokens": 200, "outputTokens": 20, "totalTokens": 220}))
        # Equal alternate totals may select cumulative or completion source.
        self.assertEqual(self.store.usage.summary("thread")["totals"]["total_tokens"], 220)
        deltas = [r for r in self.samples() if r["kind"] == "delta"]
        self.assertEqual({r["model"] for r in deltas}, {"muse-wonder-1.0", "new-model"})

    def test_turn_terminal_context_quota_and_transitive_children_not_added(self):
        self.capture(self.params())
        for method, params in [
            ("turn/completed", {"usage": {"outputTokens": 10000}}),
            ("usage/changed", {"usage": {"outputTokens": 10000}}),
            ("item/completed", {"item": {"itemId": "child", "kind": "subagent", "usage": {"outputTokens": 10000}}}),
        ]:
            self.collector.capture("thread", "native", method, {"sessionId": "native", **params})
        self.collector.capture("thread", "native", "session/contextUsage",
            {"sessionId": "native", "usedTokens": 1000, "windowTokens": 2000})
        summary = self.store.usage.summary("thread")
        self.assertEqual(summary["totals"]["total_tokens"], 110)
        self.assertEqual(summary["context"], {"used_tokens": 1000, "limit_tokens": 2000})
        self.assertEqual(summary["scope"], "direct")

    def test_transitive_child_observations_preserved_without_fabricated_inclusive_totals(self):
        params = {"sessionId": "native", "item": {"itemId": "child", "revision": 2,
            "kind": "subagent", "usage": {"inputTokens": 100, "outputTokens": 20, "cachedTokens": 50}}}
        for _ in range(2):
            self.collector.capture("thread", "native", "item/updated", params)
        self.assertEqual(len(self.samples()), 1)
        self.assertEqual(self.store.usage.summary("thread")["totals"], {})
        self.capture(self.params())
        self.assertEqual(self.store.usage.summary("thread")["totals"]["total_tokens"], 110)
        child = next(row for row in self.samples() if row["source_key"] == "muse:child-observations")
        self.assertEqual(json.loads(child["raw_json"])["transitive"], 1)
        self.assertEqual(json.loads(child["raw_json"])["usage"]["outputTokens"], 20)

    def test_snapshot_absence_unknown_and_snapshot_total_backfill(self):
        self.collector.snapshot("thread", "native", {"session": {"sessionId": "native"},
            "history": {"snapshot": {"state": {"items": []}}}})
        self.assertEqual(self.samples(), [])
        self.collector.snapshot("thread", "native", {"session": {"sessionId": "native"},
            "history": {"snapshot": {"state": {"tokenUsage": {"promptTokens": 10, "outputTokens": 2, "totalTokens": 12}}}}})
        self.assertEqual(self.store.usage.summary("thread")["totals"]["total_tokens"], 12)
        self.assertEqual(self.store.usage.summary("thread")["status"], "available")

    def test_verified_root_cumulative_complete_but_child_session_not_promoted(self):
        self.collector.snapshot("thread", "native", {"session": {"sessionId": "native"}})
        self.capture(self.params())
        self.assertEqual(self.store.usage.summary("thread")["status"], "available")
        collector = MuseUsage(self.store)
        collector.snapshot("thread", "child", {"session": {"sessionId": "child", "kind": "subagent", "parentSessionId": "native"}})
        self.assertNotIn(("thread", "child"), collector.verified_roots)

    def test_missing_counted_once_fields_does_not_add_cache_again(self):
        self.capture(self.params(cumulative={}, promptTokens=None, totalTokens=None, modelId=None))
        summary = self.store.usage.summary("thread")
        self.assertEqual(summary["totals"]["output_tokens"], 10)
        self.assertNotIn("input_tokens", summary["totals"])
        self.assertNotIn("total_tokens", summary["totals"])

    def test_malformed_missing_identity_and_storage_failure_are_nonfatal(self):
        for event in [self.params(viewCursor=None, sourceRange=None), self.params(usage=[]),
                      self.params(cumulative="invalid"), self.params(promptTokens=True)]:
            self.capture(event)
        with patch.object(self.store.usage, "record_delta", side_effect=RuntimeError("disk")):
            self.capture(self.params("another"))

    def test_msp_backfill_replay_uses_native_mapping_without_starting_host(self):
        with self.store._connection() as db:
            db.execute("INSERT INTO muse_sessions VALUES ('thread','native',?)", (str(self.root),))
        event = {"method": "session/tokenUsage", "params": self.params()}
        path = self.write([event, event])
        for _ in range(2):
            summary = backfill_usage(self.store, "thread", path)
        self.assertEqual(summary["totals"]["total_tokens"], 110)
        self.assertEqual(len(self.samples()), 2)

    def test_raw_log_backfill_ignores_mirrored_goal_events_and_foreign_streams(self):
        def entry(identity, kind="model_completed", native="thread"):
            return {"id": identity, "stream": {"kind": "session", "id": native},
                    "payload": {"kind": "run", "event": {"kind": kind, "model": "model",
                        "usage": {"input_tokens": 100, "cached_tokens": 50, "output_tokens": 10, "reasoning_tokens": 2}}}}
        path = self.write([entry("one"), entry("one"), entry("mirror", "goal_usage_attribution"), entry("foreign", native="other")])
        summary = backfill_usage(self.store, "thread", path)
        self.assertEqual(summary["totals"], {"output_tokens": 10, "reasoning_output_tokens": 2})
        self.assertEqual(summary["status"], "partial")

    def test_forked_msp_import_is_rejected_before_writing_parent_counts(self):
        path = self.write([{"method": "session/tokenUsage", "params": self.params(sessionId="thread")},
            {"session": {"sessionId": "thread", "forkedFrom": {"sessionId": "parent"}}}])
        summary = backfill_usage(self.store, "thread", path)
        self.assertEqual(summary["totals"], {})
        self.assertIn("baseline", summary["note"])

    def test_raw_log_canonical_rewrite_replaces_model_buckets(self):
        def row(identity, model):
            return {"id": identity, "stream": {"kind": "session", "id": "thread"},
                "payload": {"kind": "run", "event": {"kind": "model_completed", "model": model,
                    "usage": {"output_tokens": 10, "reasoning_tokens": 2}}}}
        path = self.write([row("one", "old"), row("two", "new")])
        self.assertEqual(backfill_usage(self.store, "thread", path)["totals"]["output_tokens"], 20)
        path.write_text(json.dumps(row("two", "new")) + "\n")
        summary = backfill_usage(self.store, "thread", path)
        self.assertEqual(summary["totals"]["output_tokens"], 10)
        self.assertEqual([m["model"] for m in summary["models"]], ["new"])

    def test_default_native_path_discovery_is_scoped_to_own_session(self):
        native = str(uuid.uuid4())
        with self.store._connection() as db:
            db.execute("INSERT INTO muse_sessions VALUES ('thread',?,?)", (native, str(self.root)))
        data = self.root / "data"
        folder = data / "muse/sessions/2026/10/20" / native
        folder.mkdir(parents=True)
        entry = {"id": "completion", "stream": {"kind": "session", "id": native}, "payload": {
            "kind": "run", "event": {"kind": "model_completed", "usage": {"output_tokens": 12}}}}
        (folder / "session.jsonl").write_text(json.dumps(entry) + "\n")
        with patch.dict("os.environ", {"XDG_DATA_HOME": str(data)}):
            summary = backfill_usage(self.store, "thread")
        self.assertEqual(summary["totals"]["output_tokens"], 12)

    def test_app_fork_without_native_provenance_is_still_unknown(self):
        self.store.register(session_id="parent", client="muse", cwd=str(self.root))
        with self.store._connection() as db:
            db.execute("UPDATE work_threads SET forked_from_thread_id='parent' WHERE thread_id='thread'")
        self.capture(self.params())
        self.assertEqual(self.store.usage.summary("thread")["totals"], {})
        self.assertIn("baseline", self.store.usage.summary("thread")["note"])
