"""Process and isolation checks for the browser interface validation fixture."""

from __future__ import annotations

import json
from pathlib import Path
import select
import subprocess
import sys
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/interface_fixture_server.py"


class InterfaceFixtureServerTests(unittest.TestCase):
    def start_fixture(self, state):
        process = subprocess.Popen(
            [sys.executable, "-B", "-W", "error::ResourceWarning", str(FIXTURE), "--state", state],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.addCleanup(self.stop_fixture, process)
        readable, _, _ = select.select([process.stdout], [], [], 20)
        self.assertTrue(readable, "Fixture did not announce readiness.")
        line = process.stdout.readline()
        if not line:
            self.fail(f"Fixture failed before readiness. {process.communicate(timeout=5)[1]}")
        return process, json.loads(line)

    @staticmethod
    def stop_fixture(process):
        if process.poll() is None:
            process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)

    def get(self, base, path):
        with urlopen(base + path, timeout=5) as response:
            return json.load(response)

    def test_disposable_fixture_retires_its_listener_and_database(self):
        process, fixture = self.start_fixture("default")
        workspace = Path(fixture["workspace"])
        self.assertTrue(workspace.is_dir())
        self.assertTrue((workspace.parent / "fixture.sqlite3").is_file())
        config = self.get(fixture["base"], "/api/browser/config")
        self.assertEqual(config["workspaceRoot"], str(workspace.parent))
        self.assertEqual(len(self.get(fixture["base"], "/api/browser/asks?status=open")["data"]), 4)
        request = Request(fixture["base"] + "/api/browser/integrations/stoic", data=b"{}", method="POST")
        with self.assertRaises(HTTPError) as failure:
            urlopen(request, timeout=5)
        with failure.exception as response:
            self.assertEqual(response.code, 403)
        self.stop_fixture(process)
        self.assertEqual(process.returncode, 0)
        self.assertFalse(workspace.parent.exists())

    def test_stream_advances_through_the_provider_event_path(self):
        _, fixture = self.start_fixture("streaming")
        thread = fixture["stream"]["thread"]
        before = self.get(fixture["base"], f"/api/browser/sessions/{thread}?recent=1")
        self.assertIn("Streaming inspection update", json.dumps(before))
        self.assertEqual(self.get(fixture["base"], "/fixture/advance"), {"advanced": True})
        after = self.get(fixture["base"], f"/api/browser/sessions/{thread}?recent=1")
        self.assertIn("Applied the final calibration sample.", json.dumps(after))


if __name__ == "__main__":
    unittest.main()
