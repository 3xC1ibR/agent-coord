from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))
from agent_coord.kimi_usage import backfill_usage, read_journal, SOURCE_KEY
from agent_coord.store import CoordinationStore


def usage(model="k3", *, agent="main", scope="turn", **counters):
    return {"type": "usage.record", "agentId": agent, "model": model,
            "usageScope": scope, "usage": counters}


def measured(model="k3", *, agent="main", scope="turn", value=10):
    return usage(model, agent=agent, scope=scope, inputOther=value, output=2,
                 inputCacheRead=3, inputCacheCreation=0)


class KimiUsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"KIMI_CODE_HOME": str(self.root / "kimi")})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.store = CoordinationStore(self.root / "state.sqlite3")
        self.real_usage = self.store.usage
        self.store.usage = Mock()
        self.store.usage.summary.return_value = {"sentinel": "summary"}
        with self.store._connection() as db:
            db.execute("CREATE TABLE kimi_sessions (thread_id TEXT PRIMARY KEY, native_id TEXT, cwd TEXT)")
            db.execute("INSERT INTO kimi_sessions VALUES ('thread', 'session_native', ?)", (str(self.root),))
        self.directory = self.root / "kimi/sessions/wd_test/session_native"
        self.directory.mkdir(parents=True)
        self.state()

    def state(self, **values):
        (self.directory / "state.json").write_text(json.dumps({
            "id": "session_native", "cwd": str(self.root), **values}), encoding="utf-8")

    def journal(self, *records, agent="main", version="1.5", tail=b""):
        path = self.directory / "agents" / agent / "wire.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        data = b"".join((json.dumps(r) + "\n").encode() for r in [
            {"type": "metadata", "protocol_version": version}, *records])
        path.write_bytes(data + tail)
        return path

    def snapshots(self):
        result = {}
        for call in self.store.usage.replace_snapshots.call_args_list[-2:]:
            values = dict(call.kwargs)
            counts = [b["totals"] for b in values["models"]]
            keys = set.intersection(*(set(c) for c in counts))
            values["totals"] = {key: sum(c[key] for c in counts) for key in keys}
            result[values["scope"]] = values
        return result

    def test_compaction_models_and_native_children_are_separate(self):
        self.journal(measured(), measured("fast", scope="session", value=20),
            {"type": "token_counting.measured", "tokens": 999999},
            {"type": "subagent.completed", "usage": {"output": 999999}})
        self.journal(measured("fast", agent="agent-0", value=5), agent="agent-0")
        self.assertEqual(backfill_usage(self.store, "thread"), {"sentinel": "summary"})
        snapshots = self.snapshots()
        self.assertEqual(snapshots["direct"]["totals"]["total_tokens"], 40)
        self.assertEqual(snapshots["including_native_children"]["totals"]["total_tokens"], 50)
        self.assertEqual(snapshots["direct"]["models"], [
            {"model": "fast", "totals": {"cached_input_tokens": 3, "cache_write_input_tokens": 0,
                 "input_tokens": 23, "output_tokens": 2, "total_tokens": 25}},
            {"model": "k3", "totals": {"cached_input_tokens": 3, "cache_write_input_tokens": 0,
                 "input_tokens": 13, "output_tokens": 2, "total_tokens": 15}}])
        self.assertEqual(snapshots["direct"]["coverage"], "partial")

    def test_replay_and_restart_replace_the_same_snapshot(self):
        self.journal(measured())
        backfill_usage(self.store, "thread")
        first = self.snapshots()["direct"]
        backfill_usage(self.store, "thread")
        second = self.snapshots()["direct"]
        self.assertEqual(first, second)
        restarted = CoordinationStore(self.store.database_path)
        restarted.usage = self.store.usage
        backfill_usage(restarted, "thread")
        self.assertEqual(self.snapshots()["direct"], first)
        self.store.usage.record_delta.assert_not_called()

    def test_incomplete_final_record_is_deferred_then_imported_once(self):
        last = json.dumps(measured(value=20)).encode()
        path = self.journal(measured(), tail=last[:20])
        backfill_usage(self.store, "thread")
        self.assertEqual(self.snapshots()["direct"]["totals"]["total_tokens"], 15)
        self.assertEqual(self.snapshots()["direct"]["raw"]["incomplete_tail"], 1)
        with path.open("ab") as output:
            output.write(last[20:] + b"\n")
        backfill_usage(self.store, "thread")
        self.assertEqual(self.snapshots()["direct"]["totals"]["total_tokens"], 40)
        backfill_usage(self.store, "thread")
        self.assertEqual(self.snapshots()["direct"]["totals"]["total_tokens"], 40)

    def test_replacement_truncation_and_model_removal_are_not_additive(self):
        path = self.journal(measured(), measured("old", value=40))
        backfill_usage(self.store, "thread")
        path.unlink()
        self.journal(measured("new", value=1))
        backfill_usage(self.store, "thread")
        snapshot = self.snapshots()["direct"]
        self.assertEqual(snapshot["source_key"], SOURCE_KEY)
        self.assertEqual(snapshot["totals"]["total_tokens"], 6)
        self.assertEqual([b["model"] for b in snapshot["models"]], ["new"])
        self.journal()
        backfill_usage(self.store, "thread")
        self.assertEqual(self.snapshots()["direct"]["totals"], {})

    def test_latest_fork_boundary_excludes_all_inherited_usage(self):
        path = self.journal(measured(value=100), {"type": "forked", "agentId": "main"},
            measured(value=200), {"type": "forked", "agentId": "main"}, measured(value=5))
        self.state(forkedFrom="session_source")
        backfill_usage(self.store, "thread")
        snapshot = self.snapshots()["direct"]
        self.assertEqual(snapshot["totals"]["total_tokens"], 10)
        provenance = next(v for k, v in snapshot["raw"].items() if k.startswith("journal_") and isinstance(v, dict))
        self.assertGreater(provenance["start_offset"], 0)
        self.assertEqual(read_journal(path, "main", forked=True)["records"], 1)

    def test_fork_without_boundary_cannot_inherit_parent_consumption(self):
        self.journal(measured(value=100))
        self.state(forkedFrom="session_source")
        backfill_usage(self.store, "thread")
        self.store.usage.replace_snapshots.assert_not_called()
        self.assertIn("no usage boundary", self.store.usage.record_unavailable.call_args.kwargs["note"])

    def test_missing_and_all_zero_counters_remain_unknown(self):
        path = self.journal(usage(inputOther=10, output=2), measured())
        result = read_journal(path, "main")
        self.assertEqual(result["totals"], {"output_tokens": 4})
        self.assertIn("missing_provider_counters", result["notes"])
        path = self.journal(usage(inputOther=0, output=0, inputCacheRead=0, inputCacheCreation=0))
        self.assertEqual(read_journal(path, "main")["totals"], {})

    def test_unsupported_version_and_shape_never_guess_usage(self):
        self.journal(measured(), version="1.4")
        backfill_usage(self.store, "thread")
        self.store.usage.replace_snapshots.assert_not_called()
        self.assertIn("Unsupported Kimi wire version", self.store.usage.record_unavailable.call_args.kwargs["note"])
        self.journal(measured(agent="other"))
        backfill_usage(self.store, "thread")
        self.store.usage.replace_snapshots.assert_not_called()
        self.assertIn("record shape", self.store.usage.record_unavailable.call_args.kwargs["note"])

    def test_missing_journal_and_wrong_native_mapping_are_unavailable(self):
        backfill_usage(self.store, "thread")
        self.store.usage.replace_snapshots.assert_not_called()
        self.journal(measured())
        self.state(id="session_someone_else")
        backfill_usage(self.store, "thread")
        self.assertIn("missing or ambiguous", self.store.usage.record_unavailable.call_args.kwargs["note"])
        self.store.usage.replace_snapshots.assert_not_called()

    def test_explicit_path_must_match_native_session_and_workspace(self):
        path = self.journal(measured())
        backfill_usage(self.store, "thread", path)
        self.assertEqual(self.snapshots()["direct"]["totals"]["total_tokens"], 15)
        self.store.usage.replace_snapshots.reset_mock()
        self.state(cwd="/other")
        backfill_usage(self.store, "thread", path)
        self.store.usage.replace_snapshots.assert_not_called()

    def test_missing_child_is_partial_without_copying_its_summary(self):
        self.journal(measured())
        self.journal(measured(agent="agent-0"), agent="agent-0", version="99")
        backfill_usage(self.store, "thread")
        snapshot = self.snapshots()["including_native_children"]
        self.assertEqual(snapshot["totals"]["total_tokens"], 15)
        self.assertEqual(snapshot["raw"]["missing_children"], 1)
        self.assertEqual(snapshot["coverage"], "partial")

    def test_raw_provenance_never_retains_conversation_content(self):
        self.journal({"type": "turn.prompt", "input": "PRIVATE_PROMPT"},
            {"type": "agent.message.appended", "message": {"text": "PRIVATE_RESPONSE"}}, measured())
        backfill_usage(self.store, "thread")
        serialized = json.dumps(self.snapshots())
        self.assertNotIn("PRIVATE_", serialized)
        self.assertIn("digest_0", serialized)

    def test_real_store_replacement_and_restart_do_not_retain_removed_models(self):
        self.store.register(session_id="thread", client="kimi", cwd=str(self.root))
        self.store.usage = self.real_usage
        self.journal(measured("old", value=100))
        self.assertEqual(backfill_usage(self.store, "thread")["totals"]["total_tokens"], 105)
        self.assertEqual(backfill_usage(self.store, "thread")["totals"]["total_tokens"], 105)
        restarted = CoordinationStore(self.store.database_path)
        self.journal(measured("new", value=5))
        result = backfill_usage(restarted, "thread")
        self.assertEqual(result["totals"]["total_tokens"], 10)
        self.assertEqual(result["models"], [{"model": "new", "totals": {
            "input_tokens": 8, "output_tokens": 2, "total_tokens": 10,
            "cached_input_tokens": 3, "cache_write_input_tokens": 0}}])
        self.journal()
        self.assertEqual(backfill_usage(restarted, "thread")["totals"], {})

    def test_combined_read_budget_covers_main_and_children(self):
        main = self.journal(measured())
        self.journal(measured(agent="agent-0"), agent="agent-0")
        with patch("agent_coord.kimi_usage._MAX_IMPORT_BYTES", main.stat().st_size + 1):
            backfill_usage(self.store, "thread")
        inclusive = self.snapshots()["including_native_children"]
        self.assertEqual(inclusive["totals"]["total_tokens"], 15)
        self.assertEqual(inclusive["raw"]["missing_children"], 1)
        with patch("agent_coord.kimi_usage._MAX_IMPORT_BYTES", 1):
            self.store.usage.replace_snapshots.reset_mock()
            backfill_usage(self.store, "thread")
        self.store.usage.replace_snapshots.assert_not_called()
        self.assertIn("bounded import size", self.store.usage.record_unavailable.call_args.kwargs["note"])

    def test_mutation_during_scan_is_rejected_and_backfill_retries_until_stable(self):
        path = self.journal(measured())
        stat = path.stat()
        changed = SimpleNamespace(st_dev=stat.st_dev, st_ino=stat.st_ino,
            st_size=stat.st_size, st_mtime_ns=stat.st_mtime_ns + 1)
        with patch.object(Path, "stat", return_value=changed):
            with self.assertRaisesRegex(ValueError, "changed during import"):
                read_journal(path, "main")
        real_read = read_journal
        calls = 0
        def flaky_read(target, agent_id, **kwargs):
            nonlocal calls
            calls += 1
            if calls < 3:
                raise ValueError("Kimi journal changed during import")
            return real_read(target, agent_id, **kwargs)
        with patch("agent_coord.kimi_usage.read_journal", side_effect=flaky_read), \
                patch("agent_coord.kimi_usage.time.sleep"):
            backfill_usage(self.store, "thread")
        self.assertEqual(calls, 3)
        self.assertEqual(self.snapshots()["direct"]["totals"]["total_tokens"], 15)
        self.store.usage.record_unavailable.assert_not_called()

    def test_agent_enumeration_is_bounded_and_missing_child_is_not_zero(self):
        self.journal(measured())
        self.journal(measured(agent="agent-0"), agent="agent-0")
        self.journal(measured(agent="agent-1"), agent="agent-1")
        with patch("agent_coord.kimi_usage._MAX_AGENT_ENTRIES", 1):
            backfill_usage(self.store, "thread")
        self.assertEqual(self.snapshots()["including_native_children"]["raw"]["agent_enumeration_truncated"], 1)
        self.assertIn("partial", self.store.usage.record_unavailable.call_args.kwargs["note"])
        child = self.directory / "agents/agent-0/wire.jsonl"
        child.unlink()
        backfill_usage(self.store, "thread")
        self.assertEqual(self.snapshots()["including_native_children"]["raw"]["missing_children"], 1)

    def test_concatenated_journals_cannot_duplicate_request_records(self):
        path = self.journal(measured(), {"type": "metadata", "protocol_version": "1.5"}, measured())
        with self.assertRaisesRegex(ValueError, "second metadata header"):
            read_journal(path, "main")

    def test_unknown_native_mapping_does_not_scan_unrelated_journals(self):
        self.journal(measured())
        with self.store._connection() as db:
            db.execute("DELETE FROM kimi_sessions")
        with patch("agent_coord.kimi_usage.read_journal") as read:
            backfill_usage(self.store, "thread")
        read.assert_not_called()
        self.store.usage.replace_snapshots.assert_not_called()
        self.assertIn("No saved Kimi", self.store.usage.record_unavailable.call_args.kwargs["note"])


if __name__ == "__main__":
    unittest.main()
