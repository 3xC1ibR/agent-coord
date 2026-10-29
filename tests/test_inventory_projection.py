from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))

from agent_coord.codex_app_server import BrowserSessions
from agent_coord.inventory_projection import ORIGINAL_REQUEST_PREVIEW, entity_tag, list_card
from agent_coord.store import CoordinationStore
from agent_coord.ui import make_ui_server
from test_codex_app_server import FakeCodex


class InventoryProjectionTests(unittest.TestCase):
    def thread(self, **values):
        return {"thread_id": "one", "title": "Work", "original_request": "Short request",
                "links": [{"id": 1, "kind": "document", "target": "docs/a.md"}],
                "checkpoint": {"id": 7, "summary": "Done", "next_actor": "nobody",
                               "context_lifecycle": {"recommendation": "reuse", "handover": {"confirmed_facts": ["x" * 400]}}},
                **values}

    def test_card_keeps_list_fields_and_drops_detail_only_fields(self):
        original = self.thread()
        card = list_card(original)
        self.assertEqual(card["link_count"], 1)
        self.assertNotIn("links", card)
        self.assertEqual(card["checkpoint"], {"id": 7, "summary": "Done", "next_actor": "nobody"})
        self.assertEqual(card["original_request"], "Short request")
        self.assertNotIn("original_request_truncated", card)
        self.assertIn("links", original, "the inventory's own record is not mutated")
        self.assertIn("context_lifecycle", original["checkpoint"])

    def test_long_requests_keep_a_marked_preview(self):
        card = list_card(self.thread(original_request="a" * (ORIGINAL_REQUEST_PREVIEW + 5)))
        self.assertEqual(len(card["original_request"]), ORIGINAL_REQUEST_PREVIEW)
        self.assertTrue(card["original_request_truncated"])

    def test_threads_without_optional_fields(self):
        card = list_card({"thread_id": "bare", "original_request": None, "checkpoint": None})
        self.assertEqual(card, {"thread_id": "bare", "original_request": None, "checkpoint": None, "link_count": 0})

    def test_entity_tag_is_a_quoted_content_validator(self):
        self.assertEqual(entity_tag(b"one"), entity_tag(b"one"))
        self.assertNotEqual(entity_tag(b"one"), entity_tag(b"two"))
        self.assertRegex(entity_tag(b"one"), r'^"[0-9a-f]{32}"$')


class InventoryTransferTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = CoordinationStore(self.root / "state.sqlite3")
        self.sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.server = make_ui_server(self.store, port=0, cwd=str(self.root), browser_sessions=self.sessions)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self.stop)
        self.url = "http://127.0.0.1:" + str(self.server.server_address[1])
        self.thread = self.sessions.create({"name": "Linked work"})["session"]["thread_id"]
        self.store.threads.capture_request(self.thread, "Request " + "x" * 2000)
        self.store.threads.add_link(self.thread, {"kind": "document", "label": "Notes", "target": str(self.root / "notes.md")})
        self.sessions.inventory_changed()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(2)

    def get(self, path, headers=None):
        try:
            response = urllib.request.urlopen(urllib.request.Request(self.url + path, headers=headers or {}), timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, response.headers.get("ETag"), response.read()

    def test_list_sends_cards_with_a_validator_and_detail_keeps_the_full_record(self):
        status, tag, body = self.get("/api/browser/threads")
        self.assertEqual(status, 200)
        self.assertRegex(tag, r'^"[0-9a-f]{32}"$')
        card = next(item for item in json.loads(body)["data"] if item["thread_id"] == self.thread)
        self.assertEqual(card["link_count"], 1)
        self.assertNotIn("links", card)
        self.assertTrue(card["original_request_truncated"])
        detail = json.loads(self.get("/api/browser/threads/" + self.thread)[2])
        self.assertEqual(detail["links"][0]["label"], "Notes")
        self.assertEqual(len(detail["original_request"]), len("Request ") + 2000)

    def test_an_unchanged_list_is_not_resent(self):
        _, tag, _ = self.get("/api/browser/threads")
        status, same, body = self.get("/api/browser/threads", {"If-None-Match": tag})
        self.assertEqual((status, same, body), (304, tag, b""))
        self.store.threads.update(self.thread, title="Renamed")
        self.sessions.inventory_changed()
        status, changed, body = self.get("/api/browser/threads", {"If-None-Match": tag})
        self.assertEqual(status, 200)
        self.assertNotEqual(changed, tag)
        self.assertIn(b"Renamed", body)

    def test_open_and_closed_lists_have_separate_validators(self):
        _, open_tag, _ = self.get("/api/browser/threads")
        status, closed_tag, _ = self.get("/api/browser/threads?archived=true", {"If-None-Match": open_tag})
        self.assertEqual(status, 200)
        self.assertNotEqual(closed_tag, open_tag)


if __name__ == "__main__":
    unittest.main()
