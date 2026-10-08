import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))
from agent_coord.store import CoordinationError, CoordinationStore
from agent_coord.workspace_files import workspace_files, MAX_PREVIEW_BYTES
from agent_coord.ui import make_ui_server


class WorkspaceFilesTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        (self.root / "folder").mkdir()
        (self.root / "z.txt").write_text("hello")
        (self.root / ".hidden").write_text("hidden")

    def call(self, operation, **values):
        return workspace_files(operation, {"root": str(self.root), **values})

    def test_lazy_listing_hidden_and_bounds(self):
        (self.root / "folder" / "child.txt").write_text("nested")
        listing = self.call("list")
        self.assertEqual([e["path"] for e in listing["entries"]], ["folder", "z.txt"])
        self.assertEqual(self.call("list", path="folder")["entries"][0]["path"], "folder/child.txt")
        self.assertEqual(len(self.call("list", hidden=True)["entries"]), 3)
        with patch("agent_coord.workspace_files.MAX_ENTRIES", 1):
            self.assertTrue(self.call("list")["truncated"])

    def test_preview_binary_large_utf8_and_special_files(self):
        self.assertEqual(self.call("preview", path="z.txt")["text"], "hello")
        (self.root / "binary").write_bytes(b"\x00\xff")
        self.assertTrue(self.call("preview", path="binary")["binary"])
        (self.root / "large").write_bytes(b"a" * (MAX_PREVIEW_BYTES - 1) + "€".encode())
        result = self.call("preview", path="large")
        self.assertTrue(result["truncated"])
        self.assertFalse(result["binary"])
        self.assertEqual(len(result["text"]), MAX_PREVIEW_BYTES - 1)
        if hasattr(os, "mkfifo"):
            os.mkfifo(self.root / "fifo")
            with self.assertRaises(CoordinationError):
                self.call("preview", path="fifo")

    def test_root_and_traversal_boundaries(self):
        for path in ["../outside", "/etc/passwd", "folder/../../outside", "bad\0path", "..\\outside"]:
            with self.subTest(path=path), self.assertRaises(CoordinationError):
                self.call("preview", path=path)
        for values in [{"root": "relative"}, {"root": None}, {"hidden": "yes"}, {"extra": True}]:
            with self.assertRaises(CoordinationError):
                self.call("list", **values)
        with self.assertRaises(CoordinationError):
            workspace_files("list", {"root": str(self.root)}, str(self.root / "folder"))
        self.assertEqual(self.call("list", path="folder")["entries"], [])

    def test_symlinks_do_not_escape_root_or_expand_recursively(self):
        (self.root / "link").symlink_to(self.root.parent, target_is_directory=True)
        entry = next(e for e in self.call("list")["entries"] if e["name"] == "link")
        self.assertTrue(entry["symlink"])
        self.assertFalse(entry["directory"])
        with self.assertRaises(CoordinationError):
            self.call("list", path="link")
        (self.root / "loop").symlink_to("loop")
        with self.assertRaises(CoordinationError):
            self.call("preview", path="loop")

    def test_http_requires_existing_csrf_boundary_and_respects_server_workspace(self):
        server = make_ui_server(CoordinationStore(self.root / "state.sqlite3"), port=0, cwd=str(self.root / "folder"))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}"
            with urllib.request.urlopen(url + "/api/browser/config") as response:
                token = json.load(response)["token"]
            def request(root, headers):
                return urllib.request.urlopen(urllib.request.Request(url + "/api/browser/files/list",
                    data=json.dumps({"root": str(root)}).encode(),
                    headers={"Content-Type": "application/json", **headers}))
            with self.assertRaises(urllib.error.HTTPError) as exc:
                request(self.root / "folder", {})
            self.assertEqual(exc.exception.code, 403); exc.exception.close()
            headers = {"X-Agent-Coord-Token": token}
            with request(self.root / "folder", headers) as response:
                self.assertEqual(json.load(response)["entries"], [])
            with self.assertRaises(urllib.error.HTTPError) as exc:
                request(self.root, headers)
            self.assertEqual(exc.exception.code, 400); exc.exception.close()
            with self.assertRaises(urllib.error.HTTPError) as exc:
                request(self.root / "folder", {**headers, "Origin": "https://untrusted.example"})
            self.assertEqual(exc.exception.code, 403); exc.exception.close()
        finally:
            server.shutdown(); server.server_close(); worker.join()


if __name__ == "__main__":
    unittest.main()
