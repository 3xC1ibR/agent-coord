"""Lazy coordination measurements: cached reads and an isolated, bounded worker.

This service never reads provider histories or tokenizes text on a request or
provider-event thread. Derived files have their own directory and database; the
coordination database is never opened by this service. No worker runs at startup.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from .store import CoordinationError

PERIODS = (7, 30, 90)
MAX_SNAPSHOT_BYTES = 512 * 1024
CACHE_SECONDS = 1.0
REFRESH_INTERVAL = 30.0
MAX_REFRESH_SECONDS = 180.0
PASS_SECONDS = 10.0
PASS_BYTES = 64 * 1024 * 1024


def _read_json(path, maximum):
    try:
        with path.open("rb") as source:
            if os.fstat(source.fileno()).st_size > maximum:
                return None
            data = source.read(maximum + 1)
        if len(data) > maximum:
            return None
        value = json.loads(data)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, RecursionError):
        return None


def _counts():
    return {"calls": 0, "threads": 0, "turns": 0, "argument_tokens": None,
            "return_tokens": None, "lifecycle_tokens": None, "argument_bytes": 0,
            "return_bytes": 0, "calls_per_turn": None, "tokens_per_turn": None}


def _valid_snapshot(value):
    if not value or value.get("schema_version") != 1:
        return False
    if not isinstance(value.get("periods"), dict):
        return False
    if any(key in value and not isinstance(value[key], dict) for key in ("measurement", "coverage")):
        return False
    for report in value["periods"].values():
        if not isinstance(report, dict):
            return False
        if any(key in report and not isinstance(report[key], dict) for key in ("period", "current", "previous")):
            return False
        if any(key in report and (not isinstance(report[key], list) or
                any(not isinstance(row, dict) for row in report[key])) for key in ("daily", "tools")):
            return False
    return True


class CoordinationMetrics:
    def __init__(self, database_path, cwd=None, *, codex_home=None, python=None,
                 clock=time.time, monotonic=time.monotonic, popen=subprocess.Popen):
        database_path = Path(database_path).expanduser().resolve()
        self.root_dir = database_path.with_name("." + database_path.name + ".coordination-metrics")
        self.cwd = str(Path(cwd).expanduser().resolve()) if cwd else None
        scope = hashlib.sha256((self.cwd or "all").encode()).hexdigest()[:24]
        self.state_dir = self.root_dir / scope
        self.codex_home = Path(codex_home or os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser().resolve()
        self.python = python
        self.clock, self.monotonic, self.popen = clock, monotonic, popen
        self.lock = threading.RLock()
        self.stopped = threading.Event()
        self.thread = None
        self.process = None
        self._running = False
        self._last_started = None
        self._cache_at = float("-inf")
        self._cached = None
        self._progress = None
        self._error = None

    def _cache(self):
        now = self.monotonic()
        if now - self._cache_at < CACHE_SECONDS:
            return
        self._cache_at = now
        snapshot = _read_json(self.state_dir / "snapshot.json", MAX_SNAPSHOT_BYTES)
        if _valid_snapshot(snapshot):
            self._cached = snapshot
        self._progress = _read_json(self.state_dir / "progress.json", 16 * 1024)
        if self._progress and not isinstance(self._progress.get("progress", {}), dict):
            self._progress = None

    def snapshot(self, days=30):
        if type(days) is not int or days not in PERIODS:
            raise CoordinationError("Choose a 7, 30, or 90 day coordination period.")
        with self.lock:
            self._cache()
            saved = self._cached or {}
            result = deepcopy(saved.get("periods", {}).get(str(days), {}))
            now = self.clock()
            result.setdefault("period", {"start": now - days * 86400, "end": now, "days": days})
            result.setdefault("current", _counts())
            result.setdefault("previous", _counts())
            result.setdefault("daily", [])
            result.setdefault("tools", [])
            result.update({"schema_version": 1,
                "measurement": deepcopy(saved.get("measurement", {
                    "unit": "bytes", "method": "utf8_bytes", "tokenizer_available": False})),
                "coverage": deepcopy(saved.get("coverage", {
                    "providers": ["codex"], "scope": self.cwd or "All workspaces",
                    "notes": ["Retained Codex tool records. Other providers and compound shell commands are not included."]}))})
            progress = self._progress or {}
            state = progress.get("state", "ready" if saved else "not_loaded")
            message = progress.get("message", "No coordination measurements yet.")
            if self._running:
                state, message = "running", "Updating coordination measurements in the background."
            elif self._error:
                state, message = "error", self._error
            elif state == "running":
                state, message = "partial", "The previous refresh stopped. Refresh to continue indexing."
            if not isinstance(state, str) or state not in {"not_loaded", "running", "ready", "partial", "unavailable", "error"}:
                state, message = "error", "Coordination measurements are unavailable."
            result["status"] = {"state": state, "message": str(message)[:500],
                "updated_at": saved.get("generated_at"), "running": self._running,
                "progress": deepcopy(progress.get("progress", {})),
                "next_refresh_at": self._last_started + REFRESH_INTERVAL if self._last_started is not None else None}
            return result

    def refresh(self, days=30):
        if type(days) is not int or days not in PERIODS:
            raise CoordinationError("Choose a 7, 30, or 90 day coordination period.")
        with self.lock:
            if self.stopped.is_set():
                raise CoordinationError("Coordination measurements are shutting down.")
            now = self.clock()
            if not self._running and (self._last_started is None or now - self._last_started >= REFRESH_INTERVAL):
                self._running = True
                self._last_started = now
                self._error = None
                self.thread = threading.Thread(target=self._run, name="coordination-metrics", daemon=True)
                self.thread.start()
        return self.snapshot(days)

    def _interpreter(self):
        configured = self.python or os.environ.get("AGENT_COORD_METRICS_PYTHON")
        if configured:
            path = Path(configured).expanduser()
            if not path.is_absolute() or not path.is_file():
                raise ValueError("Invalid metrics interpreter")
            return str(path)
        optional = self.root_dir / "python" / "bin" / "python"
        return str(optional) if optional.is_file() else sys.executable

    def _run(self):
        process = None
        started = self.monotonic()
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
            environment["TIKTOKEN_CACHE_DIR"] = os.environ.get("TIKTOKEN_CACHE_DIR", str(self.root_dir / "tokenizer-cache"))
            command = [self._interpreter(), "-B", "-m", "agent_coord.coordination_metrics_worker",
                       "--state-dir", str(self.state_dir), "--codex-home", str(self.codex_home),
                       "--max-seconds", str(PASS_SECONDS), "--max-bytes", str(PASS_BYTES)]
            if self.cwd:
                command += ["--cwd", self.cwd]
            # Keep bulk history reads below interactive app work on macOS.
            # The child also lowers its own priority on other POSIX systems.
            if sys.platform == "darwin" and Path("/usr/sbin/taskpolicy").is_file():
                command = ["/usr/sbin/taskpolicy", "-b", "-d", "throttle", *command]
            while not self.stopped.is_set() and self.monotonic() - started < MAX_REFRESH_SECONDS:
                process = self.popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, env=environment, start_new_session=True)
                with self.lock:
                    self.process = process
                # Only this supervisor waits; UI and event threads never do.
                deadline = self.monotonic() + PASS_SECONDS + 20
                while process.poll() is None and not self.stopped.wait(.1):
                    if self.monotonic() > deadline:
                        self._terminate(process)
                        raise TimeoutError("Metrics pass exceeded its budget")
                if self.stopped.is_set():
                    self._terminate(process)
                    break
                if process.returncode:
                    raise RuntimeError("Metrics worker failed")
                with self.lock:
                    self.process = None
                    self._cache_at = float("-inf")
                    self._cache()
                    more = bool((self._progress or {}).get("progress", {}).get("has_more"))
                if not more or self.stopped.wait(.2):
                    break
        except (OSError, ValueError, RuntimeError, TimeoutError):
            with self.lock:
                self._error = "Could not update coordination measurements. Existing results are preserved; retry Refresh."
        finally:
            if process is not None and process.poll() is None:
                self._terminate(process)
            with self.lock:
                self.process = None
                self._running = False
                self._cache_at = float("-inf")

    @staticmethod
    def _terminate(process):
        if process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)
        except ProcessLookupError:
            pass

    def close(self):
        self.stopped.set()
        with self.lock:
            process, thread = self.process, self.thread
        if process is not None:
            self._terminate(process)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=3)
