#!/usr/bin/env python3
"""Repeatable writer-admission workload on generated, disposable state.

No models, accounts, network requests or installed databases are used. This is
an admission regression gate; measure_backend_latency.py separately exercises
actual HTTP acceptance, provider events, indexing and history on a snapshot.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import platform
import sqlite3
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins/agent-coord/scripts'))
from agent_coord import desktop_runtime
from agent_coord.database_writer import _distribution
from agent_coord.store import CoordinationStore


def measure(seconds=15, writers=6):
    with tempfile.TemporaryDirectory(prefix='database_contention_') as directory:
        store = CoordinationStore(Path(directory) / 'state.sqlite3')
        with store._connection() as db:
            db.executescript('CREATE TABLE foreground_receipts(id INTEGER PRIMARY KEY, worker INTEGER); '
                             'CREATE TABLE background_chunks(id INTEGER PRIMARY KEY, data BLOB);')
        identity = str(store.database_path.resolve())
        desktop_runtime._OWNED_DATABASES.add(identity)
        stop = threading.Event()
        guard = threading.Lock()
        failures, latencies = [], []
        active = maximum = reads = background = 0
        barrier = threading.Barrier(writers + 4)
        def foreground(worker):
            nonlocal active, maximum
            barrier.wait()
            while not stop.is_set():
                started = time.perf_counter()
                try:
                    with store.database_operation('foreground.receipt'), store._connection() as db:
                        with guard:
                            active += 1
                            maximum = max(maximum, active)
                        try:
                            db.execute('INSERT INTO foreground_receipts(worker) VALUES (?)', (worker,))
                        finally:
                            with guard:
                                active -= 1
                    with guard:
                        latencies.append((time.perf_counter() - started) * 1000)
                except Exception as error:
                    with guard:
                        failures.append(type(error).__name__ + ': ' + str(error))
                stop.wait(.02)
        def persistence():
            nonlocal background, active, maximum
            payload = bytes(128 * 1024)
            barrier.wait()
            while not stop.is_set():
                try:
                    with store.database_operation('background.chunk', priority='background'), store._connection() as db:
                        with guard:
                            active += 1
                            maximum = max(maximum, active)
                        try:
                            db.execute('INSERT OR REPLACE INTO background_chunks VALUES (?, ?)', (background % 8, payload))
                        finally:
                            with guard:
                                active -= 1
                    background += 1
                except Exception as error:
                    with guard:
                        failures.append(type(error).__name__ + ': ' + str(error))
                stop.wait(.01)
        def reader():
            nonlocal reads
            barrier.wait()
            while not stop.is_set():
                try:
                    with store.reading(), store._connection() as db:
                        db.execute('SELECT count(*) FROM foreground_receipts').fetchone()
                    with guard:
                        reads += 1
                except Exception as error:
                    with guard:
                        failures.append(type(error).__name__ + ': ' + str(error))
                stop.wait(.01)
        started = time.monotonic()
        try:
            with ThreadPoolExecutor(max_workers=writers + 3) as pool:
                tasks = [pool.submit(foreground, n) for n in range(writers)]
                tasks += [pool.submit(persistence), pool.submit(reader), pool.submit(reader)]
                barrier.wait()
                stop.wait(seconds)
                stop.set()
                for task in tasks:
                    task.result(timeout=10)
            with store.reading(), store._connection() as db:
                durable = db.execute('SELECT count(*) FROM foreground_receipts').fetchone()[0]
            metrics = store.database_metrics()
        finally:
            stop.set()
            desktop_runtime._OWNED_DATABASES.discard(identity)
        distribution = _distribution(latencies)
        breaches = []
        if failures: breaches.append('workload errors')
        if maximum != 1: breaches.append('overlapping writable scopes')
        if durable != len(latencies): breaches.append('durable receipt count mismatch')
        if len(latencies) < writers * seconds * 5: breaches.append('insufficient foreground throughput')
        if background < seconds * 5: breaches.append('background writer starvation')
        if reads < seconds * 10: breaches.append('insufficient independent reader progress')
        if distribution['p95_ms'] > 100: breaches.append('foreground p95 exceeds 100 ms')
        if metrics['writer_hold']['p95_ms'] > 25: breaches.append('writer hold p95 exceeds 25 ms')
        if metrics['admission_timeouts']: breaches.append('writer admission timeout')
        return {'schema_version': 1, 'source_revision': os.environ.get('GITHUB_SHA'),
                'source_fingerprint': desktop_runtime.schema_fingerprint(),
                'platform': platform.platform(), 'python': platform.python_version(), 'sqlite': sqlite3.sqlite_version,
                'workload': {'seconds': seconds, 'foreground_writers': writers, 'background_writers': 1,
                             'readers': 2, 'chunk_bytes': 128 * 1024},
                'elapsed_seconds': round(time.monotonic() - started, 3),
                'foreground_latency': distribution, 'durable_receipts': durable,
                'background_commits': background, 'reads': reads,
                'maximum_concurrent_writable_scopes': maximum, 'errors': failures[:20],
                'error_count': len(failures), 'metrics': metrics, 'budget_failures': breaches}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=float, default=15)
    parser.add_argument('--writers', type=int, default=6)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.seconds <= 3600 or not 1 <= args.writers <= 64:
        parser.error('seconds must be 1–3600 and writers 1–64')
    result = measure(args.seconds, args.writers)
    encoded = json.dumps(result, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + '\n')
    print(encoded)
    return int(args.check and bool(result['budget_failures']))


if __name__ == '__main__':
    raise SystemExit(main())
