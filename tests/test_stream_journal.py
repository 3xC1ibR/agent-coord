"""Durable stdout replay through the production transport journal entrypoints."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_coord.provider_transport import Journal, Owner, TransportError
from agent_coord.stream_journal import StreamJournal


class StreamJournalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def journal(self):
        journal = Journal(self.directory, file_events=True)
        self.addCleanup(journal.close)
        return journal

    def test_transport_deltas_and_acknowledgement_never_write_sqlite(self):
        journal = self.journal()
        statements = []
        journal.db.set_trace_callback(statements.append)
        for index in range(1000):
            journal.append('channel', 'stdout', str(index))
        self.assertEqual(journal.sequence(), 1000)
        self.assertEqual(len(journal.events(0)), 256)
        journal.acknowledge_events(1000, retain_after=900)
        self.assertEqual(len(journal.events(900)), 100)
        self.assertEqual(statements, [])
        with self.assertRaises(TransportError):
            journal.events(0)

    def test_fsync_failure_does_not_publish_or_consume_sequence(self):
        journal = self.journal()
        journal.append('channel', 'stdout', 'first')
        original = os.fsync
        calls = []
        def fail_once(fd):
            calls.append(fd)
            if len(calls) == 1:
                raise OSError('injected fsync failure')
            original(fd)
        with patch('agent_coord.stream_journal.os.fsync', side_effect=fail_once):
            with self.assertRaises(OSError):
                journal.append('channel', 'stdout', 'lost')
        self.assertEqual(journal.sequence(), 1)
        self.assertEqual(journal.append('channel', 'stdout', 'second'), 2)
        self.assertEqual([e['data'] for e in journal.events(0)], ['first', 'second'])

    def test_restart_truncates_partial_tail_and_retains_sequence_floor(self):
        spool = StreamJournal(self.directory, segment_bytes=1)
        for index in range(5):
            spool.append('channel', 'stdout', str(index), 10)
        path = spool.path
        spool.close()
        with path.open('ab') as target:
            target.write(b'{"partial":')
        restored = StreamJournal(self.directory, segment_bytes=1)
        self.assertEqual(restored.sequence, 5)
        self.assertEqual(len(restored.events(0, 10, 1000)), 5)
        restored.acknowledge(5, 5)
        restored.close()
        resumed = StreamJournal(self.directory)
        self.addCleanup(resumed.close)
        self.assertEqual(resumed.append('channel', 'eof', '', 10), 6)
        self.assertEqual(resumed.events(5, 10, 1000)[0]['sequence'], 6)

    def test_complete_corruption_fails_closed(self):
        spool = StreamJournal(self.directory)
        spool.append('channel', 'stdout', 'original', 10)
        path = spool.path
        spool.close()
        path.write_bytes(path.read_bytes().replace(b'original', b'modified'))
        with self.assertRaisesRegex(ValueError, 'checksum'):
            StreamJournal(self.directory)

    def test_new_owner_selects_spool_and_legacy_journal_remains_readable(self):
        with patch('agent_coord.provider_transport.boot_identity', return_value='test-boot'):
            owner = Owner(self.directory / 'owner')
        self.addCleanup(owner.journal.close)
        self.assertIsNotNone(owner.journal.spool)
        legacy = Journal(self.directory / 'legacy')
        legacy.append('retained', 'stdout', 'old event')
        legacy.close()
        reopened = Journal(self.directory / 'legacy')
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.events(0)[0]['data'], 'old event')
