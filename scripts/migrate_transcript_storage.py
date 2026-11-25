#!/usr/bin/env python3
"""Online and CLI transcript storage migration, legacy table removal, and compaction.

Streams saved conversations from ``browser_history`` + ``browser_history_chunks``
into sharded ``turns.compact.jsonl`` + ``turns.full.jsonl`` + ``thread_turns``,
verifies exact turn and item count parity for every conversation, backfills
``search.sqlite3``, drops legacy history and 24-table search objects, and runs
``VACUUM INTO '<db>.compacted'`` with an atomic swap to reclaim SSD space.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))

from agent_coord.store import default_database_path  # noqa: E402
from agent_coord.transcript_store import (  # noqa: E402
    migrate_database,
    migrate_if_needed,
    needs_migration,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        "--database",
        dest="db",
        type=Path,
        default=None,
        help="Path to state.sqlite3 (defaults to AGENT_COORD_DB or ~/.local/state/agent-coord/state.sqlite3).",
    )
    parser.add_argument(
        "--no-compact",
        action="store_true",
        help="Skip VACUUM INTO physical compaction.",
    )
    args = parser.parse_args(argv)
    database = (args.db or default_database_path()).expanduser().resolve()
    if not database.exists():
        parser.error(f"Database file does not exist: {database}")
    report = migrate_database(database, compact=not args.no_compact)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


__all__ = ["main", "migrate_database", "migrate_if_needed", "needs_migration"]


if __name__ == "__main__":
    raise SystemExit(main())
