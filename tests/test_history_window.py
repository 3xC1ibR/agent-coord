from __future__ import annotations

import copy
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
from agent_coord.history_window import PREVIEW_CHARACTERS, PREVIEW_ENTRIES, RECENT_TURNS, recent_view
from agent_coord.store import CoordinationStore
from agent_coord.ui import make_ui_server
from test_codex_app_server import FakeCodex


def history(count, *, status="completed", output_size=2000):
    return [{"id": f"turn-{index}", "status": status, "items": [
        {"id": f"user-{index}", "type": "userMessage", "content": [{"type": "text", "text": "Request " + "r" * 900}]},
        {"id": f"command-{index}", "type": "commandExecution", "command": "rg pattern", "status": "completed",
         "aggregatedOutput": "o" * output_size, "commandActions": [{"type": "unknown", "command": "rg"}] * 15},
        {"id": f"tool-{index}", "type": "mcpToolCall", "tool": "read", "status": "completed",
         "result": {"content": [{"type": "text", "text": "t" * output_size}]}},
        {"id": f"answer-{index}", "type": "agentMessage", "text": "Answer " + "a" * 900},
    ]} for index in range(count)]


class RecentViewTests(unittest.TestCase):
    def test_keeps_the_latest_turns_and_counts_the_rest(self):
        thread = {"id": "one", "status": {"type": "idle"}, "historyUnavailable": False, "turns": history(RECENT_TURNS + 5)}
        view = recent_view(thread)
        self.assertEqual([turn["id"] for turn in view["turns"]], [f"turn-{index}" for index in range(5, RECENT_TURNS + 5)])
        self.assertEqual((view["earlierTurns"], view["view"], view["historyUnavailable"]), (5, "recent", False))

    def test_shortens_tool_fields_and_keeps_the_conversation_complete(self):
        user, command, tool, answer = recent_view({"id": "one", "turns": history(1)})["turns"][0]["items"]
        self.assertEqual(len(user["content"][0]["text"]), len("Request ") + 900)
        self.assertEqual(len(answer["text"]), len("Answer ") + 900)
        self.assertNotIn("abridged", user)
        self.assertEqual(command["aggregatedOutput"], "o" * PREVIEW_CHARACTERS)
        self.assertEqual(len(command["commandActions"]), PREVIEW_ENTRIES)
        self.assertEqual(tool["result"]["content"][0]["text"], "t" * PREVIEW_CHARACTERS)
        self.assertEqual((command["id"], command["type"], command["status"], command["command"]),
                         ("command-0", "commandExecution", "completed", "rg pattern"))
        self.assertTrue(command["abridged"] and tool["abridged"])

    def test_short_tool_calls_are_unmarked(self):
        command = recent_view({"id": "one", "turns": history(1, output_size=10)})["turns"][0]["items"][2]
        self.assertNotIn("abridged", command)

    def test_a_running_turn_stays_complete(self):
        turns = history(2)
        turns[-1]["status"] = "inProgress"
        view = recent_view({"id": "one", "turns": copy.deepcopy(turns)})
        self.assertEqual(view["turns"][-1], turns[-1])
        self.assertTrue(view["turns"][0]["items"][1]["abridged"])
        live = recent_view({"id": "one", "turns": copy.deepcopy(history(2))}, live_turn="turn-0")
        self.assertEqual(live["turns"][0], history(2)[0])

    def test_the_source_history_is_not_changed(self):
        thread = {"id": "one", "turns": history(RECENT_TURNS + 2)}
        original = copy.deepcopy(thread)
        recent_view(thread)
        self.assertEqual(thread, original)


class RecentViewTransferTests(unittest.TestCase):
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
        self.thread = self.sessions.create({"name": "Long work"})["session"]["thread_id"]
        self.sessions.rpc.threads[self.thread]["turns"] = history(RECENT_TURNS + 3, output_size=20000)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(2)
        self.sessions.close()

    def get(self, view=None):
        headers = {"X-Agent-Coord-History-View": view} if view else {}
        try:
            response = urllib.request.urlopen(urllib.request.Request(
                self.url + "/api/browser/sessions/" + self.thread, headers=headers), timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            body = response.read()
            return response.status, len(body), json.loads(body)

    def mirror(self):
        with self.store._connection() as connection:
            return json.loads(connection.execute("SELECT history_json FROM browser_history WHERE thread_id = ?",
                                                 (self.thread,)).fetchone()[0])

    def test_opening_transfers_the_recent_view_and_the_complete_history_stays_available(self):
        status, complete_size, complete = self.get()
        self.assertEqual(status, 200)
        self.assertEqual(len(complete["thread"]["turns"]), RECENT_TURNS + 3)
        self.assertNotIn("view", complete["thread"])
        saved = self.mirror()
        status, recent_size, recent = self.get("recent")
        self.assertEqual(status, 200)
        self.assertEqual((len(recent["thread"]["turns"]), recent["thread"]["earlierTurns"]), (RECENT_TURNS, 3))
        self.assertTrue(recent["thread"]["turns"][0]["items"][1]["abridged"])
        self.assertLess(recent_size, complete_size / 5)
        self.assertEqual(self.mirror(), saved, "the saved history is never replaced by a recent view")
        self.assertEqual(self.get()[2]["thread"]["turns"], complete["thread"]["turns"])
        self.assertEqual(self.get("complete")[2]["thread"]["turns"], complete["thread"]["turns"])

    def test_an_unknown_view_is_rejected(self):
        status, _, body = self.get("partial")
        self.assertEqual(status, 400)
        self.assertIn("complete or recent", body["error"])


if __name__ == "__main__":
    unittest.main()
