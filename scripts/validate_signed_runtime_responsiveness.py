#!/usr/bin/env python3
"""Run a private signed coordinator upgrade and blue/green cutover qualification.

Supports both full two-bundle operator workload validation (--previous / --candidate / --snapshot)
and fast < 90s CI APFS-cloned blue/green cutover qualification (--qualify-ci <app>).
All traffic routes through the permanent loopback runtime_edge listener with zero gateway fixtures.
"""
from __future__ import annotations

import argparse
import collections
from contextlib import closing
from functools import lru_cache
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import plistlib
import re
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from measure_backend_latency import BUDGETS
from release_workload_receipt import MIN_ARCHIVE_ITEMS, MIN_ARCHIVE_TURNS, database_bytes
from runtime_upgrade_validation import EventCursor, Validation, process_command, processes_mentioning, runtime


METRIC_NAMES = {
    "send": "send_accepted",
    "inventory": "list",
    "history": "read_target",
    "detail": "thread_target",
    "sidecar": "sidecar_status",
    "search": "search",
}
BUDGETS_MS = {name: (BUDGETS[metric]["p50"], BUDGETS[metric]["p95"]) for name, metric in METRIC_NAMES.items()}
LARGE_OUTPUT_BYTES = 10 * 1024 * 1024
STABLE_SECONDS = 30
MINIMUM_SAMPLES = 12
MAXIMUM_GAP_SECONDS = 5
UPDATE_OBSERVATION_SECONDS = 600
UPDATE_PHASE_STALL_SECONDS = 120
QUALIFY_CUTOVER_LIMIT_MS = 250.0


def diagnostic_reason(error):
    message = str(error)
    for fragment, code in (
        ("three browser clients attached", "three-browser-attachments-missing"),
        ("Missing original pending", "pending-work-missing"),
        ("Pending approval, queue or original", "pending-work-changed"),
        ("original pending approval", "initial-pending-work-missing"),
        ("Original pending work did not complete", "pending-work-delivery-mismatch"),
        ("Could not verify the signed candidate", "serving-verification-timeout"),
        ("Accepted operations did not drain", "admission-drain-timeout"),
        ("Candidate exited before readiness", "candidate-exited"),
        ("Insufficient free space for the update rollback snapshot", "rollback-capacity"),
        ("Candidate validation failed", "candidate-validation"),
        ("did not confirm", "confirmation-missing"),
        ("database is locked", "sqlite-busy"),
        ("database or disk is full", "sqlite-full"),
        ("Connection refused", "connection-refused"),
        ("timed out", "timeout"),
        ("Timed out", "timeout"),
    ):
        if fragment in message or code in message:
            return code
    return getattr(error, "sqlite_errorname", type(error).__name__ if not isinstance(error, str) else "runtime-error")


def signed_resources(bundle):
    bundle = bundle.resolve(strict=True)
    subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(bundle)], check=True)
    info = plistlib.loads((bundle / "Contents/Info.plist").read_bytes())
    resources = bundle / "Contents/Resources"
    backend = json.loads((resources / "backend.json").read_bytes())
    version = backend["runtime_version"]
    if version != f"{info['CFBundleShortVersionString']}+{info['CFBundleVersion']}":
        raise ValueError("Bundle and backend version disagree")
    return resources, {
        "version": version,
        "payload_digest": runtime.payload_digest(resources),
        "application": str(bundle),
    }


def saved_inventory(snapshot):
    with closing(sqlite3.connect(snapshot.resolve(strict=True).as_uri() + "?mode=ro", uri=True)) as db:
        rows = db.execute(
            """
            SELECT s.client, t.attention, COUNT(*)
            FROM work_threads t JOIN sessions s ON s.session_id=t.thread_id
            WHERE t.attention != 'archived' GROUP BY s.client, t.attention
        """
        ).fetchall()
    if sum(row[2] for row in rows) < 650:
        raise ValueError("The retained incident workload contained at least 650 visible threads")
    return rows


def seed_inventory(database, root, rows):
    from agent_coord.store import CoordinationStore

    store = CoordinationStore(database)
    for client, attention, count in rows:
        for number in range(count):
            name = f"Inventory {client} {attention} {number}"
            thread = str(uuid.uuid5(uuid.NAMESPACE_OID, name))
            store.register(session_id=thread, client=client, cwd=str(root), name=name)
            with store._connection() as db:
                db.execute("UPDATE work_threads SET attention=? WHERE thread_id=?", (attention, thread))


def match_snapshot_size(snapshot, database):
    """Match source database byte size without requiring 2x rollback disk capacity on APFS."""
    source_bytes = database_bytes(snapshot)
    fixture_bytes = database_bytes(database)
    if fixture_bytes < source_bytes:
        batch_bytes = 8 * 1024**2
        extra_needed = source_bytes - fixture_bytes + 2 * batch_bytes
        if sys.platform != "darwin":
            extra_needed += source_bytes
        if shutil.disk_usage(database.parent).free < extra_needed:
            raise RuntimeError("Insufficient space for the full-size private upgrade fixture")
        with closing(sqlite3.connect(database, isolation_level=None)) as db:
            page_size = db.execute("PRAGMA page_size").fetchone()[0]
            db.execute("CREATE TABLE IF NOT EXISTS release_workload_storage (content BLOB NOT NULL)")
            while fixture_bytes < source_bytes:
                db.execute("INSERT INTO release_workload_storage VALUES (zeroblob(?))",
                           (min(batch_bytes, source_bytes - fixture_bytes),))
                fixture_bytes = db.execute("PRAGMA page_count").fetchone()[0] * page_size
    return {"source_database_bytes": source_bytes, "fixture_database_bytes": fixture_bytes}


def copy_saved_histories(snapshot, database, root, *, manifest_thread=None):
    from agent_coord.store import CoordinationStore
    from agent_coord import history_persistence as history

    store = CoordinationStore(database)
    sample_key = os.urandom(32)

    @lru_cache(maxsize=16384)
    def substitute(word):
        return hashlib.shake_128(sample_key + word.encode()).hexdigest((len(word) + 1) // 2)[: len(word)]

    fields = set(
        "id type role text content status items turns summary output aggregatedOutput exitCode command cwd "
        "durationMs name arguments result error path stdout stderr start end timestamp startedAt "
        "completedAt itemId turnId threadId title annotations images text_elements".split()
    )
    enums = set(
        "userMessage agentMessage commandExecution reasoning fileChange plan toolCall toolResult "
        "mcpToolCall webSearch enteredReviewMode exitedReviewMode contextCompaction image text "
        "input_text output_text user assistant system completed failed interrupted".split()
    )

    def redact(value, key=None):
        if isinstance(value, list):
            return [redact(v) for v in value]
        if isinstance(value, dict):
            return {(k if k in fields else substitute(k)): redact(v, k) for k, v in value.items()}
        if isinstance(value, str):
            if key == "cwd":
                return str(root)
            if key in {"type", "role", "status"} and value in enums:
                return value
            return re.sub(r"[^\W_]+", lambda m: substitute(m[0]), value)
        return value

    with store._connection() as db:
        db.execute(
            """CREATE TABLE IF NOT EXISTS browser_sessions (
            thread_id TEXT PRIMARY KEY, cwd TEXT NOT NULL, name TEXT NOT NULL,
            model TEXT, effort TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
            archived INTEGER NOT NULL DEFAULT 0)"""
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS browser_history (thread_id TEXT PRIMARY KEY, history_json TEXT NOT NULL)"
        )
        history.ensure_schema(db)
    refs = []
    with closing(sqlite3.connect(snapshot.resolve(strict=True).as_uri() + "?mode=ro", uri=True)) as source:
        source.execute("PRAGMA query_only=ON")
        has_browser_history = bool(
            source.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='browser_history'").fetchone()
        )
        has_thread_turns = bool(
            source.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='thread_turns'").fetchone()
        )
        legacy = (
            [
                r[0]
                for r in source.execute(
                    """
                SELECT h.thread_id FROM browser_history h JOIN browser_sessions b USING(thread_id)
                WHERE b.archived=0 AND length(CAST(h.history_json AS BLOB)) BETWEEN 10485760 AND 33554432
                ORDER BY b.updated_at DESC LIMIT 3"""
                )
            ]
            if has_browser_history
            else []
        )
        ids = legacy + ([manifest_thread] if manifest_thread else []) + [
            r[0]
            for r in source.execute(
                "SELECT thread_id FROM browser_sessions WHERE archived=0 ORDER BY updated_at DESC LIMIT 200"
            )
        ]
        from agent_coord.transcript_store import thread_directory as transcript_directory_for

        for source_id in dict.fromkeys(ids):
            row = (
                source.execute("SELECT history_json FROM browser_history WHERE thread_id=?", (source_id,)).fetchone()
                if has_browser_history
                else None
            )
            stored = None
            if row:
                stored = json.loads(row[0])
                storage_format = "manifest" if history.MARKER in stored else "legacy"
                if refs and refs[0]["size_band"] == "large":
                    if stored.get("layout") != "turns" or storage_format != "manifest":
                        continue
                    if manifest_thread and source_id != manifest_thread:
                        continue
                    archive = history._documents(source, [stored["root"]])[0]
                    if len(archive["turns"]) < MIN_ARCHIVE_TURNS:
                        continue
                raw = history.decode_history(row[0], db=source) if storage_format == "manifest" else stored
            elif has_thread_turns:
                turn_rows = source.execute(
                    "SELECT full_offset, full_length FROM thread_turns WHERE thread_id=? ORDER BY turn_seq ASC",
                    (source_id,),
                ).fetchall()
                if not turn_rows:
                    continue
                tdir = transcript_directory_for(snapshot.resolve(strict=True), source_id)
                full_file = tdir / "turns.full.jsonl"
                if not full_file.is_file():
                    continue
                meta_file = tdir / "meta.json"
                meta_obj = json.loads(meta_file.read_text(encoding="utf-8")) if meta_file.is_file() else {}
                loaded_turns = []
                fd = os.open(full_file, os.O_RDONLY)
                try:
                    for full_off, full_len in turn_rows:
                        chunk = os.pread(fd, int(full_len), int(full_off))
                        loaded_turns.append(json.loads(chunk.decode("utf-8", "surrogatepass")))
                finally:
                    os.close(fd)
                raw = {**(meta_obj if isinstance(meta_obj, dict) else {}), "id": source_id, "turns": loaded_turns}
                storage_format = "transcript"
            else:
                continue
            turns = raw.get("turns") or []
            if len(turns) < 3:
                continue
            size = len(json.dumps(raw, ensure_ascii=False).encode())
            band = (
                "large"
                if 10 * 1024**2 <= size <= 36 * 1024**2
                else "medium"
                if 256 * 1024 <= size < 10 * 1024**2
                else None
            )
            if not band or any(r["size_band"] == band for r in refs):
                continue
            items = sum(len(t.get("items", ())) for t in turns)
            if band == "medium":
                if storage_format == "manifest" and stored.get("layout") != "turns":
                    continue
                if storage_format not in {"manifest", "transcript"}:
                    continue
                if len(turns) < MIN_ARCHIVE_TURNS or items < MIN_ARCHIVE_ITEMS:
                    continue
                if manifest_thread and source_id != manifest_thread:
                    continue
            identity = str(uuid.uuid5(uuid.NAMESPACE_OID, "signed-gate-" + band))
            safe = {"id": identity, "cwd": str(root), "turns": redact(turns)}
            for turn in safe["turns"]:
                turn["status"] = "completed"
            sanitized_size = len(json.dumps(safe, ensure_ascii=False).encode())
            if band == "large" and not 10 * 1024**2 <= sanitized_size <= 36 * 1024**2:
                continue
            if band == "medium" and not 256 * 1024 <= sanitized_size < 10 * 1024**2:
                continue
            name = "Retained " + band + " conversation"
            store.register(session_id=identity, client="codex", cwd=str(root), name=name)
            with store._connection() as db:
                db.execute(
                    "INSERT INTO browser_sessions(thread_id,cwd,name,created_at,updated_at) VALUES(?,?,?,?,?)",
                    (identity, str(root), name, time.time(), time.time()),
                )
            if storage_format in {"manifest", "transcript"}:
                prepared = history.prepare_history(safe)
                history.stage_history(store, prepared)
                with store._connection() as db:
                    history.publish_history(db, identity, prepared)
            else:
                with store._connection() as db:
                    db.execute("INSERT INTO browser_history VALUES (?,?)", (identity, json.dumps(safe)))
            refs.append(
                {
                    "fixture_id": identity,
                    "size_band": band,
                    "source_bytes": size,
                    "sanitized_bytes": sanitized_size,
                    "turns": len(turns),
                    "items": items,
                    "last_turn": safe["turns"][-1]["id"],
                    "storage_format": storage_format,
                }
            )
            if len(refs) == 2:
                return refs
    raise ValueError("Snapshot needs a long block-backed medium conversation and a large saved conversation")


def fetch(url, token, route, body=None, timeout=20, method=None):
    headers = {"X-Agent-Coord-Token": token, "X-Agent-Coord-History-View": "recent"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        url + "api/browser/" + route,
        headers=headers,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


class SignedUpgrade(Validation):
    def __init__(self, args, previous, candidate):
        super().__init__(args.report, 1, "signed-responsiveness")
        self.old, self.new = previous, candidate
        self.profile_sample = getattr(args, "profile_sample", None)
        self.snapshot = args.snapshot
        self.manifest_thread = getattr(args, "saved_manifest_thread", None)
        self.affected_report = getattr(args, "affected_report", None)
        self.candidate_only = getattr(args, "candidate_only", False)
        self.browser_module = getattr(args, "playwright_module", None)
        self.browser = None
        self.browser_err = None
        self.report.update(
            schema_version=1,
            workload_contract="ribbon-field-saved-interaction-v1",
            passed=False,
            signed_inputs=[previous[1], candidate[1]],
            limits={"milliseconds": BUDGETS_MS, "max_gap_seconds_exclusive": MAXIMUM_GAP_SECONDS},
            violations=[],
            samples=[],
            receipts=[],
        )
        allowed = {
            "HOME", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "XDG_CONFIG_HOME",
            "XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "TMPDIR",
            "PYTHONDONTWRITEBYTECODE", "PYTHONNOUSERSITE", "RIBBON_FAKE_ROOT",
        }
        self.env = {k: v for k, v in self.env.items() if k in allowed}
        self.env["PATH"] = os.pathsep.join((str(self.bin), "/usr/bin", "/bin"))
        self.report["clock_sources"] = {
            "http": "Python time.monotonic, body fully consumed",
            "own_echo": "Performance timeOrigin plus monotonic DOM visibility, minus Enter event timestamp on the same page",
            "other_window": "Both pages run in the same browser, using Performance timeOrigin to compare monotonic clocks",
            "browser_availability": "single client Date.now; phase boundary sampled by the controller on the same host",
        }
        self.sources = saved_inventory(args.snapshot)
        self.report["inventory_profile"] = self.sources
        self.report["workload"] = {
            "streams": 6,
            "claude_streams": 2,
            "codex_streams": 4,
            "initial_text_bytes_per_claude": LARGE_OUTPUT_BYTES,
            "interval_seconds_per_stream": 1,
            "stable_phase_seconds": STABLE_SECONDS,
            "source_snapshot": str(args.snapshot.resolve()),
        }
        self.peers = []
        self.phase = "setup"
        self.address = None
        self.lock = threading.Lock()
        self.workers = []
        self.stop_traffic = threading.Event()

    def persist(self):
        self.output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", dir=self.output.parent, delete=False) as handle:
            json.dump(self.report, handle, indent=2)
            handle.write("\n")
        Path(handle.name).replace(self.output)

    def signed_helper(self, resources, arguments, *, body=None, timeout=90):
        command = [str(resources / "runtime/bin/python3"), "-B", str(resources / "backend.py"),
                   "--resources", str(resources), "--db", str(self.database), *arguments]
        result = subprocess.run(command, input=json.dumps(body) if body is not None else None,
                                capture_output=True, text=True, timeout=timeout, cwd=self.root,
                                env=dict(self.env, PYTHONPATH=str(resources / "backend")))
        if result.returncode:
            raise RuntimeError(f"Signed helper exited {result.returncode} ({diagnostic_reason(result.stderr)})")
        return json.loads(result.stdout)

    def launch_signed(self, resources, *, hold=False):
        arguments = ["--runtime-bootstrap"]
        if hold:
            arguments.append("--runtime-confirm-updates")
        return self.signed_helper(resources, arguments)

    def browser_attached(self, url, since, phase):
        path = self.root / "browser-observations.jsonl"
        if not path.exists():
            return False
        origin = url.rstrip("/")
        rows = (json.loads(line) for line in path.read_bytes().splitlines(keepends=True) if line.endswith(b"\n"))
        return {r["page"] for r in rows if r.get("kind") == "browser_attachment"
                and r.get("origin") == origin and r.get("phase") == phase and r.get("start", 0) >= since
                and r.get("status") == 200} == {"opening", "composing", "receiving"}

    def start(self):
        seed_inventory(self.database, self.root, self.sources)
        self.saved = copy_saved_histories(self.snapshot, self.database, self.root, manifest_thread=self.manifest_thread)
        if not self.candidate_only:
            self.report["workload"].update(match_snapshot_size(self.snapshot, self.database))
        self.report["saved_histories"] = self.saved
        self.open_targets = [r["fixture_id"] for r in self.saved]
        profile = {"raw_bytes": LARGE_OUTPUT_BYTES * 3 // 4, "interval_seconds": 1}
        (self.root / "stream-profile.json").write_text(json.dumps(profile))
        initial = self.new if self.candidate_only else self.old
        status = self.launch_signed(initial[0])
        if Path(status["payload"]).name != initial[1]["payload_digest"]:
            raise RuntimeError("Unexpected initial serving payload")
        self.report["initial_runtime"] = self.identity(status)
        self.address = status["url"], fetch(status["url"], "", "config", timeout=3)["token"]
        for index in range(6):
            client = "claude" if index >= 4 else "codex"
            session = fetch(*self.address, "sessions", {"cwd": str(self.root), "client": client, "name": f"Stream {index}"})
            tid = session["session"]["thread_id"]
            fetch(*self.address, f"sessions/{tid}/messages", {"message": "stream"})
            self.peers.append(tid)
        self.wait(lambda: sum(r["kind"] == "turn" for r in self.records()) == 6, timeout=30)
        self.turn_id = fetch(*self.address, "sessions/" + self.peers[0])["activeTurn"]
        self.composer = fetch(
            *self.address, "sessions", {"cwd": str(self.root), "client": "codex", "name": "Composer validation"}
        )["session"]["thread_id"]
        for _ in range(3):
            key = uuid.uuid4().hex
            fetch(*self.address, f"sessions/{self.peers[0]}/messages",
                  {"message": key, "requestId": key, "expectedTurnId": self.turn_id})
            self.report["samples"].append({
                "phase": "preflight", "kind": "send", "request_id": key,
                "start": time.monotonic(), "end": time.monotonic(), "outcome": "accepted",
            })
        if getattr(self, "exercise_pending_work", False):
            self.prepare_pending_work()
        (self.root / "start-streams").touch()

        def large_streams():
            return {r["thread"] for r in self.records() if r["kind"] == "output" and r.get("size", 0) >= LARGE_OUTPUT_BYTES}

        self.wait(lambda: len(large_streams()) == 2, timeout=60)
        consumed = {}

        def consumed_large_turn(tid):
            size = len(self.text(fetch(*self.address, f"sessions/{tid}")).encode())
            consumed[tid] = size
            return size >= LARGE_OUTPUT_BYTES

        for tid in self.peers[4:]:
            self.wait(lambda: consumed_large_turn(tid), timeout=60)
        self.report["large_stream_observations"] = consumed
        self.report["workload"]["coverage"] = {
            "saved_histories_reopened": 0,
            "active_streams": 6,
            "large_streams": len(large_streams()),
            "large_stream_bytes": sum(consumed.values()),
            "accepted_verified": 0,
            "browser_sends": 0,
            "browser_opens": 0,
        }
        self.report["projection_bytes"] = self.projection_bytes()
        self.persist()
        for name in BUDGETS_MS:
            thread = threading.Thread(target=self.worker, args=(name,), daemon=True)
            thread.start()
            self.workers.append(thread)

    def projection_bytes(self):
        with closing(sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True)) as db:
            size, cursor = db.execute("SELECT length(state),cursor FROM coordinator_projection").fetchone()
        return {"compressed": size, "cursor": cursor}

    def identity(self, status):
        owner = json.loads(
            (runtime.runtime_directory(self.database) / "provider-transport" / "owner.json").read_bytes()
        )
        pid = status["pid"]
        payload = Path(status["payload"]).resolve(strict=True)
        if not payload.is_relative_to(self.root.resolve()):
            raise ValueError("Serving payload is outside the private test directory")
        arch = subprocess.check_output(
            [str(payload / "runtime/bin/python3"), "-I", "-B", "-c", "import platform; print(platform.machine())"],
            env=self.env, cwd=self.root, text=True,
        ).strip()
        command = process_command(pid)
        if not command or str(payload) not in command:
            raise AssertionError("Coordinator process does not execute the retained signed payload")
        return {
            "architecture": arch,
            "pid": pid,
            "instance": status["instance"],
            "payload": Path(status["payload"]).name,
            "process": command,
            "transport_pid": owner["pid"],
            "edge_pid": status.get("edge_pid"),
            "url": status["url"],
        }

    def worker(self, name):
        index = 0
        while not self.stop_traffic.is_set():
            phase, address = self.phase, self.address
            if phase == "setup":
                self.stop_traffic.wait(0.05)
                continue
            targets = self.open_targets + self.peers[:2]
            tid = targets[index % len(targets)]
            routes = {
                "inventory": "threads",
                "detail": "threads/" + tid,
                "history": "sessions/" + tid,
                "sidecar": f"threads/{tid}/sidecars",
                "search": "search?q=Inventory",
                "send": f"sessions/{self.peers[0]}/messages",
            }
            started = time.monotonic()
            entry = {"phase": phase, "kind": name, "start": started, "fixture": tid, "origin": address[0].rstrip("/")}
            payload = None
            if name == "send":
                key = uuid.uuid4().hex
                entry["request_id"] = key
                payload = {"message": key, "requestId": key, "expectedTurnId": self.turn_id}
            try:
                value = fetch(*address, routes[name], payload)
                if name == "inventory" and len(value["data"]) < 650:
                    raise ValueError("Inventory too small")
                if name == "history" and not value["thread"]["turns"]:
                    raise ValueError("Empty conversation")
                if name == "history" and tid in self.open_targets:
                    last = next(r["last_turn"] for r in self.saved if r["fixture_id"] == tid)
                    if not any(t["id"] == last for t in value["thread"]["turns"]):
                        raise ValueError("Saved conversation did not reopen its retained final turn")
                if name == "detail" and value["thread_id"] != tid:
                    raise ValueError("Incorrect thread")
                entry["outcome"] = "accepted" if name == "send" else "success"
            except urllib.error.HTTPError as error:
                with error:
                    entry.update(outcome="rejected", status=error.code,
                                 detail=diagnostic_reason(error.read(200).decode(errors="replace")))
            except Exception as error:
                entry.update(outcome="uncertain", detail=diagnostic_reason(error))
            entry["end"] = time.monotonic()
            with self.lock:
                self.report["samples"].append(entry)
            index += 1
            self.stop_traffic.wait(0.25)

    def publish_phase(self, name):
        routes = self.report.setdefault("interaction_routes", [])
        origin = self.address[0].rstrip("/")
        if not routes or (routes[-1]["phase"], routes[-1]["origin"]) != (name, origin):
            routes.append({"phase": name, "origin": origin, "at_ms": time.time() * 1000})
        self.phase = name
        temporary = self.root / "browser-state.partial"
        temporary.write_text(json.dumps({
            "phase": name, "url": self.address[0], "peers": self.open_targets,
            "composer": self.composer, "visit_origin": None,
        }))
        temporary.replace(self.root / "browser-state.json")
        if self.browser is None:
            self.browser_err = (self.root / "browser-errors.log").open("w")
            node = shutil.which("node")
            if not node:
                raise RuntimeError("Browser acceptance requires Node and Playwright")
            browser_cache = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or str(Path.home() / "Library/Caches/ms-playwright")
            self.browser = subprocess.Popen(
                [node, str(Path(__file__).with_name("observe_signed_conversation_interactions.mjs")),
                 str(self.root), self.browser_module or ""],
                stdout=self.browser_err, stderr=self.browser_err, cwd=self.root,
                env=dict(self.env, PLAYWRIGHT_BROWSERS_PATH=browser_cache),
            )

    def window(self, name):
        self.publish_phase(name)
        start = time.monotonic()
        self.report.setdefault("wall_phase_bounds", {})[name] = [time.time() * 1000]
        time.sleep(STABLE_SECONDS)
        self.report["wall_phase_bounds"][name].append(time.time() * 1000)
        self.report.setdefault("phase_bounds", {})[name] = [start, time.monotonic()]

    def capture_update(self):
        request = self.report.get("activation_request_id")
        if not request:
            return None
        directory = runtime.runtime_directory(self.database)
        path = directory / "update.json"
        if not path.exists():
            return None
        update = json.loads(path.read_bytes())
        if update["id"] != request:
            return None
        source = self.report["initial_runtime"]
        if (update.get("source_instance") != source["instance"]
                or update.get("previous") != str(directory / "payloads" / source["payload"])
                or update.get("target") != str(directory / "payloads" / self.new[1]["payload_digest"])
                or update.get("database") != str(self.database.resolve())):
            raise AssertionError("Durable authorization does not match the tested source, candidate and database")
        self.report["guarded_control"] = {
            key: update.get(key)
            for key in ("id", "phase", "error", "source_instance", "previous", "target",
                        "created_at", "updated_at", "admission_drain_started_at", "admission_drain_seconds", "blocked")
        }
        if update.get("error"):
            self.report["guarded_control"]["error"] = diagnostic_reason(update["error"])
        observed = {key: update.get(key) for key in (
            "phase", "updated_at", "admission_drain_started_at", "admission_drain_seconds", "blocked"
        )}
        observations = self.report.setdefault("guarded_control_observations", [])
        if not observations or observations[-1] != observed:
            observations.append(observed)
        return update["phase"]

    def stage_signed(self):
        directory = runtime.runtime_directory(self.database)
        initial = self.report["initial_runtime"]
        previous = str(directory / "payloads" / self.old[1]["payload_digest"])
        target = str(directory / "payloads" / self.new[1]["payload_digest"])
        held = self.launch_signed(self.new[0], hold=True)
        if held.get("pending") != target or held.get("pending_confirmation") is not True:
            raise RuntimeError("Exact signed candidate was not held for confirmation")
        self.report["guarded_attachment"] = {
            "instance": held.get("instance"), "payload": held.get("payload"), "pending": target,
        }
        record = runtime.discover(self.database, timeout=3)
        if (not record or initial["payload"] != self.old[1]["payload_digest"]
                or any(r.get("instance") != initial["instance"] or r.get("payload") != previous for r in (held, record))):
            raise RuntimeError("Initial coordinator changed before authorization")
        return held, record, target

    def observe_control_files(self, stop):
        directory = runtime.runtime_directory(self.database)
        timeline = self.report["transition_control_timeline"] = []
        last = None
        while True:
            sample = {}
            for label, path in (("edge", directory / "edge-route.json"), ("backend", directory / "update.json")):
                try:
                    row = json.loads(path.read_bytes())
                except FileNotFoundError:
                    continue
                except (OSError, ValueError) as error:
                    sample[label] = {"read_error": diagnostic_reason(error)}
                    continue
                if label == "backend":
                    if row.get("id") != self.report["activation_request_id"]:
                        continue
                    sample[label] = {k: row[k] for k in ("phase", "admission_drain_seconds") if k in row}
                    if row.get("error"):
                        sample[label]["error"] = diagnostic_reason(row["error"])
                else:
                    sample[label] = {k: row[k] for k in ("state", "paused") if k in row}
            if sample != last:
                timeline.append({"at_ms": int(time.time() * 1000), **sample})
                last = sample
            if stop.wait(0.25):
                return

    def swap_signed(self, staged=None):
        held, record, target = staged or self.stage_signed()
        initial = self.report["initial_runtime"]
        if staged is not None:
            record = runtime.discover(self.database, timeout=3)
            if not record or (record.get("instance"), record.get("payload")) != (held["instance"], held["payload"]):
                raise RuntimeError("Staged source coordinator changed before authorization")
        if getattr(self, "peers", None):
            self.observe_active_receipts("before_authorization")
        self.publish_phase("upgrade")
        begin = time.monotonic()
        self.report.setdefault("wall_phase_bounds", {})["upgrade"] = [time.time() * 1000]
        request = self.report["activation_request_id"] = str(uuid.uuid4())
        errors = self.report["guarded_control_observation_errors"] = []
        watcher = None
        if getattr(self, "peers", None):
            stop = threading.Event()
            watcher = threading.Thread(target=self.observe_control_files, args=(stop,), daemon=True)
            watcher.start()
        try:
            try:
                runtime.control(record, "activate-update", payload=target, request_id=request, timeout=35)
            except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
                errors.append({"operation": "activate-update", "error": diagnostic_reason(error),
                               "after_seconds": time.monotonic() - begin})
            deadline = begin + UPDATE_OBSERVATION_SECONDS
            phase_deadline = time.monotonic() + UPDATE_PHASE_STALL_SECONDS
            previous_phase = None
            while time.monotonic() < min(deadline, phase_deadline):
                phase = self.capture_update()
                if phase and phase != previous_phase:
                    previous_phase = phase
                    phase_deadline = time.monotonic() + UPDATE_PHASE_STALL_SECONDS
                if phase in {"failed", "rolled-back"}:
                    raise AssertionError("Guarded update failed: " + str(self.report["guarded_control"]["error"]))
                if phase == "committed":
                    operation = "discover"
                    try:
                        candidate = runtime.discover(self.database, timeout=3)
                        if candidate and candidate["payload"] == target and candidate["instance"] != initial["instance"]:
                            operation = "status"
                            status = runtime.control(candidate, "status", timeout=2)
                            if (status.get("status") == "serving" and status.get("payload") == target
                                    and (status.get("update") or {}).get("id") == request
                                    and status["update"].get("phase") == "committed"):
                                operation = "browser config"
                                token = fetch(status["url"], "", "config", timeout=3)["token"]
                                self.report["candidate_runtime"] = self.identity(status)
                                self.address = (status["url"], token)
                                self.report["guarded_update_phase"] = phase
                                return
                    except (OSError, ValueError, RuntimeError) as error:
                        errors.append({"operation": operation, "error": diagnostic_reason(error),
                                       "after_seconds": time.monotonic() - begin})
                time.sleep(0.05)
            raise AssertionError("Could not verify the signed candidate serving the committed guarded update")
        finally:
            if watcher is not None:
                stop.set()
                watcher.join(2)
            self.capture_update()
            self.report.setdefault("phase_bounds", {})["upgrade"] = [begin, time.monotonic()]
            self.report["wall_phase_bounds"]["upgrade"].append(time.time() * 1000)

    def summarize(self):
        samples = list(self.report["samples"])
        bounds = self.report["phase_bounds"]
        events = [r for r in self.records() if r["kind"] == "output"]
        identities = collections.defaultdict(set)
        for event in events:
            identities[event["thread"]].add((event["pid"], event["turn"]))
        self.report["stream_identity_counts"] = {key: len(values) for key, values in identities.items()}
        if any(len(identities[peer]) != 1 for peer in self.peers):
            self.report["violations"].append({"phase": "upgrade", "kind": "provider process or turn identity changed"})
        for phase in bounds:
            lo, hi = bounds[phase]
            streams = collections.Counter(r["thread"] for r in events if lo <= r["time"] <= hi)
            self.report.setdefault("observed_stream_events", {})[phase] = dict(streams)
            if any(streams[peer] < (1 if phase == "upgrade" else STABLE_SECONDS // 2) for peer in self.peers):
                self.report["violations"].append({
                    "phase": phase, "kind": "provider streaming coverage",
                    "reason": "six sustained wire streams were not observed",
                })
            summary = self.report.setdefault("results", {}).setdefault(phase, {})
            for name, limits in BUDGETS_MS.items():
                rows = [r for r in samples if r["kind"] == name]
                selected = [r for r in rows if lo <= r["start"] < hi]
                ok = lambda r: r["outcome"] in {"accepted", "success"}
                latency = sorted(1000 * (r["end"] - r["start"]) for r in selected if ok(r))
                good_ends = sorted(r["end"] for r in rows if ok(r) and lo <= r["end"] <= hi)
                earlier = [r["end"] for r in rows if ok(r) and r["end"] < lo]
                later = [r["end"] for r in rows if ok(r) and r["end"] > hi]
                points = ([max(earlier, default=lo), *good_ends, min(later, default=hi)]
                          if phase == "upgrade" else [lo, *good_ends, hi])
                gap = max(b - a for a, b in zip(points, points[1:]))
                p50 = statistics.median(latency) if latency else None
                p95 = latency[math.ceil(len(latency) * 0.95) - 1] if latency else None
                summary[name] = {
                    "attempts": len(selected), "successes": len(latency),
                    "p50_ms": p50, "p95_ms": p95, "maximum_seconds_without_success": gap,
                }
                failures = []
                if phase != "upgrade" and (len(latency) < MINIMUM_SAMPLES or len(selected) != len(latency)):
                    failures.append("insufficient successful interactions or request errors")
                if not latency or p50 > limits[0] or p95 > limits[1]:
                    failures.append("interaction latency budget exceeded" if latency else "no completed latency samples")
                if phase == "upgrade" and (not selected or gap >= MAXIMUM_GAP_SECONDS):
                    failures.append("missing cutover interaction or at least five seconds without success")
                for reason in failures:
                    self.report["violations"].append({"phase": phase, "kind": name, "reason": reason})

    def pending_state(self, pending):
        detail = fetch(*self.address, f"sessions/{pending['approval_thread']}")
        with closing(sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True)) as db:
            queue = db.execute("SELECT id,state FROM browser_message_queue WHERE thread_id=? ORDER BY created_at,rowid",
                               (pending["queue_thread"],)).fetchall()
        records = self.records()
        return {
            "approval_keys": [r["key"] for r in detail["requests"]],
            "provider_requests": [r["request"] for r in records if r["kind"] == "approval"
                                  and r["thread"] == pending["approval_thread"]],
            "provider_answers": [r["request"] for r in records if r["kind"] == "approval_answer"],
            "queue": [list(row) for row in queue],
            "queued_turns": sum(r["kind"] == "turn" and r.get("role") == "queued-after-stream" for r in records),
            "provider_turns": [[r["thread"], r["pid"], r["turn"]] for r in records if r["kind"] == "turn"
                               and r["thread"] in [*self.peers, pending["approval_thread"]]],
        }

    def prepare_pending_work(self):
        pending = self.report["pending_work"] = {}
        thread = fetch(*self.address, "sessions", {"cwd": str(self.root), "client": "claude",
                                                   "name": "Pending approval verification"})["session"]["thread_id"]
        pending.update(approval_thread=thread, queue_thread=self.peers[1])
        fetch(*self.address, f"sessions/{thread}/messages", {"message": "approval"})
        self.wait(lambda: fetch(*self.address, f"sessions/{thread}")["requests"], timeout=25)
        queued = fetch(*self.address, f"sessions/{self.peers[1]}/queue", {"message": "queued-after-stream"})
        before = pending["before"] = self.pending_state(pending)
        if (not queued["queued"] or before["queue"] != [[queued["id"], "queued"]]
                or len(before["approval_keys"]) != 1 or len(before["provider_requests"]) != 1
                or before["provider_answers"] or before["queued_turns"] or len(before["provider_turns"]) != 7
                or {r[0] for r in before["provider_turns"]} != {*self.peers, thread}):
            raise AssertionError("The original pending approval, queued message or provider turns were not observed")

    def verify_pending_work(self):
        pending = self.report["pending_work"]
        after = pending["after"] = self.pending_state(pending)
        if after != pending["before"]:
            raise AssertionError("Pending approval, queue or original provider turn changed across the handover")
        pending["preserved"] = True

    def pending_effects(self):
        pending = self.report["pending_work"]
        records = self.records()
        answers = [r["request"] for r in records if r["kind"] == "approval_answer"]
        requests = [r["request"] for r in records if r["kind"] == "approval" and r["thread"] == pending["approval_thread"]]
        queued = [r["thread"] for r in records if r["kind"] == "turn" and r.get("role") == "queued-after-stream"]
        return {"request_count": len(requests), "answer_count": len(answers), "queued_delivery_count": len(queued),
                "original_approval_answered_once": pending["before"]["provider_requests"] == requests == answers,
                "original_queue_delivered_once": queued == [pending["queue_thread"]]}

    def release_pending_work(self):
        pending = self.report["pending_work"]
        key, = pending["before"]["approval_keys"]
        fetch(*self.address, f"sessions/{pending['approval_thread']}/requests/{urllib.parse.quote(key, safe='')}",
              {"decision": "accept"})
        (self.root / "release").touch()
        self.wait(lambda: (e := self.pending_effects())["queued_delivery_count"] and e["answer_count"], timeout=60)
        effects = pending["effects"] = self.pending_effects()
        if not effects["original_approval_answered_once"] or not effects["original_queue_delivered_once"]:
            raise AssertionError("Original pending work did not complete exactly once")

    def observe_active_receipts(self, when):
        previous = self.report.setdefault("legacy_receipt_observations", {})
        tracked = {r["request_id"] for rows in previous.values() for r in rows}
        if when == "after_cleanup":
            tracked.update(row["request_id"] for row in self.report.get("receipts", [])
                           if row["http_outcome"] == "accepted" or row["durable_states"] in (["completed"], ["accepted"], ["uncertain"]))
        counts = self.provider_input_counts()
        rows = []
        with closing(sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
            for thread, key, payload, state in db.execute(
                "SELECT thread_id,request_id,payload_json,state FROM browser_submission_receipts WHERE thread_id IN (?,?)",
                (self.peers[0], self.composer)
            ):
                marker = json.loads(payload)["body"]["message"]
                if marker == "stream" or (key not in tracked and state == "completed" and counts[marker] == 1):
                    continue
                rows.append({"request_id": key, "thread_role": "browser" if thread == self.composer else "stream",
                             "durable_state": state, "provider_inputs": counts[marker]})
        previous[when] = rows
        if when == "after_cleanup":
            observed = {row["request_id"] for row in rows}
            acknowledged = {row["request_id"] for row in self.report.get("receipts", [])
                            if row["http_outcome"] == "accepted" or row["durable_states"] in (["completed"], ["accepted"], ["uncertain"])}
            for row in rows:
                if (row["durable_state"] in {"accepted", "completed", "uncertain"}
                        and (row["durable_state"] != "completed" or row["provider_inputs"] != 1)
                        or row["provider_inputs"] > 1):
                    self.report["violations"].append({"phase": "cleanup", "kind": "final provider receipt disagreement",
                                                      "request_id": row["request_id"]})
                    self.report.update(passed=False, standalone_passed=False)
            for missing in sorted(acknowledged - observed):
                self.report["violations"].append({"phase": "cleanup", "kind": "missing original accepted receipt",
                                                  "request_id": missing})
                self.report.update(passed=False, standalone_passed=False)

    def provider_input_counts(self, destinations=None, handoffs=None):
        counts = collections.Counter()
        latest = {}
        for record in self.records():
            message = record.get("message", {})
            if handoffs is not None:
                if record["kind"] == "turn":
                    latest[record["thread"]] = (record["turn"], False)
                    marker = record.get("role")
                    if marker in handoffs:
                        handoffs[marker]["turn"] = record["turn"]
                elif record["kind"] == "complete" and latest.get(record["thread"]) == (record["turn"], False):
                    latest[record["thread"]] = (record["turn"], True)
                elif record["kind"] == "input" and message.get("method") == "turn/interrupt":
                    latest.pop(message.get("params", {}).get("threadId"), None)
            if record["kind"] == "input" and message.get("method") in {"turn/steer", "turn/start"}:
                params = message["params"]
                for item in params["input"]:
                    text = item.get("text")
                    counts[text] += 1
                    if destinations is not None:
                        destinations[text].append((params.get("threadId"), params.get("expectedTurnId")))
                    if handoffs is not None and message["method"] == "turn/start":
                        predecessor = latest.pop(params.get("threadId"), None)
                        if predecessor and predecessor[1]:
                            handoffs[text] = {"previous": predecessor[0]}
        return counts

    def capture_transition_diagnostics(self, when):
        diagnostic = self.report.setdefault("transition_diagnostics", {})[when] = {
            "phase": self.phase, "at_ms": int(time.time() * 1000)}
        directory = runtime.runtime_directory(self.database)
        try:
            edge_path = directory / "edge-route.json"
            if edge_path.is_file():
                edge = json.loads(edge_path.read_bytes())
                diagnostic["edge_state"] = {k: edge.get(k) for k in ("state", "paused")}
            path = self.root / "browser-network.json"
            if path.is_file():
                diagnostic["browser_network"] = json.loads(path.read_bytes())
            diagnostic["update_phase"] = self.capture_update()
            if getattr(self, "composer", None):
                self.observe_active_receipts(when)
        except (OSError, ValueError, sqlite3.Error) as error:
            diagnostic["collection_error"] = getattr(error, "sqlite_errorname", type(error).__name__)
            self.report["violations"].append({"phase": "cleanup", "kind": "incomplete diagnostic evidence"})
            self.report.update(passed=False, standalone_passed=False)

    def verify_receipts(self):
        attempts = {
            r["request_id"]: (r["request_id"], r["outcome"], r["phase"], False)
            for r in self.report["samples"]
            if r["kind"] == "send"
        }
        browser = {
            r["marker"]: r
            for r in self.report.get("browser_observations", [])
            if r["kind"] in {"browser_send_intent", "browser_send"}
        }
        destinations = collections.defaultdict(list)
        handoffs = {}
        counts = self.provider_input_counts(destinations, handoffs)
        native, native_payloads, native_turns = {}, {}, {}
        client_origins = {r["request_id"]: r["origin"] for r in self.report["samples"]
                          if r["kind"] == "send" and "origin" in r}
        client_origins.update({r["request_id"]: r["post_origin"] for r in browser.values()
                               if r.get("request_id") and r.get("post_origin")})

        with closing(sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True)) as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(browser_submission_receipts)")}
            result_column = "result_json" if "result_json" in columns else "NULL"
            for tid, key, payload, state, result in db.execute(
                f"SELECT thread_id, request_id, payload_json, state, {result_column} FROM browser_submission_receipts WHERE thread_id IN (?, ?)",
                (self.peers[0], getattr(self, "composer", None)),
            ):
                saved_body = json.loads(payload)["body"]
                marker = saved_body["message"]
                if marker == "stream":
                    continue
                if key in native:
                    self.report["violations"].append({"phase": "upgrade", "kind": "duplicate original receipt key", "request_id": key})
                native[key] = state
                native_payloads[key] = tid, saved_body
                if result:
                    native_result = json.loads(result)
                    native_turns[key] = (native_result.get("turn") or {}).get("id") or native_result.get("turnId")
                if key not in attempts:
                    observation = browser.get(marker, {})
                    code = observation.get("status")
                    outcome = "accepted" if code and 200 <= code < 300 else "rejected" if code else "uncertain"
                    attempts[key] = (marker, outcome, observation.get("phase", "upgrade"), tid == self.composer)
        for marker, observation in browser.items():
            code = observation.get("status")
            key = observation.get("request_id")
            if key and key not in attempts:
                outcome = "accepted" if code and 200 <= code < 300 else "rejected" if code else "uncertain"
                attempts[key] = (marker, outcome, observation["phase"], True)
            elif not key and counts[marker] and not any(a[0] == marker for a in attempts.values()):
                self.report["violations"].append({"phase": observation["phase"], "kind": "browser delivery without receipt"})

        def matches_target(key, marker, thread, expected):
            targets = destinations[marker]
            if len(targets) != 1 or targets[0][0] != thread:
                return False
            if not expected or targets[0][1] == expected:
                return True
            handoff = handoffs.get(marker, {})
            return bool(targets[0][1] is None and expected == handoff.get("previous")
                        and native_turns.get(key) and native_turns[key] == handoff.get("turn"))

        for key, (marker, outcome, phase, via_browser) in attempts.items():
            state = native.get(key)
            delivered = counts[marker]
            thread = self.composer if via_browser else self.peers[0]
            observation = browser.get(marker, {}) if via_browser else {}
            expected = observation.get("expected_turn_id") if via_browser else getattr(self, "turn_id", None)
            receipt_thread, receipt_body = native_payloads.get(key, (None, {}))
            confirmed_target = receipt_thread == thread and receipt_body.get("message") == marker and matches_target(key, marker, thread, expected)
            if observation.get("receipt_turn_id") and observation.get("status") == 200:
                confirmed_target = confirmed_target and observation["receipt_turn_id"] == native_turns.get(key)
            self.report["receipts"].append({
                "request_id": key, "http_outcome": outcome, "phase": phase, "browser": via_browser,
                "durable_states": [state] if state else [], "provider_deliveries": delivered,
                "provider_target_matches": confirmed_target, "client_origin": client_origins.get(key),
            })
            if (delivered > 1
                    or (outcome == "accepted" or state in {"accepted", "completed", "uncertain"})
                    and (delivered != 1 or state != "completed" or not confirmed_target)
                    or (outcome == "rejected" or state == "rejected") and delivered):
                self.report["violations"].append({
                    "phase": phase, "kind": "durable receipt or provider delivery disagreement", "request_id": key,
                })

    def finish_browser(self):
        if self.browser is None:
            return
        (self.root / "stop-browser").touch()
        try:
            self.browser.wait(20)
        except subprocess.TimeoutExpired:
            self.browser.terminate()
            self.browser.wait(5)
        self.browser_err.close()
        path = self.root / "browser-observations.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        self.report["browser_observations"] = rows
        self.report["browser_process"] = {
            "exit_code": self.browser.returncode if isinstance(self.browser.returncode, int) else None,
            "stderr_bytes": (self.root / "browser-errors.log").stat().st_size,
        }
        for phase in self.report.get("phase_bounds", {}):
            for kind in ("browser_open", "browser_send"):
                selected = [r for r in rows if r["phase"] == phase and r["kind"] == kind]
                good = [r for r in selected if r["ok"]]
                stats = {"successes": len(good), "attempts": len(selected)}
                self.report.setdefault("browser_results", {}).setdefault(phase, {})[kind] = stats
                if kind == "browser_open" and good:
                    elapsed = sorted(r["end"] - r["start"] for r in good)
                    stats["successful_open_ms"] = {
                        "count": len(elapsed), "p50": statistics.median(elapsed),
                        "p95": elapsed[math.ceil(len(elapsed) * 0.95) - 1], "max": elapsed[-1],
                    }
                if phase == "upgrade":
                    lo, hi = self.report["wall_phase_bounds"][phase]
                    completions = sorted(r["end"] for r in rows if r["kind"] == kind and r["ok"])
                    before = max((end for end in completions if end <= lo), default=lo)
                    after = min((end for end in completions if end >= hi), default=hi)
                    points = [before, *(end for end in completions if lo < end < hi), after]
                    begin, end = max(zip(points, points[1:]), key=lambda pair: pair[1] - pair[0])
                    stats["maximum_seconds_without_success"] = (end - begin) / 1000
                    stats["gap_relative_to_upgrade_start_ms"] = [begin - lo, end - lo]
                    if not selected or stats["maximum_seconds_without_success"] >= MAXIMUM_GAP_SECONDS:
                        self.report["violations"].append({
                            "phase": phase, "kind": kind, "reason": "missing upgrade traffic or availability gap",
                        })
                if phase == "upgrade" and not good:
                    self.report["violations"].append({"phase": phase, "kind": kind, "reason": "no completed browser samples"})
                elif phase != "upgrade" and (len(good) < 3 or len(good) != len(selected)):
                    self.report["violations"].append({"phase": phase, "kind": kind, "reason": "failed or missing browser samples"})
                if kind == "browser_send" and good:
                    for metric, median, tail in [("own_ms", None, 50), ("other_ms", 150, 400),
                                                 ("http_acceptance_ms", *BUDGETS_MS["send"])]:
                        values = [r["responded"] - r["requested"] if metric == "http_acceptance_ms"
                                  and r.get("requested") is not None and r.get("responded") is not None
                                  else r.get(metric) for r in good]
                        times = sorted(v for v in values if isinstance(v, (int, float)) and math.isfinite(v) and v >= 0)
                        if len(times) != len(good):
                            self.report["violations"].append({"phase": phase, "kind": metric,
                                                              "reason": "missing completed browser latency sample"})
                        if not times:
                            continue
                        p50, p95 = statistics.median(times), times[math.ceil(len(times) * 0.95) - 1]
                        stats[metric] = {"count": len(times), "p50": p50, "p95": p95}
                        if p95 > tail or (median is not None and p50 > median):
                            self.report["violations"].append({"phase": phase, "kind": metric, "reason": "browser latency budget exceeded"})

    def execute(self):
        self.exercise_pending_work = not self.candidate_only
        if self.affected_report:
            self.report["affected_report"] = {"path": str(self.affected_report.resolve()),
                                              "sha256": hashlib.sha256(self.affected_report.read_bytes()).hexdigest()}
        try:
            self.start()
            if self.candidate_only:
                self.report["mode"] = "candidate_standalone_diagnostic"
                self.window("candidate")
            else:
                self.report["mode"] = "signed_guarded_upgrade"
                self.window("baseline")
                self.swap_signed()
                self.window("candidate")
                self.verify_pending_work()
            if self.profile_sample:
                self.publish_phase("profiling")
                witness = self.report.get("candidate_runtime") or self.report["initial_runtime"]
                command = process_command(witness["pid"])
                if command == witness["process"] and str(self.root.resolve()) in command:
                    sample = subprocess.run(["/usr/bin/sample", str(witness["pid"]), "3", "-file", str(self.profile_sample)],
                                            timeout=15, capture_output=True)
                    self.report["profile_sample"] = {
                        "after_budget_window": True, "pid": witness["pid"],
                        "created": sample.returncode == 0 and self.profile_sample.is_file(),
                        "path": str(self.profile_sample),
                    }
                else:
                    self.report["profile_sample"] = {"created": False, "reason": "Private coordinator identity changed"}
        finally:
            self.capture_transition_diagnostics("traffic_stopping")
            self.stop_traffic.set()
            for thread in self.workers:
                thread.join(25)
            self.finish_browser()
            self.capture_transition_diagnostics("traffic_stopped")
            self.capture_update()
            if "phase_bounds" in self.report:
                self.summarize()
            if self.peers and getattr(self, "composer", None):
                self.verify_receipts()
            if self.report.get("pending_work", {}).get("preserved"):
                self.release_pending_work()
            if "baseline" in self.report.get("results", {}) and self.old[1]["version"] == "0.1.12+63":
                old = self.report["initial_runtime"]
                red = any(v["phase"] == "baseline" and "latency" in v.get("reason", "") for v in self.report["violations"])
                self.report["baseline_proof"] = {
                    "red": red, "version": self.old[1]["version"],
                    "payload_digest": old["payload"], "architecture": old["architecture"],
                }
        cover = self.report["workload"]["coverage"]
        cover["saved_histories_reopened"] = len({
            r["fixture"] for r in self.report["samples"]
            if r["phase"] == "candidate" and r["kind"] == "history"
            and r["outcome"] == "success" and r["fixture"] in self.open_targets
        })
        cover["accepted_verified"] = sum(
            r["http_outcome"] == "accepted" and r["durable_states"] == ["completed"] and r["provider_deliveries"] == 1
            for r in self.report["receipts"]
        )
        for kind in ("browser_send", "browser_open"):
            cover[kind + "s"] = sum(
                r["phase"] == "candidate" and r["kind"] == kind and r["ok"]
                for r in self.report.get("browser_observations", [])
            )
        if cover["saved_histories_reopened"] != 2 or not all(cover.values()):
            self.report["violations"].append({"phase": "candidate", "kind": "incomplete workload coverage"})
        if self.candidate_only:
            self.report["standalone_passed"] = not self.report["violations"]
            return
        before, after = self.report["initial_runtime"], self.report["candidate_runtime"]
        if before["pid"] == after["pid"] or before["transport_pid"] != after["transport_pid"]:
            self.report["violations"].append({"phase": "upgrade", "kind": "coordinator or transport continuity"})
        if after.get("edge_pid") and before.get("edge_pid") and before["url"] != after["url"]:
            self.report["violations"].append({"phase": "upgrade", "kind": "edge origin changed across cutover"})
        failures = self.report["violations"]
        baseline_red = any(v["phase"] == "baseline" and "latency" in v.get("reason", "") for v in failures)
        self.report["baseline_reproduced"] = baseline_red
        old = self.report["initial_runtime"]
        if self.old[1]["version"] == "0.1.12+63":
            proof = {"red": baseline_red, "version": self.old[1]["version"],
                     "payload_digest": old["payload"], "architecture": old["architecture"]}
        else:
            raw = self.affected_report.read_bytes() if self.affected_report else b"{}"
            ref = json.loads(raw)
            if self.affected_report and hashlib.sha256(raw).hexdigest() != self.report["affected_report"]["sha256"]:
                raise AssertionError("Affected release proof changed during validation")
            proof = ref.get("baseline_proof") or {}
            if ref.get("workload_contract") != self.report["workload_contract"] or proof.get("version") != "0.1.12+63":
                proof = {}
        self.report["baseline_proof"] = proof
        self.report["passed"] = bool(
            proof.get("red")
            and proof.get("architecture") == after["architecture"]
            and self.report.get("guarded_update_phase") == "committed"
            and not any(v["phase"] != "baseline" or "receipt" in v["kind"] or "durable" in v["kind"] for v in failures)
        )

    def close(self):
        super().close()
        survivors = [pid for pid, command in processes_mentioning(str(self.root)).items()
                     if "agent_coord.runtime_edge" in command]
        self.report["edge_survivors"] = survivors
        if survivors:
            self.report.update(passed=False, standalone_passed=False)
            self.report["violations"].append({"phase": "cleanup", "kind": "edge process survived cleanup"})
        fixture_stopped = ("leftover_processes" in self.report and not survivors and not any(
            p["alive_after_cleanup"] for p in self.report["leftover_processes"].values()))
        if hasattr(self, "phase") and fixture_stopped:
            self.capture_transition_diagnostics("after_cleanup")
            pending = self.report.get("pending_work", {})
            if pending.get("preserved"):
                effects = pending["final_effects"] = self.pending_effects()
                if not effects["original_approval_answered_once"] or not effects["original_queue_delivered_once"]:
                    self.report.update(passed=False, standalone_passed=False)
                    self.report["violations"].append({"phase": "cleanup", "kind": "pending work receipt disagreement"})


def qualify_ci(app_bundle: Path, report_path: Path) -> int:
    """Fast < 90s CI qualification of APFS-cloned database responsiveness and < 250 ms blue/green cutover."""
    from agent_coord import runtime_update

    resources, bundle_info = signed_resources(app_bundle)
    report_path = report_path.resolve()
    harness = Validation(report_path, 1, "ci-cutover-qualification")
    report = {
        "schema_version": 1,
        "mode": "ci_apfs_cutover_qualification",
        "application": bundle_info,
        "passed": False,
        "violations": [],
    }
    try:
        seed_db = harness.root / "seed.sqlite3"
        seed_inventory(seed_db, harness.root, [("codex", "now", 120), ("claude", "later", 80)])
        cloned = runtime_update.clone_or_copy(seed_db, harness.database)
        report["apfs_cloned_fixture"] = cloned

        candidate_resources = harness.root / "CandidateResources"
        shutil.copytree(resources, candidate_resources, symlinks=True)
        backend_meta = json.loads((candidate_resources / "backend.json").read_text())
        parts = re.split(r"([.+])", backend_meta["runtime_version"])
        parts[-1] = str(int(parts[-1]) + 1)
        backend_meta["runtime_version"] = "".join(parts)
        (candidate_resources / "backend.json").write_text(json.dumps(backend_meta, indent=2) + "\n")

        def run_helper(bundle_res, *arguments, body=None):
            python_bin = Path(json.loads((bundle_res / "backend.json").read_text())["python"])
            if not python_bin.is_absolute():
                python_bin = bundle_res / python_bin
            proc = subprocess.run(
                [str(python_bin), "-B", str(bundle_res / "backend.py"), "--resources", str(bundle_res),
                 "--db", str(harness.database), *arguments],
                input=json.dumps(body) if body is not None else None,
                capture_output=True, text=True, timeout=45, cwd=harness.root,
                env=dict(harness.env, PYTHONPATH=str(bundle_res / "backend")),
            )
            if proc.returncode != 0:
                raise RuntimeError(f"Runtime helper failed ({proc.returncode}): {proc.stderr[-500:]}")
            return json.loads(proc.stdout)

        initial = run_helper(resources, "--runtime-bootstrap")
        edge_url = initial["url"]
        csrf_token = fetch(edge_url, "", "config", timeout=5)["token"]
        stream_session = fetch(edge_url, csrf_token, "sessions",
                               {"cwd": str(harness.root), "client": "codex", "name": "CI Stream"})["session"]["thread_id"]
        fetch(edge_url, csrf_token, f"sessions/{stream_session}/messages", {"message": "stream"})
        harness.wait(lambda: any(r["kind"] == "turn" for r in harness.records()), timeout=20)
        turn_id = fetch(edge_url, csrf_token, f"sessions/{stream_session}")["activeTurn"]

        sse_cursor = EventCursor(stream_session, turn_id)
        stop_workers = threading.Event()
        http_samples: list[dict] = []
        samples_lock = threading.Lock()

        def sse_reader():
            while not stop_workers.is_set():
                headers = {"X-Agent-Coord-Token": csrf_token, "Accept": "text/event-stream"}
                if sse_cursor.last_id:
                    headers["Last-Event-ID"] = sse_cursor.last_id
                try:
                    req = urllib.request.Request(edge_url + "api/browser/events", headers=headers)
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        event_id = None
                        while not stop_workers.is_set():
                            line = resp.readline()
                            if not line:
                                break
                            text = line.decode("utf-8", errors="replace").rstrip("\n")
                            if text.startswith("id: "):
                                event_id = text[4:]
                            elif text.startswith("data: "):
                                sse_cursor.feed(json.loads(text[6:]), event_id)
                except OSError:
                    stop_workers.wait(0.02)

        def http_load_worker(kind: str):
            counter = 0
            while not stop_workers.is_set():
                counter += 1
                started = time.monotonic()
                ok = False
                try:
                    if kind == "get":
                        data = fetch(edge_url, csrf_token, f"sessions/{stream_session}", timeout=5)
                        ok = data.get("activeTurn") == turn_id
                    elif kind == "post":
                        key = f"ci-steer-{counter:04d}"
                        fetch(edge_url, csrf_token, f"sessions/{stream_session}/messages",
                              {"message": key, "requestId": key, "expectedTurnId": turn_id}, timeout=5)
                        ok = True
                    else:
                        fetch(edge_url, csrf_token, "threads", timeout=5)
                        ok = True
                except Exception as exc:
                    with samples_lock:
                        http_samples.append({"kind": kind, "ok": False, "ms": (time.monotonic() - started) * 1000,
                                             "error": diagnostic_reason(exc)})
                else:
                    with samples_lock:
                        http_samples.append({"kind": kind, "ok": ok, "ms": (time.monotonic() - started) * 1000})
                stop_workers.wait(0.05 if kind == "post" else 0.04)

        threads = [threading.Thread(target=sse_reader, daemon=True)] + [
            threading.Thread(target=http_load_worker, args=(kind,), daemon=True)
            for kind in ("get", "post", "inventory")
        ]
        for worker in threads:
            worker.start()
        harness.wait(lambda: sse_cursor.batches > 0 and len(http_samples) >= 6, timeout=10)

        staged = run_helper(candidate_resources, "--runtime-bootstrap", "--runtime-confirm-updates")
        record = runtime.discover(harness.database, timeout=5)
        request_id = "ci-qualify-" + uuid.uuid4().hex[:12]
        runtime.control(record, "activate-update", request_id=request_id, payload=staged["pending"], timeout=15)

        cutover_started = None
        committed_at = None
        edge_route_path = runtime.runtime_directory(harness.database) / "edge-route.json"
        deadline = time.monotonic() + 30
        after_status = None
        while time.monotonic() < deadline:
            if edge_route_path.exists():
                try:
                    route_state = json.loads(edge_route_path.read_bytes()).get("state")
                    if route_state == "cutover" and cutover_started is None:
                        cutover_started = time.monotonic()
                except (OSError, ValueError):
                    pass
            try:
                current = runtime.discover(harness.database, timeout=1)
                if current and current["instance"] != initial["instance"]:
                    status = runtime.control(current, "status", timeout=2)
                    if status.get("status") == "serving" and (status.get("update") or {}).get("phase") == "committed":
                        committed_at = time.monotonic()
                        after_status = status
                        break
            except (OSError, ValueError, RuntimeError):
                pass
            time.sleep(0.005)

        time.sleep(0.3)
        stop_workers.set()
        for worker in threads:
            worker.join(timeout=5)
        (harness.root / "release").touch()

        if after_status is None:
            raise RuntimeError("Candidate coordinator did not commit within 30s")
        update_doc = after_status["update"]
        drain_ms = float(update_doc.get("admission_drain_seconds") or 0.0) * 1000.0
        route_cutover_ms = None
        if edge_route_path.exists():
            try:
                route_cutover_ms = json.loads(edge_route_path.read_bytes()).get("last_cutover_ms")
            except (OSError, ValueError):
                pass
        if isinstance(route_cutover_ms, (int, float)):
            observed_cutover_ms = float(route_cutover_ms)
        else:
            observed_cutover_ms = ((committed_at - cutover_started) * 1000.0) if cutover_started else drain_ms
        failed_requests = [s for s in http_samples if not s["ok"]]
        latencies = sorted(s["ms"] for s in http_samples if s["ok"])
        p95_ms = latencies[math.ceil(len(latencies) * 0.95) - 1] if latencies else None

        if failed_requests:
            report["violations"].append({"kind": "failed_http_requests", "count": len(failed_requests)})
        if after_status["url"] != edge_url:
            report["violations"].append({"kind": "edge_origin_changed", "before": edge_url, "after": after_status["url"]})
        if after_status["pid"] == initial["pid"]:
            report["violations"].append({"kind": "coordinator_pid_unchanged"})
        if sse_cursor.resets_after_first != 0 or sse_cursor.order_violations:
            report["violations"].append({"kind": "sse_continuity_violation"})
        if observed_cutover_ms > QUALIFY_CUTOVER_LIMIT_MS:
            report["violations"].append({"kind": "cutover_budget_exceeded", "cutover_ms": observed_cutover_ms})

        report.update(
            edge_url=edge_url,
            initial_pid=initial["pid"],
            replaced_pid=after_status["pid"],
            edge_pid=after_status.get("edge_pid"),
            http_requests=len(http_samples),
            failed_http_requests=len(failed_requests),
            http_p95_ms=p95_ms,
            cutover_ms=round(observed_cutover_ms, 2),
            sse=sse_cursor.summary(),
            passed=not report["violations"],
        )
    finally:
        harness.stopped.set()
        harness.close()
        survivors = [pid for pid, cmd in processes_mentioning(str(harness.root)).items() if pid != os.getpid()]
        report["survivors"] = survivors
        if survivors:
            report["passed"] = False
            report["violations"].append({"kind": "leaked_processes", "pids": survivors})
        else:
            shutil.rmtree(harness.root, ignore_errors=True)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2) + "\n")
    return 0 if report["passed"] else 1


def run_and_clean_up(runner):
    try:
        try:
            runner.execute()
        except Exception as error:
            runner.report["execution_error"] = {"type": type(error).__name__, "reason": diagnostic_reason(error)}
            raise
    finally:
        runner.persist()
        try:
            runner.close()
        finally:
            runner.persist()
            if "leftover_processes" in runner.report and not runner.report.get("edge_survivors") and not any(
                v["alive_after_cleanup"] for v in runner.report["leftover_processes"].values()
            ):
                shutil.rmtree(runner.root)
            else:
                runner.report.update(passed=False, standalone_passed=False)
                runner.report["violations"].append({"phase": "cleanup", "kind": "fixture process cleanup unverified"})
                runner.persist()
    result = runner.report.get("standalone_passed", False) if runner.candidate_only else runner.report["passed"]
    return 0 if result else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qualify-ci", type=Path, help="Run fast < 90s APFS-cloned blue/green cutover qualification on a built .app")
    for name in ("previous", "candidate", "snapshot"):
        parser.add_argument("--" + name, type=Path)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--saved-manifest-thread", help="Select a specific reported long conversation from the private snapshot")
    parser.add_argument("--profile-sample", type=Path)
    parser.add_argument("--affected-report", type=Path)
    parser.add_argument("--playwright-module")
    parser.add_argument("--candidate-only", action="store_true")
    args = parser.parse_args()
    if args.qualify_ci:
        return qualify_ci(args.qualify_ci, args.report)
    if not all((args.previous, args.candidate, args.snapshot)):
        parser.error("--previous, --candidate, and --snapshot are required unless --qualify-ci is set")
    old, new = signed_resources(args.previous), signed_resources(args.candidate)
    if old[1]["payload_digest"] == new[1]["payload_digest"]:
        parser.error("Two distinct signed builds are required")
    args.report = args.report.resolve()
    if args.report == args.snapshot.resolve() or any(
        args.report.is_relative_to(path.resolve())
        for path in (args.previous, args.candidate, Path(__file__).resolve().parents[1])
    ):
        parser.error("Private reports must remain separate from the source snapshot, signed bundles and checkout")
    runner = SignedUpgrade(args, old, new)
    return run_and_clean_up(runner)


if __name__ == "__main__":
    sys.exit(main())
