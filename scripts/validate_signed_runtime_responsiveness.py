#!/usr/bin/env python3
"""Run a private old-to-new signed coordinator upgrade with simultaneous interactive traffic.

On macOS, pass two signed .app directories, an offline SQLite snapshot for
inventory proportions and two saved histories, and a private report pathname.
The fixture replaces original text and identities before booting. No operator state
is booted. The provider processes, HOME, database, transport and HTTP servers
are private to this invocation. The two app directories are read-only inputs.
"""
from __future__ import annotations

import argparse
import collections
from functools import lru_cache
import hashlib
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
import tempfile
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid

from measure_backend_latency import BUDGETS
from runtime_upgrade_validation import Validation, process_command, runtime


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
    # Read only the dimensions required by the fixture; no real session IDs,
    # directories, queued jobs, user text, credentials or integrations cross.
    with sqlite3.connect(snapshot.resolve(strict=True).as_uri() + "?mode=ro", uri=True) as db:
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
    # Use the store API to generate complete sessions and project/workspace
    # affiliations using only private coordinates.
    for client, attention, count in rows:
        for number in range(count):
            name = f"Inventory {client} {attention} {number}"
            thread = str(uuid.uuid5(uuid.NAMESPACE_OID, name))
            store.register(session_id=thread, client=client, cwd=str(root), name=name)
            with store._connection() as db:
                db.execute("UPDATE work_threads SET attention=? WHERE thread_id=?", (attention, thread))


def copy_saved_histories(snapshot, database, root):
    """Import inert, text-redacted copies using the real item layout and chunk format."""
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
    with sqlite3.connect(snapshot.resolve(strict=True).as_uri() + "?mode=ro", uri=True) as source:
        source.execute("PRAGMA query_only=ON")
        # SQLite reads octet_length directly from the record header even for a
        # many-megabyte legacy document. Prefer a retained legacy large history,
        # then sample recently opened manifest-based histories for the other band.
        legacy = [
            r[0]
            for r in source.execute(
                """
            SELECT h.thread_id FROM browser_history h JOIN browser_sessions b USING(thread_id)
            WHERE b.archived=0 AND octet_length(h.history_json) BETWEEN 10485760 AND 33554432
            ORDER BY b.updated_at DESC LIMIT 3"""
            )
        ]
        ids = legacy + [
            r[0]
            for r in source.execute(
                "SELECT thread_id FROM browser_sessions WHERE archived=0 ORDER BY updated_at DESC LIMIT 200"
            )
        ]
        for source_id in ids:
            row = source.execute(
                "SELECT history_json FROM browser_history WHERE thread_id=?", (source_id,)
            ).fetchone()
            if not row:
                continue
            raw = history.decode_history(row[0], db=source)
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
            storage_format = "manifest" if history.MARKER in json.loads(row[0]) else "legacy"
            if storage_format == "manifest":
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
                    "items": sum(len(t.get("items", [])) for t in turns),
                    "last_turn": safe["turns"][-1]["id"],
                    "storage_format": storage_format,
                }
            )
            if len(refs) == 2:
                return refs
    raise ValueError("Snapshot lacks both representative medium and large saved histories")


def fetch(url, token, route, body=None, timeout=20):
    headers = {"X-Agent-Coord-Token": token, "X-Agent-Coord-History-View": "recent"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        url + "api/browser/" + route,
        headers=headers,
        data=json.dumps(body).encode() if body is not None else None,
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


class SignedUpgrade(Validation):
    def __init__(self, args, previous, candidate):
        super().__init__(args.report, 1, "signed-responsiveness")
        self.old, self.new = previous, candidate
        self.profile_sample = args.profile_sample
        self.snapshot = args.snapshot
        self.affected_report = args.affected_report
        self.candidate_only = args.candidate_only
        self.browser_module = args.playwright_module
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
        # Provider lookup can only find our wire fixtures. Do not expose user
        # executable directories, shell initialization, proxies, or credentials.
        allowed = {
            "HOME",
            "CODEX_HOME",
            "CLAUDE_CONFIG_DIR",
            "XDG_CONFIG_HOME",
            "XDG_STATE_HOME",
            "XDG_DATA_HOME",
            "XDG_CACHE_HOME",
            "TMPDIR",
            "PYTHONDONTWRITEBYTECODE",
            "PYTHONNOUSERSITE",
            "RIBBON_FAKE_ROOT",
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

    def launch_signed(self, resources, *, hold=False):
        args = [
            str(resources / "runtime/bin/python3"),
            "-B",
            str(resources / "backend.py"),
            "--resources",
            str(resources),
            "--db",
            str(self.database),
            "--runtime-bootstrap",
        ]
        if hold:
            args.append("--runtime-confirm-updates")
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=90,
            cwd=self.root,
            env=dict(self.env, PYTHONPATH=str(resources / "backend")),
        )
        if result.returncode:
            raise RuntimeError(result.stderr[-1000:])
        return json.loads(result.stdout)

    def start(self):
        seed_inventory(self.database, self.root, self.sources)
        self.saved = copy_saved_histories(self.snapshot, self.database, self.root)
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
        for i in range(6):
            client = "claude" if i >= 4 else "codex"
            session = fetch(
                *self.address, "sessions", {"cwd": str(self.root), "client": client, "name": f"Stream {i}"}
            )
            tid = session["session"]["thread_id"]
            fetch(*self.address, f"sessions/{tid}/messages", {"message": "stream"})
            self.peers.append(tid)
        self.wait(lambda: sum(r["kind"] == "turn" for r in self.records()) == 6, timeout=30)
        self.turn_id = fetch(*self.address, "sessions/" + self.peers[0])["activeTurn"]
        self.composer = fetch(
            *self.address,
            "sessions",
            {"cwd": str(self.root), "client": "codex", "name": "Composer validation"},
        )["session"]["thread_id"]
        # Take durable acknowledged submissions into the real replacement, so
        # a failed admission boundary cannot vacuously pass the receipt check.
        for _ in range(3):
            key = uuid.uuid4().hex
            fetch(
                *self.address,
                f"sessions/{self.peers[0]}/messages",
                {"message": key, "requestId": key, "expectedTurnId": self.turn_id},
            )
            self.report["samples"].append(
                {
                    "phase": "preflight",
                    "kind": "send",
                    "request_id": key,
                    "start": time.monotonic(),
                    "end": time.monotonic(),
                    "outcome": "accepted",
                }
            )
        (self.root / "start-streams").touch()

        def large_streams():
            return {
                r["thread"]
                for r in self.records()
                if r["kind"] == "output" and r.get("size", 0) >= LARGE_OUTPUT_BYTES
            }

        self.wait(lambda: len(large_streams()) == 2, timeout=60)
        # Require the baseline coordinator to have actually consumed the large
        # output before collecting measurements; provider writes alone are insufficient.
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
            t = threading.Thread(target=self.worker, args=(name,), daemon=True)
            t.start()
            self.workers.append(t)

    def projection_bytes(self):
        with sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True) as db:
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
            [
                str(payload / "runtime/bin/python3"),
                "-I",
                "-B",
                "-c",
                "import platform; print(platform.machine())",
            ],
            env=self.env,
            cwd=self.root,
            text=True,
        ).strip()
        command = process_command(pid)
        if not command or str(payload) not in command:
            raise AssertionError("Coordinator process does not execute the retained signed payload")
        return {
            "architecture": arch,
            "pid": pid,
            "instance": status["instance"],
            "payload": Path(status["payload"]).name,
            "process": process_command(pid),
            "transport_pid": owner["pid"],
            "url": status["url"],
        }

    def worker(self, name):
        index = 0
        while not self.stop_traffic.is_set():
            phase, address = self.phase, self.address
            if phase == "setup":
                self.stop_traffic.wait(0.05)
                continue
            # Read a different currently running conversation in each request.
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
            entry = {"phase": phase, "kind": name, "start": started, "fixture": tid}
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
                    entry.update(
                        outcome="rejected", status=error.code, detail=error.read(200).decode(errors="replace")
                    )
            except Exception as error:
                entry.update(outcome="uncertain", detail=repr(error)[:200])
            entry["end"] = time.monotonic()
            with self.lock:
                self.report["samples"].append(entry)
            index += 1
            self.stop_traffic.wait(0.25)

    def publish_phase(self, name):
        self.phase = name
        temporary = self.root / "browser-state.partial"
        temporary.write_text(
            json.dumps(
                {"phase": name, "url": self.address[0], "peers": self.open_targets, "composer": self.composer}
            )
        )
        temporary.replace(self.root / "browser-state.json")
        if self.browser is None:
            self.browser_err = (self.root / "browser-errors.log").open("w")
            node = shutil.which("node")
            if not node:
                raise RuntimeError(
                    "Browser acceptance requires Node and the repository's Playwright installation"
                )
            browser_cache = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or str(
                Path.home() / "Library/Caches/ms-playwright"
            )
            self.browser = subprocess.Popen(
                [
                    node,
                    str(Path(__file__).with_name("observe_signed_conversation_interactions.mjs")),
                    str(self.root),
                    self.browser_module or "",
                ],
                stdout=self.browser_err,
                stderr=self.browser_err,
                cwd=self.root,
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
        """Retain this authorization's durable decision independent of control availability."""
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
        if (
            update.get("source_instance") != source["instance"]
            or update.get("previous") != str(directory / "payloads" / source["payload"])
            or update.get("target") != str(directory / "payloads" / self.new[1]["payload_digest"])
            or update.get("database") != str(self.database.resolve())
        ):
            raise AssertionError(
                "Durable authorization does not match the tested source, candidate and database"
            )
        self.report["guarded_control"] = {
            key: update.get(key)
            for key in (
                "id",
                "phase",
                "error",
                "source_instance",
                "previous",
                "target",
                "created_at",
                "updated_at",
                "admission_drain_seconds",
                "blocked",
            )
        }
        return update["phase"]

    def swap_signed(self):
        directory = runtime.runtime_directory(self.database)
        initial = self.report["initial_runtime"]
        previous = str(directory / "payloads" / self.old[1]["payload_digest"])
        target = str(directory / "payloads" / self.new[1]["payload_digest"])
        held = self.launch_signed(self.new[0], hold=True)
        if held.get("pending") != target or held.get("pending_confirmation") is not True:
            raise RuntimeError("Exact signed candidate was not held for confirmation")
        self.report["guarded_attachment"] = {
            "instance": held.get("instance"),
            "payload": held.get("payload"),
            "pending": target,
        }
        # The guarded bootstrap already performed attach. Authenticate the
        # original instance again with its lock-independent identity handshake.
        # The control request below is addressed to that same instance.
        record = runtime.discover(self.database, timeout=3)
        if (
            not record
            or initial["payload"] != self.old[1]["payload_digest"]
            or any(
                r.get("instance") != initial["instance"] or r.get("payload") != previous
                for r in (held, record)
            )
        ):
            raise RuntimeError("Initial coordinator changed before authorization")
        self.publish_phase("upgrade")
        begin = time.monotonic()
        self.report.setdefault("wall_phase_bounds", {})["upgrade"] = [time.time() * 1000]
        request = self.report["activation_request_id"] = str(uuid.uuid4())
        errors = self.report["guarded_control_observation_errors"] = []
        try:
            try:
                runtime.control(record, "activate-update", payload=target, request_id=request, timeout=35)
            except (OSError, ValueError, RuntimeError) as error:
                # A lost response can follow a durable admission. Do not send
                # the command twice; observe the decision for this request ID.
                errors.append(
                    {
                        "operation": "activate-update",
                        "error": repr(error),
                        "after_seconds": time.monotonic() - begin,
                    }
                )
            deadline = time.monotonic() + 75
            while time.monotonic() < deadline:
                phase = self.capture_update()
                if phase in {"failed", "rolled-back"}:
                    raise AssertionError(
                        "Guarded update failed: " + str(self.report["guarded_control"]["error"])
                    )
                if phase == "committed":
                    operation = "discover"
                    try:
                        candidate = runtime.discover(self.database, timeout=3)
                        if (
                            candidate
                            and candidate["payload"] == target
                            and candidate["instance"] != initial["instance"]
                        ):
                            operation = "status"
                            status = runtime.control(candidate, "status", timeout=2)
                            if (
                                status.get("status") == "serving"
                                and status.get("payload") == target
                                and (status.get("update") or {}).get("id") == request
                                and status["update"].get("phase") == "committed"
                            ):
                                operation = "browser config"
                                self.address = (
                                    status["url"],
                                    fetch(status["url"], "", "config", timeout=3)["token"],
                                )
                                self.report["candidate_runtime"] = self.identity(status)
                                self.report["guarded_update_phase"] = phase
                                return
                    except (OSError, ValueError, RuntimeError) as error:
                        errors.append(
                            {
                                "operation": operation,
                                "error": repr(error),
                                "after_seconds": time.monotonic() - begin,
                            }
                        )
                time.sleep(0.1)
            raise AssertionError("Could not verify the signed candidate serving the committed guarded update")
        finally:
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
            self.report["violations"].append(
                {"phase": "upgrade", "kind": "provider process or turn identity changed"}
            )
        for phase in bounds:
            lo, hi = bounds[phase]
            streams = collections.Counter(r["thread"] for r in events if lo <= r["time"] <= hi)
            self.report.setdefault("observed_stream_events", {})[phase] = dict(streams)
            if any(streams[peer] < (1 if phase == "upgrade" else STABLE_SECONDS // 2) for peer in self.peers):
                self.report["violations"].append(
                    {
                        "phase": phase,
                        "kind": "provider streaming coverage",
                        "reason": "six sustained wire streams were not observed",
                    }
                )
            summary = self.report.setdefault("results", {}).setdefault(phase, {})
            for name, limits in BUDGETS_MS.items():
                rows = [r for r in samples if r["kind"] == name]
                lo, hi = bounds[phase]
                selected = [r for r in rows if lo <= r["start"] < hi]
                ok = lambda r: r["outcome"] in {"accepted", "success"}
                latency = sorted(1000 * (r["end"] - r["start"]) for r in selected if ok(r))
                good_ends = sorted(r["end"] for r in rows if ok(r) and lo <= r["end"] <= hi)
                earlier = [r["end"] for r in rows if ok(r) and r["end"] < lo]
                later = [r["end"] for r in rows if ok(r) and r["end"] > hi]
                points = (
                    [max(earlier, default=lo), *good_ends, min(later, default=hi)]
                    if phase == "upgrade"
                    else [lo, *good_ends, hi]
                )
                gap = max(b - a for a, b in zip(points, points[1:]))
                p50 = statistics.median(latency) if latency else None
                p95 = latency[math.ceil(len(latency) * 0.95) - 1] if latency else None
                result = {
                    "attempts": len(selected),
                    "successes": len(latency),
                    "p50_ms": p50,
                    "p95_ms": p95,
                    "maximum_seconds_without_success": gap,
                }
                summary[name] = result
                failures = []
                if phase != "upgrade":
                    if len(latency) < MINIMUM_SAMPLES or len(selected) != len(latency):
                        failures.append("insufficient successful interactions or request errors")
                    if not latency or p50 > limits[0] or p95 > limits[1]:
                        failures.append("interaction latency budget exceeded")
                if phase == "upgrade" and (not selected or gap >= MAXIMUM_GAP_SECONDS):
                    failures.append("missing cutover interaction or at least five seconds without success")
                for reason in failures:
                    self.report["violations"].append({"phase": phase, "kind": name, "reason": reason})

    def verify_receipts(self):
        # Start from both intent logs and the durable store. A client that
        # timed out after acceptance can otherwise disappear from the audit.
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
        counts = collections.Counter()
        records = self.records()
        for record in records:
            message = record.get("message", {})
            if record["kind"] == "input" and message.get("method") in {"turn/steer", "turn/start"}:
                counts.update(item.get("text") for item in message["params"]["input"])
        native = {}
        with sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True) as db:
            for tid, key, payload, state in db.execute(
                """
                    SELECT thread_id, request_id, payload_json, state FROM browser_submission_receipts
                    WHERE thread_id IN (?, ?)""",
                (self.peers[0], getattr(self, "composer", None)),
            ):
                marker = json.loads(payload)["body"]["message"]
                if marker == "stream":
                    continue
                native[key] = state
                if key not in attempts:
                    observation = browser.get(marker, {})
                    code = observation.get("status")
                    outcome = (
                        "accepted" if code and 200 <= code < 300 else "rejected" if code else "uncertain"
                    )
                    attempts[key] = (marker, outcome, observation.get("phase", "upgrade"), True)
        for marker, observation in browser.items():
            code = observation.get("status")
            key = observation.get("request_id")
            if key and key not in attempts:
                outcome = "accepted" if code and 200 <= code < 300 else "rejected" if code else "uncertain"
                attempts[key] = (marker, outcome, observation["phase"], True)
            elif not key and counts[marker] and not any(a[0] == marker for a in attempts.values()):
                self.report["violations"].append(
                    {"phase": observation["phase"], "kind": "browser delivery without receipt"}
                )
        for key, (marker, outcome, phase, via_browser) in attempts.items():
            state = native.get(key)
            delivered = counts[marker]
            self.report["receipts"].append(
                {
                    "request_id": key,
                    "http_outcome": outcome,
                    "phase": phase,
                    "browser": via_browser,
                    "durable_states": [state] if state else [],
                    "provider_deliveries": delivered,
                }
            )
            if (
                delivered > 1
                or (outcome == "accepted" or state in {"accepted", "completed", "uncertain"})
                and (delivered != 1 or state != "completed")
                or (outcome == "rejected" or state == "rejected")
                and delivered
            ):
                self.report["violations"].append(
                    {
                        "phase": phase,
                        "kind": "durable receipt or provider delivery disagreement",
                        "request_id": key,
                    }
                )

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
        p = self.root / "browser-observations.jsonl"
        rows = [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []
        self.report["browser_observations"] = rows
        self.report["browser_errors"] = (self.root / "browser-errors.log").read_text()[-1500:]
        for phase in self.report.get("phase_bounds", {}):
            for kind in ("browser_open", "browser_send"):
                selected = [r for r in rows if r["phase"] == phase and r["kind"] == kind]
                good = [r for r in selected if r["ok"]]
                stats = {"successes": len(good), "attempts": len(selected)}
                self.report.setdefault("browser_results", {}).setdefault(phase, {})[kind] = stats
                if phase == "upgrade":
                    lo, hi = self.report["wall_phase_bounds"][phase]
                    completions = sorted(r["end"] for r in rows if r["kind"] == kind and r["ok"])
                    before = max((end for end in completions if end <= lo), default=lo)
                    after = min((end for end in completions if end >= hi), default=hi)
                    points = [before, *(end for end in completions if lo < end < hi), after]
                    stats["maximum_seconds_without_success"] = (
                        max(b - a for a, b in zip(points, points[1:])) / 1000
                    )
                    if not selected or stats["maximum_seconds_without_success"] >= MAXIMUM_GAP_SECONDS:
                        self.report["violations"].append(
                            {
                                "phase": phase,
                                "kind": kind,
                                "reason": "missing upgrade traffic or availability gap",
                            }
                        )
                    continue
                if len(good) < 3 or len(good) != len(selected):
                    self.report["violations"].append(
                        {"phase": phase, "kind": kind, "reason": "failed or missing browser samples"}
                    )
                if kind == "browser_send" and good:
                    for metric, median, tail in [("own_ms", None, 50), ("other_ms", 150, 400)]:
                        times = sorted(r[metric] for r in good)
                        p50, p95 = statistics.median(times), times[math.ceil(len(times) * 0.95) - 1]
                        stats[metric] = {"p50": p50, "p95": p95}
                        if p95 > tail or (median is not None and p50 > median):
                            self.report["violations"].append(
                                {"phase": phase, "kind": metric, "reason": "browser latency budget exceeded"}
                            )

    def execute(self):
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
            if self.profile_sample:
                self.publish_phase("profiling")
                witness = self.report.get("candidate_runtime") or self.report["initial_runtime"]
                command = process_command(witness["pid"])
                if command == witness["process"] and str(self.root.resolve()) in command:
                    sample = subprocess.run(
                        ["/usr/bin/sample", str(witness["pid"]), "3", "-file", str(self.profile_sample)],
                        timeout=15,
                        capture_output=True,
                    )
                    self.report["profile_sample"] = {
                        "after_budget_window": True,
                        "pid": witness["pid"],
                        "created": sample.returncode == 0 and self.profile_sample.is_file(),
                        "path": str(self.profile_sample),
                    }
                else:
                    self.report["profile_sample"] = {
                        "created": False,
                        "reason": "Private coordinator identity changed",
                    }
        finally:
            self.stop_traffic.set()
            for thread in self.workers:
                thread.join(25)
            self.finish_browser()
            self.capture_update()
            if "phase_bounds" in self.report:
                self.summarize()
            if self.peers:
                self.verify_receipts()
            if "baseline" in self.report.get("results", {}) and self.old[1]["version"] == "0.1.12+63":
                old = self.report["initial_runtime"]
                red = any(
                    v["phase"] == "baseline" and "latency" in v.get("reason", "")
                    for v in self.report["violations"]
                )
                self.report["baseline_proof"] = {
                    "red": red,
                    "version": self.old[1]["version"],
                    "payload_digest": old["payload"],
                    "architecture": old["architecture"],
                }
        cover = self.report["workload"]["coverage"]
        cover["saved_histories_reopened"] = len(
            {
                r["fixture"]
                for r in self.report["samples"]
                if r["phase"] == "candidate"
                and r["kind"] == "history"
                and r["outcome"] == "success"
                and r["fixture"] in self.open_targets
            }
        )
        cover["accepted_verified"] = sum(
            r["http_outcome"] == "accepted"
            and r["durable_states"] == ["completed"]
            and r["provider_deliveries"] == 1
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
            self.report["violations"].append(
                {"phase": "upgrade", "kind": "coordinator or transport continuity"}
            )
        failures = self.report["violations"]
        baseline_red = any(v["phase"] == "baseline" and "latency" in v.get("reason", "") for v in failures)
        self.report["baseline_reproduced"] = baseline_red
        old = self.report["initial_runtime"]
        if self.old[1]["version"] == "0.1.12+63":
            proof = {
                "red": baseline_red,
                "version": self.old[1]["version"],
                "payload_digest": old["payload"],
                "architecture": old["architecture"],
            }
        else:
            ref = json.loads(self.affected_report.read_bytes()) if self.affected_report else {}
            proof = ref.get("baseline_proof") or {}
            if (
                ref.get("workload_contract") != self.report["workload_contract"]
                or proof.get("version") != "0.1.12+63"
            ):
                proof = {}
        self.report["baseline_proof"] = proof
        self.report["passed"] = bool(
            proof.get("red")
            and proof.get("architecture") == after["architecture"]
            and self.report.get("guarded_update_phase") == "committed"
            and not any(
                v["phase"] != "baseline" or "receipt" in v["kind"] or "durable" in v["kind"] for v in failures
            )
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("previous", "candidate", "snapshot", "report"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument(
        "--profile-sample",
        type=Path,
        help="Optional private three-second sample strictly after the candidate budget window",
    )
    parser.add_argument(
        "--affected-report",
        type=Path,
        help="Matching signed build 63 baseline receipt for a later healthy predecessor",
    )
    parser.add_argument(
        "--playwright-module", help="Installed Playwright module (default: demo/node_modules/playwright)"
    )
    parser.add_argument(
        "--candidate-only", action="store_true", help="Diagnostic only; never issues an upgrade pass"
    )
    args = parser.parse_args()
    old, new = signed_resources(args.previous), signed_resources(args.candidate)
    if old[1]["payload_digest"] == new[1]["payload_digest"]:
        parser.error("Two distinct signed builds are required")
    args.report = args.report.resolve()
    if args.report == args.snapshot.resolve() or any(
        args.report.is_relative_to(path.resolve())
        for path in (args.previous, args.candidate, Path(__file__).resolve().parents[1])
    ):
        parser.error(
            "Private reports must remain separate from the source snapshot, signed bundles and checkout"
        )
    runner = SignedUpgrade(args, old, new)
    try:
        try:
            runner.execute()
        except Exception as error:
            runner.report["execution_error"] = repr(error)
            raise
    finally:
        runner.persist()
        try:
            runner.close()
        finally:
            # Validation.close stops only processes proved to belong to the
            # temporary root and records any survivors.
            runner.persist()
            if "leftover_processes" in runner.report and not any(
                v["alive_after_cleanup"] for v in runner.report["leftover_processes"].values()
            ):
                shutil.rmtree(runner.root)
            else:
                runner.report.update(passed=False, standalone_passed=False)
                runner.report["violations"].append(
                    {"phase": "cleanup", "kind": "fixture process cleanup unverified"}
                )
                runner.persist()
    result = runner.report.get("standalone_passed", False) if args.candidate_only else runner.report["passed"]
    return 0 if result else 1


if __name__ == "__main__":
    sys.exit(main())
