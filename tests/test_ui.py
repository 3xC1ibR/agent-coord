from __future__ import annotations

import errno
import json
import sys
import tempfile
import threading
import unittest
import urllib.request
from http import HTTPStatus
from pathlib import Path
from unittest.mock import MagicMock, patch

PLUGIN_SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"
sys.path.insert(0, str(PLUGIN_SCRIPTS))

from agent_coord.managed_pty import output_log_path
from agent_coord.store import CoordinationError, CoordinationStore
from agent_coord.ui import _handler, build_snapshot, make_ui_server


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
            [item["session_id"] for item in filtered["parents"]], ["parent"]
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
                f"http://{host}:{port}/", timeout=2
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


if __name__ == "__main__":
    unittest.main()
