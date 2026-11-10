#!/usr/bin/env python3
"""Verify retained audit identities and interpreter capabilities without a server.

This checks documentary evidence, not application performance. It never opens a
coordination database, starts a provider, builds the app or changes issue state.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess


DIRECTORY = Path(__file__).resolve().parent
REPOSITORY = DIRECTORY.parents[2]


def read_json(name):
    return json.loads((DIRECTORY / name).read_text())


def verify():
    commits = read_json("commits.json")
    beads = read_json("beads.json")
    conversations = read_json("conversations.json")
    runtime = read_json("runtime-evidence.json")
    errors = []
    checks = {}

    records = commits["commits"]
    hashes = [record["hash"] for record in records]
    all_hashes = [record["hash"] for record in commits["all_commit_screening"]]
    if len(hashes) != len(set(hashes)) or len(all_hashes) != len(set(all_hashes)):
        errors.append("Duplicate Git identities in an inventory.")
    if not set(hashes) <= set(all_hashes):
        errors.append("Retained commits missing from the complete screening inventory.")
    if len(all_hashes) != commits["coverage"]["total_screened_commit_count"]:
        errors.append("Commit coverage count does not match screening records.")
    inspected_hashes = sorted(set(all_hashes + commits["coverage"]["reflog_only_commit_ids"]))
    result = subprocess.run(
        ["git", "cat-file", "--batch-check=%(objectname) %(objecttype)"],
        input="\n".join(inspected_hashes) + "\n", text=True, capture_output=True,
        cwd=REPOSITORY, check=True,
    )
    for requested, line in zip(inspected_hashes, result.stdout.splitlines()):
        if line != requested + " commit":
            errors.append("Unavailable Git commit: " + requested)
    checks["git"] = {"screened": len(all_hashes), "retained": len(hashes),
                     "objects_checked": len(inspected_hashes), "anchor": commits["coverage"]["anchor"],
                     "closing_head": commits["coverage"]["closing_head"]}

    issues = beads["issues"]
    if len(issues) != len({issue["id"] for issue in issues}):
        errors.append("Duplicate issue identities.")
    if len(issues) != beads["coverage"]["relevant_or_context_issue_count"]:
        errors.append("Retained issue count differs from coverage.")
    if sum(beads["coverage"]["statuses"].values()) != beads["coverage"]["all_issue_count"]:
        errors.append("Issue status counts do not reconcile.")
    checks["beads"] = {"all_issues": beads["coverage"]["all_issue_count"],
                       "retained": len(issues), "statuses": beads["coverage"]["statuses"],
                       "retained_categories": dict(Counter(issue["category"] for issue in issues)),
                       "historical_revisions": beads["coverage"]["distinct_dolt_revisions"]}
    thread_records = conversations["threads"]
    if len(thread_records) != conversations["coverage"]["selected_threads"]:
        errors.append("Conversation inventory count differs from coverage.")
    for identity, record in thread_records.items():
        if identity != record["metadata"]["thread_id"]:
            errors.append("Conversation mapping identity mismatch: " + identity)
    if sum(conversations["coverage"]["classification_counts"].values()) != len(thread_records):
        errors.append("Conversation classification counts do not reconcile.")
    coverage = conversations["coverage"]
    if coverage["precision_screened_threads"] != coverage["precision_initial_retained_threads"] + coverage["excluded_incidental_threads"]:
        errors.append("Initial precision screening counts do not reconcile.")
    if len(thread_records) != coverage["precision_initial_retained_threads"] + coverage["broad_inventory_restored_threads"]:
        errors.append("Conversation restoration counts do not reconcile.")
    checks["conversations"] = conversations.get("coverage", {})

    source_results = []
    for record in runtime["reports"]:
        source = Path(record["source"])
        if source.exists():
            same = hashlib.sha256(source.read_bytes()).hexdigest() == record["sha256"]
            source_results.append({"source": str(source), "hash_matches": same})
            if not same:
                errors.append("Captured receipt changed: " + str(source))
        else:
            source_results.append({"source": str(source), "available": False})
    checks["receipt_sources"] = source_results

    capability_code = (
        "import json,runpy,sqlite3,sys; "
        "rows=runpy.run_path(sys.argv[1]); checkpoint=runpy.run_path(sys.argv[2]); "
        "print(json.dumps({'python':sys.version.split()[0],'sqlite':sqlite3.sqlite_version,"
        "'batched_row_transfer':rows['NATIVE_JSON_REAL_ROUND_TRIP'],"
        "'concurrent_checkpoint':checkpoint['supports_concurrent_checkpoint']()}))"
    )
    capabilities = {}
    for label in ("active", "selected"):
        record = runtime[label]
        payload = Path(runtime["runtime_status"]["payload"]).parent / record["payload"]
        python = Path(record["python"])
        if not python.exists() or not payload.exists():
            capabilities[label] = {"available": False}
            continue
        result = subprocess.run(
            [str(python), "-c", capability_code,
             str(payload / "backend/agent_coord/sqlite_rows.py"),
             str(payload / "backend/agent_coord/database_checkpoint.py")],
            check=True, text=True, capture_output=True, timeout=10,
        )
        capabilities[label] = json.loads(result.stdout)
    checks["interpreter_capabilities"] = capabilities
    if capabilities.get("active", {}).get("sqlite") == "3.51.0":
        if capabilities["active"]["batched_row_transfer"] or capabilities["active"]["concurrent_checkpoint"]:
            errors.append("Active capability predicates contradict the captured runtime explanation.")
    if capabilities.get("selected", {}).get("sqlite") == "3.53.4":
        if not capabilities["selected"]["batched_row_transfer"] or not capabilities["selected"]["concurrent_checkpoint"]:
            errors.append("Selected capability predicates contradict the captured runtime explanation.")

    missing_links = []
    for document in DIRECTORY.glob("*.md"):
        for target in re.findall(r"\]\(([^)]+)\)", document.read_text()):
            if "://" in target or target.startswith("#"):
                continue
            target = target.split("#", 1)[0].strip("<>")
            if target and target != "verification.json" and not (document.parent / target).exists():
                missing_links.append({"document": document.name, "target": target})
    checks["missing_local_links"] = missing_links
    errors.extend("Missing local link: " + str(record) for record in missing_links)

    for document in sorted(DIRECTORY.glob("*.json")):
        if document.name != "verification.json":
            json.loads(document.read_text())
    checks["artifact_sha256"] = {
        document.name: hashlib.sha256(document.read_bytes()).hexdigest()
        for document in sorted(DIRECTORY.iterdir())
        if document.is_file() and document.name != "verification.json"
    }
    return {"checked_at_utc": datetime.now(timezone.utc).isoformat(),
            "scope": "Evidence identity, counts, receipt hashes, local links and interpreter predicates; no performance acceptance.",
            "passed": not errors, "errors": errors, "checks": checks}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="Save verification.json beside this script.")
    args = parser.parse_args()
    result = verify()
    if args.write:
        (DIRECTORY / "verification.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"passed": result["passed"], "errors": result["errors"],
                      "git": result["checks"]["git"], "beads": result["checks"]["beads"],
                      "interpreter_capabilities": result["checks"]["interpreter_capabilities"]}, indent=2))
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
