"""Local dispatch evidence, explicit labels, and delivery-free replay.

Read paths use SQLite read-only mode and never construct an app/provider runtime.
Live replay sends only the saved classifier input to Jev, never to app threads.
"""
from __future__ import annotations

import copy
import json
import sqlite3
import statistics
import time
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

from .attention import ClassificationError, load_config
from .dispatch_routing import DECISION_VERSION, RoutingPolicy, classify, decide, question_for
from .store import CoordinationError, default_database_path


def initialize_evaluation(db):
    db.execute("""CREATE TABLE IF NOT EXISTS dispatch_attempts (
        id INTEGER PRIMARY KEY, request_id INTEGER NOT NULL, started_at REAL NOT NULL,
        evidence_json TEXT NOT NULL)""")
    db.execute("CREATE INDEX IF NOT EXISTS dispatch_attempts_request ON dispatch_attempts(request_id,id)")
    db.execute("""CREATE TABLE IF NOT EXISTS dispatch_labels (
        id INTEGER PRIMARY KEY, request_id INTEGER NOT NULL, action TEXT NOT NULL,
        target_id TEXT, source TEXT NOT NULL, note TEXT NOT NULL, created_at REAL NOT NULL)""")
    db.execute("CREATE INDEX IF NOT EXISTS dispatch_labels_request ON dispatch_labels(request_id,id)")


def _connect(path, *, write=False):
    path = Path(path or default_database_path()).expanduser().resolve()
    db = sqlite3.connect(path.as_uri() + ("?mode=rw" if write else "?mode=ro"), uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    if not write:
        db.execute("PRAGMA query_only=ON")
    return db


def read_records(path, dispatcher_id, *, after=0, limit=20, request_id=None):
    if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise CoordinationError("Use a nonnegative request cursor and a limit from 1 to 100.")
    with closing(_connect(path)) as db:
        db.execute("BEGIN")
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "dispatch_requests" not in tables:
            return {"records": [], "next_cursor": None}
        where, params = "dispatcher_id=? AND id>?", [dispatcher_id, after]
        if request_id:
            where += " AND request_id=?"
            params.append(request_id)
        rows = db.execute("SELECT * FROM dispatch_requests WHERE " + where + " ORDER BY id LIMIT ?",
                          (*params, limit + 1)).fetchall()
        records = []
        for row in rows[:limit]:
            body = json.loads(row["body_json"])
            attempts = ([{"id": a["id"], "started_at": a["started_at"], "evidence": json.loads(a["evidence_json"])}
                         for a in db.execute("SELECT * FROM dispatch_attempts WHERE request_id=? ORDER BY id", (row["id"],))]
                        if "dispatch_attempts" in tables else [])
            labels = ([dict(label) for label in db.execute(
                "SELECT * FROM dispatch_labels WHERE request_id=? ORDER BY id", (row["id"],))]
                      if "dispatch_labels" in tables else [])
            handoffs = []
            # Match the actual provider turn, never guess from timing or similar text.
            # Several queued/steered requests can share a turn; report ambiguity.
            turn_requests = 0
            if row["target_id"] == dispatcher_id and row["turn_id"]:
                turn_requests = db.execute("SELECT COUNT(*) FROM dispatch_requests WHERE dispatcher_id=? AND turn_id=?",
                                           (dispatcher_id, row["turn_id"])).fetchone()[0]
                if "messages" in tables:
                    handoffs = [dict(m) for m in db.execute("""SELECT id,recipient_session_id AS target_id,
                        created_at,delivered_at,acknowledged_at,suppressed_at FROM messages
                        WHERE sender_session_id=? AND sender_turn_id=? AND classification='action_required'
                        AND in_reply_to IS NULL ORDER BY id""", (dispatcher_id, row["turn_id"]))]
            completion = None
            if row["turn_id"] and "turn_completions" in tables:
                completion = db.execute("SELECT status,completed_at FROM turn_completions WHERE thread_id=? AND turn_key=? ORDER BY id DESC LIMIT 1",
                                        (row["target_id"], row["turn_id"])).fetchone()
            events = []
            if "dispatch_events" in tables:
                for event in db.execute("SELECT id,payload_json FROM dispatch_events WHERE request_id=? ORDER BY id", (row["id"],)):
                    payload = json.loads(event[1])
                    events.append({"event_id": event[0], **{k: payload.get(k) for k in ("state", "target_id", "turn_id", "error", "transition_at")}})
            records.append({"id": row["id"], "request_id": row["request_id"], "dispatcher_id": dispatcher_id,
                            "created_at": row["created_at"], "cwd": row["cwd"],
                            "request": {"message": body["message"], "mentions": body.get("mentions", []),
                                        "has_images": bool(body.get("images")), "ask_dispatcher": body.get("askDispatcher", False)},
                            "evidence": json.loads(row["evidence_json"]) if row["evidence_json"] else None,
                            "attempts": attempts, "labels": labels,
                            "outcome": {"state": row["state"], "target_id": row["target_id"], "turn_id": row["turn_id"],
                                        "error": row["error"], "decision": json.loads(row["decision_json"]) if row["decision_json"] else None,
                                        "provider_completion": dict(completion) if completion else None,
                                        "events": events, "llm_handoffs": handoffs,
                                        "handoff_attribution": "shared_turn" if turn_requests > 1 else "exact_turn" if handoffs else "none"}})
    return {"records": records, "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None}


def label_request(path, dispatcher_id, request_id, action, *, target_id=None, source="review", note=""):
    if action not in {"reuse", "new", "dispatcher"} or source not in {"user", "review"}:
        raise CoordinationError("Choose reuse, new, or dispatcher and a user or review label source.")
    if (action == "reuse") != bool(target_id):
        raise CoordinationError("Only reuse labels require a target thread.")
    if len(note) > 4000:
        raise CoordinationError("Label notes must be at most 4000 characters.")
    with closing(_connect(path, write=True)) as db, db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        row = (db.execute("SELECT id FROM dispatch_requests WHERE dispatcher_id=? AND request_id=?",
                          (dispatcher_id, request_id)).fetchone() if "dispatch_requests" in tables else None)
        if not row:
            raise CoordinationError("Routing request not found in this dispatcher.")
        if target_id == dispatcher_id:
            raise CoordinationError("Use action dispatcher to label the entry conversation itself.")
        if target_id and not db.execute("SELECT 1 FROM work_threads WHERE thread_id=?", (target_id,)).fetchone():
            raise CoordinationError("Label target thread does not exist.")
        initialize_evaluation(db)
        identity = db.execute("""INSERT INTO dispatch_labels(request_id,action,target_id,source,note,created_at)
            VALUES (?,?,?,?,?,?)""", (row[0], action, target_id, source, note, time.time())).lastrowid
        return {"label_id": identity, "request_id": request_id, "action": action,
                "target_id": target_id, "source": source, "delivered_messages": 0}


def replay_record(record, *, overrides=None, live=False, config=None, instructions=None, classifier=None):
    evidence = (record["attempts"][-1]["evidence"] if record.get("attempts") else record.get("evidence")) or {}
    saved = evidence.get("input")
    if not saved:
        return {"status": "unavailable", "reason": "classifier_input_not_recorded"}
    if evidence.get("version") != 1:
        return {"status": "unavailable", "reason": "unsupported_evidence_version"}
    overrides = overrides or {}
    if not live and (instructions is not None or set(overrides) - {"min_confidence", "min_probability", "min_margin"}):
        raise CoordinationError("Model and prompt changes require --live; offline replay only changes thresholds.")
    policy = RoutingPolicy(**{**evidence["policy"], **overrides})
    state = copy.deepcopy(saved["state"])
    question = copy.deepcopy(saved["questions"]["destination"])
    if "name" in overrides:
        question = question_for(state, policy)
    if instructions is not None:
        question["instructions"] = instructions
    answer = evidence.get("answer")
    elapsed = None
    if live:
        start = time.perf_counter()
        try:
            answer = (classifier or classify)(state, config, policy, question=question)
        except ClassificationError as exc:
            return {"status": "error", "reason": str(exc)}
        elapsed = round((time.perf_counter() - start) * 1000, 1)
    if answer is None:
        return {"status": "unavailable", "reason": "classifier_answer_not_recorded"}
    decision = decide(state, answer, policy)
    chosen = next((c["thread_id"] for c in state["candidates"] if c["key"] == answer["choice"]), answer["choice"])
    ordered = sorted(answer["probabilities"].values(), reverse=True)
    result = {"status": "replayed", "decision_version": DECISION_VERSION,
              "policy": asdict(policy), "answer": answer, "decision": decision,
              "classifier_choice": chosen, "live_elapsed_ms": elapsed,
              "gates": {"confidence": answer["confidence"] >= policy.min_confidence,
                        "probability": ordered[0] >= policy.min_probability,
                        "margin": ordered[0] - ordered[1] >= policy.min_margin}}
    labels = {}
    for label in record.get("labels", []):
        labels[label["source"]] = label
    result["labels"] = {}
    for source, label in labels.items():
        correct = decision["action"] == label["action"] and (label["action"] != "reuse" or decision.get("thread_id") == label["target_id"])
        result["labels"][source] = {"label_id": label["id"], "correct": correct,
            "retrieval_hit": label["target_id"] in {c["thread_id"] for c in state["candidates"]} if label["action"] == "reuse" else None,
            "expected_retrieval_rank": next((r["rank"] for r in evidence.get("retrieval", []) if r["thread_id"] == label["target_id"]), None),
            "wrong_automatic": decision["action"] in {"reuse", "new"} and not correct}
    targets = {m["target_id"] for m in record["outcome"]["llm_handoffs"] if not m.get("suppressed_at")}
    result["llm_choice_agrees"] = (chosen in targets if len(targets) == 1 and record["outcome"]["handoff_attribution"] == "exact_turn" else None)
    if live:
        result["evaluated_input"] = {"model": policy.model, "state": state, "questions": {"destination": question}}
    return result


def lifecycle_timings(record):
    """Observed wall-clock intervals; old or ambiguous events stay unavailable."""
    events = record["outcome"].get("events", [])
    stages = {}
    for event in events:
        stages.setdefault(event["state"], []).append(event.get("transition_at"))
    result = {}

    def interval(name, start, end):
        if start is not None and end is not None and end >= start:
            result[name] = round((end - start) * 1000, 1)

    routing = stages.get("routing", [])
    if routing:
        interval("intake_wait", record["created_at"], routing[0])
    # Do not turn retries or partial legacy traces into misleading stage times.
    queued, sending, accepted = (stages.get(s, []) for s in ("queued", "sending", "accepted"))
    if len(accepted) == 1:
        interval("intake_to_accept", record["created_at"], accepted[0])
    if len(queued) == len(sending) == 1:
        interval("queue_wait", queued[0], sending[0])
    if len(sending) == len(accepted) == 1:
        interval("provider_submit", sending[0], accepted[0])
    handoffs = [h for h in record["outcome"].get("llm_handoffs", []) if not h.get("suppressed_at")]
    if record["outcome"].get("handoff_attribution") == "exact_turn" and len(handoffs) == 1:
        interval("intake_to_llm_handoff", record["created_at"], handoffs[0]["created_at"])
    return result


def summarize(records):
    replayed = [r["replay"] for r in records if r["replay"]["status"] == "replayed"]
    timings = [r["replay"].get("live_elapsed_ms") for r in records]
    timings = sorted(t for t in timings if t is not None)
    original = sorted(r["evidence"]["elapsed_ms"] for r in records if (r.get("evidence") or {}).get("elapsed_ms") is not None)
    automatic = sum(r["decision"]["action"] in {"reuse", "new"} for r in replayed)
    summary = {"records": len(records), "replayed": len(replayed), "automatic": automatic,
               "fallback": len(replayed) - automatic,
               "fallback_rate": (len(replayed) - automatic) / len(replayed) if replayed else None,
               "unavailable": sum(r["replay"]["status"] == "unavailable" for r in records),
               "errors": sum(r["replay"]["status"] == "error" for r in records),
               "live_latency_p50_ms": statistics.median(timings) if timings else None,
               "live_latency_p95_ms": timings[min(len(timings) - 1, int(len(timings) * .95))] if timings else None,
               "recorded_latency_p50_ms": statistics.median(original) if original else None,
               "recorded_latency_p95_ms": original[min(len(original) - 1, int(len(original) * .95))] if original else None,
               "llm_comparisons": sum(r["llm_choice_agrees"] is not None for r in replayed),
               "llm_agreements": sum(r["llm_choice_agrees"] is True for r in replayed), "labels": {}}
    stages = {}
    for record in records:
        values = {**(record.get("evidence") or {}).get("timings_ms", {}), **lifecycle_timings(record)}
        for name, value in values.items():
            stages.setdefault(name, []).append(value)
    summary["stage_latency_ms"] = {
        name: {"count": len(values), "p50": statistics.median(values),
               "p95": sorted(values)[min(len(values) - 1, int(len(values) * .95))]}
        for name, values in sorted(stages.items())}
    for source in ("user", "review"):
        labeled = [r for r in replayed if source in r["labels"]]
        labels = [r["labels"][source] for r in labeled]
        labeled_auto = [r["labels"][source] for r in labeled if r["decision"]["action"] in {"reuse", "new"}]
        retrieval = [r for r in labels if r["retrieval_hit"] is not None]
        summary["labels"][source] = {"count": len(labels), "correct": sum(r["correct"] for r in labels),
                                     "wrong_automatic": sum(r["wrong_automatic"] for r in labels),
                                     "accuracy": sum(r["correct"] for r in labels) / len(labels) if labels else None,
                                     "automatic_precision": sum(r["correct"] for r in labeled_auto) / len(labeled_auto) if labeled_auto else None,
                                     "retrieval_cases": len(retrieval), "retrieval_hits": sum(r["retrieval_hit"] for r in retrieval),
                                     "retrieval_recall": sum(r["retrieval_hit"] for r in retrieval) / len(retrieval) if retrieval else None}
    return summary


def evaluate(path, dispatcher_id, *, after=0, limit=20, request_id=None, overrides=None, live=False, instructions=None):
    if not live and (instructions is not None or set(overrides or {}) - {"min_confidence", "min_probability", "min_margin"}):
        raise CoordinationError("Model and prompt changes require --live; offline replay only changes thresholds.")
    # Validate flags even on an empty/legacy dataset.
    RoutingPolicy(**(overrides or {}))
    page = read_records(path, dispatcher_id, after=after, limit=limit, request_id=request_id)
    config = load_config(path or default_database_path()) if live else None
    if live and not config:
        raise CoordinationError("Live evaluation requires enabled Jev configuration.")
    for record in page["records"]:
        record["replay"] = replay_record(record, overrides=overrides, live=live, config=config, instructions=instructions)
        record["lifecycle_timings_ms"] = lifecycle_timings(record)
    return {"version": 1, "live_jev": live, "delivered_messages": 0, "read_only": True,
            "summary": summarize(page["records"]), **page}
