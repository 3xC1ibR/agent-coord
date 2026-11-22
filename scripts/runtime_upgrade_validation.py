#!/usr/bin/env python3
"""Adversarial validation of uninterrupted coordinator upgrades.

Separate driver over scripts/runtime_live_acceptance.py, which stays unchanged
and keeps its own gate. This driver subclasses it and adds the checks the gate
lacks: actual coordinator replacement (new pid, executed code identity, previous
process exit, listening socket owner), provider and tool liveness, approval
identity after every swap, exactly-once command receipts under traffic that
arrives during the swap, push-channel (SSE) cursor continuity, candidate
rollback and coordinator crash recovery.

Everything runs in a private root with an isolated HOME. Only processes whose
command line proves they belong to that root are ever signalled.

Scenarios:
  steady            live turns, N swaps with traffic during each activation
  idle-control      no live turns; proves the replacement checks are satisfiable
  rollback          a candidate that cannot serve must be rejected, then a good one activates
  crash-candidate   SIGKILL the candidate before activation; old keeps serving
  crash-coordinator SIGKILL the serving coordinator mid-turn; providers survive
  term-coordinator  unrequested SIGTERM of the serving coordinator; providers survive, no Stop proof
  stop-during-preparation
                    explicit Stop (the app's --runtime-command stop) while the trial
                    candidate prepares: no relaunch, stop proof written, every runtime
                    process exits, a later start replays nothing
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import http.client
import importlib.util
import json
import os
from pathlib import Path
import signal
import sqlite3
import socket
import subprocess
import sys
import threading
import time
import uuid
import traceback
import urllib.error
import urllib.request

# RUNTIME_VALIDATION_REPO selects the checkout whose backend, gate and build helper are
# exercised (default: this checkout). Baseline comparisons use a stable worktree so an
# implementer's half-written edit cannot contaminate a run.
REPO = Path(os.environ.get("RUNTIME_VALIDATION_REPO") or Path(__file__).resolve().parents[1]).resolve()
_spec = importlib.util.spec_from_file_location("runtime_live_acceptance", REPO / "scripts/runtime_live_acceptance.py")
live = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(live)
runtime = live.runtime
quantiles = live.quantiles

SCENARIOS = ("steady", "idle-control", "rollback", "crash-candidate", "crash-coordinator", "term-coordinator",
             "stop-during-preparation", "crash-transport-owner")
STOP_COMMAND_SECONDS = 35  # RuntimeHost.swift allows the stop command this long
ACTIVATION_SECONDS = 60
PREVIOUS_EXIT_SECONDS = 10
CREDENTIAL_MARKERS = ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
CREDENTIAL_PREFIXES = ("ANTHROPIC_", "OPENAI_", "CODEX_", "CLAUDE_", "AWS_", "GOOGLE_", "GH_", "GITHUB_", "KIMI_", "MUSE_")

IDENTITY_HOOK = """
# Executed-code identity for upgrade validation: written by the importing process itself.
import json as _identity_json, os as _identity_os, sys as _identity_sys
_identity_dir = _identity_os.path.join(_identity_os.environ["RIBBON_FAKE_ROOT"], "identity")
_identity_os.makedirs(_identity_dir, exist_ok=True)
with open(_identity_os.path.join(_identity_dir, str(_identity_os.getpid()) + ".json"), "w") as _identity_stream:
    _identity_stream.write(_identity_json.dumps({"generation": %d, "argv": _identity_sys.argv, "file": globals().get("__file__")}))
"""

FAULT_HOOK = """
# Rollback fault for upgrade validation: this payload bootstraps and passes the probe but never serves,
# so the failure surfaces in the trial after the previous coordinator has exited.
import sys as _fault_sys
if "--runtime-serve" in _fault_sys.argv:
    _fault_sys.exit(3)
"""


def process_command(pid):
    """Command line of a process, or None when it does not exist."""
    result = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, check=False)
    return result.stdout.strip() or None


def process_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def transport_journal_path(runtime_directory):
    """Read-only location of the transport journal.

    Owners that record a journal_generation keep their journal under
    provider-transport/generations/<generation>/transport.sqlite3; older owners
    use the flat provider-transport/transport.sqlite3. The record is read, never written.
    """
    directory = runtime_directory / "provider-transport"
    record_path = directory / "owner.json"
    try:
        generation = json.loads(record_path.read_text()).get("journal_generation")
    except (OSError, ValueError, AttributeError):
        generation = None
    if generation:
        try:  # the record stores uuid4().hex; the directory is the canonical hyphenated form
            canonical = str(uuid.UUID(str(generation)))
        except (ValueError, AttributeError, TypeError):
            canonical = None  # fail closed: an invalid generation never selects a directory
        if canonical:
            return directory / "generations" / canonical / "transport.sqlite3"
    flat = directory / "transport.sqlite3"
    if flat.exists():
        return flat
    for candidate in sorted(directory.glob("*.sqlite3")):
        return candidate
    return flat


def processes_mentioning(text):
    """Pids whose command line contains text; a listing only, nothing is signalled."""
    result = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True, check=False)
    found = {}
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and text in parts[1]:
            found[int(parts[0])] = parts[1]
    return found


def listener_pids(port):
    """Pids owning a listening TCP socket on the port, or None when lsof is unavailable."""
    try:
        result = subprocess.run(["lsof", "-nP", "-iTCP:" + str(port), "-sTCP:LISTEN", "-Fp"],
                                capture_output=True, text=True, check=False, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return {int(line[1:]) for line in result.stdout.splitlines() if line.startswith("p")}


def url_port(url):
    return int(url.rsplit(":", 1)[1].split("/")[0])


def tool_outcomes(records):
    """Evidence the live gate never reads: tool exit status and child exit records."""
    starts = [r for r in records if r["kind"] == "tool_start"]
    exits = [r for r in records if r["kind"] == "tool_exit"]
    completes = [r for r in records if r["kind"] == "tool_complete"]
    return {"tool_start": len(starts), "tool_exit": len(exits), "tool_complete": len(completes),
            "returncodes": [r["returncode"] for r in completes],
            "healthy": len(exits) == len(starts) == len(completes) and all(r["returncode"] == 0 for r in completes)}


def stop_path(proof, update):
    """Which Stop path the runtime took, from its durable records.

    cancelled-before-commit: the supervisor saw the cancellation when the trial candidate
    reported prepared, stopped the retained transport and wrote proof for the source instance.
    stopped-after-commit: the candidate had already committed; the stop command found and
    stopped the newly serving instance instead.
    """
    if not proof or not update:
        return "unknown"
    if not update.get("cancelled"):
        return "stopped-without-cancellation"
    if proof.get("instance") == update.get("source_instance"):
        return "cancelled-before-commit"
    return "stopped-after-commit"


def receipt_violations(outcomes, inputs):
    """Exactly-once receipts: accepted commands arrive once, rejected never, uncertain at most once.

    outcomes: list of {"token": str, "outcome": accepted|rejected|uncertain|not_sent}
    inputs: provider input records (fixture kind == "input")
    """
    serialized = [json.dumps(record.get("message")) for record in inputs]
    violations = []
    arrivals = {}
    for outcome in outcomes:
        count = sum(outcome["token"] in text for text in serialized)
        arrivals[outcome["token"]] = count
        limit = {"accepted": (1, 1), "rejected": (0, 0), "uncertain": (0, 1), "not_sent": (0, 0)}[outcome["outcome"]]
        if not limit[0] <= count <= limit[1]:
            violations.append({**outcome, "arrivals": count})
    return violations, arrivals


class EventCursor:
    """Tracks one SSE consumer's view: resets, ordering, and the stream thread's text."""

    def __init__(self, thread_id, turn_id=None):
        self.thread_id = thread_id
        self.turn_id = turn_id  # deltas of other turns on the same thread are accounted separately
        self.other_turns = Counter()
        self.completed_at = None  # when turn/completed for turn_id reached this consumer
        self.batches = 0
        self.resets_after_first = 0
        self.reset_at = []  # monotonic times of resets after the first batch
        self.resets = []  # evidence per reset: when, the last sequence number before it, the first after it
        self.order_violations = []
        self.last_seq = 0
        self.text = ""
        self.last_id = None
        self.reconnects = []
        self.last_delta_at = None
        self.delta_gaps = []
        self.sequence_gaps = []  # holes in the sequence numbers: events the consumer never received
        self.deltas = []  # (seq, length, at) for every stream delta, to localize a divergence

    def feed(self, batch, event_id=None, now=None):
        now = time.monotonic() if now is None else now
        if self.batches and batch.get("reset"):
            # A declared reset starts a new stream generation (a coordinator instance after explicit
            # Stop numbers its events from 1 again). Whether that reset was permitted is judged by
            # verify_events from its time; the ordering check below continues within the new
            # generation. Numbers that go backwards without a declared reset remain violations.
            self.resets_after_first += 1
            self.reset_at.append(round(now, 3))
            seqs = [e.get("seq") for e in batch.get("events", []) if isinstance(e.get("seq"), int)]
            self.resets.append({"at": round(now, 3), "previous_last_seq": self.last_seq,
                                "first_seq": seqs[0] if seqs else None})
            self.last_seq = 0
        self.batches += 1
        for event in batch.get("events", []):
            seq = event.get("seq")
            if isinstance(seq, int):
                if seq <= self.last_seq:
                    self.order_violations.append({"seq": seq, "previous": self.last_seq})
                elif self.last_seq and seq > self.last_seq + 1:
                    self.sequence_gaps.append({"after": self.last_seq, "seq": seq, "at": round(now, 3)})
                self.last_seq = max(self.last_seq, seq)
            params = event.get("params") or {}
            if params.get("threadId") != self.thread_id:
                continue
            method = event.get("method")
            if method == "turn/completed" and self.turn_id is not None and params.get("turnId") == self.turn_id:
                self.completed_at = now
            if method == "item/agentMessage/delta":
                if self.turn_id is not None and params.get("turnId") != self.turn_id:
                    self.other_turns[str(params.get("turnId"))] += len(params.get("delta", ""))
                    continue
                if self.last_delta_at is not None:
                    self.delta_gaps.append(now - self.last_delta_at)
                self.last_delta_at = now
                self.text += params.get("delta", "")
                self.deltas.append((seq, len(params.get("delta", "")), now))
        if event_id:
            self.last_id = event_id

    def summary(self):
        return {"batches": self.batches, "resets_after_first": self.resets_after_first,
                "sequence_gaps": self.sequence_gaps[:20], "delta_count": len(self.deltas),
                "order_violations": self.order_violations[:10], "last_seq": self.last_seq, "reset_at": self.reset_at[:10],
                "resets": self.resets[:10],
                "other_turn_delta_chars": dict(self.other_turns), "completed_observed": self.completed_at is not None,
                "reconnects": self.reconnects, "text_length": len(self.text),
                "delta_gap_seconds": quantiles(self.delta_gaps)}


class Validation(live.Acceptance):
    def __init__(self, output, swaps, scenario):
        super().__init__(output, swaps)
        self.scenario = scenario
        self.report.update(scenario=scenario, traffic=[], isolation={}, sse=None, faults=[])
        self.report["gate_hashes"]["scripts/runtime_upgrade_validation.py"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        self.report["source_checkout"] = str(REPO)
        self.isolate_environment()
        self.sse_cursor = None
        self.sse_thread = None
        self.traffic_thread = None
        self.traffic_active = threading.Event()
        self.traffic_outcomes = []
        self.identity_dir = self.root / "identity"
        self.tool_child = None
        self.transport_pid = None
        self.supervisor_pid = None

    # -- isolation -----------------------------------------------------------------

    def isolate_environment(self):
        """Isolation corrections beyond the live gate: no inherited HOME, config or credentials."""
        home = self.root / "home"
        for sub in (".codex", ".claude", ".config", ".local/state", ".local/share", ".cache"):
            (home / sub).mkdir(parents=True, exist_ok=True)
        (self.root / "tmp").mkdir(exist_ok=True)
        removed = sorted(name for name in self.env if name.startswith(CREDENTIAL_PREFIXES)
                         or any(marker in name for marker in CREDENTIAL_MARKERS))
        for name in removed:
            self.env.pop(name)
        self.env.update(HOME=str(home), CODEX_HOME=str(home / ".codex"), CLAUDE_CONFIG_DIR=str(home / ".claude"),
                        XDG_CONFIG_HOME=str(home / ".config"), XDG_STATE_HOME=str(home / ".local/state"),
                        XDG_DATA_HOME=str(home / ".local/share"), XDG_CACHE_HOME=str(home / ".cache"),
                        TMPDIR=str(self.root / "tmp"))
        self.report["isolation"] = {
            "home": str(home), "removed_variables": removed,
            "notes": ["runtime cwd is Path.home(), so HOME points at a private directory under the root",
                      "CODEX_HOME and CLAUDE_CONFIG_DIR point at empty private directories; attention.py and "
                      "timeline_clock.py read transcripts from them",
                      "the live gate inherits HOME and credential variables; this driver removes them"]}

    # -- payloads --------------------------------------------------------------------

    def resources(self, generation, *, faulty=False):
        target = super().resources(generation)
        backend = target / "backend/agent_coord/desktop_runtime.py"
        with backend.open("a") as stream:
            stream.write(IDENTITY_HOOK % generation)
            if faulty:
                stream.write(FAULT_HOOK)
        return target

    def executed_generation(self, pid):
        path = self.identity_dir / f"{pid}.json"
        return json.loads(path.read_text())["generation"] if path.exists() else None

    # -- ownership proofs ------------------------------------------------------------

    def owned(self, pid):
        command = process_command(pid)
        return bool(command) and str(self.root) in command

    def kill_owned(self, pid, reason, number=signal.SIGKILL):
        command = process_command(pid)
        if not command or str(self.root) not in command:
            raise AssertionError(f"Refusing to signal pid {pid}: not proved to belong to {self.root}: {command!r}")
        os.kill(pid, number)
        self.report["faults"].append({"pid": pid, "signal": signal.Signals(number).name, "reason": reason,
                                      "command": command, "at": time.monotonic()})

    def candidate_processes(self, digest, exclude):
        found = processes_mentioning(f"/payloads/{digest}/")
        return {pid: command for pid, command in found.items()
                if pid not in exclude and "--runtime-bootstrap" not in command and str(self.root) in command}

    # -- API helpers -----------------------------------------------------------------

    def endpoint(self):
        status = self.status()
        with urllib.request.urlopen(status["url"] + "api/browser/config", timeout=3) as response:
            token = json.load(response)["token"]
        return status["url"], token

    def post_classified(self, url, token, path, body, token_text, timeout=10):
        request = urllib.request.Request(url + path, data=json.dumps(body).encode(), headers={
            "X-Agent-Coord-Token": token, "Content-Type": "application/json"})
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                response.read()
            outcome = "accepted"
        except urllib.error.HTTPError as error:
            outcome = "rejected"
            with error:
                body = error.read()[:300].decode(errors="replace")
        except (ConnectionRefusedError, urllib.error.URLError) as error:
            reason = getattr(error, "reason", error)
            outcome = "not_sent" if isinstance(reason, ConnectionRefusedError) else "uncertain"
            body = str(error)
        except (socket.timeout, TimeoutError, http.client.RemoteDisconnected, ConnectionResetError) as error:
            outcome = "uncertain"
            body = str(error)
        else:
            body = None
        return {"token": token_text, "outcome": outcome, "seconds": time.monotonic() - started, "at": started,
                "detail": body}

    # -- SSE consumer ----------------------------------------------------------------

    def consume_events(self, thread_id):
        cursor = self.sse_cursor = EventCursor(thread_id, (getattr(self, "turns", None) or {}).get("stream"))
        while not self.stopped.is_set():
            try:
                url, token = self.endpoint()
                headers = {"X-Agent-Coord-Token": token, "Accept": "text/event-stream"}
                if cursor.last_id:
                    headers["Last-Event-ID"] = cursor.last_id
                request = urllib.request.Request(url + "api/browser/events", headers=headers)
                with urllib.request.urlopen(request, timeout=5) as response:
                    event_id = None
                    while not self.stopped.is_set():
                        line = response.readline()
                        if not line:
                            raise ConnectionError("event stream ended")
                        line = line.decode().rstrip("\n")
                        if line.startswith("id: "):
                            event_id = line[4:]
                        elif line.startswith("data: "):
                            cursor.feed(json.loads(line[6:]), event_id)
            except Exception as error:  # reconnect with the saved cursor, as EventSource does
                cursor.reconnects.append({"at": time.monotonic(), "error": str(error)[:200]})
                self.stopped.wait(.2)

    # -- traffic during activation ---------------------------------------------------

    def traffic(self, thread_id, turn_id):
        """Steer the live Codex stream turn continuously while a swap is in progress."""
        counter = 0
        url = token = None
        while not self.stopped.is_set():
            if not self.traffic_active.wait(.1):
                continue
            counter += 1
            text = f"steer-{counter:04d}-{self.scenario}"
            try:
                if url is None:
                    url, token = self.endpoint()
                outcome = self.post_classified(url, token, f"api/browser/sessions/{thread_id}/messages",
                                               {"message": text, "expectedTurnId": turn_id}, text)
            except Exception as error:
                outcome = {"token": text, "outcome": "not_sent", "detail": str(error)[:200]}
            if outcome["outcome"] != "accepted":
                url = token = None
            outcome["at"] = time.monotonic()
            self.traffic_outcomes.append(outcome)
            self.stopped.wait(.1)

    # -- shared phases ---------------------------------------------------------------

    def start_turns(self, *, release_first=False):
        first = self.launch(self.resources(0))
        self.report["initial"] = first
        self.report["initial_pid"] = self.status()["pid"]
        threads = {}
        for role, provider in (("stream", "codex"), ("tool", "codex"), ("approval", "claude")):
            value = self.api("api/browser/sessions", {"cwd": str(self.root), "client": provider,
                                                     "name": "Validation " + role})
            identity = threads[role] = value["session"]["thread_id"]
            self.api(f"api/browser/sessions/{identity}/messages", {"message": role})
        self.wait(lambda: any(x["kind"] == "approval" for x in self.records()))
        self.wait(lambda: any(x["kind"] == "tool_pid" for x in self.records()))
        # The child's own start record follows tool_pid; wait for it before freezing the pid set.
        self.tool_child = next(r["child"] for r in self.records() if r["kind"] == "tool_pid")
        self.wait(lambda: any(r["kind"] == "start" and r["pid"] == self.tool_child for r in self.records()))
        details = {role: self.api(f"api/browser/sessions/{identity}") for role, identity in threads.items()}
        self.check(all(d["running"] for d in details.values()), "three simultaneous running turns")
        self.approval_key = details["approval"]["requests"][0]["key"]
        self.turns = {role: d["activeTurn"] for role, d in details.items()}
        self.threads = threads
        self.report["threads"] = threads
        self.report["turns"] = self.turns
        self.provider_ids = {r["pid"] for r in self.records() if r["kind"] == "start"}
        self.check(all(process_alive(pid) for pid in self.provider_ids), "providers and tool child alive at start")
        self.api(f"api/browser/sessions/{threads['stream']}/queue", {"message": "queued-after-stream"})
        self.sse_thread = threading.Thread(target=self.consume_events, args=(threads["stream"],), daemon=True)
        self.sse_thread.start()
        self.wait(lambda: self.sse_cursor is not None and self.sse_cursor.batches > 0)
        self.observer = threading.Thread(target=self.observe, args=(threads["stream"],))
        self.observer.start()
        if release_first:
            self.api(f"api/browser/sessions/{threads['approval']}/requests/{self.approval_key}", {"decision": "accept"})
            (self.root / "release").touch()
            self.wait(lambda: all(not self.api(f"api/browser/sessions/{identity}")["running"]
                                  for identity in threads.values()), timeout=20)
            self.wait(lambda: any(r["kind"] == "turn" and r["role"] == "queued-after-stream" for r in self.records()))
            self.wait(lambda: any(r["kind"] == "complete" and r["thread"] == threads["stream"] and
                                  r["turn"] != self.turns["stream"] for r in self.records()))
        else:
            self.traffic_thread = threading.Thread(target=self.traffic, args=(threads["stream"], self.turns["stream"]),
                                                   daemon=True)
            self.traffic_thread.start()

    def swap(self, generation, *, faulty=False, expect_activation=True, during=None, traffic=True, until=None):
        """Bootstrap one generation and verify replacement; returns the swap record.

        Steering traffic runs throughout the activation window whenever live turns exist.
        """
        resources = self.resources(generation, faulty=faulty)
        expected = runtime.payload_digest(resources)
        before = self.status()
        started = time.monotonic()
        entry = {"generation": generation, "faulty": faulty, "expected_digest": expected, "before": before,
                 "started_at": started}
        self.report["swaps"].append(entry)
        if traffic and self.traffic_thread is not None:
            self.traffic_active.set()
        try:
            entry["bootstrap"] = self.launch(resources)
        except AssertionError as error:
            entry["bootstrap_error"] = str(error)[-2000:]
        current = None
        activated = False
        timeline, signature = [], None
        while time.monotonic() - started < ACTIVATION_SECONDS:
            if during is not None:
                during(expected, before, time.monotonic() - started)
            observed = {}
            try:
                current = self.status()
            except (OSError, RuntimeError, ValueError) as error:
                current = None
                observed = {"unavailable": type(error).__name__}
            if current is None:
                observed = observed if observed.get("unavailable") else {"unavailable": "no discovery record"}
            else:
                update = current.get("update") if isinstance(current.get("update"), dict) else None
                observed = {"pid": current.get("pid"), "payload": Path(current["payload"]).name[:12],
                            "status": current.get("status"), "pending": bool(current.get("pending")),
                            "paused": current.get("paused"), "busy": list(current.get("busy") or []),
                            "upgrade_error": (current.get("upgrade_error") or None),
                            "update_phase": update.get("phase") if update else None}
            if observed != signature:
                # Status transitions explain slow activations without a product-side log.
                timeline.append({"at": round(time.monotonic() - started, 3), **observed})
                signature = observed
            if current and Path(current["payload"]).name == expected and current.get("status") == "serving":
                activated = True
                break
            if not expect_activation and during is None and self.rejection_reported(entry, current):
                entry["rejected_seconds"] = time.monotonic() - started
                break
            if until is not None and until():
                entry["ended_early_seconds"] = time.monotonic() - started
                break
            time.sleep(.05)
        self.traffic_active.clear()
        entry.update(seconds=time.monotonic() - started, after=current, activated=activated, timeline=timeline[:200])
        if expect_activation:
            self.check(activated, f"new backend active within {ACTIVATION_SECONDS} seconds (swap {generation})", entry)
            self.verify_replacement(before, current, expected, generation)
        return entry

    def verify_replacement(self, before, after, expected, generation):
        label = f" (swap {generation})"
        self.check(after["pid"] != before["pid"], "coordinator pid changed" + label,
                   {"before": before["pid"], "after": after["pid"]})
        command = process_command(after["pid"])
        self.check(command and f"/payloads/{expected}/" in command and str(self.root) in command,
                   "serving pid executes the staged payload" + label, command)
        self.check(self.executed_generation(after["pid"]) == generation,
                   "serving pid imported generation code (identity written by the process itself)" + label,
                   {"pid": after["pid"], "generation": self.executed_generation(after["pid"])})
        exited = self.wait_quiet(lambda: not process_alive(before["pid"]), PREVIOUS_EXIT_SECONDS)
        self.check(exited, "previous coordinator exited" + label,
                   {"pid": before["pid"], "command": process_command(before["pid"])})
        owners = {}
        for name, url in (("ui", after["url"]), ("control", after.get("control_url"))):
            if url:
                owners[name] = listener_pids(url_port(url))
        entry = self.report["swaps"][-1]
        entry["listeners"] = {name: sorted(pids) if pids is not None else None for name, pids in owners.items()}
        entry["transport"] = after.get("transport")
        # Agreed contract: the coordinator owns the UI and control listeners itself;
        # a stable transport may route provider bytes but never fronts HTTP.
        self.check(all(pids is None or pids == {after["pid"]} for pids in owners.values()),
                   "listening sockets owned by the serving coordinator" + label,
                   {"listeners": entry["listeners"], "pid": after["pid"]})
        transport = after.get("transport")
        if isinstance(transport, dict):
            if self.transport_pid is None:
                self.transport_pid = transport.get("pid")
            self.check(transport.get("pid") == self.transport_pid and process_alive(self.transport_pid),
                       "declared transport owner stable and alive" + label, transport)
            coordinator_group = os.getpgid(after["pid"])
            groups = set()
            for channel in transport.get("providers", []):
                if isinstance(channel, dict) and process_alive(channel.get("pid") or 0):
                    groups.add(os.getpgid(channel["pid"]))  # measured, not the reported value
            self.check(coordinator_group not in groups, "provider process groups separate from coordinator" + label,
                       {"coordinator_pgid": coordinator_group, "provider_pgids": sorted(g for g in groups if g)})
        supervisor = self.supervisor_record()
        entry["supervisor"] = supervisor
        if supervisor:
            if self.supervisor_pid is None:
                self.supervisor_pid = supervisor.get("pid")
            self.check(supervisor.get("pid") == self.supervisor_pid and process_alive(self.supervisor_pid)
                       and supervisor.get("coordinator_pid") == after["pid"],
                       "supervisor stable and records the serving coordinator" + label,
                       {"supervisor": supervisor, "expected_pid": self.supervisor_pid, "serving": after["pid"]})

    def supervisor_record(self):
        path = runtime.runtime_directory(self.database) / "supervisor.json"
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return None

    def serves_payload(self, status, digest):
        return bool(status) and status.get("status") == "serving" and Path(status["payload"]).name == digest

    def wait_restored(self, digest, exclude_pid, timeout):
        """Wait for a serving coordinator of the given payload other than exclude_pid."""
        started = time.monotonic()
        while time.monotonic() - started < timeout:
            try:
                status = self.status()
                if self.serves_payload(status, digest) and status["pid"] != exclude_pid:
                    return status, time.monotonic() - started
            except (OSError, RuntimeError, ValueError):
                pass
            time.sleep(.05)
        return None, time.monotonic() - started

    def wait_quiet(self, predicate, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(.05)
        return predicate()

    def verify_live_turns(self, label):
        for role, identity in self.threads.items():
            detail = self.api(f"api/browser/sessions/{identity}")
            self.check(detail["running"] and detail["activeTurn"] == self.turns[role],
                       f"same {role} turn {label}", detail["activeTurn"])
            if role == "approval":
                keys = [r["key"] for r in detail.get("requests", [])]
                self.check(keys == [self.approval_key], f"approval request identity preserved {label}", keys)
        records = self.records()
        self.check(not any(r["kind"] in {"signal", "stdin_eof"} for r in records), f"no provider signal or transport EOF {label}")
        self.check({r["pid"] for r in records if r["kind"] == "start"} == self.provider_ids, f"same provider and tool processes {label}")
        dead = [pid for pid in self.provider_ids if not process_alive(pid)]
        self.check(not dead, f"providers and tool child alive {label}", dead)

    def finish_live(self):
        """Release the work and apply the gate's final checks plus the additive ones."""
        self.api(f"api/browser/sessions/{self.threads['approval']}/requests/{self.approval_key}", {"decision": "accept"})
        (self.root / "release").touch()
        self.wait(lambda: all(not self.api(f"api/browser/sessions/{identity}")["running"]
                              for identity in self.threads.values()), timeout=20)
        self.wait(lambda: any(r["kind"] == "turn" and r["role"] == "queued-after-stream" for r in self.records()))
        self.wait(lambda: any(r["kind"] == "complete" and r["thread"] == self.threads["stream"] and
                              r["turn"] != self.turns["stream"] for r in self.records()), timeout=20)
        # The push channel is allowed a bounded time to deliver the original turn's completion before
        # the consumer stops; an undrained tail after that is a delivery failure, not consumer timing.
        observed = self.wait_quiet(lambda: self.sse_cursor is not None and self.sse_cursor.completed_at is not None,
                                   timeout=10)
        self.stopped.set()
        self.observer.join(timeout=5)
        self.check(observed, "push channel delivered the original turn's completion within ten seconds",
                   self.sse_cursor.summary() if self.sse_cursor else None)
        records = self.records()
        answers = [r for r in records if r["kind"] == "approval_answer"]
        # The fixture names the request itself; a re-sent or replayed turn would add a second request record.
        requests = [r for r in records if r["kind"] == "approval" and r["thread"] == self.threads["approval"]]
        self.check(len(requests) == 1 and len(answers) == 1 and answers[0]["request"] == requests[0]["request"],
                   "original approval answered exactly once", {"requests": requests, "answers": answers})
        counts = Counter(r["role"] for r in records if r["kind"] == "turn")
        self.check(counts == Counter({"stream": 1, "tool": 1, "approval": 1, "queued-after-stream": 1}),
                   "turn invocations exactly once", dict(counts))
        outcomes = tool_outcomes(records)
        self.check(outcomes["healthy"], "long tool ran once, exited 0, child recorded its exit", outcomes)
        expected = "".join(r["delta"] for r in records if r["kind"] == "output" and
                           r["thread"] == self.threads["stream"] and r["turn"] == self.turns["stream"])
        detail = self.api(f"api/browser/sessions/{self.threads['stream']}")
        turn = next(t for t in detail["thread"]["turns"] if t["id"] == self.turns["stream"])
        actual = "".join(i.get("text", "") for i in turn["items"] if i.get("type") == "agentMessage")
        self.check(actual == expected, "output has no gaps or duplicates", {"expected": len(expected), "actual": len(actual)})
        failures = []
        for verifier, arguments in ((self.verify_observations, ()), (self.verify_events, (expected,)),
                                    (self.verify_traffic, (records,))):
            try:
                verifier(*arguments)
            except AssertionError as error:
                failures.append(error)  # every group records its evidence before the run fails
        if failures:
            raise failures[0]

    def verify_observations(self):
        previous = ""
        for observation in self.observations:
            self.check(observation["text"].startswith(previous), "visible output never retreats or duplicates")
            previous = observation["text"]
        progress = [self.observations[0]["at"]] if self.observations else []
        for a, b in zip(self.observations, self.observations[1:]):
            if a["text"] != b["text"]:
                progress.append(b["at"])
        stalls = [b - a for a, b in zip(progress, progress[1:])]
        self.report["visible_progress_interval_seconds"] = quantiles(stalls)
        self.report["api_duration_seconds"] = quantiles([o["duration"] for o in self.observations])
        self.report["activation_seconds"] = quantiles([s["seconds"] for s in self.report["swaps"] if s.get("activated")])
        # Same bound as the gate: bounded visible progress on the REST thread detail is part of the user
        # contract and stays asserted. An earlier probe (build/live-runtime-gate-review/
        # probe_rest_stream_visibility.py) saw no REST text change while streaming on the previous
        # boundary; the push channel is checked separately in verify_events, not instead of this.
        self.check(bool(stalls) and max(stalls) < 5, "visible stream stalls below five seconds", quantiles(stalls))
        # Interface reconnects are reported explicitly and bounded; they are not
        # equated with stopped provider work, which the output checks cover.
        outages = self.outage_windows(self.observer_errors)
        self.report["interface_outages"] = {"count": len(outages), "errors": len(self.observer_errors),
                                            "longest_seconds": max(outages) if outages else 0,
                                            "samples": self.observer_errors[:5]}
        self.check(not outages or max(outages) < 5, "REST interface outage windows below five seconds",
                   self.report["interface_outages"])

    @staticmethod
    def outage_windows(errors, gap=0.5):
        """Durations of consecutive-error windows (errors closer than gap seconds)."""
        windows = []
        for error in errors:
            if windows and error["at"] - windows[-1][1] <= gap:
                windows[-1][1] = error["at"]
            else:
                windows.append([error["at"], error["at"]])
        return [end - start for start, end in windows]

    def verify_events(self, expected_text=None, reset_allowed_after=None):
        """reset_allowed_after: monotonic time of an explicit Stop; one reset after it is the fresh runtime."""
        cursor = self.sse_cursor
        self.report["sse"] = cursor.summary() if cursor else None
        self.check(cursor is not None and cursor.batches > 0, "event stream delivered batches")
        if reset_allowed_after is None:
            self.check(cursor.resets_after_first == 0, "event cursor never reset across swaps", cursor.summary())
        else:
            before = [at for at in cursor.reset_at if at < reset_allowed_after]
            self.check(not before, "event cursor never reset before the explicit Stop", cursor.summary())
            self.check(len(cursor.reset_at) <= 1, "event cursor reset at most once for the fresh runtime after Stop",
                       cursor.summary())
        self.check(not cursor.order_violations, "event sequence monotonic across swaps", cursor.order_violations[:5])
        if expected_text is not None:
            self.check(cursor.text == expected_text, "push channel delivered every stream delta exactly once",
                       self.divergence(cursor, expected_text))
            gaps = cursor.delta_gaps
            self.check(bool(gaps) and max(gaps) < 5, "push channel delta gaps below five seconds", quantiles(gaps))

    def divergence(self, cursor, expected):
        """Where the pushed text first differs from the provider's output, and what was happening then."""
        pushed = cursor.text
        offset = next((i for i, (a, b) in enumerate(zip(pushed, expected)) if a != b), min(len(pushed), len(expected)))
        position, around = 0, []
        for seq, length, at in cursor.deltas:
            if position + length >= offset - 1 and len(around) < 6:
                around.append({"seq": seq, "length": length, "at": round(at, 3), "offset": position})
            position += length
        swaps = [{"generation": s["generation"], "started_at": round(s["started_at"], 3),
                  "ended_at": round(s["started_at"] + s["seconds"], 3)} for s in self.report["swaps"]]
        return {"expected": len(expected), "pushed": len(pushed), "divergence_offset": offset,
                "expected_context": expected[max(0, offset - 20):offset + 40],
                "pushed_context": pushed[max(0, offset - 20):offset + 40],
                "deltas_at_divergence": around, "sequence_gaps": cursor.sequence_gaps[:20],
                "reconnects": [round(r["at"], 3) for r in cursor.reconnects][:30], "swaps": swaps}

    def verify_traffic(self, records):
        inputs = [r for r in records if r["kind"] == "input"]
        violations, arrivals = receipt_violations(self.traffic_outcomes, inputs)
        counts = Counter(o["outcome"] for o in self.traffic_outcomes)
        self.report["traffic"] = {"outcomes": dict(counts), "violations": violations[:10],
                                  "latency_seconds": quantiles([o.get("seconds", 0) for o in self.traffic_outcomes]),
                                  "uncertain": [o for o in self.traffic_outcomes if o["outcome"] == "uncertain"][:10]}
        if self.traffic_thread is not None:
            self.check(counts["accepted"] > 0, "steering traffic was accepted during swaps", dict(counts))
            self.check(not violations, "accepted commands reached the provider exactly once, rejected never", violations[:5])
            # A request in flight when a process is deliberately killed is uncertain to any client;
            # outside an injected fault window the coordinator must answer definitely.
            windows = [(fault["at"] - 1, fault["at"] + 5) for fault in self.report["faults"]]
            if isinstance(self.report.get("stop"), dict) and self.report["stop"].get("at"):
                windows.append((self.report["stop"]["at"] - 1, self.report["stop"]["at"] + STOP_COMMAND_SECONDS))
            stray = [o for o in self.traffic_outcomes if o["outcome"] == "uncertain"
                     and not any(start <= o.get("at", 0) <= end for start, end in windows)]
            self.check(not stray, "no command outcome left uncertain outside injected fault windows",
                       {"uncertain": counts["uncertain"], "outside_fault_windows": len(stray), "samples": stray[:3]})

    # -- scenarios -------------------------------------------------------------------

    def run(self):
        getattr(self, "scenario_" + self.scenario.replace("-", "_"))()
        self.report["passed"] = True

    def scenario_steady(self):
        self.start_turns()
        for generation in range(1, self.swaps + 1):
            self.swap(generation)
            self.verify_live_turns(f"after swap {generation}")
        self.finish_live()

    def scenario_idle_control(self):
        self.start_turns(release_first=True)
        for generation in range(1, self.swaps + 1):
            self.swap(generation)
            records = self.records()
            self.check({r["pid"] for r in records if r["kind"] == "start"} == self.provider_ids,
                       f"no provider restarted (swap {generation})")
        self.stopped.set()
        self.observer.join(timeout=5)
        self.verify_events()

    def scenario_rollback(self):
        self.start_turns()
        entry = self.swap(1, faulty=True, expect_activation=False)
        self.check(not entry["activated"], "unservable candidate never activated", entry)
        previous = Path(entry["before"]["payload"]).name
        status, _ = self.wait_restored(previous, exclude_pid=None, timeout=30)
        self.check(self.serves_payload(status, previous), "previous payload serves after rejected candidate", status)
        entry["previous_pid_retained"] = bool(status) and status["pid"] == entry["before"]["pid"]
        self.check(self.rejection_reported(entry, status), "rejected candidate reported",
                   {"bootstrap_error": entry.get("bootstrap_error"),
                    **{k: (status or {}).get(k) for k in ("upgrade_error", "update", "pending")}})
        self.verify_live_turns("after rejected candidate")
        self.swap(2)
        self.verify_live_turns("after swap 2")
        self.finish_live()

    @staticmethod
    def rejection_reported(entry, status):
        if entry.get("bootstrap_error"):
            return True  # The bootstrap caller (the app) received the rejection.
        if not status:
            return False
        update = status.get("update")
        return bool(status.get("upgrade_error")) or (isinstance(update, dict) and (
            bool(update.get("error")) or update.get("phase") in {"failed", "rolled-back", "rejected"}))

    def scenario_crash_candidate(self):
        self.start_turns()
        killed = {}

        def during(expected, before, elapsed):
            if killed:
                return
            for pid, command in self.candidate_processes(expected, exclude={before["pid"]}).items():
                if "--commit-fd" in command:  # the trial candidate, after the previous coordinator exited
                    self.kill_owned(pid, "trial candidate before commit")
                    killed[pid] = command
                    return

        entry = self.swap(1, expect_activation=False, during=during)
        self.check(killed, "a candidate process was observed and killed before activation", entry)
        status = self.status()
        if entry["activated"] and status["pid"] not in killed:
            # The runtime retried with a fresh candidate: acceptable if everything else held.
            self.verify_replacement(entry["before"], status, entry["expected_digest"], 1)
        else:
            previous = Path(entry["before"]["payload"]).name
            status, _ = self.wait_restored(previous, exclude_pid=None, timeout=30)
            self.check(self.serves_payload(status, previous), "previous payload serves after candidate crash", status)
            entry["previous_pid_retained"] = bool(status) and status["pid"] == entry["before"]["pid"]
        self.verify_live_turns("after candidate crash")
        self.swap(2)
        self.verify_live_turns("after swap 2")
        self.finish_live()

    def scenario_term_coordinator(self):
        """Unrequested SIGTERM of the serving coordinator: not an explicit Stop, so work must continue."""
        self.scenario_crash_coordinator(number=signal.SIGTERM)

    def scenario_crash_coordinator(self, number=signal.SIGKILL):
        self.start_turns()
        before = self.status()
        if self.traffic_thread is not None:
            # Steering continues across the crash so receipts during the outage are classified too.
            self.traffic_active.set()
            time.sleep(.5)
        self.kill_owned(before["pid"], "serving coordinator mid-turn", number)
        killed_at = self.report["faults"][-1]["at"]
        self.wait(lambda: not process_alive(before["pid"]), timeout=10)
        proof = runtime.runtime_directory(self.database) / "stop-proof.json"
        self.check(not proof.exists(), "an unrequested signal is not recorded as an explicit Stop",
                   {"signal": signal.Signals(number).name, "exit_wait_seconds": round(time.monotonic() - killed_at, 3)})
        time.sleep(.5)
        dead = sorted(pid for pid in self.provider_ids if not process_alive(pid))
        self.check(not dead, "providers survive coordinator crash", {"dead_provider_pids": dead, "coordinator": before["pid"]})
        digest = Path(before["payload"]).name
        restored, _ = self.wait_restored(digest, exclude_pid=before["pid"], timeout=30)
        seconds = time.monotonic() - killed_at  # measured from the kill, not from the start of the wait
        self.traffic_active.clear()
        self.report["recovery"] = {"automatic": restored is not None, "seconds": seconds,
                                   "status": restored, "supervisor": self.supervisor_record()}
        self.check(restored is not None, "supervisor restarted the same payload without a bootstrap", self.report["recovery"])
        if restored is not None:
            self.check(self.executed_generation(restored["pid"]) == 0,
                       "restarted coordinator executes the original generation", restored["pid"])
            self.verify_live_turns("after coordinator restart")
        entry = self.swap(1)
        self.verify_live_turns("after coordinator crash recovery")
        self.swap(2)
        self.verify_live_turns("after swap 2")
        self.finish_live()

    def scenario_crash_transport_owner(self):
        """SIGKILL the provider transport owner while providers are open, in the same boot.

        Maintenance contract under test: an explicit Stop must refuse with the precise
        retained-evidence error, keep the owner record and its generation journal untouched,
        signal no retained pid, and neither Stop nor a relaunch may start a replacement owner or
        replay a command while the record is retained. Prior-boot recovery needs a reboot and is
        covered only by the product's unit tests with simulated boot identities.
        """
        self.start_turns()
        record_path = self.runtime_directory / "provider-transport/owner.json"
        record = json.loads(record_path.read_text())
        owner_pid = record["pid"]
        owner_command = process_command(owner_pid) or ""
        self.check("provider_transport" in owner_command and str(self.root) in owner_command,
                   "the recorded transport owner is this root's transport process",
                   {"pid": owner_pid, "command": owner_command[-160:]})
        turns_before = sum(1 for r in self.records() if r["kind"] == "turn")
        self.kill_owned(owner_pid, "provider transport owner mid-turn", signal.SIGKILL)
        self.wait(lambda: not process_alive(owner_pid), timeout=10)
        time.sleep(2)
        owner_listing = "provider_transport --directory " + str(self.runtime_directory)
        crash = self.report["owner_crash"] = {
            "record": {k: record.get(k) for k in ("pid", "instance", "boot_id", "journal_generation")},
            "providers_alive_after_crash": {str(pid): process_alive(pid) for pid in self.provider_ids},
            "owner_processes_after_crash": sorted(processes_mentioning(owner_listing))}
        try:
            crash["status_after_crash"] = self.status()
        except Exception as error:
            crash["status_after_crash"] = f"{type(error).__name__}: {error}"[:300]
        stop = {}
        resources = self.root / "app-0/Contents/Resources"
        self.request_stop(resources, stop)
        crash["stop"] = stop
        result = stop.get("result") or {}
        record_after = json.loads(record_path.read_text()) if record_path.exists() else None
        crash["record_after_stop"] = record_after
        # The two facts a caller must receive, in the product's own words (retire_dead_owner's refusal
        # or the retained shutdown failure): providers cannot be verified, evidence was retained.
        facts = ("surviving providers cannot be verified", "Retained evidence was preserved")
        output = (result.get("stderr") or "") + (result.get("stdout") or "")
        self.check(result.get("returncode") not in (0, None) and all(fact in output for fact in facts),
                   "Stop refuses the ambiguous same-boot owner death and reports the retained-evidence reason to its caller",
                   {"seconds": stop.get("seconds"), "result": result})
        self.check((stop.get("seconds") or 999) < 20, "the refusal reaches the caller before the generic 30 s exit poll would expire",
                   {"seconds": stop.get("seconds")})
        self.check(record_after == record, "the dead owner record is retained unchanged as evidence",
                   {"before": crash["record"], "after": record_after})
        journal = transport_journal_path(self.runtime_directory)
        crash["journal"] = {"path": str(journal), "exists": journal.exists(), "directory": journal.parent.name}
        self.check(journal.exists(), "the retained generation journal still exists after the refused Stop", crash["journal"])
        time.sleep(2)
        crash["owner_processes_after_stop"] = sorted(processes_mentioning(owner_listing))
        # A repeated Stop, once no coordinator record remains, runs the explicit maintenance path
        # and must state the same facts together with the recovery condition.
        second = {}
        self.request_stop(resources, second)
        crash["second_stop"] = second
        second_result = second.get("result") or {}
        second_output = (second_result.get("stderr") or "") + (second_result.get("stdout") or "")
        self.check(second_result.get("returncode") not in (0, None) and all(fact in second_output for fact in facts),
                   "a repeated Stop still refuses with the retained-evidence reason",
                   {"seconds": second.get("seconds"), "result": second_result})
        crash["second_stop_states_recovery_condition"] = "Recovery requires a verified new boot" in second_output
        self.check(json.loads(record_path.read_text()) == record if record_path.exists() else False,
                   "the dead owner record is still unchanged after the repeated Stop", crash["second_stop"])
        retired = self.runtime_directory / "provider-transport/retired"
        crash["retired_records"] = sorted(path.name for path in retired.iterdir()) if retired.exists() else []
        # A relaunch attempt while the record is retained: it may find a coordinator still
        # serving or refuse; either way no replacement owner may appear and nothing is replayed.
        launch = subprocess.run([sys.executable, "-B", str(resources / "backend.py"), "--runtime-bootstrap",
                                 "--resources", str(resources), "--db", str(self.database)],
                                env=dict(self.env, PYTHONPATH=str(resources / "backend")), text=True,
                                capture_output=True, timeout=90)
        time.sleep(2)
        record_after_launch = json.loads(record_path.read_text()) if record_path.exists() else None
        owners_after_launch = sorted(processes_mentioning(owner_listing))
        log_tail = self.file_tail(self.runtime_directory / "runtime.log", 80)
        crash["relaunch"] = {"returncode": launch.returncode, "stderr": launch.stderr[-600:],
                             "record": record_after_launch, "owner_processes": owners_after_launch,
                             "runtime_log_tail": log_tail[-25:]}
        self.check(record_after_launch == record and not crash["owner_processes_after_stop"] and not owners_after_launch,
                   "no replacement owner starts while the dead record is retained", crash["relaunch"])
        signals = [r for r in self.records() if r["kind"] == "signal"]
        crash["provider_signal_records"] = signals
        self.check(not signals, "no provider process was signalled by Stop or relaunch", signals)
        crash["refusal_logged"] = any("explicit Stop must verify retained ownership before restart" in line
                                      for line in log_tail)
        turns_after = sum(1 for r in self.records() if r["kind"] == "turn")
        crash["turn_invocations"] = {"before_crash": turns_before, "after": turns_after}
        self.check(turns_after == turns_before, "no turn invocation was replayed after the owner death",
                   crash["turn_invocations"])

    # -- explicit stop ---------------------------------------------------------------

    def request_stop(self, resources, record):
        """Issue the app's Stop exactly as RuntimeHost does: the payload's --runtime-command stop."""
        started = time.monotonic()
        try:
            result = subprocess.run([sys.executable, "-B", str(resources / "backend.py"), "--runtime-command", "stop",
                                     "--db", str(self.database)], env=dict(self.env, PYTHONPATH=str(resources / "backend")),
                                    text=True, capture_output=True, timeout=STOP_COMMAND_SECONDS + 10)
            record["result"] = {"returncode": result.returncode, "stdout": result.stdout[-500:],
                                "stderr": result.stderr[-1500:]}
        except subprocess.TimeoutExpired as error:
            record["result"] = {"returncode": None, "stdout": "", "stderr": ("timeout: " + str(error))[-500:]}
        record["seconds"] = time.monotonic() - started

    def quiescence(self, timeout):
        """Processes still naming the root, with the time each took to exit within the timeout."""
        started = time.monotonic()
        processes = {pid: command for pid, command in processes_mentioning(str(self.root)).items()
                     if pid != os.getpid()}
        exited = {}
        while time.monotonic() - started < timeout and len(exited) < len(processes):
            for pid in processes:
                if pid not in exited and not process_alive(pid):
                    exited[pid] = round(time.monotonic() - started, 2)
            time.sleep(.05)
        return {str(pid): {"command": command[-160:], "exited_after": exited.get(pid)}
                for pid, command in processes.items()}

    @property
    def runtime_directory(self):
        return self.root / ".state.sqlite3.runtime"

    def transport_record(self):
        """Durable transport ownership state: the owner record, whether its pid lives, and the journal owner."""
        record_path = self.runtime_directory / "provider-transport/owner.json"
        result = {"owner_record": None, "owner_pid_alive": None, "journal": None}
        if record_path.exists():
            try:
                record = json.loads(record_path.read_text())
            except ValueError as error:
                return {**result, "owner_record": f"unreadable: {error}"}
            result["owner_record"] = {k: record.get(k) for k in ("pid", "instance", "protocol", "journal_generation")}
            result["owner_pid_alive"] = process_alive(record.get("pid")) if record.get("pid") else None
        journal = transport_journal_path(self.runtime_directory)
        if journal.exists():
            try:
                db = sqlite3.connect(f"file:{journal}?mode=ro", uri=True, timeout=1)
                try:
                    result["journal"] = {"ownership": [tuple(r) for r in db.execute("SELECT epoch, coordinator FROM ownership")],
                                         "channels": [tuple(r) for r in db.execute("SELECT channel, pid, state FROM channels")]}
                finally:
                    db.close()
            except sqlite3.Error as error:
                result["journal"] = f"unreadable: {error}"
        return result

    @staticmethod
    def file_tail(path, lines):
        try:
            return path.read_text(errors="replace").splitlines()[-lines:]
        except OSError as error:
            return [f"unavailable: {error}"]

    @staticmethod
    def native_stacks(pid):
        """Thread summary of a surviving process (macOS sample); explains where Stop is stuck."""
        try:
            result = subprocess.run(["sample", str(pid), "1", "-mayDie"], capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired) as error:
            return {"error": str(error)[:200]}
        lines = [line for line in result.stdout.splitlines() if line.startswith(("Thread_", "    ", "  "))]
        return {"returncode": result.returncode, "summary": "\n".join(lines)[:6000]}

    def durable_update(self):
        directory = runtime.runtime_directory(self.database)
        update_path, cancellation_path = directory / "update.json", directory / "update-cancellation.json"
        update = json.loads(update_path.read_text()) if update_path.exists() else None
        if update and cancellation_path.exists() and json.loads(cancellation_path.read_text()).get("id") == update["id"]:
            update["cancelled"] = True
        return update

    def scenario_stop_during_preparation(self):
        """Stop Work and Quit while the trial candidate prepares, then reopen."""
        self.start_turns()
        before = self.status()
        supervisor = self.supervisor_record()
        resources = self.root / "app-1/Contents/Resources"  # created by swap(1) through resources()
        stop = {}

        def during(expected, before_status, elapsed):
            if stop:
                return
            candidates = {pid: command for pid, command in
                          self.candidate_processes(expected, exclude={before_status["pid"]}).items()
                          if "--commit-fd" in command}
            if candidates:
                stop.update(candidates=candidates, at=time.monotonic(), swap_elapsed=round(elapsed, 3))
                stop["thread"] = threading.Thread(target=self.request_stop, args=(resources, stop), daemon=True)
                stop["thread"].start()

        entry = self.swap(1, expect_activation=False, during=during, until=lambda: "result" in stop)
        self.report["stop"] = {key: value for key, value in stop.items() if key != "thread"}
        self.check("candidates" in stop, "Stop requested while the trial candidate was preparing",
                   {"timeline": entry["timeline"][:30]})
        stop["thread"].join(timeout=STOP_COMMAND_SECONDS + 15)
        self.report["stop"] = {key: value for key, value in stop.items() if key != "thread"}
        result = stop.get("result") or {}
        try:
            answer = json.loads(result.get("stdout") or "null")
        except ValueError:
            answer = None
        self.report["stop"]["answer"] = answer
        self.check(result.get("returncode") == 0 and isinstance(answer, dict) and answer.get("status") == "stopped"
                   and stop.get("seconds", 1e9) < STOP_COMMAND_SECONDS,
                   "Stop confirmed by the runtime command within the app's limit",
                   {"seconds": stop.get("seconds"), "result": result})
        directory = runtime.runtime_directory(self.database)
        proof_path = directory / "stop-proof.json"
        proof = json.loads(proof_path.read_text()) if proof_path.exists() else None
        update = self.durable_update()
        path = stop_path(proof, update)
        self.report["stop"].update(proof=proof, update=update, path=path)
        self.check(proof is not None and proof.get("database") == str(self.database.resolve()),
                   "stop proof written for this database", proof)
        self.check(update is not None and update.get("cancelled"), "the update records the cancellation",
                   {k: (update or {}).get(k) for k in ("phase", "cancelled", "error")})
        self.check(path in {"cancelled-before-commit", "stopped-after-commit"}, "stop path identified", path)
        # Nothing may relaunch: no serving coordinator, no --runtime-serve process, ownership released.
        relaunched, quiet_until = None, time.monotonic() + 3
        while time.monotonic() < quiet_until and relaunched is None:
            try:
                status = self.status()
            except (OSError, RuntimeError, ValueError):
                status = None
            serving = {pid: command[-120:] for pid, command in processes_mentioning(str(self.root)).items()
                       if "--runtime-serve" in command}
            if status or serving:
                relaunched = {"status": status and {k: status.get(k) for k in ("pid", "status", "payload")},
                              "serving": serving}
            time.sleep(.1)
        self.check(relaunched is None and runtime.owner_available(directory),
                   "no coordinator relaunched after Stop and ownership was released", relaunched)
        processes = self.quiescence(timeout=5)
        self.report["stop"]["processes"] = processes
        survivors = {pid: p for pid, p in processes.items() if p["exited_after"] is None}
        tool = str(self.tool_child)
        # The fixture's tool child is a grandchild of the provider; whether Stop reaches it is reported, not assumed.
        self.report["stop"]["tool_child_survived"] = tool in survivors
        self.report["stop"]["survivor_stacks"] = {pid: self.native_stacks(int(pid)) for pid in survivors if pid != tool}
        failures = []
        for condition, name, evidence in (
                (not {pid: p for pid, p in survivors.items() if pid != tool},
                 "supervisor, coordinators, transport owner and providers exited after Stop", survivors),
                (not process_alive(before["pid"]) and all(not process_alive(pid) for pid in stop["candidates"])
                 and (supervisor is None or not process_alive(supervisor["pid"])),
                 "previous coordinator, trial candidate and supervisor are gone",
                 {"previous_coordinator": process_alive(before["pid"]), "supervisor": supervisor,
                  "trial_candidates": {pid: process_alive(pid) for pid in stop["candidates"]}})):
            try:
                self.check(condition, name, evidence)
            except AssertionError as error:
                failures.append(error)  # keep going: the restart half of the scenario is evidence too
        if survivors:
            # A survivor after Stop is itself the finding; it must not also block the restart evidence.
            for pid in survivors:
                if pid != tool and self.owned(int(pid)):
                    os.kill(int(pid), signal.SIGTERM)
            self.report["stop"]["survivors_terminated_by_driver"] = sorted(pid for pid in survivors if pid != tool)
            self.wait(lambda: all(not process_alive(int(pid)) for pid in survivors if pid != tool), timeout=10)
        (self.root / "release").touch()  # the fixture tool child exits on its own once released
        # Reopening the app bootstraps the new resources again. The queued message and the interrupted
        # turns must not replay: every turn invocation stays unique.
        restarted_at = time.monotonic()
        restart = {"bootstrap_error": None, "transport_before_relaunch": self.transport_record()}
        try:
            restart["bootstrap"] = self.launch(resources)
        except AssertionError as error:
            restart["bootstrap_error"] = str(error)[-2000:]
            restart["runtime_log_tail"] = self.file_tail(self.runtime_directory / "runtime.log", 80)
            restart["supervisor_log_tail"] = self.file_tail(self.runtime_directory / "supervisor.log", 40)
            restart["transport_after_failure"] = self.transport_record()
        status = None
        while time.monotonic() - restarted_at < ACTIVATION_SECONDS:
            try:
                status = self.status()
            except (OSError, RuntimeError, ValueError):
                status = None
            if status and status.get("status") == "serving":
                break
            time.sleep(.1)
        restart.update(seconds=time.monotonic() - restarted_at,
                       status=status and {k: status.get(k) for k in ("pid", "status", "payload", "pending", "update",
                                                                     "upgrade_error", "paused")})
        self.report["restart"] = restart
        self.check(status is not None and status.get("status") == "serving", "runtime serves again after Stop", restart)
        time.sleep(2)  # give any replay a chance to show up in the fixture records
        # Serving is not enough: the reopened coordinator must consume provider events and run new work.
        # A paused projection (transport error in status) or a new turn that never completes is a failed reopen.
        reopen = restart["reopen"] = {"transport": None, "turn": None, "completed": False, "seconds": None}
        try:
            transport = (self.status() or {}).get("transport")
            reopen["transport"] = transport
            self.check(transport is not None and not transport.get("error"),
                       "projection healthy after reopen (no transport or delivery error)", transport)
        except AssertionError as error:
            failures.append(error)
        try:
            opened_at = time.monotonic()
            value = self.api("api/browser/sessions", {"cwd": str(self.root), "client": "codex", "name": "Validation reopen"})
            reopen_thread = value["session"]["thread_id"]
            self.api(f"api/browser/sessions/{reopen_thread}/messages", {"message": "reopen"})
            reopen["turn"] = reopen_thread
            completed = self.wait_quiet(lambda: any(r["kind"] == "complete" and r["thread"] == reopen_thread
                                                    for r in self.records()), timeout=20)
            reopen["completed"] = completed
            reopen["seconds"] = time.monotonic() - opened_at
            if completed:
                self.wait_quiet(lambda: not self.api(f"api/browser/sessions/{reopen_thread}")["running"], timeout=10)
                detail = self.api(f"api/browser/sessions/{reopen_thread}")
                reopen["running_after_completion"] = detail["running"]
                reopen["agent_text"] = "".join(i.get("text", "") for turn in detail["thread"]["turns"]
                                               for i in turn["items"] if i.get("type") == "agentMessage")
                reopen["provider_text"] = "".join(r["delta"] for r in self.records() if r["kind"] == "output"
                                                  and r["thread"] == reopen_thread)
            self.check(completed and not reopen.get("running_after_completion", True)
                       and reopen.get("agent_text") == reopen.get("provider_text") and reopen.get("provider_text"),
                       "a newly submitted turn completes after reopen and its output reaches the interface", reopen)
        except AssertionError as error:
            failures.append(error)
        except Exception as error:  # an interface request that hangs or errors after reopen is the finding itself
            reopen["error"] = f"{type(error).__name__}: {error}"
            reopen["seconds"] = time.monotonic() - opened_at
            try:
                self.check(False, "a newly submitted turn completes after reopen and its output reaches the interface", reopen)
            except AssertionError as failure:
                failures.append(failure)
        self.stopped.set()
        self.observer.join(timeout=5)
        records = self.records()
        counts = Counter(r["role"] for r in records if r["kind"] == "turn")
        self.report["restart"]["turn_invocations"] = dict(counts)
        self.check(all(count == 1 for count in counts.values()) and counts["stream"] == 1,
                   "no turn invocation repeated across Stop and restart", dict(counts))
        for verifier, arguments in ((self.verify_events, (None, stop["at"])), (self.verify_traffic, (records,))):
            try:
                verifier(*arguments)
            except AssertionError as error:
                failures.append(error)
        if failures:
            raise failures[0]

    def close(self):
        self.stopped.set()
        self.traffic_active.clear()
        for thread in (self.sse_thread, self.traffic_thread):
            if thread is not None:
                thread.join(timeout=10)
        if self.observations:
            # Same timeline as the swaps: (monotonic time, visible text length, running flag).
            self.report["observation_timeline"] = [(round(o["at"], 3), len(o["text"]), o["running"])
                                                   for o in self.observations]
            self.report["observer_error_timeline"] = [(round(e["at"], 3), str(e["error"])[:60])
                                                      for e in self.observer_errors]
        if self.sse_cursor is not None and self.report.get("sse") is None:
            self.report["sse"] = self.sse_cursor.summary()
        if isinstance(self.report.get("traffic"), list):
            # The run stopped before the final evaluation; keep the raw outcome counts.
            self.report["traffic"] = {"outcomes": dict(Counter(o["outcome"] for o in self.traffic_outcomes)),
                                      "evaluated": False}
        self.report["identity_files"] = sorted(p.name for p in self.identity_dir.glob("*.json")) if self.identity_dir.exists() else []
        journal = transport_journal_path(runtime.runtime_directory(self.database))
        self.report["transport_journal"] = {"path": str(journal), "exists": journal.exists(),
                                            "bytes": journal.stat().st_size if journal.exists() else 0}
        if self.sse_cursor is not None:
            windows = self.outage_windows([{"at": r["at"]} for r in self.sse_cursor.reconnects])
            self.report["sse_reconnect_windows_seconds"] = quantiles(windows)
        # A failed run can leave the fake approval unanswered; the gate's stop then
        # waits on that turn and leaks the runtime. Answer it before stopping.
        try:
            if getattr(self, "threads", None) and not any(r["kind"] == "approval_answer" for r in self.records()):
                self.api(f"api/browser/sessions/{self.threads['approval']}/requests/{self.approval_key}", {"decision": "accept"})
        except Exception as error:
            self.report["cleanup_approval_error"] = str(error)[:300]
        super().close()
        self.report["tool_outcomes"] = tool_outcomes(self.records())
        # Nothing from the user's runtime is ever signalled: cleanup went through the
        # private discovery record. Processes still naming this root are proved
        # ours by that command line, then terminated and reported.
        self.report["leftover_processes"] = self.terminate_leftovers()
        self.output.write_text(json.dumps(self.report, indent=2) + "\n")

    def terminate_leftovers(self):
        # Stop has returned; everything under the root should already be gone. Allow a short grace
        # period and record how long any survivor took, then terminate what remains.
        started = time.monotonic()
        leftovers = {pid: command for pid, command in processes_mentioning(str(self.root)).items() if pid != os.getpid()}
        exited_after = {}
        while time.monotonic() - started < 5 and any(process_alive(pid) for pid in leftovers):
            time.sleep(.1)
            for pid in leftovers:
                if pid not in exited_after and not process_alive(pid):
                    exited_after[pid] = round(time.monotonic() - started, 2)
        self.report["leftover_exit_seconds"] = {str(pid): seconds for pid, seconds in exited_after.items()}
        for pid in leftovers:
            if self.owned(pid):
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and any(process_alive(pid) for pid in leftovers):
            time.sleep(.1)
        for pid in leftovers:
            if process_alive(pid) and self.owned(pid):
                os.kill(pid, signal.SIGKILL)
        return {str(pid): {"command": command, "alive_after_cleanup": process_alive(pid)} for pid, command in leftovers.items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenario", choices=SCENARIOS + ("all",), default="all")
    parser.add_argument("--swaps", type=int, default=5, help="swaps for steady and idle-control")
    parser.add_argument("--report", type=Path, required=True, help="report file; per-scenario files sit beside it")
    args = parser.parse_args()
    if args.swaps < 1:
        parser.error("--swaps must be positive")
    scenarios = SCENARIOS if args.scenario == "all" else (args.scenario,)
    summary = {"scenarios": {}, "passed": True}
    report = args.report.resolve()
    report.parent.mkdir(parents=True, exist_ok=True)
    for scenario in scenarios:
        output = report.with_name(f"{report.stem}-{scenario}{report.suffix or '.json'}")
        run = Validation(output, args.swaps, scenario)
        try:
            run.run()
        except Exception:
            run.report["error"] = traceback.format_exc()
        finally:
            run.close()
        failed = [c["name"] for c in run.report["checks"] if not c["passed"]]
        summary["scenarios"][scenario] = {"passed": run.report["passed"], "report": str(output), "root": str(run.root),
                                          "failed_checks": failed, "error": (run.report.get("error") or "").splitlines()[-1:]}
        summary["passed"] = summary["passed"] and run.report["passed"]
        print(json.dumps({"scenario": scenario, "passed": run.report["passed"], "failed_checks": failed}), flush=True)
    report.write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps({"passed": summary["passed"], "report": str(report)}))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
