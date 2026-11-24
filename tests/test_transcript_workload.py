"""Content-free workload reports must reject missing progress and actual delta writes."""
import copy
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from measure_transcript_workload import gates, measure


class TranscriptWorkloadTests(unittest.TestCase):
    def report(self):
        return {'error_count': 0, 'open': {'count': 5, 'p95_ms': 1},
                'send_accept': {'count': 5, 'p95_ms': 1}, 'writer_hold': {'p95_ms': 1},
                'admission_timeouts': 0, 'durable_receipts': 5, 'finalized_writes': 2,
                'workload': {'seconds': 1}, 'search': {'indexed': 2, 'database_isolated': True}, 'streaming': {'turns': 10, 'projected_deltas': 100,
                'state_commits': 0, 'transport_changes': 0}}

    def test_each_required_outcome_is_gated(self):
        baseline = self.report()
        self.assertEqual(gates(baseline), [])
        for section, field, value in [('open', 'p95_ms', 15), ('send_accept', 'p95_ms', 15),
                ('writer_hold', 'p95_ms', 2), ('streaming', 'state_commits', 1),
                ('streaming', 'transport_changes', 1), ('streaming', 'turns', 9),
                ('streaming', 'projected_deltas', 0), ('search', 'indexed', 0),
                ('open', 'count', 0)]:
            with self.subTest(section=section, field=field):
                report = copy.deepcopy(baseline)
                report[section][field] = value
                self.assertTrue(gates(report))
        for field, value in [('error_count', 1), ('admission_timeouts', 1),
                             ('durable_receipts', 4), ('finalized_writes', 0)]:
            with self.subTest(field=field):
                report = copy.deepcopy(baseline)
                report[field] = value
                self.assertTrue(gates(report))

    def test_small_real_fixture_progress_and_receipts(self):
        report = measure(seconds=1, threads=2, turns=3, rounds=2)
        self.assertEqual(report['error_count'], 0, report['errors'])
        self.assertGreater(report['open']['count'], 0)
        self.assertGreater(report['send_accept']['count'], 0)
        self.assertEqual(report['durable_receipts'], report['send_accept']['count'])
        self.assertGreater(report['finalized_writes'], 0)
        self.assertEqual(report['streaming']['projected_deltas'], 20)
        self.assertEqual(report['streaming']['turns'], 10)
        # Hardware latency is measured by --check, never asserted by a unit fixture.
        if report['streaming']['state_commits'] or report['streaming']['transport_changes']:
            self.assertIn('SQLite writes during streaming deltas', report['budget_failures'])
        if not report['search']['available']:
            self.assertIn('isolated search made no progress', report['budget_failures'])
