"""Muse token accounting from MSP and explicit durable logs, without model calls.

Root cumulative counters exclude native children. Raw child counters have
provider-dependent cache conventions, so they are retained as observations,
never silently added to the root's counted-once totals.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path

from .claude_usage import _count, _is_fork, _lines, _sum

log = logging.getLogger(__name__)


def normalize_cumulative(value):
    if not isinstance(value, dict):
        return {}
    fields = {"promptTokens": "input_tokens", "outputTokens": "output_tokens", "totalTokens": "total_tokens",
              "cacheReadTokens": "cached_input_tokens", "cacheWriteTokens": "cache_write_input_tokens"}
    result = {target: number for source, target in fields.items()
              if (number := _count(value.get(source))) is not None}
    if "total_tokens" not in result and "input_tokens" in result and "output_tokens" in result:
        result["total_tokens"] = result["input_tokens"] + result["output_tokens"]
    return result


def normalize_completion(value):
    if not isinstance(value, dict):
        return {}
    result = normalize_cumulative(value)
    raw = value.get("usage")
    if isinstance(raw, dict):
        for source, target in {"outputTokens": "output_tokens", "cacheReadTokens": "cached_input_tokens",
                               "cacheWriteTokens": "cache_write_input_tokens", "reasoningTokens": "reasoning_output_tokens"}.items():
            if (number := _count(raw.get(source))) is not None:
                result[target] = number
    # inputTokens/cachedTokens deliberately omitted: cache inclusion differs by
    # provider. MSP promptTokens already counts the prompt exactly once.
    if "total_tokens" not in result and "input_tokens" in result and "output_tokens" in result:
        result["total_tokens"] = result["input_tokens"] + result["output_tokens"]
    return result


def _identity(params):
    source = params.get("sourceRange")
    if isinstance(source, dict) and source:
        return "source:" + hashlib.sha256(json.dumps(source, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    cursor = params.get("viewCursor")
    return "cursor:" + cursor if isinstance(cursor, str) and cursor else None


class MuseUsage:
    def __init__(self, store, on_event=None):
        self.store, self.on_event = store, on_event
        self.inherited = set()
        self.verified_roots = set()

    def _changed(self, thread_id):
        if self.on_event:
            self.on_event({"method": "thread/usage/updated", "params": {
                "threadId": thread_id, "usage": self.store.usage.summary(thread_id)}})

    def capture(self, thread_id, native_id, method, params):
        try:
            if self._capture(thread_id, native_id, method, params):
                self._changed(thread_id)
        except Exception:
            log.debug("Could not persist Muse usage", exc_info=True)

    def _capture(self, thread_id, native_id, method, params):
        if method not in {"session/tokenUsage", "session/contextUsage", "item/started", "item/updated", "item/completed"}:
            return False
        if not isinstance(params, dict) or params.get("sessionId") != native_id:
            return False
        if (thread_id, native_id) in self.inherited:
            return False
        if _is_fork(self.store, thread_id):
            self.store.usage.record_unavailable(thread_id, provider="muse", native_id=native_id,
                note="Muse fork usage has no verified baseline; inherited consumption is excluded.")
            return True
        usage = self.store.usage
        if method in {"item/started", "item/updated", "item/completed"}:
            item = params.get("item")
            if not isinstance(item, dict) or item.get("kind") != "subagent" or not isinstance(item.get("usage"), dict):
                return False
            # Preserve transitive child observations separately, without
            # claiming counted-once totals that the wire does not supply.
            item_id, revision = item.get("itemId"), _count(item.get("revision"))
            if not isinstance(item_id, str) or not item_id or revision is None:
                return False
            identity = hashlib.sha256(json.dumps([item_id, revision]).encode()).hexdigest()
            counters = {key: number for key, value in item["usage"].items()
                        if (number := _count(value)) is not None}
            if not counters:
                return False
            usage.record_delta(thread_id, provider="muse", native_id=native_id,
                source_key="muse:child-observations", event_id=identity, totals={},
                scope="including_native_children", coverage="partial",
                raw={"transitive": 1, "usage": counters})
            return True
        if method == "session/contextUsage":
            used, limit = _count(params.get("usedTokens")), _count(params.get("windowTokens"))
            if used is None:
                return False
            usage.record_context(thread_id, provider="muse", native_id=native_id, used=used, limit=limit)
            return True
        if method != "session/tokenUsage":
            return False
        identity = _identity(params)
        if not identity:
            return False  # Neither summation nor replay identity can be trusted.
        model = params.get("modelId")
        model = model if isinstance(model, str) and model else None
        raw = {key: number for key, value in (params.get("usage") or {}).items()
               if (number := _count(value)) is not None} if isinstance(params.get("usage"), dict) else None
        cumulative = normalize_cumulative(params.get("cumulative"))
        completion = normalize_completion(params)
        if completion:
            usage.record_delta(thread_id, provider="muse", native_id=native_id,
                source_key="muse:completions", event_id=identity, totals=completion, model=model,
                coverage="partial", raw=raw)
        if cumulative:
            usage.record_snapshot(thread_id, provider="muse", native_id=native_id,
                source_key="muse:cumulative", totals=cumulative,
                coverage="complete" if (thread_id, native_id) in self.verified_roots else "partial", raw=raw)
        return bool(cumulative or completion)

    def snapshot(self, thread_id, native_id, result):
        """Fold a read/resume snapshot only if the host actually served totals."""
        try:
            session = result.get("session") or {}
            if session.get("sessionId") != native_id:
                return
            if session.get("forkedFrom") or session.get("sideFrom") or _is_fork(self.store, thread_id):
                self.inherited.add((thread_id, native_id))
                self.store.usage.record_unavailable(thread_id, provider="muse", native_id=native_id,
                    note="Muse inherited history has no verified usage baseline; cumulative attribution is unavailable.")
                self._changed(thread_id)
                return
            # A matching native start/read/resume envelope proves the root
            # identity/lineage. Its counted-once cumulative is a complete direct
            # stream, even though child usage and optional counters are separate.
            if not session.get("parentSessionId") and session.get("kind") != "subagent":
                self.verified_roots.add((thread_id, native_id))
            history = result.get("history") or {}
            snapshot = history.get("snapshot") or {}
            state = snapshot.get("state") or {}
            totals = normalize_cumulative(state.get("tokenUsage"))
            if totals:
                self.store.usage.record_snapshot(thread_id, provider="muse", native_id=native_id,
                    source_key="muse:cumulative", totals=totals,
                    coverage="complete" if (thread_id, native_id) in self.verified_roots else "partial",
                    raw=None)
                self._changed(thread_id)
            context = state.get("contextUsage")
            if isinstance(context, dict):
                self.capture(thread_id, native_id, "session/contextUsage", {"sessionId": native_id, **context})
        except Exception:
            log.debug("Could not persist Muse snapshot usage", exc_info=True)


def _raw_completion(entry):
    payload = entry.get("payload")
    if not isinstance(payload, dict) or payload.get("kind") != "run":
        return None
    event = payload.get("event")
    if not isinstance(event, dict) or event.get("kind") != "model_completed":
        return None
    raw = event.get("usage")
    if not isinstance(raw, dict):
        return None
    # The observed Meta runtime includes cache reads inside input_tokens. This
    # is not a safe normalization for arbitrary providers/log versions. Retain
    # only convention-independent output/reasoning counters in the fallback.
    counted = {target: number for source, target in {
        "output_tokens": "output_tokens", "reasoning_tokens": "reasoning_output_tokens"}.items()
        if (number := _count(raw.get(source))) is not None}
    return event.get("model"), counted


def backfill_usage(store, thread_id, transcript_path=None):
    """Explicit MSP capture/native log import. Never spawn or attach a provider.

    MSP snapshots/events are preferred. Internal raw logs lack counted-once
    prompt fields; their fallback therefore reports output/reasoning only.
    """
    native_id = thread_id
    try:
        if _is_fork(store, thread_id):
            raise ValueError("Muse inherited history requires a verified baseline.")
        with store._connection() as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE name = 'muse_sessions'").fetchone()
            row = db.execute("SELECT native_id FROM muse_sessions WHERE thread_id = ?", (thread_id,)).fetchone() if exists else None
        native_id = row[0] if row else thread_id
        if transcript_path is None:
            # Never start a host to discover a path or subscribe to a session.
            root = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "muse/sessions"
            import uuid
            name = str(uuid.UUID(native_id))
            paths = list(root.glob(f"*/*/*/{name}/session.jsonl"))
            if len(paths) != 1:
                raise ValueError("Muse native transcript is missing or ambiguous.")
            transcript_path = paths[0]
        # Refuse inherited source totals before any writes; do not leave half an
        # imported parent's spend behind when lineage appears later in the log.
        for entry in _lines(transcript_path):
            result = entry.get("result", entry)
            session = result.get("session") if isinstance(result, dict) else None
            if (isinstance(session, dict) and (session.get("forkedFrom") or session.get("sideFrom"))
                    or entry.get("payload_type") in {"session.fork.created", "session.side_chat.created"}):
                raise ValueError("Muse inherited history requires a verified baseline.")
        collector, found, raw_messages = MuseUsage(store), False, {}
        for entry in _lines(transcript_path):
            # A saved read response uses the exact native snapshot envelope.
            result = entry.get("result", entry)
            if isinstance(result, dict) and isinstance(result.get("session"), dict):
                if result["session"].get("forkedFrom") or result["session"].get("sideFrom"):
                    raise ValueError("Muse inherited totals require a verified baseline.")
                collector.snapshot(thread_id, native_id, result)
                found = found or bool(normalize_cumulative((result.get("history") or {}).get("snapshot", {}).get("state", {}).get("tokenUsage")))
            params = entry.get("params")
            if isinstance(params, dict):
                found = collector._capture(thread_id, native_id, entry.get("method"), params) or found
            stream = entry.get("stream")
            if not isinstance(stream, dict) or stream.get("kind") != "session" or stream.get("id") != native_id:
                continue
            value = _raw_completion(entry)
            if value and isinstance(entry.get("id"), str):
                raw_messages[entry["id"]] = value
        if raw_messages:
            buckets = {}
            for model, totals in raw_messages.values():
                if totals:
                    buckets.setdefault(model if isinstance(model, str) else None, []).append(totals)
            models = [{"model": model, "totals": _sum(values)} for model, values in buckets.items()]
            if None in buckets:
                models = [item for item in models if item["model"] is not None] + [
                    {"model": None, "totals": _sum(values for _, values in raw_messages.values())}]
            store.usage.replace_snapshots(thread_id, provider="muse", native_id=native_id,
                source_key="muse:raw-log", models=models, coverage="partial", raw={"prompt_unavailable": 1})
            found = bool(buckets) or found
        if not found:
            store.usage.record_unavailable(thread_id, provider="muse", native_id=native_id,
                note="No attributable Muse token records were found; native children are excluded.")
    except Exception:
        log.debug("Could not backfill Muse usage", exc_info=True)
        store.usage.record_unavailable(thread_id, provider="muse", native_id=native_id,
            note="Muse usage backfill could not read attributable native records; inherited history requires a baseline.")
    return store.usage.summary(thread_id)
