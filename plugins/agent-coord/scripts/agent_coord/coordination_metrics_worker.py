"""Incremental, isolated measurement of retained coordination tool payloads.

This module is a child-process entry point, never a provider-event handler.
Its SQLite database is a disposable derived index, not coordination state.
Source databases are opened read-only; only counters and identifiers persist.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import shlex
import sqlite3
import time

SCHEMA_VERSION = 1
INDEX_VERSION = 3
MAX_LINE_BYTES = 2 * 1024 * 1024
MAX_TOKENIZED_BYTES = 256 * 1024
MAX_SOURCES = 10000
MAX_SOURCE_CANDIDATES = 100000
MAX_SNAPSHOT_BYTES = 512 * 1024
RETENTION_DAYS = 180
PERIOD_DAYS = (7, 30, 90)
MCP_OPERATIONS = frozenset({"update_checkpoint", "query_activity", "read_activity_history", "ask_human",
    "schedule_prompt", "move_thread", "send_message", "reply", "other"})
CLI_OPERATIONS = frozenset({"install-cli", "activity", "antigravity", "sidecar", "subscribe", "subscription",
    "job", "register", "status", "set-activity", "checkpoint", "thread", "project", "role", "repository",
    "begin-work", "end-work", "unregister", "list", "conflicts", "send", "ask", "reply", "handoff", "inbox",
    "message", "ack", "delegate", "delegation", "wake-zellij", "ui", "session-control", "help", "other"})
NOTES = [
    "Retained Codex MCP calls and literal standalone Agent Coord CLI commands only; other providers and compound commands are excluded.",
    "Payload sizes are not billed usage or exact model input: programs can filter returned content. Schemas, skills, reasoning, replay and caching are not attributed.",
    "MCP arguments use canonical compact JSON; CLI arguments use the captured standalone command. Outer exec program text and execution wrapper overhead are excluded.",
    "Per-turn values cover observed turns containing measured coordination calls, not all user tasks.",
    "Workspace attribution follows the native index's current working directory; inherited events from other native threads are excluded.",
    "Token totals and per-turn token values sum measured payloads. Compare each token field's measured-call count with total calls: partial totals exclude unknown values, and wholly unavailable token measurements remain unknown.",
    "Lifecycle-field attribution is measured for MCP checkpoints; CLI checkpoint lifecycle attribution is unavailable.",
    "Incomplete source inventories preserve earlier measurements; source discovery gaps are reported separately from resumable transcript work.",
]


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def byte_count(text):
    return len(text.encode("utf-8", errors="replace"))


class TokenCounter:
    def __init__(self, encode=None, version="unavailable"):
        self.encode = encode
        self.version = version

    @property
    def available(self):
        return self.encode is not None

    @property
    def identity(self):
        return "o200k_base:" + self.version if self.available else "utf8_bytes"

    def count(self, text):
        if not self.available or byte_count(text) > MAX_TOKENIZED_BYTES:
            return None
        return len(self.encode(text))

    def metadata(self):
        return {"unit": "tokens" if self.available else "bytes",
                "method": "o200k_base" if self.available else "utf8_bytes",
                "tokenizer_available": self.available, "tokenizer_version": self.version}


def load_tokenizer():
    """Use only a preinstalled tokenizer with a prewarmed local vocabulary.

    tiktoken normally downloads its vocabulary on cache miss. Disable the
    uncached reader while constructing the encoder, so this worker never does.
    """
    try:
        import tiktoken
        from tiktoken import load
        original = load.read_file
        def no_download(_path):
            raise OSError("Tokenizer vocabulary is not cached locally.")
        load.read_file = no_download
        try:
            encoder = tiktoken.get_encoding("o200k_base")
        finally:
            load.read_file = original
        return TokenCounter(lambda text: encoder.encode(text, disallowed_special=()),
                            importlib.metadata.version("tiktoken"))
    except (ImportError, OSError, ValueError, RuntimeError):
        return TokenCounter()


def atomic_json(path, value):
    encoded = canonical(value).encode("utf-8")
    if len(encoded) > MAX_SNAPSHOT_BYTES:
        raise ValueError("Metrics snapshot exceeds its size limit.")
    temporary = path.with_name(path.name + "." + str(os.getpid()) + ".tmp")
    try:
        with temporary.open("wb") as stream:
            stream.write(encoded)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _identifier(value):
    return value if isinstance(value, str) and 0 < len(value) <= 256 else None


def _timestamp(value):
    if not isinstance(value, str):
        raise ValueError("Missing event timestamp.")
    instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    result = instant.timestamp()
    if not math.isfinite(result):
        raise ValueError("Invalid event timestamp.")
    return result


def text_blocks(value):
    if isinstance(value, str):
        return [value], 0
    if isinstance(value, dict):
        value = value.get("content")
    if not isinstance(value, list):
        return [], 1 if value is not None else 0
    texts, nontext = [], 0
    for block in value:
        if isinstance(block, dict) and block.get("type") in {"text", "input_text", "output_text"} and isinstance(block.get("text"), str):
            texts.append(block["text"])
        else:
            nontext += 1
    return texts, nontext


def standalone_command(command):
    """Return (operation, text, excluded); never evaluate shell text."""
    if not isinstance(command, list) or not command or not all(isinstance(word, str) for word in command):
        return None
    shell = len(command) >= 3 and command[-2] in {"-lc", "-c", "-ic"}
    text = command[-1] if shell else shlex.join(command)
    leading = text.lstrip().split(None, 1)[0].strip("\"'") if text.strip() else ""
    if Path(leading).name != "agent-coord":
        return None
    try:
        lexer = shlex.shlex(text, posix=True, punctuation_chars=";&|<>()")
        lexer.whitespace_split = True
        words = list(lexer)
    except ValueError:
        return ("other", text, True)
    if not words or Path(words[0]).name != "agent-coord":
        return None
    if any(word and set(word) <= set(";&|<>()") for word in words) or "$(" in text or "`" in text:
        return ("other", text, True)
    action = words[1] if len(words) > 1 else "help"
    return (action if action in CLI_OPERATIONS else "other", text, False)


def measure_record(record, source_thread, tokenizer, counters, cutoff, now):
    if not isinstance(record, dict) or record.get("type") != "event_msg":
        return None
    payload = record.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "item_completed":
        return None
    item = payload.get("item")
    if not isinstance(item, dict):
        return None
    kind = item.get("type", "")
    if kind == "McpToolCall" and item.get("server") == "agent-coord":
        transport = "mcp"
        operation = item.get("tool") if item.get("tool") in MCP_OPERATIONS else "other"
        arguments = item.get("arguments")
        argument_known = "arguments" in item
        argument_text = canonical(arguments) if argument_known else None
        result = item.get("result")
        texts, nontext = text_blocks(result)
        result_known = (isinstance(result, (str, list)) or
                        isinstance(result, dict) and isinstance(result.get("content"), list)) and not nontext
        lifecycle_text = canonical({key: value for key, value in arguments.items() if key != "context_lifecycle"}) if isinstance(arguments, dict) and "context_lifecycle" in arguments else None
    elif kind == "CommandExecution":
        command = standalone_command(item.get("command"))
        if command is None:
            return None
        if command[2]:
            counters["excluded_compound_cli_events"] += 1
            return None
        transport, operation, argument_text = "cli", command[0], command[1]
        argument_known = True
        output = item.get("aggregated_output")
        texts = [output] if isinstance(output, str) else []
        result_known, nontext, lifecycle_text = isinstance(output, str), 0, None
    else:
        return None
    thread = _identifier(payload.get("thread_id")) or source_thread
    if thread != source_thread:
        counters["excluded_inherited_events"] += 1
        return None
    occurred = _timestamp(record.get("timestamp"))
    if occurred < cutoff:
        return None
    # A growing source can append events after this pass's report cutoff.
    # Retain them now, then let a later snapshot's window include them; skipping
    # them while advancing the file offset would lose live measurements.
    if occurred > now + 86400:
        counters["invalid_future_timestamps"] += 1
        return None
    identity = _identifier(item.get("id"))
    if identity is None:
        counters["missing_call_ids"] += 1
        return None
    argument_tokens = tokenizer.count(argument_text) if argument_known else None
    return_counts = [tokenizer.count(text) for text in texts]
    return_tokens = sum(return_counts) if result_known and all(value is not None for value in return_counts) and tokenizer.available else None
    lifecycle_tokens = 0 if tokenizer.available and argument_known else None
    if transport == "cli" and operation == "checkpoint":
        lifecycle_tokens = None
        counters["unknown_cli_lifecycle"] += 1
    if lifecycle_text is not None:
        reduced = tokenizer.count(lifecycle_text)
        lifecycle_tokens = argument_tokens - reduced if argument_tokens is not None and reduced is not None else None
    if not argument_known:
        counters["missing_argument_payloads"] += 1
    if tokenizer.available and ((argument_known and argument_tokens is None) or (result_known and return_tokens is None)):
        counters["unmeasured_token_payloads"] += 1
    if nontext:
        counters["nontext_results"] += 1
    if not result_known:
        counters["missing_or_nontext_results"] += 1
    return (transport + ":" + identity, transport, operation, thread,
            _identifier(payload.get("turn_id")), occurred,
            argument_tokens, return_tokens, lifecycle_tokens, byte_count(argument_text) if argument_known else None,
            sum(byte_count(text) for text in texts) if result_known else None)


def discover_sources(codex_home, cwd, cutoff, *, deadline=None, clock=time.monotonic, sleep=time.sleep):
    home = codex_home.resolve()
    indexes = sorted(home.glob("state_*.sqlite"), key=lambda path: int(path.stem.split("_")[-1]) if path.stem.split("_")[-1].isdigit() else -1, reverse=True)
    counters = Counter()
    workspace = Path(cwd).expanduser().resolve() if cwd else None
    for index in indexes[:4]:
        if not index.resolve().is_relative_to(home) or index.is_symlink():
            continue
        try:
            connection = sqlite3.connect(index.as_uri() + "?mode=ro", uri=True, timeout=.2)
            sources, seen, directories = [], set(), {}
            active_since = clock()
            def inventory_budget():
                nonlocal active_since
                current = clock()
                if deadline is not None and current >= deadline:
                    return 1
                if current - active_since >= .03:
                    sleep(min(.06, max(0, deadline - current)) if deadline is not None else .06)
                    active_since = clock()
                return int(deadline is not None and clock() >= deadline)
            try:
                connection.set_progress_handler(inventory_budget, 1000)
                # Native cwd may use a symlink alias unknown to this process.
                # Resolve before applying the selected-source cap, streaming a
                # bounded inventory rather than filtering raw strings in SQL.
                cursor = connection.execute("SELECT id,rollout_path,cwd FROM threads WHERE updated_at>=? ORDER BY updated_at DESC LIMIT ?",
                                            (int(cutoff), MAX_SOURCE_CANDIDATES + 1))
                for candidate_index, (thread_id, raw_path, raw_cwd) in enumerate(cursor):
                    if candidate_index >= MAX_SOURCE_CANDIDATES:
                        counters.update(inventory_incomplete=1, source_candidate_limit_exceeded=1)
                        break
                    if inventory_budget():
                        counters.update(inventory_incomplete=1, source_discovery_budget_exceeded=1)
                        break
                    if (not _identifier(thread_id) or not isinstance(raw_path, str)
                            or not isinstance(raw_cwd, str) or len(raw_cwd) > 4096):
                        counters["invalid_source_records"] += 1
                        continue
                    try:
                        if raw_cwd not in directories:
                            directories[raw_cwd] = Path(raw_cwd).expanduser().resolve()
                        directory = directories[raw_cwd]
                        if workspace and directory != workspace and not directory.is_relative_to(workspace):
                            continue
                        path = Path(raw_path)
                        resolved = path.resolve()
                        if not path.is_absolute() or path.is_symlink() or not resolved.is_relative_to(home):
                            counters["excluded_source_paths"] += 1
                            continue
                    except (OSError, ValueError, RuntimeError):
                        counters["invalid_source_records"] += 1
                        continue
                    source = (str(resolved), thread_id)
                    if source in seen:
                        continue
                    if len(sources) >= MAX_SOURCES:
                        counters.update(inventory_incomplete=1, source_limit_exceeded=1)
                        break
                    seen.add(source)
                    sources.append(source)
            except sqlite3.OperationalError as error:
                if "interrupted" not in str(error).lower():
                    raise
                counters.update(inventory_incomplete=1, source_discovery_budget_exceeded=1)
            finally:
                connection.close()
        except sqlite3.Error:
            continue
        return sources, counters
    return None, counters


def initialize(connection, identity, scope):
    schema = """
        PRAGMA journal_mode=WAL;
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sources(
            path TEXT PRIMARY KEY,thread_id TEXT NOT NULL,device INTEGER NOT NULL,inode INTEGER NOT NULL,
            size INTEGER NOT NULL,mtime INTEGER NOT NULL,offset INTEGER NOT NULL,prefix_length INTEGER NOT NULL,
            prefix_hash TEXT NOT NULL,discarding INTEGER NOT NULL,counters_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS calls(
            call_id TEXT PRIMARY KEY,transport TEXT NOT NULL,operation TEXT NOT NULL,thread_id TEXT NOT NULL,
            turn_id TEXT,occurred_at REAL NOT NULL,argument_tokens INTEGER,return_tokens INTEGER,lifecycle_tokens INTEGER,
            argument_bytes INTEGER,return_bytes INTEGER);
        CREATE TABLE IF NOT EXISTS observations(
            source TEXT NOT NULL REFERENCES sources(path) ON DELETE CASCADE,
            call_id TEXT NOT NULL REFERENCES calls(call_id) ON DELETE CASCADE,
            PRIMARY KEY(source,call_id));
        CREATE INDEX IF NOT EXISTS calls_time ON calls(occurred_at);
        CREATE INDEX IF NOT EXISTS calls_operation_time ON calls(transport,operation,occurred_at);
        CREATE INDEX IF NOT EXISTS observations_call ON observations(call_id);
    """
    connection.executescript(schema)
    # Version 3 distinguishes absent argument capture from explicit JSON null.
    # The private index is disposable; recreate its old nonnullable schema.
    if any(row[1] == "argument_bytes" and row[3] for row in connection.execute("PRAGMA table_info(calls)")):
        connection.executescript("DROP TABLE observations; DROP TABLE calls; DROP TABLE sources;" + schema)
    expected = canonical({"schema": SCHEMA_VERSION, "index": INDEX_VERSION,
                          "measurement": identity, "scope": scope})
    previous = connection.execute("SELECT value FROM metadata WHERE key='identity'").fetchone()
    if previous is not None and previous[0] != expected:
        with connection:
            connection.execute("DELETE FROM observations")
            connection.execute("DELETE FROM calls")
            connection.execute("DELETE FROM sources")
    with connection:
        connection.execute("INSERT OR REPLACE INTO metadata VALUES ('identity',?)", (expected,))


def _source_reset(connection, path):
    # Restrict orphan checks to this source's IDs. Scanning all retained calls
    # once for every retired source makes source turnover quadratic.
    connection.execute("""DELETE FROM calls WHERE call_id IN
        (SELECT call_id FROM observations WHERE source=?) AND NOT EXISTS
        (SELECT 1 FROM observations WHERE observations.call_id=calls.call_id AND source<>?)""", (path, path))
    connection.execute("DELETE FROM sources WHERE path=?", (path,))


def _save_batch(connection, source, entries):
    with connection:
        connection.execute("""INSERT INTO sources VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(path) DO UPDATE SET thread_id=excluded.thread_id,device=excluded.device,
            inode=excluded.inode,size=excluded.size,mtime=excluded.mtime,offset=excluded.offset,
            prefix_length=excluded.prefix_length,prefix_hash=excluded.prefix_hash,
            discarding=excluded.discarding,counters_json=excluded.counters_json""", source)
        connection.executemany("INSERT OR IGNORE INTO calls VALUES (?,?,?,?,?,?,?,?,?,?,?)", entries)
        connection.executemany("INSERT OR IGNORE INTO observations VALUES (?,?)", ((source[0], entry[0]) for entry in entries))


def _sum_column(row, name, available=True, *, partial=False):
    known = row[name + "_known"]
    if not available or (row["calls"] and not known) or (not partial and known != row["calls"]):
        return None
    return row[name + "_sum"] or 0


def _counts(connection, start, end, tokenizer, operation=None):
    condition = "occurred_at>=? AND occurred_at<?"
    parameters = [start, end]
    if operation:
        condition += " AND transport=? AND operation=?"
        parameters.extend(operation)
    fields = ["argument_tokens", "return_tokens", "lifecycle_tokens", "argument_bytes", "return_bytes"]
    sql = "SELECT COUNT(*) AS calls,COUNT(DISTINCT thread_id) AS threads,COUNT(DISTINCT CASE WHEN turn_id IS NOT NULL THEN thread_id||'/'||turn_id END) AS turns," + ",".join("SUM(" + field + ") AS " + field + "_sum,COUNT(" + field + ") AS " + field + "_known" for field in fields) + " FROM calls WHERE " + condition
    row = connection.execute(sql, parameters).fetchone()
    result = {key: row[key] for key in ("calls", "threads", "turns")}
    result.update({field: _sum_column(row, field, tokenizer.available if field.endswith("tokens") else True,
                                      partial=field.endswith("tokens")) for field in fields})
    result.update({field.removesuffix("s") + "_calls": row[field + "_known"]
                   for field in ("argument_tokens", "return_tokens", "lifecycle_tokens")})
    result["calls_per_turn"] = round(result["calls"] / result["turns"], 3) if result["turns"] else None
    total_tokens = result["argument_tokens"] + result["return_tokens"] if result["argument_tokens"] is not None and result["return_tokens"] is not None else None
    result["tokens_per_turn"] = round(total_tokens / result["turns"], 2) if total_tokens is not None and result["turns"] else None
    return result


def make_snapshot(connection, tokenizer, now, coverage):
    periods = {}
    for days in PERIOD_DAYS:
        start, previous = now - days * 86400, now - days * 2 * 86400
        current = _counts(connection, start, now, tokenizer)
        tools = []
        operations = connection.execute("SELECT DISTINCT transport,operation FROM calls WHERE occurred_at>=? AND occurred_at<? ORDER BY transport,operation", (start, now)).fetchall()
        for transport, operation in operations:
            counts = _counts(connection, start, now, tokenizer, (transport, operation))
            counts.update({"transport": transport, "operation": operation,
                           "previous": _counts(connection, previous, start, tokenizer, (transport, operation))})
            measured = counts["argument_token_calls"] if tokenizer.available else 0
            counts["p50_argument_tokens"] = counts["p95_argument_tokens"] = None
            if measured:
                ordered = "SELECT argument_tokens FROM calls WHERE occurred_at>=? AND occurred_at<? AND transport=? AND operation=? AND argument_tokens IS NOT NULL ORDER BY argument_tokens LIMIT ? OFFSET ?"
                values = [row[0] for row in connection.execute(ordered, (start, now, transport, operation,
                          2 if measured % 2 == 0 else 1, (measured - 1) // 2))]
                counts["p50_argument_tokens"] = sum(values) / len(values)
                counts["p95_argument_tokens"] = connection.execute(ordered, (start, now, transport, operation,
                                                1, math.ceil(measured * .95) - 1)).fetchone()[0]
            counts["measured_argument_calls"] = measured
            tools.append(counts)
        daily = []
        day = datetime.fromtimestamp(start, timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        while day < now:
            counts = _counts(connection, max(start, day), min(now, day + 86400), tokenizer)
            daily.append({"date": datetime.fromtimestamp(day, timezone.utc).date().isoformat(), **counts})
            day += 86400
        periods[str(days)] = {"period": {"start": start, "end": now, "days": days}, "current": current,
                             "previous": _counts(connection, previous, start, tokenizer), "daily": daily, "tools": tools}
    return {"schema_version": SCHEMA_VERSION, "generated_at": now, "measurement": tokenizer.metadata(),
            "coverage": coverage, "periods": periods}


def bounded_snapshot(connection, tokenizer, now, coverage, *, deadline, clock, sleep):
    """Bound SQL aggregation work as well as source reading, preserving snapshots.

    SQLite invokes the handler during scans and sorts, so a large derived index
    cannot hold this child at full CPU indefinitely after scanning has yielded.
    No database mutations are performed while this callback is installed.
    """
    active_since = clock()
    def check_budget():
        nonlocal active_since
        current = clock()
        if current >= deadline:
            return 1
        if current - active_since >= .03:
            sleep(min(.06, deadline - current))
            active_since = clock()
        return int(clock() >= deadline)
    if check_budget():
        return None
    connection.set_progress_handler(check_budget, 1000)
    try:
        return make_snapshot(connection, tokenizer, now, coverage)
    except sqlite3.OperationalError as error:
        if "interrupted" in str(error).lower():
            return None
        raise
    finally:
        connection.set_progress_handler(None, 0)


def aggregation_failures(connection, failed):
    """Bound retries that cannot advance an unchanged derived history."""
    with connection:
        if not failed:
            connection.execute("DELETE FROM metadata WHERE key='aggregation_failure'")
            return 0
        revision = tuple(connection.execute("SELECT COUNT(*),MAX(rowid) FROM calls").fetchone())
        previous = connection.execute("SELECT value FROM metadata WHERE key='aggregation_failure'").fetchone()
        saved = json.loads(previous[0]) if previous else {}
        failures = saved.get("failures", 0) + 1 if saved.get("revision") == list(revision) else 1
        connection.execute("INSERT OR REPLACE INTO metadata VALUES ('aggregation_failure',?)",
                           (canonical({"revision": revision, "failures": failures}),))
        return failures


def run_pass(state_dir, codex_home, *, cwd=None, max_seconds=30, max_bytes=64 * 1024 * 1024,
             tokenizer=None, now=None, clock=time.monotonic, sleep=time.sleep):
    state_dir, codex_home = Path(state_dir).expanduser().resolve(), Path(codex_home).expanduser().resolve()
    if max_seconds <= 0 or max_bytes <= 0:
        raise ValueError("Worker budgets must be positive.")
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    tokenizer = load_tokenizer() if tokenizer is None else tokenizer
    now = time.time() if now is None else now
    started = clock()
    deadline = started + max_seconds
    scan_deadline = started + max_seconds * .75
    scope = str(Path(cwd).expanduser().resolve()) if cwd else "All indexed workspaces"
    progress_path, snapshot_path = state_dir / "progress.json", state_dir / "snapshot.json"
    progress = {"state": "running", "message": "Measuring retained coordination calls.", "updated_at": now,
                "running": True, "progress": {"files_done": 0, "files_total": 0, "bytes_processed": 0, "has_more": False}}
    atomic_json(progress_path, progress)
    sources, inventory_counts = discover_sources(codex_home, cwd, now - RETENTION_DAYS * 86400,
                                                deadline=started + max_seconds * .2, clock=clock, sleep=sleep)
    if sources is None:
        # The manager uses separate directories for each scope, but never retain
        # an older workspace's snapshot if a caller reuses a state directory.
        try:
            if snapshot_path.stat().st_size <= MAX_SNAPSHOT_BYTES:
                previous_scope = json.loads(snapshot_path.read_text()).get("coverage", {}).get("scope")
                if previous_scope != scope:
                    snapshot_path.unlink(missing_ok=True)
        except (OSError, ValueError, AttributeError):
            pass
        progress.update(state="unavailable", running=False, message="A readable native Codex index is unavailable.")
        atomic_json(progress_path, progress)
        return progress
    progress["progress"]["files_total"] = len(sources)
    connection = sqlite3.connect(state_dir / "metrics.sqlite3", timeout=1)
    connection.row_factory = sqlite3.Row
    try:
        initialize(connection, tokenizer.identity, scope)
        with connection:
            connection.execute("DELETE FROM calls WHERE occurred_at<?", (now - RETENTION_DAYS * 86400,))
            if not inventory_counts.get("inventory_incomplete"):
                active = {path for path, _ in sources}
                for row in connection.execute("SELECT path FROM sources").fetchall():
                    if row[0] not in active:
                        _source_reset(connection, row[0])
        # Finish already started files before opening newer files. This prevents
        # continuously active conversations from starving historical recovery.
        saved = {row["path"]: row for row in connection.execute("SELECT * FROM sources")}
        sources.sort(key=lambda item: 0 if item[0] in saved and saved[item[0]]["offset"] < saved[item[0]]["size"] else 1)
        budget_bytes = 0
        files_done, tail_files, transient_errors = 0, 0, Counter()
        has_more = False
        active_since = clock()
        for source_index, (path_text, thread_id) in enumerate(sources):
            if clock() >= scan_deadline or budget_bytes >= max_bytes:
                has_more = source_index < len(sources)
                break
            path = Path(path_text)
            try:
                if path.is_symlink() or not path.resolve().is_relative_to(codex_home):
                    transient_errors["excluded_source_paths"] += 1
                    continue
                stat = path.stat()
                previous = saved.get(path_text)
                counters = Counter(json.loads(previous["counters_json"])) if previous else Counter()
                if previous and previous["thread_id"] == thread_id and previous["device"] == stat.st_dev and previous["inode"] == stat.st_ino and previous["size"] == stat.st_size and previous["mtime"] == stat.st_mtime_ns and previous["offset"] == stat.st_size:
                    files_done += 1
                    continue
                with path.open("rb") as source:
                    prefix_length = min(previous["prefix_length"] if previous and previous["prefix_length"] else 4096, stat.st_size)
                    prefix = source.read(prefix_length)
                    prefix_hash = hashlib.sha256(prefix).hexdigest()
                    changed = previous and (previous["thread_id"] != thread_id or previous["device"] != stat.st_dev or previous["inode"] != stat.st_ino or stat.st_size < previous["offset"] or previous["prefix_hash"] != prefix_hash or (stat.st_size == previous["size"] and stat.st_mtime_ns != previous["mtime"]))
                    if changed:
                        with connection:
                            _source_reset(connection, path_text)
                        previous, counters = None, Counter()
                        source.seek(0)
                        prefix_length = min(4096, stat.st_size)
                        prefix_hash = hashlib.sha256(source.read(prefix_length)).hexdigest()
                    offset = previous["offset"] if previous else 0
                    discarding = bool(previous["discarding"]) if previous else False
                    source.seek(offset)
                    entries, last_flush = [], offset
                    pending_tail = False
                    def flush():
                        nonlocal entries, last_flush
                        state = (path_text, thread_id, stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns,
                                 offset, prefix_length, prefix_hash, int(discarding), canonical(counters))
                        _save_batch(connection, state, entries)
                        entries, last_flush = [], offset
                    while source.tell() < stat.st_size:
                        remaining = max_bytes - budget_bytes
                        if remaining <= 0 or clock() >= scan_deadline:
                            has_more = True
                            break
                        limit = min(MAX_LINE_BYTES + 1, remaining, stat.st_size - source.tell())
                        line_start = source.tell()
                        line = source.readline(limit)
                        budget_bytes += len(line)
                        if not line:
                            break
                        if discarding:
                            offset = source.tell()
                            if line.endswith(b"\n"):
                                discarding = False
                        elif not line.endswith(b"\n"):
                            if len(line) > MAX_LINE_BYTES or (len(line) == max_bytes and source.tell() < stat.st_size):
                                counters["oversized_records"] += 1
                                discarding = True
                                offset = source.tell()
                            elif source.tell() >= stat.st_size:
                                pending_tail = True
                                break
                            else:
                                source.seek(line_start)
                                has_more = True
                                break
                        else:
                            offset = source.tell()
                            if b"agent-coord" in line:
                                try:
                                    record = json.loads(line)
                                    entry = measure_record(record, thread_id, tokenizer, counters, now - RETENTION_DAYS * 86400, now)
                                    if entry is not None:
                                        entries.append(entry)
                                except (ValueError, TypeError, KeyError, RecursionError, OverflowError):
                                    counters["malformed_records"] += 1
                        if len(entries) >= 100 or offset - last_flush >= 1024 * 1024:
                            flush()
                        if clock() - active_since >= .03:
                            flush()
                            sleep(min(.06, max(0, scan_deadline - clock())))
                            active_since = clock()
                    flush()
                    if offset == stat.st_size:
                        files_done += 1
                    elif pending_tail:
                        tail_files += 1
                    else:
                        has_more = True
            except (OSError, ValueError):
                transient_errors["unreadable_sources"] += 1
            progress["progress"].update(files_done=files_done, bytes_processed=budget_bytes, has_more=has_more)
            if source_index % 25 == 0:
                atomic_json(progress_path, progress)
        coverage = Counter(inventory_counts)
        coverage.update(transient_errors)
        for row in connection.execute("SELECT counters_json FROM sources"):
            coverage.update(json.loads(row[0]))
        coverage.update({"files_done": files_done, "files_total": len(sources), "pending_tail_files": tail_files})
        coverage_document = {"providers": ["codex"], "scope": scope, "notes": NOTES, **dict(coverage)}
        snapshot = bounded_snapshot(connection, tokenizer, now, coverage_document,
                                    deadline=deadline, clock=clock, sleep=sleep)
        aggregation_pending = snapshot is None
        repeated_aggregation_failure = False
        if snapshot is not None:
            atomic_json(snapshot_path, snapshot)
            aggregation_failures(connection, False)
        else:
            failures = aggregation_failures(connection, True)
            repeated_aggregation_failure = failures >= 2 and not has_more
            has_more = not repeated_aggregation_failure
        incomplete = aggregation_pending or any(coverage.get(key) for key in ("inventory_incomplete", "invalid_source_records", "malformed_records", "oversized_records", "unreadable_sources", "excluded_source_paths", "source_limit_exceeded", "missing_call_ids", "missing_argument_payloads", "unmeasured_token_payloads", "missing_or_nontext_results", "unknown_cli_lifecycle"))
        state = "partial" if has_more or incomplete or tail_files else "ready"
        message = "More retained history remains to be measured." if has_more else "Measurements updated."
        if aggregation_pending:
            message = ("Report aggregation exceeded its budget; the previous snapshot is retained."
                       if repeated_aggregation_failure else "Measurements indexed; the report update will continue in the next pass.")
        if not tokenizer.available:
            message += " Tokenizer unavailable; showing byte measurements."
        elif incomplete:
            message += " Some records have incomplete measurement coverage."
        progress.update(state=state, running=False, message=message, updated_at=time.time())
        progress["progress"].update(files_done=files_done, files_total=len(sources), bytes_processed=budget_bytes,
                                     has_more=has_more, aggregation_pending=aggregation_pending,
                                     aggregation_retry_limited=repeated_aggregation_failure,
                                     elapsed_seconds=round(clock() - started, 3))
        atomic_json(progress_path, progress)
        return progress
    finally:
        connection.close()


@contextmanager
def worker_lock(state_dir):
    import fcntl
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state_dir / "worker.lock").open("a+") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--codex-home", required=True, type=Path)
    parser.add_argument("--cwd")
    parser.add_argument("--max-seconds", type=float, default=30)
    parser.add_argument("--max-bytes", type=int, default=64 * 1024 * 1024)
    arguments = parser.parse_args(argv)
    if not 0 < arguments.max_seconds <= 300 or not 0 < arguments.max_bytes <= 1024 * 1024 * 1024:
        parser.error("Use a positive time budget up to 300 seconds and byte budget up to 1 GiB.")
    os.umask(0o077)
    try:
        os.nice(10)
    except (AttributeError, OSError):
        pass
    state = arguments.state_dir.expanduser().resolve()
    with worker_lock(state) as acquired:
        if not acquired:
            return 0
        try:
            run_pass(state, arguments.codex_home, cwd=arguments.cwd,
                     max_seconds=arguments.max_seconds, max_bytes=arguments.max_bytes)
            return 0
        except (OSError, sqlite3.Error, ValueError, TypeError, RuntimeError):
            atomic_json(state / "progress.json", {"state": "error", "running": False,
                "message": "Coordination measurements could not be updated. The previous snapshot is retained.",
                "updated_at": time.time(), "progress": {"has_more": False}})
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
