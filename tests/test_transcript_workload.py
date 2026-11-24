"""Content-free workload reports must reject missing progress and actual delta writes."""
import copy
import logging
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from measure_transcript_workload import DownstreamErrors, gates, measure


class TranscriptWorkloadTests(unittest.TestCase):
    def report(self):
        return {'mixed_stream_deltas': 20, 'open_paths': {'managed': 5, 'external': 5}, 'error_count': 0, 'open': {'count': 5, 'p95_ms': 1},
                'send_accept': {'count': 5, 'p95_ms': 1}, 'writer_hold': {'p95_ms': 1},
                'admission_timeouts': 0, 'durable_receipts': 5, 'finalized_writes': 2,
                'seeded': {'threads': 2, 'turns': 6}, 'workload': {'seconds': 1, 'threads': 2, 'turns_per_thread': 3}, 'search': {'indexed': 2, 'database_isolated': True, 'new_transcript_published': True}, 'streaming': {'turns': 10, 'projected_deltas': 100,
                'state_data_version_changes': 0, 'transport_changes': 0}}

    def test_each_required_outcome_is_gated(self):
        baseline = self.report()
        self.assertEqual(gates(baseline), [])
        for section, field, value in [('open', 'p95_ms', 15), ('send_accept', 'p95_ms', 15),
                ('writer_hold', 'p95_ms', 2), ('streaming', 'state_data_version_changes', 1),
                ('streaming', 'transport_changes', 1), ('streaming', 'turns', 9),
                ('streaming', 'projected_deltas', 0), ('search', 'indexed', 0),
                ('open', 'count', 0), ('seeded', 'turns', 5)]:
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

    def test_downstream_failure_counts_are_content_free_and_gated(self):
        observer = DownstreamErrors()
        record = logging.LogRecord('agent_coord.transcript_migration', logging.ERROR, '', 0,
                                   'private transcript body', (), (OSError, OSError('private path'), None))
        observer.emit(record)
        snapshot = observer.snapshot()
        self.assertEqual(snapshot['error_count'], 1)
        self.assertEqual(snapshot['errors'][0]['type'], 'OSError')
        self.assertNotIn('private', repr(snapshot))
        report = self.report()
        report['http_error_count'] = 0
        observer.apply(report)
        self.assertEqual(report['error_count'], 1)
        self.assertIn('downstream service errors', report['budget_failures'])
        for key, value in [('persistence', {'required_failures': 1}),
                           ('persistence', {'optional_failures': 1}),
                           ('migration', {'error_present': True}),
                           ('setup_migration_complete', False)]:
            with self.subTest(key=key):
                report = self.report()
                report[key] = value
                self.assertTrue(gates(report))

    def test_small_real_fixture_progress_and_receipts(self):
        report = measure(seconds=1, threads=2, turns=3, rounds=2)
        self.assertEqual(report['error_count'], 0, report['errors'])
        self.assertTrue(report['setup_migration_complete'])
        self.assertEqual(report['downstream']['error_count'], 0)
        self.assertEqual(report['persistence']['required_failures'], 0)
        self.assertEqual(report['persistence']['optional_failures'], 0)
        self.assertEqual(report['seeded'], {'threads': 2, 'turns': 6})
        if report['search']['available']:
            self.assertTrue(report['search']['database_isolated'])
            self.assertGreater(report['search']['indexed'], 0)
            self.assertGreaterEqual(report['search']['durable_coverage']['saved_histories'], 2)
            self.assertTrue(report['search']['new_transcript_published'])
            self.assertTrue(report['coverage']['normal_search_publication_worker'])
            self.assertGreater(report['mixed_stream_deltas'], 0)
            self.assertGreater(min(report['open_paths'].values()), 0)
        self.assertGreater(report['open']['count'], 0)
        self.assertGreater(report['send_accept']['count'], 0)
        self.assertEqual(report['durable_receipts'], report['send_accept']['count'])
        self.assertGreater(report['finalized_writes'], 0)
        self.assertEqual(report['streaming']['projected_deltas'], 20)
        self.assertEqual(report['streaming']['turns'], 10)
        # Hardware latency is measured by --check, never asserted by a unit fixture.
        if report['streaming']['state_data_version_changes'] or report['streaming']['transport_changes']:
            self.assertIn('SQLite writes during streaming deltas', report['budget_failures'])
        if not report['search']['available']:
            self.assertIn('isolated search made no progress', report['budget_failures'])
