#!/usr/bin/env python3
"""Extract the raw records used by the repeated-attempt meta-analysis.

Read-only: Git and Beads are queried with read commands, and the coordination
database is opened with ``mode=ro`` and ``PRAGMA query_only``. No provider is
resumed. Output is written to a scratch directory outside the repository.

Usage: python3 collect_sources.py OUTPUT_DIRECTORY [--repo PATH] [--db PATH]
"""

import argparse
import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path

DEFAULT_DB = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "agent-coord/state.sqlite3"


def run(args, cwd):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=True).stdout


def collect_git(repo, out):
    refs = [r for r in run(["git", "for-each-ref", "--format=%(refname) %(objectname)"], repo).split("\n") if r]
    reachable = set(run(["git", "rev-list", "--all"], repo).split())
    reflog = list(dict.fromkeys(run(["git", "reflog", "--all", "--format=%H"], repo).split()))
    reflog_only = [h for h in reflog if h not in reachable]
    sep, fs = "\x1e", "\x1f"
    fmt = sep + fs.join(["%H", "%P", "%an", "%aI", "%cI", "%s", "%b"]) + fs
    text = run(["git", "log", "--all", "--numstat", "--format=" + fmt] + reflog_only, repo)
    commits = []
    for block in text.split(sep)[1:]:
        parts = block.split(fs)
        commit_hash, parents, author, authored, committed, subject, body = parts[:7]
        files = []
        for line in (parts[7] if len(parts) > 7 else "").strip().split("\n"):
            cells = line.split("\t")
            if len(cells) == 3:
                files.append({"path": cells[2], "added": cells[0], "deleted": cells[1]})
        commits.append({
            "hash": commit_hash, "parents": parents.split(), "author": author,
            "authored": authored, "committed": committed, "subject": subject,
            "body": body.strip(), "files": files, "reflog_only": commit_hash in reflog_only,
        })
    (out / "git_all.json").write_text(json.dumps({"refs": refs, "reflog_only": reflog_only, "commits": commits}))
    return {"refs": len(refs), "commits": len(commits), "reflog_only": len(reflog_only)}


def collect_beads(repo, out):
    issues = json.loads(run(["bd", "list", "--all", "--limit", "0", "--json"], repo))
    details = {}
    for issue in issues:
        text = run(["bd", "show", issue["id"], "--json", "--include-comments"], repo)
        record = json.loads(text)
        details[issue["id"]] = record[0] if isinstance(record, list) else record
    (out / "beads_show.json").write_text(json.dumps(details))
    return {"issues": len(details)}


QUERIES = {
    "threads": """select w.thread_id, p.root, w.project_cwd, w.title, w.title_source,
        substr(w.original_request, 1, 8000) original_request, length(w.original_request) original_request_len,
        w.attention, w.created_at, w.updated_at, w.forked_from_thread_id, w.domain, s.client,
        s.cwd session_cwd, s.started_at, s.ended_at, b.model, b.effort, b.yolo, b.permission_mode,
        b.client browser_client
        from work_threads w join work_projects p on p.id = w.project_id
        left join sessions s on s.session_id = w.thread_id
        left join browser_sessions b on b.thread_id = w.thread_id""",
    "checkpoints": """select id, thread_id, phase, summary, next_action, next_actor, session_id, turn_id,
        author, created_at, context_lifecycle_json from thread_checkpoints""",
    "user_messages": """select thread_id, turn_id, item_id, role, timestamp, substr(text, 1, 6000) text,
        length(text) text_len, position from conversation_search_messages where role = 'user'""",
    "activity": """select sequence, event_id, kind, thread_id, occurred_at, actor, source, turn_id, details_json
        from activity_events where kind in ('message_submitted', 'turn_started', 'turn_ended', 'decision_submitted')""",
    "messages": """select id, sender_session_id, recipient_session_id, substr(body, 1, 4000) body,
        length(body) body_len, created_at, classification, thread_id, reply_required, in_reply_to,
        context_lifecycle_json from messages""",
    "delegations": """select delegation_id, parent_session_id, child_session_id, client, cwd, bead_id,
        substr(instructions, 1, 4000) instructions, status, mode, created_at, completed_at,
        substr(result_message, 1, 3000) result_message, error, model, reasoning_effort from delegations""",
    "usage_models": """select thread_id, provider, model, count(*) n, min(observed_at) first, max(observed_at) last
        from thread_usage_samples group by 1, 2, 3""",
    "history_index_status": "select thread_id, status from conversation_search_histories",
}


def collect_database(db, out):
    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    connection.execute("pragma query_only = 1")
    connection.row_factory = sqlite3.Row
    counts = {}
    try:
        for name, query in QUERIES.items():
            rows = [dict(row) for row in connection.execute(query)]
            (out / f"{name}.json").write_text(json.dumps(rows))
            counts[name] = len(rows)
    finally:
        connection.close()
    return counts


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    receipt = {"collected_at": time.time(), "repo": str(args.repo), "db": str(args.db)}
    receipt["git"] = collect_git(args.repo, args.output)
    receipt["beads"] = collect_beads(args.repo, args.output)
    receipt["database"] = collect_database(args.db, args.output)
    (args.output / "collection_receipt.json").write_text(json.dumps(receipt, indent=1))
    print(json.dumps(receipt, indent=1))


if __name__ == "__main__":
    main()
