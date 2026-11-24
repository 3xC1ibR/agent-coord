"""Transport contracts for fetching earlier turns and one unabridged turn."""
from unittest import TestCase
from unittest.mock import patch

import test_ui


class HistoryRouteTests(TestCase):
    setUp = test_ui.BrowserHTTPTests.setUp
    stop_server = test_ui.BrowserHTTPTests.stop_server
    request = test_ui.BrowserHTTPTests.request

    def test_page_and_full_turn_preserve_identifiers_and_query_only_access(self):
        def page(thread, *, before_seq, limit):
            self.assertTrue(self.store._connection_options.reading)
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

    def test_invalid_cursors_never_read_or_resume_provider(self):
        with patch.object(self.sessions, "read_history_window", create=True) as read:
            for query in ("limit=0", "limit=201", "limit=1&limit=2", "before_seq=-3", "before_seq=", "before_seq=999999999999999999999"):
                with self.subTest(query=query):
                    self.assertEqual(self.request("/api/browser/sessions/any?" + query)[0], 400)
            read.assert_not_called()
