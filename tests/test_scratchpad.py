from __future__ import annotations

import unittest
from urllib.parse import urlencode

import test_ui
from agent_coord.store import CoordinationStore


class ScratchpadTests(unittest.TestCase):
    setUp = test_ui.BrowserHTTPTests.setUp
    stop_server = test_ui.BrowserHTTPTests.stop_server
    request = test_ui.BrowserHTTPTests.request

    def thread(self, name, cwd=None):
        self.store.register(session_id=name, client="codex", cwd=str(cwd or self.root))
        return "/api/browser/threads/" + name + "/scratchpad"

    def test_notes_round_trip_clear_and_survive_restart_without_changing_thread(self):
        path = self.thread("one")
        other = self.thread("two")
        before = self.store.threads.get("one", history=True)
        self.assertEqual(self.request(path), (200, {"text": ""}))
        text = "  Notes\n\n<arbitrary> & text 📝\t\n"
        self.assertEqual(self.request(path, {"text": text}), (200, {"text": text}))
        self.assertEqual(self.request(path), (200, {"text": text}))
        self.assertEqual(self.request(other), (200, {"text": ""}))
        self.assertEqual(self.store.threads.get("one", history=True), before)
        reopened = CoordinationStore(self.store.database_path)
        self.assertEqual(reopened.threads.scratchpad("one"), {"text": text})
        self.assertEqual(self.request(path, {"text": ""}), (200, {"text": ""}))
        self.assertEqual(reopened.threads.scratchpad("one"), {"text": ""})

    def test_notes_remain_editable_when_thread_is_closed(self):
        path = self.thread("closed")
        self.store.threads.update("closed", attention="archived")
        self.assertEqual(self.request(path, {"text": "Keep this"})[0], 200)
        self.store.threads.update("closed", attention="now")
        self.assertEqual(self.request(path)[1], {"text": "Keep this"})

    def test_validation_authentication_and_workspace_boundaries(self):
        path = self.thread("one")
        self.request(path, {"text": "Original"})
        for body in ({}, {"text": None}, {"text": 1}, {"text": []},
                     {"text": "x", "title": "No titles"}, {"text": "x" * 100_001}):
            with self.subTest(body_type=type(body.get("text"))):
                self.assertEqual(self.request(path, body)[0], 400)
        self.assertEqual(self.request(path, {"text": "x"}, {"X-Agent-Coord-Token": "invalid"})[0], 403)
        outside = self.thread("outside", self.root.parent)
        for body in (None, {"text": "x"}):
            self.assertEqual(self.request(outside, body)[0], 400)
            self.assertEqual(self.request("/api/browser/threads/missing/scratchpad", body)[0], 400)
        self.assertEqual(self.request(path)[1], {"text": "Original"})

    def test_existing_databases_gain_scratchpad_storage(self):
        self.thread("existing")
        with self.store._connection() as db:
            db.execute("DROP TABLE thread_scratchpads")
        reopened = CoordinationStore(self.store.database_path)
        self.assertEqual(reopened.threads.scratchpad("existing"), {"text": ""})
        self.assertEqual(reopened.threads.scratchpad("existing", "New note"), {"text": "New note"})

    def test_project_notes_are_shared_across_threads_but_isolated_from_other_projects(self):
        endpoint = "/api/browser/workspaces/scratchpad"
        path = str(self.root)
        read = endpoint + "?" + urlencode({"path": path})
        self.assertEqual(self.request(read), (200, {"text": ""}))
        self.assertEqual(self.store.threads.list(), [])
        with self.store._connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM project_scratchpads").fetchone()[0], 0)
        text = "  Follow up\n\nAcross conversations 📝\n"
        self.assertEqual(self.request(endpoint, {"path": path, "text": text}), (200, {"text": text}))
        self.thread("one"); self.thread("two")
        self.assertEqual(self.request(read), (200, {"text": text}))
        self.assertEqual(self.store.threads.scratchpad("one"), {"text": ""})
        other = self.root / "other"
        other.mkdir()
        self.assertEqual(self.request(endpoint + "?" + urlencode({"path": str(other)}))[1], {"text": ""})
        # Equivalent paths identify the same project, and a restart keeps its note.
        alias = self.root / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        self.assertEqual(self.request(endpoint + "?" + urlencode({"path": str(alias)}))[1], {"text": text})
        reopened = CoordinationStore(self.store.database_path)
        self.assertEqual(reopened.threads.project_scratchpad(path), {"text": text})
        self.request(endpoint, {"path": path, "text": ""})
        self.assertEqual(reopened.threads.project_scratchpad(path), {"text": ""})

    def test_project_notes_validate_text_paths_and_authentication(self):
        endpoint = "/api/browser/workspaces/scratchpad"
        for path in (None, "", "relative", str(self.root.parent), str(self.root / "missing")):
            self.assertEqual(self.request(endpoint + "?" + urlencode({"path": path or ""}))[0], 400)
            self.assertEqual(self.request(endpoint, {"path": path, "text": "note"})[0], 400)
        for text in (None, 2, [], "x" * 100_001):
            self.assertEqual(self.request(endpoint, {"path": str(self.root), "text": text})[0], 400)
        self.assertEqual(self.request(endpoint, {"path": str(self.root)})[0], 400)
        body = {"path": str(self.root), "text": "note"}
        self.assertEqual(self.request(endpoint, {**body, "title": "Not supported"})[0], 400)
        self.assertEqual(self.request(endpoint, body, {"X-Agent-Coord-Token": "invalid"})[0], 403)

    def test_existing_database_gains_project_notes_without_changing_thread_notes(self):
        self.thread("existing")
        self.store.threads.scratchpad("existing", "Thread note")
        with self.store._connection() as db:
            db.execute("DROP TABLE project_scratchpads")
        reopened = CoordinationStore(self.store.database_path)
        self.assertEqual(reopened.threads.project_scratchpad(str(self.root)), {"text": ""})
        self.assertEqual(reopened.threads.scratchpad("existing"), {"text": "Thread note"})


if __name__ == "__main__":
    unittest.main()
