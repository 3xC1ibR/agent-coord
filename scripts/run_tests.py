#!/usr/bin/env python3
"""Run intact unittest discovery suites in processes and audit equivalence.

No extra packages or environment substitution. --jobs 1 runs the full discovery
suite in order; larger values isolate each top-level discovery suite in a child.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


def test_ids(suite):
    if isinstance(suite, unittest.TestSuite):
        return [name for child in suite for name in test_ids(child)]
    return [suite.id()]


def environment():
    packages = {}
    for name in ("cryptography", "http-ece", "pywebpush"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    # Hash the inherited environment rather than disclosing tokens in reports.
    inherited = json.dumps(sorted(os.environ.items())).encode()
    return {"executable": sys.executable, "version": sys.version,
            "prefix": sys.prefix, "base_prefix": sys.base_prefix,
            "flags": list(sys.flags), "xoptions": dict(sys._xoptions),
            "sys_path": list(sys.path),
            "packages": packages, "warnings": list(sys.warnoptions),
            "environment_sha256": hashlib.sha256(inherited).hexdigest()}


def interpreter_options():
    # Use the same flag reconstruction as stdlib multiprocessing; extend it for
    # arbitrary -X options (the Python 3.10 helper has a fixed allowlist).
    options = subprocess._args_from_interpreter_flags()
    present = {options[i + 1] for i, value in enumerate(options[:-1]) if value == "-X"}
    for name, value in sys._xoptions.items():
        option = name if value is True else f"{name}={value}"
        if option not in present:
            options.extend(["-X", option])
    if sys.flags.hash_randomization:
        options.append("-R")
    if getattr(sys.flags, "safe_path", False) and not sys.flags.isolated:
        options.append("-P")
    return options


def source_files(start):
    paths = {Path(__file__).resolve()}
    roots = [start]
    if start.is_relative_to(ROOT):
        roots += [ROOT / "plugins/agent-coord", ROOT / "scripts", ROOT / "desktop/macos",
                  ROOT / "docs", ROOT / ".agents/plugins", ROOT / ".github"]
        paths.update(p for p in (ROOT / "pyproject.toml", ROOT / "uv.lock") if p.exists())
    for root in roots:
        for path in root.rglob("*"):
            if (path.is_file() and "__pycache__" not in path.parts
                    and path.suffix != ".pyc" and path.name != ".DS_Store"):
                paths.add(path)
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)}


class RecordingResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.started = []
        self.events = []

    def startTest(self, test):
        self.started.append(test.id())
        super().startTest(test)

    def addSuccess(self, test):
        self.events.append([test.id(), "pass", ""])
        super().addSuccess(test)

    def addSkip(self, test, reason):
        self.events.append([test.id(), "skip", reason])
        super().addSkip(test, reason)

    def addError(self, test, err):
        self.events.append([test.id(), "error", self._exc_info_to_string(err, test)])
        super().addError(test, err)

    def addFailure(self, test, err):
        self.events.append([test.id(), "failure", self._exc_info_to_string(err, test)])
        super().addFailure(test, err)

    def addExpectedFailure(self, test, err):
        self.events.append([test.id(), "expected_failure", ""])
        super().addExpectedFailure(test, err)

    def addUnexpectedSuccess(self, test):
        self.events.append([test.id(), "unexpected_success", ""])
        super().addUnexpectedSuccess(test)

    def addSubTest(self, test, subtest, err):
        outcome = "subtest_pass" if err is None else "subtest_failure"
        self.events.append([subtest.id(), outcome, ""])
        super().addSubTest(test, subtest, err)


def run_suite(suite):
    discovered = test_ids(suite)
    started = time.monotonic()
    result = unittest.TextTestRunner(verbosity=2, resultclass=RecordingResult).run(suite)
    return {"discovered": discovered, "started": result.started, "events": result.events,
            "tests_run": result.testsRun, "successful": result.wasSuccessful(),
            "wall_seconds": time.monotonic() - started}


def differences(before, after):
    errors = []
    for key in ("environment", "sources"):
        if before[key] != after[key]:
            errors.append(key + " differs")
    for key in ("discovered", "started", "events"):
        left = Counter(json.dumps(item, sort_keys=True) for item in before[key])
        right = Counter(json.dumps(item, sort_keys=True) for item in after[key])
        if left != right:
            errors.append(f"{key} differs: {sum((left-right).values())} missing, "
                          f"{sum((right-left).values())} added occurrences")
    if not before["successful"] or not after["successful"]:
        errors.append("both runs must pass all integrity and test checks")
    return errors


def main(argv=None):
    began = time.monotonic()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=int, default=min(6, os.cpu_count() or 1))
    parser.add_argument("--start-directory", type=Path, default=ROOT / "tests")
    parser.add_argument("--pattern", default="test*.py")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--compare", type=Path, help="Fail unless this earlier report is equivalent")
    parser.add_argument("--timeout", type=float, default=300, help="Seconds per isolated suite")
    parser.add_argument("--worker", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.jobs < 1 or args.timeout <= 0:
        parser.error("jobs and timeout must be positive")
    if args.report is None:
        parser.error("--report is required")
    if sys.flags.inspect or sys.flags.interactive:
        parser.error("interactive interpreter options are unsupported")
    args.start_directory = args.start_directory.resolve()
    # The interpreter option is also inherited by test subprocesses through
    # sys.warnoptions; do not silently relax the repository's warning policy.
    if "error::ResourceWarning" not in sys.warnoptions:
        parser.error("invoke python with -W error::ResourceWarning")
    sources = source_files(args.start_directory) if args.worker is None else None
    suite = unittest.defaultTestLoader.discover(str(args.start_directory), pattern=args.pattern)
    if args.worker is not None:
        inherited = environment()
        data = run_suite(list(suite)[args.worker])
        data["environment"] = inherited
        args.report.write_text(json.dumps(data, indent=2) + "\n")
        return 0 if data["successful"] else 1

    report = {"schema": 1, "jobs": args.jobs, "environment": environment(),
              "sources": sources, "discovered": test_ids(suite),
              "started": [], "events": [], "units": [], "integrity_errors": []}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    logdir = args.report.parent / (args.report.stem + ".logs")
    logdir.mkdir(exist_ok=True)
    active = set()
    lock = threading.Lock()
    cancelled = threading.Event()

    def stop(proc):
        # Each worker owns a new process group, including its test subprocesses.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def worker(index, expected, directory):
        output = directory / f"{index}.json"
        log = logdir / f"{index}.log"
        command = [sys.executable, *interpreter_options()]
        command.extend([str(Path(__file__).resolve()), "--worker", str(index),
                        "--start-directory", str(args.start_directory), "--pattern", args.pattern,
                        "--report", str(output)])
        with log.open("w") as stream:
            with lock:
                if cancelled.is_set():
                    return {"successful": False, "error": "cancelled"}
                proc = subprocess.Popen(command, stdout=stream, stderr=stream, start_new_session=True)
                active.add(proc)
            try:
                code = proc.wait(timeout=args.timeout)
            except subprocess.TimeoutExpired:
                stop(proc)
                proc.wait()
                return {"successful": False, "error": f"suite {index} timed out; see {log}"}
            finally:
                with lock:
                    active.discard(proc)
        if code not in (0, 1) or not output.exists():
            return {"successful": False, "error": f"suite {index} exited {code}; see {log}"}
        data = json.loads(output.read_text())
        data.update(index=index, log=str(log))
        if data["discovered"] != expected:
            data["error"] = f"suite {index} discovery changed"
        if data["environment"] != report["environment"]:
            data["error"] = f"suite {index} environment changed"
        return data

    if args.jobs == 1:
        units = [run_suite(suite)]
    else:
        previous_sigterm = signal.getsignal(signal.SIGTERM)

        def terminate(signum, frame):
            raise SystemExit(128 + signum)

        signal.signal(signal.SIGTERM, terminate)
        pool = ThreadPoolExecutor(max_workers=args.jobs)
        temporary = tempfile.TemporaryDirectory(prefix="unittest-results-")
        try:
            futures = [pool.submit(worker, index, test_ids(unit), Path(temporary.name))
                       for index, unit in enumerate(suite) if unit.countTestCases()]
            units = []
            for future in as_completed(futures):
                unit = future.result()
                units.append(unit)
                print(f"{len(units)}/{len(futures)} suites finished", flush=True)
        finally:
            cancelled.set()
            with lock:
                for proc in active:
                    stop(proc)
            pool.shutdown(wait=True, cancel_futures=True)
            temporary.cleanup()
            signal.signal(signal.SIGTERM, previous_sigterm)

    for unit in units:
        report["started"].extend(unit.get("started", []))
        report["events"].extend(unit.get("events", []))
        if unit.get("error"):
            report["integrity_errors"].append(unit["error"])
        report["units"].append({k: v for k, v in unit.items()
                                if k not in ("events", "started", "discovered", "environment")})
    if Counter(report["discovered"]) != Counter(report["started"]):
        report["integrity_errors"].append("executed test occurrences differ from discovery")
    if not report["discovered"]:
        report["integrity_errors"].append("no tests discovered")
    if report["sources"] != source_files(args.start_directory):
        report["integrity_errors"].append("source files changed during run")
    report["wall_seconds"] = time.monotonic() - began
    report["successful"] = all(u["successful"] for u in units) and not report["integrity_errors"]
    report["comparison_errors"] = []
    if args.compare:
        report["comparison_errors"] = differences(json.loads(args.compare.read_text()), report)
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(f"{len(report['started'])} test occurrences in {report['wall_seconds']:.2f}s; "
          f"report: {args.report}")
    for error in report["integrity_errors"] + report["comparison_errors"]:
        print(error, file=sys.stderr)
    return 0 if report["successful"] and not report["comparison_errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
