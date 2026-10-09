#!/usr/bin/env python3
"""Shadow evaluation of dispatch logic; no message delivery or agent creation.

Snapshot: python3 scripts/validate_dispatch_routing.py snapshot --db PATH --output snapshot.json
Evaluate: python3 scripts/validate_dispatch_routing.py evaluate --snapshot snapshot.json
          --cases cases.json --split development --live --output results.json

Cases: JSON list of {id, split, request, expected: [{action, thread_id?}],
       expected_candidates?: [thread IDs], overlays?: {thread ID: fixture fields}}.
Overlays represent explicitly labelled counterfactual states and never modify
the source snapshot or app database. Omitting --live evaluates retrieval only.
Policy options tune the Choice instructions, shortlist and decision gates.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))

from agent_coord.attention import load_config
from agent_coord.dispatch_routing import RoutingPolicy, build_state, route
from agent_coord.dispatch_snapshot import read_snapshot


def write_private(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # These artifacts can contain private conversation excerpts.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as output:
        json.dump(data, output, ensure_ascii=False, indent=2)
        output.write("\n")
    path.chmod(0o600)


def check_output(path, inputs):
    output = Path(path).expanduser().resolve()
    for source in inputs:
        source = Path(source).expanduser().resolve()
        if output == source or (output.exists() and source.exists() and output.samefile(source)):
            raise ValueError("Output must not overwrite an input or the app database")


def evaluate_case(snapshot, case, policy, config=None, *, live=False):
    view = copy.deepcopy(snapshot)
    for thread in view["threads"]:
        thread.update(case.get("overlays", {}).get(thread["thread_id"], {}))
    started = time.monotonic()
    result = (route(view, case["request"], config, policy) if live else
              {"state": build_state(view, case["request"], policy)})
    ids = {c["thread_id"] for c in result["state"]["candidates"]}
    expected_ids = case.get("expected_candidates", [])
    result.update(case_id=case["id"], split=case["split"], elapsed_seconds=round(time.monotonic() - started, 3),
                  retrieval_hit=bool(ids & set(expected_ids)) if expected_ids else None,
                  expected=case["expected"], counterfactual=bool(case.get("overlays")))
    if live:
        decision = result["decision"]
        result["correct"] = any(all(decision.get(k) == v for k, v in expected.items()) for expected in case["expected"])
        result["automatic"] = decision["action"] in {"reuse", "new"}
        result["wrong_automatic"] = result["automatic"] and not result["correct"]
    return result


def summarize(results):
    retrieved = [r for r in results if r["retrieval_hit"] is not None]
    live = [r for r in results if "correct" in r]
    automatic = [r for r in live if r["automatic"]]
    timings = sorted(r["elapsed_seconds"] for r in results)
    return {"cases": len(results), "retrieval_cases": len(retrieved),
            "retrieval_hits": sum(r["retrieval_hit"] for r in retrieved),
            "correct": sum(r["correct"] for r in live), "automatic": len(automatic),
            "wrong_automatic": sum(r["wrong_automatic"] for r in live),
            "fallback": len(live) - len(automatic),
            "automatic_precision": sum(r["correct"] for r in automatic) / len(automatic) if automatic else None,
            "latency_p50_seconds": statistics.median(timings) if timings else None,
            "latency_p95_seconds": timings[min(len(timings) - 1, int(len(timings) * .95))] if timings else None,
            "input_tokens": sum((r.get("answer") or {}).get("usage", {}).get("input_tokens", 0) for r in results)}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    snapshot = commands.add_parser("snapshot")
    default_db = os.environ.get("AGENT_COORD_DB", str(Path.home() / ".local/state/agent-coord/state.sqlite3"))
    snapshot.add_argument("--db", default=default_db)
    snapshot.add_argument("--output", required=True)
    evaluate = commands.add_parser("evaluate")
    evaluate.add_argument("--snapshot", required=True)
    evaluate.add_argument("--cases", required=True)
    evaluate.add_argument("--split", choices=("development", "validation"), required=True)
    evaluate.add_argument("--output", required=True)
    evaluate.add_argument("--live", action="store_true", help="Send bounded candidate context to Jev; never send to app threads")
    evaluate.add_argument("--config-db", default=default_db, help="Resolve existing Jev credential configuration beside this path")
    evaluate.add_argument("--policy", choices=("baseline", "continuity"), default="continuity")
    evaluate.add_argument("--model", default=RoutingPolicy().model)
    evaluate.add_argument("--candidates", type=int, default=8)
    evaluate.add_argument("--confidence", type=float, default=.75)
    evaluate.add_argument("--probability", type=float, default=.70)
    evaluate.add_argument("--margin", type=float, default=.20)
    evaluate.add_argument("--workers", type=int, choices=range(1, 5), default=2)
    args = parser.parse_args()
    try:
        check_output(args.output, [args.db] if args.command == "snapshot" else
                     [args.snapshot, args.cases, args.config_db])
    except ValueError as exc:
        parser.error(str(exc))
    if args.command == "snapshot":
        result = read_snapshot(args.db)
        write_private(args.output, result)
        print(json.dumps({"threads": len(result["threads"]), "output": args.output, "read_only": True}))
        return
    config = load_config(args.config_db) if args.live else None
    if args.live and config is None:
        parser.error("Live evaluation requires enabled Jev configuration")
    snapshot_bytes = Path(args.snapshot).read_bytes()
    cases_bytes = Path(args.cases).read_bytes()
    snapshot = json.loads(snapshot_bytes)
    cases = [c for c in json.loads(cases_bytes) if c["split"] == args.split]
    if not cases:
        parser.error("No cases in the selected split")
    policy = RoutingPolicy(name=args.policy, model=args.model, candidate_limit=args.candidates,
                           min_confidence=args.confidence, min_probability=args.probability, min_margin=args.margin)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(lambda c: evaluate_case(snapshot, c, policy, config, live=args.live), cases))
    report = {"snapshot_sha256": hashlib.sha256(snapshot_bytes).hexdigest(),
              "cases_sha256": hashlib.sha256(cases_bytes).hexdigest(), "policy": asdict(policy),
              "split": args.split, "live_jev": args.live, "delivered_messages": 0,
              "summary": summarize(results), "results": results}
    write_private(args.output, report)
    print(json.dumps({"output": args.output, **report["summary"]}))


if __name__ == "__main__":
    main()
