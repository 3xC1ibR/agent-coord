from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"
sys.path.insert(0, str(SCRIPTS))

from agent_coord import checkpoint_mcp, cli
from agent_coord.context import request_session
from agent_coord.store import CoordinationStore


class CallerIdentityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = CoordinationStore(self.root / "state.sqlite3")
        for name in ("parent", "child", "peer"):
            self.store.register(session_id=name, client="codex", cwd=str(self.root))
        self.store.threads.checkpoint("parent", {"phase": "investigation", "summary": "Discussion only."})
        self.store.threads.record_fork("child", self.store.threads.get("parent"), turn_id="parent-turn")
        self.store.begin_work(session_id="parent", scopes=["parent.txt"])
        self.pending = self.store.send_message(sender_session_id="peer", recipient_session_id="parent",
                                               body="Parent's request", reply_required=True)
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def invoke(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = cli.main(["--db", str(self.store.database_path), *args])
        return status, stdout.getvalue(), stderr.getvalue()

    def mcp(self, name, arguments, identity="child"):
        params = {"name": name, "arguments": arguments}
        if identity is not None:
            params["_meta"] = {"threadId": identity}
        with patch.object(checkpoint_mcp, "CoordinationStore", return_value=self.store):
            return checkpoint_mcp.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params})["result"]

    def test_fork_cannot_write_or_consume_parent_state_through_cli(self):
        original = self.store.threads.get("parent", history=True)
        commands = [
            ["register", "--session-id", "parent", "--client", "codex"],
            ["checkpoint", "--session-id", "parent", "--json", '{"summary":"Implemented"}'],
            ["begin-work", "--session-id", "parent", "--scope", "implementation.py"],
            ["end-work", "--session-id", "parent"],
            ["set-activity", "--session-id", "parent", "--activity", "implementing"],
            ["unregister", "--session-id", "parent"],
            ["send", "--from-session", "parent", "--session", "peer", "--reply-required", "Work"],
            ["reply", "--from-session", "parent", "--message-id", str(self.pending["id"]), "Done"],
            ["ack", "--session-id", "parent", "--message-id", str(self.pending["id"])],
            ["inbox", "--session-id", "parent"],
        ]
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "child"}):
            for command in commands:
                with self.subTest(command=command):
                    status, output, error = self.invoke(*command)
                    self.assertEqual(status, 2, error)
                    self.assertEqual(output, "")
                    self.assertIn("Caller identity mismatch", error)
        self.assertEqual(self.store.threads.get("parent", history=True), original)
        self.assertEqual(self.store.get_session("parent")["write_scope"], ["parent.txt"])
        pending = self.store.inbox("parent", mark_delivered=False)
        self.assertEqual([m["id"] for m in pending], [self.pending["id"]])
        self.assertIsNone(pending[0]["delivered_at"])
        self.assertIsNone(pending[0]["acknowledged_at"])
        self.assertEqual(self.store.inbox("peer", mark_delivered=False), [])

    def test_real_cli_routes_fork_replies_back_to_child_and_leaves_parent_alone(self):
        environment = {"PATH": os.defpath, "CODEX_THREAD_ID": "child"}
        command = [sys.executable, str(SCRIPTS / "agent-coord"), "--db", str(self.store.database_path)]
        bad = subprocess.run([*command, "send", "--from-session", "parent", "--session", "peer",
                              "--reply-required", "Wrong actor"], env=environment, capture_output=True, text=True, timeout=10)
        self.assertEqual(bad.returncode, 2, bad.stderr)
        good = subprocess.run([*command, "send", "--session", "peer", "--reply-required", "Child's work"],
                              env=environment, capture_output=True, text=True, timeout=10)
        self.assertEqual(good.returncode, 0, good.stderr)
        receipt = json.loads(good.stdout)
        self.assertEqual(receipt["sender_session_id"], "child")
        reply = self.store.reply_message(sender_session_id="peer", message_id=receipt["id"], body="Done")
        self.assertEqual(reply["recipient_session_id"], "child")
        self.assertEqual([m["id"] for m in self.store.inbox("parent", mark_delivered=False)], [self.pending["id"]])

    def test_matching_caller_and_explicit_recipient_are_allowed(self):
        with patch.dict(os.environ, {"AGENT_COORD_SESSION_ID": "child"}):
            status, _, error = self.invoke("checkpoint", "--session-id", "child", "--json",
                                          '{"phase":"implementation","summary":"Child work"}')
            self.assertEqual(status, 0, error)
            status, output, error = self.invoke("send", "--from-session", "child", "--session", "parent",
                                               "--no-reply-required", "A message for the parent")
            self.assertEqual(status, 0, error)
            self.assertEqual(json.loads(output)["sender_session_id"], "child")

    def test_inspection_and_thread_management_keep_explicit_targets(self):
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "child"}):
            for command in (["status", "--session-id", "parent"],
                            ["thread", "show", "--session-id", "parent"],
                            ["inbox", "--session-id", "parent", "--all", "--peek"],
                            ["thread", "update", "--session-id", "parent", "--title", "Renamed by request"]):
                status, _, error = self.invoke(*command)
                self.assertEqual(status, 0, error)
        self.assertIsNone(self.store.inbox("parent", mark_delivered=False)[0]["delivered_at"])

    def test_unbound_admin_cli_can_name_an_actor(self):
        status, _, error = self.invoke("checkpoint", "--session-id", "child", "--json",
                                      '{"phase":"investigation","summary":"Administrative update"}')
        self.assertEqual(status, 0, error)

    def test_mcp_rejects_parent_actor_for_every_mutating_tool(self):
        for name, args in (
            ("update_checkpoint", {"session_id": "parent", "summary": "Wrong thread"}),
            ("ask_human", {"session_id": "parent", "question": "Approve?", "blocking": True}),
            ("schedule_prompt", {"session_id": "parent", "mode": "self"}),
            ("move_thread", {"from_session": "parent", "session_id": "peer", "cwd": str(self.root)}),
        ):
            with self.subTest(name=name):
                result = self.mcp(name, args)
                self.assertTrue(result["isError"])
                self.assertIn("Caller identity mismatch", result["content"][0]["text"])
        self.assertEqual(self.store.threads.get("parent")["checkpoint"]["summary"], "Discussion only.")

    def test_mcp_without_bound_identity_cannot_trust_explicit_parent_id(self):
        result = self.mcp("update_checkpoint", {"session_id": "parent", "summary": "Wrong thread"}, identity=None)
        self.assertTrue(result["isError"])
        self.assertIn("current tool shell", result["content"][0]["text"])
        self.assertEqual(self.store.threads.get("parent")["checkpoint"]["summary"], "Discussion only.")

    def test_mcp_metadata_beats_stale_parent_environment(self):
        with patch.dict(os.environ, {"AGENT_COORD_SESSION_ID": "parent"}):
            result = self.mcp("update_checkpoint", {"phase": "implementation", "summary": "Child work"})
        self.assertNotIn("isError", result)
        self.assertEqual(self.store.threads.get("child")["checkpoint"]["summary"], "Child work")
        self.assertEqual(self.store.threads.get("parent")["checkpoint"]["summary"], "Discussion only.")
        self.assertIsNone(request_session.get())

    def test_mcp_move_keeps_target_distinct_from_actor(self):
        from test_codex_app_server import BrowserSessions, FakeCodex
        sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(sessions.close)
        target = sessions.create({"cwd": str(self.root)})["session"]["thread_id"]
        result = self.mcp("move_thread", {"session_id": target, "cwd": str(self.root), "request_id": "move-parent"})
        self.assertNotIn("isError", result)
        from agent_coord.app_control import AppControl
        saved = AppControl(self.store).status("move-parent")
        self.assertEqual(saved["thread_id"], target)
        with self.store._connection() as db:
            actor = db.execute("SELECT sender_session_id FROM app_control_requests WHERE request_id = ?",
                               ("move-parent",)).fetchone()[0]
        self.assertEqual(actor, "child")

    def test_fork_resume_instructions_identify_child_and_warn_about_parent(self):
        from test_codex_app_server import BrowserSessions, FakeCodex
        sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(sessions.close)
        parent = sessions.create({"cwd": str(self.root)})["session"]["thread_id"]
        child = sessions.fork_work_thread(parent)["thread_id"]
        sessions.read(child)
        resume = next(params for method, params in sessions.rpc.calls if method == "thread/resume")
        instructions = resume["developerInstructions"]
        self.assertIn("Your current Agent Coord session ID is " + child, instructions)
        self.assertIn("This is a fork of " + parent, instructions)
        self.assertIn("Do not use the parent's identity", instructions)


if __name__ == "__main__":
    unittest.main()
