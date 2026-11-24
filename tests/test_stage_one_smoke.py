"""Exercise native timeout cleanup with an unresponsive private owner and detached child."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == "darwin", "native smoke harness requires macOS")
class SmokeCleanupTests(unittest.TestCase):
    def test_forced_timeout_reaps_private_transport_and_detached_descendant(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = root / "Fixture.app"
            executable = app / "Contents/MacOS/Agent Coord"
            executable.parent.mkdir(parents=True)
            owner = root / "unresponsive.py"
            owner.write_text("""import json, os, signal, subprocess, sys, time
from pathlib import Path
directory = Path(sys.argv[-1])
directory.mkdir(parents=True, exist_ok=True)
child = subprocess.Popen([sys.executable, "-c",
    "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"],
    start_new_session=True)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
(directory / "owner.json").write_text(json.dumps({"pid": os.getpid(), "protocol": 1,
    "url": "http://127.0.0.1:1/", "token": "fixture", "instance": "fixture"}))
Path(os.environ["SMOKE_RECEIPT"]).write_text(json.dumps([os.getpid(), child.pid, str(directory)]))
time.sleep(60)
""")
            executable.write_text("#!" + sys.executable + "\n" + """import os, subprocess, sys, time
from pathlib import Path
directory = Path(sys.argv[sys.argv.index("--smoke-directory") + 1]) / ".state.sqlite3.runtime/provider-transport"
subprocess.Popen([sys.executable, os.environ["SMOKE_OWNER"], "agent_coord.provider_transport",
    "--directory", str(directory)], start_new_session=True, stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(60)
""")
            executable.chmod(0o700)
            receipt = root / "receipt"
            # A separate live process must survive cleanup of the private fixture.
            unrelated = subprocess.Popen(["sleep", "60"], start_new_session=True)
            try:
                command = [sys.executable, str(ROOT / "desktop/macos/smoke_test.py"),
                           "--app", str(app), "--timeout", "2", "--report", str(root / "report")]
                run = subprocess.run(command, capture_output=True, text=True, timeout=35,
                    env={**os.environ, "SMOKE_OWNER": str(owner), "SMOKE_RECEIPT": str(receipt),
                         "TMPDIR": "/private/tmp"})
                self.assertNotEqual(run.returncode, 0)
                self.assertIn("Native smoke test timed out", run.stderr)
                owner_pid, child_pid, directory = json.loads(receipt.read_bytes())
                self.assertIsNone(unrelated.poll())
                for pid in (owner_pid, child_pid):
                    with self.assertRaises(ProcessLookupError):
                        os.kill(pid, 0)
                self.assertFalse(Path(directory).parents[1].exists())
            finally:
                if receipt.exists():
                    for pid in json.loads(receipt.read_bytes())[:2]:
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                unrelated.terminate()
                unrelated.wait(timeout=5)
