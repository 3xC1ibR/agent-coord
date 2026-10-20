"""Working Set layout behavior and production asset delivery."""
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path


class WorkingSetTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is required for browser tests")
    def test_working_set(self):
        result = subprocess.run(
            ["node", "--test", "tests/test_web_working_set.js"],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_assets_are_served_and_loaded(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))
        from agent_coord.store import CoordinationStore
        from agent_coord.ui import make_ui_server
        with tempfile.TemporaryDirectory() as directory:
            server = make_ui_server(CoordinationStore(Path(directory) / "state.sqlite3"), port=0)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                base = "http://127.0.0.1:" + str(server.server_address[1])
                with urllib.request.urlopen(base + "/") as response:
                    page = response.read()
                    self.assertIn(b'id="working-set-overview"', page)
                    self.assertIn(b'href="/working-set.css"', page)
                    self.assertIn(b'src="/working-set.js"', page)
                for path, mime in [("working-set.js", "javascript"), ("working-set.css", "css")]:
                    with urllib.request.urlopen(base + "/" + path) as response:
                        self.assertEqual(response.status, 200)
                        self.assertIn(mime, response.headers["Content-Type"])
            finally:
                server.shutdown()
                worker.join(timeout=5)
                server.server_close()
