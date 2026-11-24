#!/usr/bin/env python3
"""Measure isolated search on generated data or a bounded read-only snapshot.

All destination state is disposable. Receipts contain counts and timings only.
"""
from __future__ import annotations
import argparse
import hashlib
import subprocess
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins/agent-coord/scripts'))
from agent_coord.history_persistence import decode_history
from agent_coord.search_store import SearchStore
from agent_coord.store import CoordinationStore, _StoreCursor
from agent_coord.thread_search import search_threads


def distribution(values):
    values = sorted(values)
    return {'count': len(values), 'p50_ms': values[len(values) // 2],
            'p95_ms': values[min(len(values)-1, int(len(values)*.95))], 'max_ms': max(values)}


def source_records(source, count, turns):
    if source is None:
        for index in range(count):
            yield {'thread_id': f'thread-{index}', 'revision': 1,
                'metadata': {'title': f'Database analysis {index}'},
                'history': {'turns': [{'id': f'turn-{turn}', 'status': 'completed', 'items': [
                    {'id': f'message-{turn}', 'type': 'agentMessage',
                     'text': f'Database writer revision {index}::{turn} API::get_user Straße 日本語 ' + 'ordinary material ' * 30}]
                    } for turn in range(turns)]}}
        return
    connection = sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA query_only=ON')
    try:
        rows = connection.execute('''SELECT h.thread_id,h.history_json,t.title,t.original_request,t.domain
            FROM browser_history h JOIN work_threads t ON t.thread_id=h.thread_id ORDER BY h.rowid LIMIT ?''', (count,))
        for row in rows:
            yield {'thread_id': row['thread_id'], 'revision': 1,
                   'metadata': {'title': row['title'], 'request': row['original_request'], 'domain': row['domain']},
                   'history': decode_history(row['history_json'], db=connection)}
    finally:
        connection.close()


def measure(source, count, turns, repetitions, explain=False):
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix='isolated-search-measurement-') as directory:
        store = CoordinationStore(Path(directory) / 'state.sqlite3')
        index = SearchStore.for_store(store)
        try:
            # A bounded sample is materialized so verification reuses identical
            # source bytes. The full migration consumes streaming iterators.
            records = list(source_records(source, count, turns))
            for record in records:
                store.register(session_id=record['thread_id'], client='codex', cwd=directory,
                               name=record['metadata']['title'] or 'Conversation')
                store.threads.capture_request(record['thread_id'], record['metadata'].get('request') or 'Conversation')
            rebuild_started = time.perf_counter()
            list(index.rebuild_steps(records))
            verification = index.verify(records)
            index.activate()
            rebuild_seconds = time.perf_counter()-rebuild_started
            timings, plans = {}, []
            execute = _StoreCursor.execute
            def inspect(cursor, sql, parameters=()):
                if ('isolated_search' in sql and sql.lstrip().upper().startswith(('SELECT', 'WITH'))):
                    independent = cursor.connection.cursor(factory=sqlite3.Cursor)
                    try:
                        plan = independent.execute('EXPLAIN QUERY PLAN ' + sql, parameters).fetchall()
                        plans.append({'nodes': [tuple(row) for row in plan],
                                      'fts_scans': sum('SCAN f VIRTUAL TABLE INDEX' in row[3] for row in plan)})
                    finally:
                        independent.close()
                return execute(cursor, sql, parameters)
            if explain:
                with patch.object(_StoreCursor, 'execute', inspect):
                    search_threads(store, 'database writer')
            for query in ('database', 'runtime', 'API::get_user', 'strasse', '日本', 'x', 'database writer'):
                values = []
                for _ in range(repetitions):
                    before = time.perf_counter()
                    search_threads(store, query)
                    values.append((time.perf_counter()-before)*1000)
                timings[query] = distribution(values)
            revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
            fingerprint = hashlib.sha256((Path(__file__).read_bytes() +
                (Path(__file__).parents[1] / 'plugins/agent-coord/scripts/agent_coord/search_store.py').read_bytes() +
                (Path(__file__).parents[1] / 'plugins/agent-coord/scripts/agent_coord/thread_search.py').read_bytes())).hexdigest()
            return {'source_revision': revision, 'source_fingerprint': fingerprint,
                    'workload': 'bounded read-only private snapshot' if source else 'generated finalized histories',
                    'coverage': index.coverage(), 'verification': verification, 'queries': timings,
                    'query_plans': plans,
                    'rebuild_seconds': rebuild_seconds, 'total_seconds': time.perf_counter()-started}
        finally:
            index.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--explain', action='store_true')
    parser.add_argument('--threads', type=int, default=2000)
    parser.add_argument('--turns', type=int, default=20)
    parser.add_argument('--repetitions', type=int, default=20)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if min(args.threads, args.turns, args.repetitions) < 1:
        parser.error('Workload sizes must be positive')
    result = measure(args.source, args.threads, args.turns, args.repetitions, args.explain)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))
