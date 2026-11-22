"""Stale discovery must not bypass bounded runtime ownership admission."""
import os
import json
import signal
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

from agent_coord import desktop_runtime as runtime


class BootstrapRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = Path(self.temporary.name) / "state.sqlite3"
        self.directory = runtime.runtime_directory(self.database)
        self.payload = self.directory / "payloads" / "test-payload"
        self.payload.mkdir(parents=True)
        runtime.atomic_json(self.payload / "backend.json", {"python": sys.executable})
        runtime.atomic_json(self.directory / "discovery.json", {
            "database": str(self.database.resolve()), "instance": "stopped-instance",
            "control_url": "http://127.0.0.1:1/", "control_token": "isolated-test-token"})

    def test_stale_discovery_waits_for_finite_owner_before_launch(self):
        owner = runtime.lock_file(self.directory / "owner.lock")
        release = threading.Timer(.15, os.close, args=(owner,))
        release.start()

        def launch(*args, **kwargs):
            self.assertFalse(runtime.owner_available(self.directory), "Launch lost its exclusive reservation")
            raise OSError("Reached reserved launch boundary")

        try:
            with patch.object(runtime, "stage_payload", return_value=self.payload), \
                    patch.object(runtime, "control", side_effect=ConnectionRefusedError("Stopped endpoint")), \
                    patch.object(runtime.subprocess, "Popen", side_effect=launch) as spawned:
                with self.assertRaisesRegex(OSError, "Reached reserved launch boundary"):
                    runtime.bootstrap(self.database, self.payload)
            spawned.assert_called_once()
            self.assertTrue(runtime.owner_available(self.directory))
        finally:
            release.join()

    def test_stale_discovery_never_launches_while_owner_stays_busy(self):
        owner = runtime.lock_file(self.directory / "owner.lock")
        try:
            with patch.object(runtime, "stage_payload", return_value=self.payload), \
                    patch.object(runtime, "control", side_effect=ConnectionRefusedError("Unreachable endpoint")), \
                    patch.object(runtime.time, "monotonic", side_effect=[0, 21]), \
                    patch.object(runtime.subprocess, "Popen") as spawned:
                with self.assertRaisesRegex(RuntimeError, "owner is unavailable"):
                    runtime.bootstrap(self.database, self.payload)
            spawned.assert_not_called()
            self.assertFalse(runtime.owner_available(self.directory))
        finally:
            os.close(owner)


class StopFailureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = Path(self.temporary.name) / "state.sqlite3"
        self.directory = runtime.runtime_directory(self.database)
        self.instance, self.transport = uuid.uuid4().hex, uuid.uuid4().hex
        (self.directory / "provider-transport").mkdir(parents=True)
        runtime.atomic_json(self.directory / "provider-transport/owner.json", {"instance": self.transport})
        self.failure = {"runtime_instance": self.instance, "transport_instance": self.transport,
                        "database": str(self.database.resolve()), "error": "Retained ownership is uncertain"}
        self.path = runtime.stop_failure_path(self.directory, self.instance)
        self.path.parent.mkdir()
        runtime.atomic_json(self.path, self.failure)

    def test_matching_failure_reports_error_without_creating_stop_proof(self):
        self.assertIn(self.failure["error"], runtime.retained_stop_failure(self.directory, self.database, self.instance))
        self.assertFalse((self.directory / "stop-proof.json").exists())

    def test_mismatched_runtime_transport_or_database_cannot_supply_failure(self):
        for field in ("runtime_instance", "transport_instance", "database"):
            with self.subTest(field=field):
                runtime.atomic_json(self.path, {**self.failure, field: "different"})
                self.assertIsNone(runtime.retained_stop_failure(self.directory, self.database, self.instance))

    def test_held_runtime_or_transport_lock_defers_failure_report(self):
        for name in ("owner.lock", "provider-transport/owner.lock"):
            with self.subTest(lock=name):
                guard = runtime.lock_file(self.directory / name)
                try:
                    self.assertIsNone(runtime.retained_stop_failure(self.directory, self.database, self.instance))
                finally:
                    os.close(guard)
        self.assertIn(self.failure["error"], runtime.retained_stop_failure(self.directory, self.database, self.instance))


class RuntimeRestartTests(unittest.TestCase):
    def setUp(self):
        from test_desktop_runtime import RuntimeTests
        self.runtime = RuntimeTests("test_stop_during_trial_rolls_back_without_restarting")
        self.runtime.setUp()
        self.addCleanup(self.runtime.tearDown)
        self.runtime.env["RIBBON_FAKE_PERSIST"] = "1"

    def completed_message(self, status, thread, message):
        fixture = self.runtime
        before = len([call for call in fixture.calls() if call.get("method") == "turn/start"])
        fixture.api(status["url"], f"api/browser/sessions/{thread}/messages", {"message": message})
        fixture.wait(lambda: len([call for call in fixture.calls() if call.get("method") == "turn/start"]) == before + 1)
        fixture.wait(lambda: not fixture.api(status["url"], f"api/browser/sessions/{thread}")["activeTurn"])
        detail = fixture.api(status["url"], f"api/browser/sessions/{thread}")
        turn = detail["thread"]["turns"][-1]
        self.assertEqual(turn["status"], "completed")
        self.assertEqual(turn["items"][0]["content"][0]["text"], message)
        self.assertIn("output while desktop disconnected", json.dumps(detail))
        self.assertFalse(runtime.control(runtime.discover(fixture.database), "status")["transport"]["error"])

    def test_confirmed_stop_reopen_executes_new_provider_work(self):
        fixture = self.runtime
        first = fixture.launch()
        thread = fixture.api(first["url"], "api/browser/sessions", {"cwd": str(fixture.root)})["session"]["thread_id"]
        self.completed_message(first, thread, "before stop")
        record = runtime.discover(fixture.database)
        old_transport = runtime.control(record, "status")["transport"]["instance"]
        runtime.control(record, "stop")
        directory = runtime.runtime_directory(fixture.database)
        fixture.wait(lambda: runtime.owner_available(directory))
        second = fixture.launch()
        self.assertNotEqual(runtime.control(runtime.discover(fixture.database), "status")["transport"]["instance"], old_transport)
        self.completed_message(second, thread, "after reopen")
        calls = [call["params"]["input"][0]["text"] for call in fixture.calls() if call.get("method") == "turn/start"]
        self.assertEqual(calls, ["before stop", "after reopen"])

    def test_first_stop_after_owner_crash_reports_retained_failure_promptly(self):
        fixture = self.runtime
        first = fixture.launch()
        thread = fixture.api(first["url"], "api/browser/sessions", {"cwd": str(fixture.root)})["session"]["thread_id"]
        fixture.api(first["url"], f"api/browser/sessions/{thread}/messages", {"message": "held-work"})
        record = runtime.discover(fixture.database)
        fixture.wait(lambda: "active turns" in runtime.control(record, "status")["busy"])
        directory = runtime.runtime_directory(fixture.database)
        owner_path = directory / "provider-transport/owner.json"
        retained = owner_path.read_bytes()
        owner = json.loads(retained)
        command = subprocess.run(["ps", "-p", str(owner["pid"]), "-o", "command="],
                                 check=True, text=True, capture_output=True).stdout
        self.assertIn(str(directory / "provider-transport"), command)
        # This owner was launched by the private fixture, authenticated above;
        # no PID from installed discovery or the user's runtime is consulted.
        os.kill(owner["pid"], signal.SIGKILL)
        started = time.monotonic()
        try:
            result = subprocess.run([sys.executable, "-B", str(fixture.resources / "backend.py"),
                                     "--db", str(fixture.database), "--runtime-command", "stop"],
                                    env=fixture.env, text=True, capture_output=True, timeout=12)
        except subprocess.TimeoutExpired:
            self.fail("First Stop did not return its failure promptly:\n" + fixture.log()[-15000:])
        self.assertNotEqual(result.returncode, 0)
        self.assertLess(time.monotonic() - started, 8)
        self.assertIn("surviving providers cannot be verified", result.stderr)
        self.assertIn("Retained evidence was preserved", result.stderr)
        self.assertNotIn('"status": "stopped"', result.stdout)
        self.assertFalse((directory / "stop-proof.json").exists())
        self.assertEqual(owner_path.read_bytes(), retained)
        self.assertEqual(len([call for call in fixture.calls() if call.get("method") == "turn/start"]), 1)

    def test_cancelled_prepared_trial_exits_without_kill_delay_or_stale_discovery(self):
        fixture = self.runtime
        source = fixture.resources / "backend/agent_coord/desktop_runtime.py"
        source.write_text(source.read_text() + '''
_original_trial_serve = Runtime.serve
def prepared_trial_barrier(self):
    if self.commit_fd is not None:
        original_write = os.write
        def write_prepared(fd, data):
            if data == b"prepared\\n":
                (self.directory / "prepared-trial").touch()
                while not (self.directory / "release-prepared-trial").exists(): time.sleep(.01)
            return original_write(fd, data)
        os.write = write_prepared
    return _original_trial_serve(self)
Runtime.serve = prepared_trial_barrier
''')
        first, record, thread, staged, candidate = fixture.prepare_requested_update(message="held-work")
        runtime.control(record, "activate-update", request_id="cancel-prepared", payload=staged["pending"])
        directory = runtime.runtime_directory(fixture.database)
        fixture.wait(lambda: (directory / "prepared-trial").exists(), timeout=20)
        started = time.monotonic()
        with subprocess.Popen([sys.executable, "-B", str(candidate / "backend.py"), "--db", str(fixture.database),
                               "--runtime-command", "stop"], env=fixture.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as helper:
            try:
                fixture.wait(lambda: (directory / "update-cancellation.json").exists())
            finally:
                (directory / "release-prepared-trial").touch()
            stdout, stderr = helper.communicate(timeout=15)
        self.assertEqual(helper.returncode, 0, stderr)
        self.assertEqual(json.loads(stdout)["status"], "stopped")
        self.assertLess(time.monotonic() - started, 4.5)
        self.assertFalse((directory / "discovery.json").exists())
        self.assertTrue(runtime.owner_available(directory))
        second = fixture.launch()
        self.completed_message(second, thread, "new work after cancellation")


if __name__ == "__main__":
    unittest.main()
