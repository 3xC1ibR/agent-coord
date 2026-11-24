#!/usr/bin/env python3
"""Verify, activate, or compact transcripts on a tool-owned offline copy only."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from contextlib import closing

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))
from agent_coord.store import CoordinationStore
from agent_coord.transcript_store import TranscriptStore


def owned_copy(source, destination):
    receipt_path = Path(str(destination) + ".migration.json")
    if source == destination or (destination.exists() and source.samefile(destination)):
        raise ValueError("Source and destination must differ")
    if destination.exists():
        if not receipt_path.exists():
            raise ValueError("Refusing an existing destination without a migration ownership receipt")
        receipt = json.loads(receipt_path.read_text())
        if receipt != {"source": str(source), "destination": str(destination), "inode": destination.stat().st_ino}:
            raise ValueError("Destination does not match its migration ownership receipt")
        return
    if receipt_path.exists():
        raise ValueError("Migration receipt exists without its destination")
    if sys.platform == "darwin" and not Path(str(source) + "-wal").exists():
        subprocess.run(["cp", "-c", str(source), str(destination)], check=True)
    else:
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original:
            with closing(sqlite3.connect(destination)) as copy:
                original.backup(copy)
    os.chmod(destination, 0o600)
    with os.fdopen(os.open(receipt_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as output:
        json.dump({"source": str(source), "destination": str(destination), "inode": destination.stat().st_ino}, output)
        output.flush()
        os.fsync(output.fileno())


def compact(destination):
    """Produce a separate compact candidate; retain original for rollback.

    Only legacy transcript tables are removed here. Search/projection retention
    and their verification are owned by their respective migrations.
    """
    output = Path(str(destination) + ".compacted.sqlite3")
    temporary = Path(str(destination) + ".compaction.sqlite3")
    if output.exists() or temporary.exists():
        raise ValueError("Compaction output already exists; inspect it before retrying")
    with closing(sqlite3.connect(destination.as_uri() + "?mode=ro", uri=True)) as source:
        with closing(sqlite3.connect(temporary)) as candidate:
            source.backup(candidate)
            missing = candidate.execute("""SELECT count(*) FROM browser_history h LEFT JOIN transcript_generations g
                ON g.thread_id=h.thread_id WHERE g.active IS NOT 1 OR g.source_root IS NULL OR
                g.source_root != 'legacy:' || COALESCE((SELECT revision FROM transcript_legacy_revisions r WHERE r.thread_id=h.thread_id),0)""").fetchone()[0]
            if missing:
                raise ValueError("Compaction requires activated, unchanged, verified legacy roots")
            candidate.execute("UPDATE transcript_generations SET source_root=NULL")
            candidate.execute("DROP TABLE browser_history")
            candidate.execute("DROP TABLE browser_history_chunks")
            candidate.commit()
            candidate.execute("VACUUM INTO ?", (str(output),))
    os.chmod(output, 0o600)
    # Both offline candidates refer to the same immutable append spans. The
    # original database is never replaced; reversal selects it unchanged.
    Path(str(output) + ".transcripts").symlink_to(Path(str(destination) + ".transcripts"), target_is_directory=True)
    temporary.unlink()
    return str(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--activate", action="store_true", help="Activate verified roots on the offline copy only")
    parser.add_argument("--rollback", action="store_true", help="Select equivalent retained legacy roots")
    parser.add_argument("--compact", action="store_true", help="Write a separate compact candidate after verified activation")
    args = parser.parse_args()
    source, destination = args.source.resolve(), args.destination.resolve()
    owned_copy(source, destination)
    if args.compact:
        print(json.dumps({"compacted": compact(destination), "rollback_database": str(destination)}))
        return
    store = CoordinationStore(destination)
    transcripts = TranscriptStore(store, minimum_free_bytes=2 * 1024 ** 3)
    if args.rollback:
        transcripts.deactivate()
        print(json.dumps({"destination": str(destination), "activated": False}))
        return
    reports = transcripts.migrate(activate=args.activate, on_progress=lambda report: print(json.dumps(report), file=sys.stderr, flush=True))
    print(json.dumps({"destination": str(destination), "activated": args.activate, "threads": len(reports),
                      "turns": sum(r["turns"] for r in reports), "items": sum(r["items"] for r in reports)}))


if __name__ == "__main__":
    main()
