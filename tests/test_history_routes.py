"""Transport contracts for fetching earlier turns and one unabridged turn."""
from unittest import TestCase
from unittest.mock import patch

import test_ui
from agent_coord.transcript_store import TranscriptStore


class HistoryRouteTests(TestCase):
    setUp = test_ui.BrowserHTTPTests.setUp
    stop_server = test_ui.BrowserHTTPTests.stop_server
    request = test_ui.BrowserHTTPTests.request

    def test_page_and_full_turn_preserve_identifiers_and_query_only_access(self):
        def page(thread, *, before_seq, limit, turn_id):
            self.assertTrue(self.store._connection_options.reading)
            self.assertIsNone(turn_id)
            self.assertEqual((thread, before_seq, limit), ("thread/a", 20, 4))
            return {"id": thread, "turns": [], "beforeSeq": 16}

        with patch.object(self.sessions, "read_history_window", create=True, side_effect=page):
            status, result = self.request("/api/browser/sessions/thread%2Fa?before_seq=20&limit=4")
            self.assertEqual((status, result["thread"]["beforeSeq"]), (200, 16))
        with patch.object(self.sessions, "read_history_turn", create=True, return_value={"id": "001/2"}) as full:
            status, result = self.request("/api/browser/sessions/thread%2Fa/turns/001%2F2/full")
            self.assertEqual((status, result["turn"]["id"]), (200, "001/2"))
            full.assert_called_once_with("thread/a", "001/2")
        self.assertEqual(self.request("/api/browser/config")[1]["conversationHistoryPages"], 1)

    def test_turn_target_page_preserves_identifier_and_reports_missing_target(self):
        with patch.object(self.sessions, "read_history_window", create=True, return_value=None) as read:
            status, _ = self.request("/api/browser/sessions/abc?turn_id=p%2F%2B9")
            self.assertEqual(status, 404)
            read.assert_called_once_with("abc", before_seq=None, limit=40, turn_id="p/+9")

    def test_indexed_page_full_turn_and_workspace_boundary(self):
        identity = self.sessions.create({})["session"]["thread_id"]
        turns = [{"id": str(n), "status": "completed", "items": [
            {"type": "commandExecution", "id": "command", "output": "λ" * 2048}]}
            for n in range(45)]
        TranscriptStore(self.store).save(identity, {"id": identity, "turns": turns}, activate=True)
        path = "/api/browser/sessions/" + identity
        with patch.object(self.sessions.rpc, "request", side_effect=AssertionError("provider resumed")):
            code, page = self.request(path + "?turn_id=8&limit=4")
            self.assertEqual(code, 200)
            self.assertEqual([t["id"] for t in page["thread"]["turns"]], ["5", "6", "7", "8"])
            self.assertEqual(page["thread"]["beforeSeq"], 5)
            self.assertTrue(page["thread"]["turns"][0]["items"][0]["abridged"])
            code, detail = self.request(path + "/turns/5/full")
            self.assertEqual((code, detail["turn"]), (200, turns[5]))
            self.assertEqual(self.request(path + "?turn_id=absent")[0], 404)
            with self.store._connection() as db:
                db.execute("UPDATE browser_sessions SET cwd=? WHERE thread_id=?", (str(self.root.parent), identity))
            for route in (path + "?limit=4", path + "/turns/5/full"):
                code, result = self.request(route)
                self.assertEqual(code, 400)
                self.assertNotIn("thread", result)
                self.assertNotIn("turn", result)

    def test_invalid_cursors_never_read_or_resume_provider(self):
        with patch.object(self.sessions, "read_history_window", create=True) as read:
            for query in ("limit=0", "limit=201", "limit=1&limit=2", "before_seq=-3", "before_seq=",
                          "before_seq=999999999999999999999", "turn_id=t&before_seq=8", "turn_id=",
                          "turn_id=t&turn_id=u"):
                with self.subTest(query=query):
                    self.assertEqual(self.request("/api/browser/sessions/any?" + query)[0], 400)
            read.assert_not_called()
