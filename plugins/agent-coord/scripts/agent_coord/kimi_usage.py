"""Versioned, read-only accounting of Kimi's native request journals.

Wire 1.5 ``usage.record`` entries are request deltas, including session-scoped
compaction requests. Context measurements and subagent summaries are not usage
records. Replacing a canonical snapshot, rather than adding an import to the old
total, makes replay, process restarts and journal rewrites non-additive.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from pathlib import Path

from .kimi_setup import kimi_home


WIRE_VERSIONS = frozenset({"1.5"})
SOURCE_KEY = "kimi-wire-v1.5"
_IMPORT_LOCK = threading.RLock()
_MAX_LINE = 8 * 1024 * 1024
_MAX_IMPORT_BYTES = 64 * 1024 * 1024
_MAX_AGENT_ENTRIES = 64
_COUNTERS = {
    "inputOther": "uncached_input_tokens",
    "output": "output_tokens",
    "inputCacheRead": "cached_input_tokens",
    "inputCacheCreation": "cache_write_input_tokens",
}


def _sum_totals(entries):
    """A counter remains unknown if any contributing request omits it."""
    if not entries:
        return {}
    keys = set.intersection(*(set(entry) for entry in entries))
    return {key: sum(entry[key] for entry in entries) for key in sorted(keys)}


def _totals(usage):
    values = {target: usage[source] for source, target in _COUNTERS.items()
              if isinstance(usage.get(source), int) and not isinstance(usage[source], bool)
              and usage[source] >= 0}
    # The engine substitutes emptyUsage() when the provider omitted counters.
    # An all-zero record cannot establish measured consumption.
    if len(values) == 4 and not any(values.values()):
        return {}
    inputs = ("uncached_input_tokens", "cached_input_tokens", "cache_write_input_tokens")
    if all(key in values for key in inputs):
        values["input_tokens"] = sum(values[key] for key in inputs)
        if "output_tokens" in values:
            values["total_tokens"] = values["input_tokens"] + values["output_tokens"]
    values.pop("uncached_input_tokens", None)
    return values


def read_journal(path, agent_id, *, forked=False, max_bytes=_MAX_IMPORT_BYTES):
    """Read one supported canonical journal; return only counts/provenance.

    Byte offsets are provenance within this incarnation, not cross-rewrite event
    IDs. An incomplete last JSON line is deferred until its newline is written.
    Nothing from prompts, responses, tools, credentials or raw usage is retained.
    """
    path = Path(path)
    models, records, notes = {}, 0, set()
    boundary, offset, version, marker = 0, 0, None, False
    digest = hashlib.sha256()
    with path.open("rb") as journal:
        stat = os.fstat(journal.fileno())
        if stat.st_size > max_bytes:
            raise ValueError("Kimi journal exceeds the bounded import size")
        first = True
        while True:
            line = journal.readline(_MAX_LINE + 1)
            if not line:
                break
            if journal.tell() > max_bytes:
                raise ValueError("Kimi journal exceeds the bounded import size")
            if len(line) > _MAX_LINE:
                raise ValueError("Kimi journal has an oversized record")
            if not line.endswith(b"\n"):
                notes.add("incomplete_tail")
                break
            digest.update(line)
            offset = journal.tell()
            try:
                record = json.loads(line)
            except (ValueError, UnicodeError):
                notes.add("invalid_record")
                if first:
                    raise ValueError("Kimi journal metadata is missing") from None
                continue
            if not isinstance(record, dict):
                notes.add("invalid_record")
                continue
            if first:
                first = False
                version = record.get("protocol_version")
                if record.get("type") != "metadata" or version not in WIRE_VERSIONS:
                    raise ValueError("Unsupported Kimi wire version")
                continue
            kind = record.get("type")
            if kind == "metadata":
                raise ValueError("Kimi journal contains a second metadata header")
            if kind == "forked":
                if record.get("agentId") != agent_id:
                    raise ValueError("Kimi fork marker has a different agent identity")
                # Forks copy their source's journal. Only newly incurred work
                # after the last inherited fork boundary belongs to this agent.
                models, records, boundary, marker = {}, 0, offset, True
                notes.add("inherited_prefix_excluded")
                continue
            if kind != "usage.record":
                continue
            usage, model = record.get("usage"), record.get("model")
            if (record.get("agentId") != agent_id
                    or record.get("usageScope") not in {"turn", "session"}
                    or not isinstance(usage, dict) or not isinstance(model, str)
                    or not model or len(model) > 500):
                raise ValueError("Unsupported Kimi usage record shape")
            totals = _totals(usage)
            if len(totals) < 5:
                notes.add("missing_provider_counters")
            models.setdefault(model, []).append(totals)
            if len(models) > 200:
                raise ValueError("Kimi journal has too many model buckets")
            records += 1
        if first:
            raise ValueError("Kimi journal metadata is incomplete")
        current = path.stat()
        if ((current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)
                != (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)):
            raise ValueError("Kimi journal changed during import")
    if forked and not marker:
        raise ValueError("Kimi forked journal has no usage boundary")
    buckets = [{"model": model, "totals": _sum_totals(entries)}
               for model, entries in sorted(models.items())]
    return {"totals": _sum_totals([entry["totals"] for entry in buckets]),
            "models": buckets, "records": records, "notes": sorted(notes),
            "provenance": {"agent_id": agent_id, "wire_version": version,
                           "device": stat.st_dev, "inode": stat.st_ino,
                           "size_bytes": stat.st_size,
                           "start_offset": boundary, "end_offset": offset,
                           "prefix_sha256": digest.hexdigest()}}


def _session_directory(native_id, cwd, transcript_path):
    if not native_id or not re.fullmatch(r"[A-Za-z0-9_-]+", native_id):
        raise ValueError("Invalid Kimi native session identity")
    if transcript_path is not None:
        target = Path(transcript_path).expanduser().resolve()
        if target.is_dir():
            candidates = [target]
        elif target.name == "wire.jsonl" and target.parent.parent.name == "agents":
            candidates = [target.parent.parent.parent]
        else:
            raise ValueError("Kimi backfill path must be a native session or agent wire journal")
    else:
        candidates = list((kimi_home() / "sessions").glob("*/" + native_id))
    matches = []
    for directory in candidates:
        try:
            state_path = directory / "state.json"
            if state_path.stat().st_size > _MAX_LINE:
                continue
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (isinstance(state, dict) and state.get("id") == native_id
                and state.get("cwd") == cwd):
            matches.append((directory, state))
    if len(matches) != 1:
        raise ValueError("Mapped Kimi native session journal is missing or ambiguous")
    return matches[0]


def _child_journals(directory):
    """Bound directory work as well as bytes, including missing child logs."""
    paths, truncated = [], False
    with os.scandir(directory / "agents") as entries:
        for index, entry in enumerate(entries):
            if index >= _MAX_AGENT_ENTRIES:
                truncated = True
                break
            if entry.name != "main" and entry.is_dir(follow_symlinks=False):
                paths.append(Path(entry.path) / "wire.jsonl")
    return sorted(paths), truncated


def backfill_usage(store, thread_id, transcript_path=None):
    """Replace canonical native totals at lifecycle boundaries or explicit import.

    Callers use lifecycle capture, explicit CLI backfill, or one-time recovery
    when opening an old thread without telemetry. Never rescan on overview polls,
    recurring detail reads or streamed content. All results remain partial:
    missing provider counters or historical data cannot be reconstructed as bills.
    """
    with _IMPORT_LOCK:
        with store._connection() as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='kimi_sessions'").fetchone()
            row = db.execute("SELECT native_id, cwd FROM kimi_sessions WHERE thread_id = ?", (thread_id,)).fetchone() if exists else None
        if not row:
            store.usage.record_unavailable(thread_id, provider="kimi", note="No saved Kimi native session mapping")
            return store.usage.summary(thread_id, provider="kimi")
        native_id, cwd = row["native_id"], row["cwd"]
        try:
            directory, state = _session_directory(native_id, cwd, transcript_path)
            main_path = directory / "agents/main/wire.jsonl"
            if not main_path.resolve().is_relative_to(directory.resolve()):
                raise ValueError("Kimi journal is outside its mapped native session")
            main = read_journal(main_path, "main", forked=bool(state.get("forkedFrom")), max_bytes=_MAX_IMPORT_BYTES)
        except (OSError, ValueError) as exc:
            store.usage.record_unavailable(thread_id, provider="kimi", native_id=native_id, note=str(exc))
            return store.usage.summary(thread_id, provider="kimi")
        children, missing = [], []
        remaining = _MAX_IMPORT_BYTES - main["provenance"]["size_bytes"]
        try:
            paths, truncated = _child_journals(directory)
        except OSError:
            paths, truncated = [], True
        for path in paths:
            try:
                if not path.resolve().is_relative_to(directory.resolve()):
                    raise ValueError("Kimi child journal is outside its native session")
                child = read_journal(path, path.parent.name, forked=bool(state.get("forkedFrom")), max_bytes=remaining)
                children.append(child)
                remaining -= child["provenance"]["size_bytes"]
            except (OSError, ValueError):
                missing.append(path.parent.name)
        # These snapshots have already excluded inherited fork history.
        store.usage.set_baseline(thread_id, provider="kimi", native_id=native_id, totals={})
        for scope, journals in (("direct", [main]), ("including_native_children", [main, *children])):
            by_model = {}
            for journal in journals:
                for bucket in journal["models"]:
                    by_model.setdefault(bucket["model"], []).append(bucket["totals"])
            buckets = [{"model": model, "totals": _sum_totals(entries)}
                       for model, entries in sorted(by_model.items())]
            raw = {"records": sum(j["records"] for j in journals), "journal_count": len(journals),
                   "missing_children": len(missing) if scope != "direct" else 0,
                   "agent_enumeration_truncated": int(truncated) if scope != "direct" else 0}
            for note in {note for j in journals for note in j["notes"]}:
                raw[note] = sum(note in j["notes"] for j in journals)
            # Shared telemetry accepts only numeric objects. Keep bounded,
            # content-free journal provenance, not prompts or arbitrary text.
            for journal in journals[:30]:
                provenance = journal["provenance"]
                key = "journal_" + hashlib.sha256(provenance["agent_id"].encode()).hexdigest()[:16]
                raw[key] = {k: provenance[k] for k in ("device", "inode", "size_bytes", "start_offset", "end_offset")}
                raw[key]["wire_major"], raw[key]["wire_minor"] = 1, 5
                digest = provenance["prefix_sha256"]
                for index, start in enumerate(range(0, len(digest), 15)):
                    raw[key]["digest_" + str(index)] = int(digest[start:start + 15], 16)
            store.usage.replace_snapshots(thread_id, provider="kimi", native_id=native_id,
                source_key=SOURCE_KEY, models=buckets or [{"model": None, "totals": {}}],
                scope=scope, coverage="partial", raw=raw)
        if missing or truncated:
            store.usage.record_unavailable(thread_id, provider="kimi", native_id=native_id,
                note="Kimi usage is partial: some native-child journals were missing, unsupported, or exceeded the import bounds")
        return store.usage.summary(thread_id, provider="kimi")
