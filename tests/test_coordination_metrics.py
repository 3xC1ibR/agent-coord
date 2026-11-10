from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from http.server import ThreadingHTTPServer
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))
from agent_coord.coordination_metrics import CoordinationMetrics, MAX_SNAPSHOT_BYTES
from agent_coord.desktop_runtime import AdmissionGate
from agent_coord.store import CoordinationError, CoordinationStore
from agent_coord.ui import _handler


class MetricsServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = 1000
        self.popen = Mock()
        self.service = CoordinationMetrics(self.root / "coordination.sqlite3", "/workspace/one",
            clock=lambda: self.now, monotonic=lambda: self.now, popen=self.popen)
        self.addCleanup(self.service.close)

    def save(self, calls=12):
        self.service.state_dir.mkdir(parents=True, exist_ok=True)
        payload = {"schema_version": 1, "generated_at": self.now,
                   "measurement": {"unit": "tokens", "method": "o200k_base", "tokenizer_available": True},
                   "coverage": {"providers": ["codex"], "notes": []},
                   "periods": {"7": {"current": {"calls": calls}, "tools": [{"operation": "checkpoint"}]},
                               "30": {"current": {"calls": calls * 2}}}}
        (self.service.state_dir / "snapshot.json").write_text(json.dumps(payload))
        return payload

    def test_constructor_and_reads_never_start_worker_or_create_storage(self):
        self.assertFalse(self.service.root_dir.exists())
        for _ in range(10):
            result = self.service.snapshot(7)
            self.assertEqual(result["status"]["state"], "not_loaded")
            self.assertIsNone(result["current"]["argument_tokens"])
        self.assertFalse(self.service.root_dir.exists())
        self.popen.assert_not_called()

    def test_cache_is_bounded_returns_copies_and_preserves_last_good_snapshot(self):
        self.save()
        first = self.service.snapshot(7)
        first["tools"].clear()
        self.assertEqual(len(self.service.snapshot(7)["tools"]), 1)
        self.save(99)
        self.assertEqual(self.service.snapshot(7)["current"]["calls"], 12)
        self.now += 2
        self.assertEqual(self.service.snapshot(7)["current"]["calls"], 99)
        (self.service.state_dir / "snapshot.json").write_bytes(b" " * (MAX_SNAPSHOT_BYTES + 1))
        self.now += 2
        self.assertEqual(self.service.snapshot(7)["current"]["calls"], 99)
        self.popen.assert_not_called()

    def test_workspace_isolation_and_invalid_periods(self):
        other = CoordinationMetrics(self.root / "coordination.sqlite3", "/workspace/two")
        self.addCleanup(other.close)
        self.save()
        self.assertNotEqual(self.service.state_dir, other.state_dir)
        self.assertEqual(other.snapshot()["status"]["state"], "not_loaded")
        for days in (True, 0, 31, "30", None):
            with self.assertRaises(CoordinationError):
                self.service.snapshot(days)

    def test_malformed_derived_files_preserve_last_valid_measurements(self):
        self.save()
        self.assertEqual(self.service.snapshot(7)["current"]["calls"], 12)
        for malformed in ({"7": "invalid"}, {"7": {"current": []}}, {"7": {"tools": [None]}}):
            (self.service.state_dir / "snapshot.json").write_text(json.dumps({
                "schema_version": 1, "periods": malformed}))
            (self.service.state_dir / "progress.json").write_text(json.dumps({"state": [], "progress": []}))
            self.now += 2
            self.assertEqual(self.service.snapshot(7)["current"]["calls"], 12)
        (self.service.state_dir / "progress.json").write_text(json.dumps({"state": []}))
        self.now += 2
        self.assertEqual(self.service.snapshot()["status"]["state"], "error")

    def test_refresh_coalesces_and_snapshot_does_not_wait_for_worker(self):
        entered, release = threading.Event(), threading.Event()
        def run():
            entered.set()
            release.wait(2)
            with self.service.lock:
                self.service._running = False
        with patch.object(self.service, "_run", side_effect=run) as worker:
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(lambda _: self.service.refresh(), range(20)))
            self.assertTrue(entered.wait(1))
            started = time.monotonic()
            self.assertEqual(self.service.snapshot()["status"]["state"], "running")
            self.assertLess(time.monotonic() - started, .1)
            self.assertEqual(worker.call_count, 1)
            release.set()
            self.service.thread.join(2)
            self.service.refresh()
            self.assertEqual(worker.call_count, 1)

    def test_worker_launch_is_separate_bounded_and_never_downloads_dependencies(self):
        process = Mock(returncode=0)
        process.poll.return_value = 0
        self.popen.return_value = process
        self.service.refresh()
        self.service.thread.join(2)
        self.assertFalse(self.service.thread.is_alive())
        command = self.popen.call_args.args[0]
        self.assertIn("agent_coord.coordination_metrics_worker", command)
        self.assertIn("--max-seconds", command)
        self.assertIn("--max-bytes", command)
        if sys.platform == "darwin" and Path("/usr/sbin/taskpolicy").is_file():
            self.assertEqual(command[:4], ["/usr/sbin/taskpolicy", "-b", "-d", "throttle"])
        self.assertNotIn("pip", command)
        self.assertNotIn("uv", command)
        self.assertTrue(self.popen.call_args.kwargs["start_new_session"])
        self.assertEqual(self.popen.call_args.kwargs["stdout"], subprocess.DEVNULL)
        self.assertIn("TIKTOKEN_CACHE_DIR", self.popen.call_args.kwargs["env"])

    def test_failure_is_sanitized_and_retains_prior_data(self):
        self.save()
        self.popen.side_effect = OSError("secret path credentials")
        self.service.refresh()
        self.service.thread.join(2)
        result = self.service.snapshot(7)
        self.assertEqual(result["current"]["calls"], 12)
        self.assertEqual(result["status"]["state"], "error")
        self.assertNotIn("secret", json.dumps(result))

    def test_stale_running_marker_is_not_a_permanent_spinner(self):
        self.save()
        (self.service.state_dir / "progress.json").write_text(json.dumps({"state": "running"}))
        result = self.service.snapshot()
        self.assertEqual(result["status"]["state"], "partial")
        self.assertFalse(result["status"]["running"])


class MetricsHTTPTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = CoordinationStore(Path(temp.name) / "state.sqlite3")
        self.sessions = SimpleNamespace(store=self.store, cwd=temp.name, admission=AdmissionGate(), inventory_changed=Mock())
        self.metrics = CoordinationMetrics(self.store.database_path, self.sessions.cwd)
        self.addCleanup(self.metrics.close)
        self.refresh = patch.object(self.metrics, "refresh", return_value={"status": {"state": "running"}})
        self.refresh_mock = self.refresh.start()
        self.addCleanup(self.refresh.stop)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(
            self.store, None, None, self.sessions, "test-csrf", coordination_metrics=self.metrics))
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self.stop)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(2)

    def request(self, path, body=None, headers=None):
        headers = {"Content-Type": "application/json", "X-Agent-Coord-Token": "test-csrf", **(headers or {})}
        request = urllib.request.Request(self.url + path, data=json.dumps(body).encode() if body is not None else None, headers=headers)
        try:
            response = urllib.request.urlopen(request, timeout=2)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, response.headers, response.read().decode()

    def test_metrics_reads_are_passive_and_query_is_strict(self):
        code, _, body = self.request("/api/browser/coordination-metrics?days=7")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["period"]["days"], 7)
        self.refresh_mock.assert_not_called()
        for query in ("days=1", "days=7&days=30", "days=bad", "cwd=/private", "days="):
            self.assertEqual(self.request("/api/browser/coordination-metrics?" + query)[0], 400)

    def test_refresh_requires_csrf_and_cannot_choose_paths_or_interpreter(self):
        path = "/api/browser/coordination-metrics/refresh"
        self.assertEqual(self.request(path, {}, {"X-Agent-Coord-Token": ""})[0], 403)
        for body in ({"path": "/private"}, {"python": "/bin/sh"}, {"days": 7}):
            self.assertEqual(self.request(path, body)[0], 400)
        self.refresh_mock.assert_not_called()
        self.assertEqual(self.request(path, {})[0], 202)
        self.refresh_mock.assert_called_once_with()
        self.sessions.inventory_changed.assert_not_called()

    def test_dashboard_framing_and_assets(self):
        assets = Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts/agent_coord/web"
        if not (assets / "coordination-metrics.html").is_file():
            self.skipTest("Dashboard component is not present yet")
        for path in ("/usage/coordination", "/coordination-metrics.js", "/coordination-metrics.css"):
            self.assertEqual(self.request(path)[0], 200)
        self.assertEqual(self.request("/usage/coordination")[1]["X-Frame-Options"], "DENY")
        self.assertEqual(self.request("/usage/coordination?embedded=1")[1]["X-Frame-Options"], "SAMEORIGIN")
        self.assertEqual(self.request("/usage/coordination", headers={"Host": "attacker.example"})[0], 403)


if __name__ == "__main__":
    unittest.main()
