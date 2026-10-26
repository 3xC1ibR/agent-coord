from __future__ import annotations

import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))
from agent_coord.context import coordination_bootstrap
from agent_coord.codex_app_server import BrowserSessions
from agent_coord.store import CoordinationStore
from agent_coord.ui import make_ui_server
from test_codex_app_server import FakeCodex


class OpenUITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = CoordinationStore(Path(temporary.name) / "state.sqlite3")

    def test_providers_receive_the_installed_authoring_guide(self):
        for client in ("codex", "claude", "kimi", "muse", "antigravity"):
            with self.subTest(client=client):
                self.store.register(session_id=client, client=client, cwd=tempfile.gettempdir())
                bootstrap = coordination_bootstrap(self.store, client)
                self.assertIn("references/openui.md", bootstrap)
                self.assertIn("OpenUI response", bootstrap)

    def test_browser_regressions(self):
        result = subprocess.run(["node", "--test", str(Path(__file__).with_name("test_web_openui.js")),
                                 str(Path(__file__).with_name("test_web_markdown.js"))], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_assets_are_fixed_local_files_and_frame_has_an_isolated_policy(self):
        sessions = BrowserSessions(self.store, tempfile.gettempdir(), rpc_factory=FakeCodex)
        remote = MagicMock()
        remote.origin = "https://paired.example"
        remote.authenticated.return_value = False
        server = make_ui_server(self.store, port=0, browser_sessions=sessions, remote_access=remote)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(worker.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = "http://127.0.0.1:" + str(server.server_address[1])
        for path, kind in (("/openui.js", "text/javascript"), ("/openui.css", "text/css"),
                           ("/openui-frame.js", "text/javascript"), ("/openui-frame.html", "text/html"),
                           ("/vendor/openui/openui-bundle.min.js", "text/javascript"),
                           ("/vendor/openui/openui-styles.css", "text/css")):
            with self.subTest(path=path), urllib.request.urlopen(url + path) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers.get_content_type(), kind)
                self.assertGreater(len(response.read()), 100)
                policy = response.headers["Content-Security-Policy"]
                if path == "/openui-frame.html":
                    self.assertIn("script-src " + url + ";", policy)
                    self.assertIn("style-src " + url + " 'unsafe-inline';", policy)
                    self.assertNotIn("script-src 'self'", policy)
                    self.assertIn("connect-src 'none'", policy)
                    self.assertIn("form-action 'none'", policy)
                    self.assertIn("frame-ancestors 'self'", policy)
                    self.assertNotIn("allow-same-origin", policy)
                if path == "/openui.js":
                    self.assertIn("connect-src 'self'", policy)
        for path in ("/vendor/openui/LICENSE", "/vendor/openui/../../context.py"):
            with self.subTest(path=path), self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(url + path)
            self.assertEqual(error.exception.code, 404)
            error.exception.close()
        # Sandboxed remote subresources have no pairing cookie. Only the fixed
        # renderer files may load anonymously; the app and thread APIs may not.
        for path in ("/openui-frame.html", "/openui-frame.js", "/vendor/openui/openui-bundle.min.js", "/vendor/openui/openui-styles.css"):
            request = urllib.request.Request(url + path, headers={"Host": "paired.example"})
            with urllib.request.urlopen(request) as response:
                self.assertEqual(response.status, 200)
                if path == "/openui-frame.html":
                    self.assertIn("script-src https://paired.example;", response.headers["Content-Security-Policy"])
        for path in ("/api/browser/config", "/openui.js"):
            request = urllib.request.Request(url + path, headers={"Host": "paired.example"})
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request)
            self.assertEqual(error.exception.code, 401)
            error.exception.close()
