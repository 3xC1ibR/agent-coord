import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))
from agent_coord import launch_metrics
from agent_coord.store import CoordinationStore
from agent_coord.ui import make_ui_server


class LaunchMetricsHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = CoordinationStore(Path(self.temp.name) / "state.sqlite3")
        launch_metrics.initialize(self.store)
        self.server = make_ui_server(self.store, port=0, cwd=self.temp.name)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self.stop)
        self.url = "http://127.0.0.1:" + str(self.server.server_port)
        self.token = self.request("config")[1]["token"]

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(2)

    def request(self, route, body=None, token=None):
        headers = {}
        if body is not None:
            headers = {"Content-Type": "application/json", "X-Agent-Coord-Token": token if token is not None else self.token}
        req = urllib.request.Request(self.url + "/api/browser/" + route,
            data=json.dumps(body).encode() if body is not None else None, headers=headers)
        try:
            response = urllib.request.urlopen(req, timeout=3)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.status, json.loads(response.read())

    def test_preference_requires_csrf_and_clears_queue(self):
        self.assertTrue(self.request("launch-metrics")[1]["enabled"])
        self.assertEqual(self.request("launch-metrics", {"enabled": False}, token="wrong")[0], 403)
        self.assertEqual(self.request("launch-metrics", {"enabled": "false"})[0], 400)
        self.assertEqual(self.request("launch-metrics", {"enabled": False})[0], 200)
        self.assertFalse(self.request("launch-metrics")[1]["enabled"])
        with self.store._connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM launch_outbox").fetchone()[0], 0)

    def test_explicit_attribution_requires_valid_token_and_csrf(self):
        self.assertEqual(self.request("launch-metrics/attribution", {"download_token": "bad"})[0], 400)
        token = str(uuid.uuid4())
        self.assertEqual(self.request("launch-metrics/attribution", {"download_token": token}, token="bad")[0], 403)
        self.assertEqual(self.request("launch-metrics/attribution", {"download_token": token})[0], 200)
        with self.store._connection() as db:
            self.assertEqual(db.execute("SELECT download_token FROM launch_install").fetchone()[0], token)
