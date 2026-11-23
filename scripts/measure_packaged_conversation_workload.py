"""Exercise a packaged macOS client under streaming load with generated data."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile

from validate_signed_runtime_responsiveness import (
    SignedUpgrade,
    fetch,
    run_and_clean_up,
    seed_inventory,
    signed_resources,
)
from agent_coord import history_persistence
from agent_coord.store import CoordinationStore


class GeneratedConversationWorkload(SignedUpgrade):
    def execute(self):
        (self.root / "measure-browser-execution").touch()
        try:
            super().execute()
        finally:
            if self.address:
                url, token = self.address
                try:
                    self.report["backend_performance"] = fetch(url, token, "performance", timeout=5)
                except (OSError, ValueError) as error:
                    self.report["performance_read_error"] = str(error)


def generated_history(name, turns, commands, lines):
    history = {"id": name, "turns": []}
    for turn in range(turns):
        prefix = f"{name}-{turn}"
        items = [{
            "type": "userMessage", "id": prefix + "-user",
            "content": [{"type": "text", "text": f"Read the measurement for pass {turn}."}],
        }]
        for command in range(commands):
            output = "".join(
                f"Measurement {turn:03}/{command:03}/{line:03} bytes {(turn + 1) * (command + 1) + line:08} completed successfully.\n"
                for line in range(lines)
            )
            items.append({
                "type": "commandExecution", "id": f"{prefix}-command-{command}",
                "command": "measure", "status": "completed", "exitCode": 0,
                "aggregatedOutput": output,
            })
        items.append({
            "type": "agentMessage", "id": prefix + "-assistant",
            "text": f"Pass {turn} finished with {commands} measurements.",
        })
        history["turns"].append({"id": prefix, "status": "completed", "items": items})
    return history


def generate_snapshot(database):
    root = database.parent
    seed_inventory(database, root, [("codex", "now", 350), ("claude", "later", 350)])
    store = CoordinationStore(database)
    large = generated_history("generated-large", turns=64, commands=64, lines=48)
    encoded = json.dumps(large)
    with store._connection() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS browser_sessions (
            thread_id TEXT PRIMARY KEY, archived INTEGER NOT NULL, updated_at REAL NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS browser_history (
            thread_id TEXT PRIMARY KEY, history_json TEXT NOT NULL)""")
        history_persistence.ensure_schema(db)
        db.executemany("INSERT INTO browser_sessions(thread_id, archived, updated_at) VALUES (?, 0, 1)",
                       [("generated-large",), ("generated-medium",)])
        db.execute("INSERT INTO browser_history VALUES (?, ?)", (large["id"], encoded))
    # The reported slow archive contained 331 turns and 1,714 items. Keep this
    # generated counterpart large enough to exercise the same recent-window read.
    medium = generated_history("generated-medium", turns=331, commands=3, lines=72)
    prepared = history_persistence.prepare_history(medium)
    history_persistence.stage_history(store, prepared)
    with store._connection() as db:
        history_persistence.publish_history(db, medium["id"], prepared)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--playwright-module")
    args = parser.parse_args()
    args.report = args.report.resolve()
    bundle = signed_resources(args.app)
    with tempfile.TemporaryDirectory(prefix="rf-generated-conversations-") as directory:
        args.snapshot = Path(directory) / "generated.sqlite3"
        generate_snapshot(args.snapshot)
        args.profile_sample = args.report.with_suffix(".sample.txt")
        args.affected_report = None
        args.candidate_only = True
        runner = GeneratedConversationWorkload(args, bundle, bundle)
        runner.report.pop("signed_inputs")
        runner.report.update(workload_contract="generated-packaged-conversations-v1", package=bundle[1])
        return run_and_clean_up(runner)


if __name__ == "__main__":
    raise SystemExit(main())
