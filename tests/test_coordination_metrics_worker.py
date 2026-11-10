from __future__ import annotations

from contextlib import closing
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))
from agent_coord import coordination_metrics_worker as worker


NOW = 1794268800.0


def event(identity="call-1", *, thread="thread-1", turn="turn-1", timestamp="2026-11-09T12:00:00Z",
          arguments=None, result="OK", tool="update_checkpoint"):
    item = {"type": "McpToolCall", "id": identity, "server": "agent-coord", "tool": tool,
            "arguments": {"summary": "Short", "context_lifecycle": {"reason": "Useful"}} if arguments is None else arguments,
            "status": "completed", "result": {"content": [{"type": "text", "text": result}]} if result is not None else None}
    return {"timestamp": timestamp, "type": "event_msg",
            "payload": {"type": "item_completed", "thread_id": thread, "turn_id": turn, "item": item}}


def command_event(command, identity="command-1", **kwargs):
    record = event(identity, **kwargs)
    record["payload"]["item"] = {"type": "CommandExecution", "id": identity,
        "command": ["/bin/zsh", "-lc", command], "aggregated_output": "receipt", "exit_code": 0}
    return record


class WorkerFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "codex"
        self.home.mkdir()
        self.state = self.root / "derived"
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.index = self.home / "state_5.sqlite"
        with closing(sqlite3.connect(self.index)) as connection, connection:
            connection.execute("CREATE TABLE threads(id TEXT,rollout_path TEXT,cwd TEXT,updated_at INTEGER)")
        self.tokenizer = worker.TokenCounter(lambda text: list(text), "test")

    def source(self, records, *, name="session.jsonl", thread="thread-1", cwd=None, newline=True):
        path = self.home / name
        lines = [json.dumps(record) if not isinstance(record, str) else record for record in records]
        path.write_text("\n".join(lines) + ("\n" if newline and lines else ""))
        with closing(sqlite3.connect(self.index)) as connection, connection:
            connection.execute("INSERT INTO threads VALUES (?,?,?,?)", (thread, str(path), str(cwd or self.workspace), int(NOW)))
        return path

    def run_worker(self, **kwargs):
        options = {"tokenizer": self.tokenizer, "now": NOW, "sleep": lambda _: None}
        options.update(kwargs)
        return worker.run_pass(self.state, self.home, **options)

    def snapshot(self, days="7"):
        return json.loads((self.state / "snapshot.json").read_text())["periods"][days]

    def database(self):
        connection = sqlite3.connect(self.state / "metrics.sqlite3")
        self.addCleanup(connection.close)
        return connection

    def test_incremental_replay_is_idempotent_and_contains_no_payload_text(self):
        path = self.source([event(arguments={"summary": "PRIVATE-CONTENT"})])
        source_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        index_hash = hashlib.sha256(self.index.read_bytes()).hexdigest()
        first = self.run_worker()
        self.assertEqual(first["state"], "ready")
        self.assertEqual(self.snapshot()["current"]["calls"], 1)
        self.assertEqual(self.run_worker()["progress"]["bytes_processed"], 0)
        self.assertEqual(self.snapshot()["current"]["calls"], 1)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), source_hash)
        self.assertEqual(hashlib.sha256(self.index.read_bytes()).hexdigest(), index_hash)
        self.assertNotIn(b"PRIVATE-CONTENT", (self.state / "metrics.sqlite3").read_bytes())
        self.assertNotIn("PRIVATE-CONTENT", (self.state / "snapshot.json").read_text())

    def test_append_and_partial_tail_resume(self):
        path = self.source([event()])
        self.run_worker()
        with path.open("a") as stream:
            stream.write(json.dumps(event("call-2", turn="turn-2")))
        progress = self.run_worker()
        self.assertEqual(self.snapshot()["current"]["calls"], 1)
        self.assertFalse(progress["progress"]["has_more"])
        self.assertEqual(progress["state"], "partial")
        with path.open("a") as stream:
            stream.write("\n")
        self.run_worker()
        self.assertEqual(self.snapshot()["current"]["calls"], 2)
        self.assertEqual(self.snapshot()["current"]["turns"], 2)

    def test_duplicate_and_fork_inheritance_are_not_double_counted(self):
        self.source([event(), event()], name="parent.jsonl")
        self.source([event(), event("child-call", thread="child")], name="child.jsonl", thread="child")
        self.run_worker()
        self.assertEqual(self.snapshot()["current"]["calls"], 2)
        coverage = json.loads((self.state / "snapshot.json").read_text())["coverage"]
        self.assertEqual(coverage["excluded_inherited_events"], 1)

    def test_truncation_removes_old_source_observations(self):
        path = self.source([event(), event("call-2")])
        self.run_worker()
        path.write_text(json.dumps(event("replacement")) + "\n")
        self.run_worker()
        self.assertEqual(self.snapshot()["current"]["calls"], 1)
        self.assertEqual(self.database().execute("SELECT call_id FROM calls").fetchone()[0], "mcp:replacement")

    def test_multiple_flushes_preserve_source_membership(self):
        path = self.source([event("call-" + str(i)) for i in range(210)])
        self.run_worker()
        self.assertEqual(self.database().execute("SELECT COUNT(*) FROM observations").fetchone()[0], 210)
        path.write_text(json.dumps(event("replacement")) + "\n")
        self.run_worker()
        self.assertEqual(self.snapshot()["current"]["calls"], 1)

    def test_replacement_preserves_an_event_observed_in_another_source(self):
        first = self.source([event()], name="first.jsonl")
        self.source([event()], name="second.jsonl")
        self.run_worker()
        replacement = first.with_suffix(".new")
        replacement.write_text(json.dumps(event("replacement")) + "\n")
        replacement.replace(first)
        self.run_worker()
        self.assertEqual(self.snapshot()["current"]["calls"], 2)

    def test_workspace_and_source_path_isolation(self):
        self.source([event()], name="included.jsonl")
        self.source([event("outside", thread="outside")], name="outside.jsonl", thread="outside", cwd=self.root / "elsewhere")
        external = self.root / "external.jsonl"
        external.write_text(json.dumps(event("external")) + "\n")
        with closing(sqlite3.connect(self.index)) as connection, connection:
            connection.execute("INSERT INTO threads VALUES (?,?,?,?)", ("thread-1", str(external), str(self.workspace), int(NOW)))
        self.run_worker(cwd=str(self.workspace))
        self.assertEqual(self.snapshot()["current"]["calls"], 1)
        snapshot = json.loads((self.state / "snapshot.json").read_text())
        self.assertEqual(snapshot["coverage"]["excluded_source_paths"], 1)

    def test_workspace_filter_precedes_inventory_cap(self):
        selected = self.source([event()], name="selected.jsonl")
        self.source([event("foreign-1", thread="foreign-1")], name="foreign-1.jsonl", thread="foreign-1", cwd=self.root / "foreign")
        self.source([event("foreign-2", thread="foreign-2")], name="foreign-2.jsonl", thread="foreign-2", cwd=self.root / "foreign")
        with closing(sqlite3.connect(self.index)) as connection, connection:
            connection.execute("UPDATE threads SET updated_at=? WHERE rollout_path=?", (int(NOW) - 10, str(selected)))
        with patch.object(worker, "MAX_SOURCES", 2):
            self.run_worker(cwd=str(self.workspace))
        self.assertEqual(self.snapshot()["current"]["calls"], 1)

    def test_workspace_canonical_scope_includes_native_symlink_aliases(self):
        alias = self.root / "workspace_alias"
        alias.symlink_to(self.workspace, target_is_directory=True)
        descendant = alias / "component"
        descendant.mkdir()
        self.source([event()], name="alias.jsonl", cwd=alias)
        self.source([event("descendant", thread="descendant")], name="descendant.jsonl",
                    thread="descendant", cwd=descendant)
        self.source([event("foreign", thread="foreign")], name="foreign.jsonl",
                    thread="foreign", cwd=self.root / "foreign")
        with patch.object(worker, "MAX_SOURCES", 2):
            self.run_worker(cwd=str(self.workspace.resolve()))
        self.assertEqual(self.snapshot()["current"]["calls"], 2)

    def test_incomplete_candidate_inventory_preserves_prior_measurements(self):
        selected = self.source([event()], name="selected.jsonl")
        self.run_worker(cwd=str(self.workspace))
        self.source([event("foreign", thread="foreign")], name="foreign.jsonl",
                    thread="foreign", cwd=self.root / "foreign")
        with closing(sqlite3.connect(self.index)) as connection, connection:
            connection.execute("UPDATE threads SET updated_at=? WHERE rollout_path=?", (int(NOW) - 10, str(selected)))
        with patch.object(worker, "MAX_SOURCE_CANDIDATES", 1):
            result = self.run_worker(cwd=str(self.workspace))
        self.assertEqual(result["state"], "partial")
        self.assertFalse(result["progress"]["has_more"])
        self.assertEqual(self.snapshot()["current"]["calls"], 1)
        coverage = json.loads((self.state / "snapshot.json").read_text())["coverage"]
        self.assertEqual(coverage["inventory_incomplete"], 1)
        self.assertEqual(coverage["source_candidate_limit_exceeded"], 1)

    def test_source_inventory_deadline_is_explicit_and_preserves_sources(self):
        self.source([event()])
        self.run_worker(cwd=str(self.workspace))
        original = worker.discover_sources
        def expired_inventory(*args, **kwargs):
            return original(*args, **{**kwargs, "deadline": 0, "clock": lambda: 1})
        with patch.object(worker, "discover_sources", side_effect=expired_inventory):
            result = self.run_worker(cwd=str(self.workspace))
        self.assertEqual(result["state"], "partial")
        self.assertFalse(result["progress"]["has_more"])
        self.assertEqual(self.snapshot()["current"]["calls"], 1)
        coverage = json.loads((self.state / "snapshot.json").read_text())["coverage"]
        self.assertEqual(coverage["source_discovery_budget_exceeded"], 1)

    def test_selected_inventory_cap_preserves_earlier_measurements(self):
        self.source([event()], name="first.jsonl")
        self.source([event("second", thread="second")], name="second.jsonl", thread="second")
        self.run_worker(cwd=str(self.workspace))
        with patch.object(worker, "MAX_SOURCES", 1):
            result = self.run_worker(cwd=str(self.workspace))
        self.assertEqual(result["state"], "partial")
        self.assertEqual(self.snapshot()["current"]["calls"], 2)

    def test_scope_change_rebuilds_derived_counts(self):
        self.source([event()])
        self.run_worker()
        self.run_worker(cwd=str(self.root / "other"))
        self.assertEqual(self.snapshot()["current"]["calls"], 0)

    def test_missing_tokenizer_produces_null_tokens_and_known_bytes(self):
        self.source([event()])
        self.run_worker(tokenizer=worker.TokenCounter())
        current = self.snapshot()["current"]
        self.assertIsNone(current["argument_tokens"])
        self.assertIsNone(current["return_tokens"])
        self.assertIsNone(current["lifecycle_tokens"])
        self.assertEqual(current["argument_token_calls"], 0)
        self.assertEqual(current["return_token_calls"], 0)
        self.assertEqual(current["lifecycle_token_calls"], 0)
        self.assertGreater(current["argument_bytes"], 0)
        self.assertEqual(current["return_bytes"], 2)
        self.run_worker()
        self.assertIsNotNone(self.snapshot()["current"]["argument_tokens"])
        self.assertEqual(self.snapshot()["current"]["calls"], 1)

    def test_malformed_oversized_and_missing_results_have_explicit_coverage(self):
        self.source(['{"agent-coord":invalid}', 'agent-coord ' + 'x' * 100, event(result=None)])
        with patch.object(worker, "MAX_LINE_BYTES", 80):
            result = self.run_worker()
        coverage = json.loads((self.state / "snapshot.json").read_text())["coverage"]
        self.assertEqual(coverage["malformed_records"], 1)
        self.assertGreaterEqual(coverage["oversized_records"], 2)
        self.assertEqual(result["state"], "partial")
        self.assertFalse(result["progress"]["has_more"])

    def test_known_call_missing_result_is_not_zero(self):
        self.source([event(result=None)])
        self.run_worker()
        counts = self.snapshot()["current"]
        self.assertEqual(counts["calls"], 1)
        self.assertIsNone(counts["return_tokens"])
        self.assertIsNone(counts["return_bytes"])
        self.assertIsNone(counts["tokens_per_turn"])
        self.assertEqual(counts["return_token_calls"], 0)
        self.assertEqual(counts["argument_token_calls"], 1)

    def test_result_capture_distinguishes_missing_from_explicit_empty(self):
        cases = (({}, False), ({"content": None, "structuredContent": {"status": "ok"}}, False),
                 ({"structuredContent": {"status": "ok"}}, False), (None, False),
                 ({"content": []}, True), ([], True), ("", True))
        for result, known in cases:
            with self.subTest(result=result):
                record = event()
                record["payload"]["item"]["result"] = result
                counters = worker.Counter()
                row = worker.measure_record(record, "thread-1", self.tokenizer, counters, 0, NOW)
                self.assertEqual(row[7], 0 if known else None)
                self.assertEqual(row[10], 0 if known else None)
                self.assertEqual(counters["missing_or_nontext_results"], 0 if known else 1)

    def test_absent_argument_payload_is_unknown(self):
        record = event()
        del record["payload"]["item"]["arguments"]
        self.source([record])
        result = self.run_worker()
        self.assertEqual(result["state"], "partial")
        counts = self.snapshot()["current"]
        self.assertEqual(counts["calls"], 1)
        self.assertIsNone(counts["argument_tokens"])
        self.assertIsNone(counts["argument_bytes"])
        self.assertIsNone(counts["lifecycle_tokens"])
        self.assertEqual(counts["argument_token_calls"], 0)
        self.assertEqual(counts["lifecycle_token_calls"], 0)
        self.assertEqual(counts["return_token_calls"], 1)
        coverage = json.loads((self.state / "snapshot.json").read_text())["coverage"]
        self.assertEqual(coverage["missing_argument_payloads"], 1)

    def test_explicit_null_arguments_are_measured(self):
        record = event()
        record["payload"]["item"]["arguments"] = None
        self.source([record])
        self.run_worker()
        counts = self.snapshot()["current"]
        self.assertEqual(counts["argument_tokens"], len("null"))
        self.assertEqual(counts["argument_bytes"], len("null"))
        self.assertEqual(counts["argument_token_calls"], 1)
        self.assertEqual(counts["lifecycle_tokens"], 0)

    def test_legacy_nonnullable_argument_index_is_rebuilt(self):
        self.state.mkdir()
        with closing(sqlite3.connect(self.state / "metrics.sqlite3")) as connection, connection:
            connection.execute("""CREATE TABLE calls(
                call_id TEXT PRIMARY KEY,transport TEXT NOT NULL,operation TEXT NOT NULL,thread_id TEXT NOT NULL,
                turn_id TEXT,occurred_at REAL NOT NULL,argument_tokens INTEGER,return_tokens INTEGER,lifecycle_tokens INTEGER,
                argument_bytes INTEGER NOT NULL,return_bytes INTEGER)""")
        record = event()
        del record["payload"]["item"]["arguments"]
        self.source([record])
        self.run_worker()
        self.assertIsNone(self.snapshot()["current"]["argument_bytes"])
        column = next(row for row in self.database().execute("PRAGMA table_info(calls)") if row[1] == "argument_bytes")
        self.assertFalse(column[3])

    def test_partial_token_totals_sum_measured_calls_with_coverage(self):
        self.source([event(), event("missing-result", result=None)])
        self.run_worker()
        counts = self.snapshot()["current"]
        self.assertEqual(counts["calls"], 2)
        self.assertEqual(counts["return_tokens"], 2)
        self.assertEqual(counts["return_token_calls"], 1)
        self.assertEqual(counts["argument_token_calls"], 2)
        self.assertEqual(counts["lifecycle_token_calls"], 2)
        self.assertEqual(self.snapshot()["tools"][0]["return_token_calls"], 1)
        self.assertIsNotNone(counts["tokens_per_turn"])

    def test_known_empty_period_has_zero_tokens_and_zero_coverage_counts(self):
        self.source([event(timestamp="2026-10-15T12:00:00Z")])
        self.run_worker()
        counts = self.snapshot()["current"]
        self.assertEqual(counts["calls"], 0)
        for key in ("argument_tokens", "return_tokens", "lifecycle_tokens",
                    "argument_token_calls", "return_token_calls", "lifecycle_token_calls"):
            self.assertEqual(counts[key], 0)
        self.assertIsNone(counts["tokens_per_turn"])

    def test_empty_period_without_tokenizer_remains_unknown(self):
        self.source([])
        self.run_worker(tokenizer=worker.TokenCounter())
        counts = self.snapshot()["current"]
        self.assertIsNone(counts["argument_tokens"])
        self.assertIsNone(counts["return_tokens"])
        self.assertEqual(counts["argument_token_calls"], 0)

    def test_byte_budget_yields_and_resumes_without_lost_calls(self):
        first = event()
        line_length = len(json.dumps(first).encode()) + 1
        self.source([first, event("call-2"), event("call-3")])
        result = self.run_worker(max_bytes=line_length + 20)
        self.assertLessEqual(result["progress"]["bytes_processed"], line_length + 20)
        self.assertTrue(result["progress"]["has_more"])
        self.assertEqual(self.snapshot()["current"]["calls"], 1)
        self.run_worker()
        self.assertEqual(self.snapshot()["current"]["calls"], 3)

    def test_tiny_byte_budget_discards_large_record_with_forward_progress(self):
        self.source([event()])
        for _ in range(20):
            result = self.run_worker(max_bytes=50)
            self.assertLessEqual(result["progress"]["bytes_processed"], 50)
            if not result["progress"]["has_more"]:
                break
        else:
            self.fail("Worker failed to advance past a record larger than its byte budget")
        self.assertEqual(self.snapshot()["current"]["calls"], 0)
        self.assertEqual(json.loads((self.state / "snapshot.json").read_text())["coverage"]["oversized_records"], 1)

    def test_wall_time_budget_marks_resumable_backlog(self):
        self.source([event()])
        ticks = iter([0, 2, 2, 2, 2, 2, 2])
        result = self.run_worker(max_seconds=1, clock=lambda: next(ticks, 2))
        self.assertTrue(result["progress"]["has_more"])
        self.assertTrue(result["progress"]["aggregation_pending"])
        self.assertFalse((self.state / "snapshot.json").exists())

    def test_unstarted_sources_preserve_native_recency_order(self):
        old = self.source([event("old")], name="a-old.jsonl")
        recent = self.source([event("new")], name="z-new.jsonl")
        with closing(sqlite3.connect(self.index)) as connection, connection:
            connection.execute("UPDATE threads SET updated_at=? WHERE rollout_path=?", (int(NOW) - 100, str(old)))
        result = self.run_worker(max_bytes=recent.stat().st_size)
        self.assertTrue(result["progress"]["has_more"])
        self.assertEqual(self.database().execute("SELECT call_id FROM calls").fetchone()[0], "mcp:new")

    def test_partial_source_precedes_newer_unstarted_source(self):
        partial = self.source([event("first"), event("second")], name="partial.jsonl")
        line_size = len((json.dumps(event("first")) + "\n").encode())
        self.run_worker(max_bytes=line_size)
        self.source([event("new")], name="new.jsonl")
        with closing(sqlite3.connect(self.index)) as connection, connection:
            connection.execute("UPDATE threads SET updated_at=? WHERE rollout_path=?", (int(NOW) + 1, str(self.home / "new.jsonl")))
        self.run_worker(max_bytes=partial.stat().st_size - line_size)
        measured = {row[0] for row in self.database().execute("SELECT call_id FROM calls")}
        self.assertEqual(measured, {"mcp:first", "mcp:second"})

    def test_large_sql_aggregation_obeys_deadline_and_keeps_previous_snapshot(self):
        self.source([event()])
        self.run_worker()
        previous = (self.state / "snapshot.json").read_bytes()
        connection = self.database()
        def expensive_query(*_):
            connection.execute("WITH RECURSIVE sequence(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM sequence WHERE x<10000000) SELECT sum(x) FROM sequence").fetchone()
            self.fail("Aggregation should have been interrupted")
        ticks = iter([0, 0, 0, 2, 2, 2])
        with patch.object(worker, "make_snapshot", expensive_query):
            result = worker.bounded_snapshot(connection, self.tokenizer, NOW, {}, deadline=1,
                                              clock=lambda: next(ticks, 2), sleep=lambda _: None)
        self.assertIsNone(result)
        self.assertEqual((self.state / "snapshot.json").read_bytes(), previous)
        self.assertEqual(connection.execute("SELECT 1").fetchone()[0], 1)

    def test_repeated_aggregation_failures_stop_automatic_retries(self):
        self.source([event()])
        self.run_worker()
        previous = (self.state / "snapshot.json").read_bytes()
        with patch.object(worker, "bounded_snapshot", return_value=None):
            first = self.run_worker()
            second = self.run_worker()
        self.assertTrue(first["progress"]["has_more"])
        self.assertFalse(second["progress"]["has_more"])
        self.assertTrue(second["progress"]["aggregation_retry_limited"])
        self.assertEqual(second["state"], "partial")
        self.assertEqual((self.state / "snapshot.json").read_bytes(), previous)

    def test_current_previous_lifecycle_and_percentiles(self):
        current = [event("call-" + str(i), arguments={"summary": "x" * i, "context_lifecycle": {"reason": "test"}}) for i in range(1, 21)]
        self.source(current + [event("previous", timestamp="2026-10-31T12:00:00Z")])
        self.run_worker()
        result = self.snapshot()
        self.assertEqual(result["current"]["calls"], 20)
        self.assertEqual(result["previous"]["calls"], 1)
        counts = sorted(len(worker.canonical(e["payload"]["item"]["arguments"])) for e in current)
        self.assertEqual(result["tools"][0]["p95_argument_tokens"], counts[18])
        self.assertGreater(result["current"]["lifecycle_tokens"], 0)
        self.assertEqual(sum(day["calls"] for day in result["daily"]), 20)

    def test_cli_payload_and_compound_exclusion(self):
        self.source([command_event("agent-coord send --session target 'hello; world'"),
                     command_event("agent-coord inbox; cat secrets", "excluded"),
                     command_event("echo agent-coord send", "mention")])
        self.run_worker()
        result = self.snapshot()
        self.assertEqual(result["current"]["calls"], 1)
        self.assertEqual(result["tools"][0]["operation"], "send")
        self.assertEqual(result["tools"][0]["transport"], "cli")
        self.assertEqual(json.loads((self.state / "snapshot.json").read_text())["coverage"]["excluded_compound_cli_events"], 1)

    def test_cli_checkpoint_lifecycle_is_unknown_not_zero(self):
        self.source([command_event("agent-coord checkpoint --json '{\"summary\":\"Done\",\"context_lifecycle\":{\"reason\":\"Useful\"}}'")])
        self.run_worker()
        counts = self.snapshot()["current"]
        self.assertIsNone(counts["lifecycle_tokens"])
        self.assertEqual(counts["lifecycle_token_calls"], 0)
        self.assertGreater(counts["argument_tokens"], 0)
        self.assertEqual(json.loads((self.state / "snapshot.json").read_text())["coverage"]["unknown_cli_lifecycle"], 1)

    def test_unavailable_index_preserves_previous_snapshot(self):
        self.source([event()])
        self.run_worker()
        original = (self.state / "snapshot.json").read_bytes()
        self.index.unlink()
        progress = self.run_worker()
        self.assertEqual(progress["state"], "unavailable")
        self.assertFalse(progress["progress"]["has_more"])
        self.assertEqual((self.state / "snapshot.json").read_bytes(), original)

    def test_unavailable_index_does_not_expose_another_scope_snapshot(self):
        self.source([event()])
        self.run_worker()
        self.index.unlink()
        self.run_worker(cwd=str(self.root / "other"))
        self.assertFalse((self.state / "snapshot.json").exists())

    def test_live_event_after_report_cutoff_is_retained_for_next_snapshot(self):
        from datetime import datetime, timezone
        timestamp = datetime.fromtimestamp(NOW + 2, timezone.utc).isoformat()
        self.source([event(timestamp=timestamp)])
        self.run_worker()
        self.assertEqual(self.snapshot()["current"]["calls"], 0)
        self.assertEqual(self.database().execute("SELECT COUNT(*) FROM calls").fetchone()[0], 1)
        self.run_worker(now=NOW + 3)
        self.assertEqual(self.snapshot()["current"]["calls"], 1)

    def test_invalid_source_format_is_bounded(self):
        self.source([event(tool="private-operation-name")])
        self.run_worker()
        self.assertEqual(self.snapshot()["tools"][0]["operation"], "other")


class TokenizerTests(unittest.TestCase):
    def test_unknown_tokenizer_does_not_import_as_runtime_dependency(self):
        with patch.dict(sys.modules, {"tiktoken": None}):
            self.assertFalse(worker.load_tokenizer().available)

    def test_tokenizer_cache_miss_never_downloads(self):
        from types import SimpleNamespace
        attempts = []
        load = SimpleNamespace(read_file=lambda path: attempts.append(path))
        original = load.read_file
        fake = SimpleNamespace(load=load, get_encoding=lambda _: load.read_file("https://example.invalid/vocab"))
        with patch.dict(sys.modules, {"tiktoken": fake, "tiktoken.load": load}):
            self.assertFalse(worker.load_tokenizer().available)
        self.assertEqual(attempts, [])
        self.assertIs(load.read_file, original)


if __name__ == "__main__":
    unittest.main()
