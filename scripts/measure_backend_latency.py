#!/usr/bin/env python3
"""Measure Ribbon Field backend responsiveness on real data under agent load.

The backend under test is the repository's own HTTP server and BrowserSessions,
run in a child process against an APFS clone of a database snapshot. Provider
processes are replaced by in-process synthetic providers that stream like busy
agents (JSON-decoded events at a configurable rate, realistic transcript sizes),
so the backend performs the same persistence, publication and GIL-bound work as
the installed runtime without contacting a model or a live database.

    python3 scripts/measure_backend_latency.py --snapshot build/.../state.sqlite3 \
        [--agents 12] [--windows 3] [--hook-rate 4] [--sends 30] [--output report.json]
        [--hold SECONDS]   # keep the loaded server running for browser measurement

The parent process generates client load (windows that poll like the web UI and
hold event streams, hook processes that write like terminal agents) and records
latency distributions. Nothing here touches the installed runtime.
"""
from __future__ import annotations

import argparse
import copy
from http import HTTPStatus
import json
import math
import os
import platform
from pathlib import Path
import random
import shutil
import signal
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import weakref

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "plugins/agent-coord/scripts"


# Server-side budgets (milliseconds) for the realistic profile: six streaming
# agents, three event-driven windows and hook writers on a real database
# snapshot. The web UI shows a sent message locally on Enter; these bound how
# long its acceptance, delivery to other windows and the views behind it take.
BUDGETS = {
    "send_accepted": {"p50": 150, "p95": 400},
    "send_event_visible": {"p50": 150, "p95": 400},
    "list": {"p50": 300, "p95": 800},
    "background_list": {"p50": 300, "p95": 800},
    "read_target": {"p50": 300, "p95": 800},
    "thread_target": {"p50": 100, "p95": 300},
    "background_thread": {"p50": 100, "p95": 300},
    "sidecar_status": {"p50": 100, "p95": 300},
    "background_sidecar_status": {"p50": 100, "p95": 300},
    "search": {"p50": 300, "p95": 800},
}


def check_budgets(summary):
    failures = []
    for name, limits in BUDGETS.items():
        for statistic, limit in limits.items():
            value = summary.get(name, {}).get(statistic)
            if value is None or value > limit:
                failures.append(f"{name} {statistic} {value} ms exceeds {limit} ms")
    return failures


def check_database_health(metrics):
    """A fast response does not excuse failed or undrained persistence."""
    if not isinstance(metrics, dict) or not metrics:
        return ["Database performance metrics are unavailable"]
    persistence = metrics.get("persistence") or {}
    history = metrics.get("history") or {}
    if not isinstance(persistence, dict) or not isinstance(history, dict):
        return ["Database performance metrics are malformed"]
    failures = []
    for name, value in (("admission_timeouts", metrics.get("admission_timeouts")),
                        ("persistence.retries", persistence.get("retries")),
                        ("persistence.failure_count", persistence.get("failure_count")),
                        ("persistence.optional_failure_count", persistence.get("optional_failure_count", 0)),
                        ("persistence.pending", persistence.get("pending")),
                        ("history.failures", history.get("failures")),
                        ("history.search_seed_failures", history.get("search_seed_failures", 0))):
        if value != 0:
            failures.append(f"{name} must be zero, observed {value}")
    search = metrics.get("search")
    if search is not None:
        if not isinstance(search, dict):
            failures.append("Search metrics are malformed")
        else:
            for operation, values in search.items():
                if not isinstance(values, dict) or values.get("failures", 0) != 0:
                    failures.append(f"Search operation {operation} reported failures or malformed metrics")
    index = search.get("substring_index") if isinstance(search, dict) else None
    if not isinstance(index, dict):
        failures.append("Substring index coverage metrics are unavailable")
    elif (index.get("supported") is not True or index.get("state") != "complete"
          or index.get("remaining_sources") != 0 or index.get("failures") != 0
          or index.get("backfill_running") is not False
          or index.get("backfill_completed") is not True):
        failures.append("Substring index must complete without failures before acceptance")
    checkpoint = metrics.get("checkpoint")
    if checkpoint is not None:
        if not isinstance(checkpoint, dict):
            failures.append("Checkpoint metrics are malformed")
        elif checkpoint.get("errors") != 0:
            failures.append(f"checkpoint.errors must be zero, observed {checkpoint.get('errors')}")
        elif checkpoint.get("state") not in {"running", "unsupported_sqlite"}:
            failures.append(f"Checkpoint maintenance is not running: {checkpoint.get('state')}")
    return failures


def check_stream_workload(report):
    """Verify the advertised streaming load was actually present and sustained."""
    before, after = report.get("workload_before"), report.get("workload_after")
    if not isinstance(before, dict) or not isinstance(after, dict):
        return ["Synthetic stream workload observations are unavailable"]
    if not isinstance(before.get("threads"), dict) or not isinstance(after.get("threads"), dict):
        return ["Synthetic stream workload observations are malformed"]
    timestamps = before.get("observed_monotonic"), after.get("observed_monotonic")
    if any(not isinstance(value, (int, float)) or not math.isfinite(value) for value in timestamps):
        return ["Synthetic stream workload observation timestamps are malformed"]
    elapsed = timestamps[1] - timestamps[0]
    if elapsed <= 0:
        return ["Synthetic stream workload observation interval must be positive"]
    expected = report.get("expected_stream_threads", [])
    failures = []
    if len(expected) != report.get("requested_agents"):
        failures.append(f"Requested {report.get('requested_agents')} streaming agents but selected {len(expected)}")
    rates = report["stream_rates_hz"] = {}
    for thread in expected:
        for name, snapshot in (("before", before), ("after", after)):
            observed = snapshot["threads"].get(thread, {})
            if not isinstance(observed, dict):
                failures.append(f"Synthetic stream {thread} {name}: malformed worker statistics")
                continue
            if observed.get("active_streams") != 1:
                failures.append(f"Synthetic stream {thread} {name}: expected one worker, observed {observed.get('active_streams')}")
            if observed.get("stream_errors", 0):
                failures.append(f"Synthetic stream {thread} {name}: {observed['stream_errors']} worker errors")
        observations = [snapshot["threads"].get(thread, {}) for snapshot in (before, after)]
        if any(not isinstance(observed, dict) or not isinstance(observed.get("delta_events", 0), int)
               for observed in observations):
            failures.append(f"Synthetic stream {thread}: malformed event counts")
            continue
        initial, final = [observed.get("delta_events", 0) for observed in observations]
        rate = (final - initial) / elapsed
        rates[thread] = round(rate, 3)
        # Account for scheduling/encoding overhead without accepting a workload
        # that silently emits substantially fewer events than requested.
        if rate < report["delta_rate"] * .9:
            failures.append(f"Synthetic stream {thread}: {rate:.3f} events/s is below 90% of {report['delta_rate']}")
    return failures


def drain_workload(client, threads, report, timeout=10, quiet_period=1):
    """Stop only synthetic agents in the isolated clone, then bound the drain."""
    report["drain_completed"] = False
    deadline = time.monotonic() + timeout
    for thread in threads:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            report.setdefault("drain_errors", []).append("Synthetic agent stop deadline exceeded")
            break
        try:
            client.call(f"/api/browser/sessions/{thread}/interrupt", {}, timeout=remaining)
        except Exception as error:
            report.setdefault("drain_errors", []).append(str(error))
    quiet_since = None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            report.setdefault("drain_errors", []).append("Persistence drain deadline exceeded")
            return
        try:
            _, metrics, _ = client.call("/api/browser/performance", timeout=remaining)
            report["database_metrics"] = metrics
        except Exception as error:
            report["database_metrics_unavailable"] = str(error)
            report.setdefault("drain_errors", []).append("Persistence drain metrics failed: " + str(error))
            return
        now = time.monotonic()
        if now >= deadline:
            report.setdefault("drain_errors", []).append("Persistence drain deadline exceeded")
            return
        if not isinstance(metrics, dict) or not isinstance(metrics.get("persistence"), dict):
            report.setdefault("drain_errors", []).append("Persistence drain metrics are malformed")
            return
        if metrics["persistence"].get("pending") == 0:
            if quiet_since is None:
                quiet_since = now
            if now - quiet_since >= quiet_period:
                report["drain_completed"] = True
                return
        else:
            quiet_since = None
        time.sleep(min(.1, deadline - now))


def stop_process(process, label, report, timeout):
    """Reap an owned measurement child even when orderly termination fails."""
    if process is None:
        return True
    try:
        if process.poll() is None:
            process.terminate()
    except ProcessLookupError:
        pass
    except Exception as error:
        report.setdefault("cleanup_errors", []).append(f"{label} termination failed: {error}")
    try:
        process.wait(timeout=timeout)
        return True
    except Exception as error:
        report.setdefault("cleanup_errors", []).append(f"{label} did not exit cleanly: {error}")
    try:
        process.kill()
    except ProcessLookupError:
        pass
    except Exception as error:
        report.setdefault("cleanup_errors", []).append(f"{label} kill failed: {error}")
    try:
        process.wait(timeout=5)
        return True
    except Exception as error:
        report.setdefault("cleanup_errors", []).append(f"{label} could not be reaped: {error}")
        return False


def cleanup_workload(server, hooks, client, threads, report, quiet_period):
    """Keep diagnostics failures from abandoning the isolated backend."""
    hooks_reaped = not hooks
    report["children_reaped"] = False
    try:
        results = []
        for index, hook in enumerate(hooks):
            results.append(stop_process(hook, f"Hook writer {index + 1}", report, timeout=5))
        hooks_reaped = all(results)
        if client is None:
            report["database_metrics_unavailable"] = "Measurement client initialization did not complete"
            return
        try:
            _, report["workload_after"], _ = client.call("/api/measurement/workload", timeout=5)
        except Exception as error:
            report.setdefault("drain_errors", []).append("Synthetic stream observations failed: " + str(error))
        try:
            _, report["database_metrics_under_load"], _ = client.call("/api/browser/performance", timeout=5)
        except Exception as error:
            report["database_metrics_unavailable"] = str(error)
        try:
            drain_workload(client, threads, report, quiet_period=quiet_period)
        except Exception as error:
            report["drain_completed"] = False
            report.setdefault("drain_errors", []).append("Persistence drain failed: " + str(error))
    finally:
        backend_reaped = stop_process(server, "Measurement backend", report, timeout=20)
        report["children_reaped"] = hooks_reaped and backend_reaped


def remove_workload_database(report, root, output):
    """Keep small diagnostics, never retain an inactive database on test failure."""
    root = Path(root).resolve()
    destination = Path(output).resolve() if output else root / "report.json"
    report["report_path"] = str(destination)
    report["database_retained"] = True
    if not report.get("children_reaped"):
        report.setdefault("cleanup_errors", []).append("Database retained because owned child termination was not confirmed")
        return destination
    preserved = {root / "server.log", destination}
    try:
        # This invocation owns the entire temporary directory. The live
        # database and the source snapshot are always outside it.
        if root.exists():
            for entry in root.iterdir():
                if entry in preserved or entry in destination.parents:
                    continue
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
        report["database_retained"] = False
    except OSError as error:
        report.setdefault("cleanup_errors", []).append("Workload database cleanup failed: " + str(error))
    log = root / "server.log"
    if log.exists():
        report["server_log"] = str(log)
        if root not in destination.parents:
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                saved_log = destination.with_suffix(".server.log")
                shutil.copy2(log, saved_log)
                log.unlink()
                report["server_log"] = str(saved_log)
            except OSError as error:
                report.setdefault("cleanup_errors", []).append("Measurement log preservation failed: " + str(error))
    try:
        if root not in destination.parents and root.exists() and not any(root.iterdir()):
            root.rmdir()
    except OSError as error:
        report.setdefault("cleanup_errors", []).append("Measurement directory cleanup failed: " + str(error))
    return destination


def finish_report(report, root, output):
    """Persist failed measurements, including incomplete diagnostics and cleanup."""
    destination = remove_workload_database(report, root, output)
    report["summary"] = {name: quantiles(values) for name, values in report["samples"].items() if name != "errors"}
    report["error_count"] = len(report["samples"].get("errors", []))
    report["budget_failures"] = (check_budgets(report["summary"])
                                 + check_database_health(report.get("database_metrics"))
                                 + check_stream_workload(report)
                                 + report.get("drain_errors", []) + report.get("cleanup_errors", []))
    if "database_metrics_unavailable" in report:
        report["budget_failures"].append("Database performance metrics unavailable: " + report["database_metrics_unavailable"])
    if not report.get("drain_completed"):
        report["budget_failures"].append("Persistence drain did not complete")
    report["artifacts_retained"] = True
    if not output:
        report.pop("samples")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, indent=1))
    print(json.dumps({k: v for k, v in report.items() if k != "samples"}, indent=1))


def quantiles(values):
    ordered = sorted(values)
    if not ordered:
        return {"n": 0}
    pick = lambda q: round(ordered[min(len(ordered) - 1, int(q * (len(ordered) - 1) + 0.5))], 1)
    return {"n": len(ordered), "p50": pick(.5), "p90": pick(.9), "p95": pick(.95), "max": round(ordered[-1], 1),
            "mean": round(statistics.fmean(ordered), 1)}


def interrupt_measurement(signum, frame):
    """Route ordinary process termination through owned-resource cleanup."""
    raise KeyboardInterrupt(f"Measurement terminated by signal {signum}")


# --------------------------------------------------------------------------
# Child process: the backend under test with synthetic providers.
# --------------------------------------------------------------------------

class SyntheticProvider:
    """Wire-compatible provider double. Events pass through JSON like a pipe."""

    history_kb = 1500
    stored_history = False
    delta_rate = 8.0
    stream_seconds = 600.0
    short_turn_seconds = 1.5
    instances = weakref.WeakSet()
    instances_lock = threading.Lock()

    def __init__(self, store, callback):
        self.store = store
        self.callback = callback
        self.threads = {}
        self.lock = threading.Lock()
        self.stream_lock = threading.Lock()
        self.stream_workers = {}
        self.stream_statistics = {}
        self.closed = False
        with self.instances_lock:
            self.instances.add(self)

    @classmethod
    def workload_snapshot(cls):
        with cls.instances_lock:
            instances = list(cls.instances)
        threads = {}
        for instance in instances:
            with instance.stream_lock:
                for thread, statistics in instance.stream_statistics.items():
                    totals = threads.setdefault(thread, dict(active_streams=0, streams_started=0,
                        streams_completed=0, delta_events=0, stream_errors=0))
                    for name, value in statistics.items():
                        totals[name] += value
        return {"observed_monotonic": time.monotonic(), "threads": threads}

    def _start_stream(self, thread_id, turn, duration):
        key = thread_id, turn["id"]
        with self.stream_lock:
            if self.closed or key in self.stream_workers:
                return
            worker = threading.Thread(target=self._stream, args=(thread_id, turn, duration), daemon=True)
            self.stream_workers[key] = worker
            statistics = self.stream_statistics.setdefault(thread_id, dict(active_streams=0, streams_started=0,
                streams_completed=0, delta_events=0, stream_errors=0))
            statistics["active_streams"] += 1
            statistics["streams_started"] += 1
        worker.start()

    def _stream(self, thread_id, turn, duration):
        try:
            self._stream_events(thread_id, turn, duration)
        except Exception:
            with self.stream_lock:
                self.stream_statistics[thread_id]["stream_errors"] += 1
            raise
        finally:
            with self.stream_lock:
                self.stream_workers.pop((thread_id, turn["id"]), None)
                self.stream_statistics[thread_id]["active_streams"] -= 1
                self.stream_statistics[thread_id]["streams_completed"] += 1

    def _emit(self, message):
        if not self.closed:
            self.callback(json.loads(json.dumps(message)))

    def _thread(self, thread_id, cwd="/"):
        with self.lock:
            if thread_id not in self.threads:
                self.threads[thread_id] = {"id": thread_id, "cwd": cwd, "turns": self._history(thread_id)}
            return self.threads[thread_id]

    def _history(self, thread_id):
        if self.stored_history:
            # The snapshot's saved transcript, as the provider would return it.
            try:
                from agent_coord.history_persistence import read_history
            except ImportError:  # Matched measurements of retained older backends.
                with self.store.reading(), self.store._connection() as connection:
                    row = connection.execute("SELECT history_json FROM browser_history WHERE thread_id = ?", (thread_id,)).fetchone()
                history = json.loads(row[0]) if row else None
            else:
                history = read_history(self.store, thread_id)
            return (history or {}).get("turns", [])
        rng = random.Random(thread_id)
        turns, size = [], 0
        while size < self.history_kb * 1024:
            turn_id = uuid.UUID(int=rng.getrandbits(128)).hex
            items = [{"id": turn_id + "-u", "type": "userMessage",
                      "content": [{"type": "text", "text": "Request " + "x" * 300}]}]
            for index in range(3):
                items.append({"id": f"{turn_id}-c{index}", "type": "commandExecution", "command": "rg pattern",
                              "status": "completed", "exitCode": 0, "aggregatedOutput": "line\n" * 700})
                items.append({"id": f"{turn_id}-a{index}", "type": "agentMessage", "text": "Finding. " * 250})
            turns.append({"id": turn_id, "status": "completed", "items": items})
            size += len(json.dumps(items))
        return turns

    def request(self, method, params=None):
        params = params or {}
        thread_id = params.get("threadId")
        if method == "model/list":
            return {"data": [{"model": "synthetic", "isDefault": True, "supportedReasoningEfforts": []}]}
        if method in {"config/read"}:
            return {"config": {"model": "synthetic", "model_reasoning_effort": None}}
        if method == "skills/list":
            return {"data": []}
        if method == "thread/start":
            thread_id = str(uuid.uuid4())
            with self.lock:
                self.threads[thread_id] = {"id": thread_id, "cwd": params.get("cwd", "/"), "turns": []}
        if method in {"thread/start", "thread/resume", "thread/read", "thread/fork"}:
            thread = copy.deepcopy(self._thread(thread_id, params.get("cwd", "/")))
            if params.get("excludeTurns") or (method == "thread/read" and not params.get("includeTurns")):
                thread["turns"] = []
            # A real provider response crosses a pipe and is decoded on the reader thread.
            return json.loads(json.dumps({"thread": thread, "model": "synthetic", "reasoningEffort": None}))
        if method == "turn/start":
            thread = self._thread(thread_id)
            turn = {"id": uuid.uuid4().hex, "status": "inProgress",
                    "items": [{"id": uuid.uuid4().hex, "type": "userMessage", "content": params["input"]}]}
            with self.lock:
                thread["turns"].append(turn)
            text = " ".join(part.get("text", "") for part in params["input"])
            duration = self.stream_seconds if "[synthetic-load]" in text else self.short_turn_seconds
            self._emit({"method": "turn/started", "params": {"threadId": thread_id, "turn": turn}})
            self._start_stream(thread_id, turn, duration)
            return {"turn": copy.deepcopy(turn)}
        if method == "turn/steer":
            thread = self._thread(thread_id)
            turn = thread["turns"][-1] if thread["turns"] else None
            if not turn or turn["status"] != "inProgress" or turn["id"] != params.get("expectedTurnId"):
                from agent_coord.store import CoordinationError
                raise CoordinationError("Active turn mismatch.")
            item = {"id": uuid.uuid4().hex, "type": "userMessage", "content": params["input"]}
            turn["items"].append(item)
            self._emit({"method": "item/completed", "params": {"threadId": thread_id, "turnId": turn["id"], "item": item}})
            text = " ".join(part.get("text", "") for part in params["input"])
            duration = self.stream_seconds if "[synthetic-load]" in text else self.short_turn_seconds
            # A restored in-progress turn has no synthetic worker until its
            # first steer. Subsequent steers must reuse that worker.
            self._start_stream(thread_id, turn, duration)
            return {"turnId": turn["id"]}
        if method == "turn/interrupt":
            thread = self._thread(thread_id)
            if thread["turns"]:
                thread["turns"][-1]["status"] = "interrupted"
            return {}
        return {}

    def _stream_events(self, thread_id, turn, duration):
        started = time.monotonic()
        ends = started + duration
        message = {"id": uuid.uuid4().hex, "type": "agentMessage", "text": ""}
        turn["items"].append(message)
        self._emit({"method": "item/started", "params": {"threadId": thread_id, "turnId": turn["id"], "item": message}})
        sequence = 0
        while time.monotonic() < ends and not self.closed and turn["status"] == "inProgress":
            sequence += 1
            delta = "token " * 6
            message["text"] += delta
            self._emit({"method": "item/agentMessage/delta", "params": {
                "threadId": thread_id, "turnId": turn["id"], "itemId": message["id"], "delta": delta}})
            with self.stream_lock:
                self.stream_statistics[thread_id]["delta_events"] += 1
            if sequence % 12 == 0:
                # Live providers also report MCP server and thread status changes
                # (about 2/s across a dozen agents); the web UI refreshes its list on them.
                self._emit({"method": "mcpServer/startupStatus/updated", "params": {
                    "threadId": thread_id, "name": "agent-coord", "status": "ready"}})
            if sequence % 40 == 0:
                tool = {"id": uuid.uuid4().hex, "type": "commandExecution", "command": "pytest -q",
                        "status": "inProgress"}
                self._emit({"method": "item/started", "params": {"threadId": thread_id, "turnId": turn["id"], "item": tool}})
                tool.update(status="completed", exitCode=0, aggregatedOutput="ok\n" * 200)
                turn["items"].append(tool)
                self._emit({"method": "item/completed", "params": {"threadId": thread_id, "turnId": turn["id"], "item": tool}})
                self._emit({"method": "thread/tokenUsage/updated", "params": {"threadId": thread_id, "turnId": turn["id"],
                            "tokenUsage": {"total": {"inputTokens": 1000 * sequence, "outputTokens": 10 * sequence,
                                                     "totalTokens": 1010 * sequence}, "last": {"totalTokens": 900},
                                           "modelContextWindow": 400000}}})
            # Maintain the offered rate while the backend processes events.
            # Sleeping a full interval after each callback would silently
            # reduce the workload as callbacks become slower.
            time.sleep(max(0, started + sequence / self.delta_rate - time.monotonic()))
        if turn["status"] == "inProgress":
            turn["status"] = "completed"
        self._emit({"method": "item/completed", "params": {"threadId": thread_id, "turnId": turn["id"], "item": message}})
        self._emit({"method": "turn/completed", "params": {"threadId": thread_id, "turn": copy.deepcopy(turn)}})

    def write(self, message):
        pass

    def close(self):
        self.closed = True


class NormalizedSyntheticProvider(SyntheticProvider):
    """Model adapters whose saved transcript is owned by BrowserSessions."""

    def _history(self, thread_id):
        return []  # The adapter retains current execution, not a transcript copy.

    def reconcile_history(self, thread_id, history):
        with self.lock:
            turns = self.threads.get(thread_id, {}).get("turns", [])
            latest = copy.deepcopy(turns[-1]) if turns else None
        active = latest and latest["status"] == "inProgress"
        saved = next((turn for turn in history.get("turns", []) if latest and turn["id"] == latest["id"]), None)
        if latest and (active or saved is None or saved.get("status") == "inProgress"):
            history["turns"] = [turn for turn in history.get("turns", []) if turn["id"] != latest["id"]] + [latest]
        if not active:
            for turn in history.get("turns", []):
                if turn.get("status") == "inProgress":
                    turn["status"] = "interrupted"
        history["status"] = {"type": "active" if active else "idle"}
        return history

    def request(self, method, params=None):
        params = params or {}
        if method not in {"thread/resume", "thread/read"}:
            return super().request(method, params)
        thread_id = params["threadId"]
        metadata_only = params.get("excludeTurns") if method == "thread/resume" else not params.get("includeTurns", True)
        if metadata_only:
            with self.lock:
                turns = self.threads.get(thread_id, {}).get("turns", [])
                active = bool(turns and turns[-1]["status"] == "inProgress")
            thread = {"id": thread_id, "turns": [], "status": {"type": "active" if active else "idle"}}
        else:
            from agent_coord.history_persistence import read_history
            thread = self.reconcile_history(thread_id, read_history(self.store, thread_id) or {"id": thread_id, "turns": []})
        return {"thread": thread, "model": "synthetic", "reasoningEffort": None}


class StackSampler(threading.Thread):
    """Python-level sampling profile of every thread (diagnostic only)."""

    def __init__(self, path, interval=0.02):
        super().__init__(daemon=True)
        self.path, self.interval, self.counts, self.samples = path, interval, {}, 0

    def run(self):
        own = threading.get_ident()
        while True:
            time.sleep(self.interval)
            self.samples += 1
            for ident, frame in sys._current_frames().items():
                if ident == own:
                    continue
                # Read code metadata directly. traceback.extract_stack also
                # resolves source lines, which dominated the old 200 Hz probe.
                stack = []
                for _ in range(7):
                    if frame is None:
                        break
                    code = frame.f_code
                    stack.append(f"{os.path.basename(code.co_filename)}:{code.co_name}:{frame.f_lineno}")
                    frame = frame.f_back
                key = " <- ".join(stack)
                self.counts[key] = self.counts.get(key, 0) + 1

    def write(self):
        ranked = sorted(self.counts.items(), key=lambda item: -item[1])[:60]
        Path(self.path).write_text(f"samples {self.samples}\n" + "\n".join(f"{count:7d} {key}" for key, count in ranked))


def measurement_handler(handler):
    """Extend the production HTTP handler only inside the isolated workload."""
    class MeasurementHandler(handler):
        def do_GET(self):
            if urllib.parse.urlparse(self.path).path == "/api/measurement/workload" and self._authorized():
                self._json(HTTPStatus.OK, SyntheticProvider.workload_snapshot())
            else:
                super().do_GET()
    return MeasurementHandler


def serve(arguments):
    # --source selects another backend tree, e.g. a retained runtime payload,
    # so the same workload measures the code a runtime is actually running.
    sys.path.insert(0, arguments.source or str(SCRIPTS))
    for key in ("AGENT_COORD_SESSION_ID", "CODEX_THREAD_ID", "CLAUDECODE"):
        os.environ.pop(key, None)
    from agent_coord.store import CoordinationStore
    from agent_coord.codex_app_server import BrowserSessions
    from agent_coord import ui
    from agent_coord import desktop_runtime
    SyntheticProvider.history_kb = arguments.history_kb
    SyntheticProvider.stored_history = arguments.stored_history
    SyntheticProvider.delta_rate = arguments.delta_rate
    store = CoordinationStore(Path(arguments.db))
    # Match the production runtime's ownership context. This database is an
    # isolated clone; external pressure below deliberately uses raw SQLite.
    owner = desktop_runtime.lock_file(desktop_runtime.runtime_directory(store.database_path) / "owner.lock")
    desktop_runtime._OWNED_DATABASES.add(str(store.database_path.resolve()))
    workspace = arguments.cwd or None
    normalized = NormalizedSyntheticProvider if arguments.stored_history else SyntheticProvider
    sessions = BrowserSessions(store, workspace, rpc_factory=SyntheticProvider, claude_factory=normalized,
                               antigravity_factory=normalized, kimi_factory=normalized,
                               muse_factory=normalized)
    server = ui.make_ui_server(store, port=0, cwd=workspace, browser_sessions=sessions)
    server.RequestHandlerClass = measurement_handler(server.RequestHandlerClass)
    if arguments.thread_switch_interval is not None:
        sys.setswitchinterval(arguments.thread_switch_interval)
    sampler = StackSampler(arguments.profile) if arguments.profile else None
    if sampler:
        sampler.start()
    print(json.dumps({"port": server.server_address[1], "pid": os.getpid(),
                      "thread_switch_interval": sys.getswitchinterval(),
                      "source_fingerprint": desktop_runtime.schema_fingerprint()}), flush=True)
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    # A measurement whose driver was killed must not leave this backend
    # consuming CPU on a shared machine.
    parent = os.getppid()
    def stop_when_orphaned():
        while os.getppid() == parent:
            time.sleep(1)
        server.shutdown()
    threading.Thread(target=stop_when_orphaned, daemon=True).start()
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        if sampler:
            sampler.write()
        server.server_close()
        desktop_runtime._OWNED_DATABASES.discard(str(store.database_path.resolve()))
        os.close(owner)


# --------------------------------------------------------------------------
# Parent process: client load and measurement.
# --------------------------------------------------------------------------

class Client:
    def __init__(self, port):
        self.base = f"http://127.0.0.1:{port}"
        config = json.loads(urllib.request.urlopen(self.base + "/api/browser/config", timeout=60).read())
        # Streams start at the current completion cursor, as the web UI does.
        self.token, self.completion_cursor = config["token"], config["completionCursor"]

    def call(self, path, body=None, timeout=120):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.base + path, data=data, method="GET" if body is None else "POST",
                                         headers={"X-Agent-Coord-Token": self.token, "Content-Type": "application/json",
                                                  "Origin": self.base})
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read()
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"{error.code} {path}: {error.read()[:300]!r}") from None
        return (time.perf_counter() - started) * 1000, json.loads(payload) if payload else None, len(payload)


class EventWatcher(threading.Thread):
    """Hold the event stream like a window and report when a marker appears."""

    def __init__(self, client):
        super().__init__(daemon=True)
        self.client, self.stopped = client, threading.Event()
        self.condition, self.seen = threading.Condition(), {}

    def run(self):
        while not self.stopped.is_set():
            try:
                request = urllib.request.Request(self.client.base + f"/api/browser/events?completion_after={self.client.completion_cursor}",
                                                 headers={"X-Agent-Coord-Token": self.client.token})
                with urllib.request.urlopen(request, timeout=30) as stream:
                    for raw in stream:
                        if self.stopped.is_set():
                            return
                        if not raw.startswith(b"data: "):
                            continue
                        text = raw.decode("utf-8", "replace")
                        if "probe-" not in text:
                            continue
                        now = time.perf_counter()
                        with self.condition:
                            for marker in list(self.waiting):
                                if marker in text:
                                    self.seen[marker] = now
                            self.condition.notify_all()
            except Exception:
                time.sleep(.2)

    waiting = set()

    def wait_for(self, marker, timeout=60):
        with self.condition:
            self.condition.wait_for(lambda: marker in self.seen, timeout=timeout)
            return self.seen.pop(marker, None)


CONVERSATION_ONLY = {"thread/usage/updated", "thread/tokenUsage/updated", "turn/diff/updated", "turn/plan/updated",
                     "account/rateLimits/updated", "deprecationNotice"}


def refreshes_list(event, ignored=CONVERSATION_ONLY):
    """The web UI's rule (app.js changesThreadList) for scheduling a list refresh."""
    method = event.get("method") or ""
    return bool(event.get("requestKey")) or not (method in ignored or method.startswith(("item/", "hook/"))
                                                  or method.endswith(("/delta", "/outputDelta")))


class Window(threading.Thread):
    """Reproduce one visible web window: event stream, event-driven list refreshes
    (debounced 150 ms, one in flight, at most one per min_interval) and the 5 s
    metadata poll."""

    ignored = CONVERSATION_ONLY
    min_interval = 0.0

    def __init__(self, client, selected, stop, samples):
        super().__init__(daemon=True)
        self.client, self.selected, self.stop, self.samples = client, selected, stop, samples
        self.wanted = threading.Event()

    def run(self):
        threading.Thread(target=self._stream, daemon=True).start()
        threading.Thread(target=self._poll, daemon=True).start()
        threading.Thread(target=self._poll_sidecars, daemon=True).start()
        last = 0.0
        while not self.stop.is_set():
            if not self.wanted.wait(.5):
                continue
            self.stop.wait(max(.15, self.min_interval - (time.monotonic() - last)))
            self.wanted.clear()
            last = time.monotonic()
            self._fetch(("list", "/api/browser/threads"), ("organization", "/api/browser/organization"),
                        ("views", "/api/browser/views"), ("asks", "/api/browser/asks"),
                        ("schedules", "/api/browser/schedules"))

    def _poll(self):
        self.stop.wait(random.random() * 5)
        while not self.stop.is_set():
            self.wanted.set()
            self._fetch(("thread", "/api/browser/threads/" + self.selected))
            self.stop.wait(5)

    def _poll_sidecars(self):
        self.stop.wait(random.random() * 5)
        while not self.stop.is_set():
            # sidecars.js uses an independent setInterval: a slow response
            # does not postpone the next request or the metadata poll.
            threading.Thread(target=self._fetch, args=(("sidecar_status",
                "/api/browser/threads/" + self.selected + "/sidecars"),), daemon=True).start()
            self.stop.wait(5)

    def _fetch(self, *requests):
        for name, path in requests:
            try:
                elapsed, _, _ = self.client.call(path)
                self.samples.setdefault("background_" + name, []).append(elapsed)
            except Exception as error:
                if not self.stop.is_set():  # Requests cut off by teardown are not failures.
                    self.samples.setdefault("errors", []).append(f"{name}: {error}")

    def _stream(self):
        while not self.stop.is_set():
            try:
                request = urllib.request.Request(self.client.base + f"/api/browser/events?completion_after={self.client.completion_cursor}",
                                                 headers={"X-Agent-Coord-Token": self.client.token})
                with urllib.request.urlopen(request, timeout=30) as stream:
                    for raw in stream:
                        if self.stop.is_set():
                            return
                        if raw.startswith(b"data: "):
                            batch = json.loads(raw[6:])
                            if (batch.get("completions") or batch.get("approvals") or batch.get("snoozes")
                                    or any(refreshes_list(event, self.ignored) for event in batch.get("events", []))):
                                self.wanted.set()
            except Exception:
                self.stop.wait(.5)


HOOK_SOURCE = r"""
import sqlite3, sys, time, json, random
database, rate, sessions = sys.argv[1], float(sys.argv[2]), json.loads(sys.argv[3])
import os
parent = os.getppid()
while os.getppid() == parent:
    started = time.time()
    connection = sqlite3.connect(database, timeout=5)
    connection.execute("PRAGMA busy_timeout = 5000")
    session = random.choice(sessions)
    with connection:
        connection.execute("UPDATE sessions SET last_seen_at = ?, last_tool_json = ? WHERE session_id = ?",
                           (time.time(), json.dumps({"at": time.time(), "kind": "run", "label": "pytest -q", "name": "Bash"}), session))
    connection.close()
    time.sleep(max(0, 1 / rate - (time.time() - started)))
"""


def pick_threads(database, count):
    connection = sqlite3.connect(database)
    rows = connection.execute("""SELECT b.thread_id FROM browser_sessions b JOIN work_threads t ON t.thread_id = b.thread_id
        WHERE b.archived = 0 AND t.attention != 'archived' AND b.client IN ('codex', 'claude')
        ORDER BY b.updated_at DESC LIMIT ?""", (count,)).fetchall()
    connection.close()
    return [row[0] for row in rows]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", nargs="?", default="measure", choices=["measure", "serve"])
    parser.add_argument("--snapshot", help="Database snapshot to clone (never modified).")
    parser.add_argument("--db", help=argparse.SUPPRESS)
    parser.add_argument("--cwd", default="", help="Workspace boundary; empty serves every workspace like the app.")
    parser.add_argument("--agents", type=int, default=12)
    parser.add_argument("--windows", type=int, default=3)
    parser.add_argument("--hook-rate", type=float, default=4.0)
    parser.add_argument("--delta-rate", type=float, default=8.0)
    parser.add_argument("--history-kb", type=int, default=1500)
    parser.add_argument("--stored-history", action="store_true",
                        help="Serve each conversation's saved snapshot history instead of a synthetic one.")
    parser.add_argument("--sends", type=int, default=30)
    parser.add_argument("--reads", type=int, default=15)
    parser.add_argument("--warmup", type=float, default=15.0)
    parser.add_argument("--hold", type=float, default=0.0)
    parser.add_argument("--output")
    parser.add_argument("--check", action="store_true", help="Exit 1 when a latency budget is exceeded.")
    parser.add_argument("--client-list-interval", type=float, default=0.0,
                        help="Minimum seconds between a window's event-driven list refreshes (web UI throttle).")
    parser.add_argument("--client-ignores", default="",
                        help="Comma-separated extra event methods the windows do not refresh the list for.")
    parser.add_argument("--source", help="Directory containing the agent_coord package to measure (default: this repository).")
    parser.add_argument("--profile", help="Write a Python stack sample profile of the backend to this file.")
    parser.add_argument("--thread-switch-interval", type=float,
                        help="Diagnostic override for the backend Python thread switch interval, in seconds.")
    arguments = parser.parse_args()
    if not 0 < arguments.delta_rate < float("inf"):
        parser.error("--delta-rate must be finite and greater than zero")
    if arguments.mode == "serve":
        return serve(arguments)
    if not arguments.snapshot:
        parser.error("--snapshot is required")
    root = Path(tempfile.mkdtemp(prefix="ribbon-latency-"))
    database = root / "state.sqlite3"
    server = server_log = client = None
    hooks, busy = [], []
    report = {"snapshot": arguments.snapshot, "source": arguments.source or str(SCRIPTS), "root": str(root), "target": None, "agents": 0,
              "requested_agents": arguments.agents, "expected_stream_threads": [],
              "python": platform.python_version(), "sqlite": sqlite3.sqlite_version, "platform": platform.platform(),
              "windows": arguments.windows, "hook_rate": arguments.hook_rate, "delta_rate": arguments.delta_rate,
              "history_kb": arguments.history_kb, "stored_history": arguments.stored_history,
              "profile_enabled": bool(arguments.profile), "samples": {}}
    stop = threading.Event()
    previous_termination = signal.signal(signal.SIGTERM, interrupt_measurement)
    try:
        subprocess.run(["cp", "-c", arguments.snapshot, str(database)], check=True)
        threads = pick_threads(database, arguments.agents + 1)
        if not threads:
            raise RuntimeError("The snapshot contains no eligible conversations")
        target, busy = threads[0], threads[1:]
        report.update(target=target, agents=len(busy), expected_stream_threads=busy)
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith(("AGENT_COORD_", "RIBBON_")) and key not in {"CODEX_THREAD_ID", "CLAUDECODE"}}
        server_log = open(root / "server.log", "w")
        server = subprocess.Popen([sys.executable, "-B", __file__, "serve", "--db", str(database), "--cwd", arguments.cwd,
                                   "--history-kb", str(arguments.history_kb), "--delta-rate", str(arguments.delta_rate)]
                                  + (["--stored-history"] if arguments.stored_history else [])
                                  + (["--profile", str(Path(arguments.profile).resolve())] if arguments.profile else [])
                                  + (["--thread-switch-interval", str(arguments.thread_switch_interval)]
                                     if arguments.thread_switch_interval is not None else [])
                                  + (["--source", str(Path(arguments.source).resolve())] if arguments.source else []),
                                  stdout=subprocess.PIPE, stderr=server_log, env=environment, text=True)
        started = server.stdout.readline()
        if not started:
            raise RuntimeError("backend failed to start; see " + str(root / "server.log"))
        identity = json.loads(started)
        port = identity["port"]
        report["source_fingerprint"] = identity.get("source_fingerprint")
        report["thread_switch_interval"] = identity.get("thread_switch_interval")
        report["port"] = port
        client = Client(port)
        samples = report["samples"]
        report["load_start_failures"] = []
        for thread in busy:
            try:
                client.call(f"/api/browser/sessions/{thread}/messages", {"message": "[synthetic-load] keep working"})
            except RuntimeError as error:
                report["load_start_failures"].append(str(error))
        Window.min_interval = arguments.client_list_interval
        Window.ignored = CONVERSATION_ONLY | set(filter(None, arguments.client_ignores.split(",")))
        report["client"] = {"list_interval": Window.min_interval, "ignored": sorted(Window.ignored - CONVERSATION_ONLY)}
        for _ in range(arguments.windows):
            Window(client, target, stop, samples).start()
        if arguments.hook_rate:
            hooks.append(subprocess.Popen([sys.executable, "-c", HOOK_SOURCE, str(database), str(arguments.hook_rate),
                                           json.dumps(busy or [target])]))
        watcher = EventWatcher(client)
        watcher.start()
        print(json.dumps({"status": "loaded", "port": port, "target": target}), flush=True)
        time.sleep(arguments.warmup)
        _, report["workload_before"], _ = client.call("/api/measurement/workload", timeout=5)
        process_cpu_before = cpu_seconds(server.pid)
        measured_since = time.monotonic()
        for index in range(arguments.sends):
            marker = f"probe-{uuid.uuid4().hex[:10]}"
            watcher.waiting.add(marker)
            sent = time.perf_counter()
            elapsed, result, _ = client.call(f"/api/browser/sessions/{target}/messages", {"message": f"Measure {marker}"})
            seen = watcher.wait_for(marker)
            watcher.waiting.discard(marker)
            samples.setdefault("send_accepted", []).append(elapsed)
            if seen:
                samples.setdefault("send_event_visible", []).append((seen - sent) * 1000)
            time.sleep(SyntheticProvider.short_turn_seconds + .7)
        for _ in range(arguments.reads):
            for name, path in (("read_target", f"/api/browser/sessions/{target}"),
                               ("thread_target", f"/api/browser/threads/{target}"),
                               ("sidecar_status", f"/api/browser/threads/{target}/sidecars"),
                               ("list", "/api/browser/threads"),
                               ("search", "/api/browser/search?q=latency&scope=all")):
                elapsed, _, size = client.call(path)
                samples.setdefault(name, []).append(elapsed)
                report.setdefault("bytes", {})[name] = size
        report["backend_cpu_percent"] = round(100 * (cpu_seconds(server.pid) - process_cpu_before) /
                                              (time.monotonic() - measured_since), 1)
        if arguments.hold:
            print(json.dumps({"status": "holding", "port": port, "target": target, "seconds": arguments.hold}), flush=True)
            time.sleep(arguments.hold)
    except BaseException as error:
        report["measurement_error"] = type(error).__name__ + ": " + str(error)
        report["samples"].setdefault("errors", []).append(report["measurement_error"])
        raise
    finally:
        # A second ordinary termination must not interrupt child reaping and
        # database deletion. SIGKILL remains an operating-system hard stop.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        stop.set()
        try:
            try:
                cleanup_workload(server, hooks, client, busy, report,
                                 quiet_period=max(SyntheticProvider.short_turn_seconds + .5, 2 / arguments.delta_rate))
            finally:
                for handle in (server.stdout if server is not None else None, server_log):
                    if handle is not None:
                        try:
                            handle.close()
                        except Exception as error:
                            report.setdefault("cleanup_errors", []).append("Measurement stream close failed: " + str(error))
                finish_report(report, root, arguments.output)
        finally:
            signal.signal(signal.SIGTERM, previous_termination)
    if arguments.check and (report["budget_failures"] or report["error_count"] or report.get("load_start_failures")):
        sys.exit(1)


def cpu_seconds(pid):
    output = subprocess.run(["ps", "-o", "cputime=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    if not output:
        return 0.0
    parts = [float(part) for part in output.replace("-", ":").split(":")]
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + part
    return seconds


if __name__ == "__main__":
    main()
