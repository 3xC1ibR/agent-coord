#!/usr/bin/env python3
"""One-time legacy Ribbon Field cutover, launched outside the app by launchd.

Preparation is inert. Arm only after the release owner supplies validated final
artifacts and a readiness message. A claimed execution is never automatically
retried, including after failure. Database backups are evidence, not automatic
rollback: terminal workers may continue writing throughout the maintenance.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid


CONTINUATION = (
    "The user authorized a one-time Ribbon Field maintenance interruption. "
    "The app has now been updated. Continue the existing assignment in this same "
    "conversation. First inspect the latest checkpoint, files, jobs and external "
    "effects: a tool may have been interrupted after partially succeeding. Do not "
    "blindly repeat tool calls or create replacement agents. Preserve unanswered "
    "approval/questions and ask again if needed. Old queued messages remain "
    "preserved and may need review; do not bulk replay uncertain queue items. "
    "Do not repeat the rollout: its detached controller may still be restoring "
    "other conversations. Inspect the report before taking further rollout actions. "
)


def write_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex)
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
        json.dump(value, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def read_json(path):
    return json.loads(Path(path).read_text())


def same_process(before, current):
    # Parent PID and argv may change when a child is orphaned or execs a tool.
    # Such a process still belongs to the captured lifetime until it exits.
    return current is not None and all(before[key] == current[key] for key in ("pid", "uid", "birth"))


def tree_hash(root):
    """Hash regular content, symlink targets and executable bits without following links."""
    root = Path(root)
    digest = hashlib.sha256()
    for directory, directories, files in os.walk(root, followlinks=False):
        for name in sorted(directories + files):
            path = Path(directory) / name
            relative = str(path.relative_to(root))
            digest.update(relative.encode() + b"\0")
            if path.is_symlink():
                digest.update(b"link\0" + os.readlink(path).encode() + b"\0")
            elif path.is_file():
                digest.update(b"file\0" + str(path.stat().st_mode & 0o111).encode() + b"\0")
                with path.open("rb") as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(block)
            elif path.is_dir():
                digest.update(b"dir\0")
            else:
                raise RuntimeError(f"Unexpected special file: {relative}")
        directories.sort()
    return digest.hexdigest()


def bundle_info(path):
    with (Path(path) / "Contents/Info.plist").open("rb") as stream:
        info = plistlib.load(stream)
    return {key: info[key] for key in (
        "CFBundleIdentifier", "CFBundleExecutable", "CFBundleShortVersionString", "CFBundleVersion")}


def clean_environment(resources):
    environment = dict(os.environ)
    for key in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "CONDA_PREFIX"):
        environment.pop(key, None)
    environment.update(PYTHONPATH=str(Path(resources) / "backend"), PYTHONNOUSERSITE="1")
    return environment


def database_snapshot(database):
    """Read-only recovery evidence; never initialize a newer store under the old app."""
    uri = Path(database).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=30)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")
        rows = connection.execute("""SELECT t.*, b.client, s.turn_active
            FROM work_threads t JOIN browser_sessions b ON b.thread_id=t.thread_id
            JOIN sessions s ON s.session_id=t.thread_id""").fetchall()
        result = {}
        for row in rows:
            item = dict(row)
            tid = item["thread_id"]
            for key, query in (
                ("checkpoint", "SELECT * FROM thread_checkpoints WHERE thread_id=? ORDER BY id DESC LIMIT 1"),
                ("wake", "SELECT * FROM browser_inbox_wake WHERE thread_id=?"),
                ("closing", "SELECT * FROM thread_close_requests WHERE thread_id=? AND status IN ('queued','closing') LIMIT 1"),
            ):
                found = connection.execute(query, (tid,)).fetchone()
                item[key] = dict(found) if found else None
            completion = connection.execute("SELECT * FROM turn_completions WHERE thread_id=? AND turn_key=? ORDER BY id DESC LIMIT 1",
                                            (tid, item["turn_key"])).fetchone()
            item["completion"] = dict(completion) if completion else None
            item["queue"] = [dict(q) for q in connection.execute(
                "SELECT * FROM browser_message_queue WHERE thread_id=? ORDER BY created_at,rowid", (tid,))]
            result[tid] = item
        return result


def restore_decision(before, after, *, interrupted_by_controller=False):
    if after is None:
        return "missing"
    if after["attention"] == "archived" or after.get("closing"):
        return "closed_or_closing"
    if before.get("turn_key") != after.get("turn_key"):
        return "newer_turn"
    if (after.get("completion") or {}).get("status") == "completed":
        return "completed"
    wake = after.get("wake") or {}
    if wake.get("dispatching"):
        return "uncertain_wake"
    if wake.get("pause_kind") == "stop" and not interrupted_by_controller:
        return "unconfirmed_stop"
    return "continue"


def accepted_stop_matches(record, current, attempt):
    """An interrupt receipt can precede persistence of its terminal event."""
    observed = attempt.get("observed_after_stop") or {}
    if (attempt.get("stop") != "accepted" or current is None
            or observed.get("turn_key") != record.get("turn_key")
            or current.get("turn_key") != record.get("turn_key")):
        return False
    completion = current.get("completion") or {}
    if completion.get("status") not in (None, "interrupted", "failed"):
        return False
    completed_at = completion.get("completed_at")
    return completed_at is None or completed_at >= attempt["stop_started_at"]


class MacOperations:
    def __init__(self, plan):
        self.plan = plan

    def run(self, argv, *, timeout=120, environment=None):
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=environment)
        if result.returncode:
            raise RuntimeError(f"{Path(argv[0]).name} failed ({result.returncode}): {result.stderr[-2000:]}")
        return result.stdout.strip()

    def verify_bundle(self, path):
        self.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(path)])
        self.run(["/usr/sbin/spctl", "--assess", "--type", "execute", str(path)])
        self.run(["/usr/bin/xcrun", "stapler", "validate", str(path)])

    def processes(self):
        output = self.run(["/bin/ps", "-axo", "pid=,ppid=,uid=,lstart=,command="])
        found = {}
        for line in output.splitlines():
            fields = line.split(None, 8)
            if len(fields) == 9:
                found[int(fields[0])] = {"pid": int(fields[0]), "ppid": int(fields[1]),
                    "uid": int(fields[2]), "birth": " ".join(fields[3:8]), "command": fields[8]}
        return found

    def legacy(self):
        app = Path(self.plan["installed"])
        executable = str(app / "Contents/MacOS" / self.plan["installed_info"]["CFBundleExecutable"])
        processes = self.processes()
        native = [p for p in processes.values() if p["uid"] == os.getuid() and p["command"] == executable]
        if len(native) != 1:
            raise RuntimeError("Expected exactly one native process at the installed bundle path")
        native = native[0]
        backend_path = str(app / "Contents/Resources/backend.py")
        database = self.plan["database"]
        backends = [p for p in processes.values() if p["uid"] == os.getuid() and p["ppid"] == native["pid"]
                    and backend_path in p["command"] and ("--db " + database) in p["command"]]
        if len(backends) != 1:
            raise RuntimeError("Cannot bind the legacy backend to the native app and canonical database")
        backend = backends[0]
        ports = self.run(["/usr/sbin/lsof", "-nP", "-a", "-p", str(backend["pid"]), "-iTCP", "-sTCP:LISTEN", "-Fn"])
        addresses = [line[1:] for line in ports.splitlines() if line.startswith("n127.0.0.1:")]
        if len(addresses) != 1:
            raise RuntimeError("Expected one loopback listener owned by the verified legacy backend")
        owned = {backend["pid"]}
        while True:
            children = {p["pid"] for p in processes.values() if p["ppid"] in owned}
            expanded = owned | children
            if expanded == owned:
                break
            owned = expanded
        providers = [p for p in processes.values() if p["pid"] in owned and p["pid"] != backend["pid"]]
        return {"native": native, "backend": backend, "providers": providers,
                "url": "http://" + addresses[0] + "/"}

    def api(self, url, path, body=None):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port:
            raise RuntimeError("Control URL must be an explicit IPv4 loopback address")
        headers = {}
        if body is not None:
            config = self.api(url, "api/browser/config")
            headers["X-Agent-Coord-Token"] = config["token"]
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(urllib.parse.urljoin(url, path),
            data=json.dumps(body).encode() if body is not None else None, headers=headers)
        # Never log the config token, and never retry a mutation after a lost response.
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)

    def inventory(self, url):
        config = self.api(url, "api/browser/config")
        if config.get("workspaceRoot") is not None:
            raise RuntimeError("The legacy backend is workspace-scoped; global cutover is not supported")
        rows = self.api(url, "api/browser/sessions")["data"]
        return rows + self.api(url, "api/browser/sessions?archived=true")["data"]

    def backup_database(self, target):
        source = sqlite3.connect(Path(self.plan["database"]).as_uri() + "?mode=ro", uri=True, timeout=30)
        destination = sqlite3.connect(target)
        try:
            source.backup(destination)
            if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Database backup failed integrity check")
        finally:
            destination.close()
            source.close()

    def copy_bundle(self, source, target):
        if Path(target).exists():
            raise RuntimeError("Refusing to overwrite an existing backup or staging bundle")
        self.run(["/usr/bin/ditto", str(source), str(target)], timeout=600)

    def stop_legacy(self, identity):
        # No PID is signalled. The Apple event targets the verified installed app.
        refreshed = self.legacy()
        if any(refreshed[key] != identity[key] for key in ("native", "backend", "url")):
            raise RuntimeError("Legacy process identity changed before shutdown")
        # Include provider roots created after the earlier preflight.
        # A previously captured tool may already have been orphaned by Stop.
        # Keep both snapshots so reparenting cannot hide a surviving process.
        owned = [identity["native"], identity["backend"],
                 *identity["providers"], *refreshed["providers"]]
        self.run(["/usr/bin/osascript", "-e", "on run argv", "-e", "tell application (item 1 of argv) to quit",
                  "-e", "end run", self.plan["installed"]], timeout=60)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            current = self.processes()
            if all(not same_process(process, current.get(process["pid"])) for process in owned):
                return
            time.sleep(.2)
        raise RuntimeError("Legacy app/backend did not exit; replacement was not attempted")

    def install(self, candidate, directory):
        installed = Path(self.plan["installed"])
        staging = installed.with_name(".rf-rollout-" + self.plan["id"] + ".app")
        self.copy_bundle(candidate, staging)
        if tree_hash(staging) != self.plan["candidate_sha256"]:
            raise RuntimeError("Installed staging content differs from approved candidate")
        old = directory / "previous-installed.app"
        installed.rename(old)
        try:
            staging.rename(installed)
        except BaseException:
            old.rename(installed)
            raise

    def runtime(self, action):
        resources = Path(self.plan["installed"]) / "Contents/Resources"
        arguments = [str(resources / "runtime/bin/python3"), "-B", str(resources / "backend.py"),
                     "--db", self.plan["database"]]
        arguments += (["--runtime-bootstrap", "--resources", str(resources), "--runtime-start-paused"]
                      if action == "bootstrap" else ["--runtime-command", action])
        return json.loads(self.run(arguments, environment=clean_environment(resources)))

    def open_app(self):
        self.run(["/usr/bin/open", "-a", self.plan["installed"]])

    def confirm_continuation(self, receipt):
        if not receipt.get("dispatched"):
            return bool((receipt.get("turn") or {}).get("id"))
        request = receipt.get("receipt") or {}
        if not isinstance(request.get("id"), int):
            return False
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            with closing(sqlite3.connect(Path(self.plan["database"]).as_uri() + "?mode=ro", uri=True)) as db:
                row = db.execute("SELECT state,turn_id FROM dispatch_requests WHERE id=?", (request["id"],)).fetchone()
            if not row or row[0] in {"uncertain", "failed", "cancelled", "dismissed"}:
                return False
            if row[0] == "accepted":
                return bool(row[1])
            time.sleep(.25)
        return False


class Controller:
    def __init__(self, plan, operations=None):
        self.plan = plan
        self.directory = Path(plan["directory"])
        self.operations = operations or MacOperations(plan)
        self.state = {"status": "prepared", "phase": "prepared", "attempts": {}, "errors": []}

    def save(self, phase=None):
        if phase:
            self.state["phase"] = phase
        self.state["updated_at"] = time.time()
        write_json(self.directory / "state.json", self.state)

    def preflight(self):
        plan = self.plan
        if not isinstance(plan.get("readiness_message_id"), int) or plan["readiness_message_id"] <= 13330:
            raise RuntimeError("A final validated readiness reply is required before arming")
        if not plan.get("authorization"):
            raise RuntimeError("Missing saved user authorization")
        for key in ("installed", "candidate", "database", "directory"):
            path = Path(plan[key])
            if not path.is_absolute() or str(path.resolve()) != str(path):
                raise RuntimeError(f"{key} must be an absolute canonical path")
        installed, candidate, directory = (Path(plan[key]) for key in ("installed", "candidate", "directory"))
        if installed == candidate or any(directory.is_relative_to(bundle) for bundle in (installed, candidate)):
            raise RuntimeError("Preparation and candidate paths must be outside the replaceable installation")
        if installed.parent.stat().st_dev != directory.stat().st_dev:
            raise RuntimeError("Recovery directory must share the installation filesystem for atomic bundle moves")
        for key in ("installed", "candidate"):
            path = Path(plan[key])
            if bundle_info(path) != plan[key + "_info"] or tree_hash(path) != plan[key + "_sha256"]:
                raise RuntimeError(f"{key} identity/content changed")
        if plan["installed_info"]["CFBundleIdentifier"] != plan["candidate_info"]["CFBundleIdentifier"]:
            raise RuntimeError("Candidate has a different bundle identity")
        resources = Path(plan["candidate"]) / "Contents/Resources"
        if not (resources / "runtime/bin/python3").is_file():
            raise RuntimeError("Candidate must contain a standalone Python runtime")
        metadata = read_json(Path(plan["installed"]) / "Contents/Resources/backend.json")
        if str(Path(metadata.get("database", "")).resolve()) != plan["database"]:
            raise RuntimeError("Installed app database differs from the approved canonical database")
        self.operations.verify_bundle(plan["candidate"])
        identity = self.operations.legacy()
        self.operations.inventory(identity["url"])
        return identity

    def execute(self):
        # O_EXCL is a durable failure/success guard as well as a concurrency guard.
        try:
            descriptor = os.open(self.directory / "execution-claimed", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise RuntimeError("This cutover was already attempted; inspect state.json instead of replaying it")
        os.close(descriptor)
        try:
            legacy = self.preflight()
            self.state["legacy"] = legacy
            self.save("backing_up")
            candidate = self.directory / "candidate.app"
            self.operations.copy_bundle(self.plan["candidate"], candidate)
            self.operations.copy_bundle(self.plan["installed"], self.directory / "previous.app")
            if tree_hash(candidate) != self.plan["candidate_sha256"]:
                raise RuntimeError("Detached candidate copy differs from validated candidate")
            if tree_hash(self.directory / "previous.app") != self.plan["installed_sha256"]:
                raise RuntimeError("Rollback bundle copy differs from the old installation")
            self.operations.backup_database(self.directory / "before.sqlite3")
            # Cover turns admitted while either the DB or live inventory is read.
            self.state["boundary_started_at"] = time.time()
            before = database_snapshot(self.plan["database"])
            live = self.operations.inventory(legacy["url"])
            roster = {row["thread_id"]: before[row["thread_id"]] for row in live
                      if row["status"] in {"running", "needs input"} and row["thread_id"] in before}
            self.state["roster"] = roster
            self.save("stopping")
            # Stop each observed turn and journal acceptance. A failed/ambiguous
            # stop is retained for review; app shutdown still stops owned work.
            for tid, record in roster.items():
                if record["attention"] == "archived" or record.get("closing"):
                    continue
                current = database_snapshot(self.plan["database"]).get(tid)
                if current is None or current.get("turn_key") != record.get("turn_key"):
                    continue
                if (current.get("completion") or {}).get("status") == "completed":
                    continue
                self.state["attempts"][tid] = {"stop": "attempting", "stop_started_at": time.time()}
                self.save()
                try:
                    self.operations.api(legacy["url"], "api/browser/sessions/" + tid + "/interrupt", {})
                    self.state["attempts"][tid]["stop"] = "accepted"
                    observed = database_snapshot(self.plan["database"]).get(tid)
                    # Bind the receipt to the actual post-interrupt turn, since
                    # the legacy HTTP Stop route has no expected-turn guard.
                    self.state["attempts"][tid]["observed_after_stop"] = observed
                except Exception as exc:
                    self.state["attempts"][tid]["stop"] = "uncertain"
                    self.state["errors"].append(f"Stop {tid}: {exc}")
                self.save()
            self.operations.stop_legacy(legacy)
            self.save("legacy_stopped")
            self.operations.backup_database(self.directory / "stopped.sqlite3")
            stopped = database_snapshot(self.plan["database"])
            write_json(self.directory / "stopped-threads.json", stopped)
            # Account for work admitted during the legacy app's unavoidable
            # stop window. Unknown stop intent remains review-required.
            for tid, record in stopped.items():
                if record.get("turn_started_at", 0) and record["turn_started_at"] >= self.state["boundary_started_at"]:
                    roster[tid] = record
            self.state["roster"] = roster
            self.save("installing")
            self.operations.install(candidate, self.directory)
            self.save("bootstrapping")
            runtime = self.operations.runtime("bootstrap")
            self.verify_runtime(runtime)
            self.state["runtime"] = runtime
            self.operations.open_app()
            status = self.operations.runtime("status")
            self.verify_runtime(status)
            if status["instance"] != runtime["instance"]:
                raise RuntimeError("Runtime ownership changed while opening the app")
            self.save("installed_paused")
            resumed = self.operations.runtime("resume")
            if (resumed.get("database") != runtime["database"] or resumed.get("instance") != runtime["instance"]
                    or resumed.get("paused") is not False or resumed.get("pending")):
                raise RuntimeError("Resume did not confirm the same candidate instance admitting work")
            self.save("restoring")
            for tid, record in roster.items():
                attempt = self.state["attempts"].setdefault(tid, {})
                current = database_snapshot(self.plan["database"]).get(tid)
                own_stop = accepted_stop_matches(record, current, attempt)
                decision = restore_decision(record, current, interrupted_by_controller=own_stop)
                if decision != "continue":
                    attempt["resume"] = "skipped:" + decision
                    if decision in {"missing", "uncertain_wake", "unconfirmed_stop"}:
                        self.state["errors"].append(f"Continuation needs review for {tid}: {decision}")
                    self.save()
                    continue
                live = {row["thread_id"]: row for row in self.operations.inventory(runtime["url"])}
                if live.get(tid, {}).get("status") in {"running", "needs input"}:
                    attempt["resume"] = "already_active"
                    self.save()
                    continue
                attempt["resume"] = "attempting"
                self.save()
                try:
                    receipt = self.operations.api(runtime["url"], "api/browser/sessions/" + tid + "/messages", {
                        "message": CONTINUATION + "Rollout report: " + str(self.directory / "state.json"),
                        "askDispatcher": True, "requestId": self.plan["id"] + ":" + tid,
                    })
                    confirmed = self.operations.confirm_continuation(receipt)
                    attempt.update(resume="accepted" if confirmed else "pending_review", receipt=receipt)
                    if not confirmed:
                        self.state["errors"].append(f"Continuation for {tid} has no confirmed provider turn; inspect receipt without retrying")
                except Exception as exc:
                    attempt["resume"] = "uncertain"
                    self.state["errors"].append(f"Continue {tid}: {exc}")
                self.save()
            self.state["status"] = "installed_with_followup" if self.state["errors"] else "installed"
            self.save("finished")
        except BaseException as exc:
            self.state["status"] = "failed"
            self.state["errors"].append(str(exc))
            self.save()
            # Never restore the shared DB or start the old app over an unknown
            # candidate runtime/schema. Retain all bundles and evidence.
            raise

    def verify_runtime(self, runtime):
        if runtime.get("database") != self.plan["database"] or runtime.get("paused") is not True:
            raise RuntimeError("Candidate runtime is not paused on the approved database")
        if runtime.get("status") != "serving" or not runtime.get("instance"):
            raise RuntimeError("Candidate did not report authenticated serving ownership")
        if runtime.get("busy"):
            raise RuntimeError("Candidate runtime admitted work before restoration")
        if runtime.get("pending") or runtime.get("protocol") != 1 or runtime.get("schema") != 1:
            raise RuntimeError("Candidate runtime protocol/schema or pending upgrade does not match the rollout")
        resources = Path(self.plan["installed"]) / "Contents/Resources"
        payload = Path(runtime.get("payload", "")).resolve()
        expected_parent = Path(self.plan["database"]).parent / ("." + Path(self.plan["database"]).name + ".runtime/payloads")
        if payload.parent != expected_parent or not payload.is_dir():
            raise RuntimeError("Runtime payload is not retained under the canonical database")
        if read_json(payload / "backend.json") != read_json(resources / "backend.json"):
            raise RuntimeError("Runtime metadata differs from the installed candidate")
        if tree_hash(payload / "backend") != tree_hash(resources / "backend"):
            raise RuntimeError("Runtime backend content differs from the installed candidate")


def prepare(args):
    directory = Path(args.directory).expanduser().resolve()
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    installed = Path(args.installed).expanduser().resolve()
    candidate = Path(args.candidate).expanduser().resolve()
    plan = {"id": "rf-" + uuid.uuid4().hex, "installed": str(installed), "candidate": str(candidate),
            "database": str(Path(args.database).expanduser().resolve()), "directory": str(directory),
            "installed_info": bundle_info(installed), "candidate_info": bundle_info(candidate),
            "installed_sha256": tree_hash(installed), "candidate_sha256": tree_hash(candidate),
            "readiness_message_id": args.readiness_message,
            "authorization": "User authorized one-time tool interruption and stop/update/resume in thread 01a183d8-8395-7633-8e1c-d34aaf5310d8."}
    write_json(directory / "plan.json", plan)
    shutil.copyfile(Path(__file__), directory / "controller.py")
    Controller(plan).preflight()
    return {"status": "prepared", "plan": str(directory / "plan.json"), "candidate_sha256": plan["candidate_sha256"]}


def arm(plan_path):
    plan = read_json(plan_path)
    controller = Controller(plan)
    controller.preflight()
    directory = Path(plan["directory"])
    if (directory / "execution-claimed").exists() or (directory / "armed").exists():
        raise RuntimeError("This rollout is already armed or attempted")
    label = "com.agentcoord.rollout." + plan["id"]
    interpreter = Path(sys.executable).resolve()
    if any(interpreter.is_relative_to(Path(plan[key])) for key in ("installed", "candidate")):
        raise RuntimeError("Detached controller Python must live outside replaceable app bundles")
    controller.operations.run([str(interpreter), "-I", "-c", "import plistlib, sqlite3, urllib.request, hashlib"])
    plist = {"Label": label, "RunAtLoad": True, "KeepAlive": False,
             "ProgramArguments": [str(interpreter), "-I", str(directory / "controller.py"), "execute", str(directory / "plan.json")],
             "WorkingDirectory": str(directory), "StandardOutPath": str(directory / "controller.log"),
             "StandardErrorPath": str(directory / "controller.log"), "ProcessType": "Background"}
    with (directory / "launchd.plist").open("wb") as stream:
        plistlib.dump(plist, stream)
    write_json(directory / "armed", {"label": label, "at": time.time()})
    controller.operations.run(["/bin/launchctl", "bootstrap", f"gui/{os.getuid()}", str(directory / "launchd.plist")])
    return {"status": "armed", "label": label, "report": str(directory / "state.json")}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    preparation = commands.add_parser("prepare", help="Verify and record the final candidate without stopping work")
    for key in ("installed", "candidate", "database", "directory"):
        preparation.add_argument("--" + key, required=True)
    preparation.add_argument("--readiness-message", required=True, type=int)
    for command in ("preflight", "arm", "execute"):
        commands.add_parser(command).add_argument("plan")
    args = parser.parse_args(argv)
    if args.command == "prepare":
        result = prepare(args)
    elif args.command == "arm":
        result = arm(args.plan)
    else:
        controller = Controller(read_json(args.plan))
        result = controller.preflight() if args.command == "preflight" else controller.execute()
    print(json.dumps(result or {"status": "complete"}))


if __name__ == "__main__":
    main()
