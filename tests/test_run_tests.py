from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

RUNNER = Path(__file__).resolve().parents[1] / "scripts/run_tests.py"
spec = importlib.util.spec_from_file_location("run_tests", RUNNER)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class TestRunnerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.tests = self.root / "cases"
        self.tests.mkdir()

    def run_cases(self, jobs=2, extra=(), expected=0, interpreter=()):
        report = self.root / f"report-{jobs}.json"
        command = [sys.executable, *interpreter, "-W", "error::ResourceWarning", str(RUNNER),
                   "--jobs", str(jobs), "--start-directory", str(self.tests),
                   "--report", str(report), *extra]
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return json.loads(report.read_text())

    def write(self, filename, body):
        (self.tests / filename).write_text(body)

    def test_serial_and_parallel_preserve_imported_duplicates_fixtures_and_subtests(self):
        self.write("test_a.py", '''import unittest
class Cases(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.fixture = ['ready']
    def test_subtests(self):
        self.assertEqual(self.fixture, ['ready'])
        for i in range(3):
            with self.subTest(i=i): self.assertLess(i, 3)
    @unittest.skip('same reason')
    def test_skipped(self): pass
    @unittest.expectedFailure
    def test_expected(self): self.fail('expected')
''')
        self.write("test_b.py", "from test_a import Cases\n")
        serial = self.run_cases(1)
        parallel = self.run_cases(2, ("--compare", str(self.root / "report-1.json")))
        self.assertEqual(len(serial["discovered"]), 6)
        self.assertEqual(len(set(serial["discovered"])), 3)
        self.assertEqual(runner.differences(serial, parallel), [])
        self.assertEqual(sum(e[1] == "subtest_pass" for e in parallel["events"]), 6)

    def test_failed_subtest_and_unexpected_success_fail_run(self):
        self.write("test_bad.py", '''import unittest
class Cases(unittest.TestCase):
    def test_subtest(self):
        with self.subTest(value=1): self.fail('bad subtest')
    @unittest.expectedFailure
    def test_unexpected(self): pass
''')
        report = self.run_cases(expected=1)
        self.assertFalse(report["successful"])
        self.assertEqual({e[1] for e in report["events"]},
                         {"subtest_failure", "unexpected_success"})

    def test_worker_exit_and_timeout_cannot_pass(self):
        self.write("test_exit.py", '''import unittest, os
class Cases(unittest.TestCase):
    def test_exit(self): os._exit(7)
''')
        report = self.run_cases(expected=1)
        self.assertTrue(any("exited 7" in e for e in report["integrity_errors"]))
        self.write("test_exit.py", '''import unittest, time
class Cases(unittest.TestCase):
    def test_wait(self): time.sleep(10)
''')
        report = self.run_cases(extra=("--timeout", "0.5"), expected=1)
        self.assertTrue(any("timed out" in e for e in report["integrity_errors"]))

    def test_import_error_and_empty_discovery_cannot_pass(self):
        self.write("test_bad.py", "raise RuntimeError('broken import')\n")
        report = self.run_cases(expected=1)
        self.assertFalse(report["successful"])
        self.assertTrue(any(e[1] == "error" for e in report["events"]))
        (self.tests / "test_bad.py").unlink()
        report = self.run_cases(expected=1)
        self.assertIn("no tests discovered", report["integrity_errors"])

    def test_source_changes_and_resource_warnings_cannot_pass(self):
        self.write("test_change.py", '''import unittest
from pathlib import Path
class Cases(unittest.TestCase):
    def test_change(self):
        path = Path(__file__)
        path.write_text(path.read_text() + '# changed during execution\\n')
''')
        report = self.run_cases(expected=1)
        self.assertIn("source files changed during run", report["integrity_errors"])
        self.write("test_change.py", '''import unittest, warnings
class Cases(unittest.TestCase):
    def test_warning(self): warnings.warn('must fail', ResourceWarning)
''')
        report = self.run_cases(expected=1)
        self.assertTrue(any(e[1] == "error" and "ResourceWarning" in e[2]
                            for e in report["events"]))

    def test_interpreter_flags_match_in_workers(self):
        self.write("test_flags.py", '''import unittest, sys
class Cases(unittest.TestCase):
    def test_flags(self):
        self.assertFalse(__debug__)
        self.assertTrue(sys.flags.dont_write_bytecode)
        self.assertEqual(sys._xoptions['utf8'], '1')
''')
        flags = ("-O", "-B", "-X", "utf8=1")
        serial = self.run_cases(1, interpreter=flags)
        parallel = self.run_cases(2, interpreter=flags,
                                  extra=("--compare", str(self.root / "report-1.json")))
        self.assertEqual(runner.differences(serial, parallel), [])

    def test_environment_capture_is_immutable_when_tests_change_import_paths(self):
        self.write("test_paths.py", '''import unittest, sys
class Cases(unittest.TestCase):
    def test_path(self): sys.path.insert(0, '/synthetic-test-only-path')
''')
        serial = self.run_cases(1)
        parallel = self.run_cases(2, ("--compare", str(self.root / "report-1.json")))
        self.assertNotIn('/synthetic-test-only-path', serial['environment']['sys_path'])
        self.assertEqual(runner.differences(serial, parallel), [])

    def test_discovery_time_and_source_mutation_are_in_integrity_window(self):
        self.write("test_import.py", '''import unittest, time
time.sleep(0.25)
class Cases(unittest.TestCase):
    def test_ok(self): pass
''')
        report = self.run_cases(1)
        self.assertGreaterEqual(report["wall_seconds"], 0.25)
        self.write("test_import.py", '''import unittest
from pathlib import Path
path = Path(__file__)
path.write_text(path.read_text() + '# import changed source\\n')
class Cases(unittest.TestCase):
    def test_ok(self): pass
''')
        report = self.run_cases(1, expected=1)
        self.assertIn("source files changed during run", report["integrity_errors"])

    def test_sigterm_cleans_up_running_worker(self):
        marker = self.root / "worker.pid"
        self.write("test_wait.py", f'''import unittest, os, time
from pathlib import Path
class Cases(unittest.TestCase):
    def test_wait(self):
        Path({str(marker)!r}).write_text(str(os.getpid()))
        time.sleep(30)
''')
        command = [sys.executable, "-W", "error::ResourceWarning", str(RUNNER),
                   "--jobs", "2", "--start-directory", str(self.tests),
                   "--report", str(self.root / "terminated.json")]
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        child_pid = None
        try:
            deadline = time.monotonic() + 10
            while not marker.exists() and proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(marker.exists(), "worker did not reach test")
            child_pid = int(marker.read_text())
            proc.send_signal(signal.SIGTERM)
            proc.communicate(timeout=10)
            self.assertNotEqual(proc.returncode, 0)
            with self.assertRaises(ProcessLookupError):
                os.kill(child_pid, 0)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.communicate()
            if child_pid is not None:
                try:
                    os.killpg(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_comparison_rejects_changed_skips_counts_environment_and_sources(self):
        base = {"environment": {}, "sources": {}, "successful": True,
                "discovered": ["a", "a"], "started": ["a", "a"],
                "events": [["a", "skip", "reason"], ["a", "skip", "reason"]]}
        for key, value in [("environment", {"version": "other"}),
                           ("sources", {"test.py": "changed"}),
                           ("discovered", ["a"]), ("started", ["a"]),
                           ("events", [["a", "skip", "other"], ["a", "skip", "reason"]])]:
            with self.subTest(key=key):
                changed = {**base, key: value}
                self.assertTrue(any(key in error for error in runner.differences(base, changed)))


if __name__ == "__main__":
    unittest.main()
