#!/usr/bin/env python3
"""Exercise real runtime payload swaps against independent isolated providers.

This is an acceptance gate, not a simulation of the coordinator. It copies the
actual backend and invokes its public bootstrap/control and browser APIs. All
state, commands, providers and cleanup belong to a newly created private root.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.request

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "plugins/agent-coord/scripts"))
from agent_coord import desktop_runtime as runtime


def quantiles(values):
    ordered = sorted(values)
    if not ordered:
        return {"count": 0, "p50": None, "p95": None, "max": None}
    return {"count": len(ordered), "p50": ordered[int((len(ordered) - 1) * .5)],
            "p95": ordered[int((len(ordered) - 1) * .95)], "max": ordered[-1]}


class Acceptance:
    def __init__(self, output, swaps):
        self.root = Path(tempfile.mkdtemp(prefix="ribbon-live-acceptance-"))
        self.output = output
        self.swaps = swaps
        self.database = self.root / "state.sqlite3"
        self.report = {"root": str(self.root), "swaps_requested": swaps, "swaps": [],
                       "checks": [], "passed": False}
        self.env = dict(os.environ)
        # No inherited coordination identity or real provider credentials.
        for key in list(self.env):
            if key.startswith(("AGENT_COORD_", "RIBBON_RUNTIME_", "ZELLIJ")) or key in {
                    "CODEX_THREAD_ID", "CLAUDECODE", "PYTHONHOME"}:
                self.env.pop(key)
        self.env.update(RIBBON_FAKE_ROOT=str(self.root), PYTHONNOUSERSITE="1",
                        PYTHONDONTWRITEBYTECODE="1", PYTHONWARNINGS="error::ResourceWarning")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        fixture = REPO / "tests/fixtures/runtime_live_provider.py"
        for provider in ("codex", "claude"):
            executable = self.bin / provider
            executable.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + " " +
                                  shlex.quote(str(fixture)) + " " + provider + ' "$@"\n')
            executable.chmod(0o700)
        self.env["PATH"] = str(self.bin) + os.pathsep + self.env["PATH"]
        spec = importlib.util.spec_from_file_location("acceptance_build", REPO / "desktop/macos/build.py")
        self.build = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.build)
        self.stopped = threading.Event()
        self.observer = None
        self.observations = []
        self.observer_errors = []
        self.provider_ids = set()
        self.report["gate_hashes"] = {str(path.relative_to(REPO)): hashlib.sha256(path.read_bytes()).hexdigest()
                                      for path in (Path(__file__).resolve(), fixture)}

    def check(self, condition, name, evidence=None):
        self.report["checks"].append({"name": name, "passed": bool(condition), "evidence": evidence})
        if not condition:
            raise AssertionError(name + ": " + repr(evidence))

    def records(self):
        path = self.root / "provider.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def wait(self, predicate, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            time.sleep(.025)
        raise AssertionError(f"Condition timed out after {timeout} seconds")

    def resources(self, generation):
        target = self.root / f"app-{generation}/Contents/Resources"
        target.mkdir(parents=True)
        self.build.copy_backend(target)
        shutil.copyfile(REPO / "desktop/macos/backend.py", target / "backend.py")
        (target / "backend.json").write_text(json.dumps({"python": sys.executable, "database": None,
            "path": None, **runtime.compatibility_metadata(f"8000.0.{generation}+{generation + 1}")}))
        backend = target / "backend/agent_coord/desktop_runtime.py"
        with backend.open("a") as stream:
            stream.write(f"\nACCEPTANCE_PAYLOAD_GENERATION = {generation}\n")
        return target

    def launch(self, resources):
        result = subprocess.run([sys.executable, "-B", str(resources / "backend.py"),
            "--runtime-bootstrap", "--resources", str(resources), "--db", str(self.database)],
            env=dict(self.env, PYTHONPATH=str(resources / "backend")), text=True,
            capture_output=True, timeout=60)
        if result.returncode:
            raise AssertionError("Bootstrap failed: " + result.stderr)
        return json.loads(result.stdout)

    def status(self):
        record = runtime.discover(self.database, timeout=2)
        return runtime.control(record, "status", timeout=2) if record else None

    def api(self, path, body=None):
        status = self.status()
        headers = {}
        if body is not None:
            with urllib.request.urlopen(status["url"] + "api/browser/config", timeout=3) as response:
                headers["X-Agent-Coord-Token"] = json.load(response)["token"]
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(status["url"] + path,
            data=json.dumps(body).encode() if body is not None else None, headers=headers)
        with urllib.request.urlopen(request, timeout=3) as response:
            return json.load(response)

    @staticmethod
    def text(detail):
        return "".join(item.get("text", "") for turn in detail["thread"].get("turns", [])
                       for item in turn.get("items", []) if item.get("type") == "agentMessage")

    def observe(self, identity):
        while not self.stopped.is_set():
            started = time.monotonic()
            try:
                detail = self.api("api/browser/sessions/" + identity)
                self.observations.append({"at": time.monotonic(), "duration": time.monotonic() - started,
                                          "text": self.text(detail), "running": detail["running"]})
            except Exception as error:
                self.observer_errors.append({"at": time.monotonic(), "error": str(error)})
            self.stopped.wait(.1)

    def run(self):
        first = self.launch(self.resources(0))
        self.report["initial"] = first
        threads = {}
        for role, provider in (("stream", "codex"), ("tool", "codex"), ("approval", "claude")):
            value = self.api("api/browser/sessions", {"cwd": str(self.root), "client": provider,
                                                     "name": "Acceptance " + role})
            identity = threads[role] = value["session"]["thread_id"]
            self.api(f"api/browser/sessions/{identity}/messages", {"message": role})
        self.wait(lambda: any(x["kind"] == "approval" for x in self.records()))
        self.wait(lambda: any(x["kind"] == "tool_pid" for x in self.records()))
        details = {role: self.api(f"api/browser/sessions/{identity}") for role, identity in threads.items()}
        self.check(all(d["running"] for d in details.values()), "three simultaneous running turns")
        approval_key = details["approval"]["requests"][0]["key"]
        turns = {role: d["activeTurn"] for role, d in details.items()}
        self.report["threads"] = threads
        self.report["turns"] = turns
        self.provider_ids = {r["pid"] for r in self.records() if r["kind"] == "start"}
        self.api(f"api/browser/sessions/{threads['stream']}/queue", {"message": "queued-after-stream"})
        self.observer = threading.Thread(target=self.observe, args=(threads["stream"],))
        self.observer.start()
        for generation in range(1, self.swaps + 1):
            resources = self.resources(generation)
            expected = runtime.payload_digest(resources)
            before = self.status()
            started = time.monotonic()
            self.launch(resources)
            current = None
            while time.monotonic() - started < 60:
                try:
                    current = self.status()
                    if current and Path(current["payload"]).name == expected:
                        break
                except (OSError, RuntimeError, ValueError):
                    pass
                time.sleep(.05)
            elapsed = time.monotonic() - started
            self.report["swaps"].append({"generation": generation, "seconds": elapsed,
                "before": before, "after": current, "expected_digest": expected})
            self.check(current and Path(current["payload"]).name == expected and elapsed <= 60,
                       "new backend active within 60 seconds", self.report["swaps"][-1])
            for role, identity in threads.items():
                detail = self.api(f"api/browser/sessions/{identity}")
                self.check(detail["running"] and detail["activeTurn"] == turns[role],
                           f"same {role} turn after swap {generation}", detail["activeTurn"])
            records = self.records()
            self.check(not any(r["kind"] in {"signal", "stdin_eof"} for r in records),
                       "no provider signal or transport EOF")
            self.check({r["pid"] for r in records if r["kind"] == "start"} == self.provider_ids,
                       "same provider and tool processes")
        self.api(f"api/browser/sessions/{threads['approval']}/requests/{approval_key}", {"decision": "accept"})
        (self.root / "release").touch()
        self.wait(lambda: all(not self.api(f"api/browser/sessions/{identity}")["running"]
                              for identity in threads.values()), timeout=20)
        self.wait(lambda: any(r["kind"] == "turn" and r["role"] == "queued-after-stream" for r in self.records()))
        self.stopped.set()
        self.observer.join(timeout=5)
        records = self.records()
        self.check(len([r for r in records if r["kind"] == "approval_answer"]) == 1,
                   "original approval answered exactly once")
        counts = Counter(r["role"] for r in records if r["kind"] == "turn")
        self.check(counts == Counter({"stream": 1, "tool": 1, "approval": 1, "queued-after-stream": 1}),
                   "turn invocations exactly once", dict(counts))
        self.check(len([r for r in records if r["kind"] == "tool_start"]) == 1,
                   "long tool invoked exactly once")
        expected = "".join(r["delta"] for r in records if r["kind"] == "output" and
                           r["thread"] == threads["stream"] and r["turn"] == turns["stream"])
        detail = self.api(f"api/browser/sessions/{threads['stream']}")
        turn = next(t for t in detail["thread"]["turns"] if t["id"] == turns["stream"])
        actual = "".join(i.get("text", "") for i in turn["items"] if i.get("type") == "agentMessage")
        self.check(actual == expected, "output has no gaps or duplicates", {"expected": len(expected), "actual": len(actual)})
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
        self.report["activation_seconds"] = quantiles([s["seconds"] for s in self.report["swaps"]])
        self.check(bool(stalls) and max(stalls) < 5, "visible stream stalls below five seconds", quantiles(stalls))
        self.report["passed"] = True

    def close(self):
        self.stopped.set()
        if self.observer is not None:
            self.observer.join(timeout=10)
        # Release only fake tool children before explicit cleanup. This never
        # reads or signals a PID from the user's runtime or another test root.
        (self.root / "release").touch()
        try:
            record = runtime.discover(self.database, timeout=2)
            if record:
                runtime.control(record, "stop", timeout=5)
                self.wait(lambda: runtime.owner_available(runtime.runtime_directory(self.database)), timeout=30)
        except Exception as error:
            self.report["cleanup_error"] = str(error)
        self.report["observer_errors"] = self.observer_errors
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.output.write_text(json.dumps(self.report, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--swaps", type=int, default=50)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if args.swaps < 1:
        parser.error("--swaps must be positive")
    run = Acceptance(args.report.resolve(), args.swaps)
    try:
        run.run()
    except Exception:
        run.report["error"] = traceback.format_exc()
    finally:
        run.close()
    print(json.dumps({"passed": run.report["passed"], "report": str(run.output), "root": str(run.root)}))
    return 0 if run.report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
