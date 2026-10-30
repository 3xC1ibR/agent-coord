"""Durable token attribution. Telemetry contains counters, never transcript text.

Snapshots replace a cumulative stream; deltas have durable event identities.
Different sources and scopes are alternatives, not additive measurements.
"""
from __future__ import annotations

import importlib
import json
import math
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .store import CoordinationError

COUNTERS = ("input_tokens", "output_tokens", "total_tokens", "cached_input_tokens",
            "cache_write_input_tokens", "reasoning_output_tokens")
SCOPES = {"direct", "including_native_children"}
MAX_TRANSCRIPT_BYTES = 64 * 1024 * 1024
MAX_LINE_BYTES = 2 * 1024 * 1024


def _text(value, name):
    if not isinstance(value, str) or not value.strip() or len(value) > 500:
        raise CoordinationError(f"Usage {name} must be nonempty text of at most 500 characters.")
    return value.strip()


def _count(value):
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise CoordinationError("Usage counters must be nonnegative integers.")
    return value


def _totals(value):
    if not isinstance(value, dict) or set(value) - set(COUNTERS):
        raise CoordinationError("Usage totals must contain only normalized token counters.")
    return {key: _count(count) for key, count in value.items() if count is not None}


def _raw(value):
    # Provider counter names may differ; arbitrary strings/content are forbidden.
    if value is None:
        return None
    if not isinstance(value, dict) or len(value) > 40:
        raise CoordinationError("Raw usage must be a small counter object.")
    result = {}
    for key, item in value.items():
        _text(key, "counter name")
        if isinstance(item, dict):
            if any(isinstance(v, dict) for v in item.values()):
                raise CoordinationError("Raw usage nesting is too deep.")
            result[key] = _raw(item)
        elif item is not None:
            result[key] = _count(item)
    return result


def _time(value, clock):
    if value is None:
        return clock()
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError as exc:
            raise CoordinationError("Invalid usage observation time.") from exc
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise CoordinationError("Invalid usage observation time.")
    return float(value)


def _iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value is not None else None


def _sum(values):
    values = list(values)
    if not values:
        return {}
    # Missing means unknown, including when one model/native bucket omits it.
    return {key: sum(value[key] for value in values) for key in COUNTERS
            if all(key in value for value in values)}


def codex_totals(raw):
    if not isinstance(raw, dict):
        raise CoordinationError("Codex usage is missing counters.")
    names = {"inputTokens": "input_tokens", "outputTokens": "output_tokens",
             "totalTokens": "total_tokens", "cachedInputTokens": "cached_input_tokens",
             "cacheWriteInputTokens": "cache_write_input_tokens",
             "reasoningOutputTokens": "reasoning_output_tokens"}
    return _totals({names.get(key, key): value for key, value in raw.items()
                    if key in names or key in COUNTERS})


class ThreadUsageStore:
    def __init__(self, store, *, initialize=True):
        self.store = store
        if not initialize:
            return
        with store._connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS thread_usage_samples (
                    thread_id TEXT NOT NULL REFERENCES work_threads(thread_id) ON DELETE CASCADE,
                    provider TEXT NOT NULL, native_id TEXT NOT NULL,
                    source_key TEXT NOT NULL, scope TEXT NOT NULL,
                    kind TEXT NOT NULL, event_id TEXT NOT NULL, model TEXT NOT NULL,
                    totals_json TEXT NOT NULL, raw_json TEXT, coverage TEXT NOT NULL,
                    observed_at REAL NOT NULL,
                    PRIMARY KEY(thread_id,provider,native_id,source_key,scope,kind,event_id,model)
                );
                CREATE TABLE IF NOT EXISTS thread_usage_baselines (
                    thread_id TEXT NOT NULL REFERENCES work_threads(thread_id) ON DELETE CASCADE,
                    provider TEXT NOT NULL, native_id TEXT NOT NULL,
                    totals_json TEXT, PRIMARY KEY(thread_id,provider,native_id)
                );
                CREATE TABLE IF NOT EXISTS thread_usage_context (
                    thread_id TEXT NOT NULL REFERENCES work_threads(thread_id) ON DELETE CASCADE,
                    provider TEXT NOT NULL, native_id TEXT NOT NULL,
                    used_tokens INTEGER, limit_tokens INTEGER, observed_at REAL NOT NULL,
                    PRIMARY KEY(thread_id,provider,native_id)
                );
                CREATE TABLE IF NOT EXISTS thread_usage_notes (
                    thread_id TEXT NOT NULL REFERENCES work_threads(thread_id) ON DELETE CASCADE,
                    provider TEXT NOT NULL, native_id TEXT NOT NULL,
                    note TEXT NOT NULL, observed_at REAL NOT NULL,
                    PRIMARY KEY(thread_id,provider,native_id)
                );
                CREATE TABLE IF NOT EXISTS thread_usage_epochs (
                    thread_id TEXT NOT NULL REFERENCES work_threads(thread_id) ON DELETE CASCADE,
                    provider TEXT NOT NULL, native_id TEXT NOT NULL, position INTEGER NOT NULL,
                    PRIMARY KEY(thread_id,provider,native_id), UNIQUE(thread_id,provider,position)
                );
                CREATE INDEX IF NOT EXISTS usage_samples_thread ON thread_usage_samples(thread_id,provider);
            """)

    def _identity(self, thread_id, provider, native_id):
        return (_text(thread_id, "thread ID"), _text(provider, "provider"),
                _text(native_id, "native ID"))

    def set_baseline(self, thread_id, *, provider, native_id, totals=None):
        """Freeze a fork boundary. Unknown boundaries cannot expose inherited totals."""
        identity = self._identity(thread_id, provider, native_id)
        encoded = json.dumps(_totals(totals), sort_keys=True) if totals is not None else None
        with self.store._connection() as db:
            db.execute("""INSERT INTO thread_usage_baselines VALUES (?,?,?,?)
                          ON CONFLICT(thread_id,provider,native_id) DO UPDATE SET totals_json=excluded.totals_json
                          WHERE thread_usage_baselines.totals_json IS NULL""", (*identity, encoded))

    def start_epoch(self, thread_id, *, provider, native_id, previous_native_id=None):
        """Declare a verified disjoint native reset, never an alias or child.

        Only adapters with a confirmed top-level reset may call this. Same-native
        resume/replay is a no-op. A branching or stale transition is rejected.
        """
        identity = self._identity(thread_id, provider, native_id)
        if previous_native_id is not None:
            previous_native_id = _text(previous_native_id, "previous native ID")
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT 1 FROM thread_usage_epochs WHERE thread_id=? AND provider=? AND native_id=?", identity).fetchone()
            if existing:
                return False
            last = db.execute("""SELECT native_id,position FROM thread_usage_epochs
                WHERE thread_id=? AND provider=? ORDER BY position DESC LIMIT 1""", identity[:2]).fetchone()
            if previous_native_id and previous_native_id != native_id:
                if last is None:
                    db.execute("INSERT INTO thread_usage_epochs VALUES (?,?,?,0)", (*identity[:2], previous_native_id))
                    position = 1
                elif last["native_id"] == previous_native_id:
                    position = last["position"] + 1
                else:
                    raise CoordinationError("Native usage reset does not follow the current accounting period.")
            elif last is None:
                position = 0
            else:
                raise CoordinationError("A new usage period requires its previous native ID.")
            db.execute("INSERT INTO thread_usage_epochs VALUES (?,?,?,?)", (*identity, position))
            # A new reset period has no inherited history, even when the app
            # thread originally began as a fork. Its root period keeps its own
            # separately established fork baseline.
            if position > 0:
                db.execute("""INSERT INTO thread_usage_baselines VALUES (?,?,?,'{}')
                    ON CONFLICT(thread_id,provider,native_id) DO UPDATE SET totals_json='{}'""", identity)
        return True

    def epoch_position(self, thread_id, *, provider, native_id):
        """Return a durable period index; >0 proves a confirmed native reset."""
        identity = self._identity(thread_id, provider, native_id)
        with self.store._connection() as db:
            row = db.execute("SELECT position FROM thread_usage_epochs WHERE thread_id=? AND provider=? AND native_id=?", identity).fetchone()
        return row["position"] if row else None

    def record_snapshot(self, thread_id, *, provider, native_id, source_key, totals,
                        model=None, scope="direct", coverage="partial", raw=None,
                        baseline=None, observed_at=None):
        return self._record(thread_id, provider=provider, native_id=native_id, source_key=source_key,
                            totals=totals, model=model, scope=scope, coverage=coverage, raw=raw,
                            baseline=baseline, observed_at=observed_at, kind="snapshot", event_id="")

    def record_delta(self, thread_id, *, provider, native_id, event_id, totals,
                     source_key="deltas", model=None, scope="direct", coverage="partial",
                     raw=None, observed_at=None):
        return self._record(thread_id, provider=provider, native_id=native_id, source_key=source_key,
                            totals=totals, model=model, scope=scope, coverage=coverage, raw=raw,
                            baseline=None, observed_at=observed_at, kind="delta",
                            event_id=_text(event_id, "event ID"))

    def replace_snapshots(self, thread_id, *, provider, native_id, source_key, models,
                          scope="direct", coverage="partial", raw=None, observed_at=None):
        """Atomically replace a fully parsed canonical source, including removed models.

        Unlike record_snapshot, this explicitly allows canonical log corrections.
        Callers must mark truncated/partial scans as partial, not complete.
        """
        identity = self._identity(thread_id, provider, native_id)
        source_key = _text(source_key, "source key")
        scope = scope.replace("-", "_") if isinstance(scope, str) else scope
        if scope not in SCOPES or coverage not in {"complete", "partial"}:
            raise CoordinationError("Invalid usage scope or coverage.")
        if not isinstance(models, list) or len(models) > 200:
            raise CoordinationError("Usage model buckets must be a bounded list.")
        samples = []
        seen = set()
        for item in models:
            if not isinstance(item, dict):
                raise CoordinationError("Invalid usage model bucket.")
            model = _text(item["model"], "model") if item.get("model") is not None else ""
            if model in seen:
                raise CoordinationError("Duplicate usage model bucket.")
            seen.add(model)
            samples.append((model, json.dumps(_totals(item.get("totals")), sort_keys=True)))
        if not samples:
            # Keep a timestamped empty source so stale pre-rewrite scans cannot
            # resurrect removed buckets after a crash or reconnect.
            samples.append(("", "{}"))
        raw = json.dumps(_raw(raw)) if raw is not None else None
        observed = _time(observed_at, self.store.clock)
        key = (*identity, source_key, scope)
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            latest = db.execute("""SELECT MAX(observed_at) FROM thread_usage_samples WHERE thread_id=?
                AND provider=? AND native_id=? AND source_key=? AND scope=? AND kind='snapshot'""", key).fetchone()[0]
            if latest is not None and latest > observed:
                return False
            db.execute("""DELETE FROM thread_usage_samples WHERE thread_id=? AND provider=?
                AND native_id=? AND source_key=? AND scope=? AND kind='snapshot'""", key)
            db.executemany("INSERT INTO thread_usage_samples VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                           [(*key, "snapshot", "", model, counts, raw, coverage, observed)
                            for model, counts in samples])
            db.execute("DELETE FROM thread_usage_notes WHERE thread_id=? AND provider=? AND native_id=?", identity)
        return True

    def _record(self, thread_id, *, provider, native_id, source_key, totals, model, scope,
                coverage, raw, baseline, observed_at, kind, event_id):
        identity = self._identity(thread_id, provider, native_id)
        source_key = _text(source_key, "source key")
        model = _text(model, "model") if model is not None else ""
        scope = scope.replace("-", "_") if isinstance(scope, str) else scope
        if scope not in SCOPES or coverage not in {"complete", "partial"}:
            raise CoordinationError("Invalid usage scope or coverage.")
        totals, raw = _totals(totals), _raw(raw)
        baseline = _totals(baseline) if baseline is not None else None
        observed = _time(observed_at, self.store.clock)
        key = (*identity, source_key, scope, kind, event_id, model)
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if baseline is not None:
                db.execute("""INSERT INTO thread_usage_baselines VALUES (?,?,?,?)
                              ON CONFLICT(thread_id,provider,native_id) DO UPDATE SET totals_json=excluded.totals_json
                              WHERE thread_usage_baselines.totals_json IS NULL""", (*identity, json.dumps(baseline)))
            if kind == "delta":
                previous = db.execute("""SELECT * FROM thread_usage_samples WHERE thread_id=? AND provider=?
                    AND native_id=? AND source_key=? AND scope=? AND kind=? AND event_id=?""", key[:-1]).fetchone()
            else:
                previous = db.execute("""SELECT * FROM thread_usage_samples WHERE thread_id=? AND provider=?
                    AND native_id=? AND source_key=? AND scope=? AND kind=? AND event_id=? AND model=?""", key).fetchone()
            if previous:
                before = json.loads(previous["totals_json"])
                if kind == "delta":
                    # A stable event is a single charge, even after crash/replay.
                    return False
                if observed < previous["observed_at"]:
                    if all(value <= before.get(counter, -1) for counter, value in totals.items()):
                        return False
                    # A queued live event can be newer native consumption than
                    # a transcript backfill with a later observation clock.
                    # Cumulative knowledge must not be lost to wall-clock order.
                    observed = previous["observed_at"]
                    if previous["coverage"] == "partial":
                        coverage = "partial"
                if any(totals.get(k, v) < v for k, v in before.items()):
                    # A provider reset/truncated log is not a new billing epoch.
                    # Preserve known consumption and report incomplete coverage.
                    coverage = "partial"
                totals = {k: max(v, totals.get(k, v)) for k, v in before.items()} | {
                    k: v for k, v in totals.items() if k not in before}
            db.execute("""INSERT OR REPLACE INTO thread_usage_samples VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (*key, json.dumps(totals, sort_keys=True), json.dumps(raw) if raw is not None else None,
                        coverage, observed))
            db.execute("DELETE FROM thread_usage_notes WHERE thread_id=? AND provider=? AND native_id=?", identity)
        return True

    def record_context(self, thread_id, *, provider, native_id, used, limit, observed_at=None):
        identity = self._identity(thread_id, provider, native_id)
        used = _count(used) if used is not None else None
        limit = _count(limit) if limit is not None else None
        observed = _time(observed_at, self.store.clock)
        with self.store._connection() as db:
            db.execute("""INSERT INTO thread_usage_context VALUES (?,?,?,?,?,?)
                          ON CONFLICT(thread_id,provider,native_id) DO UPDATE SET
                          used_tokens=excluded.used_tokens,limit_tokens=excluded.limit_tokens,observed_at=excluded.observed_at
                          WHERE excluded.observed_at >= thread_usage_context.observed_at""", (*identity, used, limit, observed))

    def record_unavailable(self, thread_id, *, provider, native_id=None, note, observed_at=None):
        identity = self._identity(thread_id, provider, native_id or thread_id)
        note = _text(note, "note")
        observed = _time(observed_at, self.store.clock)
        with self.store._connection() as db:
            db.execute("INSERT OR REPLACE INTO thread_usage_notes VALUES (?,?,?,?,?)", (*identity, note, observed))

    @staticmethod
    def _stream(rows, baseline):
        models = {}
        if rows[0]["kind"] == "snapshot":
            aggregate = next((json.loads(row["totals_json"]) for row in rows if not row["model"]), None)
            for row in rows:
                if row["model"]:
                    models[row["model"]] = json.loads(row["totals_json"])
            totals = aggregate if aggregate is not None else _sum(models.values())
        else:
            totals = _sum(json.loads(row["totals_json"]) for row in rows)
            for row in rows:
                key = row["model"] or None
                models.setdefault(key, []).append(json.loads(row["totals_json"]))
            models = {key: _sum(values) for key, values in models.items()}
        complete = all(row["coverage"] == "complete" for row in rows)
        note = None
        if baseline is not False and baseline != {} and rows[0]["kind"] == "snapshot":
            if baseline is None:
                return {}, {}, False, "Fork baseline is unavailable; inherited consumption is excluded."
            if rows[0]["kind"] == "snapshot":
                adjusted = {}
                for key, value in totals.items():
                    if key in baseline and value >= baseline[key]:
                        adjusted[key] = value - baseline[key]
                    elif key in baseline:
                        complete = False
                totals = adjusted
                # An aggregate fork baseline cannot safely assign parent costs to models.
                models = {}
                if not totals:
                    note = "Fork counters do not cover the saved baseline."
                    complete = False
        return totals, models, complete, note

    def summary(self, thread_id, provider=None):
        with self.store._connection() as db:
            session = db.execute("SELECT client FROM sessions WHERE session_id=?", (thread_id,)).fetchone()
            if not session:
                raise CoordinationError("Work thread not found.")
            provider = provider or session["client"]
            rows = db.execute("SELECT * FROM thread_usage_samples WHERE thread_id=? AND provider=?", (thread_id, provider)).fetchall()
            baselines = {r["native_id"]: json.loads(r["totals_json"]) if r["totals_json"] is not None else None
                         for r in db.execute("SELECT * FROM thread_usage_baselines WHERE thread_id=? AND provider=?", (thread_id, provider))}
            context = db.execute("SELECT * FROM thread_usage_context WHERE thread_id=? AND provider=? ORDER BY observed_at DESC LIMIT 1", (thread_id, provider)).fetchone()
            notes = db.execute("SELECT * FROM thread_usage_notes WHERE thread_id=? AND provider=? ORDER BY observed_at DESC", (thread_id, provider)).fetchall()
            fork = db.execute("SELECT forked_from_thread_id FROM work_threads WHERE thread_id=?", (thread_id,)).fetchone()
            epochs = db.execute("SELECT native_id FROM thread_usage_epochs WHERE thread_id=? AND provider=? ORDER BY position", (thread_id, provider)).fetchall()
        streams = {}
        for row in rows:
            streams.setdefault((row["native_id"], row["scope"], row["source_key"], row["kind"]), []).append(row)
        candidates = []
        for (native, scope, source, kind), samples in streams.items():
            baseline = baselines.get(native, None if fork and fork[0] else False)
            totals, models, complete, note = self._stream(samples, baseline)
            candidates.append({"native": native, "scope": scope, "source": source, "totals": totals,
                               "models": models, "complete": complete, "note": note,
                               "observed": max(row["observed_at"] for row in samples)})
        # Native IDs may be aliases/resumes or trees. No implicit cross-ID addition.
        # Inclusive observations cover direct usage, so choose one scope as well.
        def rank(candidate):
            return (bool(candidate["totals"]), candidate["totals"].get("total_tokens", -1),
                    candidate["complete"], candidate["observed"], candidate["source"])
        best = max(candidates, key=rank, default=None)
        period_note = None
        if epochs:
            periods = [max((c for c in candidates if c["native"] == epoch["native_id"]),
                           key=rank, default=None) for epoch in epochs]
            known = [period for period in periods if period and period["totals"]]
            if known:
                mixed_scope = len({period["scope"] for period in known}) > 1
                models = {}
                for period in known:
                    for model, value in (period["models"] or {None: period["totals"]}).items():
                        models.setdefault(model, []).append(value)
                best = {"totals": _sum(period["totals"] for period in known),
                        "models": {model: _sum(values) for model, values in models.items()},
                        "complete": len(known) == len(epochs) and not mixed_scope
                                    and all(period["complete"] for period in known),
                        "scope": "including_native_children" if any(period["scope"] == "including_native_children" for period in known) else "direct",
                        "note": None}
                if len(known) != len(epochs):
                    period_note = "Usage for one or more confirmed native periods is unavailable; totals cover the observed periods."
                elif mixed_scope:
                    period_note = "Native periods expose different usage scopes; child coverage is incomplete."
                epoch_ids = {epoch["native_id"] for epoch in epochs}
                if any(c["totals"] and c["native"] not in epoch_ids for c in candidates):
                    period_note = "Unmapped native usage is excluded from the confirmed accounting periods."
                    best["complete"] = False
            else:
                best = None
                period_note = "Usage for the confirmed native accounting periods is unavailable."
        elif len({c["native"] for c in candidates if c["totals"]}) > 1:
            period_note = "Multiple native usage identities are present; totals cover one identity only."
            best = {**best, "complete": False}
        totals = best["totals"] if best else {}
        available = best and best["complete"] and all(k in totals for k in COUNTERS[:3])
        status = "available" if available and not notes else "partial" if totals else "unavailable"
        model_list = [{"model": model, "totals": counts} for model, counts in (best["models"].items() if best else [])]
        observed = max([r["observed_at"] for r in rows] + [r["observed_at"] for r in notes]
                       + ([context["observed_at"]] if context else []), default=None)
        note = period_note or (notes[0]["note"] if notes else best["note"] if best else None)
        if note is None and status != "available":
            note = "Usage coverage is incomplete." if totals else "Token consumption telemetry is unavailable."
        return {"provider": provider, "status": status, "scope": best["scope"] if best else "direct",
                "totals": totals, "models": model_list,
                "context": {"used_tokens": context["used_tokens"], "limit_tokens": context["limit_tokens"]} if context else None,
                "observed_at": _iso(observed), "note": note}

    def raw_snapshot(self, thread_id, *, provider, native_id):
        """Latest unadjusted cumulative counter, only for freezing a fork boundary."""
        with self.store._connection() as db:
            row = db.execute("""SELECT totals_json FROM thread_usage_samples
                WHERE thread_id=? AND provider=? AND native_id=? AND kind='snapshot' AND model=''
                ORDER BY observed_at DESC LIMIT 1""", (thread_id, provider, native_id)).fetchone()
        return json.loads(row[0]) if row else None

    def backfill(self, thread_id, transcript_path=None):
        provider = self.store.get_session(thread_id)["client"]
        if provider == "codex":
            return backfill_codex(self.store, thread_id, transcript_path)
        if provider in {"claude", "muse", "kimi"}:
            module = importlib.import_module("." + provider + "_usage", __package__)
            return module.backfill_usage(self.store, thread_id, transcript_path)
        self.record_unavailable(thread_id, provider=provider,
            note="Antigravity exposes context size only; token consumption telemetry is unsupported."
                 if provider == "antigravity" else "This provider does not expose token consumption telemetry.")
        return self.summary(thread_id)


def _codex_path(thread_id, transcript_path=None):
    if transcript_path is not None:
        path = Path(transcript_path).expanduser()
        if not path.is_absolute() or path.is_symlink():
            raise CoordinationError("Usage transcript must be an absolute regular file.")
    else:
        # Escape glob metacharacters by validating native IDs before discovery.
        import uuid
        try:
            identity = str(uuid.UUID(thread_id))
        except (ValueError, TypeError) as exc:
            raise CoordinationError("Cannot locate a transcript for this native thread ID.") from exc
        root = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
        paths = [p for folder in ("sessions", "archived_sessions")
                 for p in (root / folder).glob(f"**/*-{identity}.jsonl")]
        # Recent app-server versions can keep rollouts outside sessions/.
        # Query their native index read-only, never initialize/migrate it.
        for index in sorted(root.glob("state_*.sqlite"), reverse=True)[:4]:
            try:
                connection = sqlite3.connect(index.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.2)
                try:
                    row = connection.execute("SELECT rollout_path FROM threads WHERE id=?", (identity,)).fetchone()
                finally:
                    connection.close()
                if row and isinstance(row[0], str) and Path(row[0]).is_absolute():
                    candidate = Path(row[0])
                    if candidate.is_file():
                        paths.append(candidate)
            except sqlite3.Error:
                continue
        paths = list({str(p.resolve()): p for p in paths}.values())
        if len(paths) != 1:
            raise CoordinationError("A unique native transcript is unavailable.")
        path = paths[0]
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_TRANSCRIPT_BYTES:
        raise CoordinationError("Usage transcript is missing or exceeds the recovery size limit.")
    return path


def _codex_fork_baseline(metadata, *, seen=None, budget=None):
    """Recover a native paginated fork's frozen prefix, never the parent's tail.

    Both the byte boundary and last ordinal must still match the immutable
    history_base reference. A missing or rewritten parent prefix fails closed.
    """
    base = metadata.get("history_base")
    if not isinstance(base, dict):
        return None
    parent = base.get("thread_id")
    ordinal, offset = base.get("end_ordinal_exclusive"), base.get("end_byte_offset")
    if (not isinstance(parent, str) or parent != metadata.get("forked_from_id")
            or type(ordinal) is not int or ordinal < 1 or type(offset) is not int or offset < 1
            or metadata.get("forked_from_ordinal_exclusive") != ordinal):
        return None
    seen = set() if seen is None else seen
    budget = [MAX_TRANSCRIPT_BYTES] if budget is None else budget
    if parent in seen or len(seen) >= 8 or offset > budget[0]:
        return None
    seen.add(parent)
    try:
        path = _codex_path(parent)
        if offset > path.stat().st_size:
            return None
        latest = None
        parent_metadata = None
        last_ordinal = None
        with path.open("rb") as stream:
            while stream.tell() < offset:
                line = stream.readline(min(MAX_LINE_BYTES + 1, offset - stream.tell()))
                budget[0] -= len(line)
                if not line or len(line) > MAX_LINE_BYTES or not line.endswith(b"\n"):
                    return None
                event = json.loads(line)
                if not isinstance(event, dict) or type(event.get("ordinal")) is not int:
                    return None
                event_ordinal = event["ordinal"]
                if event_ordinal >= ordinal or last_ordinal is not None and event_ordinal != last_ordinal + 1:
                    return None
                last_ordinal = event_ordinal
                payload = event.get("payload")
                if not isinstance(payload, dict):
                    continue
                if parent_metadata is None:
                    if event.get("type") != "session_meta" or payload.get("id") != parent:
                        return None
                    parent_metadata = payload
                if event.get("type") == "event_msg" and payload.get("type") == "token_count":
                    info = payload.get("info")
                    if isinstance(info, dict) and isinstance(info.get("total_token_usage"), dict):
                        latest = codex_totals(info["total_token_usage"])
            if stream.tell() != offset or last_ordinal != ordinal - 1:
                return None
        if latest is not None:
            return latest
        if parent_metadata and parent_metadata.get("forked_from_id"):
            return _codex_fork_baseline(parent_metadata, seen=seen, budget=budget)
        # A complete prefix with no consumption records cannot establish zero
        # unless the provider explicitly supplied an all-zero token counter.
        return None
    except (OSError, ValueError, CoordinationError):
        return None


def backfill_codex(store, thread_id, transcript_path=None):
    """Read one bounded rollout without resuming or running a provider."""
    try:
        path = _codex_path(thread_id, transcript_path)
        latest = None
        malformed = False
        context_used = context_limit = None
        consumed = 0
        with path.open("rb") as stream:
            first = stream.readline(MAX_LINE_BYTES + 1)
            consumed += len(first)
            metadata = json.loads(first)
            if (len(first) > MAX_LINE_BYTES or not isinstance(metadata, dict)
                    or metadata.get("type") != "session_meta" or not isinstance(metadata.get("payload"), dict)
                    or metadata["payload"].get("id") != thread_id):
                raise CoordinationError("Usage transcript identity does not match this thread.")
            native_metadata = metadata["payload"]
            native_fork = native_metadata.get("forked_from_id") or native_metadata.get("forked_from")
            while True:
                line = stream.readline(MAX_LINE_BYTES + 1)
                if not line:
                    break
                consumed += len(line)
                if len(line) > MAX_LINE_BYTES or consumed > MAX_TRANSCRIPT_BYTES:
                    malformed = True
                    break
                try:
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        malformed = True
                        continue
                    payload = event.get("payload") or {}
                    if event.get("type") != "event_msg" or payload.get("type") != "token_count":
                        continue
                    info = payload.get("info")
                    if not isinstance(info, dict) or not isinstance(info.get("total_token_usage"), dict):
                        malformed = True
                        continue
                    totals = codex_totals(info["total_token_usage"])
                    if not totals:
                        continue
                    latest = totals
                    last = info.get("last_token_usage")
                    context_limit = info.get("model_context_window")
                    context_used = (last or {}).get("total_tokens") if isinstance(last, dict) else None
                except (ValueError, TypeError, AttributeError, CoordinationError):
                    malformed = True
        if native_fork:
            # Historical native forks without a saved boundary remain unknown.
            # total-minus-last is unsafe if the first retained event isn't the
            # child's very first model request, so never guess from it.
            store.usage.set_baseline(thread_id, provider="codex", native_id=thread_id,
                                     totals=_codex_fork_baseline(native_metadata,
                                            budget=[max(0, MAX_TRANSCRIPT_BYTES-consumed)]))
        if latest is None:
            raise CoordinationError("Native transcript contains no token consumption telemetry.")
        store.usage.record_snapshot(thread_id, provider="codex", native_id=thread_id,
                                    source_key="codex-cumulative", totals=latest,
                                    coverage="partial" if malformed else "complete")
        if context_used is not None or context_limit is not None:
            store.usage.record_context(thread_id, provider="codex", native_id=thread_id,
                                       used=context_used, limit=context_limit)
        if malformed:
            store.usage.record_unavailable(thread_id, provider="codex", note="Transcript recovery was incomplete or truncated.")
    except (OSError, ValueError, CoordinationError):
        # Do not persist exception text: JSON decode errors/path names can
        # contain user material. A failed scan must preserve earlier samples.
        store.usage.record_unavailable(thread_id, provider="codex", note="Native usage recovery is unavailable or the transcript identity could not be verified.")
    return store.usage.summary(thread_id)
