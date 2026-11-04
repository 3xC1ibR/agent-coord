"""On-disk update transaction and rollback under exclusive runtime ownership."""
from __future__ import annotations

import os
from contextlib import closing
from pathlib import Path
import shutil
import sqlite3
import time

TERMINAL = {"committed", "rolled-back", "failed"}


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
    # Cancellation has its own durable record so a concurrent phase rename can
    # never erase Stop, even if it read update.json before cancellation arrived.
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
               (Path(database), Path(database + "-wal")) if path.exists())
    if shutil.disk_usage(directory).free < size * 2 + 1024 * 1024:
        raise RuntimeError("Insufficient free space for the update rollback snapshot.")


def snapshot(database, directory, update):
    from .desktop_runtime import atomic_json, read_json
    backup = directory / "update-backup.sqlite3"
    check_capacity(database, directory)
    temporary = backup.with_suffix(".temporary")
    temporary.unlink(missing_ok=True)
    with closing(sqlite3.connect(database)) as source, closing(sqlite3.connect(temporary)) as target:
        source.backup(target)
    with temporary.open("rb") as stream:
        os.fsync(stream.fileno())
    os.replace(temporary, backup)
    schema = directory / "schema.json"
    atomic_json(directory / "update-backup.json", {
        "id": update["id"], "schema": read_json(schema) if schema.exists() else None})
    return write(directory, update, phase="prepared", backup=True)


def restore(database, directory, update):
    from .desktop_runtime import atomic_json, read_json
    update = write(directory, update, phase="rolling-back")
    metadata = read_json(directory / "update-backup.json")
    if metadata["id"] != update["id"]:
        raise RuntimeError("The rollback snapshot belongs to another update.")
    # The owner lock is held and every trial process has exited. Repeating this
    # replacement after a crash is safe; the original backup remains immutable.
    temporary = Path(database + ".restore")
    shutil.copyfile(directory / "update-backup.sqlite3", temporary)
    with temporary.open("rb") as stream:
        os.fsync(stream.fileno())
    for suffix in ("-wal", "-shm"):
        Path(database + suffix).unlink(missing_ok=True)
    os.replace(temporary, database)
    parent = os.open(Path(database).parent, os.O_RDONLY)
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
        write(directory, update, phase="rolled-back", recovery_uncertain=True, requires_review=True, error="The update was interrupted before candidate activation; review unfinished work.")
        return Path(update["previous"])
    return Path(update["previous"])
