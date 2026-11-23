#!/usr/bin/env python3
"""Bind a private signed-workload result to the bytes of one macOS release attempt."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing

REPO = "3xC1ibR/agent-coord"
OPERATOR_ID = 111322275
WORKFLOW = ".github/workflows/release-macos.yml"
ROOT = Path(__file__).resolve().parents[1]
ARCHITECTURES = ("arm64", "x86_64")
DESCRIPTION = "Signed upgrade passed the private interactive workload"
# Resource digests from the two notarized 0.1.12+63 update archives published by run 45079465559.
AFFECTED_PAYLOADS = {
    "arm64": "0b429828658aee1d9fffa3e3ed4e9f0155ec2dd39810fff733773d75775de541",
    "x86_64": "4e25f00e09d97f326bcdc8c0d25559d53a323c61b0e124b8487887bb2a53a2f0",
}


def github(route, *args):
    output = subprocess.check_output(
        ["gh", "api", "--hostname", "github.com", route, *args], text=True, timeout=60,
    )
    return json.loads(output)


def release_run(run_id):
    if not re.fullmatch(r"[1-9][0-9]*", str(run_id)):
        raise ValueError("Invalid GitHub Actions run ID")
    run = github(f"repos/{REPO}/actions/runs/{run_id}")
    if (run["id"] != int(run_id) or run["path"] != WORKFLOW
            or run["repository"]["full_name"] != REPO or run["head_repository"]["full_name"] != REPO
            or run["event"] not in {"push", "workflow_dispatch"}
            or type(run["run_attempt"]) is not int or run["run_attempt"] < 1
            or not re.fullmatch("[0-9a-f]{40}", run["head_sha"])):
        raise ValueError("GitHub run is not a release of this repository")
    return run


def context(run, version, build, architecture, archive_digest, dmg_digest):
    if (architecture not in ARCHITECTURES or not re.fullmatch(r"\d+\.\d+\.\d+", version)
            or not re.fullmatch(r"[1-9]\d*", str(build))
            or not all(re.fullmatch("[0-9a-f]{64}", d) for d in (archive_digest, dmg_digest))):
        raise ValueError("Invalid signed release artifact identity")
    identity = [1, REPO, WORKFLOW, run["id"], run["run_attempt"], run["head_sha"],
                version, str(build), architecture, archive_digest, dmg_digest]
    fingerprint = hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
    return f"rf/signed-workload/{architecture}/{fingerprint}"


def target(run):
    return f"https://github.com/{REPO}/actions/runs/{run['id']}/attempts/{run['run_attempt']}"


def status_is_valid(status, run):
    return (status["state"] == "success" and status["creator"]["id"] == OPERATOR_ID
            and status["target_url"] == target(run) and status["description"] == DESCRIPTION)


def pending_contexts(run, expected):
    pending = set(expected)
    page = 1
    while pending:
        statuses = github(f"repos/{REPO}/commits/{run['head_sha']}/statuses?per_page=100&page={page}")
        for status in statuses:  # API returns newest first; a failed or superseding status blocks.
            if status["context"] in pending:
                if not status_is_valid(status, run):
                    raise ValueError("Invalid or superseded operator workload receipt")
                pending.remove(status["context"])
        if len(statuses) < 100:
            break
        page += 1
    return pending


def require_release_receipts(release, wait_seconds=0):
    run_id = release["run_url"].rsplit("/", 1)[-1]
    run = release_run(run_id)
    if (release["source_commit"] != run["head_sha"]
            or os.environ.get("GITHUB_RUN_ID", run_id) != run_id
            or os.environ.get("GITHUB_RUN_ATTEMPT", str(run["run_attempt"])) != str(run["run_attempt"])):
        raise ValueError("Release source or workflow attempt mismatch")
    expected = {context(run, release["version"], release["build_number"], arch,
                        release["updates"][arch]["sha256"], release["downloads"][arch]["sha256"])
                for arch in ARCHITECTURES}
    deadline = time.monotonic() + wait_seconds
    while pending_contexts(run, expected):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("Missing authenticated signed-workload receipts for this release attempt")
        print("Waiting for operator Mac workload validation", file=sys.stderr, flush=True)
        time.sleep(min(60, remaining))
    if release_run(run_id)["run_attempt"] != run["run_attempt"]:
        raise ValueError("Release was rerun during workload validation")


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def database_bytes(database):
    """Logical SQLite size, including committed pages that remain in the WAL."""
    with closing(sqlite3.connect(database.resolve(strict=True).as_uri() + "?mode=ro", uri=True)) as db:
        return db.execute("SELECT page_count * page_size FROM pragma_page_count, pragma_page_size").fetchone()[0]


def verify_report(report, *, candidate, version, build, architecture, digest, snapshot_bytes):
    if (report.get("schema_version") != 1 or report.get("passed") is not True
            or report.get("workload_contract") != "ribbon-field-saved-interaction-v1"
            or report.get("mode") != "signed_guarded_upgrade" or report.get("guarded_update_phase") != "committed"):
        raise ValueError("Signed guarded upgrade did not pass")
    if any(v.get("phase") != "baseline" for v in report["violations"]):
        raise ValueError("Candidate or guarded upgrade has a recorded violation")
    signed = report["signed_inputs"][1]
    served = report["candidate_runtime"]
    if (Path(signed["application"]).resolve() != candidate.resolve()
            or signed["version"] != f"{version}+{build}" or signed["payload_digest"] != digest
            or served["payload"] != digest or served["architecture"] != architecture):
        raise ValueError("The candidate actually served does not match the extracted archive")
    proof = report["baseline_proof"]
    if (proof["version"] != "0.1.12+63" or proof["red"] is not True
            or proof["architecture"] != architecture or proof["payload_digest"] != AFFECTED_PAYLOADS[architecture]):
        raise ValueError("Missing affected signed-build evidence for this architecture")
    workload = report["workload"]
    fixture_bytes = workload.get("fixture_database_bytes")
    if (snapshot_bytes <= 0 or workload.get("source_database_bytes") != snapshot_bytes
            or type(fixture_bytes) is not int or fixture_bytes < snapshot_bytes):
        raise ValueError("The guarded upgrade did not exercise the full workload database size")
    verify_workload(report)
    coverage = report["workload"]["coverage"]
    accepted = 0
    unique = set()
    for receipt in report["receipts"]:
        key = receipt["request_id"]
        outcome = receipt["http_outcome"]
        deliveries = receipt["provider_deliveries"]
        durable = receipt["durable_states"]
        completed_once = type(deliveries) is int and deliveries == 1 and durable == ["completed"]
        absent = type(deliveries) is int and deliveries == 0 and not durable
        if (not key or key in unique or not (completed_once or absent)
                or outcome not in {"accepted", "rejected", "uncertain"}
                or (outcome == "accepted" and not completed_once)
                or (outcome == "rejected" and not absent)):
            raise ValueError("Missing, duplicated or inconsistent accepted work")
        unique.add(key)
        accepted += outcome == "accepted"
    if accepted != coverage["accepted_verified"]:
        raise ValueError("Accepted count does not match the verified provider receipts")



def verify_workload(report):
    histories = report["saved_histories"]
    bands = {"medium": (256 * 1024, 10 * 1024**2 - 1), "large": (10 * 1024**2, 36 * 1024**2)}
    if len(histories) != 2 or {h["size_band"] for h in histories} != set(bands):
        raise ValueError("Representative saved conversation histories are missing")
    if len({h["fixture_id"] for h in histories}) != 2:
        raise ValueError("Saved history fixtures must be distinct")
    for history in histories:
        minimum, maximum = bands[history["size_band"]]
        if (history["turns"] < 3 or any(type(history[k]) is not int or not minimum <= history[k] <= maximum
                                        for k in ("source_bytes", "sanitized_bytes"))):
            raise ValueError("Saved history size or turn count is outside its workload band")
    reopened = {r.get("fixture") for r in report["samples"] if r["phase"] == "candidate"
                and r["kind"] == "history" and r["outcome"] == "success"}
    if not {h["fixture_id"] for h in histories}.issubset(reopened):
        raise ValueError("Candidate did not reopen both sampled histories")
    for name in ("browser_open", "browser_send"):
        if report["browser_results"]["upgrade"][name]["attempts"] < 1:
            raise ValueError(f"Missing {name} attempts during the guarded upgrade")
    workload = report["workload"]
    if (type(workload["claude_streams"]) is not int or workload["claude_streams"] < 2
            or type(workload["initial_text_bytes_per_claude"]) is not int
            or workload["initial_text_bytes_per_claude"] < 10 * 1024**2):
        raise ValueError("Two incident-sized Claude output streams are required")
    identities = report["stream_identity_counts"]
    if len(identities) < 6 or any(type(n) is not int or n != 1 for n in identities.values()):
        raise ValueError("The active provider stream identities did not remain continuous")
    for phase, minimum in (("baseline", 15), ("upgrade", 1), ("candidate", 15)):
        observed = report["observed_stream_events"][phase]
        if any(type(observed.get(key)) is not int or observed[key] < minimum for key in identities):
            raise ValueError(f"Provider streams were not exercised throughout the {phase} phase")
    coverage = workload["coverage"]
    for key, minimum in {"saved_histories_reopened": 2, "active_streams": 6, "large_streams": 2,
                         "large_stream_bytes": 20 * 1024**2,
                         "accepted_verified": 1, "browser_sends": 1, "browser_opens": 1}.items():
        if type(coverage.get(key)) is not int or coverage[key] < minimum:
            raise ValueError(f"Representative workload did not exercise {key}")

def signed_code_hash(app):
    requirement = 'anchor apple generic and identifier "com.agentcoord.desktop" and certificate leaf[subject.OU] = "R6Z7YTP6YG"'
    subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", "-R=" + requirement, app], check=True)
    details = subprocess.run(["/usr/bin/codesign", "--display", "--verbose=3", app],
                             check=True, capture_output=True, text=True)
    match = re.search(r"^CDHash=([0-9a-f]{40})$", details.stderr, re.MULTILINE)
    if not match:
        raise ValueError("Missing signed application CodeDirectory hash")
    return match.group(1)


def verify_installer_app(candidate, dmg, directory):
    archive_signature = signed_code_hash(candidate)
    mount = directory / "mounted-installer"
    mount.mkdir()
    subprocess.run(["/usr/bin/hdiutil", "attach", "-readonly", "-nobrowse", "-mountpoint",
                    str(mount), str(dmg)], check=True, stdout=subprocess.DEVNULL)
    try:
        if signed_code_hash(mount / "Ribbon Field.app") != archive_signature:
            raise ValueError("Installer application differs from the signed update application")
    finally:
        subprocess.run(["/usr/bin/hdiutil", "detach", str(mount)], check=True, stdout=subprocess.DEVNULL)


def run_operator_gate(args):
    if platform.system() != "Darwin" or github("user")["id"] != OPERATOR_ID:
        raise ValueError("A signed workload receipt requires the authenticated operator Mac")
    run = release_run(args.run)
    head = subprocess.check_output(["git", "-C", ROOT, "rev-parse", "HEAD"], text=True).strip()
    if head != run["head_sha"]:
        raise ValueError("Use a clean checkout of the release commit to run its gate")
    subprocess.run(["git", "-C", ROOT, "diff", "--quiet", "HEAD", "--", "scripts", "plugins",
                    "desktop", "tests/fixtures"], check=True)
    report = args.report.resolve()
    report.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if subprocess.run(["git", "-C", str(report.parent), "rev-parse", "--is-inside-work-tree"],
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        raise ValueError("Write the private workload report outside Git")
    snapshot = args.snapshot.resolve(strict=True)
    if snapshot == (Path.home()/".local/state/agent-coord/state.sqlite3").resolve():
        raise ValueError("Pass an offline workload snapshot")
    if report.exists():
        raise ValueError("Pass an unused private report path")
    with tempfile.TemporaryDirectory(prefix="rf-signed-workload-") as temporary:
        directory = Path(temporary)
        subprocess.run(["gh", "run", "download", str(args.run), "--repo", REPO,
                        "--name", f"macos-release-{args.architecture}", "--dir", str(directory)], check=True)
        [archive] = directory.glob(f"Ribbon-Field-*-{args.architecture}.zip")
        version = archive.name.removeprefix("Ribbon-Field-").removesuffix(f"-{args.architecture}.zip")
        dmg = archive.with_suffix(".dmg")
        source = json.loads(dmg.with_suffix(".dmg.provenance.json").read_text())
        if (source["version"] != version or source["source_commit"] != head
                or source["run_url"] != f"https://github.com/{REPO}/actions/runs/{run['id']}"):
            raise ValueError("Downloaded provenance does not match the release run")
        status_context = context(run, version, source["build_number"], args.architecture,
                                 file_hash(archive), file_hash(dmg))
        subprocess.run(["/usr/bin/ditto", "-x", "-k", archive, directory/"extracted"], check=True)
        candidate = directory/"extracted/Ribbon Field.app"
        verify_installer_app(candidate, dmg, directory)
        sys.path.insert(0, str(ROOT/"plugins/agent-coord/scripts"))
        from agent_coord.desktop_runtime import payload_digest
        digest = payload_digest(candidate/"Contents/Resources")
        command = [sys.executable, ROOT/"scripts/validate_signed_runtime_responsiveness.py",
                   "--previous", args.previous.resolve(strict=True), "--candidate", candidate,
                   "--snapshot", snapshot, "--report", report]
        if args.affected_report:
            command.extend(["--affected-report", args.affected_report.resolve(strict=True)])
        if args.playwright_module:
            command.extend(["--playwright-module", args.playwright_module.resolve(strict=True)])
        subprocess.run(command, cwd=ROOT, check=True)
        verify_report(json.loads(report.read_text()), candidate=candidate, version=version,
                      build=source["build_number"], architecture=args.architecture, digest=digest,
                      snapshot_bytes=database_bytes(snapshot))
        if release_run(args.run)["run_attempt"] != run["run_attempt"]:
            raise ValueError("Release was rerun during validation")
        result = github(f"repos/{REPO}/statuses/{head}", "-f", "state=success", "-f",
                        f"context={status_context}", "-f", f"description={DESCRIPTION}", "-f",
                        f"target_url={target(run)}")
        if result.get("context") != status_context or not status_is_valid(result, run):
            raise ValueError("GitHub did not confirm an authenticated receipt")
        print(f"Verified workload receipt for run {run['id']} attempt {run['run_attempt']} ({args.architecture})")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=int, required=True)
    parser.add_argument("--architecture", choices=ARCHITECTURES, required=True)
    parser.add_argument("--previous", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--affected-report", type=Path)
    parser.add_argument("--playwright-module", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    lock_directory = Path.home()/".local/state/agent-coord/diagnostics"
    lock_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (lock_directory/"signed-workload-validation.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        run_operator_gate(args)


if __name__ == "__main__":
    main()
