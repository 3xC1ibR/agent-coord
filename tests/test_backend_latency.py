from __future__ import annotations

from contextlib import ExitStack, redirect_stdout
import copy
from http.server import ThreadingHTTPServer
import importlib.util
import io
import json
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("measure_backend_latency", ROOT / "scripts/measure_backend_latency.py")
latency = importlib.util.module_from_spec(spec)
spec.loader.exec_module(latency)


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class BackendLatencyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def healthy_metrics(self, pending=0):
        return {"admission_timeouts": 0, "history": {"failures": 0},
                "search": {"substring_index": {"supported": True, "state": "complete",
                                               "remaining_sources": 0, "failures": 0,
                                               "backfill_running": False, "backfill_completed": True}},
                "persistence": {"retries": 0, "failure_count": 0, "pending": pending}}

    def test_normalized_metadata_read_does_not_load_saved_transcript(self):
        provider = latency.NormalizedSyntheticProvider(Mock(), lambda event: None)
        self.addCleanup(provider.close)
        with patch.object(provider, "_history", side_effect=AssertionError("transcript load")):
            self.assertEqual(provider.request("thread/resume", {"threadId": "one", "excludeTurns": True})
                             ["thread"]["turns"], [])
            self.assertEqual(provider.request("thread/read", {"threadId": "one", "includeTurns": False})
                             ["thread"]["status"], {"type": "idle"})
        mirror = {"id": "one", "turns": [{"id": "saved", "status": "completed", "items": [{"text": "archive"}]}]}
        provider.threads["one"] = {"turns": [{"id": "current", "status": "inProgress", "items": [{"text": "live"}]}]}
        result = provider.reconcile_history("one", copy.deepcopy(mirror))
        self.assertEqual([turn["id"] for turn in result["turns"]], ["saved", "current"])
        result["turns"][-1]["items"][0]["text"] = "independent"
        self.assertEqual(provider.threads["one"]["turns"][-1]["items"][0]["text"], "live")

    def passing_report(self):
        return {"samples": {name: [1.0] for name in latency.BUDGETS},
                "children_reaped": True,
                "database_metrics": self.healthy_metrics(), "drain_completed": True,
                "requested_agents": 0, "expected_stream_threads": [], "delta_rate": 6,
                "workload_before": {"observed_monotonic": 0.0, "threads": {}},
                "workload_after": {"observed_monotonic": 1.0, "threads": {}}}

    def save_report(self, report):
        artifact = self.root / "measurement.json"
        workload = self.root / "workload"
        workload.mkdir(exist_ok=True)
        (workload / "state.sqlite3").write_bytes(b"disposable database")
        (workload / "state.sqlite3-wal").write_bytes(b"disposable WAL")
        (workload / ".state.sqlite3.runtime").mkdir(exist_ok=True)
        (workload / "server.log").write_text("preserved diagnostic")
        with redirect_stdout(io.StringIO()):
            latency.finish_report(report, workload, str(artifact))
        saved = json.loads(artifact.read_text())
        if report.get("children_reaped"):
            self.assertFalse(workload.exists())
            self.assertFalse(saved["database_retained"])
            self.assertEqual(Path(saved["server_log"]).read_text(), "preserved diagnostic")
        return saved

    def process(self):
        process = Mock()
        process.poll.return_value = None
        process.wait.return_value = 0
        process.stdout = io.StringIO(json.dumps({"port": 12345}) + "\n")
        return process

    def test_complete_healthy_measurement_passes(self):
        saved = self.save_report(self.passing_report())
        self.assertEqual(saved["budget_failures"], [])
        self.assertTrue(saved["artifacts_retained"])

    def test_database_with_unreaped_child_is_retained_and_fails_acceptance(self):
        report = self.passing_report()
        report["children_reaped"] = False
        saved = self.save_report(report)
        self.assertTrue(saved["database_retained"])
        self.assertTrue((self.root / "workload/state.sqlite3").exists())
        self.assertTrue(any("termination was not confirmed" in error for error in saved["budget_failures"]))

    def test_default_report_preserves_only_small_diagnostics(self):
        workload = self.root / "workload"
        workload.mkdir()
        (workload / "state.sqlite3").write_bytes(b"disposable database")
        (workload / "server.log").write_text("preserved diagnostic")
        report = self.passing_report()
        report["samples"]["errors"] = ["KeyboardInterrupt"]
        with redirect_stdout(io.StringIO()):
            latency.finish_report(report, workload, None)
        self.assertEqual({path.name for path in workload.iterdir()}, {"report.json", "server.log"})
        self.assertEqual(json.loads((workload / "report.json").read_text())["error_count"], 1)

    def test_workload_endpoint_uses_real_http_handler_and_authorization(self):
        # Exercise the real _json/_send path: it requires HTTPStatus, not int.
        with patch.object(sys, "path", [str(latency.SCRIPTS), *sys.path]):
            from agent_coord.store import CoordinationStore
            from agent_coord.ui import _handler
        store = CoordinationStore(self.root / "state.sqlite3")
        remote = Mock(origin="https://measurement.example")
        remote.authenticated.side_effect = lambda cookie: cookie == "session=valid"
        handler = latency.measurement_handler(_handler(store, None, str(self.root),
            csrf_token="measurement-token", remote_access=remote))
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        worker.start()
        url = f"http://127.0.0.1:{server.server_address[1]}/api/measurement/workload"
        try:
            for headers in ({"X-Agent-Coord-Token": "measurement-token"},
                            {"Host": "measurement.example", "Cookie": "session=valid"}):
                with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=2) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.headers["Content-Type"], "application/json; charset=utf-8")
                    body = json.load(response)
                    self.assertIsInstance(body["observed_monotonic"], float)
                    self.assertIsInstance(body["threads"], dict)
            for headers, status in (({"Host": "measurement.example", "Cookie": "session=invalid"}, 401),
                                    ({"Host": "untrusted.example"}, 403)):
                with self.assertRaises(urllib.error.HTTPError) as rejected:
                    urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=2)
                self.assertEqual(rejected.exception.code, status)
                rejected.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            worker.join(2)

    def test_metrics_loss_cannot_reuse_a_healthy_sample_as_a_completed_drain(self):
        client = Mock()
        client.call.side_effect = [(0, self.healthy_metrics(), 0), TimeoutError("metrics timed out")]
        report, clock = self.passing_report(), Clock()
        with patch.object(latency.time, "monotonic", clock.monotonic), patch.object(latency.time, "sleep", clock.sleep):
            latency.drain_workload(client, [], report, timeout=2, quiet_period=1)
        saved = self.save_report(report)
        self.assertFalse(saved["drain_completed"])
        self.assertTrue(saved["artifacts_retained"])
        self.assertTrue(any("metrics timed out" in error for error in saved["budget_failures"]))

    def test_drain_requires_a_continuous_quiet_period(self):
        clock, client, report = Clock(), Mock(), {}
        observations = iter([0, 1, 0])
        client.call.side_effect = lambda *args, **kwargs: (0, self.healthy_metrics(next(observations, 0)), 0)
        with patch.object(latency.time, "monotonic", clock.monotonic), patch.object(latency.time, "sleep", clock.sleep):
            latency.drain_workload(client, [], report, timeout=2, quiet_period=1)
        self.assertTrue(report["drain_completed"])
        self.assertGreaterEqual(clock.now, 1.2)

    def test_late_healthy_sample_does_not_satisfy_drain_deadline(self):
        clock, client, report = Clock(), Mock(), {}
        def delayed_response(*args, **kwargs):
            clock.now += 2
            return 0, self.healthy_metrics(), 0
        client.call.side_effect = delayed_response
        with patch.object(latency.time, "monotonic", clock.monotonic):
            latency.drain_workload(client, [], report, timeout=1, quiet_period=0)
        self.assertFalse(report["drain_completed"])
        self.assertIn("Persistence drain deadline exceeded", report["drain_errors"])

    def test_latency_pass_does_not_hide_database_failure_or_missing_counters(self):
        unhealthy = [None, {}, {"persistence": []}, {"history": None}]
        for field in ("retries", "failure_count", "optional_failure_count", "pending"):
            metrics = self.healthy_metrics()
            metrics["persistence"][field] = 1
            unhealthy.append(metrics)
        for search in ({"documents": {"failures": 1}}, {"documents": None}, []):
            metrics = self.healthy_metrics()
            metrics["search"] = search
            unhealthy.append(metrics)
        for field in ("admission_timeouts", "history"):
            metrics = self.healthy_metrics()
            metrics[field] = 1 if field == "admission_timeouts" else {"failures": 1}
            unhealthy.append(metrics)
        for metrics in unhealthy:
            with self.subTest(metrics=metrics):
                report = self.passing_report()
                report["database_metrics"] = metrics
                saved = self.save_report(report)
                self.assertTrue(saved["budget_failures"])
                self.assertTrue(saved["artifacts_retained"])

    def test_incomplete_or_unavailable_substring_coverage_fails_acceptance(self):
        cases = [None, {}, {"supported": False}, {"state": "partial"},
                 {"state": "failed"}, {"remaining_sources": 1}, {"failures": 1},
                 {"backfill_running": True}, {"backfill_completed": False}]
        for change in cases:
            with self.subTest(change=change):
                metrics = self.healthy_metrics()
                if change:
                    metrics["search"]["substring_index"].update(change)
                else:
                    metrics["search"]["substring_index"] = change
                self.assertTrue(latency.check_database_health(metrics))

    def test_hook_timeout_and_drain_exception_do_not_abandon_backend(self):
        server, hook, client, report = self.process(), Mock(), Mock(), self.passing_report()
        hook.poll.return_value = None
        hook.wait.side_effect = [subprocess.TimeoutExpired("hook", 5), 0]
        client.call.return_value = (0, self.healthy_metrics(), 0)
        with patch.object(latency, "drain_workload", side_effect=RuntimeError("drain interrupted")):
            latency.cleanup_workload(server, [hook], client, ["synthetic"], report, quiet_period=2)
        hook.kill.assert_called_once()
        self.assertEqual(hook.wait.call_count, 2)
        server.terminate.assert_called_once()
        server.wait.assert_called_once_with(timeout=20)
        saved = self.save_report(report)
        self.assertTrue(any("Hook writer" in error for error in saved["budget_failures"]))
        self.assertTrue(any("drain interrupted" in error for error in saved["budget_failures"]))
        server.stdout.close()

    def test_failed_under_load_diagnostics_are_not_hidden_by_successful_drain(self):
        server, client, report = self.process(), Mock(), self.passing_report()
        client.call.side_effect = TimeoutError("under-load diagnostics timed out")
        with patch.object(latency, "drain_workload"):
            latency.cleanup_workload(server, [], client, [], report, quiet_period=2)
        saved = self.save_report(report)
        self.assertTrue(any("under-load diagnostics timed out" in error for error in saved["budget_failures"]))
        server.stdout.close()

    def test_owned_child_that_ignores_termination_is_killed_and_reaped(self):
        process = subprocess.Popen([sys.executable, "-c",
            "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print('ready',flush=True); time.sleep(60)"],
            stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), "ready")
            report = {}
            latency.stop_process(process, "Test child", report, timeout=.05)
            self.assertIsNotNone(process.poll())
            self.assertTrue(report["cleanup_errors"])
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            process.stdout.close()

    def test_steering_restored_active_turn_starts_exactly_one_stream(self):
        delta_started, release = threading.Event(), threading.Event()
        events = []
        def receive(event):
            events.append(event)
            if event["method"] == "item/agentMessage/delta":
                delta_started.set()
                release.wait(2)
        provider = latency.SyntheticProvider(None, receive)
        provider.delta_rate = 100
        historic = {"id": "previous-item", "type": "agentMessage", "text": "Keep this history"}
        turn = {"id": "saved-turn", "status": "inProgress", "items": [copy.deepcopy(historic)]}
        provider.threads["saved-thread"] = {"id": "saved-thread", "turns": [turn]}
        barrier = threading.Barrier(6)
        errors = []
        def steer():
            try:
                barrier.wait(2)
                provider.request("turn/steer", {"threadId": "saved-thread", "expectedTurnId": "saved-turn",
                    "input": [{"type": "text", "text": "[synthetic-load] continue"}]})
            except Exception as error:
                errors.append(error)
        callers = [threading.Thread(target=steer) for _ in range(6)]
        try:
            for caller in callers:
                caller.start()
            for caller in callers:
                caller.join(2)
            self.assertEqual(errors, [])
            self.assertTrue(delta_started.wait(2))
            snapshot = provider.workload_snapshot()["threads"]["saved-thread"]
            self.assertEqual(snapshot["active_streams"], 1)
            self.assertEqual(snapshot["streams_started"], 1)
            self.assertEqual(turn["items"][0], historic)
            provider.request("turn/interrupt", {"threadId": "saved-thread"})
        finally:
            provider.close()
            release.set()
            for caller in callers:
                caller.join(2)
            with provider.stream_lock:
                workers = list(provider.stream_workers.values())
            for worker in workers:
                worker.join(2)
        observed = provider.workload_snapshot()["threads"]["saved-thread"]
        self.assertEqual(observed["active_streams"], 0)
        self.assertEqual(observed["streams_completed"], 1)
        self.assertGreater(observed["delta_events"], 0)
        self.assertEqual(observed["stream_errors"], 0)

    def test_stream_cadence_includes_callback_processing_time(self):
        clock = Clock()
        def receive(event):
            if event["method"] == "item/agentMessage/delta":
                clock.sleep(.04)
        provider = latency.SyntheticProvider(None, receive)
        provider.delta_rate = 10
        provider.stream_statistics["thread"] = {"delta_events": 0}
        turn = {"id": "turn", "status": "inProgress", "items": []}
        try:
            with patch.object(latency.time, "monotonic", clock.monotonic), patch.object(latency.time, "sleep", clock.sleep):
                provider._stream_events("thread", turn, 1)
            self.assertEqual(provider.stream_statistics["thread"]["delta_events"], 10)
        finally:
            provider.close()

    def test_nominal_agent_count_does_not_hide_missing_or_slow_streams(self):
        report = self.passing_report()
        report.update(requested_agents=2, expected_stream_threads=["one", "two"])
        report["workload_before"]["threads"] = {key: dict(active_streams=1, delta_events=10, stream_errors=0)
                                               for key in ("one", "two")}
        report["workload_after"]["threads"] = {"one": dict(active_streams=1, delta_events=16, stream_errors=0),
                                                "two": dict(active_streams=0, delta_events=11, stream_errors=0)}
        saved = self.save_report(report)
        self.assertEqual(saved["stream_rates_hz"], {"one": 6.0, "two": 1.0})
        self.assertTrue(any("expected one worker" in failure for failure in saved["budget_failures"]))
        self.assertTrue(any("events/s is below" in failure for failure in saved["budget_failures"]))

    def main_context(self, *, client=None, client_error=None, spawn_error=None):
        stack = ExitStack()
        self.addCleanup(stack.close)
        workload = self.root / "workload"
        workload.mkdir()
        (workload / "state.sqlite3").write_bytes(b"disposable database")
        output = self.root / "report.json"
        server = self.process()
        self.addCleanup(server.stdout.close)
        stack.enter_context(patch.object(latency.sys, "argv", ["measure_backend_latency.py", "--snapshot", "unused.sqlite3",
            "--output", str(output), "--windows", "0", "--sends", "0", "--reads", "0", "--hook-rate", "0", "--warmup", "0", "--check"]))
        stack.enter_context(patch.object(latency.tempfile, "mkdtemp", return_value=str(workload)))
        stack.enter_context(patch.object(latency.subprocess, "run"))
        stack.enter_context(patch.object(latency, "pick_threads", return_value=["target", "synthetic"]))
        popen = stack.enter_context(patch.object(latency.subprocess, "Popen", return_value=server, side_effect=spawn_error))
        stack.enter_context(patch.object(latency, "Client", return_value=client, side_effect=client_error))
        stack.enter_context(patch.object(latency, "EventWatcher"))
        stack.enter_context(patch.object(latency, "cpu_seconds", return_value=0))
        stack.enter_context(redirect_stdout(io.StringIO()))
        return server, popen, output

    def test_client_initialization_failure_still_reaps_backend_and_saves_report(self):
        server, popen, output = self.main_context(client_error=RuntimeError("configuration failed"))
        with self.assertRaisesRegex(RuntimeError, "configuration failed"):
            latency.main()
        server.terminate.assert_called_once()
        server.wait.assert_called_once_with(timeout=20)
        self.assertTrue(server.stdout.closed)
        self.assertTrue(popen.call_args.kwargs["stderr"].closed)
        saved = json.loads(output.read_text())
        self.assertEqual(saved["measurement_error"], "RuntimeError: configuration failed")
        self.assertTrue(saved["artifacts_retained"])
        self.assertFalse(saved["database_retained"])
        self.assertFalse((self.root / "workload").exists())

    def test_backend_spawn_failure_still_closes_log_and_saves_report(self):
        _, popen, output = self.main_context(spawn_error=OSError("spawn failed"))
        with self.assertRaisesRegex(OSError, "spawn failed"):
            latency.main()
        self.assertTrue(popen.call_args.kwargs["stderr"].closed)
        saved = json.loads(output.read_text())
        self.assertEqual(saved["measurement_error"], "OSError: spawn failed")
        self.assertTrue(saved["artifacts_retained"])
        self.assertFalse(saved["database_retained"])
        self.assertFalse((self.root / "workload").exists())

    def test_interruption_still_reaps_children_and_removes_database(self):
        server, _, output = self.main_context(client_error=KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            latency.main()
        server.wait.assert_called_once_with(timeout=20)
        saved = json.loads(output.read_text())
        self.assertFalse(saved["database_retained"])
        self.assertFalse((self.root / "workload").exists())
        self.assertIn("KeyboardInterrupt", saved["measurement_error"])

    def test_termination_routes_through_cleanup_and_restores_signal_handler(self):
        previous = signal.getsignal(signal.SIGTERM)
        def terminate(*args):
            handler = signal.getsignal(signal.SIGTERM)
            self.assertIs(handler, latency.interrupt_measurement)
            handler(signal.SIGTERM, None)
        server, _, output = self.main_context(client_error=terminate)
        with self.assertRaisesRegex(KeyboardInterrupt, "terminated by signal"):
            latency.main()
        self.assertIs(signal.getsignal(signal.SIGTERM), previous)
        server.wait.assert_called_once_with(timeout=20)
        self.assertFalse(json.loads(output.read_text())["database_retained"])
        self.assertFalse((self.root / "workload").exists())

    def test_unexpected_drain_failure_still_saves_report_and_reaps_backend(self):
        client = Mock()
        client.call.return_value = (0, self.healthy_metrics(), 0)
        server, _, output = self.main_context(client=client)
        with patch.object(latency, "drain_workload", side_effect=ValueError("invalid diagnostics")):
            with self.assertRaises(SystemExit) as failure:
                latency.main()
        self.assertEqual(failure.exception.code, 1)
        server.terminate.assert_called_once()
        server.wait.assert_called_once_with(timeout=20)
        saved = json.loads(output.read_text())
        self.assertTrue(any("invalid diagnostics" in error for error in saved["budget_failures"]))
        self.assertTrue(saved["artifacts_retained"])


if __name__ == "__main__":
    unittest.main()
