"""Deterministic checks for the upgrade validation driver's evaluators and proofs.

The driver itself runs disposable runtimes for minutes; these tests cover the
logic that decides pass/fail so a weakened evaluator cannot pass silently.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("runtime_upgrade_validation", ROOT / "scripts/runtime_upgrade_validation.py")
validation = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(validation)


class ToolOutcomeTests(unittest.TestCase):
    def test_killed_tool_child_is_unhealthy_even_without_signal_records(self):
        records = [{"kind": "tool_start"}, {"kind": "tool_complete", "returncode": -9}]
        outcome = validation.tool_outcomes(records)
        self.assertFalse(outcome["healthy"])
        self.assertEqual(outcome["returncodes"], [-9])
        self.assertEqual(outcome["tool_exit"], 0)

    def test_complete_tool_run_is_healthy(self):
        records = [{"kind": "tool_start"}, {"kind": "tool_exit"}, {"kind": "tool_complete", "returncode": 0}]
        self.assertTrue(validation.tool_outcomes(records)["healthy"])


class ReceiptTests(unittest.TestCase):
    def inputs(self, *texts):
        return [{"kind": "input", "message": {"method": "turn/steer", "params": {"input": [{"text": t}]}}} for t in texts]

    def test_accepted_once_rejected_never_uncertain_at_most_once(self):
        # Tokens must be distinctive: matching is by substring of the serialized frame.
        outcomes = [{"token": "steer-0001", "outcome": "accepted"}, {"token": "steer-0002", "outcome": "rejected"},
                    {"token": "steer-0003", "outcome": "uncertain"}, {"token": "steer-0004", "outcome": "not_sent"}]
        violations, arrivals = validation.receipt_violations(outcomes, self.inputs("steer-0001", "steer-0003"))
        self.assertEqual(violations, [])
        self.assertEqual(arrivals, {"steer-0001": 1, "steer-0002": 0, "steer-0003": 1, "steer-0004": 0})

    def test_duplicate_and_lost_commands_are_violations(self):
        outcomes = [{"token": "dup", "outcome": "accepted"}, {"token": "lost", "outcome": "accepted"},
                    {"token": "ghost", "outcome": "rejected"}]
        violations, _ = validation.receipt_violations(outcomes, self.inputs("dup", "dup", "ghost"))
        self.assertEqual([v["token"] for v in violations], ["dup", "lost", "ghost"])
        self.assertEqual([v["arrivals"] for v in violations], [2, 0, 1])


class StopPathTests(unittest.TestCase):
    def test_stop_paths_are_told_apart_by_durable_records(self):
        update = {"id": "u1", "source_instance": "old", "cancelled": True}
        self.assertEqual(validation.stop_path({"instance": "old"}, update), "cancelled-before-commit")
        self.assertEqual(validation.stop_path({"instance": "new"}, update), "stopped-after-commit")
        self.assertEqual(validation.stop_path({"instance": "old"}, {"id": "u1", "source_instance": "old"}),
                         "stopped-without-cancellation")
        self.assertEqual(validation.stop_path(None, update), "unknown")
        self.assertEqual(validation.stop_path({"instance": "old"}, None), "unknown")


class EventCursorTests(unittest.TestCase):
    def batch(self, *seqs, reset=False, thread="t", deltas=()):
        events = [{"seq": s, "method": "browser/changed", "params": {}} for s in seqs]
        events += [{"seq": s, "method": "item/agentMessage/delta", "params": {"threadId": thread, "delta": d}}
                   for s, d in deltas]
        return {"seq": max([*seqs, *(s for s, _ in deltas)], default=0), "reset": reset, "events": events}

    def test_first_batch_reset_is_tolerated_but_later_resets_count(self):
        cursor = validation.EventCursor("t")
        cursor.feed(self.batch(1, 2, reset=True), "2:0", now=0)
        cursor.feed(self.batch(3, reset=True), "3:0", now=1)
        self.assertEqual(cursor.resets_after_first, 1)
        self.assertEqual(cursor.reset_at, [1])
        self.assertEqual(cursor.summary()["reset_at"], [1])
        self.assertEqual(cursor.last_id, "3:0")

    def test_declared_reset_starts_a_new_generation_without_order_violations(self):
        # After explicit Stop the relaunched coordinator numbers its events from 1 again and
        # declares the reset on its first batch. The reset itself is judged by verify_events
        # (permitted only after the Stop); ordering continues within the new generation.
        cursor = validation.EventCursor("t")
        cursor.feed(self.batch(178, 179, 180, reset=True), now=0)
        cursor.feed(self.batch(1, 2, 3, reset=True), now=6.4)
        cursor.feed(self.batch(4, 5), now=6.5)
        self.assertEqual(cursor.resets_after_first, 1)
        self.assertEqual(cursor.reset_at, [6.4])
        self.assertEqual(cursor.resets, [{"at": 6.4, "previous_last_seq": 180, "first_seq": 1}])
        self.assertEqual(cursor.order_violations, [])
        self.assertEqual(cursor.sequence_gaps, [])
        self.assertEqual(cursor.last_seq, 5)
        self.assertEqual(cursor.summary()["resets"], cursor.resets)

    def test_sequence_restart_without_a_declared_reset_remains_a_violation(self):
        # Numbers going backwards without a declared reset (a silent replacement of the
        # event log during a swap) are still ordering violations, and a declared reset does
        # not forgive violations that happen inside the new generation.
        cursor = validation.EventCursor("t")
        cursor.feed(self.batch(178, 179, 180, reset=True), now=0)
        cursor.feed(self.batch(1, 2), now=1)
        self.assertEqual(cursor.order_violations, [{"seq": 1, "previous": 180}, {"seq": 2, "previous": 180}])
        self.assertEqual(cursor.resets_after_first, 0)
        cursor.feed(self.batch(181, 182, reset=True), now=2)
        cursor.feed(self.batch(181), now=3)
        self.assertEqual(cursor.order_violations[-1], {"seq": 181, "previous": 182})
        self.assertEqual(cursor.resets, [{"at": 2, "previous_last_seq": 180, "first_seq": 181}])

    def test_deltas_of_other_turns_on_the_thread_are_accounted_separately(self):
        cursor = validation.EventCursor("t", "turn-a")
        cursor.feed({"seq": 2, "events": [
            {"seq": 1, "method": "item/agentMessage/delta", "params": {"threadId": "t", "turnId": "turn-a", "delta": "[1]"}},
            {"seq": 2, "method": "item/agentMessage/delta", "params": {"threadId": "t", "turnId": "turn-b", "delta": "[1]"}},
            {"seq": 3, "method": "item/agentMessage/delta", "params": {"threadId": "other", "turnId": "turn-a", "delta": "x"}},
            {"seq": 4, "method": "turn/completed", "params": {"threadId": "t", "turnId": "turn-a"}}]}, now=3)
        self.assertEqual(cursor.text, "[1]")
        self.assertEqual(cursor.other_turns, {"turn-b": 3})
        self.assertEqual(cursor.completed_at, 3)
        self.assertEqual(cursor.summary()["other_turn_delta_chars"], {"turn-b": 3})
        self.assertTrue(cursor.summary()["completed_observed"])

    def test_sequence_regression_and_text_assembly(self):
        cursor = validation.EventCursor("t")
        cursor.feed(self.batch(deltas=[(1, "[1]"), (2, "[2]")]), now=0)
        cursor.feed(self.batch(deltas=[(2, "[2]")]), now=0.5)  # replayed frame
        cursor.feed(self.batch(deltas=[(3, "[3]")]), now=7)
        self.assertEqual(cursor.text, "[1][2][2][3]")
        cursor.feed(self.batch(deltas=[(6, "[6]")]), now=8)  # 4 and 5 never arrived
        self.assertEqual(cursor.sequence_gaps, [{"after": 3, "seq": 6, "at": 8}])
        self.assertEqual(cursor.summary()["sequence_gaps"], cursor.sequence_gaps)
        self.assertEqual([d[:2] for d in cursor.deltas], [(1, 3), (2, 3), (2, 3), (3, 3), (6, 3)])
        self.assertEqual(cursor.order_violations, [{"seq": 2, "previous": 2}])
        self.assertEqual(max(cursor.delta_gaps), 6.5)
        self.assertNotIn("delta_gaps", cursor.summary())
        self.assertEqual(cursor.summary()["delta_gap_seconds"]["max"], 6.5)


class OutageWindowTests(unittest.TestCase):
    def test_consecutive_errors_form_one_window(self):
        errors = [{"at": 10.0}, {"at": 10.3}, {"at": 10.6}, {"at": 20.0}]
        windows = validation.Validation.outage_windows(errors)
        self.assertEqual(len(windows), 2)
        self.assertAlmostEqual(windows[0], 0.6)
        self.assertEqual(windows[1], 0)


class PayloadHookTests(unittest.TestCase):
    def test_fault_hook_allows_bootstrap_but_refuses_to_serve(self):
        for arguments, expected in ((["--runtime-bootstrap"], 0), (["--runtime-probe"], 0), (["--runtime-serve"], 3)):
            result = subprocess.run([sys.executable, "-c", validation.FAULT_HOOK, *arguments], check=False)
            self.assertEqual(result.returncode, expected, arguments)

    def test_identity_hook_records_the_importing_process(self):
        with tempfile.TemporaryDirectory() as root:
            env = dict(os.environ, RIBBON_FAKE_ROOT=root)
            result = subprocess.run([sys.executable, "-c", validation.IDENTITY_HOOK % 7, "--runtime-serve"],
                                    env=env, check=False)
            self.assertEqual(result.returncode, 0)
            files = list(Path(root, "identity").glob("*.json"))
            self.assertEqual(len(files), 1)
            self.assertEqual(json.loads(files[0].read_text())["generation"], 7)


class OwnershipProofTests(unittest.TestCase):
    def test_only_processes_naming_the_root_are_signalled(self):
        with tempfile.TemporaryDirectory(prefix="validation-root-") as root:
            marker = Path(root, "marker")
            owned = subprocess.Popen([sys.executable, "-c", "import time, sys; time.sleep(30)", str(marker)])
            foreign = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
            try:
                probe = validation.Validation.__new__(validation.Validation)
                probe.root = Path(root)
                probe.report = {"faults": []}
                self.assertTrue(probe.owned(owned.pid))
                self.assertFalse(probe.owned(foreign.pid))
                with self.assertRaises(AssertionError):
                    probe.kill_owned(foreign.pid, "must refuse")
                self.assertIsNone(foreign.poll())
                probe.kill_owned(owned.pid, "allowed")
                self.assertEqual(owned.wait(timeout=5), -9)
                self.assertEqual(probe.report["faults"][0]["pid"], owned.pid)
            finally:
                for process in (owned, foreign):
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=5)

    def test_process_listing_is_read_only_and_finds_the_root(self):
        with tempfile.TemporaryDirectory(prefix="validation-root-") as root:
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", root])
            try:
                self.assertIn(child.pid, validation.processes_mentioning(root))
                self.assertTrue(validation.process_alive(child.pid))
            finally:
                child.kill()
                child.wait(timeout=5)
            self.assertFalse(validation.process_alive(child.pid))


class TransportJournalPathTests(unittest.TestCase):
    def test_generation_from_owner_record_wins_over_flat_journal(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime_dir = Path(temp)
            transport = runtime_dir / "provider-transport"
            transport.mkdir()
            (transport / "transport.sqlite3").write_bytes(b"")
            # The owner records uuid4().hex (32 hexadecimal digits) and stores the journal under the
            # canonical hyphenated form of the same UUID.
            record_form = "9b2b48ab568244ef9e98ac84fbeb462c"
            (transport / "owner.json").write_text(json.dumps({"pid": 1, "journal_generation": record_form}))
            self.assertEqual(validation.transport_journal_path(runtime_dir),
                             transport / "generations/9b2b48ab-5682-44ef-9e98-ac84fbeb462c/transport.sqlite3")
            canonical = "9b2b48ab-5682-44ef-9e98-ac84fbeb462c"
            (transport / "owner.json").write_text(json.dumps({"pid": 1, "journal_generation": canonical}))
            self.assertEqual(validation.transport_journal_path(runtime_dir),
                             transport / f"generations/{canonical}/transport.sqlite3")

    def test_invalid_generation_fails_closed_to_flat_journal(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime_dir = Path(temp)
            transport = runtime_dir / "provider-transport"
            transport.mkdir()
            for bad in ("0123abcd", "not-a-uuid", 42, ""):
                (transport / "owner.json").write_text(json.dumps({"pid": 1, "journal_generation": bad}))
                self.assertEqual(validation.transport_journal_path(runtime_dir), transport / "transport.sqlite3", bad)

    def test_flat_journal_without_generation_or_record(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime_dir = Path(temp)
            transport = runtime_dir / "provider-transport"
            transport.mkdir()
            self.assertEqual(validation.transport_journal_path(runtime_dir), transport / "transport.sqlite3")
            (transport / "owner.json").write_text(json.dumps({"pid": 1}))
            self.assertEqual(validation.transport_journal_path(runtime_dir), transport / "transport.sqlite3")
            (transport / "owner.json").write_text("{not json")
            self.assertEqual(validation.transport_journal_path(runtime_dir), transport / "transport.sqlite3")

    def test_generation_with_path_separators_is_not_followed(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime_dir = Path(temp)
            transport = runtime_dir / "provider-transport"
            transport.mkdir()
            (transport / "owner.json").write_text(json.dumps({"pid": 1, "journal_generation": "../../x"}))
            self.assertEqual(validation.transport_journal_path(runtime_dir), transport / "transport.sqlite3")


if __name__ == "__main__":
    unittest.main()
