from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
PLUGIN_SCRIPTS = ROOT / "plugins/agent-coord/scripts"
for path in (str(SCRIPTS), str(PLUGIN_SCRIPTS)):
    if path not in sys.path:
        sys.path.insert(0, path)

SCRIPT = SCRIPTS / "measure_packaged_conversation_workload.py"
spec = importlib.util.spec_from_file_location("measure_packaged_conversation_workload", SCRIPT)
workload = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workload)


class PackagedConversationWorkloadAdvisoryLatencyTests(unittest.TestCase):
    def test_advisory_latency_allows_only_wall_clock_latency_misses(self):
        report = {
            "standalone_passed": False,
            "violations": [
                {"phase": "candidate", "kind": "send", "reason": "interaction latency budget exceeded"},
                {"phase": "candidate", "kind": "http_acceptance_ms", "reason": "browser latency budget exceeded"},
            ],
        }
        advisory, blocking = workload.apply_advisory_latency(report)
        self.assertEqual(len(advisory), 2)
        self.assertEqual(blocking, [])
        self.assertTrue(report["latency_advisory"])
        self.assertTrue(report["standalone_passed"])
        self.assertEqual(report["violations"], [])
        self.assertEqual(len(report["advisory_latency_violations"]), 2)

    def test_advisory_latency_still_fails_correctness_and_missing_samples(self):
        for violation in (
            {"phase": "candidate", "kind": "send", "reason": "no completed latency samples"},
            {"phase": "candidate", "kind": "send", "reason": "insufficient successful interactions or request errors"},
            {"phase": "candidate", "kind": "browser_send", "reason": "failed or missing browser samples"},
            {"phase": "cleanup", "kind": "missing original accepted receipt", "request_id": "req-1"},
            {"phase": "candidate", "kind": "incomplete workload coverage"},
        ):
            with self.subTest(violation=violation):
                report = {
                    "standalone_passed": False,
                    "violations": [
                        {"phase": "candidate", "kind": "other_ms", "reason": "browser latency budget exceeded"},
                        violation,
                    ],
                }
                advisory, blocking = workload.apply_advisory_latency(report)
                self.assertEqual(len(advisory), 1)
                self.assertEqual(blocking, [violation])
                self.assertFalse(report["standalone_passed"])
                self.assertEqual(report["violations"], [violation])

    def test_advisory_latency_still_fails_on_execution_error(self):
        report = {
            "standalone_passed": False,
            "execution_error": {"type": "RuntimeError", "reason": "boom"},
            "violations": [],
        }
        workload.apply_advisory_latency(report)
        self.assertFalse(report["standalone_passed"])


if __name__ == "__main__":
    unittest.main()
