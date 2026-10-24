"""Decision layer: durable asks, answer delivery, CLI/MCP surfaces, attention, and the UI endpoint."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_SCRIPTS = ROOT / "plugins/agent-coord/scripts"
sys.path.insert(0, str(PLUGIN_SCRIPTS))

from agent_coord.checkpoint_mcp import ask_human, dispatch
from agent_coord.cli import _parser
from agent_coord.cli import run as cli_run
from agent_coord.store import CoordinationError, CoordinationStore


class AskStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.now = 1700000000.0
        self.store = CoordinationStore(self.root / "state.sqlite3", clock=lambda: self.now)
        for name in ("asker", "peer"):
            self.store.register(session_id=name, client="claude", cwd=str(self.root), name=name.title())

    def test_create_validates_mode_context_and_options(self):
        with self.assertRaisesRegex(CoordinationError, "blocking"):
            self.store.asks.create("asker", "Which?", blocking=None)
        with self.assertRaisesRegex(CoordinationError, "default"):
            self.store.asks.create("asker", "Which?", blocking=False)
        with self.assertRaisesRegex(CoordinationError, "at least two"):
            self.store.asks.create("asker", "Which?", blocking=True, options=["Only"])
        with self.assertRaisesRegex(CoordinationError, "Duplicate"):
            self.store.asks.create("asker", "Which?", blocking=True, options=["A", "A"])
        with self.assertRaisesRegex(CoordinationError, "empty"):
            self.store.asks.create("asker", "   ", blocking=True)
        with self.assertRaises(CoordinationError):
            self.store.asks.create("unknown", "Which?", blocking=True)
        ask = self.store.asks.create("asker", " Which format? ", blocking=False, options=["CSV", "JSON"],
                                     context="Default: CSV.")
        self.assertEqual(ask["question"], "Which format?")
        self.assertEqual(ask["status"], "open")
        self.assertFalse(ask["blocking"])
        self.assertEqual(ask["options"], ["CSV", "JSON"])
        self.assertEqual(self.store.inbox("asker", mark_delivered=False), [])

    def test_answer_delivers_self_addressed_message_and_closes_the_ask(self):
        ask = self.store.asks.create("asker", "Which format?", blocking=True, options=["CSV", "JSON"])
        with self.assertRaisesRegex(CoordinationError, "not one of"):
            self.store.asks.answer(ask["id"], option="XML")
        with self.assertRaisesRegex(CoordinationError, "exactly one"):
            self.store.asks.answer(ask["id"], option="CSV", answer="CSV")
        answered = self.store.asks.answer(ask["id"], option="JSON")
        self.assertEqual(answered["status"], "answered")
        self.assertEqual(answered["answer"], "JSON")
        messages = self.store.inbox("asker", mark_delivered=False)
        self.assertEqual(len(messages), 1)
        message = messages[0]
        self.assertEqual(message["id"], answered["answer_message_id"])
        self.assertEqual(message["thread_id"], f"ask:{ask['id']}")
        self.assertEqual(message["classification"], "action_required")
        self.assertFalse(message["reply_required"])
        self.assertIn("Answer: JSON", message["body"])
        self.assertIn("Which format?", message["body"])
        with self.assertRaisesRegex(CoordinationError, "already answered"):
            self.store.asks.answer(ask["id"], answer="again")
        free = self.store.asks.create("asker", "Name?", blocking=False, context="Default: untitled.")
        self.assertEqual(self.store.asks.answer(free["id"], answer="  Ribbon  ")["answer"], "Ribbon")
        self.assertEqual(self.store.asks.list(status="open"), [])
        self.assertEqual([a["id"] for a in self.store.asks.list(status="answered", session_id="asker")], [ask["id"], free["id"]])

    def test_withdraw_and_supersede_are_owner_only(self):
        ask = self.store.asks.create("asker", "Which?", blocking=True)
        with self.assertRaisesRegex(CoordinationError, "not yours"):
            self.store.asks.withdraw(ask["id"], "peer")
        with self.assertRaisesRegex(CoordinationError, "not yours"):
            self.store.asks.create("peer", "Replacement?", blocking=True, supersedes=ask["id"])
        replacement = self.store.asks.create("asker", "Replacement?", blocking=True, supersedes=ask["id"])
        self.assertEqual(replacement["supersedes"], ask["id"])
        self.assertEqual(self.store.asks.get(ask["id"])["status"], "withdrawn")
        with self.assertRaisesRegex(CoordinationError, "already withdrawn"):
            self.store.asks.answer(ask["id"], answer="late")
        self.assertEqual(self.store.asks.withdraw(replacement["id"], "asker")["status"], "withdrawn")
        self.assertEqual(self.store.asks.withdraw(replacement["id"], "asker")["status"], "withdrawn")
        self.assertEqual(self.store.inbox("asker", mark_delivered=False), [])

    def test_wait_returns_the_answer_and_acknowledges_its_message(self):
        ask = self.store.asks.create("asker", "Which?", blocking=True)
        ticks = []
        def sleep(seconds):
            ticks.append(seconds)
            self.now += seconds
            if len(ticks) == 2:
                self.store.asks.answer(ask["id"], answer="Go")
        result = self.store.asks.wait(ask["id"], "asker", timeout_seconds=30, sleep=sleep)
        self.assertEqual((result["status"], result["answer"], result["timed_out"]), ("answered", "Go", False))
        self.assertEqual(self.store.inbox("asker", unread_only=True, mark_delivered=False), [])
        slow = self.store.asks.create("asker", "Later?", blocking=True)
        def expire(seconds):
            self.now += seconds
        timed = self.store.asks.wait(slow["id"], "asker", timeout_seconds=5, sleep=expire)
        self.assertTrue(timed["timed_out"])
        self.assertEqual(timed["status"], "open")
        with self.assertRaisesRegex(CoordinationError, "not yours"):
            self.store.asks.wait(slow["id"], "peer", timeout_seconds=1, sleep=expire)

    def test_overview_orders_blocking_first_with_thread_fields(self):
        self.store.asks.create("peer", "Non-blocking first created?", blocking=False, context="Default: yes.")
        self.now += 60
        blocking = self.store.asks.create("asker", "Blocking later?", blocking=True)
        self.now += 90
        overview = self.store.asks.overview()
        self.assertEqual([item["id"] for item in overview], [blocking["id"], blocking["id"] - 1])
        self.assertEqual(overview[0]["thread_title"], "Asker")
        self.assertEqual(overview[0]["age_seconds"], 90)
        self.assertEqual(overview[1]["age_seconds"], 150)
        self.assertEqual(overview[0]["cwd"], str(self.root))

    def test_cli_creates_lists_withdraws_and_rejects_ambiguous_usage(self):
        db = str(self.store.database_path)
        def run(*argv):
            return cli_run(_parser().parse_args(["--db", db, *argv]))
        with patch.dict(os.environ, {"AGENT_COORD_SESSION_ID": "asker"}, clear=False):
            with self.assertRaisesRegex(CoordinationError, "--blocking or --non-blocking"):
                run("ask", "Which?")
            with self.assertRaisesRegex(CoordinationError, "--wait applies"):
                run("ask", "--non-blocking", "--context", "Default: A.", "--wait", "1", "Which?")
            with self.assertRaisesRegex(CoordinationError, "exactly one"):
                run("ask", "--list", "Which?")
            for timeout in ("-1", "nan", "inf"):
                with self.assertRaisesRegex(CoordinationError, "finite, non-negative"):
                    run("ask", "--blocking", "--wait", timeout, "Which?")
            self.assertEqual(run("ask", "--list"), [])
            created = run("ask", "--non-blocking", "--context", "Default: A.", "--option", "A", "--option", "B", "Which?")
            self.assertEqual((created["status"], created["options"], created["blocking"]), ("open", ["A", "B"], False))
            self.assertEqual([a["id"] for a in run("ask", "--list")], [created["id"]])
            self.assertEqual(run("ask", "--withdraw", str(created["id"]))["status"], "withdrawn")
            self.assertEqual(run("ask", "--list"), [])
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(CoordinationError, "No caller session"):
                run("ask", "--blocking", "Which?")

    def test_mcp_tool_creates_waits_and_is_discoverable(self):
        with patch.dict(os.environ, {"AGENT_COORD_SESSION_ID": "asker"}, clear=False):
            receipt = ask_human({"question": "Which?", "blocking": False, "context": "Default: A.", "options": ["A", "B"]},
                                store=self.store)
            self.assertEqual(receipt, {"status": "ok", "ask_id": receipt["ask_id"], "state": "open"})
            with self.assertRaisesRegex(CoordinationError, "default"):
                ask_human({"question": "Which?", "blocking": False}, store=self.store)
            with self.assertRaisesRegex(CoordinationError, "blocking asks only"):
                ask_human({"question": "Which?", "blocking": False, "context": "x", "wait_seconds": 5}, store=self.store)
            with self.assertRaisesRegex(CoordinationError, "input fields"):
                ask_human({"question": "Which?", "blocking": True, "extra": 1}, store=self.store)
            blocking = ask_human({"question": "Go?", "blocking": True}, store=self.store)
            self.store.asks.answer(blocking["ask_id"], answer="Yes")
            original_wait = self.store.asks.wait
            def expire(seconds):
                self.now += seconds
            def wait(*args, **kwargs):
                return original_wait(*args, **kwargs, sleep=expire)
            with patch.object(self.store.asks, "wait", side_effect=wait):
                waited = ask_human({"question": "Already?", "blocking": True, "wait_seconds": 1}, store=self.store)
            self.assertEqual(waited["state"], "open")
            self.assertIn("End your turn", waited["note"])
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(CoordinationError, "No caller session"):
                ask_human({"question": "Which?", "blocking": True}, store=self.store)
        catalog = dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
        self.assertEqual([tool["name"] for tool in catalog], ["update_checkpoint", "move_thread", "ask_human"])
        self.assertEqual(catalog[2]["inputSchema"]["required"], ["question", "blocking"])

    def test_mcp_stdio_call_uses_inherited_identity(self):
        config = json.loads((ROOT / "plugins/agent-coord/.mcp.json").read_text())["mcpServers"]["agent-coord"]
        command = [config["command"], *[arg.replace("${CLAUDE_PLUGIN_ROOT}", str(ROOT / "plugins/agent-coord")) for arg in config["args"]]]
        messages = [{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "ask_human", "arguments": {
            "question": "Ship it?", "blocking": False, "context": "Default: yes."}}}]
        environment = {**os.environ, "AGENT_COORD_DB": str(self.store.database_path), "AGENT_COORD_SESSION_ID": "asker"}
        result = subprocess.run(command, input="\n".join(map(json.dumps, messages)) + "\n", cwd=self.root,
                                env=environment, text=True, capture_output=True, timeout=20, check=True)
        receipt = json.loads(json.loads(result.stdout.splitlines()[0])["result"]["content"][0]["text"])
        self.assertEqual(receipt["state"], "open")
        self.assertEqual(self.store.asks.get(receipt["ask_id"])["session_id"], "asker")


class AskAttentionTests(unittest.TestCase):
    def setUp(self):
        from test_codex_app_server import BrowserSessions, FakeCodex
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.now = 1700000000.0
        self.store = CoordinationStore(self.root / "state.sqlite3", clock=lambda: self.now)
        self.sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(self.sessions.close)
        self.thread = self.sessions.create({"name": "Checkout", "cwd": str(self.root)})["session"]["thread_id"]
        self.store.touch(self.thread, turn_active=False)

    def thread_view(self, *, inventory):
        if inventory:
            return next(t for t in self.sessions.list_work_threads() if t["thread_id"] == self.thread)
        return self.sessions.work_thread(self.thread, history=False)

    def test_open_blocking_ask_marks_thread_as_needing_the_user_until_answered(self):
        self.assertFalse(self.thread_view(inventory=False)["needs_attention"])
        quiet = self.store.asks.create(self.thread, "Non-blocking?", blocking=False, context="Default: proceed.")
        self.assertFalse(self.thread_view(inventory=True)["needs_attention"])
        ask = self.store.asks.create(self.thread, "Which account?", blocking=True)
        for inventory in (False, True):
            view = self.thread_view(inventory=inventory)
            self.assertTrue(view["needs_attention"], inventory)
            self.assertEqual(view["attention_reason"], "blocked")
            self.assertEqual(view["attention_key"], f"ask:{ask['id']}")
            self.assertEqual(view["attention_action"], "Decision needed: Which account?")
            self.assertEqual(view["open_ask"]["id"], ask["id"])
            self.assertEqual(view["response_state"], "input")
        notifications = self.sessions.approval_notifications()
        self.assertEqual([n["request_key"] for n in notifications], [f"ask:{ask['id']}"])
        self.assertEqual((notifications[0]["status"], notifications[0]["thread_id"]), ("question", self.thread))
        self.assertTrue(self.sessions.claim_approval_notification(f"ask:{ask['id']}"))
        self.assertFalse(self.sessions.claim_approval_notification(f"ask:{ask['id']}"))
        self.assertEqual(self.sessions.approval_notifications(), [])
        self.assertEqual(len(self.sessions.approval_notifications(include_claimed=True)), 1)
        self.store.asks.answer(ask["id"], answer="Staging")
        view = self.thread_view(inventory=True)
        self.assertNotEqual(view["attention_key"], f"ask:{ask['id']}")
        self.assertIsNone(view["open_ask"])
        self.assertEqual(self.sessions.approval_notifications(include_claimed=True), [])
        self.store.asks.withdraw(quiet["id"], self.thread)
        self.assertFalse(self.sessions.claim_approval_notification("ask:999"))

    def test_closed_or_snoozed_threads_do_not_alert_for_open_asks(self):
        ask = self.store.asks.create(self.thread, "Which?", blocking=True)
        self.sessions.update_work_thread(self.thread, {"attention": "archived"})
        view = self.thread_view(inventory=False)
        self.assertFalse(view["needs_attention"])
        self.assertEqual(self.sessions.approval_notifications(), [])
        self.assertFalse(self.sessions.claim_approval_notification(f"ask:{ask['id']}"))
        self.assertEqual([item["id"] for item in self.store.asks.overview()], [ask["id"]])

    def test_http_endpoint_lists_and_answers_asks(self):
        from agent_coord.ui import make_ui_server
        server = make_ui_server(self.store, host="127.0.0.1", port=0, cwd=str(self.root), browser_sessions=self.sessions)
        for component in (server.app_control, self.sessions.inbox_wake, self.sessions.sidecars):
            component.close()
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base = "http://127.0.0.1:" + str(server.server_address[1])
        def get(path):
            with urllib.request.urlopen(base + path) as response:
                return json.load(response)
        token = get("/api/browser/config")["token"]
        def post(path, body, auth=token):
            request = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                                             headers={"Content-Type": "application/json", "X-Agent-Coord-Token": auth})
            with urllib.request.urlopen(request) as response:
                return json.load(response)
        try:
            self.store.register(session_id="terminal", client="codex", cwd="/elsewhere", name="Terminal worker")
            later = self.store.asks.create("terminal", "Non-blocking?", blocking=False, context="Default: A.", options=["A", "B"])
            self.now += 5
            blocking = self.store.asks.create(self.thread, "Blocking?", blocking=True)
            listed = get("/api/browser/asks")["data"]
            self.assertEqual([item["id"] for item in listed], [blocking["id"], later["id"]])
            self.assertEqual(listed[1]["thread_title"], "Terminal worker")
            answered = post("/api/browser/asks/" + str(later["id"]), {"option": "B"})
            self.assertEqual((answered["status"], answered["answer"]), ("answered", "B"))
            self.assertEqual(self.store.inbox("terminal", mark_delivered=False)[0]["thread_id"], f"ask:{later['id']}")
            free = post("/api/browser/asks/" + str(blocking["id"]), {"answer": "Use staging"})
            self.assertEqual(free["answer"], "Use staging")
            self.assertEqual(get("/api/browser/asks")["data"], [])
            for body, path in (({"answer": "x", "option": "A"}, str(blocking["id"])), ({"answer": "x"}, "nope"),
                               ({"answer": "x"}, str(blocking["id"]))):
                with self.assertRaises(urllib.error.HTTPError) as rejected:
                    post("/api/browser/asks/" + path, body)
                self.assertEqual(rejected.exception.code, 400)
            with self.assertRaises(urllib.error.HTTPError) as forbidden:
                post("/api/browser/asks/" + str(blocking["id"]), {"answer": "x"}, auth="wrong")
            self.assertEqual(forbidden.exception.code, 403)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)

    def test_asks_assets_are_served(self):
        from agent_coord.ui import make_ui_server
        server = make_ui_server(self.store, host="127.0.0.1", port=0, cwd=str(self.root), browser_sessions=self.sessions)
        for component in (server.app_control, self.sessions.inbox_wake, self.sessions.sidecars):
            component.close()
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base = "http://127.0.0.1:" + str(server.server_address[1])
        try:
            for path, needle in (("/asks.js", b"class AsksUI"), ("/asks.css", b".asks-count"), ("/", b'id="open-asks"')):
                with urllib.request.urlopen(base + path) as response:
                    self.assertIn(needle, response.read(), path)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)


class AskWebTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is required for browser tests")
    def test_decisions_dialog_behaviour(self):
        result = subprocess.run(["node", "--test", "tests/test_web_asks.js"], cwd=ROOT,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
