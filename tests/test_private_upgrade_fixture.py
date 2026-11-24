from __future__ import annotations

from contextlib import closing
import hashlib
import ctypes
import json
import os
from pathlib import Path
import signal
import stat
import sqlite3
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import private_upgrade_fixture as fixture


class PortableRefusalTests(unittest.TestCase):
    def test_non_macos_has_no_physical_copy_fallback(self):
        with patch.object(fixture.sys, 'platform', 'linux'):
            with self.assertRaisesRegex(fixture.FixturePreparationError, 'macOS APFS'):
                fixture._system()


@unittest.skipUnless(sys.platform == 'darwin', 'APFS clonefile is macOS-only')
class PrivateFixtureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='private-fixture-marker-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / 'offline.sqlite3'
        self.marker = 'CREDENTIAL-PRIVATE-IDENTITY-MARKER'
        with closing(sqlite3.connect(self.source)) as db:
            db.execute('CREATE TABLE credentials(id INTEGER PRIMARY KEY AUTOINCREMENT, secret TEXT)')
            db.execute('INSERT INTO credentials(secret) VALUES (?)', (self.marker * 10000,))
            db.execute('CREATE TABLE queues(id INTEGER PRIMARY KEY, parent INTEGER REFERENCES credentials(id), command BLOB)')
            db.execute('INSERT INTO queues VALUES (1,1,?)', (os.urandom(2 * 1024 * 1024),))
            db.execute('CREATE INDEX queue_parent ON queues(parent)')
            db.execute('CREATE VIEW private_identity AS SELECT secret FROM credentials')
            db.execute('CREATE TRIGGER private_trigger AFTER DELETE ON credentials BEGIN DELETE FROM queues; END')
            db.execute("CREATE VIRTUAL TABLE private_fts USING fts5(secret, content='credentials', content_rowid='id')")
            db.execute('INSERT INTO private_fts(rowid,secret) VALUES (1,?)', (self.marker * 10000,))
            db.execute('CREATE VIRTUAL TABLE contentless_fts USING fts5(secret,content=\'\')')
            db.execute('INSERT INTO contentless_fts(secret) VALUES (?)', (self.marker,))
            db.execute('ANALYZE')
            db.execute('PRAGMA user_version=98')
            db.execute('PRAGMA application_id=12345')
            db.commit()
        self.before = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.source.chmod(0o600)

    def prepare(self, root):
        return fixture.prepare_private_clone(self.source, root / 'state.sqlite3', root)

    def test_all_source_objects_and_internal_rows_removed_without_shrinking_or_holes(self):
        with fixture.private_workspace(self.root) as root:
            database = root / 'state.sqlite3'
            report = self.prepare(root)
            self.assertEqual(os.stat(root).st_mode & 0o777, 0o700)
            self.assertEqual(os.stat(database).st_mode & 0o777, 0o600)
            self.assertEqual(report['source']['bytes'], report['fixture']['bytes'])
            self.assertEqual(report['source']['bytes'], report['fixture']['first_hole'])
            self.assertGreaterEqual(report['fixture']['allocated_bytes'], report['source']['bytes'])
            self.assertEqual(report['empty_schema']['reachable_source_objects'], 0)
            self.assertEqual(report['empty_schema']['internal_rows'], 0)
            self.assertTrue(report['source_unchanged'])
            self.assertFalse(report['export_permitted'])
            with closing(sqlite3.connect(database)) as db:
                self.assertEqual(db.execute('SELECT name FROM sqlite_schema').fetchall(), [('sqlite_sequence',)])
                self.assertEqual(db.execute('SELECT * FROM sqlite_sequence').fetchall(), [])
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 0)
                self.assertEqual(db.execute('PRAGMA application_id').fetchone()[0], 0)
                with self.assertRaises(sqlite3.OperationalError):
                    db.execute('SELECT * FROM private_fts WHERE private_fts MATCH ?', (self.marker,))
            # This feature expressly allows private forensic residue, but never
            # claims secure erasure or permits exporting the database.
            data = database.read_bytes()
            self.assertIn(self.marker.encode(), data)
            # Forensic residue must be confined to actual SQLite freelist
            # pages, not slack in the surviving schema/sequence b-trees.
            page_size = report['empty_schema']['page_size']
            trunk = int.from_bytes(data[32:36], 'big')
            free_pages = set()
            while trunk:
                free_pages.add(trunk)
                offset = (trunk - 1) * page_size
                count = int.from_bytes(data[offset+4:offset+8], 'big')
                for index in range(count):
                    begin = offset + 8 + 4 * index
                    free_pages.add(int.from_bytes(data[begin:begin+4], 'big'))
                trunk = int.from_bytes(data[offset:offset+4], 'big')
            self.assertEqual(len(free_pages), report['empty_schema']['freelist_count'])
            for page in range(1, report['empty_schema']['page_count'] + 1):
                if page not in free_pages:
                    active = data[(page-1)*page_size:page*page_size]
                    for original in (self.marker, 'credentials', 'private_fts', 'private_identity', 'private_trigger'):
                        self.assertNotIn(original.encode(), active)
            self.assertNotIn(self.marker, str(report))
        self.assertFalse(root.exists())
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), self.before)

    def test_extended_acl_is_refused_before_private_data_is_cloned(self):
        with fixture.private_workspace(self.root) as root:
            subprocess.run(['/bin/chmod', '+a', 'everyone allow read,search', str(root)], check=True)
            try:
                with self.assertRaisesRegex(fixture.FixturePreparationError, 'Extended ACL'):
                    self.prepare(root)
                self.assertEqual(list(root.iterdir()), [])
            finally:
                subprocess.run(['/bin/chmod', '-N', str(root)], check=True)

    def test_copied_extended_attributes_are_removed_only_from_clone(self):
        name = 'com.ribbonfield.private-marker'
        library = fixture._system()
        put = library.setxattr
        put.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p,
                        ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int]
        put.restype = ctypes.c_int
        value = self.marker.encode()
        self.assertEqual(put(os.fsencode(self.source), name.encode(), value, len(value), 0, 0), 0)
        with fixture.private_workspace(self.root) as root:
            report = self.prepare(root)
            self.assertGreaterEqual(report['extended_attributes_removed'], 1)
            self.assertEqual(fixture._extended_attributes(root / 'state.sqlite3'), [])
        get = library.getxattr
        get.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p,
                        ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int]
        get.restype = ctypes.c_ssize_t
        result = ctypes.create_string_buffer(len(value))
        self.assertEqual(get(os.fsencode(self.source), name.encode(), result, len(value), 0, 0), len(value))
        self.assertEqual(result.raw, value)

    def test_error_cleans_partially_prepared_clone_without_leaking_source_text(self):
        with fixture.private_workspace(self.root) as root:
            with patch.object(fixture, '_scrub', side_effect=sqlite3.DatabaseError(self.marker)):
                with self.assertRaises(fixture.FixturePreparationError) as raised:
                    self.prepare(root)
            self.assertNotIn(self.marker, str(raised.exception))
            self.assertFalse((root / 'state.sqlite3').exists())
            self.assertEqual(list(root.iterdir()), [])

    def test_hot_wal_is_refused_without_a_clone(self):
        with closing(sqlite3.connect(self.source)) as source:
            source.execute('PRAGMA journal_mode=WAL')
            source.execute('PRAGMA wal_autocheckpoint=0')
            source.execute('INSERT INTO credentials(secret) VALUES (?)', (self.marker,))
            source.commit()
            with fixture.private_workspace(self.root) as root:
                with self.assertRaisesRegex(fixture.FixturePreparationError, 'pending journals'):
                    self.prepare(root)
                self.assertEqual(list(root.iterdir()), [])

    def test_existing_target_and_nonprivate_workspace_are_refused(self):
        with fixture.private_workspace(self.root) as root:
            root.chmod(0o755)
            with self.assertRaisesRegex(fixture.FixturePreparationError, '0700'):
                self.prepare(root)
            root.chmod(0o700)
            (root / 'state.sqlite3').write_text('existing')
            with self.assertRaisesRegex(fixture.FixturePreparationError, 'new direct child'):
                self.prepare(root)
            self.assertEqual((root / 'state.sqlite3').read_text(), 'existing')

    def test_source_hash_change_and_auto_vacuum_are_refused(self):
        with fixture.private_workspace(self.root) as root:
            with self.assertRaisesRegex(fixture.FixturePreparationError, 'hash does not match'):
                fixture.prepare_private_clone(self.source, root / 'state.sqlite3', root, expected_sha256='0' * 64)
            source = self.root / 'auto.sqlite3'
            with closing(sqlite3.connect(source)) as db:
                db.execute('PRAGMA auto_vacuum=FULL')
                db.execute('CREATE TABLE private_rows(secret TEXT)')
                db.commit()
            with self.assertRaisesRegex(fixture.FixturePreparationError, 'auto-vacuum'):
                fixture.prepare_private_clone(source, root / 'state.sqlite3', root)
            self.assertFalse((root / 'state.sqlite3').exists())

    def test_source_immutable_flags_are_refused_before_cloning(self):
        os.chflags(self.source, stat.UF_IMMUTABLE)
        try:
            with fixture.private_workspace(self.root) as root:
                with self.assertRaisesRegex(fixture.FixturePreparationError, 'prevent private clone cleanup'):
                    self.prepare(root)
                self.assertEqual(list(root.iterdir()), [])
        finally:
            os.chflags(self.source, 0)

    def test_clone_failure_never_falls_back_to_a_physical_copy(self):
        with fixture.private_workspace(self.root) as root:
            with patch.object(fixture, '_clone', side_effect=fixture.FixturePreparationError('clone refused')), \
                    patch.object(fixture.shutil, 'copyfile', side_effect=AssertionError('physical fallback')):
                with self.assertRaisesRegex(fixture.FixturePreparationError, 'clone refused'):
                    self.prepare(root)
            self.assertEqual(list(root.iterdir()), [])

    def test_production_capacity_preflight_is_unchanged_and_failure_cleans_clone(self):
        required = self.source.stat().st_size * 2 + 1024 * 1024
        usage = SimpleNamespace(free=required - 1)
        with fixture.private_workspace(self.root) as root:
            with patch.object(fixture.shutil, 'disk_usage', return_value=usage):
                with self.assertRaisesRegex(RuntimeError, 'Insufficient free space for the update rollback snapshot'):
                    self.prepare(root)
            self.assertEqual(list(root.iterdir()), [])

    def test_source_modified_during_clone_is_detected(self):
        clone = fixture._clone
        def changed(source, destination):
            clone(source, destination)
            with closing(sqlite3.connect(source)) as db:
                db.execute('INSERT INTO credentials(secret) VALUES (?)', (self.marker,))
                db.commit()
        with fixture.private_workspace(self.root) as root, patch.object(fixture, '_clone', changed):
            with self.assertRaisesRegex(fixture.FixturePreparationError, 'source changed'):
                self.prepare(root)
            self.assertEqual(list(root.iterdir()), [])

    def test_cleanup_removes_every_residual_copy_on_exception(self):
        root = Path(tempfile.mkdtemp(prefix='private-upgrade-fixture-', dir=self.root))
        with self.assertRaisesRegex(RuntimeError, 'qualification failed'):
            with fixture.cleanup_on_exit(root):
                self.prepare(root)
                (root / 'rollback.sqlite3').write_bytes(b'private residual copy')
                raise RuntimeError('qualification failed')
        self.assertFalse(root.exists())
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), self.before)

    def test_removed_root_does_not_hide_unexpected_guardian_failure(self):
        root = Path(tempfile.mkdtemp(prefix='private-upgrade-fixture-', dir=self.root))
        with self.assertRaisesRegex(fixture.FixturePreparationError, 'could not be verified'):
            with fixture.cleanup_on_exit(root) as cleanup:
                self.prepare(root)
                os.kill(cleanup['guardian_pid'], signal.SIGKILL)
        self.assertFalse(root.exists())
        self.assertEqual(cleanup['exit_status'], -signal.SIGKILL)
        self.assertTrue(cleanup['root_removed'])
        self.assertFalse(cleanup['verified'])

    def test_sigterm_runs_private_workspace_cleanup(self):
        root = Path(tempfile.mkdtemp(prefix='private-upgrade-fixture-', dir=self.root))
        code = """import sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from private_upgrade_fixture import cleanup_on_exit
with cleanup_on_exit(Path(sys.argv[2])):
 Path(sys.argv[2], 'ready').touch()
 time.sleep(30)
"""
        process = subprocess.Popen([sys.executable, '-c', code, str(ROOT / 'scripts'), str(root)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            import time
            end = time.monotonic() + 5
            while not (root / 'ready').exists() and process.poll() is None and time.monotonic() < end:
                time.sleep(.01)
            self.assertTrue((root / 'ready').exists())
            process.send_signal(signal.SIGTERM)
            process.communicate(timeout=5)
            self.assertFalse(root.exists())
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_rollback_measurement_compares_actual_full_copy_and_restore(self):
        with fixture.private_workspace(self.root) as root:
            self.prepare(root)
            database = root / 'state.sqlite3'
            with closing(sqlite3.connect(database)) as db:
                db.execute('CREATE TABLE anonymized_rows(value TEXT)')
                db.execute("INSERT INTO anonymized_rows VALUES ('redacted')")
                db.commit()
            result = fixture.measure_rollback_io(database, root)
            self.assertTrue(result['performed'])
            self.assertTrue(result['restore_performed'])
            for key in ('backup', 'direct_copy', 'restored'):
                self.assertEqual(result[key]['bytes'], self.source.stat().st_size)
                self.assertGreaterEqual(result[key]['allocated_bytes'], self.source.stat().st_size)
            with closing(sqlite3.connect(database)) as db:
                self.assertEqual(db.execute('SELECT * FROM anonymized_rows').fetchall(), [('redacted',)])

    def test_sigkill_controller_still_removes_private_residual_copies(self):
        root = Path(tempfile.mkdtemp(prefix='private-upgrade-fixture-', dir=self.root))
        code = """import sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from private_upgrade_fixture import cleanup_on_exit,prepare_private_clone
root=Path(sys.argv[2])
with cleanup_on_exit(root):
 prepare_private_clone(Path(sys.argv[3]),root/'state.sqlite3',root)
 (root/'rollback.sqlite3').write_bytes(b'private residual rollback')
 (root/'ready').touch()
 time.sleep(30)
"""
        process = subprocess.Popen([sys.executable, '-c', code, str(ROOT / 'scripts'), str(root), str(self.source)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            import time
            end = time.monotonic() + 10
            while not (root / 'ready').exists() and process.poll() is None and time.monotonic() < end:
                time.sleep(.01)
            self.assertTrue((root / 'ready').exists())
            process.kill()
            process.communicate(timeout=5)
            end = time.monotonic() + 5
            while root.exists() and time.monotonic() < end:
                time.sleep(.01)
            self.assertFalse(root.exists())
            self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), self.before)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_real_snapshot_and_restore_remain_full_allocated_operations(self):
        sys.path.insert(0, str(ROOT / 'plugins/agent-coord/scripts'))
        from agent_coord import runtime_update
        with fixture.private_workspace(self.root) as root:
            self.prepare(root)
            database = root / 'state.sqlite3'
            with closing(sqlite3.connect(database)) as db:
                db.execute('CREATE TABLE anonymized_rows(id INTEGER PRIMARY KEY, value TEXT)')
                db.execute("INSERT INTO anonymized_rows VALUES (1,'redacted')")
                db.commit()
            directory = root / 'runtime'
            directory.mkdir(mode=0o700)
            update = {'id': 'private-fixture-marker', 'previous': 'previous', 'target': 'candidate'}
            update = runtime_update.snapshot(str(database), directory, update)
            backup = directory / 'update-backup.sqlite3'
            self.assertEqual(fixture.extent_evidence(backup)['bytes'], self.source.stat().st_size)
            with closing(sqlite3.connect(database)) as db:
                db.execute('DELETE FROM anonymized_rows')
                db.commit()
            runtime_update.restore(str(database), directory, update)
            self.assertEqual(fixture.extent_evidence(database)['bytes'], self.source.stat().st_size)
            with closing(sqlite3.connect(database)) as db:
                self.assertEqual(db.execute('SELECT * FROM anonymized_rows').fetchall(), [(1,'redacted')])
                self.assertEqual(db.execute('PRAGMA integrity_check').fetchall(), [('ok',)])
        self.assertFalse(root.exists())
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), self.before)


@unittest.skipUnless(sys.platform == 'darwin', 'APFS clonefile is macOS-only')
class SignedFixtureInjectionTests(unittest.TestCase):
    setUp = PrivateFixtureTests.setUp
    prepare = PrivateFixtureTests.prepare
    def test_opt_in_scrubs_before_first_application_seed_and_preserves_size_gate(self):
        import validate_signed_runtime_responsiveness as gate
        with fixture.private_workspace(self.root) as root:
            run = object.__new__(gate.SignedUpgrade)
            run.root, run.database, run.snapshot = root, root / 'state.sqlite3', self.source
            run.private_apfs_fixture, run.candidate_only = True, False
            run.report, run.sources, run.manifest_thread = {'workload': {}}, [], None
            run.old = (root / 'previous', {})
            def seed(database, workspace, rows):
                self.assertEqual(fixture.verify_empty(database)['reachable_source_objects'], 0)
                with closing(sqlite3.connect(database)) as db:
                    db.execute('CREATE TABLE anonymized_inventory(id INTEGER)')
                    db.commit()
            run.launch_signed = unittest.mock.Mock(side_effect=RuntimeError('application boot deliberately omitted'))
            with patch.object(gate, 'seed_inventory', side_effect=seed), \
                    patch.object(gate, 'copy_saved_histories', return_value=[]), \
                    patch.object(gate, 'match_snapshot_size', wraps=gate.match_snapshot_size) as size:
                with self.assertRaisesRegex(RuntimeError, 'boot deliberately omitted'):
                    run.start()
                size.assert_called_once_with(self.source, run.database)
            self.assertEqual(run.report['workload']['source_database_bytes'], self.source.stat().st_size)
            self.assertEqual(run.report['workload']['fixture_database_bytes'], self.source.stat().st_size)
            self.assertFalse(run.report['workload']['fixture_preparation']['export_permitted'])

    def test_opt_in_failed_gate_still_deletes_private_residual_root(self):
        import validate_signed_runtime_responsiveness as gate
        root = Path(tempfile.mkdtemp(prefix='private-upgrade-fixture-', dir=self.root))
        run = SimpleNamespace(root=root, private_apfs_fixture=True, report={}, persist=lambda: None)
        def refused(runner):
            self.prepare(root)
            (root / 'rollback.sqlite3').write_bytes(b'private residual copy')
            raise RuntimeError('gate failure')
        with patch.object(gate, '_run_and_clean_up', side_effect=refused):
            with self.assertRaisesRegex(RuntimeError, 'gate failure'):
                gate.run_and_clean_up(run)
        self.assertFalse(root.exists())

    def validation_cleanup(self, failing):
        import runtime_upgrade_validation as validation
        import validate_signed_runtime_responsiveness as gate

        output = self.root / 'cleanup-receipt.json'
        run = validation.Validation(output, 1, 'idle-control')
        run.root.chmod(0o700)
        self.addCleanup(lambda: fixture.shutil.rmtree(run.root, ignore_errors=True))
        run.private_apfs_fixture, run.candidate_only = True, False
        run.report.update(passed=not failing, violations=[])
        run.persist = lambda: output.write_text(json.dumps(run.report))
        child = None
        reaper = None

        def execute():
            nonlocal child, reaper
            self.prepare(run.root)
            (run.root / 'rollback.sqlite3').write_bytes(b'private residual rollback')
            child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)', str(run.root)],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            # Validation proves child exit with kill(pid, 0). Reap the child as
            # an ordinary fixture parent would, without changing that check.
            reaper = threading.Thread(target=child.wait)
            reaper.start()
            if failing:
                raise RuntimeError('qualification deliberately failed')

        run.execute = execute
        try:
            if failing:
                with self.assertRaisesRegex(RuntimeError, 'qualification deliberately failed'):
                    gate.run_and_clean_up(run)
            else:
                self.assertEqual(gate.run_and_clean_up(run), 0)
            saved = json.loads(output.read_text())
            cleanup = saved['private_workspace_cleanup']
            self.assertEqual(cleanup['exit_status'], 0)
            self.assertTrue(cleanup['verified'])
            self.assertTrue(cleanup['root_removed'])
            self.assertNotEqual(child.pid, cleanup['guardian_pid'])
            self.assertNotIn(str(cleanup['guardian_pid']), saved['leftover_processes'])
            self.assertIn(str(child.pid), saved['leftover_processes'])
            self.assertFalse(saved['leftover_processes'][str(child.pid)]['alive_after_cleanup'])
            self.assertEqual(child.returncode, -signal.SIGTERM)
            self.assertFalse(run.root.exists())
            self.assertEqual(saved['passed'], not failing)
            self.assertEqual(saved['violations'], [])
            if failing:
                self.assertEqual(saved['execution_error']['type'], 'RuntimeError')
            self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), self.before)
        finally:
            if child is not None and child.poll() is None:
                child.kill()
            if reaper is not None:
                reaper.join(timeout=5)

    def test_real_validation_close_preserves_guardian_and_terminates_owned_child(self):
        self.validation_cleanup(False)

    def test_real_validation_close_preserves_failed_gate_and_verified_guardian_exit(self):
        self.validation_cleanup(True)


class DefaultInjectionTests(unittest.TestCase):
    def test_candidate_only_opt_in_is_rejected_before_a_workspace_exists(self):
        import validate_signed_runtime_responsiveness as gate
        with patch.object(gate.Validation, '__init__') as initialize:
            with self.assertRaisesRegex(ValueError, 'guarded upgrade'):
                gate.SignedUpgrade(SimpleNamespace(private_apfs_fixture=True,candidate_only=True), None, None)
            initialize.assert_not_called()

    def test_default_signed_fixture_does_not_invoke_clone_preparation(self):
        import validate_signed_runtime_responsiveness as gate
        run = object.__new__(gate.SignedUpgrade)
        run.database, run.root, run.sources = Path('unused.sqlite3'), Path('.'), []
        with patch.object(fixture, 'prepare_private_clone') as prepare, \
                patch.object(gate, 'seed_inventory', side_effect=RuntimeError('normal seed entry')):
            with self.assertRaisesRegex(RuntimeError, 'normal seed entry'):
                run.start()
            prepare.assert_not_called()
