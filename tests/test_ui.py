from __future__ import annotations

import errno
import json
import sys
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from http import HTTPStatus
from pathlib import Path
from unittest.mock import MagicMock, patch

PLUGIN_SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"
sys.path.insert(0, str(PLUGIN_SCRIPTS))

from agent_coord.managed_pty import output_log_path
from agent_coord.store import CoordinationError, CoordinationStore
from agent_coord.ui import _handler, build_snapshot, make_ui_server
from agent_coord.codex_app_server import BrowserSessions
from test_codex_app_server import FakeCodex


class OperatorUITests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.now = 1_700_000_000.0
        self.store = CoordinationStore(
            self.root / "state.sqlite3", clock=lambda: self.now
        )
        self.store.register(
            session_id="parent", client="codex", cwd=str(self.root), name="release"
        )
        self.store.register(
            session_id="child", client="claude", cwd=str(self.root), name="validator"
        )
        self.store.create_delegation(
            parent_session_id="parent",
            cwd=str(self.root),
            bead_id="work-a",
            scopes=["tests/**"],
            instructions="Validate the release.",
            mode="reviewed",
            client="claude",
            runtime_kind="managed-pty",
            name="release-validator",
            delegation_id="delegation-a",
        )
        child_log = output_log_path(self.store, "delegation-a")
        self.store.mark_delegation_launched(
            "delegation-a",
            runtime_kind="managed-pty",
            supervisor_pid=700,
            output_log_path=str(child_log),
        )
        self.store.set_delegation_child_process(
            "delegation-a",
            supervisor_pid=700,
            child_pid=701,
            output_log_path=str(child_log),
        )
        self.store.attach_delegation("delegation-a", "child")
        child_log.write_text("focused tests passed\n")

    def add_second_child(self) -> None:
        self.now += 10
        self.store.register(
            session_id="child-alpha",
            client="codex",
            cwd=str(self.root),
            name="alpha-worker",
        )
        self.store.create_delegation(
            parent_session_id="parent",
            cwd=str(self.root),
            bead_id="work-b",
            scopes=["src/**"],
            instructions="Implement work-b.",
            mode="reviewed",
            runtime_kind="managed-pty",
            name="alpha-worker",
            delegation_id="delegation-b",
        )
        self.store.attach_delegation("delegation-b", "child-alpha")

    def test_snapshot_contains_parent_child_status_and_output(self) -> None:
        snapshot = build_snapshot(self.store, parent_session_id="parent")

        self.assertEqual(snapshot["process_count"], 2)
        parent = snapshot["parents"][0]
        self.assertEqual(parent["session_id"], "parent")
        child = parent["children"][0]
        self.assertEqual(child["child_session_id"], "child")
        self.assertEqual(child["runtime_kind"], "managed-pty")
        self.assertEqual(child["name"], "release-validator")
        self.assertIn("focused tests passed", child["output"])
        self.assertEqual(snapshot["sort_by"], "last_activity")

    def test_snapshot_persists_zellij_output_for_later_ui_restarts(self) -> None:
        self.store.register(
            session_id="zellij-child",
            client="codex",
            cwd=str(self.root),
            name="zellij-validator",
        )
        self.store.create_delegation(
            parent_session_id="parent",
            cwd=str(self.root),
            bead_id="work-zellij",
            scopes=["README.md"],
            instructions="Validate in Zellij.",
            mode="reviewed",
            runtime_kind="zellij",
            delegation_id="delegation-zellij",
        )
        self.store.mark_delegation_launched(
            "delegation-zellij",
            runtime_kind="zellij",
            zellij_session="test-session",
            pane_id="terminal_42",
        )
        self.store.attach_delegation("delegation-zellij", "zellij-child")
        with (
            patch(
                "agent_coord.managed_pty.shutil.which",
                return_value="/mock/zellij",
            ),
            patch(
                "agent_coord.managed_pty.ZellijClient.dump_screen",
                return_value="Persisted Zellij output\n",
            ),
        ):
            first = build_snapshot(self.store, parent_session_id="parent")

        reopened = CoordinationStore(self.store.database_path)
        with patch("agent_coord.managed_pty.shutil.which", return_value=None):
            second = build_snapshot(reopened, parent_session_id="parent")

        first_child = next(
            child
            for child in first["parents"][0]["children"]
            if child["delegation_id"] == "delegation-zellij"
        )
        second_child = next(
            child
            for child in second["parents"][0]["children"]
            if child["delegation_id"] == "delegation-zellij"
        )
        self.assertEqual(first_child["output"], "Persisted Zellij output")
        self.assertEqual(second_child["output"], first_child["output"])

    def test_snapshot_explains_missing_legacy_zellij_output(self) -> None:
        self.store.create_delegation(
            parent_session_id="parent",
            cwd=str(self.root),
            bead_id="work-legacy",
            scopes=["README.md"],
            instructions="Legacy Zellij work.",
            mode="reviewed",
            runtime_kind="zellij",
            delegation_id="delegation-legacy",
        )
        self.store.mark_delegation_launched(
            "delegation-legacy",
            runtime_kind="zellij",
            zellij_session="gone-session",
            pane_id="terminal_99",
        )
        self.store.cancel_delegation(
            "delegation-legacy",
            parent_session_id="parent",
            reason="Recovered final result.",
        )

        with patch("agent_coord.managed_pty.shutil.which", return_value=None):
            snapshot = build_snapshot(self.store, parent_session_id="parent")

        child = next(
            child
            for child in snapshot["parents"][0]["children"]
            if child["delegation_id"] == "delegation-legacy"
        )
        self.assertIn("No terminal snapshot", child["output"])
        self.assertIn("Recovered final result.", child["output"])

    def test_snapshot_filters_repository_and_sorts_tree(self) -> None:
        self.add_second_child()

        created = build_snapshot(
            self.store,
            cwd=str(self.root),
            sort_by="created",
        )
        by_name = build_snapshot(
            self.store,
            cwd=str(self.root),
            sort_by="name",
        )
        self.now += 10
        self.store.touch("child")
        by_activity = build_snapshot(self.store, cwd=str(self.root))

        self.assertEqual(created["repository"], str(self.root.resolve()))
        self.assertEqual(created["parents"][0]["children"][0]["name"], "alpha-worker")
        self.assertEqual(by_name["parents"][0]["children"][0]["name"], "alpha-worker")
        self.assertEqual(
            by_activity["parents"][0]["children"][0]["name"],
            "release-validator",
        )

        other = self.root / "other"
        other.mkdir()
        self.now += 10
        self.store.register(
            session_id="other-parent",
            client="codex",
            cwd=str(other),
            name="other",
        )
        self.store.create_delegation(
            parent_session_id="other-parent",
            cwd=str(other),
            bead_id="work-other",
            scopes=["src/**"],
            instructions="Other repository work.",
            mode="reviewed",
            delegation_id="delegation-other",
        )

        self.assertEqual(len(build_snapshot(self.store)["parents"]), 2)
        filtered = build_snapshot(self.store, cwd=str(self.root))
        self.assertEqual(
            {item["session_id"] for item in filtered["parents"]}, {"parent", "other-parent"}
        )
        filtered = build_snapshot(self.store, cwd=str(other))
        self.assertEqual(
            [item["session_id"] for item in filtered["parents"]], ["other-parent"]
        )

    def test_snapshot_includes_complete_message_history_without_mutation(self) -> None:
        first = self.store.send_message(
            sender_session_id="parent",
            recipient_session_id="child",
            body="First message.",
        )
        self.store.inbox("child")
        self.store.acknowledge("child", first["id"])
        second = self.store.send_message(
            sender_session_id="parent",
            recipient_session_id="child",
            body="Second message.",
        )

        snapshot = build_snapshot(self.store, parent_session_id="parent")
        child = snapshot["parents"][0]["children"][0]

        self.assertEqual(
            [message["id"] for message in child["messages"]],
            [first["id"], second["id"]],
        )
        self.assertEqual(child["unacknowledged_message_count"], 1)
        pending = self.store.inbox("child", mark_delivered=False)
        self.assertEqual([message["id"] for message in pending], [second["id"]])

    def test_http_ui_serves_shell_and_snapshot(self) -> None:
        server = make_ui_server(
            self.store,
            host="127.0.0.1",
            port=0,
            parent_session_id="parent",
            cwd=str(self.root),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address[:2]
        try:
            with urllib.request.urlopen(
                f"http://{host}:{port}/monitor", timeout=2
            ) as response:
                page = response.read().decode()
            with urllib.request.urlopen(
                f"http://{host}:{port}/api/snapshot", timeout=2
            ) as response:
                payload = json.loads(response.read())
        finally:
            server.shutdown()
            thread.join(2)
            server.server_close()

        self.assertIn("Process tree", page)
        self.assertIn('id="sort"', page)
        self.assertIn("child-card", page)
        self.assertEqual(payload["repository"], str(self.root.resolve()))
        self.assertEqual(payload["parents"][0]["children"][0]["bead_id"], "work-a")

    def test_expected_client_disconnect_does_not_escape_handler(self) -> None:
        handler_type = _handler(self.store, "parent", str(self.root))
        handler = object.__new__(handler_type)
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        handler.wfile = MagicMock()
        handler.wfile.write.side_effect = BrokenPipeError(errno.EPIPE, "closed")
        handler.close_connection = False

        handler._send(HTTPStatus.OK, "text/plain", b"response")

        self.assertTrue(handler.close_connection)

    def test_ui_rejects_non_loopback_binding(self) -> None:
        with self.assertRaisesRegex(CoordinationError, "only binds to loopback"):
            make_ui_server(self.store, host="0.0.0.0", port=0)

    def test_snapshot_includes_standalone_sessions(self) -> None:
        self.store.register(session_id="standalone", client="codex", cwd=str(self.root))
        ids = {item["session_id"] for item in build_snapshot(self.store)["parents"]}
        self.assertEqual(ids, {"parent", "standalone"})


class BrowserHTTPTests(unittest.TestCase):
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

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(2)

    def request(self, path, body=None, headers=None):
        request_headers = dict(headers or {})
        if body is not None:
            request_headers.setdefault("Content-Type", "application/json")
            request_headers.setdefault("X-Agent-Coord-Token", getattr(self, "token", ""))
        request = urllib.request.Request(self.url + path, data=json.dumps(body).encode() if body is not None else None, headers=request_headers)
        try:
            response = urllib.request.urlopen(request, timeout=2)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            content = response.read().decode()
            return response.status, json.loads(content) if response.headers.get_content_type() == "application/json" else content

    def test_session_model_commands_and_yolo_over_http(self):
        status, created = self.request("/api/browser/sessions", {"name": "Settings", "yolo": True})
        self.assertEqual(status, 201)
        path = "/api/browser/sessions/" + created["session"]["thread_id"]
        self.assertTrue(created["session"]["yolo"])
        self.assertEqual(created["session"]["model"], "available-model")
        status, changed = self.request(path + "/messages", {"message": "/model other-model low"})
        self.assertEqual(status, 200)
        self.assertIn("Next message", changed["command"]["message"])
        detail = self.request(path)[1]
        self.assertFalse(detail["running"])
        self.assertEqual(detail["thread"]["turns"], [])
        self.assertEqual((detail["session"]["model"], detail["session"]["effort"]), ("other-model", "low"))
        self.assertEqual(self.request(path + "/messages", {"message": "/effort unsupported"})[0], 400)
        self.assertEqual(self.request(path, {"model": "available-model", "effort": "high"})[0], 200)
        self.assertEqual(self.request(path + "/messages", {"message": "Begin"})[0], 200)
        self.assertEqual(self.request(path + "/messages", {"message": "/effort medium"})[0], 409)
        self.assertEqual(self.request("/api/browser/sessions", {"yolo": "true"})[0], 400)

    def test_browser_create_chat_interrupt_archive_and_restore(self):
        status, shell = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn("New session", shell)
        self.assertEqual(self.request("/app.js")[0], 200)
        self.assertEqual(self.request("/styles.css")[0], 200)
        self.assertEqual(self.request("/markdown.js")[0], 200)
        self.assertIn('src="/markdown.js"', shell)
        monitor = self.request("/monitor")[1]
        self.assertIn('src="/markdown.js"', monitor)
        self.assertIn("messageMarkdown.render(m.body)", monitor)
        status, result = self.request("/api/browser/sessions", {"name": "Browser workflow"})
        self.assertEqual(status, 201)
        path = "/api/browser/sessions/" + result["session"]["thread_id"]
        self.assertEqual(self.request(path + "/messages", {"message": "Hello"})[0], 200)
        self.assertEqual(self.request(path + "/messages", {"message": "Duplicate"})[0], 409)
        self.assertTrue(self.request(path)[1]["running"])
        self.assertEqual(self.request(path + "/interrupt", {})[0], 200)
        self.assertEqual(self.request(path, {"archived": True})[0], 200)
        self.assertEqual(self.request("/api/browser/sessions")[1]["data"], [])
        self.assertEqual(self.request(path, {"archived": False})[0], 200)

    def test_mutations_require_same_origin_and_token(self):
        for headers in [{"X-Agent-Coord-Token": ""}, {"X-Agent-Coord-Token": "wrong"}, {"Origin": "https://attacker.example"}, {"Host": "attacker.example"}, {"Sec-Fetch-Site": "cross-site"}]:
            self.assertEqual(self.request("/api/browser/sessions", {}, headers)[0], 403)
        self.assertEqual(self.sessions.rpc.calls, [])
        self.assertEqual(self.request("/api/browser/config", headers={"Host": "attacker.example"})[0], 403)

    def test_parent_workspace_offers_child_repositories_and_keeps_created_session_visible(self):
        repo = self.root / "repo"
        (repo / ".git").mkdir(parents=True)
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        self.store.register(session_id="outside", client="codex", cwd=outside.name)
        self.assertEqual(self.request("/api/browser/config")[1]["workspaceRoot"], str(self.root))
        status, choices = self.request("/api/browser/workspaces")
        self.assertEqual(status, 200)
        self.assertEqual({item["cwd"] for item in choices["data"]}, {str(self.root), str(repo)})
        self.assertEqual(self.sessions.rpc.calls, [])
        status, result = self.request("/api/browser/sessions", {"cwd": str(repo)})
        self.assertEqual(status, 201)
        thread_id = result["session"]["thread_id"]
        self.assertEqual(result["session"]["cwd"], str(repo))
        self.assertEqual(self.request("/api/browser/sessions/" + thread_id)[0], 200)
        for route in ("sessions", "threads"):
            self.assertEqual([item["thread_id"] for item in self.request("/api/browser/" + route)[1]["data"]], [thread_id])
        self.assertEqual([item["session_id"] for item in self.request("/api/snapshot")[1]["parents"]], [thread_id])
        self.assertEqual(self.request("/api/browser/sessions", {"cwd": outside.name})[0], 400)

    def test_invalid_requests_do_not_start_codex(self):
        self.assertEqual(self.request("/api/browser/sessions", [], {})[0], 400)
        self.assertEqual(self.request("/api/browser/sessions", {}, {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("/api/browser/sessions", {"name": "x" * 140000})[0], 413)
        self.assertEqual(self.sessions.rpc.calls, [])

    def test_thread_metadata_checkpoint_links_and_attention_over_http(self):
        self.store.register(session_id="terminal", client="codex", cwd=str(self.root))
        path = "/api/browser/threads/terminal"
        self.assertEqual(self.request(path, {"attention": "later"})[0], 200)
        self.assertEqual(self.request(path + "/checkpoint", {"phase": "investigation", "summary": "An answer worth revisiting."})[0], 200)
        status, result = self.request(path + "/links", {"kind": "document", "target": "notes.md"})
        self.assertEqual(status, 200)
        self.assertEqual(result["links"][0]["target"], str(self.root / "notes.md"))
        link_id = result["links"][0]["id"]
        self.assertEqual(self.request(path + "/links", {"remove": link_id})[1]["links"], [])
        threads = self.request("/api/browser/threads")[1]["data"]
        self.assertEqual(threads[0]["attention"], "later")
        self.assertEqual(threads[0]["checkpoint"]["summary"], "An answer worth revisiting.")
        self.assertEqual(self.sessions.rpc.calls, [])
        self.assertEqual(self.request(path, {"attention": "archived"}, {"X-Agent-Coord-Token": "wrong"})[0], 403)
        self.assertEqual(self.request(path, {"attention": []})[0], 400)
        self.assertEqual(self.request(path + "/links", {"kind": "document", "target": "javascript:alert(1)"})[0], 400)
        self.assertEqual(self.request(path, {"attention": "archived"})[0], 200)
        self.assertEqual(self.request("/api/browser/threads")[1]["data"], [])
        self.assertEqual(len(self.request("/api/browser/threads?archived=true")[1]["data"]), 1)


if __name__ == "__main__":
    unittest.main()
