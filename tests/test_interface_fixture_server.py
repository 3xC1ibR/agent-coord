"""Process and isolation checks for the browser interface validation fixture."""

from __future__ import annotations

import json
from pathlib import Path
import select
import subprocess
import sys
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/interface_fixture_server.py"


class InterfaceFixtureServerTests(unittest.TestCase):
    def start_fixture(self, state, *options):
        process = subprocess.Popen(
            [sys.executable, "-B", "-W", "error::ResourceWarning", str(FIXTURE), "--state", state, *options],
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

    def test_interaction_fixture_accepts_a_visit_at_the_browser_wall_clock(self):
        _, fixture = self.start_fixture("interactions")
        base = fixture["base"]
        token = self.get(base, "/api/browser/config")["token"]
        event = {
            "event_id": str(uuid4()), "kind": "visit_started", "thread_id": fixture["threads"]["other"]["id"],
            "occurred_at": time.time(), "window_id": str(uuid4()), "visit_id": str(uuid4()), "source": "focus",
        }
        request = Request(base + "/api/browser/activity/events", data=json.dumps({"events": [event]}).encode(),
                          headers={"Content-Type": "application/json", "X-Agent-Coord-Token": token, "Origin": base},
                          method="POST")
        with urlopen(request, timeout=5) as response:
            receipt = json.load(response)["receipts"][0]
        self.assertEqual(receipt["event_id"], event["event_id"])
        self.assertGreater(receipt["sequence"], 0)

    def test_cacheable_fixture_limits_the_policy_to_its_html_shell(self):
        _, fixture = self.start_fixture("default", "--cacheable-shell")
        for path, expected in (("/", "private, no-cache"), ("/api/browser/config", "no-store"),
                               ("/fixture/document", "no-store")):
            with urlopen(fixture["base"] + path, timeout=5) as response:
                self.assertEqual(response.headers["Cache-Control"], expected)


if __name__ == "__main__":
    unittest.main()
