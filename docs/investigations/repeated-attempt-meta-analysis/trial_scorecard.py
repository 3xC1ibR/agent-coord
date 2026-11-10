#!/usr/bin/env python3
"""Ledger and scorecard for the pre-registered repeated-attempt trial.

The ledger is an append-only JSON Lines file. Each line is one event:
``register``, ``accept``, ``recur``, ``withdraw`` or ``exclude``. Scores are
computed from the ledger alone; ``scan`` only proposes candidate recurrences
for a judge to confirm, using read-only queries of the coordination database.

Usage:
  trial_scorecard.py register --id T01 --thread THREAD --stratum R|N --symptom TEXT --dod TEXT [--saga S01] [--changes C1,C2]
  trial_scorecard.py accept   --id T01 --claim TEXT --evidence live_measurement,unit_tests --running yes|no|unknown [--payload HASH]
  trial_scorecard.py recur    --id T01 --record ID --quote TEXT --observed-at ISO --judge NAME
  trial_scorecard.py exclude  --id T01 --reason TEXT
  trial_scorecard.py scan     --id T01 --terms 'refresh,timed out' [--db PATH]
  trial_scorecard.py score    [--stratum R|N] [--as-of ISO] [--baseline-rate P] [--json]

The ledger path defaults to trial/ledger.jsonl beside this script; override
with --ledger. Times are UTC.
"""

import argparse
import datetime
import json
import math
import os
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_LEDGER = HERE / "trial" / "ledger.jsonl"
DEFAULT_DB = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "agent-coord/state.sqlite3"
WINDOW_DAYS = 14
EVIDENCE_TYPES = {
    "none", "unit_tests", "integration_tests", "synthetic_workload", "live_measurement",
    "user_confirmation", "activation_receipt", "review",
}


def utc_now():
    return datetime.datetime.now(datetime.UTC)


def parse_time(value):
    if value is None:
        return None
    parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise SystemExit(f"time must carry a UTC offset: {value}")
    return parsed.astimezone(datetime.UTC)


def iso(moment):
    return moment.astimezone(datetime.UTC).isoformat().replace("+00:00", "Z")


def read_ledger(path):
    if not path.exists():
        return []
    events = []
    for number, line in enumerate(path.read_text().splitlines(), 1):
        if line.strip():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError as error:
                raise SystemExit(f"{path}:{number}: {error}")
    return events


def append(path, event):
    path.parent.mkdir(parents=True, exist_ok=True)
    event = {"recorded_at": iso(utc_now()), **event}
    with path.open("a") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
    print(json.dumps(event, ensure_ascii=False))


def assignments(events):
    """Fold events into per-assignment state."""
    state = {}
    for event in events:
        key = event["id"]
        if event["event"] == "register":
            if key in state:
                raise SystemExit(f"duplicate registration for {key}")
            state[key] = {"register": event, "accepts": [], "recurrences": [], "excluded": None}
        elif key not in state:
            raise SystemExit(f"{event['event']} for unregistered assignment {key}")
        elif event["event"] == "accept":
            state[key]["accepts"].append(event)
        elif event["event"] == "recur":
            state[key]["recurrences"].append(event)
        elif event["event"] == "exclude":
            state[key]["excluded"] = event
    return state


def outcome(record, as_of):
    """Primary measure: accepted once, with no same-symptom recurrence within 14 days.

    Returns (status, detail). Status is one of: success, failure, pending,
    excluded, censored.
    """
    if record["excluded"]:
        return "excluded", record["excluded"]["reason"]
    accepts = record["accepts"]
    if not accepts:
        return "pending", "no acceptance claim yet"
    first = parse_time(accepts[0]["accepted_at"])
    if len(accepts) > 1:
        return "failure", f"{len(accepts)} acceptance claims (re-acceptance after a further attempt)"
    for recurrence in record["recurrences"]:
        observed = parse_time(recurrence["observed_at"])
        if first <= observed <= first + datetime.timedelta(days=WINDOW_DAYS):
            return "failure", f"recurrence {recurrence['record']} at {recurrence['observed_at']}"
    if as_of < first + datetime.timedelta(days=WINDOW_DAYS):
        return "censored", f"window closes {iso(first + datetime.timedelta(days=WINDOW_DAYS))}"
    return "success", "accepted once; no recurrence within 14 days"


def binomial_tail(successes, trials, rate):
    """P(X >= successes) for X ~ Binomial(trials, rate)."""
    return sum(math.comb(trials, k) * rate ** k * (1 - rate) ** (trials - k) for k in range(successes, trials + 1))


def command_register(args):
    append(args.ledger, {
        "event": "register", "id": args.id, "thread": args.thread, "stratum": args.stratum, "symptom": args.symptom,
        "dod": args.dod, "saga": args.saga, "changes": [c for c in (args.changes or "").split(",") if c],
        "registered_at": iso(parse_time(args.at) if args.at else utc_now()),
    })


def command_accept(args):
    evidence = [e for e in args.evidence.split(",") if e]
    unknown = set(evidence) - EVIDENCE_TYPES
    if unknown:
        raise SystemExit(f"unknown evidence types: {sorted(unknown)}")
    append(args.ledger, {
        "event": "accept", "id": args.id, "claim": args.claim, "evidence": evidence,
        "running": args.running, "payload": args.payload,
        "accepted_at": iso(parse_time(args.at) if args.at else utc_now()),
    })


def command_recur(args):
    append(args.ledger, {
        "event": "recur", "id": args.id, "record": args.record, "quote": args.quote,
        "observed_at": iso(parse_time(args.observed_at)), "judge": args.judge,
    })


def command_exclude(args):
    append(args.ledger, {"event": "exclude", "id": args.id, "reason": args.reason})


def command_scan(args):
    record = assignments(read_ledger(args.ledger)).get(args.id)
    if not record:
        raise SystemExit(f"unregistered assignment {args.id}")
    if not record["accepts"]:
        raise SystemExit("scan requires an acceptance claim")
    start = parse_time(record["accepts"][0]["accepted_at"]).timestamp()
    end = start + WINDOW_DAYS * 86400
    terms = [t.strip().lower() for t in args.terms.split(",") if t.strip()]
    connection = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=10)
    connection.execute("pragma query_only = 1")
    try:
        clauses = " or ".join("lower(text) like ?" for _ in terms)
        rows = connection.execute(
            f"select thread_id, item_id, timestamp, substr(text, 1, 400) from conversation_search_messages "
            f"where role = 'user' and timestamp between ? and ? and ({clauses}) order by timestamp",
            [start, end] + [f"%{t}%" for t in terms]).fetchall()
        threads = connection.execute(
            f"select thread_id, created_at, title, substr(original_request, 1, 400) from work_threads "
            f"where created_at between ? and ? and ({' or '.join('lower(title || original_request) like ?' for _ in terms)})",
            [start, end] + [f"%{t}%" for t in terms]).fetchall()
    finally:
        connection.close()
    print(json.dumps({
        "assignment": args.id, "window_utc": [iso(datetime.datetime.fromtimestamp(start, datetime.UTC)),
                                              iso(datetime.datetime.fromtimestamp(end, datetime.UTC))],
        "note": "Candidates only. A judge confirms the same symptom before recording `recur`.",
        "user_messages": [{"thread": r[0], "item": r[1], "at": iso(datetime.datetime.fromtimestamp(r[2], datetime.UTC)), "text": r[3]} for r in rows],
        "new_threads": [{"thread": r[0], "at": iso(datetime.datetime.fromtimestamp(r[1], datetime.UTC)), "title": r[2], "request": r[3]} for r in threads],
    }, indent=1, ensure_ascii=False))


def command_score(args):
    as_of = parse_time(args.as_of) if args.as_of else utc_now()
    state = assignments(read_ledger(args.ledger))
    rows = []
    for key in sorted(state):
        stratum = state[key]["register"].get("stratum")
        if args.stratum and stratum != args.stratum:
            continue
        status, detail = outcome(state[key], as_of)
        accepts = state[key]["accepts"]
        rows.append({
            "id": key, "stratum": stratum, "status": status, "detail": detail,
            "running_verified_at_claim": accepts[0]["running"] if accepts else None,
            "evidence": accepts[0]["evidence"] if accepts else [],
            "changes": state[key]["register"]["changes"],
        })
    decided = [r for r in rows if r["status"] in ("success", "failure")]
    successes = sum(r["status"] == "success" for r in decided)
    summary = {
        "as_of": iso(as_of), "stratum": args.stratum or "all", "registered": len(rows),
        "counts": {s: sum(r["status"] == s for r in rows) for s in ("success", "failure", "pending", "censored", "excluded")},
        "decided": len(decided), "successes": successes,
    }
    if args.baseline_rate is not None and decided:
        summary["baseline_rate"] = args.baseline_rate
        summary["p_value_one_sided"] = round(binomial_tail(successes, len(decided), args.baseline_rate), 4)
    if args.json:
        print(json.dumps({"summary": summary, "assignments": rows}, indent=1, ensure_ascii=False))
        return
    for r in rows:
        print(f"{r['id']:6} {r['stratum']!s:2} {r['status']:9} running={r['running_verified_at_claim']!s:7} {r['detail']}")
    print(json.dumps(summary, indent=1))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("register"); p.add_argument("--id", required=True); p.add_argument("--thread", required=True)
    p.add_argument("--stratum", choices=["R", "N"], required=True)
    p.add_argument("--symptom", required=True); p.add_argument("--dod", required=True); p.add_argument("--saga")
    p.add_argument("--changes"); p.add_argument("--at"); p.set_defaults(func=command_register)
    p = sub.add_parser("accept"); p.add_argument("--id", required=True); p.add_argument("--claim", required=True)
    p.add_argument("--evidence", required=True); p.add_argument("--running", choices=["yes", "no", "unknown"], required=True)
    p.add_argument("--payload"); p.add_argument("--at"); p.set_defaults(func=command_accept)
    p = sub.add_parser("recur"); p.add_argument("--id", required=True); p.add_argument("--record", required=True)
    p.add_argument("--quote", required=True); p.add_argument("--observed-at", required=True); p.add_argument("--judge", required=True)
    p.set_defaults(func=command_recur)
    p = sub.add_parser("exclude"); p.add_argument("--id", required=True); p.add_argument("--reason", required=True)
    p.set_defaults(func=command_exclude)
    p = sub.add_parser("scan"); p.add_argument("--id", required=True); p.add_argument("--terms", required=True)
    p.add_argument("--db", type=Path, default=DEFAULT_DB); p.set_defaults(func=command_scan)
    p = sub.add_parser("score"); p.add_argument("--stratum", choices=["R", "N"]); p.add_argument("--as-of"); p.add_argument("--baseline-rate", type=float)
    p.add_argument("--json", action="store_true"); p.set_defaults(func=command_score)
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
