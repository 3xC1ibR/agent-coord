from __future__ import annotations

import base64
import copy
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))

from agent_coord.codex_app_server import BrowserSessions
from agent_coord.image_inputs import MAX_IMAGE_BYTES, message_images
from agent_coord.store import CoordinationError, CoordinationStore
from agent_coord.ui import make_ui_server
from test_codex_app_server import FakeCodex


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=")


def image(data=PNG, mime="image/png", name="screenshot.png"):
    return {"name": name, "url": f"data:{mime};base64," + base64.b64encode(data).decode()}


class ImageValidationTests(unittest.TestCase):
    def test_text_only_and_image_only_inputs(self):
        self.assertEqual(message_images({"message": "  Hello  "}), ("Hello", []))
        self.assertEqual(message_images({"images": [image()]}), ("", [image()]))
        for mime, data in [("image/png", PNG), ("image/jpeg", b"\xff\xd8\xff\xe0"),
                           ("image/webp", b"RIFF\x00\x00\x00\x00WEBP"), ("image/gif", b"GIF89a")]:
            with self.subTest(mime=mime):
                self.assertEqual(len(message_images({"images": [image(data, mime)]})[1]), 1)

    def test_rejects_invalid_or_unsafe_images_and_empty_messages(self):
        invalid = [
            {}, {"message": "  "}, {"message": None}, {"message": 12}, {"message": "x" * 100001},
            {"images": None}, {"images": {}}, {"images": [image()] * 5}, {"images": [None]},
            {"images": [{"url": "https://example.com/image.png"}]},
            {"images": [{"url": "file:///private/image.png"}]},
            {"images": [{"url": "data:image/png;base64,%%%"}]},
            {"images": [{"url": "data:image/png;base64,é"}]},
            {"images": [image(b"<svg/>", "image/svg+xml")]},
            {"images": [image(b"not an image")]}, {"images": [image(b"")]},
            {"images": [image(name="x" * 256)]}, {"images": [image(name=42)]},
            {"images": [image(PNG, "image/jpeg")]},
        ]
        for body in invalid:
            with self.subTest(body=str(body)[:100]), self.assertRaises(CoordinationError):
                message_images(body)

    def test_per_image_limit_and_commands_do_not_discard_attachments(self):
        with self.assertRaisesRegex(CoordinationError, "5 MiB"):
            message_images({"images": [image(PNG + b"x" * MAX_IMAGE_BYTES)]})
        for command in ["/help", "/MODEL available-model", "/effort high"]:
            with self.assertRaisesRegex(CoordinationError, "Remove attached images"):
                message_images({"message": command, "images": [image()]})


class BrowserImageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = CoordinationStore(self.root / "state.sqlite3")
        self.sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(self.sessions.close)
        self.thread_id = self.sessions.create({})["session"]["thread_id"]

    def test_image_only_turn_and_steering_reach_codex_and_persist_in_history(self):
        self.sessions.send(self.thread_id, {"images": [image()]})
        self.assertEqual(self.sessions.rpc.calls[-1][1]["input"], [{"type": "image", "url": image()["url"]}])
        self.assertEqual(self.store.threads.get(self.thread_id)["original_request"], "[Image]")
        self.sessions.send(self.thread_id, {"message": "Compare these", "images": [image(), image(name="second.png")]})
        method, params = self.sessions.rpc.calls[-1]
        self.assertEqual(method, "turn/steer")
        self.assertEqual(params["input"][0], {"type": "text", "text": "Compare these"})
        self.assertEqual([entry["type"] for entry in params["input"]], ["text", "image", "image"])
        self.sessions.close()
        reopened = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(reopened.close)
        history = reopened._history(self.thread_id)
        self.assertEqual(history["turns"][0]["items"][-1]["content"], params["input"])

    def test_validation_occurs_before_codex_and_failed_send_can_retry(self):
        calls = len(self.sessions.rpc.calls)
        with self.assertRaises(CoordinationError):
            self.sessions.send(self.thread_id, {"images": [image(b"invalid")]})
        self.assertEqual(len(self.sessions.rpc.calls), calls)
        self.sessions.rpc.fail_turn = True
        with self.assertRaisesRegex(CoordinationError, "Provider unavailable"):
            self.sessions.send(self.thread_id, {"images": [image()]})
        self.sessions.rpc.fail_turn = False
        self.sessions.send(self.thread_id, {"images": [image()]})
        self.assertEqual(len(self.sessions.rpc.threads[self.thread_id]["turns"]), 1)

    def test_queued_images_survive_restart_and_dispatch_after_review(self):
        self.sessions.send(self.thread_id, {"message": "Begin"})
        self.sessions.queue.enqueue(self.thread_id, {"images": [image()]})
        self.assertEqual(self.sessions.queue.list(self.thread_id)[0]["images"], [image()])
        threads = copy.deepcopy(self.sessions.rpc.threads)
        threads[self.thread_id]["turns"][0]["status"] = "completed"
        self.sessions.close()
        reopened = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(reopened.close)
        reopened.rpc.threads = threads
        pending = reopened.queue.list(self.thread_id)
        self.assertEqual(pending[0]["state"], "paused")
        self.assertEqual(pending[0]["images"], [image()])
        reopened.queue.change(self.thread_id, {"action": "resume"})
        with reopened.changed:
            self.assertTrue(reopened.changed.wait_for(lambda: not reopened.queue.list(self.thread_id), timeout=5))
        method, params = next((m, p) for m, p in reversed(reopened.rpc.calls) if m == "turn/start")
        self.assertEqual(params["input"], [{"type": "image", "url": image()["url"]}])
        self.assertFalse(any(m == "turn/steer" for m, _ in reopened.rpc.calls))

    def test_existing_queue_table_is_migrated_without_losing_messages(self):
        self.sessions.close()
        with self.store._connection() as db:
            db.execute("DROP TABLE browser_message_queue")
            db.execute("""CREATE TABLE browser_message_queue (
                id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, message TEXT NOT NULL,
                state TEXT NOT NULL, error TEXT, owner TEXT NOT NULL, created_at REAL NOT NULL)""")
            db.execute("INSERT INTO browser_message_queue VALUES ('old', ?, 'Keep me', 'queued', NULL, 'owner', 1)", (self.thread_id,))
        reopened = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(reopened.close)
        item = reopened.queue.list(self.thread_id)[0]
        self.assertEqual((item["message"], item["images"], item["state"]), ("Keep me", [], "paused"))


class BrowserImageHTTPTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = CoordinationStore(self.root / "state.sqlite3")
        self.sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.server = make_ui_server(self.store, port=0, cwd=str(self.root), browser_sessions=self.sessions)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self.stop_server)
        self.url = "http://127.0.0.1:" + str(self.server.server_address[1])
        self.token = self.request("/api/browser/config")[1]["token"]
        self.thread_id = self.sessions.create({})["session"]["thread_id"]

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(2)

    def request(self, path, body=None, token=None):
        headers = {"Content-Type": "application/json", "X-Agent-Coord-Token": token or getattr(self, "token", "")}
        request = urllib.request.Request(self.url + path, data=json.dumps(body).encode() if body is not None else None, headers=headers)
        try:
            response = urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            content = response.read().decode()
            result = json.loads(content) if response.headers.get_content_type() == "application/json" else content
            return response.status, result, response.headers

    def test_large_images_work_on_message_and_queue_routes_only(self):
        body = {"images": [image(PNG + b"x" * (150 * 1024))]}
        path = "/api/browser/sessions/" + self.thread_id
        self.assertEqual(self.request(path + "/messages", body)[0], 200)
        self.assertEqual(self.request(path + "/queue", body)[0], 200)
        self.assertEqual(self.request(path, body)[0], 413)
        self.assertEqual(self.request(path + "/messages", body, token="wrong")[0], 403)
        self.assertEqual(self.request(path + "/messages", {"images": [image(b"bad") ]})[0], 400)
        with patch("agent_coord.ui.MAX_MESSAGE_BODY_BYTES", 100):
            self.assertEqual(self.request(path + "/messages", body)[0], 413)

    def test_image_assets_and_csp_allow_only_local_image_previews(self):
        status, html, headers = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn('src="/image-attachments.js"', html)
        self.assertIn('href="/image-attachments.css"', html)
        self.assertIn("img-src 'self' data:", headers["Content-Security-Policy"])
        self.assertEqual(self.request("/image-attachments.js")[0], 200)
        self.assertEqual(self.request("/image-attachments.css")[0], 200)
