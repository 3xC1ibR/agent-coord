#!/usr/bin/env python3
"""Generated Stage 3 workload; no production database or provider process is used."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from collections import deque
from contextlib import closing, ExitStack
import hashlib
import inspect
import json
import os
from pathlib import Path
import platform
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins/agent-coord/scripts'))
from agent_coord import desktop_runtime, ui
from agent_coord.store import CoordinationStore
from agent_coord.codex_app_server import BrowserSessions
from agent_coord.coordinator_projection import CoordinatorProjection
from agent_coord.provider_transport import Journal
from agent_coord.transcript_store import TranscriptStore, ensure_schema
from agent_coord.search_publication import install_change_capture, read_metadata, _search_history
from agent_coord.database_writer import _distribution
from measure_backend_latency import SyntheticProvider


class ControlledProvider(SyntheticProvider):
    """Reuse the realistic provider wire contract; the driver owns event timing."""
    stored_history = True

    def __init__(self, *args):
        super().__init__(*args)
        self.next_id, self.ready = 0, True
        self.initialized_response, self.pending_methods = True, {}

    def _start_stream(self, *args):
        pass


class RetainedClient:
    def __init__(self, journal):
        self.journal = journal
        self.record = {'instance': str(uuid.uuid4()), 'pid': os.getpid()}
        self.epoch = 1

    def call(self, action, **values):
        if action == 'acknowledge_events':
            return self.journal.acknowledge_events(**values)
        return {'outcome': 'written', 'result': {}}


def turn(sequence):
    return {'id': f'turn-{sequence}', 'status': 'completed', 'items': [
        {'id': f'user-{sequence}', 'type': 'userMessage', 'content': [{'type': 'text', 'text': 'generated request'}]},
        {'id': f'agent-{sequence}', 'type': 'agentMessage', 'text': 'generated finding searchable_identifier'},
        {'id': f'tool-{sequence}', 'type': 'commandExecution', 'command': 'rg generated',
         'status': 'completed', 'exitCode': 0, 'aggregatedOutput': 'generated output\n' * 32}]}


def gates(report):
    failures = []
    if report['mixed_stream_deltas'] < 10: failures.append('mixed streaming made no progress')
    if min(report['open_paths'].values()) < 1: failures.append('managed/external opens not covered')
    if report['error_count']: failures.append('workload errors')
    expected = report['workload']
    if report['seeded'] != {'threads': expected['threads'], 'turns': expected['threads'] * expected['turns_per_thread']}:
        failures.append('seeded transcript inventory mismatch')
    for name in ('open', 'send_accept'):
        if report[name]['count'] < 1: failures.append(name + ' made no progress')
        if report[name]['p95_ms'] >= 15: failures.append(name + ' p95 must be <15 ms')
    if report['writer_hold']['p95_ms'] >= 2: failures.append('writer hold p95 must be <2 ms')
    if report['admission_timeouts']: failures.append('writer admission timeout')
    if not report['search']['database_isolated']: failures.append('search database is not isolated')
    for name in ('open', 'send_accept'):
        if report[name]['count'] < report['workload']['seconds'] * 5:
            failures.append(name + ' insufficient throughput')
    if report['durable_receipts'] != report['send_accept']['count']: failures.append('durable receipt mismatch')
    if report['finalized_writes'] < 1: failures.append('finalized writer made no progress')
    if not report['search'].get('new_transcript_published'): failures.append('new transcript publication missing')
    if report['search']['indexed'] < 1: failures.append('isolated search made no progress')
    stream = report['streaming']
    if stream['turns'] != 10 or stream['projected_deltas'] < 10: failures.append('ten streaming turns not observed')
    if (stream['state_data_version_changes'] or stream['transport_changes'] or
            stream.get('state_write_statements', 0) or stream.get('transport_write_statements', 0)): failures.append('SQLite writes during streaming deltas')
    return failures


def measure(seconds=15, threads=2000, turns=350, rounds=10):
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=Path(__file__).resolve().parents[1], text=True).strip()
    harness_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    setup_start = time.monotonic()
    errors = []
    with tempfile.TemporaryDirectory(prefix='transcript_workload_') as directory, ExitStack() as cleanup:
        root = Path(directory)
        store = CoordinationStore(root / 'state.sqlite3')
        identity = str(store.database_path.resolve())
        desktop_runtime._OWNED_DATABASES.add(identity)
        resources = {}
        def close_resources():
            with ExitStack() as closing_stack:
                closing_stack.callback(desktop_runtime._OWNED_DATABASES.discard, identity)
                for name in ('search', 'journal', 'sessions'):
                    if name in resources: closing_stack.callback(resources[name].close)
                if 'worker' in resources: closing_stack.callback(resources['worker'].join)
                if 'server' in resources:
                    closing_stack.callback(resources['server'].server_close)
                    closing_stack.callback(resources['server'].shutdown)
        cleanup.callback(close_resources)
        transcripts = TranscriptStore(store)
        try:
            from agent_coord.search_store import SearchStore
            search = SearchStore.for_store(store)
            resources['search'] = search
        except ImportError:
            search = None
        sessions = BrowserSessions(store, directory, rpc_factory=ControlledProvider)
        resources['sessions'] = sessions
        with store._connection() as db:
            ensure_schema(db)
            install_change_capture(db)
        histories = [turn(n) for n in range(turns)]
        pending_search = deque()
        for n in range(threads):
            thread = f'fixture-{n:05d}'
            store.register(session_id=thread, client='codex', cwd=directory)
            store.threads.ensure(thread)
            history = {'id': thread, 'cwd': directory, 'turns': histories}
            transcripts.save(thread, history, activate=True)
            if n % 2:
                with store._connection() as db:
                    db.execute('INSERT INTO browser_sessions(thread_id,cwd,name,created_at,updated_at) VALUES (?,?,?,?,?)',
                               (thread, directory, 'Generated managed thread', store.clock(), store.clock()))
            if search:
                pending_search.append(search.enqueue_history(thread, history, revision=0, metadata={'title': 'Generated workload'}))
                if len(pending_search) >= 16:
                    pending_search.popleft().result(timeout=120)
        if search: search.flush()
        with store.reading(), store._connection() as db:
            seeded = {'threads': db.execute('SELECT count(*) FROM transcript_generations').fetchone()[0],
                      'turns': db.execute('SELECT count(*) FROM thread_turns').fetchone()[0]}
            storage_settings = {key: db.execute('PRAGMA ' + key).fetchone()[0] for key in ('journal_mode', 'synchronous')}
        composer = sessions.create({})['session']['thread_id']
        streams = [sessions.create({})['session']['thread_id'] for _ in range(10)]
        for thread in streams:
            sessions.send(thread, {'message': 'generated stream'})
        sessions.persistence.flush()
        def baseline_records():
            with store.reading(), store._connection() as db:
                identities = [row[0] for row in db.execute('SELECT thread_id FROM work_threads ORDER BY thread_id')]
            for thread in identities:
                with store.reading(), store._connection() as db:
                    metadata = read_metadata(db, thread)
                yield {'thread_id': thread, 'history': _search_history(store, thread), 'metadata': metadata, 'revision': 0}
        if search:
            list(search.rebuild_steps(baseline_records()))
            verified = search.verify(baseline_records())
            if verified['failures']: raise ValueError('Search baseline verification failed')
            search.activate()
        server = ui.make_ui_server(store, port=0, cwd=directory, browser_sessions=sessions)
        server.start_storage_maintenance(lambda: True)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        resources.update(server=server, worker=worker)
        base = f'http://127.0.0.1:{server.server_address[1]}'
        with urllib.request.urlopen(base + '/api/browser/config') as response:
            token = json.load(response)['token']
        def request(path, body=None):
            data = None if body is None else json.dumps(body).encode()
            req = urllib.request.Request(base + path, data=data, headers={
                'X-Agent-Coord-Token': token, 'Origin': base, 'Content-Type': 'application/json',
                'X-Agent-Coord-History-View': 'recent'})
            start = time.perf_counter()
            with urllib.request.urlopen(req, timeout=30) as response:
                result = json.load(response)
            return (time.perf_counter() - start) * 1000, result
        publisher = server.search_publications
        if publisher is None: raise ValueError('Search publication worker not installed')
        journal_options = {'file_events': True} if 'file_events' in inspect.signature(Journal).parameters else {}
        journal = Journal(root / 'retained', **journal_options)
        resources['journal'] = journal
        projection = CoordinatorProjection(sessions, RetainedClient(journal))
        for thread in streams:
            projection.attach(thread, os.getpid(), sessions._event, provider='codex', thread_id=thread)
        sessions.projection = projection
        store.provider_projection = projection
        def emit(method, thread, **values):
            message = {'method': method, 'params': {'threadId': thread, 'turnId': sessions.active[thread], **values}}
            journal.append(thread, 'stdout', json.dumps(message))
        for thread in streams:
            emit('item/started', thread, item={'id': 'agent', 'type': 'agentMessage', 'text': ''})
        projection.project_batch(journal.events(0))
        sessions.persistence.flush()
        setup_seconds = time.monotonic() - setup_start
        # A separate delta-only interval makes boundary/foreground writes distinguishable.
        from contextlib import contextmanager
        write_statements = {'state': 0, 'transport': 0}
        def trace(database):
            def record(statement):
                if statement.lstrip().split(' ', 1)[0].upper() in {
                        'INSERT', 'UPDATE', 'DELETE', 'REPLACE', 'CREATE', 'DROP', 'ALTER', 'VACUUM'}:
                    write_statements[database] += 1
            return record
        original_connection = store._connection
        traced = []
        @contextmanager
        def traced_connection(*args, **kwargs):
            with original_connection(*args, **kwargs) as db:
                db.set_trace_callback(trace('state'))
                traced.append(db)
                yield db
        store._connection = traced_connection
        journal.db.set_trace_callback(trace('transport'))
        try:
            with closing(sqlite3.connect(store.database_path)) as observer:
                before_state = observer.execute('PRAGMA data_version').fetchone()[0]
                before_transport = journal.db.total_changes
                projected = 0
                with ThreadPoolExecutor(max_workers=10) as producers:
                    for _ in range(rounds):
                        list(producers.map(lambda thread: emit('item/agentMessage/delta', thread,
                            itemId='agent', delta='generated token '), streams))
                        batch = journal.events(projection.cursor)
                        projection.project_batch(batch)
                        projected += len(batch)
                sessions.persistence.flush()
                observed = sum(any(item.get('id') == 'agent' and
                    item.get('text') == 'generated token ' * rounds
                    for item in sessions.histories[thread]['turns'][-1]['items'])
                    for thread in streams)
                stream_result = {'turns': observed, 'projected_deltas': projected,
                    'state_data_version_changes': observer.execute('PRAGMA data_version').fetchone()[0] - before_state,
                    'transport_changes': journal.db.total_changes - before_transport,
                    'state_write_statements': write_statements['state'],
                    'transport_write_statements': write_statements['transport'],
                    'event_storage': journal.bounds().get('event_storage', 'sqlite')}
        finally:
            store._connection = original_connection
            journal.db.set_trace_callback(None)
            for connection in traced:
                try: connection.set_trace_callback(None)
                except sqlite3.ProgrammingError: pass
        # Capture only timed-operation holds, excluding fixture construction and migrations.
        with store._writer.condition:
            store._writer.holds.clear()
            store._writer.waits.clear()
        initial_timeouts = store.database_metrics()['admission_timeouts']
        samples = {'open': [], 'send_accept': []}
        open_paths = {'managed': 0, 'external': 0}
        mixed_deltas = 0
        publication_targets = []
        receipts = []
        counts = {'finalized': 0, 'indexed': 0}
        stop = threading.Event()
        def foreground(kind):
            index = 0
            composer_turn = None
            while not stop.is_set():
                try:
                    if kind == 'open':
                        elapsed, result = request(f'/api/browser/sessions/fixture-{index % threads:05d}')
                        thread = result['thread']
                        total = thread.get('totalTurns')
                        if not isinstance(total, int) or total < turns:
                            raise ValueError('Seeded transcript total missing')
                        if index % threads != 0 and total != turns:
                            raise ValueError('Unmutated transcript total changed')
                        expected_before = max(0, total - 40)
                        if ([value['id'] for value in thread['turns']] !=
                                [f'turn-{sequence}' for sequence in range(expected_before, total)] or
                                thread.get('earlierTurns') != expected_before or
                                thread.get('beforeSeq') != expected_before or thread.get('view') != 'recent'):
                            raise ValueError('Seeded transcript page mismatch')
                        open_paths['managed' if index % threads % 2 else 'external'] += 1
                    else:
                        key = f'composer-{index}'
                        body = {'message': 'generated composer request', 'requestId': key}
                        if composer_turn: body['expectedTurnId'] = composer_turn
                        elapsed, result = request(f'/api/browser/sessions/{composer}/messages', body)
                        composer_turn = result.get('acceptedMessage', {}).get('turnId') or result.get('turn', {}).get('id') or composer_turn
                        receipts.append(key)
                    samples[kind].append(elapsed)
                except Exception as error:
                    errors.append({'operation': kind, 'type': type(error).__name__})
                index += 1
                stop.wait(.02)
        def finalize():
            index = 0
            while not stop.is_set():
                try:
                    thread = 'fixture-00000'
                    value = turn(turns + index)
                    transcripts.save_turns(thread, {turns + index: value}, metadata={'id': thread, 'cwd': directory}, turn_count=turns + index + 1, activate=True)
                    counts['finalized'] += 1
                    if index == 0:
                        store.register(session_id='new-publication', client='codex', cwd=directory)
                        transcripts.save('new-publication', {'id': 'new-publication', 'cwd': directory, 'turns': [turn(0)]}, activate=True)
                        with store.reading(), store._connection() as db:
                            new_sequence = db.execute("SELECT sequence FROM search_publications WHERE thread_id='new-publication' AND kind='history'").fetchone()[0]
                        publication_targets.append(('new-publication', new_sequence))
                    # Normal source commits must drive the installed asynchronous worker.
                    with store.reading(), store._connection() as db:
                        sequence = db.execute("SELECT sequence FROM search_publications WHERE thread_id=? AND kind='history'", (thread,)).fetchone()[0]
                    publication_targets.append((thread, sequence))
                except Exception as error:
                    errors.append({'operation': 'finalize', 'type': type(error).__name__})
                index += 1
                stop.wait(.1)
        def stream_mixed():
            nonlocal mixed_deltas
            with ThreadPoolExecutor(max_workers=10) as producers:
                while not stop.is_set():
                    try:
                        list(producers.map(lambda thread: emit('item/agentMessage/delta', thread,
                            itemId='agent', delta='generated token '), streams))
                        batch = journal.events(projection.cursor)
                        projection.project_batch(batch)
                        mixed_deltas += len(batch)
                    except Exception as error:
                        errors.append({'operation': 'mixed_stream', 'type': type(error).__name__})
                    stop.wait(.125)
        start = time.monotonic()
        try:
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = [pool.submit(foreground, kind) for kind in samples] + [pool.submit(finalize), pool.submit(stream_mixed)]
                stop.wait(seconds)
                stop.set()
                for future in futures: future.result(timeout=60)
            elapsed = time.monotonic() - start
            with store.reading(), store._connection() as db:
                durable = sum(db.execute('SELECT count(*) FROM browser_submission_receipts WHERE thread_id=? AND request_id=? AND state=?', (composer, key, 'completed')).fetchone()[0] for key in receipts)
            # Drain source outbox through its installed worker, never direct enqueue.
            deadline = time.monotonic() + 10
            applied = {}
            while publication_targets and time.monotonic() < deadline:
                with search.connection(readonly=True) as db:
                    applied = dict(db.execute("SELECT thread_id,sequence FROM applied_publications WHERE kind='history'"))
                if all(applied.get(thread, -1) >= sequence for thread, sequence in publication_targets): break
                time.sleep(.02)
            counts['indexed'] = sum(sequence <= applied.get(thread, -1) for thread, sequence in publication_targets)
            new_publication_applied = 'new-publication' in applied
            metrics = store.database_metrics()
            report = {'schema_version': 1, 'workload': {'threads': threads, 'turns_per_thread': turns, 'streaming_turns': 10, 'delta_rounds': rounds, 'seconds': seconds},
                'seeded': seeded, 'storage_settings': storage_settings, 'setup_seconds': round(setup_seconds, 3), 'elapsed_seconds': round(elapsed, 3),
                'open': {**_distribution(samples['open']), 'count': len(samples['open'])}, 'send_accept': {**_distribution(samples['send_accept']), 'count': len(samples['send_accept'])},
                'request_operations': metrics['requests'], 'writer_hold': metrics['writer_hold'], 'writer_wait': metrics['writer_wait'], 'admission_timeouts': metrics['admission_timeouts'] - initial_timeouts,
                'durable_receipts': durable, 'finalized_writes': counts['finalized'],
                'search': {'available': search is not None, 'indexed': counts['indexed'], 'database_isolated': bool(search and search.path != store.database_path), 'new_transcript_published': new_publication_applied, 'durable_coverage': search.coverage() if search else None},
                'streaming': stream_result, 'mixed_stream_deltas': mixed_deltas, 'open_paths': open_paths, 'projection_lock_hold': _distribution(projection.stream_metrics['lock_holds_ms']), 'error_count': len(errors), 'errors': errors[:20],
                'throughput_per_second': {kind: round(len(values) / elapsed, 2) for kind, values in samples.items()},
                'runtime': {'python': platform.python_version(), 'executable': sys.executable, 'sqlite': sqlite3.sqlite_version, 'platform': platform.platform(), 'revision': revision, 'harness_sha256': harness_sha256},
                'coverage': {'http_recent_open': True, 'durable_composer_receipt': True, 'retained_transport_journal': True, 'live_provider_process': False,
                             'search_unavailable': search is None, 'normal_search_publication_worker': publisher is not None, 'verified_search_active': search.active, 'search_http_query': False, 'search_async_finalized_indexing': search is not None, 'streaming_interval': 'isolated zero-write interval plus simultaneous streaming/HTTP/finalization' }}
            report['budget_failures'] = gates(report)
            return report
        finally:
            stop.set()



def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=float, default=15)
    parser.add_argument('--threads', type=int, default=2000)
    parser.add_argument('--turns', type=int, default=350)
    parser.add_argument('--rounds', type=int, default=10)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.seconds <= 3600 or min(args.threads, args.turns, args.rounds) < 1:
        parser.error('seconds must be 1–3600; threads, turns and rounds must be positive')
    report = measure(args.seconds, args.threads, args.turns, args.rounds)
    encoded = json.dumps(report, indent=2) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded)
    print(encoded, end='')
    return int(args.check and bool(report['budget_failures']))


if __name__ == '__main__':
    raise SystemExit(main())
