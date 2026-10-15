from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
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

from agent_coord.jobs import ManagedJobs, TERMINAL_STATES, DEFAULT_LOG_LIMIT_BYTES, supervise
from agent_coord.store import CoordinationError, CoordinationStore


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
        self.jobs.launch("owner", job["id"])
        result = self.wait(job["id"])
        self.assertEqual(result["state"], "timed_out")
        self.assertLessEqual(Path(result["output_path"]).stat().st_size, DEFAULT_LOG_LIMIT_BYTES)
        self.assertTrue(self.jobs.logs("owner", job["id"])["gap"])

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
