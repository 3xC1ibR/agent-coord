from __future__ import annotations

import http.client
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import quote, urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))
from agent_coord.html_preview import read_html
from agent_coord.remote_access import RemoteAccess
from agent_coord.store import CoordinationError, CoordinationStore
from agent_coord.ui import _handler
from test_remote_access import FakeTailscale


class HTMLFileTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        self.file = self.workspace / "report with spaces.HTML"
        self.file.write_text("<!doctype html><h1>Result</h1><script>window.ready=true</script>")

    def test_absolute_relative_and_internal_symlink(self):
        (self.workspace / "linked.html").symlink_to(self.file)
        for path in (str(self.file), self.file.name, "./" + self.file.name, "linked.html"):
            with self.subTest(path=path):
                result = read_html(str(self.workspace), path)
                self.assertIn("window.ready=true", result["html"])
                self.assertEqual(result["name"], self.file.name)

    def test_traversal_and_symlinks_cannot_leave_project(self):
        outside = self.root / "private.html"
        outside.write_text("private")
        (self.workspace / "escape.html").symlink_to(outside)
        (self.workspace / "escape-dir").symlink_to(self.root, target_is_directory=True)
        for path in (str(outside), "../private.html", "escape.html", "escape-dir/private.html"):
            with self.subTest(path=path), self.assertRaisesRegex(CoordinationError, "outside"):
                read_html(str(self.workspace), path)

    def test_invalid_missing_empty_oversized_and_special_files(self):
        (self.workspace / "notes.txt").write_text("not HTML")
        (self.workspace / "empty.html").write_text("  ")
        (self.workspace / "invalid.html").write_bytes(b"\xff")
        (self.workspace / "directory.html").mkdir()
        os.mkfifo(self.workspace / "pipe.html")
        for path in ("", "bad\0.html", "missing.html", "notes.txt", "empty.html", "invalid.html", "directory.html", "pipe.html"):
            with self.subTest(path=path), self.assertRaises(CoordinationError):
                read_html(str(self.workspace), path)
        with patch("agent_coord.html_preview.MAX_HTML_BYTES", 10), self.assertRaisesRegex(CoordinationError, "too large"):
            read_html(str(self.workspace), self.file.name)

    def test_browser_behaviors(self):
        result = subprocess.run(["node", "--test", str(Path(__file__).with_name("test_web_html_preview.js"))], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class HTMLPreviewHTTPTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        self.file = self.workspace / "report.html"
        self.file.write_text("<!doctype html><h1>Private report</h1><script>window.ready=true</script>")
        self.store = CoordinationStore(self.root / "state.sqlite3")
        self.store.register(session_id="author", client="codex", cwd=str(self.workspace))
        self.store.register(session_id="outside", client="codex", cwd=str(self.root))
        self.remote = RemoteAccess(self.store.database_path, tailscale=FakeTailscale())
        self.remote.target = "http://127.0.0.1:9876"
        self.remote.enable()
        cookie = self.remote.redeem(self.remote.pairing()["url"].split("#pair=")[1], "Phone")
        self.cookie = cookie.split(";")[0]
        self.addCleanup(self.remote.close)
        # No provider runtime is created or resumed by these read-only routes.
        sessions = SimpleNamespace(cwd=str(self.workspace))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(self.store, None, None, sessions, remote_access=self.remote, provider_limits=object()))
        worker = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        worker.start()
        self.addCleanup(worker.join, 2)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def get(self, path, cookie=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            headers = {"Host": "mac.test.ts.net"}
            if cookie:
                headers["Cookie"] = cookie
            connection.request("GET", path, headers=headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def url(self, route="html-preview", **changes):
        fields = {"thread": "author", "path": str(self.file), **changes}
        if route == "html-preview-content":
            return "/api/browser/html-preview-content/" + quote(fields["thread"], safe="") + "/" + quote(fields["path"], safe="")
        return "/api/browser/" + route + "?" + urlencode(fields)

    def test_pairing_required_for_every_preview_asset_and_content(self):
        for path in ("/html-preview", "/html-preview.js", "/html-preview.css", self.url(), self.url("html-preview-content")):
            with self.subTest(path=path):
                self.assertEqual(self.get(path)[0], 401)
                self.assertEqual(self.get(path, self.cookie)[0], 200)
        self.remote.disable()
        self.assertIn(self.get(self.url(), self.cookie)[0], (401, 403))

    def test_metadata_and_html_keep_content_isolated_even_when_opened_directly(self):
        status, headers, data = self.get(self.url(), self.cookie)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data), {"name": "report.html"})
        status, headers, data = self.get(self.url("html-preview-content"), self.cookie)
        self.assertEqual(status, 200)
        self.assertIn(b"Private report", data)
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        policy = headers["Content-Security-Policy"]
        for directive in ("sandbox allow-scripts", "connect-src 'none'", "frame-src 'none'", "form-action 'none'", "base-uri 'none'"):
            self.assertIn(directive, policy)
        self.assertNotIn("allow-same-origin", policy)
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_workspace_filter_and_explicit_file_errors(self):
        for changes, error in (({"thread": "outside"}, "workspace"), ({"path": "missing.html"}, "missing")):
            status, _, data = self.get(self.url(**changes), self.cookie)
            self.assertEqual(status, 400)
            self.assertIn(error, json.loads(data)["error"])
        self.assertEqual(self.get(self.url() + "&path=other.html", self.cookie)[0], 400)
        self.assertEqual(self.get("/api/browser/html-preview", self.cookie)[0], 400)


if __name__ == "__main__":
    unittest.main()
