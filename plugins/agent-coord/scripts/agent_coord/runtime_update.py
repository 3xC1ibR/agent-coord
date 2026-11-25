"""On-disk update transaction, copy-on-write APFS snapshot, and rollback."""
from __future__ import annotations

from contextlib import closing
import ctypes
import errno
from functools import lru_cache
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import time

TERMINAL = {"committed", "rolled-back", "failed"}
CLONEFILE_UNSUPPORTED_ERRNOS = frozenset({errno.ENOTSUP, errno.EOPNOTSUPP, errno.EXDEV, errno.ENOSYS})


@lru_cache(maxsize=1)
def _darwin_clonefile():
    if sys.platform != "darwin":
        return None
    library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    function = library.clonefile
    function.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32]
    function.restype = ctypes.c_int

    def clone(source: Path, destination: Path) -> None:
        destination.unlink(missing_ok=True)
        if function(os.fsencode(source), os.fsencode(destination), 0) != 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code), str(source), None, str(destination))

    return clone


def clone_or_copy(source: Path, destination: Path) -> bool:
    """Clone via APFS clonefile(2) when supported; return True if zero-copy cloned."""
    clone = _darwin_clonefile()
    temporary = destination.with_name(f"{destination.name}.{os.getpid()}.temporary")
    temporary.unlink(missing_ok=True)
    try:
        if clone is not None:
            try:
                clone(source, temporary)
                os.replace(temporary, destination)
                return True
            except OSError as error:
                if error.errno not in CLONEFILE_UNSUPPORTED_ERRNOS:
                    raise
        shutil.copyfile(source, temporary)
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        return False
    finally:
        temporary.unlink(missing_ok=True)


def read(directory):
    from .desktop_runtime import read_json
    path = directory / "update.json"
    value = read_json(path) if path.exists() else None
    cancellation = directory / "update-cancellation.json"
    if value and cancellation.exists() and read_json(cancellation).get("id") == value["id"]:
        value["cancelled"] = True
    return value


def write(directory, value, **changes):
    from .desktop_runtime import atomic_json
    if changes.get("cancelled"):
        atomic_json(directory / "update-cancellation.json", {"id": value["id"]})
    current = read(directory)
    if current and current["id"] == value["id"]:
        value = {**value, **current}
    value = {**value, **changes, "updated_at": time.time()}
    atomic_json(directory / "update.json", value)
    return value


def check_capacity(database, directory):
    size = sum(path.stat().st_size for path in
               (Path(database), Path(str(database) + "-wal")) if path.exists())
    if shutil.disk_usage(directory).free < size * 2 + 1024 * 1024:
        raise RuntimeError("Insufficient free space for the update rollback snapshot.")


def clone_database_files_direct(database: Path, backup: Path, backup_wal: Path) -> bool:
    """Fast < 1 ms clonefile(2) refresh when coordinator writers are already quiescent."""
    clone = _darwin_clonefile()
    if clone is None or not database.exists():
        return False
    wal_source = Path(str(database) + "-wal")
    try:
        clone(database, backup)
        if wal_source.exists() and wal_source.stat().st_size > 0:
            clone(wal_source, backup_wal)
        else:
            backup_wal.unlink(missing_ok=True)
        return True
    except OSError as error:
        if error.errno in CLONEFILE_UNSUPPORTED_ERRNOS:
            return False
        raise


def _clone_database_snapshot(database: Path, backup: Path, backup_wal: Path) -> bool:
    clone = _darwin_clonefile()
    if clone is None:
        return False
    wal_source = Path(str(database) + "-wal")
    temp_db = backup.with_suffix(".temporary")
    temp_wal = backup_wal.with_suffix(".wal-temporary")
    temp_db.unlink(missing_ok=True)
    temp_wal.unlink(missing_ok=True)
    try:
        connection = None
        try:
            connection = sqlite3.connect(str(database), timeout=0.25, isolation_level=None)
            try:
                connection.execute("PRAGMA wal_checkpoint(PASSIVE)")
            except sqlite3.Error:
                pass
            try:
                connection.execute("BEGIN IMMEDIATE")
            except sqlite3.Error:
                pass
            if database.exists():
                clone(database, temp_db)
            else:
                sqlite3.connect(str(temp_db)).close()
            has_wal = wal_source.exists() and wal_source.stat().st_size > 0
            if has_wal:
                clone(wal_source, temp_wal)
        finally:
            if connection is not None:
                try:
                    if connection.in_transaction:
                        connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                connection.close()
        os.replace(temp_db, backup)
        if has_wal:
            os.replace(temp_wal, backup_wal)
        else:
            backup_wal.unlink(missing_ok=True)
        return True
    except OSError as error:
        if error.errno in CLONEFILE_UNSUPPORTED_ERRNOS:
            return False
        raise
    finally:
        temp_db.unlink(missing_ok=True)
        temp_wal.unlink(missing_ok=True)


def snapshot(database, directory, update):
    from .desktop_runtime import atomic_json, read_json
    database_path = Path(database)
    backup = directory / "update-backup.sqlite3"
    backup_wal = directory / "update-backup.sqlite3-wal"
    cloned = _clone_database_snapshot(database_path, backup, backup_wal)
    if not cloned:
        check_capacity(str(database_path), directory)
        temporary = backup.with_suffix(".temporary")
        temporary.unlink(missing_ok=True)
        with closing(sqlite3.connect(str(database_path))) as source, closing(sqlite3.connect(str(temporary))) as target:
            source.backup(target)
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, backup)
        backup_wal.unlink(missing_ok=True)
    schema = directory / "schema.json"
    atomic_json(directory / "update-backup.json", {
        "id": update["id"],
        "schema": read_json(schema) if schema.exists() else None,
        "cloned": cloned,
    })
    return write(directory, update, phase="prepared", backup=True)


def restore(database, directory, update):
    from .desktop_runtime import atomic_json, read_json
    update = write(directory, update, phase="rolling-back")
    metadata = read_json(directory / "update-backup.json")
    if metadata["id"] != update["id"]:
        raise RuntimeError("The rollback snapshot belongs to another update.")
    database_path = Path(database)
    backup = directory / "update-backup.sqlite3"
    backup_wal = directory / "update-backup.sqlite3-wal"
    for suffix in ("-wal", "-shm"):
        Path(str(database_path) + suffix).unlink(missing_ok=True)
    clone_or_copy(backup, database_path)
    if backup_wal.exists() and backup_wal.stat().st_size > 0:
        clone_or_copy(backup_wal, Path(str(database_path) + "-wal"))
        with closing(sqlite3.connect(str(database_path))) as restored:
            restored.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    parent = os.open(database_path.parent, os.O_RDONLY)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)
    if metadata["schema"] is None:
        (directory / "schema.json").unlink(missing_ok=True)
    else:
        atomic_json(directory / "schema.json", metadata["schema"])
    return write(directory, update, phase="rolled-back")


def recover(database, directory, selected):
    """Bootstrap recovery runs only after acquiring the database owner lock."""
    update = read(directory)
    if not update:
        return selected
    if update.get("superseded_by"):
        return Path(update["superseded_by"])
    if update["phase"] == "committed":
        return Path(update["target"])
    if update["phase"] in {"prepared", "trial", "rolling-back"}:
        restore(database, directory, update)
        return Path(update["previous"])
    if update["phase"] not in TERMINAL:
        write(directory, update, phase="rolled-back", recovery_uncertain=True, requires_review=True,
              error="The update was interrupted before candidate activation; review unfinished work.")
        return Path(update["previous"])
    return Path(update["previous"])
