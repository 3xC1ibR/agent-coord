import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))
from agent_coord.store import CoordinationStore
from agent_coord.ui import make_ui_server


class ThreadTabsTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is required for conversation tab tests")
    def test_browser_behavior(self):
        result = subprocess.run(
            ["node", "--test", str(Path(__file__).with_name("test_web_thread_tabs.js"))],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_tab_assets_are_served_and_loaded(self):
        with tempfile.TemporaryDirectory() as directory:
            server = make_ui_server(CoordinationStore(Path(directory) / "state.sqlite3"), host="127.0.0.1", port=0)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                base = "http://127.0.0.1:" + str(server.server_address[1])
                with urllib.request.urlopen(base + "/") as response:
                    page = response.read()
                    self.assertIn(b'id="conversation-tabs"', page)
                    self.assertIn(b'src="/thread-tabs.js"', page)
                    self.assertIn(b'href="/thread-tabs.css"', page)
                for path in ["/thread-tabs.js", "/thread-tabs.css"]:
                    with urllib.request.urlopen(base + path) as response:
                        self.assertEqual(response.status, 200)
                        self.assertNotIn("text/html", response.headers["Content-Type"])
            finally:
                server.shutdown()
                worker.join(timeout=5)
                server.server_close()
