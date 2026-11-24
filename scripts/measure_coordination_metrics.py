#!/usr/bin/env python3
"""Compare the same foreground workload with coordination collection off/on.

Uses the existing large-history HTTP harness and a disposable native transcript
fixture. No installed conversations or provider processes are changed. The output
records both runs and the original harness budgets; budgets are never relaxed.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid


def fixture(root, mebibytes):
    root.mkdir(parents=True)
    now = datetime.now(timezone.utc).isoformat()
    target = root / "sessions" / "measurement.jsonl"
    target.parent.mkdir()
    thread_id = str(uuid.uuid4())
    returned = "Measured coordination history. " * 4000
    written = count = 0
    with target.open("w") as output:
        while written < mebibytes * 1024 * 1024:
            record = {"timestamp": now, "type": "event_msg", "payload": {
                "type": "item_completed", "thread_id": thread_id, "turn_id": f"turn-{count // 5}",
                "item": {"type": "McpToolCall", "server": "agent-coord", "tool": "read_activity_history",
                         "id": f"measurement-{count}", "status": "completed",
                         "arguments": {"thread_id": thread_id, "limit": 20},
                         "result": {"content": [{"type": "text", "text": returned}]}}}}
            line = json.dumps(record, separators=(",", ":")) + "\n"
            output.write(line)
            written += len(line.encode())
            count += 1
    with closing(sqlite3.connect(root / "state_5.sqlite")) as database, database:
        database.execute("CREATE TABLE threads(id TEXT PRIMARY KEY, rollout_path TEXT, updated_at INTEGER, cwd TEXT)")
        database.execute("INSERT INTO threads VALUES(?,?,?,?)", (thread_id, str(target), int(time.time()), str(root)))
    return {"bytes": written, "calls": count, "threads": 1}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--workload", choices=("foreground", "database"), default="foreground")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--fixture-mib", type=int, default=128)
    parser.add_argument("--sends", type=int, default=12)
    parser.add_argument("--reads", type=int, default=6)
    parser.add_argument("--warmup", type=float, default=15.0)
    parser.add_argument("--seconds", type=int, default=15, help="Duration of each database admission run")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.fixture_mib <= 512:
        parser.error("fixture-mib must be 1–512")
    if args.workload == "foreground" and args.snapshot is None:
        parser.error("--snapshot is required for the foreground workload")
    if not 1 <= args.seconds <= 300:
        parser.error("seconds must be 1–300")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    reports = {}
    with tempfile.TemporaryDirectory(prefix="coordination_metrics_measurement_") as directory:
        root = Path(directory)
        workload = fixture(root / "codex", args.fixture_mib)
        environment = os.environ.copy()
        environment["CODEX_HOME"] = str(root / "codex")
        for mode in ("disabled", "enabled"):
            result_path = args.output.with_name(args.output.stem + "-" + mode + ".json")
            log_path = result_path.with_suffix(".log")
            if args.workload == "database":
                report = measure_admission(root / mode, root / "codex", mode == "enabled", args.seconds)
                result_path.write_text(json.dumps(report, indent=2) + "\n")
                reports[mode] = report
                print(json.dumps({"mode": mode, "report": str(result_path)}), flush=True)
                continue
            command = [sys.executable, "-B", str(Path(__file__).with_name("measure_backend_latency.py")),
                       "--snapshot", str(args.snapshot.resolve()), "--agents", "6", "--windows", "3",
                       "--hook-rate", "3", "--delta-rate", "6", "--stored-history", "--warmup", str(args.warmup),
                       "--sends", str(args.sends), "--reads", str(args.reads), "--output", str(result_path.resolve())]
            if mode == "enabled":
                command.append("--coordination-metrics")
            with log_path.open("w") as log:
                result = subprocess.run(command, env=environment, stdout=log, stderr=subprocess.STDOUT)
            report = json.loads(result_path.read_text()) if result_path.exists() else {"measurement_error": "Harness did not publish a report"}
            reports[mode] = report
            print(json.dumps({"mode": mode, "exit_code": result.returncode, "report": str(result_path)}), flush=True)
    failures = []
    for mode, report in reports.items():
        if report.get("measurement_error") or report.get("error_count") or report.get("budget_failures") or report.get("load_start_failures"):
            failures.append(mode + " " + args.workload + " workload failed its existing acceptance criteria")
    observed = reports["enabled"].get("coordination_metrics", {}).get("observations", [])
    if not any(row.get("status", {}).get("running") for row in observed):
        failures.append("No active collector was observed during the measured workload")
    if not any((row.get("calls") or 0) > 0 for row in observed):
        failures.append("Collector progress was not observed during the measured workload")
    combined = {"schema_version": 1, "workload": args.workload, "fixture": workload, "runs": reports, "failures": failures,
                "note": "Compare matched foreground latency distributions and inspect coverage; no causal token savings are inferred."}
    args.output.write_text(json.dumps(combined, indent=2) + "\n")
    print(json.dumps({"failures": failures, "output": str(args.output)}), flush=True)
    return int(bool(failures) and args.check)


def measure_admission(root, codex_home, enabled, seconds):
    from measure_database_contention import measure
    from agent_coord.coordination_metrics import CoordinationMetrics
    from agent_coord.database_writer import _distribution

    service = CoordinationMetrics(root / "state.sqlite3", codex_home=codex_home)
    stopped = threading.Event()
    latencies, observations, errors = [], [], []
    def read_snapshots():
        while not stopped.is_set():
            started = time.perf_counter()
            try:
                snapshot = service.snapshot()
                json.dumps(snapshot)
                latencies.append((time.perf_counter() - started) * 1000)
                observations.append({"status": snapshot["status"], "calls": snapshot["current"]["calls"]})
            except Exception as error:
                errors.append(type(error).__name__)
            stopped.wait(.05)
    reader = threading.Thread(target=read_snapshots, name="metrics-measurement-reader")
    try:
        if enabled:
            service.refresh()
        reader.start()
        report = measure(seconds=seconds, writers=6)
        report["coordination_metrics"] = {"observations": observations, "snapshot_errors": errors,
            "snapshot_read_ms": _distribution(latencies), "snapshot_reads_per_second": 20,
            "note": "Cached snapshot reads include JSON serialization; these are not HTTP latency samples."}
        if errors:
            report["budget_failures"].append("Cached metric reads failed")
        return report
    finally:
        stopped.set()
        if reader.ident is not None:
            reader.join(3)
        service.close()


if __name__ == "__main__":
    raise SystemExit(main())
