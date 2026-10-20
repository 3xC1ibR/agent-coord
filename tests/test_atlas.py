"""Live Atlas rendering and overview layout interactions."""
import shutil
import subprocess
import unittest
from pathlib import Path


class AtlasBrowserTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is required for browser tests")
    def test_live_atlas_and_overview_layout(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            ["node", "--test", "tests/test_web_atlas.js", "tests/test_web_overview_layout.js"],
            cwd=root, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_atlas_assets_are_served_and_loaded(self):
        import sys
        import tempfile
        import threading
        import urllib.request
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
                    self.assertIn(b'id="atlas-overview"', page)
                    self.assertIn(b'href="/atlas.css"', page)
                    for script in [b"atlas-layout.js", b"atlas.js", b"overview-layout.js"]:
                        self.assertIn(b'src="/' + script + b'"', page)
                for path, mime in [("atlas-layout.js", "javascript"), ("atlas.js", "javascript"),
                                   ("overview-layout.js", "javascript"), ("atlas.css", "css")]:
                    with urllib.request.urlopen(base + "/" + path) as response:
                        self.assertEqual(response.status, 200)
                        self.assertIn(mime, response.headers["Content-Type"])
            finally:
                server.shutdown()
                worker.join(timeout=5)
                server.server_close()
