#!/usr/bin/env python3
"""Run a private old-to-new signed coordinator upgrade with simultaneous interactive traffic.

On macOS, pass two signed .app directories, an offline SQLite snapshot for
database size, inventory proportions and two saved histories, and a private report pathname.
The fixture replaces original text and identities before booting. No operator state
is booted. The provider processes, HOME, database, transport and HTTP servers
are private to this invocation. The two app directories are read-only inputs.
"""
from __future__ import annotations

import argparse
import collections
from functools import lru_cache
from contextlib import closing
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
import tempfile
import sys
import threading
import time
import urllib.error
import urllib.request
import urllib.parse
import uuid

from measure_backend_latency import BUDGETS
from release_workload_receipt import MIN_ARCHIVE_ITEMS, MIN_ARCHIVE_TURNS, database_bytes
from runtime_upgrade_validation import Validation, listener_pids, process_command, processes_mentioning, runtime
from agent_coord import runtime_edge


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
# Collection limits only. Interaction failures and availability gaps are
# evaluated over the entire observed upgrade, including rollback snapshot time.
UPDATE_OBSERVATION_SECONDS = 600
UPDATE_PHASE_STALL_SECONDS = 120


def diagnostic_reason(error):
    # Control and provider exceptions can carry signed URLs, tokens or command arguments.
    # Match fixed operational messages without persisting arbitrary text from the runtime.
    message = str(error)
    for fragment, code in (
        ("three browser clients attached", "three-browser-attachments-missing"),
        ("Missing original pending", "pending-work-missing"),
        ("Pending approval, queue or original", "pending-work-changed"),
        ("original pending approval", "initial-pending-work-missing"),
        ("Original pending work did not complete", "pending-work-delivery-mismatch"),
        ("Could not verify the signed candidate", "serving-verification-timeout"),
        ("durable intake reconciliation", "intake-reconciliation-timeout"),
        ("Accepted operations did not drain", "admission-drain-timeout"),
        ("accepted submission still needs confirmation", "unresolved-accepted-submission"),
        ("forwarded request is still completing", "forward-in-progress"),
        ("Candidate exited before readiness", "candidate-exited"),
        ("Insufficient free space for the update rollback snapshot", "rollback-capacity"),
        ("Candidate validation failed", "candidate-validation"),
        ("did not confirm", "confirmation-missing"),
        ("released its rejected preparation", "preparation-rejected"),
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
    # Read only the dimensions required by the fixture; no real session IDs,
    # directories, queued jobs, user text, credentials or integrations cross.
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
    # Use the store API to generate complete sessions and project/workspace
    # affiliations using only private coordinates.
    for client, attention, count in rows:
        for number in range(count):
            name = f"Inventory {client} {attention} {number}"
            thread = str(uuid.uuid5(uuid.NAMESPACE_OID, name))
            store.register(session_id=thread, client=client, cwd=str(root), name=name)
            with store._connection() as db:
                db.execute("UPDATE work_threads SET attention=? WHERE thread_id=?", (attention, thread))


def match_snapshot_size(snapshot, database):
    """Maintain the rollback copy cost without importing private tables or jobs."""
    source_bytes = database_bytes(snapshot)
    fixture_bytes = database_bytes(database)
    if fixture_bytes < source_bytes:
        batch_bytes = 8 * 1024**2
        # Allow room for the added data, one complete rollback copy and the
        # temporary journal for an individual insert.
        required_space = source_bytes - fixture_bytes + source_bytes + 2 * batch_bytes
        if shutil.disk_usage(database.parent).free < required_space:
            raise RuntimeError("Insufficient space for the full-size private upgrade fixture and rollback copy")
        with closing(sqlite3.connect(database, isolation_level=None)) as db:
            page_size = db.execute("PRAGMA page_size").fetchone()[0]
            db.execute("CREATE TABLE IF NOT EXISTS release_workload_storage (content BLOB NOT NULL)")
            while fixture_bytes < source_bytes:
                db.execute("INSERT INTO release_workload_storage VALUES (zeroblob(?))",
                           (min(batch_bytes, source_bytes - fixture_bytes),))
                fixture_bytes = db.execute("PRAGMA page_count").fetchone()[0] * page_size
    return {"source_database_bytes": source_bytes, "fixture_database_bytes": fixture_bytes}


def copy_saved_histories(snapshot, database, root, *, manifest_thread=None):
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
    with closing(sqlite3.connect(snapshot.resolve(strict=True).as_uri() + "?mode=ro", uri=True)) as source:
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
        ids = legacy + ([manifest_thread] if manifest_thread else []) + [
            r[0]
            for r in source.execute(
                "SELECT thread_id FROM browser_sessions WHERE archived=0 ORDER BY updated_at DESC LIMIT 200"
            )
        ]
        for source_id in dict.fromkeys(ids):
            row = source.execute(
                "SELECT history_json FROM browser_history WHERE thread_id=?", (source_id,)
            ).fetchone()
            if not row:
                continue
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
            if band == "medium" and (storage_format != "manifest" or stored.get("layout") != "turns"
                    or len(turns) < MIN_ARCHIVE_TURNS or items < MIN_ARCHIVE_ITEMS
                    or manifest_thread and source_id != manifest_thread):
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
                    "items": items,
                    "last_turn": safe["turns"][-1]["id"],
                    "storage_format": storage_format,
                }
            )
            if len(refs) == 2:
                return refs
    raise ValueError("Snapshot needs a long block-backed medium conversation and a large saved conversation")


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
        self.manifest_thread = getattr(args, "saved_manifest_thread", None)
        self.affected_report = args.affected_report
        self.source_baseline_report = getattr(args, "source_baseline_report", None)
        self.candidate_only = args.candidate_only
        self.stable_origin = getattr(args, "stable_origin", False)
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
            if getattr(self, "stable_origin", False):
                arguments.append("--runtime-stable-origin")
            elif self.gateway_capable():
                arguments.append("--runtime-interaction-continuity")
        return self.signed_helper(resources, arguments)

    def gateway_capable(self):
        return not getattr(self, "stable_origin", False) and (
            self.new[0] / "backend/agent_coord/runtime_gateway_lifecycle.py").is_file()

    def bind_stable_origin(self, held):
        directory = runtime.runtime_directory(self.database)
        edge = runtime_edge.read_address(directory)
        if (not edge or held.get("edge_url") != edge["url"]
                or held.get("edge_observer") != edge["observer"]):
            raise RuntimeError("The verified payload did not establish a stable origin")
        command = process_command(edge["pid"]) or ""
        if ("-m agent_coord.runtime_edge" not in command or str(directory) not in command
                or not any(str(directory / "payloads" / bundle[1]["payload_digest"]) in command
                           for bundle in (self.old, self.new))):
            raise RuntimeError("The stable listener does not execute a retained signed payload")
        self.edge = edge
        self.edge_observations = []
        self.address = edge["url"], fetch(edge["url"], "", "config", timeout=5)["token"]
        self.report["stable_origin"] = {"url": edge["url"], "listener_instance": edge["instance"]}

    def observe_stable_origin(self, stop):
        while not stop.is_set():
            before = time.monotonic()
            try:
                status = runtime_edge.probe(self.edge) or {}
            except (OSError, ValueError, http.client.HTTPException):
                status = {}
            self.edge_observations.append({"before": before, "after": time.monotonic(),
                "serving": status.get("instance") if status.get("status") == "serving" else None,
                "edge_instance": status.get("edge_instance")})
            stop.wait(.01)

    def verify_stable_origin(self):
        report = self.report["stable_origin"]
        previous = self.report["initial_runtime"]["instance"]
        successor = self.report["candidate_runtime"]["instance"]
        observations = self.edge_observations
        ready = next((sample for sample in observations if sample["serving"] == successor), None)
        before = max((sample["before"] for sample in observations if sample["serving"] == previous
                      and ready and sample["after"] < ready["before"]), default=None)
        milliseconds = 1000 * (ready["after"] - before) if ready and before is not None else None
        report["cutover_ms"] = max(milliseconds, report.get("fenced_ms") or 0) if milliseconds is not None else None
        traffic = [row for row in self.report["samples"]
                   if row["phase"] == "upgrade" and row["kind"] in {"send", "detail", "history"}]
        browser = [row for row in self.report.get("browser_observations", [])
                   if row["phase"] == "upgrade" and row["kind"] in {"browser_send", "browser_open"}]
        sends = [1000 * (row["end"] - row["start"]) for row in traffic if row["kind"] == "send"]
        sends.extend(row["responded"] - row["requested"] for row in browser if row["kind"] == "browser_send"
                     and row.get("responded") is not None and row.get("requested") is not None)
        sends.sort()
        report["send_p99_ms"] = sends[math.ceil(len(sends) * .99) - 1] if sends else None
        report["http_failures"] = sum(row["outcome"] not in {"accepted", "success"} for row in traffic)
        report["http_failures"] += sum(not row["ok"] for row in browser)
        attachments = [row for row in self.report.get("browser_observations", [])
                       if row["kind"] == "browser_attachment"]
        network_path = self.root / "browser-network.json"
        document_loads = collections.Counter()
        if network_path.exists():
            for key, count in json.loads(network_path.read_text())["counts"].items():
                phase, page, origin, _, route, result = key.split(" ", 5)
                if route == "document":
                    document_loads[page] += count
                if (phase == "upgrade" and route in {"sessions.messages", "sessions.detail", "threads.detail"}
                        and (not result.isdigit() or int(result) >= 400)):
                    report["http_failures"] += count
        report["browser_reloads"] = sum(max(0, count - 1) for count in document_loads.values())
        report["verified"] = bool(ready and before is not None and traffic and browser and sends
            and report["cutover_ms"] < 250 and report["send_p99_ms"] < 150
            and not report["http_failures"] and not report["browser_reloads"]
            and document_loads == {"opening": 1, "composing": 1, "receiving": 1}
            and {row["page"] for row in attachments} == {"opening", "composing", "receiving"}
            and all(row["origin"] == report["url"].rstrip("/") for row in attachments)
            and all(row["edge_instance"] in {None, report["listener_instance"]} for row in observations))
        if not report["verified"]:
            self.report["violations"].append({"phase": "upgrade", "kind": "stable origin qualification failed"})

    def gateway_control(self, route, *, body=None):
        directory = runtime.runtime_directory(self.database)
        record = runtime.read_json(directory / "interaction-gateway/gateway.json")
        target = str(directory / "payloads" / self.new[1]["payload_digest"])
        address = urllib.parse.urlsplit(record["url"])
        if (record["database"] != str(self.database.resolve()) or record["payload"] != target
                or record["url"] != f"http://127.0.0.1:{address.port}/"):
            raise RuntimeError("The gateway does not belong to this private signed upgrade")
        witness = self.report.get("interaction_gateway")
        if witness and (record["url"], record["instance"]) != (witness["url"], witness["instance"]):
            raise RuntimeError("The qualified gateway changed identity")
        with closing(http.client.HTTPConnection("127.0.0.1", address.port, timeout=2)) as connection:
            connection.request("POST" if body is not None else "GET", "/_runtime/" + route,
                         json.dumps(body) if body is not None else None,
                         {"Authorization": "Bearer " + record["control_token"], "Content-Type": "application/json"})
            response = connection.getresponse()
            if response.status != 200:
                raise RuntimeError(f"Private gateway {route} returned HTTP {response.status}")
            result = json.loads(response.read(65537))
        if route == "identity" and any(result.get(k) != record[k] for k in ("database", "payload", "url", "instance")):
            raise RuntimeError("The private gateway identity handshake failed")
        return result

    def wait_for_gateway(self, predicate, *, timeout, description):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.25)
        raise AssertionError("Timed out waiting for " + description)

    def wait_for_intake_settlement(self, deadline):
        """Keep observing while outstanding work shrinks under continuous traffic."""
        intake = runtime.runtime_directory(self.database) / "interaction-gateway/message-intake.sqlite3"
        now = time.monotonic()
        stall_deadline = now + UPDATE_PHASE_STALL_SECONDS
        next_sample = now
        lowest = None
        while now < min(deadline, stall_deadline):
            if self.gateway_control("identity").get("settled") is True:
                return
            if now >= next_sample and intake.is_file():
                with closing(sqlite3.connect(intake.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
                    pending, = db.execute("SELECT COUNT(*) FROM intake WHERE state IN ('uncertain','queued')").fetchone()
                self.report.setdefault("intake_settlement_observations", []).append({"at_ms": time.time()*1000,
                                                                                   "pending": pending})
                if lowest is None or pending < lowest:
                    lowest = pending
                    stall_deadline = now + UPDATE_PHASE_STALL_SECONDS
                next_sample = now + 5
            time.sleep(.25)
            now = time.monotonic()
        raise AssertionError("Timed out waiting for durable intake reconciliation")

    def route_source_interactions(self, held, target, phase="baseline"):
        identity = self.gateway_control("identity")
        if held.get("interaction_url") != identity["url"] or held.get("interaction_payload") != target:
            raise RuntimeError("The staged helper did not bind the exact signed interaction endpoint")
        config = fetch(identity["url"], "", "config", timeout=3)
        if config.get("interactionContinuity") is not True or not config.get("token"):
            raise RuntimeError("The private gateway did not advertise interaction continuity")
        owners = listener_pids(urllib.parse.urlsplit(identity["url"]).port)
        if owners is None or len(owners) != 1:
            raise RuntimeError("Could not verify the owner of the private gateway listener")
        pid, = owners
        command = process_command(pid) or ""
        if not all(value in command for value in ("--runtime-interaction-gateway", str(self.database), target)):
            raise RuntimeError("The gateway listener is not executing the private signed payload")
        self.report["interaction_gateway"] = {key: identity[key] for key in ("instance", "database", "payload", "url", "settled")}
        self.report["interaction_gateway"]["pid"] = pid
        visit = config.get("visitTransport")
        if visit:
            address = urllib.parse.urlsplit(visit["origin"])
            if (address.scheme != "http" or address.hostname != "127.0.0.1" or not address.port
                    or visit["origin"] != f"http://127.0.0.1:{address.port}"
                    or listener_pids(address.port) != owners or not visit.get("token")):
                raise RuntimeError("Visit reporting is outside the verified private gateway")
            self.report["interaction_gateway"]["visit_origin"] = visit["origin"]
        self.address = identity["url"], config["token"]
        # Authenticate the actual three browser pages on the held source.
        # A slow old coordinator may never finish a send. Every attempt is
        # measured and audited but its success is not admission to the rehearsal.
        routed_at = int(time.time() * 1000)
        self.report["interaction_gateway"]["source_routed_at_ms"] = routed_at
        self.publish_phase(phase)
        self.wait_for_gateway(lambda: self.browser_attached(identity["url"], routed_at, phase),
                              timeout=60, description="three browser clients attached to the staged source")
        self.report["interaction_gateway"]["source_clients_verified"] = True

    def browser_attached(self, url, since, phase):
        path = self.root / "browser-observations.jsonl"
        if not path.exists():
            return False
        origin = url.rstrip("/")
        rows = (json.loads(line) for line in path.read_bytes().splitlines(keepends=True) if line.endswith(b"\n"))
        return {r["page"] for r in rows if r.get("kind") == "browser_attachment"
                and r.get("origin") == origin and r.get("phase") == phase and r.get("start", 0) >= since
                and r.get("continuity") is True and r.get("status") == 200} == {"opening", "composing", "receiving"}

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
        if getattr(self, "exercise_pending_work", False):
            self.prepare_pending_work()
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
                if name == "send" and "transferSubmission" in value:
                    receipt = value["transferSubmission"]
                    if receipt.get("requestId") != key or receipt.get("threadId") != self.peers[0]:
                        raise ValueError("Transfer receipt does not identify the submitted request")
                    entry["transfer_state"] = receipt["state"]
                entry["outcome"] = "accepted" if name == "send" else "success"
            except urllib.error.HTTPError as error:
                with error:
                    entry.update(
                        outcome="rejected", status=error.code, detail=diagnostic_reason(error.read(200).decode(errors="replace"))
                    )
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
        temporary.write_text(
            json.dumps(
                {"phase": name, "url": self.address[0], "peers": self.open_targets, "composer": self.composer,
                 "visit_origin": (self.report["interaction_gateway"].get("visit_origin")
                                 if self.report.get("interaction_gateway", {}).get("url") == self.address[0] else None)}
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
        if getattr(self, "stable_origin", False):
            update = runtime_edge.adoption_status(directory, {"update": update})["update"]
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
                "admission_drain_started_at",
                "admission_drain_seconds",
                "blocked",
            )
        }
        if update.get("error"):
            self.report["guarded_control"]["error"] = diagnostic_reason(update["error"])
        # The final receipt can overwrite the start of an earlier drain attempt.
        # Keep each observed change, including a drain that reopened admission.
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
        return held, record, target

    def observe_control_files(self, stop):
        # The signed activation command can block. Observe only atomically replaced
        # files while it runs, without competing for the coordinator writer or API.
        directory = runtime.runtime_directory(self.database)
        timeline = self.report["transition_control_timeline"] = []
        last = None
        while True:
            sample = {}
            for label, path in (("gateway", directory / "interaction-gateway/transition.json"),
                                ("activation", directory / "interaction-gateway/activation-request.json"),
                                ("backend", directory / "update.json")):
                try:
                    row = json.loads(path.read_bytes())
                except FileNotFoundError:
                    continue
                except (OSError, ValueError) as error:
                    sample[label] = {"read_error": diagnostic_reason(error)}
                    continue
                if row.get("request_id", row.get("id")) != self.report["activation_request_id"]:
                    continue
                sample[label] = {k: row[k] for k in ("state", "phase", "released", "admission_drain_seconds")
                                 if k in row}
                if row.get("error"):
                    sample[label]["error"] = diagnostic_reason(row["error"])
            if sample != last:
                timeline.append({"at_ms": int(time.time() * 1000), **sample})
                last = sample
            if stop.wait(.25):
                return

    def swap_signed(self, staged=None):
        gateway = self.gateway_capable()
        stable = getattr(self, "stable_origin", False)
        held, record, target = staged or self.stage_signed()
        initial = self.report["initial_runtime"]
        if gateway and staged is None:
            # Other rehearsals can call swap_signed directly on an attached source.
            try:
                self.route_source_interactions(held, target)
            finally:
                if "baseline" in self.report.get("phase_bounds", {}):
                    self.report["phase_bounds"]["baseline"][1] = time.monotonic()
                    self.report["wall_phase_bounds"]["baseline"][1] = time.time() * 1000
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
        edge_stop = threading.Event()
        edge_watcher = None
        if stable:
            # The predecessor and replacement must both be sampled, even when
            # the activation command returns before the route is switched.
            edge_watcher = threading.Thread(target=self.observe_stable_origin, args=(edge_stop,), daemon=True)
            edge_watcher.start()
        if getattr(self, "peers", None):
            stop = threading.Event()
            watcher = threading.Thread(target=self.observe_control_files, args=(stop,), daemon=True)
            watcher.start()
        try:
            try:
                if stable:
                    self.signed_helper(self.new[0], ["--runtime-command", "activate-update", "--runtime-stable-origin"],
                                       body={"payload": target, "request_id": request}, timeout=75)
                elif gateway:
                    self.signed_helper(self.new[0], ["--runtime-command", "activate-update",
                                       "--runtime-interaction-continuity"],
                                       body={"payload": target, "request_id": request}, timeout=75)
                else:
                    runtime.control(record, "activate-update", payload=target, request_id=request, timeout=35)
            except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
                # A lost response can follow a durable admission. Do not send
                # the command twice; observe the decision for this request ID.
                errors.append(
                    {
                        "operation": "activate-update",
                        "error": diagnostic_reason(error),
                        "after_seconds": time.monotonic() - begin,
                    }
                )
            deadline = begin + UPDATE_OBSERVATION_SECONDS
            phase_deadline = time.monotonic() + UPDATE_PHASE_STALL_SECONDS
            previous_phase = None
            while time.monotonic() < min(deadline, phase_deadline):
                phase = self.capture_update()
                if phase and phase != previous_phase:
                    previous_phase = phase
                    phase_deadline = time.monotonic() + UPDATE_PHASE_STALL_SECONDS
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
                            if stable:
                                status = runtime_edge.adoption_status(runtime.runtime_directory(self.database), status)
                            if (
                                status.get("status") == "serving"
                                and status.get("payload") == target
                                and (status.get("update") or {}).get("id") == request
                                and status["update"].get("phase") == "committed"
                            ):
                                operation = "browser config"
                                direct = (
                                    status["url"],
                                    fetch(status["url"], "", "config", timeout=3)["token"],
                                )
                                self.report["candidate_runtime"] = self.identity(status)
                                if stable:
                                    edge_status = runtime_edge.probe(self.edge) or {}
                                    if (edge_status.get("status") != "serving"
                                            or edge_status.get("payload") != target
                                            or edge_status.get("instance") != candidate["instance"]
                                            or edge_status.get("edge_instance") != self.edge["instance"]):
                                        continue
                                    if fetch(*self.address, "config", timeout=3)["token"] != self.address[1]:
                                        raise RuntimeError("The browser token changed during activation")
                                    self.report["stable_origin"]["fenced_ms"] = edge_status.get("edge_handover_ms")
                                    time.sleep(.03)  # Let the observer collect the confirmed route.
                                elif gateway:
                                    if self.address[0] == direct[0]:
                                        raise AssertionError("Interaction gateway is the coordinator endpoint")
                                    self.candidate_direct_address = direct
                                    self.wait_for_intake_settlement(deadline)
                                    self.report["interaction_gateway"]["settled"] = True
                                else:
                                    self.address = direct
                                self.report["guarded_update_phase"] = phase
                                return
                    except (OSError, ValueError, RuntimeError) as error:
                        errors.append(
                            {
                                "operation": operation,
                                "error": diagnostic_reason(error),
                                "after_seconds": time.monotonic() - begin,
                            }
                        )
                time.sleep(0.1)
            raise AssertionError("Could not verify the signed candidate serving the committed guarded update")
        finally:
            if edge_watcher is not None:
                edge_stop.set()
                edge_watcher.join(1)
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
                    failures.append("interaction latency budget exceeded" if latency else "no completed latency samples")
                if phase == "upgrade" and (not selected or gap >= MAXIMUM_GAP_SECONDS):
                    failures.append("missing cutover interaction or at least five seconds without success")
                for reason in failures:
                    self.report["violations"].append({"phase": phase, "kind": name, "reason": reason})

    def pending_state(self, pending):
        address = getattr(self, "candidate_direct_address", self.address)
        detail = fetch(*address, f"sessions/{pending['approval_thread']}")
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
        # Release only the two synthetic items whose identities survived the swap.
        fetch(*self.address, f"sessions/{pending['approval_thread']}/requests/{urllib.parse.quote(key, safe='')}",
              {"decision": "accept"})
        (self.root / "release").touch()
        def both_observed():
            effects = self.pending_effects()
            return effects["queued_delivery_count"] and effects["answer_count"]
        self.wait(both_observed, timeout=60)
        effects = pending["effects"] = self.pending_effects()
        if not effects["original_approval_answered_once"] or not effects["original_queue_delivered_once"]:
            raise AssertionError("Original pending work did not complete exactly once")

    def observe_active_receipts(self, when):
        """Compare the original request with its independent provider input, without issuing any work."""
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
            # Previously observed pending work must remain addressable under its original key.
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

    def terminal_receipt_service(self, intake, started, now):
        """Measure queue age and canonical service time separately, using the original receipt keys."""
        with closing(sqlite3.connect(intake.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
            db.execute("ATTACH DATABASE ? AS canonical", (self.database.as_uri() + "?mode=ro",))
            by_thread = {}
            for role, thread in (("direct", self.peers[0]), ("browser", self.composer)):
                rows = db.execute("""SELECT i.state, i.created_at, r.state, r.created_at, r.updated_at
                    FROM intake i LEFT JOIN canonical.browser_submission_receipts r
                    ON i.thread_id=r.thread_id AND i.request_id=r.request_id WHERE i.thread_id=?""",
                    (thread,)).fetchall()
                completed = [(created, end) for _, _, state, created, end in rows
                             if state == "completed" and end >= started]
                pending = [accepted for state, accepted, _, _, _ in rows if state in {"queued", "uncertain"}]
                durations = sorted((end - created) * 1000 for created, end in completed)
                by_thread[role] = {
                    "intake_arrivals_since_draining": sum(accepted >= started for _, accepted, _, _, _ in rows),
                    "intake_outstanding": len(pending),
                    "oldest_outstanding_age_s": max(0, now - min(pending)) if pending else None,
                    "canonical_completed_since_draining": len(completed),
                    "canonical_completed_last_30s": sum(end >= max(started, now - 30) for _, end in completed),
                    "canonical_service_ms_p50": statistics.median(durations) if durations else None,
                    "canonical_service_ms_p95": durations[math.ceil(len(durations) * .95) - 1] if durations else None,
                    "canonical_service_total_s": sum(durations) / 1000,
                }
            return by_thread

    def capture_transition_diagnostics(self, when):
        diagnostic = self.report.setdefault("transition_diagnostics", {})[when] = {
            "phase": self.phase, "at_ms": int(time.time() * 1000)}
        gateway = runtime.runtime_directory(self.database) / "interaction-gateway"
        diagnostic["gateway_record_exists"] = (gateway / "gateway.json").is_file()
        try:
            path = gateway / "transition.json"
            if path.is_file():
                fence = json.loads(path.read_bytes())
                diagnostic["gateway_state"] = {k: fence.get(k) for k in ("state", "request_id", "released")}
            intake = gateway / "message-intake.sqlite3"
            if intake.is_file():
                with closing(sqlite3.connect(intake.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
                    diagnostic["intake_states"] = dict(db.execute("SELECT state,COUNT(*) FROM intake GROUP BY state"))
                started = next((row["at_ms"] for row in self.report.get("transition_control_timeline", [])
                                if row.get("gateway", {}).get("state") == "draining"), None)
                if started is not None and self.peers and self.composer:
                    diagnostic["terminal_receipt_service"] = self.terminal_receipt_service(
                        intake, started / 1000, diagnostic["at_ms"] / 1000)
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
        destinations = collections.defaultdict(list)
        handoffs = {}
        counts = self.provider_input_counts(destinations, handoffs)
        native = {}
        native_payloads = {}
        native_turns = {}
        admissions = {}
        transfer_phase_names = {"baseline": "staged_source", "upgrade": "handoff", "gateway_candidate": "resumed_gateway"}
        client_origins = {r["request_id"]: r["origin"] for r in self.report["samples"]
                          if r["kind"] == "send" and "origin" in r}
        client_origins.update({r["request_id"]: r["post_origin"] for r in browser.values()
                               if r.get("request_id") and r.get("post_origin")})

        with closing(sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True)) as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(browser_submission_receipts)")}
            result_column = "result_json" if "result_json" in columns else "NULL"
            for tid, key, payload, state, result in db.execute(
                f"""
                    SELECT thread_id, request_id, payload_json, state, {result_column} FROM browser_submission_receipts
                    WHERE thread_id IN (?, ?)""",
                (self.peers[0], getattr(self, "composer", None)),
            ):
                saved_body = json.loads(payload)["body"]
                marker = saved_body["message"]
                if marker == "stream":
                    continue
                if key in native:
                    self.report["violations"].append({"phase": "upgrade", "kind": "duplicate original receipt key",
                                                      "request_id": key})
                native[key] = state
                native_payloads[key] = tid, saved_body
                if result:
                    native_result = json.loads(result)
                    native_turns[key] = (native_result.get("turn") or {}).get("id") or native_result.get("turnId")
                if key not in attempts:
                    observation = browser.get(marker, {})
                    code = observation.get("status")
                    outcome = (
                        "accepted" if code and 200 <= code < 300 else "rejected" if code else "uncertain"
                    )
                    attempts[key] = (marker, outcome, observation.get("phase", "upgrade"), tid == self.composer)
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
        def matches_target(key, marker, thread, expected):
            targets = destinations[marker]
            if len(targets) != 1 or targets[0][0] != thread:
                return False
            if not expected or targets[0][1] == expected:
                return True
            # A native start after the explicitly targeted turn is valid
            # only when the wire log proves it was the latest completion
            # and the ordinary receipt names the exact successor created.
            handoff = handoffs.get(marker, {})
            return bool(targets[0][1] is None and expected == handoff.get("previous") and
                        native_turns.get(key) and native_turns[key] == handoff.get("turn"))

        if self.report.get("interaction_gateway"):
            # Audit in both directions. A lost browser response or provider
            # receipt cannot erase a request already accepted by the gateway.
            gateway_origin = self.report["interaction_gateway"]["url"].rstrip("/")
            intake = runtime.runtime_directory(self.database) / "interaction-gateway/message-intake.sqlite3"
            with closing(sqlite3.connect(intake.resolve(strict=True).as_uri() + "?mode=ro", uri=True)) as db:
                rows = db.execute("SELECT thread_id,request_id,body,state,created_at FROM intake ORDER BY sequence").fetchall()
            transferred = set()
            registration_counts = collections.defaultdict(collections.Counter)
            for thread, key, body, state, at in rows:
                original = json.loads(body)
                marker = original["message"]
                route = next((r for r in reversed(self.report.get("interaction_routes", []))
                              if r["at_ms"] <= 1000 * at), {})
                phase = transfer_phase_names.get(route.get("phase"), "unclassified")
                if route.get("origin") != gateway_origin:
                    phase = "outside_gateway_window"
                target_matches = matches_target(key, marker, thread, original.get("expectedTurnId"))
                admissions[key] = {"phase": phase, "at_ms": 1000 * at, "state": state,
                                   "provider_target_matches": target_matches}
                channel = "browser" if thread == self.composer else "direct"
                registration_counts[phase][channel] += 1
                # Native receipts persist semantic composer fields. The private intake retains
                # routing fields as well, including the original request ID and expected turn.
                semantic = {k: v for k, v in original.items()
                            if k not in {"requestId", "windowId", "activity", "expectedTurnId"}}
                if key in transferred or original.get("requestId") != key:
                    self.report["violations"].append({"phase": "upgrade", "kind": "gateway registration key disagreement",
                                                      "request_id": key})
                transferred.add(key)
                via_browser = thread == self.composer
                attempts.setdefault(key, (marker, "uncertain", route.get("phase", "unknown"), via_browser))
                if (thread not in {self.peers[0], self.composer} or state != "committed"
                        or native.get(key) != "completed" or counts[marker] != 1 or attempts[key][0] != marker
                        or not target_matches or native_payloads.get(key) != (thread, semantic)
                        or attempts[key][1] == "rejected"):
                    self.report["violations"].append({"phase": "upgrade", "kind": "gateway receipt disagreement",
                                                      "request_id": key, "gateway_state": state})
            acknowledged = {r["request_id"] for r in self.report["samples"] if "transfer_state" in r}
            acknowledged.update(r["request_id"] for r in browser.values() if r.get("status") == 202 and r.get("request_id"))
            for key in acknowledged - transferred:
                self.report["violations"].append({"phase": "upgrade", "kind": "missing gateway receipt", "request_id": key})
            self.report["interaction_gateway"]["receipts"] = {
                "total": len(rows), "states": dict(collections.Counter(r[3] for r in rows)),
                "intake_registrations_by_phase": dict(registration_counts),
                "browser": sum(r[0] == self.composer for r in rows),
                "direct": sum(r[0] == self.peers[0] for r in rows),
            }
            if self.report.get("baseline_endpoint") == "staged_gateway":
                if any(registration_counts["handoff"][kind] == 0 for kind in ("browser", "direct")):
                    self.report["violations"].append({"phase": "upgrade", "kind": "missing handoff intake receipt coverage"})
            if not any(r[0] == self.composer for r in rows) or not any(r[0] == self.peers[0] for r in rows):
                self.report["violations"].append({"phase": "upgrade", "kind": "missing gateway receipt coverage"})
        for key, (marker, outcome, phase, via_browser) in attempts.items():
            state = native.get(key)
            delivered = counts[marker]
            thread = self.composer if via_browser else self.peers[0]
            observation = browser.get(marker, {}) if via_browser else {}
            expected = observation.get("expected_turn_id") if via_browser else getattr(self, "turn_id", None)
            receipt_thread, receipt_body = native_payloads.get(key, (None, {}))
            identity_matches = receipt_thread == thread and receipt_body.get("message") == marker
            confirmed_target = (identity_matches and matches_target(key, marker, thread, expected) and
                                admissions.get(key, {}).get("provider_target_matches", True))
            if observation.get("receipt_turn_id") and observation.get("status") == 200:
                confirmed_target = confirmed_target and observation["receipt_turn_id"] == native_turns.get(key)
            self.report["receipts"].append(
                {
                    "request_id": key,
                    "http_outcome": outcome,
                    "phase": phase,
                    "browser": via_browser,
                    "durable_states": [state] if state else [],
                    "provider_deliveries": delivered,
                    "provider_target_matches": confirmed_target,
                    "client_origin": client_origins.get(key),
                    "gateway_registration": admissions.get(key),
                }
            )
            if (
                delivered > 1
                or (outcome == "accepted" or state in {"accepted", "completed", "uncertain"})
                and (delivered != 1 or state != "completed" or not confirmed_target)
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

        if self.report.get("baseline_endpoint") == "staged_gateway" and self.report.get("interaction_gateway"):
            completed = {name: {"browser": 0, "direct": 0} for name in transfer_phase_names.values()}
            for row in self.report["receipts"]:
                if (row["http_outcome"] == "accepted" and row["client_origin"] == gateway_origin
                        and row["phase"] in transfer_phase_names and row["durable_states"] == ["completed"]
                        and row["provider_deliveries"] == 1 and row["provider_target_matches"]):
                    completed[transfer_phase_names[row["phase"]]]["browser" if row["browser"] else "direct"] += 1
            self.report["interaction_gateway"]["receipts"]["confirmed_client_work_by_phase"] = completed
            for phase, channels in completed.items():
                if not all(channels.values()):
                    self.report["violations"].append({"phase": phase, "kind": "missing confirmed gateway client receipts"})

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
        self.report["browser_process"] = {"exit_code": self.browser.returncode if isinstance(self.browser.returncode, int) else None,
                                          "stderr_bytes": (self.root / "browser-errors.log").stat().st_size}
        for phase in self.report.get("phase_bounds", {}):
            for kind in ("browser_open", "browser_send"):
                selected = [r for r in rows if r["phase"] == phase and r["kind"] == kind]
                good = [r for r in selected if r["ok"]]
                stats = {"successes": len(good), "attempts": len(selected)}
                self.report.setdefault("browser_results", {}).setdefault(phase, {})[kind] = stats
                if kind == "browser_open" and good:
                    elapsed = sorted(r["end"] - r["start"] for r in good)
                    stats["successful_open_ms"] = {"count": len(elapsed), "p50": statistics.median(elapsed),
                                                   "p95": elapsed[math.ceil(len(elapsed) * 0.95) - 1], "max": elapsed[-1]}
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
                        self.report["violations"].append(
                            {
                                "phase": phase,
                                "kind": kind,
                                "reason": "missing upgrade traffic or availability gap",
                            }
                        )
                if phase == "upgrade" and not good:
                    self.report["violations"].append({"phase": phase, "kind": kind, "reason": "no completed browser samples"})
                elif phase != "upgrade" and (len(good) < 3 or len(good) != len(selected)):
                    self.report["violations"].append(
                        {"phase": phase, "kind": kind, "reason": "failed or missing browser samples"}
                    )
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
                            self.report["violations"].append(
                                {"phase": phase, "kind": metric, "reason": "browser latency budget exceeded"}
                            )

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
                staged = None
                if self.stable_origin:
                    staged = self.stage_signed()
                    self.bind_stable_origin(staged[0])
                    self.window("baseline")
                elif self.gateway_capable():
                    self.report["baseline_endpoint"] = "staged_gateway"
                    self.record_source_baseline()
                    staged = self.stage_signed()
                    started = time.monotonic()
                    wall_start = time.time() * 1000
                    try:
                        self.route_source_interactions(staged[0], staged[2], "baseline")
                        self.window("baseline")
                    finally:
                        self.report.setdefault("phase_bounds", {})["baseline"] = [started, time.monotonic()]
                        self.report.setdefault("wall_phase_bounds", {})["baseline"] = [wall_start, time.time() * 1000]
                else:
                    self.window("baseline")
                self.swap_signed(staged)
                if self.report.get("interaction_gateway"):
                    self.window("gateway_candidate")
                    self.address = self.candidate_direct_address
                self.window("candidate")
                self.verify_pending_work()
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
            if self.stable_origin and self.report.get("candidate_runtime"):
                self.verify_stable_origin()
            if self.report.get("pending_work", {}).get("preserved"):
                self.release_pending_work()
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
            raw = self.affected_report.read_bytes() if self.affected_report else b"{}"
            ref = json.loads(raw)
            if self.affected_report and hashlib.sha256(raw).hexdigest() != self.report["affected_report"]["sha256"]:
                raise AssertionError("Affected release proof changed during validation")
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
                self.report.get("baseline_endpoint") == "staged_gateway" or v["phase"] != "baseline"
                or "receipt" in v["kind"] or "durable" in v["kind"] for v in failures
            )
        )

    def record_source_baseline(self):
        # Preserve the exact previous receiver's direct workload, including
        # failures and unresolved receipts. Its source window now runs through
        # the staged production endpoint and has the full release budgets.
        path = self.source_baseline_report or self.affected_report
        try:
            raw = path.read_bytes() if path else b"{}"
            ref = json.loads(raw)
        except (ValueError, OSError):
            ref = {}
        if (ref.get("workload_contract") != self.report["workload_contract"]
                or ref.get("initial_runtime", {}).get("payload") != self.old[1]["payload_digest"]
                or ref.get("interaction_gateway") or not ref.get("receipts")
                or not ref.get("browser_results", {}).get("baseline")):
            self.report["violations"].append({"phase": "baseline", "kind": "missing signed direct source baseline receipt"})
            return
        self.report["direct_source_baseline"] = {
            "path": str(path.resolve()), "sha256": hashlib.sha256(raw).hexdigest(),
            "payload": ref["initial_runtime"]["payload"], "browser_baseline": ref["browser_results"]["baseline"],
            "receipt_count": len(ref["receipts"]), "receipt_violations": sum(
                "receipt" in v["kind"] or "durable" in v["kind"] for v in ref.get("violations", [])),
        }

    def close(self):
        directory = runtime.runtime_directory(self.database) / "interaction-gateway"
        if (directory / "gateway.json").exists():
            try:
                identity = self.gateway_control("identity")
                result = self.gateway_control("retire", body={})
                if not identity.get("settled") or result.get("retired") is not True:
                    raise RuntimeError("The private gateway did not confirm retirement")
                self.report["gateway_retired"] = True
            except Exception as error:
                # A broken witness must never skip teardown of this isolated fixture.
                self.report["gateway_retired"] = False
                self.report["violations"].append({"phase": "cleanup", "kind": "gateway retirement",
                                                  "reason": diagnostic_reason(error)})
                self.report.update(passed=False, standalone_passed=False)
        # The inherited cleanup revalidates each PID against the unique fixture
        # root before signalling it, including a gateway that refused retirement.
        super().close()
        survivors = [pid for pid, command in processes_mentioning(str(self.root)).items()
                     if "--runtime-interaction-gateway" in command]
        self.report["gateway_survivors"] = survivors
        if survivors:
            self.report.update(passed=False, standalone_passed=False)
            self.report["violations"].append({"phase": "cleanup", "kind": "gateway process survived cleanup"})

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
            # Validation.close stops only processes proved to belong to the
            # temporary root and records any survivors.
            runner.persist()
            if "leftover_processes" in runner.report and not runner.report.get("gateway_survivors") and not any(
                v["alive_after_cleanup"] for v in runner.report["leftover_processes"].values()
            ):
                shutil.rmtree(runner.root)
            else:
                runner.report.update(passed=False, standalone_passed=False)
                runner.report["violations"].append(
                    {"phase": "cleanup", "kind": "fixture process cleanup unverified"}
                )
                runner.persist()
    result = runner.report.get("standalone_passed", False) if runner.candidate_only else runner.report["passed"]
    return 0 if result else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("previous", "candidate", "snapshot", "report"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--saved-manifest-thread", help="Select a specific reported long conversation from the private snapshot")
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
        "--source-baseline-report", type=Path,
        help="Private signed direct workload for the exact previous payload when qualifying a staged gateway",
    )
    parser.add_argument(
        "--playwright-module", help="Installed Playwright module (default: demo/node_modules/playwright)"
    )
    parser.add_argument(
        "--candidate-only", action="store_true", help="Diagnostic only; never issues an upgrade pass"
    )
    parser.add_argument("--stable-origin", action="store_true",
                        help="Qualify the signed candidate through its permanent desktop origin")
    args = parser.parse_args()
    old, new = signed_resources(args.previous), signed_resources(args.candidate)
    if args.stable_origin and (args.candidate_only or not (new[0] / "backend/agent_coord/runtime_edge.py").is_file()):
        parser.error("Stable-origin qualification requires a capable upgrade candidate")
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
    return run_and_clean_up(runner)


if __name__ == "__main__":
    sys.exit(main())
