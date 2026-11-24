#!/usr/bin/env python3
"""Opt-in, local-only APFS upgrade fixture with private residual free pages.

This is logical isolation, not forensic erasure. The stand-alone CLI reports
fixture preparation at rest; it cannot issue a signed-upgrade release receipt. The clone and every rollback
copy must remain in an owned 0700 ephemeral workspace and must never be exported.
Normal exits and exceptions clean the workspace. An isolated EOF guardian also
removes it after controller termination, including SIGKILL. A host power loss
can still leave a private root; no artifact from this mode may be exported.
"""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import select
import subprocess
import sqlite3
import stat
import sys
import tempfile
import threading
import time


class FixturePreparationError(RuntimeError):
    """Preparation refused without disclosing source schema or contents."""


class _Statfs(ctypes.Structure):
    # Darwin public struct statfs, sys/mount.h (__DARWIN_STRUCT_STATFS64).
    _fields_ = [('block_size', ctypes.c_uint32), ('io_size', ctypes.c_int32),
        ('counts', ctypes.c_uint64 * 5), ('fsid', ctypes.c_int32 * 2),
        ('owner', ctypes.c_uint32), ('type', ctypes.c_uint32),
        ('flags', ctypes.c_uint32), ('subtype', ctypes.c_uint32),
        ('filesystem', ctypes.c_char * 16), ('mount', ctypes.c_char * 1024),
        ('device', ctypes.c_char * 1024), ('extended_flags', ctypes.c_uint32),
        ('reserved', ctypes.c_uint32 * 7)]


def _system():
    if sys.platform != 'darwin':
        raise FixturePreparationError('Private clone fixtures require macOS APFS')
    return ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)


def _require_apfs(path):
    library = _system()
    method = library.statfs
    method.argtypes = [ctypes.c_char_p, ctypes.POINTER(_Statfs)]
    method.restype = ctypes.c_int
    result = _Statfs()
    if method(os.fsencode(path), ctypes.byref(result)) != 0 or result.filesystem != b'apfs':
        raise FixturePreparationError('Private clone fixtures require an APFS volume')


def _reject_extended_acl(path):
    library = _system()
    get_acl, get_entry, release = library.acl_get_file, library.acl_get_entry, library.acl_free
    get_acl.argtypes, get_acl.restype = [ctypes.c_char_p, ctypes.c_int], ctypes.c_void_p
    get_entry.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)]
    get_entry.restype = ctypes.c_int
    release.argtypes, release.restype = [ctypes.c_void_p], ctypes.c_int
    ctypes.set_errno(0)
    acl = get_acl(os.fsencode(path), 0x100)  # Darwin ACL_TYPE_EXTENDED.
    if not acl:
        if ctypes.get_errno() == errno.ENOENT:
            return
        raise FixturePreparationError('Private fixture ACL could not be verified')
    try:
        entry = ctypes.c_void_p()
        if get_entry(acl, 0, ctypes.byref(entry)) == 0:
            raise FixturePreparationError('Extended ACL entries are not permitted on private fixture storage')
        if ctypes.get_errno() != errno.EINVAL:
            raise FixturePreparationError('Private fixture ACL could not be verified')
    finally:
        release(acl)


def _private_root(root):
    root = Path(root)
    info = root.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise FixturePreparationError('Fixture workspace must be an owned private 0700 directory')
    _require_apfs(root)
    _reject_extended_acl(root)
    return root.resolve(strict=True)


def _hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _identity(path):
    info = path.stat()
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def extent_evidence(path):
    info = path.stat()
    allocated = info.st_blocks * 512
    if allocated < info.st_size:
        raise FixturePreparationError('Full-size fixture has insufficient allocated extents')
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        # On this opt-in macOS path, failure to prove no hole is a refusal.
        if os.lseek(descriptor, 0, os.SEEK_HOLE) != info.st_size:
            raise FixturePreparationError('Sparse holes are not permitted in upgrade fixtures')
    finally:
        os.close(descriptor)
    return {'bytes': info.st_size, 'allocated_bytes': allocated, 'first_hole': info.st_size}


def _source_geometry(source):
    for suffix in ('-wal', '-journal'):
        sidecar = Path(str(source) + suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            raise FixturePreparationError('Source must be an immutable offline database without pending journals')
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
        geometry = {key: db.execute('PRAGMA ' + key).fetchone()[0]
                    for key in ('page_size', 'page_count', 'auto_vacuum')}
    if geometry['auto_vacuum'] != 0:
        raise FixturePreparationError('Source auto-vacuum mode cannot preserve the required page count')
    if geometry['page_size'] * geometry['page_count'] != source.stat().st_size:
        raise FixturePreparationError('Source file length must equal its complete committed page geometry')
    return geometry


def _clone(source, destination):
    library = _system()
    method = library.clonefile
    method.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
    method.restype = ctypes.c_int
    if method(os.fsencode(source), os.fsencode(destination), 0) != 0:
        raise FixturePreparationError('APFS clonefile failed; no physical-copy fallback is permitted')
    destination.chmod(0o600)


def _extended_attributes(path):
    library = _system()
    method = library.listxattr
    method.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
    method.restype = ctypes.c_ssize_t
    size = method(os.fsencode(path), None, 0, 1)  # Darwin XATTR_NOFOLLOW.
    if size < 0:
        raise FixturePreparationError('Private clone extended attributes could not be inspected')
    if not size:
        return []
    buffer = ctypes.create_string_buffer(size)
    length = method(os.fsencode(path), buffer, size, 1)
    if length < 0:
        raise FixturePreparationError('Private clone extended attributes changed during verification')
    return [name for name in buffer.raw[:length].split(b'\0') if name]


def _clear_extended_attributes(path):
    # clonefile also copies extended attributes. Those are reachable file
    # content, not permitted residual SQLite free pages, so remove every one.
    library = _system()
    remove = library.removexattr
    remove.argtypes, remove.restype = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int], ctypes.c_int
    names = _extended_attributes(path)
    for name in names:
        if remove(os.fsencode(path), name, 1) != 0:
            raise FixturePreparationError('Private clone extended attributes could not be removed')
    if _extended_attributes(path):
        raise FixturePreparationError('Private clone retains reachable extended attributes')
    return len(names)


def _identifier(value):
    return '"' + value.replace('"', '""') + '"'


def verify_empty(database):
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
        db.execute('PRAGMA query_only=ON')
        objects = db.execute('SELECT type,name FROM sqlite_schema').fetchall()
        if any(kind != 'table' or name != 'sqlite_sequence' for kind, name in objects):
            raise FixturePreparationError('Private fixture retains a reachable source schema object')
        if objects:
            columns = [row[1] for row in db.execute('PRAGMA table_info(sqlite_sequence)')]
            if columns != ['name', 'seq'] or db.execute('SELECT COUNT(*) FROM sqlite_sequence').fetchone()[0]:
                raise FixturePreparationError('Private fixture retains nonempty internal metadata')
        tables = {row[1] for row in db.execute('PRAGMA table_list') if row[0] == 'main'}
        if tables - {'sqlite_schema', 'sqlite_sequence'}:
            raise FixturePreparationError('Private fixture retains a reachable virtual or shadow table')
        if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise FixturePreparationError('Empty private fixture failed SQLite integrity verification')
        return {'reachable_source_objects': 0, 'internal_rows': 0,
                'internal_tables': len(objects),
                'page_size': db.execute('PRAGMA page_size').fetchone()[0],
                'page_count': db.execute('PRAGMA page_count').fetchone()[0],
                'freelist_count': db.execute('PRAGMA freelist_count').fetchone()[0]}


def _scrub(database):
    with closing(sqlite3.connect(database)) as db:
        db.execute('PRAGMA foreign_keys=OFF')
        db.execute('PRAGMA trusted_schema=OFF')
        db.execute('PRAGMA secure_delete=FAST')
        db.execute('PRAGMA journal_mode=DELETE')
        if db.execute('PRAGMA foreign_keys').fetchone()[0]:
            raise FixturePreparationError('Foreign-key enforcement could not be disabled for clone preparation')
        db.execute('BEGIN IMMEDIATE')
        for kind in ('trigger', 'view'):
            for name, in db.execute('SELECT name FROM sqlite_schema WHERE type=?', (kind,)).fetchall():
                db.execute('DROP ' + kind.upper() + ' ' + _identifier(name))
        # Drop virtual roots first, allowing SQLite itself to remove their shadow
        # structures. No writable_schema, extension loading or row parsing occurs.
        virtuals = [row[1] for row in db.execute('PRAGMA table_list')
                    if row[0] == 'main' and row[2] == 'virtual']
        for name in virtuals:
            db.execute('DROP TABLE ' + _identifier(name))
        for name, in db.execute("SELECT name FROM sqlite_schema WHERE type='table'").fetchall():
            if name == 'sqlite_sequence':
                db.execute('DELETE FROM sqlite_sequence')
            else:
                db.execute('DROP TABLE ' + _identifier(name))
        db.execute('PRAGMA user_version=0')
        db.execute('PRAGMA application_id=0')
        db.commit()


def _capacity(database, root):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins/agent-coord/scripts'))
    from agent_coord.runtime_update import check_capacity
    # Exactly the production preflight: two full database copies plus 1 MiB.
    check_capacity(str(database), root)


def prepare_private_clone(snapshot, database, workspace, *, expected_sha256=None):
    """Prepare before application access. Caller must always destroy workspace.

    Source names and values are never returned. Receipts contain only geometry,
    timings and a content hash. Residual free pages make exporting this clone or
    any of its rollback copies forbidden even after logical isolation succeeds.
    """
    root = _private_root(workspace)
    source = Path(snapshot).resolve(strict=True)
    destination = Path(database)
    if destination.parent.resolve(strict=True) != root or destination.exists() or destination.is_symlink():
        raise FixturePreparationError('Fixture database must be a new direct child of its private workspace')
    destination = root / destination.name
    if source.is_relative_to(root) or not source.is_file():
        raise FixturePreparationError('Source must remain outside the ephemeral fixture workspace')
    if source.stat().st_dev != root.stat().st_dev:
        raise FixturePreparationError('Source and fixture must reside on the same APFS volume')
    protected = 0
    for name in ('UF_IMMUTABLE', 'SF_IMMUTABLE', 'UF_APPEND', 'SF_APPEND'):
        protected |= getattr(stat, name, 0)
    if getattr(source.stat(), 'st_flags', 0) & protected:
        raise FixturePreparationError('Source filesystem flags would prevent private clone cleanup')
    original = _identity(source)
    source_extent = extent_evidence(source)
    geometry = _source_geometry(source)
    _capacity(source, root)  # Refuse before cloning when the original preflight cannot pass.
    source_hash = _hash(source)
    if expected_sha256 is not None and source_hash != expected_sha256:
        raise FixturePreparationError('Immutable source hash does not match its verification receipt')
    if _identity(source) != original:
        raise FixturePreparationError('Source changed before cloning')
    started = time.monotonic()
    try:
        _clone(source, destination)
        attributes_removed = _clear_extended_attributes(destination)
        _reject_extended_acl(destination)
        clone_seconds = time.monotonic() - started
        scrub_started = time.monotonic()
        _scrub(destination)
        scrub_seconds = time.monotonic() - scrub_started
        empty = verify_empty(destination)
        fixture_extent = extent_evidence(destination)
        if (empty['page_count'] != geometry['page_count'] or empty['page_size'] != geometry['page_size']
                or fixture_extent['bytes'] != source_extent['bytes']):
            raise FixturePreparationError('Clone preparation changed the required file or page geometry')
        _capacity(destination, root)
        if _identity(source) != original or _hash(source) != source_hash:
            raise FixturePreparationError('Immutable source changed during fixture preparation')
        return {'method': 'private-apfs-clone-and-ddl', 'source_unchanged': True,
                'source_sha256': source_hash, 'source': source_extent, 'fixture': fixture_extent,
                'empty_schema': empty, 'clone_seconds': clone_seconds, 'scrub_seconds': scrub_seconds,
                'contains_private_freelist_residue': True, 'export_permitted': False,
                'extended_attributes_removed': attributes_removed, 'extended_attributes_remaining': 0,
                'capacity_preflight': 'unchanged-two-full-copies-plus-one-MiB'}
    except BaseException as error:
        for suffix in ('', '-wal', '-shm', '-journal'):
            Path(str(destination) + suffix).unlink(missing_ok=True)
        if isinstance(error, sqlite3.Error):
            raise FixturePreparationError('Private clone SQLite preparation failed') from None
        raise


def _guardian_identity(root, device, inode):
    if not root.name.startswith(('private-upgrade-fixture-', 'ribbon-live-acceptance-')):
        raise FixturePreparationError('Cleanup guardian requires a generated fixture workspace')
    info = root.lstat()
    if (info.st_dev, info.st_ino) != (device, inode):
        raise FixturePreparationError('Cleanup workspace identity changed')
    return _private_root(root)


def _guardian_main(pipe_fd, ready_fd):
    if not stat.S_ISFIFO(os.fstat(pipe_fd).st_mode):
        return 2
    # Initialization and liveness share an inherited pipe. Keeping the root
    # out of argv prevents the gate's strict command-line ownership scan from
    # mistaking its cleanup supervisor for a backend left behind by Stop.
    with os.fdopen(pipe_fd, 'rb') as pipe:
        configuration = pipe.readline(65537)
        if not configuration.endswith(b'\n') or len(configuration) > 65536:
            return 2
        root, device, inode = json.loads(configuration)
        root = _guardian_identity(Path(root), device, inode)
        os.write(ready_fd, b'ready\n')
        os.close(ready_fd)
        try:
            while pipe.read(64):
                pass
        finally:
            if root.exists():
                _guardian_identity(root, device, inode)
                shutil.rmtree(root)
    return 0


@contextmanager
def cleanup_on_exit(root):
    """Own teardown and supervise crash cleanup of every residual copy.

    The guardian receives no coordination identity or provider credentials. Only
    this controller holds its noninheritable write pipe; fixture backends cannot
    keep it alive. No PID is signalled by the guardian, and its root is bound to
    the original owned-directory device/inode before any private clone exists.
    """
    root = _private_root(root)
    info = root.stat()
    _guardian_identity(root, info.st_dev, info.st_ino)
    pipe_read, pipe_write = os.pipe()
    ready_read, ready_write = os.pipe()
    guardian = None
    previous = None
    receipt = {'guardian_pid': None, 'ready': False, 'exit_status': None,
               'root_removed': False, 'verified': False}
    try:
        guardian = subprocess.Popen([sys.executable, '-I', '-B', str(Path(__file__).resolve()),
            '--cleanup-guardian', str(pipe_read), str(ready_write)],
            pass_fds=(pipe_read, ready_write), close_fds=True, start_new_session=True,
            env={'PATH': '/usr/bin:/bin', 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONNOUSERSITE': '1'},
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.close(pipe_read)
        pipe_read = None
        os.close(ready_write)
        ready_write = None
        receipt['guardian_pid'] = guardian.pid
        configuration = memoryview(json.dumps([str(root), info.st_dev, info.st_ino]).encode() + b'\n')
        while configuration:
            configuration = configuration[os.write(pipe_write, configuration):]
        ready, _, _ = select.select([ready_read], [], [], 10)
        if not ready or os.read(ready_read, 32) != b'ready\n':
            raise FixturePreparationError('Private workspace cleanup guardian did not become ready')
        os.close(ready_read)
        ready_read = None
        receipt['ready'] = True
        if threading.current_thread() is threading.main_thread():
            previous = signal.getsignal(signal.SIGTERM)
            def terminate(number, frame):
                raise SystemExit(128 + number)
            signal.signal(signal.SIGTERM, terminate)
        yield receipt
    finally:
        try:
            if root.exists():
                _guardian_identity(root, info.st_dev, info.st_ino)
                shutil.rmtree(root)
        finally:
            for descriptor in (pipe_read, pipe_write, ready_read, ready_write):
                if descriptor is not None:
                    os.close(descriptor)
            if previous is not None:
                signal.signal(signal.SIGTERM, previous)
            if guardian is not None:
                try:
                    status = guardian.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    # Its sole job remains cleanup; never stop it while files
                    # could still contain residual source pages.
                    raise FixturePreparationError('Private workspace cleanup guardian did not finish') from None
                receipt.update(exit_status=status, root_removed=not root.exists())
                receipt['verified'] = receipt['ready'] and status == 0 and receipt['root_removed']
                if not receipt['verified']:
                    raise FixturePreparationError('Private workspace cleanup could not be verified')


@contextmanager
def private_workspace(parent=None):
    """Own the complete lifetime, including all residual rollback copies."""
    with tempfile.TemporaryDirectory(prefix='private-upgrade-fixture-', dir=parent) as directory:
        root = Path(directory).resolve()
        root.chmod(0o700)
        with cleanup_on_exit(root):
            yield root


def measure_rollback_io(database, root, *, compare_restore=True):
    """Use actual runtime backup/restore, never clone or sparse substitutes."""
    from agent_coord import runtime_update
    root = _private_root(root)
    before = extent_evidence(database)
    _capacity(database, root)
    reserve = 2 * 1024**3
    # The optional demonstration also keeps an operating-space reserve; it
    # never weakens the production two-copy preflight above.
    if shutil.disk_usage(root).free < before['bytes'] + reserve:
        return {'performed': False, 'reason': 'optional-io-operating-space-reserve'}
    directory = root / 'rollback-measurement'
    directory.mkdir(mode=0o700)
    update = {'id': 'private-at-rest-measurement', 'previous': 'previous', 'target': 'candidate'}
    free_before = shutil.disk_usage(root).free
    started = time.monotonic()
    update = runtime_update.snapshot(str(database), directory, update)
    backup_seconds = time.monotonic() - started
    backup = directory / 'update-backup.sqlite3'
    backup_extent = extent_evidence(backup)
    if backup_extent['bytes'] != before['bytes']:
        raise FixturePreparationError('Rollback backup does not preserve the complete database size')
    result = {'performed': True, 'backup': backup_extent, 'backup_seconds': backup_seconds,
              'free_bytes_before_backup': free_before, 'free_bytes_after_backup': shutil.disk_usage(root).free,
              'restore_performed': False}
    if shutil.disk_usage(root).free < before['bytes'] + reserve:
        result['restore_skip_reason'] = 'optional-io-operating-space-reserve'
        return result
    if compare_restore:
        # Same source, destination volume, byte count and fsync as runtime
        # restore. Delete this private comparison before the real restore so
        # their temporary allocation never overlaps.
        comparison = root / 'restore-comparison.sqlite3'
        started = time.monotonic()
        shutil.copyfile(backup, comparison)
        with comparison.open('rb') as stream:
            os.fsync(stream.fileno())
        result['direct_copy_seconds'] = time.monotonic() - started
        result['direct_copy'] = extent_evidence(comparison)
        comparison.unlink()
    if shutil.disk_usage(root).free < before['bytes'] + reserve:
        result['restore_skip_reason'] = 'optional-io-operating-space-reserve'
        return result
    started = time.monotonic()
    runtime_update.restore(str(database), directory, update)
    result['restore_seconds'] = time.monotonic() - started
    result['restored'] = extent_evidence(database)
    result['restore_performed'] = True
    if result['restored']['bytes'] != before['bytes']:
        raise FixturePreparationError('Restored fixture changed the required full database length')
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
        if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise FixturePreparationError('Restored fixture failed SQLite integrity verification')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--private-apfs-fixture', action='store_true', required=True)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--source-sha256')
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--saved-manifest-thread')
    parser.add_argument('--rollback-io', action='store_true')
    args = parser.parse_args()
    source = args.source.resolve(strict=True)
    report = args.report.resolve()
    checkout = Path(__file__).resolve().parents[1]
    if report == source or report.is_relative_to(checkout):
        parser.error('Private fixture receipts must remain outside the checkout and source')
    report.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = report.parent.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700 or report.exists():
        parser.error('Pass a new receipt pathname under an owned private 0700 directory')
    receipt = {'schema_version': 1, 'receipt_kind': 'private-upgrade-fixture-at-rest',
               'source_unchanged': False, 'cleanup_complete': False,
               'backend_started': False, 'providers_started': False, 'passed': False}
    workspace = None
    original_hash = None
    try:
        with private_workspace(report.parent) as root:
            workspace = root
            database = root / 'state.sqlite3'
            receipt['preparation'] = prepare_private_clone(source, database, root,
                                                         expected_sha256=args.source_sha256)
            original_hash = receipt['preparation']['source_sha256']
            # Reuse precisely the signed gate's established redaction and
            # inventory projection; no alternative source sampling or text copy.
            from validate_signed_runtime_responsiveness import saved_inventory, seed_inventory, copy_saved_histories, match_snapshot_size
            dimensions = saved_inventory(source)
            seed_inventory(database, root, dimensions)
            receipt['saved_histories'] = copy_saved_histories(source, database, root,
                                                            manifest_thread=args.saved_manifest_thread)
            receipt['workload'] = match_snapshot_size(source, database)
            receipt['seeded_fixture'] = extent_evidence(database)
            import gc
            gc.collect()  # Close unused seed stores before exclusive rollback I/O.
            with closing(sqlite3.connect(database)) as db:
                checkpoint = db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
                if checkpoint[0]:
                    raise FixturePreparationError('Seed connections did not release the fixture for at-rest measurement')
            if args.rollback_io:
                receipt['rollback_io'] = measure_rollback_io(database, root)
            receipt['source_unchanged'] = _hash(source) == original_hash
            receipt['preparation_passed'] = True
            receipt['rollback_io_qualified'] = (not args.rollback_io or
                (receipt['rollback_io'].get('performed') and receipt['rollback_io'].get('restore_performed')))
            receipt['passed'] = receipt['source_unchanged'] and receipt['rollback_io_qualified']
    except BaseException as error:
        receipt['passed'] = False
        reason = ('rollback-capacity' if str(error).startswith('Insufficient free space for the update rollback snapshot.')
                  else 'private-fixture-preparation-or-measurement-refused')
        receipt['error'] = {'type': type(error).__name__, 'reason': reason}
        if original_hash is not None:
            receipt['source_unchanged'] = _hash(source) == original_hash
    finally:
        receipt['cleanup_complete'] = workspace is not None and not workspace.exists()
        receipt['passed'] = receipt['passed'] and receipt['cleanup_complete']
        descriptor = os.open(report, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w') as stream:
            stream.write(json.dumps(receipt, indent=2) + '\n')
        print(json.dumps({'passed': receipt['passed'], 'report': str(report),
                          'cleanup_complete': receipt['cleanup_complete']}))
    return 0 if receipt['passed'] else 1


if __name__ == '__main__':
    if sys.argv[1:2] == ['--cleanup-guardian']:
        if len(sys.argv) != 4:
            raise SystemExit(2)
        raise SystemExit(_guardian_main(*map(int, sys.argv[2:])))
    raise SystemExit(main())
