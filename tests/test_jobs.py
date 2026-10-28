from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))

from agent_coord.jobs import (ManagedJobs, TERMINAL_STATES, DEFAULT_LOG_LIMIT_BYTES, TRUNCATION_MARKER, supervise,
                              _supervisor_lock, _touch_supervisor)
from agent_coord.store import CoordinationError, CoordinationStore
from agent_coord.event_subscriptions import EventSubscriptions


class ManagedJobTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = CoordinationStore(self.root / "state.sqlite3")
        for identity in ("owner", "other"):
            self.store.register(session_id=identity, client="codex", cwd=str(self.root))
        self.jobs = ManagedJobs(self.store)
        self.addCleanup(self.cancel_remaining)

    def cancel_remaining(self):
        for job in self.jobs.list("owner"):
            if not job["terminal"]:
                self.jobs.cancel("owner", job["id"])
                self.wait(job["id"])

    def command(self, code, **kwargs):
        return self.jobs.create("owner", [sys.executable, "-c", code], **kwargs)

    def wait(self, job_id, predicate=None):
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            job = self.jobs.get("owner", job_id)
            if predicate(job) if predicate else job["state"] in TERMINAL_STATES:
                return job
            time.sleep(0.03)
        self.fail(f"Job did not reach expected state: {job}")

    def test_detached_job_survives_launcher_exit_and_store_reconstruction(self):
        launcher = """import sys
from agent_coord.store import CoordinationStore
from agent_coord.jobs import ManagedJobs
jobs=ManagedJobs(CoordinationStore(sys.argv[1]))
job=jobs.create('owner',[sys.executable,'-c',\"import time; time.sleep(.3); print('survived')\"],job_id='detached')
jobs.launch('owner',job['id'])
"""
        environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts")}
        result = subprocess.run([sys.executable, "-W", "error::ResourceWarning", "-c", launcher,
                                 str(self.store.database_path)], capture_output=True, text=True, env=environment, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.jobs = ManagedJobs(CoordinationStore(self.store.database_path))
        self.assertEqual(self.wait("detached")["state"], "completed")
        self.assertIn("survived", self.jobs.logs("owner", "detached")["text"])

    def test_nonzero_and_missing_executable_record_terminal_failure(self):
        job = self.command("import sys; print('failure output'); sys.exit(7)")
        self.jobs.launch("owner", job["id"])
        self.assertEqual(self.wait(job["id"])["exit_code"], 7)
        self.assertIn("failure output", self.jobs.logs("owner", job["id"])["text"])
        missing = self.jobs.create("owner", [str(self.root / "missing-executable")])
        self.jobs.launch("owner", missing["id"])
        self.assertEqual(self.wait(missing["id"])["state"], "failed")

    def test_duplicate_concurrent_launch_runs_the_command_once(self):
        marker = self.root / "executions"
        job = self.command(f"from pathlib import Path; import time; p=Path({str(marker)!r}); p.open('a').write('once\\n'); time.sleep(.2)", job_id="repeat")
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.jobs.launch("owner", job["id"]), range(2)))
        self.assertEqual(sum(result["launched"] for result in results), 1)
        self.assertEqual(self.wait(job["id"])["state"], "completed")
        self.assertEqual(marker.read_text(), "once\n")
        self.assertFalse(self.jobs.launch("owner", job["id"])["launched"])

    def test_cancellation_of_queued_and_running_jobs_is_durable(self):
        queued = self.command("raise RuntimeError('must not run')")
        self.assertEqual(self.jobs.cancel("owner", queued["id"])["state"], "cancelled")
        self.assertFalse(self.jobs.launch("owner", queued["id"])["launched"])
        running = self.command("import time; time.sleep(30)")
        self.jobs.launch("owner", running["id"])
        self.wait(running["id"], lambda row: row["state"] == "running")
        self.jobs.cancel("owner", running["id"])
        self.assertEqual(self.wait(running["id"])["state"], "cancelled")

    def test_noisy_command_still_honors_timeout_and_bounds_disk_output(self):
        job = self.command("import os; data=b'x'*65536\nwhile True: os.write(1,data)", timeout=0.3)
        started = time.monotonic()
        self.jobs.launch("owner", job["id"])
        result = self.wait(job["id"])
        self.assertEqual(result["state"], "timed_out")
        self.assertLess(time.monotonic() - started, 3)
        self.assertLessEqual(Path(result["output_path"]).stat().st_size, DEFAULT_LOG_LIMIT_BYTES)
        logs = self.jobs.logs("owner", job["id"])
        self.assertGreater(logs["size"], 0)
        # The deadline need not allow enough output to fill the default cap.
        self.assertEqual(logs["gap"], logs["size"] > DEFAULT_LOG_LIMIT_BYTES)

    def test_output_exceeding_disk_limit_reports_truncation(self):
        # Force truncation by byte count, independently of machine throughput.
        output_bytes = 2 * DEFAULT_LOG_LIMIT_BYTES + 73
        job = self.command(f"import sys; sys.stdout.buffer.write(b'x'*{output_bytes})")
        self.jobs.launch("owner", job["id"])
        result = self.wait(job["id"])
        self.assertEqual(result["state"], "completed")
        output = Path(result["output_path"]).read_bytes()
        self.assertLessEqual(len(output), DEFAULT_LOG_LIMIT_BYTES)
        self.assertTrue(output.startswith(TRUNCATION_MARKER))
        self.assertEqual(output[len(TRUNCATION_MARKER):], b"x" * (len(output) - len(TRUNCATION_MARKER)))
        logs = self.jobs.logs("owner", job["id"])
        self.assertTrue(logs["gap"])
        self.assertEqual(logs["size"], output_bytes)
        self.assertEqual(logs["offset"], output_bytes - len(output))

    def test_timeout_kills_descendant_that_ignores_termination_after_leader_exits(self):
        marker = self.root / "descendant-progress"
        descendant = ("import signal,time; from pathlib import Path; "
                      "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                      f"p=Path({str(marker)!r})\n"
                      "while True:\n with p.open('a') as stream: stream.write('alive\\n')\n time.sleep(.03)")
        job = self.command(f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{descendant!r}]); time.sleep(30)", timeout=0.4)
        with self.store._connection() as db:
            db.execute("UPDATE jobs SET state='starting',supervisor_token='group-test',launched_at=? WHERE id=?",
                       (time.time(), job["id"]))
        result = supervise(self.store, job["id"], "group-test", poll_seconds=0.02, grace_seconds=0.1)
        self.assertEqual(result["state"], "timed_out")
        self.assertTrue(marker.exists(), "Descendant must start before testing group cleanup")
        size = marker.stat().st_size
        time.sleep(0.15)
        self.assertEqual(marker.stat().st_size, size, "Termination-ignoring descendant must stop writing")

    def test_job_argv_is_literal_and_owner_identity_is_not_inherited(self):
        code = "import json,os,sys; print(json.dumps([sys.argv[1:],os.environ.get('AGENT_COORD_SESSION_ID'),os.environ.get('CODEX_THREAD_ID')]))"
        job = self.jobs.create("owner", [sys.executable, "-c", code, "$(false)", "two words", ""])
        with patch.dict(os.environ, {"AGENT_COORD_SESSION_ID": "secret-owner", "CODEX_THREAD_ID": "secret-owner"}):
            self.jobs.launch("owner", job["id"])
        self.wait(job["id"])
        output = json.loads(self.jobs.logs("owner", job["id"])["text"])
        self.assertEqual(output, [["$(false)", "two words", ""], None, None])

    def test_multibyte_logs_can_be_paged_without_losing_characters(self):
        text = "a🙂é日本語z"
        job = self.command(f"import sys; sys.stdout.write({text!r})")
        self.jobs.launch("owner", job["id"])
        self.wait(job["id"])
        output, offset = "", 0
        for _ in range(30):
            page = self.jobs.logs("owner", job["id"], offset=offset, limit=2)
            output += page["text"]
            offset = page["next_offset"]
            if page["eof"]:
                break
        self.assertTrue(page["eof"])
        self.assertEqual(output, text)

    def test_invalid_input_and_owner_only_operations(self):
        for command in ([], "echo hello", [""], ["printf", "bad\0argument"]):
            with self.subTest(command=command), self.assertRaises(CoordinationError):
                self.jobs.create("owner", command)
        for timeout in (0, -1, float("nan"), float("inf"), True, 10 ** 1000):
            with self.subTest(timeout=timeout), self.assertRaises(CoordinationError):
                self.command("pass", timeout=timeout)
        job = self.command("pass", job_id="owned")
        for operation in (self.jobs.get, self.jobs.cancel, self.jobs.launch, self.jobs.logs):
            with self.subTest(operation=operation.__name__), self.assertRaises(CoordinationError):
                operation("other", job["id"])
        with self.assertRaises(CoordinationError):
            self.command("pass", job_id="../escape")
        self.assertEqual(self.jobs.list("other"), [])

    def test_idempotency_rejects_conflicting_commands(self):
        first = self.command("pass", job_id="same")
        self.assertEqual(self.command("pass", job_id="same"), first)
        with self.assertRaises(CoordinationError):
            self.command("print('different')", job_id="same")

    def test_stale_supervisor_is_unknown_and_never_relaunched(self):
        job = self.command("raise RuntimeError('must not run')")
        with self.store._connection() as db:
            db.execute("UPDATE jobs SET state='running',supervisor_pid=NULL,supervisor_token='old',heartbeat_at=0 WHERE id=?", (job["id"],))
        self.assertEqual(self.jobs.get("owner", job["id"])["state"], "unknown")
        self.assertFalse(self.jobs.launch("owner", job["id"])["launched"])

    @contextmanager
    def contended_supervisor(self, code, **kwargs):
        # Exercise real SQLite contention, with a short busy wait so this also
        # reproduces the old five-second timeout without slowing every test.
        connect = self.store._connect

        def impatient_connect():
            db = connect()
            db.execute("PRAGMA busy_timeout = 20")
            return db

        job = self.command(code, **kwargs)
        with self.store._connection() as db:
            db.execute("UPDATE jobs SET state='starting',supervisor_token='contention',launched_at=? WHERE id=?",
                       (time.time(), job["id"]))
        with patch.object(self.store, "_connect", impatient_connect), ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(supervise, self.store, job["id"], "contention",
                                 poll_seconds=0.01, heartbeat_seconds=0.02, grace_seconds=0.05,
                                 log_limit_bytes=8192, log_retain_bytes=4096)
            try:
                self.wait(job["id"], lambda row: row["child_pid"] is not None)
                yield job, future
            finally:
                self.jobs.cancel("owner", job["id"])
                # Always observe exceptions and finish the supervisor before
                # temporary paths disappear, even if an assertion fails.
                future.result(timeout=12)

    def test_writer_lock_preserves_command_and_delivers_completion_once(self):
        marker, finish = self.root / "alive", self.root / "finish"
        code = (f"from pathlib import Path; import time,os; p=Path({str(marker)!r}); "
                f"finish=Path({str(finish)!r})\n"
                "while not finish.exists():\n p.write_text(str(time.time())); os.write(1,b'x'*4096); time.sleep(.01)\n"
                "print('completed after contention')")
        events = EventSubscriptions(self.store)
        with self.contended_supervisor(code) as (job, future):
            subscription = events.create("owner", jobs=[job["id"]], continuation="continue")
            with self.store._connection() as writer:
                writer.execute("BEGIN IMMEDIATE")
                time.sleep(0.2)
                before = marker.read_text()
                time.sleep(0.15)
                self.assertNotEqual(marker.read_text(), before, "A busy heartbeat must not kill the command")
                finish.touch()
                time.sleep(0.15)
                self.assertFalse(future.done(), "The supervisor must retain the result until it can be saved")
            self.assertEqual(future.result(timeout=5)["state"], "completed")
        self.assertTrue(self.jobs.logs("owner", job["id"])["gap"])
        self.assertIn("completed after contention", self.jobs.logs("owner", job["id"])["text"])
        restarted = EventSubscriptions(CoordinationStore(self.store.database_path))
        self.assertEqual(len(restarted.process_once()), 1)
        self.assertEqual(restarted.process_once(), [])
        self.assertEqual(restarted.get("owner", subscription["id"])["items"][0]["outcome"]["state"], "completed")

    def test_writer_lock_does_not_delay_command_timeout(self):
        marker = self.root / "progress"
        code = (f"from pathlib import Path; import time; p=Path({str(marker)!r})\n"
                "while True:\n p.write_text(str(time.time())); time.sleep(.01)")
        with self.contended_supervisor(code, timeout=0.3) as (job, future):
            with self.store._connection() as writer:
                writer.execute("BEGIN IMMEDIATE")
                time.sleep(0.55)
                before = marker.read_text()
                time.sleep(0.1)
                self.assertEqual(marker.read_text(), before, "The timeout must stop the child while SQLite is busy")
                self.assertFalse(future.done(), "Terminal persistence should wait for the writer")
            self.assertEqual(future.result(timeout=5)["state"], "timed_out")

    def test_local_heartbeat_protects_busy_supervisor_but_not_abandoned_or_stuck_one(self):
        for lost in ("lock", "heartbeat"):
            with self.subTest(lost=lost):
                job = self.command("pass")
                with self.store._connection() as db:
                    db.execute("UPDATE jobs SET state='running',supervisor_pid=?,supervisor_token='busy',heartbeat_at=0 WHERE id=?",
                               (os.getpid(), job["id"]))
                with _supervisor_lock(self.store, job["id"]):
                    _touch_supervisor(self.store, job["id"])
                    self.assertEqual(self.jobs.get("owner", job["id"])["state"], "running")
                    if lost == "heartbeat":
                        path = Path(job["output_path"]).parent / "supervisor.lock"
                        os.utime(path, (0, 0))
                        self.assertEqual(self.jobs.get("owner", job["id"])["state"], "unknown")
                self.assertEqual(self.jobs.get("owner", job["id"])["state"], "unknown")
                self.assertFalse(self.jobs.launch("owner", job["id"])["launched"])

    def test_stale_sample_cannot_override_a_fresh_supervisor_heartbeat(self):
        job = self.command("pass")
        with self.store._connection() as db:
            db.execute("UPDATE jobs SET state='running',supervisor_pid=?,supervisor_token='active',heartbeat_at=0 WHERE id=?", (os.getpid(), job["id"]))
            stale = db.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone()
            db.execute("UPDATE jobs SET heartbeat_at=? WHERE id=?", (time.time(), job["id"]))
        self.assertEqual(self.jobs._reconcile(stale)["state"], "running")
        with self.store._connection() as db:
            db.execute("UPDATE jobs SET state='completed',finished_at=? WHERE id=?", (time.time(),job["id"]))


if __name__ == "__main__":
    unittest.main()
