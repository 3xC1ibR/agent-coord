#!/usr/bin/env python3
"""Verify transcripts on an explicit offline copy; never modifies the source."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
from contextlib import closing

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))
from agent_coord.store import CoordinationStore
from agent_coord.transcript_store import TranscriptStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--activate", action="store_true", help="Activate verified roots on the offline copy only")
    args = parser.parse_args()
    source, destination = args.source.resolve(), args.destination.resolve()
    if source == destination or (destination.exists() and source.samefile(destination)):
        parser.error("Source and destination must differ")
    if not destination.exists():
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original:
            with closing(sqlite3.connect(destination)) as copy:
                original.backup(copy)
    store = CoordinationStore(destination)
    reports = TranscriptStore(store).migrate(activate=args.activate)
    print(json.dumps({"destination": str(destination), "activated": args.activate, "threads": len(reports),
                      "turns": sum(r["turns"] for r in reports), "items": sum(r["items"] for r in reports)}))


if __name__ == "__main__":
    main()
