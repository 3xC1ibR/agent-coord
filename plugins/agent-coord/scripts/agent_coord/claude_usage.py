"""Best-effort Claude telemetry; stream results and saved messages have different scopes.

Live assistant output counts are placeholders. Only result.usage is a direct
turn delta; result.modelUsage is an alternative, child-inclusive cumulative view.
No conversation content is passed to the usage store.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path

log = logging.getLogger(__name__)


def _count(value):
    return value if type(value) is int and 0 <= value <= 2**63 - 1 else None


def normalize_usage(usage, *, camel=False):
    if not isinstance(usage, dict):
        return {}
    names = {
        "input_tokens": "inputTokens" if camel else "input_tokens",
        "output_tokens": "outputTokens" if camel else "output_tokens",
        "cached_input_tokens": "cacheReadInputTokens" if camel else "cache_read_input_tokens",
        "cache_write_input_tokens": "cacheCreationInputTokens" if camel else "cache_creation_input_tokens",
        "reasoning_output_tokens": "thinkingTokens" if camel else None,
    }
    result = {key: value for key, name in names.items()
              if name and (value := _count(usage.get(name))) is not None}
    if not camel:
        details = usage.get("output_tokens_details")
        if isinstance(details, dict):
            thinking = _count(details.get("thinking_tokens"))
            if thinking is not None:
                result["reasoning_output_tokens"] = thinking
    # Claude reports cache reads/writes beside uncached input. Missing splits
    # are unknown, not zero; do not claim a counted-once prompt without them.
    if all(key in result for key in ("input_tokens", "cached_input_tokens", "cache_write_input_tokens")):
        result["input_tokens"] += result["cached_input_tokens"] + result["cache_write_input_tokens"]
    else:
        result.pop("input_tokens", None)
    if "input_tokens" in result and "output_tokens" in result:
        result["total_tokens"] = result["input_tokens"] + result["output_tokens"]
    return result


def _lines(path):
    path = Path(path).expanduser()
    if not path.is_absolute() or path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError("A bounded absolute native transcript is required.")
    with path.open("rb") as stream:
        while line := stream.readline(2 * 1024 * 1024 + 1):
            if len(line) > 2 * 1024 * 1024:
                raise ValueError("Native transcript line exceeds the recovery bound.")
            try:
                value = json.loads(line)
            except (ValueError, UnicodeError):
                continue  # A live JSONL file may have a partial tail.
            if isinstance(value, dict):
                yield value


def _fork_path(store, thread_id):
    with store._connection() as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE name = 'claude_sessions'").fetchone()
        row = db.execute("SELECT fork_path FROM claude_sessions WHERE thread_id = ?", (thread_id,)).fetchone() if exists else None
    return row[0] if row else None


def _is_fork(store, thread_id):
    with store._connection() as db:
        row = db.execute("SELECT forked_from_thread_id FROM work_threads WHERE thread_id = ?", (thread_id,)).fetchone()
    return bool(row and row[0])


def native_id(store, thread_id):
    with store._connection() as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(claude_sessions)")}
        row = db.execute("SELECT native_id FROM claude_sessions WHERE thread_id=?", (thread_id,)).fetchone() if "native_id" in columns else None
    return row[0] if row and row[0] else thread_id


def _own_epoch(store, thread_id, identity):
    position = store.usage.epoch_position(thread_id, provider="claude", native_id=identity)
    return position is not None and position > 0


def _sum(values):
    values = list(values)
    return {key: sum(item[key] for item in values) for key in values[0]
            if all(key in item for item in values)} if values else {}


def _subtract(totals, baseline):
    # Missing baseline counters are unknown, never an assumed zero.
    return {key: value - baseline[key] for key, value in totals.items()
            if key in baseline and value >= baseline[key]}


def _cost_models(entries):
    latest = None
    for entry in entries:
        if entry.get("type") == "cost-state" and isinstance(entry.get("modelUsage"), dict):
            latest = entry["modelUsage"]
    return latest


class ClaudeUsage:
    def __init__(self, store, on_event=None):
        self.store, self.on_event = store, on_event

    def _changed(self, thread_id):
        if self.on_event:
            self.on_event({"method": "thread/usage/updated", "params": {
                "threadId": thread_id, "usage": self.store.usage.summary(thread_id)}})

    def capture(self, thread_id, message):
        """Telemetry errors must never fail execution or drop a display frame."""
        try:
            if self._reset(thread_id, message):
                self._changed(thread_id)
                return
            # App identity remains stable when a native conversation resets.
            # Never let a nested agent overwrite the owning native identity.
            if (isinstance(message, dict) and not message.get("parent_tool_use_id")
                    and (message.get("type") == "result" or message.get("type") == "system" and message.get("subtype") == "init")):
                identity = message.get("session_id")
                if isinstance(identity, str) and identity and len(identity) <= 500:
                    with self.store._connection() as db:
                        db.execute("UPDATE claude_sessions SET native_id=? WHERE thread_id=?", (identity, thread_id))
            if self._capture(thread_id, message):
                self._changed(thread_id)
        except Exception:
            log.debug("Could not persist Claude usage", exc_info=True)

    def _reset(self, thread_id, message):
        if not isinstance(message, dict) or message.get("type") != "conversation_reset" or message.get("parent_tool_use_id"):
            return False
        identity = message.get("new_conversation_id")
        if not isinstance(identity, str):
            return False
        # Native wire schema requires a UUID. Ordinary init/result identity
        # corrections are aliases, never proof of disjoint consumption.
        try:
            identity = str(uuid.UUID(identity))
        except ValueError:
            return False
        previous = native_id(self.store, thread_id)
        if identity == previous:
            return True
        self.store.usage.start_epoch(thread_id, provider="claude", native_id=identity, previous_native_id=previous)
        with self.store._connection() as db:
            db.execute("UPDATE claude_sessions SET native_id=? WHERE thread_id=?", (identity, thread_id))
        return True

    def _capture(self, thread_id, message):
        if not isinstance(message, dict) or message.get("type") != "result" or message.get("parent_tool_use_id"):
            return False
        identity = message.get("session_id") or native_id(self.store, thread_id)
        if not isinstance(identity, str) or not identity:
            return False
        changed = False
        totals = normalize_usage(message.get("usage"))
        # Results without a durable identity cannot safely be summed on replay.
        event_id = message.get("uuid")
        if totals and isinstance(event_id, str) and event_id:
            # A crash can synthesize a zeroed result. Keep earlier observations.
            if message.get("subtype") != "error_during_execution" or any(totals.values()):
                self.store.usage.record_delta(thread_id, provider="claude", native_id=identity,
                    source_key="claude:results", event_id=event_id, totals=totals,
                    scope="direct", coverage="partial")
                changed = True
        models = message.get("modelUsage")
        if isinstance(models, dict) and models:
            baseline = None
            own_epoch = _own_epoch(self.store, thread_id, identity)
            fork_path = None if own_epoch else _fork_path(self.store, thread_id)
            if fork_path:
                baseline = _cost_models(_lines(fork_path))
                if baseline is None:
                    self.store.usage.record_unavailable(thread_id, provider="claude", native_id=identity,
                        note="Fork inherited model totals have no saved baseline; only observed direct turns are attributed.")
                    return True
            elif _is_fork(self.store, thread_id) and not own_epoch:
                self.store.usage.record_unavailable(thread_id, provider="claude", native_id=identity,
                    note="Fork cumulative totals have no verified baseline; only observed direct turns are attributed.")
                return True
            for model, usage in models.items():
                if not isinstance(model, str) or not model:
                    continue
                counted = normalize_usage(usage, camel=True)
                if not counted or message.get("subtype") == "error_during_execution" and not any(counted.values()):
                    continue
                base = normalize_usage(baseline.get(model, {}), camel=True) if baseline is not None else None
                # A model absent from the frozen parent was first used by child.
                if baseline is not None and model not in baseline:
                    base = {key: 0 for key in counted}
                if base is not None:
                    counted = _subtract(counted, base)
                    self.store.usage.set_baseline(thread_id, provider="claude", native_id=identity, totals={})
                if not counted:
                    continue
                self.store.usage.record_snapshot(thread_id, provider="claude", native_id=identity,
                    source_key="claude:model-usage", totals=counted, model=model,
                    scope="including_native_children", coverage="partial")
                changed = True
        return changed


def backfill_usage(store, thread_id, transcript_path=None):
    """Read explicit/native JSONL only. Never execute Claude or start a query."""
    identity = thread_id
    try:
        identity = native_id(store, thread_id)
        own_epoch = _own_epoch(store, thread_id, identity)
        fork_path = None if own_epoch else _fork_path(store, thread_id)
        inherited = list(_lines(fork_path)) if fork_path else []
        inherited_ids = {e.get("message", {}).get("id") for e in inherited
                         if isinstance(e.get("message"), dict)} - {None}
        if transcript_path is None:
            root = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / "projects"
            # Native IDs are filenames, not user-controlled glob expressions.
            name = str(uuid.UUID(identity))
            paths = list(root.glob(f"*/{name}.jsonl"))
            if len(paths) != 1:
                raise ValueError("Claude native transcript is missing or ambiguous.")
            transcript_path = paths[0]
        messages, costs = {}, []
        unknown_fork = _is_fork(store, thread_id) and not fork_path and not own_epoch
        for entry in _lines(transcript_path):
            entry_identity = entry.get("sessionId") or entry.get("session_id")
            # Fork copies retain the parent's identity. Never count that history.
            if entry_identity != identity:
                continue
            if entry.get("type") == "cost-state":
                costs.append(entry)
            message = entry.get("message")
            if entry.get("type") != "assistant" or entry.get("isSidechain") or not isinstance(message, dict):
                continue
            mid = message.get("id")
            if not isinstance(mid, str) or not mid or mid in inherited_ids or unknown_fork:
                continue
            if isinstance(message.get("usage"), dict):
                messages[mid] = message
        buckets = {}
        for message in messages.values():
            model = message.get("model")
            model = model if isinstance(model, str) and model else None
            values = normalize_usage(message["usage"])
            if values:
                buckets.setdefault(model, []).append(values)
        models = [{"model": model, "totals": _sum(values)} for model, values in buckets.items()]
        # model=None is the aggregate in core storage, not an unknown-model
        # bucket. Add an aggregate over *all* messages if any model is unknown.
        if None in buckets:
            models = [item for item in models if item["model"] is not None] + [
                {"model": None, "totals": _sum(normalize_usage(m["usage"]) for m in messages.values())}]
        if fork_path:
            store.usage.set_baseline(thread_id, provider="claude", native_id=identity, totals={})
        if not unknown_fork:
            store.usage.replace_snapshots(thread_id, provider="claude", native_id=identity,
                source_key="claude:transcript", models=models, coverage="partial",
                raw={"deduplicated_messages": len(messages)})
        latest_models = _cost_models(costs)
        if latest_models:
            # Reuse exactly the same model/baseline policy as live result frames.
            ClaudeUsage(store)._capture(thread_id, {"type": "result", "session_id": identity,
                "modelUsage": latest_models})
        if not buckets and not latest_models:
            store.usage.record_unavailable(thread_id, provider="claude", native_id=identity,
                note="No attributable Claude token records were found; inherited fork history is excluded.")
    except Exception:
        # Paths and exception strings can include sensitive data. Keep notes fixed.
        log.debug("Could not backfill Claude usage", exc_info=True)
        store.usage.record_unavailable(thread_id, provider="claude", native_id=identity,
            note="Claude usage backfill could not read an attributable native transcript.")
    return store.usage.summary(thread_id)
