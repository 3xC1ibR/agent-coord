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
import json
import os
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
    "read_target": {"p50": 300, "p95": 800},
    "thread_target": {"p50": 100, "p95": 300},
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


def quantiles(values):
    ordered = sorted(values)
    if not ordered:
        return {"n": 0}
    pick = lambda q: round(ordered[min(len(ordered) - 1, int(q * (len(ordered) - 1) + 0.5))], 1)
    return {"n": len(ordered), "p50": pick(.5), "p90": pick(.9), "p95": pick(.95), "max": round(ordered[-1], 1),
            "mean": round(statistics.fmean(ordered), 1)}


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

    def __init__(self, store, callback):
        self.store = store
        self.callback = callback
        self.threads = {}
        self.lock = threading.Lock()
        self.closed = False

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
            with self.store._connection() as connection:
                row = connection.execute("SELECT history_json FROM browser_history WHERE thread_id = ?", (thread_id,)).fetchone()
            return json.loads(row[0]).get("turns", []) if row else []
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
            threading.Thread(target=self._stream, args=(thread_id, turn, duration), daemon=True).start()
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
            return {"turnId": turn["id"]}
        if method == "turn/interrupt":
            thread = self._thread(thread_id)
            if thread["turns"]:
                thread["turns"][-1]["status"] = "interrupted"
            return {}
        return {}

    def _stream(self, thread_id, turn, duration):
        ends = time.monotonic() + duration
        message = {"id": uuid.uuid4().hex, "type": "agentMessage", "text": ""}
        self._emit({"method": "item/started", "params": {"threadId": thread_id, "turnId": turn["id"], "item": message}})
        sequence = 0
        while time.monotonic() < ends and not self.closed and turn["status"] == "inProgress":
            sequence += 1
            delta = "token " * 6
            message["text"] += delta
            self._emit({"method": "item/agentMessage/delta", "params": {
                "threadId": thread_id, "turnId": turn["id"], "itemId": message["id"], "delta": delta}})
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
                self._emit({"method": "item/completed", "params": {"threadId": thread_id, "turnId": turn["id"], "item": tool}})
                self._emit({"method": "thread/tokenUsage/updated", "params": {"threadId": thread_id, "turnId": turn["id"],
                            "tokenUsage": {"total": {"inputTokens": 1000 * sequence, "outputTokens": 10 * sequence,
                                                     "totalTokens": 1010 * sequence}, "last": {"totalTokens": 900},
                                           "modelContextWindow": 400000}}})
            time.sleep(1 / self.delta_rate)
        if turn["status"] == "inProgress":
            turn["status"] = "completed"
        turn["items"].append(copy.deepcopy(message))
        self._emit({"method": "item/completed", "params": {"threadId": thread_id, "turnId": turn["id"], "item": message}})
        self._emit({"method": "turn/completed", "params": {"threadId": thread_id, "turn": copy.deepcopy(turn)}})

    def write(self, message):
        pass

    def close(self):
        self.closed = True


class StackSampler(threading.Thread):
    """Python-level sampling profile of every thread (diagnostic only)."""

    def __init__(self, path, interval=0.005):
        super().__init__(daemon=True)
        self.path, self.interval, self.counts, self.samples = path, interval, {}, 0

    def run(self):
        import traceback
        own = threading.get_ident()
        while True:
            time.sleep(self.interval)
            self.samples += 1
            for ident, frame in sys._current_frames().items():
                if ident == own:
                    continue
                stack = traceback.extract_stack(frame)[-7:]
                key = " <- ".join(f"{Path(entry.filename).name}:{entry.name}:{entry.lineno}" for entry in reversed(stack))
                self.counts[key] = self.counts.get(key, 0) + 1

    def write(self):
        ranked = sorted(self.counts.items(), key=lambda item: -item[1])[:60]
        Path(self.path).write_text(f"samples {self.samples}\n" + "\n".join(f"{count:7d} {key}" for key, count in ranked))


def serve(arguments):
    # --source selects another backend tree, e.g. a retained runtime payload,
    # so the same workload measures the code a runtime is actually running.
    sys.path.insert(0, arguments.source or str(SCRIPTS))
    for key in ("AGENT_COORD_SESSION_ID", "CODEX_THREAD_ID", "CLAUDECODE"):
        os.environ.pop(key, None)
    from agent_coord.store import CoordinationStore
    from agent_coord.codex_app_server import BrowserSessions
    from agent_coord import ui
    SyntheticProvider.history_kb = arguments.history_kb
    SyntheticProvider.stored_history = arguments.stored_history
    SyntheticProvider.delta_rate = arguments.delta_rate
    store = CoordinationStore(Path(arguments.db))
    workspace = arguments.cwd or None
    sessions = BrowserSessions(store, workspace, rpc_factory=SyntheticProvider, claude_factory=SyntheticProvider,
                               antigravity_factory=SyntheticProvider, kimi_factory=SyntheticProvider,
                               muse_factory=SyntheticProvider)
    server = ui.make_ui_server(store, port=0, cwd=workspace, browser_sessions=sessions)
    sampler = StackSampler(arguments.profile) if arguments.profile else None
    if sampler:
        sampler.start()
    print(json.dumps({"port": server.server_address[1], "pid": os.getpid()}), flush=True)
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
    arguments = parser.parse_args()
    if arguments.mode == "serve":
        return serve(arguments)
    if not arguments.snapshot:
        parser.error("--snapshot is required")
    root = Path(tempfile.mkdtemp(prefix="ribbon-latency-"))
    database = root / "state.sqlite3"
    subprocess.run(["cp", "-c", arguments.snapshot, str(database)], check=True)
    threads = pick_threads(database, arguments.agents + 1)
    target, busy = threads[0], threads[1:]
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("AGENT_COORD_", "RIBBON_")) and key not in {"CODEX_THREAD_ID", "CLAUDECODE"}}
    server = subprocess.Popen([sys.executable, "-B", __file__, "serve", "--db", str(database), "--cwd", arguments.cwd,
                               "--history-kb", str(arguments.history_kb), "--delta-rate", str(arguments.delta_rate)]
                              + (["--stored-history"] if arguments.stored_history else [])
                              + (["--profile", str(Path(arguments.profile).resolve())] if arguments.profile else [])
                              + (["--source", str(Path(arguments.source).resolve())] if arguments.source else []),
                              stdout=subprocess.PIPE, stderr=open(root / "server.log", "w"), env=environment, text=True)
    hooks = []
    report = {"snapshot": arguments.snapshot, "source": arguments.source or str(SCRIPTS), "root": str(root), "target": target, "agents": len(busy),
              "windows": arguments.windows, "hook_rate": arguments.hook_rate, "delta_rate": arguments.delta_rate,
              "history_kb": arguments.history_kb, "samples": {}}
    stop = threading.Event()
    try:
        started = server.stdout.readline()
        if not started:
            raise RuntimeError("backend failed to start; see " + str(root / "server.log"))
        port = json.loads(started)["port"]
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
    finally:
        stop.set()
        for hook in hooks:
            hook.terminate()
        server.terminate()
        try:
            server.wait(20)
        except subprocess.TimeoutExpired:
            server.kill()
        report["summary"] = {name: quantiles(values) for name, values in report["samples"].items() if name != "errors"}
        report["error_count"] = len(report["samples"].get("errors", []))
        report["budget_failures"] = check_budgets(report["summary"])
        report.pop("samples") if not arguments.output else None
        if arguments.output:
            Path(arguments.output).write_text(json.dumps(report, indent=1))
        print(json.dumps({k: v for k, v in report.items() if k != "samples"}, indent=1))
        shutil.rmtree(root, ignore_errors=True)
    if arguments.check and (report["budget_failures"] or report["error_count"]):
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
