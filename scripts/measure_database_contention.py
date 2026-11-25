#!/usr/bin/env python3
"""Stage 3 real coordination, transcript, and isolated search contention gate.

Seeds a disposable 2,000-thread fixture (including a 350-turn conversation with
large tool outputs) inside tempfile.TemporaryDirectory(), verifies 0 state.sqlite3
writes during 10 concurrent streaming turns, and measures p50/p95/p99 latencies
for concurrent open (read_window), send_accept, search_threads, and writer_hold.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import copy
import json
import os
from pathlib import Path
import platform
import random
import sqlite3
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))

from agent_coord import desktop_runtime
from agent_coord.codex_app_server import BrowserSessions
from agent_coord.coordinator_projection import CoordinatorProjection
from agent_coord.database_writer import _distribution
from agent_coord.store import CoordinationStore
from agent_coord.thread_search import search_threads

CORPUS_THREADS = 2000
DEEP_THREAD_TURNS = 350
CONCURRENT_STREAMING_TURNS = 10


class _BenchmarkCodexRPC:
    """In-memory provider double for deterministic offline contention measurement."""

    def __init__(self, store: CoordinationStore, callback) -> None:
        self.store = store
        self.callback = callback
        self.threads: dict[str, dict] = {}
        self.settings: dict[str, dict] = {}
        self.next_id = 0
        self.ready = True
        self.initialized_response = True
        self.pending_methods: dict[int, str] = {}
        self.closed = False

    def start(self) -> None:
        self.ready = True

    def request(self, method: str, params: dict | None = None, **_kwargs) -> dict:
        params = params or {}
        tid = params.get("threadId")
        if method == "model/list":
            return {
                "data": [
                    {
                        "model": "benchmark-model",
                        "isDefault": True,
                        "serviceTiers": [{"id": "priority"}],
                        "defaultReasoningEffort": "medium",
                        "supportedReasoningEfforts": [{"reasoningEffort": "medium"}],
                    }
                ]
            }
        if method == "thread/start":
            tid = f"live-{len(self.threads) + 1}"
            self.threads[tid] = {"id": tid, "cwd": params.get("cwd", ""), "turns": []}
            return {"thread": copy.deepcopy(self.threads[tid]), "model": "benchmark-model", "reasoningEffort": "medium"}
        if method in {"thread/resume", "thread/read"}:
            thread = self.threads.setdefault(tid, {"id": tid, "cwd": params.get("cwd", ""), "turns": []})
            snap = copy.deepcopy(thread)
            if params.get("excludeTurns") or (method == "thread/read" and not params.get("includeTurns")):
                snap["turns"] = []
            return {"thread": snap, "model": "benchmark-model", "reasoningEffort": "medium"}
        if method == "turn/start":
            turns = self.threads.setdefault(tid, {"id": tid, "cwd": params.get("cwd", ""), "turns": []})["turns"]
            turn = {
                "id": f"turn-{len(turns)}",
                "status": "inProgress",
                "items": [{"id": f"user-{len(turns)}", "type": "userMessage", "content": params.get("input", [])}],
            }
            turns.append(turn)
            self.callback({"method": "turn/started", "params": {"threadId": tid, "turn": copy.deepcopy(turn)}})
            return {"turn": copy.deepcopy(turn)}
        if method == "turn/steer":
            turns = self.threads[tid]["turns"]
            turn = turns[-1]
            item = {"id": f"steer-{len(turn['items'])}", "type": "userMessage", "content": params.get("input", [])}
            turn["items"].append(item)
            return {"turnId": turn["id"]}
        return {}

    def write(self, _message: dict) -> None:
        return

    def close(self) -> None:
        self.closed = True


class _BenchmarkTransportClient:
    record = {"instance": "benchmark-transport", "pid": 42424}
    epoch = 1

    def call(self, action: str, **_values) -> dict:
        if action == "events":
            return {"events": []}
        if action == "status":
            return {"channels": []}
        return {"outcome": "written", "result": {"pid": 42424}}

    def command(self, *_args, **_kwargs) -> dict:
        return {"closed": True}


def _seed_corpus(store: CoordinationStore, sessions: BrowserSessions, root: Path) -> tuple[str, list[str], list[str]]:
    """Fast batch seed of 2,000 threads + a 350-turn deep thread + 10 active streaming threads."""
    now = time.time()
    cwd_str = str(root)
    deep_id = "seeded-deep-350"
    seeded_ids = [deep_id] + [f"seeded-{idx:04d}" for idx in range(1, CORPUS_THREADS)]
    topics = ("database", "protocol", "validation", "scheduler", "telemetry", "compiler", "indexing", "transport")

    with store.database_operation("benchmark.seed"), store._connection() as db:
        db.execute("BEGIN IMMEDIATE")
        with store.sharing_transaction(db):
            browser_rows = []
            for idx, tid in enumerate(seeded_ids):
                topic_a = topics[idx % len(topics)]
                topic_b = topics[(idx * 3 + 1) % len(topics)]
                title = f"Subsystem {topic_a} {topic_b} {idx:04d}"
                request = f"Verify {topic_a} throughput and {topic_b} invariants for unit {idx}"
                ts = now - (CORPUS_THREADS - idx)
                store.register(session_id=tid, client="codex", cwd=cwd_str, name=title)
                store.threads.capture_request(tid, request, connection=db)
                if idx % 4 == 0:
                    db.execute("UPDATE work_threads SET attention='archived' WHERE thread_id=?", (tid,))
                browser_rows.append(
                    (tid, cwd_str, title, "benchmark-model", "medium", 0, int(idx % 4 == 0), "codex", ts, ts)
                )
            db.executemany(
                """INSERT INTO browser_sessions (
                       thread_id, cwd, name, model, effort, yolo, archived, client, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                browser_rows,
            )

    # Seed the 350-turn deep conversation with realistic > 2 KB tool outputs.
    tool_blob = ("0010  LOAD   R1, BUFFER_OFFSET\n" * 80)
    deep_turns = [
        {
            "id": f"deep-turn-{seq}",
            "status": "completed",
            "startedAt": now - 4000 + seq * 2,
            "completedAt": now - 3999 + seq * 2,
            "items": [
                {
                    "id": f"u-{seq}",
                    "type": "userMessage",
                    "content": [{"type": "text", "text": f"Inspect database validation block {seq}"}],
                },
                {
                    "id": f"cmd-{seq}",
                    "type": "commandExecution",
                    "command": "pytest -q",
                    "status": "completed",
                    "aggregatedOutput": tool_blob,
                },
                {
                    "id": f"a-{seq}",
                    "type": "agentMessage",
                    "text": f"Completed database latency protocol check for iteration {seq}.",
                },
            ],
        }
        for seq in range(DEEP_THREAD_TURNS)
    ]
    store.transcripts.save_turns(deep_id, {"id": deep_id, "turns": deep_turns, "status": {"type": "idle"}})

    # Seed finalized transcript turns across a realistic slice of conversations for FTS5 search.
    for idx in range(1, 250):
        tid = seeded_ids[idx]
        topic_a = topics[idx % len(topics)]
        topic_b = topics[(idx * 3 + 1) % len(topics)]
        store.transcripts.save_turns(
            tid,
            {
                "id": tid,
                "turns": [
                    {
                        "id": "t-0",
                        "status": "completed",
                        "startedAt": now - 600 + idx,
                        "completedAt": now - 590 + idx,
                        "items": [
                            {
                                "id": "u-0",
                                "type": "userMessage",
                                "content": [{"type": "text", "text": f"Investigate {topic_a} contention in {topic_b}"}],
                            },
                            {
                                "id": "a-0",
                                "type": "agentMessage",
                                "text": f"Measured {topic_a} {topic_b} specialist metrics within specification.",
                            },
                        ],
                    }
                ],
            },
        )
    store.search_store.flush(timeout=15.0)

    # Create 10 active streaming sessions and 6 dedicated foreground send targets.
    streaming_ids = [sessions.create({"name": f"Stream Agent {i}"})["session"]["thread_id"] for i in range(CONCURRENT_STREAMING_TURNS)]
    send_ids = [sessions.create({"name": f"Foreground Target {i}"})["session"]["thread_id"] for i in range(64)]
    sessions.persistence.flush(timeout=10.0)
    return deep_id, seeded_ids, streaming_ids + send_ids


def measure(seconds: float = 10.0, writers: int = 6) -> dict:
    sys.setswitchinterval(min(sys.getswitchinterval(), 0.001))
    with tempfile.TemporaryDirectory(prefix="database_contention_") as directory:
        root = Path(directory).resolve()
        store = CoordinationStore(root / "state.sqlite3")
        identity = str(store.database_path.resolve())
        desktop_runtime._OWNED_DATABASES.add(identity)
        sessions = BrowserSessions(store, str(root), rpc_factory=_BenchmarkCodexRPC)
        client = _BenchmarkTransportClient()
        projection = CoordinatorProjection(sessions, client)
        sessions.projection = projection
        store.provider_projection = projection
        projection.attach("codex", 42424, sessions._event, provider="codex")

        seq_lock = threading.Lock()
        seq_counter = 0

        def next_seq() -> int:
            nonlocal seq_counter
            with seq_lock:
                seq_counter += 1
                return seq_counter

        try:
            deep_id, seeded_ids, live_pool = _seed_corpus(store, sessions, root)
            streaming_ids = live_pool[:CONCURRENT_STREAMING_TURNS]
            send_ids = live_pool[CONCURRENT_STREAMING_TURNS : CONCURRENT_STREAMING_TURNS + writers]

            # Start all 10 streaming turns cleanly before measuring pure streaming deltas.
            active_turns: dict[str, str] = {}
            for tid in streaming_ids:
                turn = sessions.send(tid, {"message": "Begin streaming workload"})["turn"]
                active_turns[tid] = turn["id"]
            sessions.persistence.flush(timeout=10.0)

            # Clear one-time corpus seeding samples so metrics reflect steady-state workload only.
            with store._writer.condition:
                store._writer.waits.clear()
                store._writer.holds.clear()
                store._writer.slow_operations.clear()
                store._writer.slow_writer_scopes.clear()

            # Phase 1: Verify 0 state.sqlite3 writes across 10 concurrent streaming turns
            # emitting item/started, token deltas, output deltas, and item/completed.
            before_scopes = store.database_metrics()["completed_scopes"]
            pure_stream_batch = []
            for round_idx in range(5):
                for tid in streaming_ids:
                    turn_id = active_turns[tid]
                    msg_id = f"stream-msg-{round_idx}"
                    if round_idx == 0:
                        pure_stream_batch.append(
                            {
                                "sequence": next_seq(),
                                "channel": "codex",
                                "stream": "stdout",
                                "data": json.dumps(
                                    {
                                        "method": "item/started",
                                        "params": {
                                            "threadId": tid,
                                            "turnId": turn_id,
                                            "item": {"id": msg_id, "type": "agentMessage", "text": ""},
                                        },
                                    }
                                ),
                            }
                        )
                    pure_stream_batch.append(
                        {
                            "sequence": next_seq(),
                            "channel": "codex",
                            "stream": "stdout",
                            "data": json.dumps(
                                {
                                    "method": "item/agentMessage/delta",
                                    "params": {
                                        "threadId": tid,
                                        "turnId": turn_id,
                                        "itemId": "stream-msg-0",
                                        "delta": f" token-{round_idx}",
                                    },
                                }
                            ),
                        }
                    )
                    if round_idx == 4:
                        pure_stream_batch.append(
                            {
                                "sequence": next_seq(),
                                "channel": "codex",
                                "stream": "stdout",
                                "data": json.dumps(
                                    {
                                        "method": "item/completed",
                                        "params": {
                                            "threadId": tid,
                                            "turnId": turn_id,
                                            "item": {
                                                "id": "stream-msg-0",
                                                "type": "agentMessage",
                                                "text": " token-0 token-1 token-2 token-3 token-4",
                                            },
                                        },
                                    }
                                ),
                            }
                        )
            projection.project_batch(pure_stream_batch)
            after_scopes = store.database_metrics()["completed_scopes"]
            streaming_sqlite_writes = after_scopes - before_scopes

            # Phase 2: Full concurrent workload (10 streaming turns + foreground open, send_accept, search).
            stop = threading.Event()
            guard = threading.Lock()
            failures: list[str] = []
            open_latencies: list[float] = []
            send_latencies: list[float] = []
            search_latencies: list[float] = []
            stream_frames = 0
            worker_count = writers + 5
            barrier = threading.Barrier(worker_count + 1)

            def streaming_driver() -> None:
                nonlocal stream_frames
                step = 0
                barrier.wait()
                while not stop.is_set():
                    try:
                        batch = []
                        for tid in streaming_ids:
                            turn_id = active_turns[tid]
                            item_id = f"live-item-{step // 8}"
                            if step % 8 == 0:
                                batch.append(
                                    {
                                        "sequence": next_seq(),
                                        "channel": "codex",
                                        "stream": "stdout",
                                        "data": json.dumps(
                                            {
                                                "method": "item/started",
                                                "params": {
                                                    "threadId": tid,
                                                    "turnId": turn_id,
                                                    "item": {"id": item_id, "type": "agentMessage", "text": ""},
                                                },
                                            }
                                        ),
                                    }
                                )
                            batch.append(
                                {
                                    "sequence": next_seq(),
                                    "channel": "codex",
                                    "stream": "stdout",
                                    "data": json.dumps(
                                        {
                                            "method": "item/agentMessage/delta",
                                            "params": {
                                                "threadId": tid,
                                                "turnId": turn_id,
                                                "itemId": item_id,
                                                "delta": " delta-payload",
                                            },
                                        }
                                    ),
                                }
                            )
                            if step % 8 == 7:
                                batch.append(
                                    {
                                        "sequence": next_seq(),
                                        "channel": "codex",
                                        "stream": "stdout",
                                        "data": json.dumps(
                                            {
                                                "method": "item/completed",
                                                "params": {
                                                    "threadId": tid,
                                                    "turnId": turn_id,
                                                    "item": {
                                                        "id": item_id,
                                                        "type": "agentMessage",
                                                        "text": "completed segment",
                                                    },
                                                },
                                            }
                                        ),
                                    }
                                )
                        projection.project_batch(batch)
                        with guard:
                            stream_frames += len(batch)
                        step += 1
                    except Exception as exc:  # noqa: BLE001
                        with guard:
                            failures.append(f"stream: {type(exc).__name__}: {exc}")
                    stop.wait(0.01)

            def open_worker(seed_offset: int) -> None:
                rng = random.Random(100 + seed_offset)
                iteration = 0
                barrier.wait()
                while not stop.is_set():
                    target = deep_id if iteration % 2 == 0 else seeded_ids[rng.randrange(250)]
                    iteration += 1
                    t0 = time.perf_counter()
                    try:
                        window = store.transcripts.read_window(target, limit=40)
                        if target == deep_id and (window is None or len(window["turns"]) != 40):
                            raise AssertionError("Unexpected deep window turn count")
                        elapsed = (time.perf_counter() - t0) * 1000.0
                        with guard:
                            open_latencies.append(elapsed)
                    except Exception as exc:  # noqa: BLE001
                        with guard:
                            failures.append(f"open: {type(exc).__name__}: {exc}")
                    stop.wait(0.01)

            def send_worker(worker_idx: int) -> None:
                target = send_ids[worker_idx]
                req_num = 0
                expected_turn_id: str | None = None
                barrier.wait()
                while not stop.is_set():
                    req_num += 1
                    payload: dict = {"message": f"Foreground instruction {req_num}", "requestId": f"w{worker_idx}-{req_num}"}
                    if expected_turn_id is not None:
                        payload["expectedTurnId"] = expected_turn_id
                    t0 = time.perf_counter()
                    try:
                        res = sessions.submit(target, payload)
                        if "turn" in res:
                            expected_turn_id = res["turn"]["id"]
                        elif "turnId" in res:
                            expected_turn_id = res["turnId"]
                        elapsed = (time.perf_counter() - t0) * 1000.0
                        with guard:
                            send_latencies.append(elapsed)
                    except Exception as exc:  # noqa: BLE001
                        with guard:
                            failures.append(f"send: {type(exc).__name__}: {exc}")
                    stop.wait(0.055)

            def search_worker(worker_idx: int) -> None:
                queries = ("database", "protocol telemetry", "scheduler indexing", "telemetry compiler")
                iteration = worker_idx
                barrier.wait()
                while not stop.is_set():
                    q = queries[iteration % len(queries)]
                    iteration += 1
                    t0 = time.perf_counter()
                    try:
                        page = search_threads(store, q, limit=20)
                        if not page["items"]:
                            raise AssertionError(f"Empty search results for {q}")
                        elapsed = (time.perf_counter() - t0) * 1000.0
                        with guard:
                            search_latencies.append(elapsed)
                    except Exception as exc:  # noqa: BLE001
                        with guard:
                            failures.append(f"search: {type(exc).__name__}: {exc}")
                    stop.wait(0.02)

            started = time.monotonic()
            with ThreadPoolExecutor(max_workers=worker_count) as pool:
                tasks = [pool.submit(streaming_driver)]
                tasks += [pool.submit(open_worker, i) for i in range(2)]
                tasks += [pool.submit(search_worker, i) for i in range(2)]
                tasks += [pool.submit(send_worker, i) for i in range(writers)]
                barrier.wait()
                stop.wait(seconds)
                stop.set()
                for task in tasks:
                    task.result(timeout=15)

            elapsed_seconds = round(time.monotonic() - started, 3)
            metrics = store.database_metrics()
            search_status = store.search_store.status()
            state_bytes = store.database_path.stat().st_size
        finally:
            desktop_runtime._OWNED_DATABASES.discard(identity)
            sessions.close()
            store.transcripts.close()
            store.search_store.close()
            store.close_idle_connections()

    open_dist = _distribution(open_latencies)
    send_dist = _distribution(send_latencies)
    search_dist = _distribution(search_latencies)
    writer_hold = metrics["writer_hold"]

    breaches = []
    if failures:
        breaches.append("workload errors")
    if streaming_sqlite_writes != 0:
        breaches.append(f"streaming turns wrote to state.sqlite3 ({streaming_sqlite_writes} scopes)")
    if open_dist["p95_ms"] > 15.0:
        breaches.append(f"open p95 ({open_dist['p95_ms']} ms) exceeds 15 ms")
    if send_dist["p95_ms"] > 15.0:
        breaches.append(f"send_accept p95 ({send_dist['p95_ms']} ms) exceeds 15 ms")
    if search_dist["p95_ms"] > 50.0:
        breaches.append(f"search p95 ({search_dist['p95_ms']} ms) exceeds 50 ms")
    if writer_hold["p95_ms"] > 5.0:
        breaches.append(f"writer_hold p95 ({writer_hold['p95_ms']} ms) exceeds 5 ms")
    if metrics["admission_timeouts"]:
        breaches.append("writer admission timeout")

    return {
        "schema_version": 2,
        "source_revision": os.environ.get("GITHUB_SHA"),
        "source_fingerprint": desktop_runtime.schema_fingerprint(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "workload": {
            "seconds": seconds,
            "seeded_threads": CORPUS_THREADS,
            "deep_thread_turns": DEEP_THREAD_TURNS,
            "concurrent_streaming_turns": CONCURRENT_STREAMING_TURNS,
            "foreground_send_writers": writers,
            "open_readers": 2,
            "search_readers": 2,
        },
        "elapsed_seconds": elapsed_seconds,
        "streaming_state_sqlite_writes": streaming_sqlite_writes,
        "streamed_frames": stream_frames,
        "state_database_bytes": state_bytes,
        "search_database_bytes": search_status["size_bytes"],
        "open_latency": open_dist,
        "send_accept_latency": send_dist,
        "search_latency": search_dist,
        "foreground_latency": send_dist,
        "writer_hold": writer_hold,
        "errors": failures[:20],
        "error_count": len(failures),
        "metrics": metrics,
        "budget_failures": breaches,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=10.0)
    parser.add_argument("--writers", type=int, default=6)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.seconds <= 3600 or not 1 <= args.writers <= 64:
        parser.error("seconds must be 1-3600 and writers 1-64")
    result = measure(args.seconds, args.writers)
    encoded = json.dumps(result, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n")
    print(encoded)
    return int(args.check and bool(result["budget_failures"]))


if __name__ == "__main__":
    raise SystemExit(main())
