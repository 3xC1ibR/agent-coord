from __future__ import annotations

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


class ThreadPaneTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is required for pane tests")
    def test_layout_and_lifecycle(self):
        result = subprocess.run(["node", "--test", str(Path(__file__).with_name("test_web_thread_panes.js"))],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_only_explicit_pane_document_can_be_framed_by_same_origin(self):
        with tempfile.TemporaryDirectory() as directory:
            store = CoordinationStore(Path(directory) / "state.sqlite3")
            server = make_ui_server(store, host="127.0.0.1", port=0)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                base = "http://127.0.0.1:" + str(server.server_address[1])
                for path in ["/", "/?pane=0", "/monitor", "/thread-panes.js", "/thread-panes.css"]:
                    with urllib.request.urlopen(base + path) as response:
                        self.assertEqual(response.status, 200)
                        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
                        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
                with urllib.request.urlopen(base + "/?pane=1") as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.headers["X-Frame-Options"], "SAMEORIGIN")
                    self.assertIn("frame-ancestors 'self'", response.headers["Content-Security-Policy"])
                    self.assertIn(b'id="thread-panes"', response.read())
            finally:
                server.shutdown()
                worker.join(timeout=5)
                server.server_close()


if __name__ == "__main__":
    unittest.main()
