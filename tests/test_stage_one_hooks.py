"""Cross-provider child hooks and read-only tool checks during a runtime restart."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"
sys.path.insert(0, str(SCRIPTS))
from agent_coord import desktop_runtime
from agent_coord.hook import handle, handle_antigravity
from agent_coord.store import CoordinationError, CoordinationStore


class HookIsolationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = CoordinationStore(self.root / "state.sqlite3")
        self.store.register(session_id="sender", client="codex", cwd=str(self.root))

    def event(self, name, **values):
        return {"hook_event_name": name, "cwd": str(self.root), "session_id": "native-child", **values}

    def prepare_parent(self, provider):
        self.store.register(session_id=provider, client=provider, cwd=str(self.root))
        self.store.begin_work(session_id=provider, scopes=[provider + "/**"])
        return self.store.send_message(sender_session_id="sender", recipient_session_id=provider,
                                       body="Parent inbox work")

    def test_child_events_preserve_parent_registration_inbox_lifecycle_and_lease(self):
        for provider in ("claude", "codex", "kimi", "muse"):
            with self.subTest(provider=provider), patch.dict(os.environ, {
                    "AGENT_COORD_CLIENT": provider, "AGENT_COORD_SESSION_ID": provider, "AGENT_COORD_ZELLIJ_WAKE": "0"}):
                message = self.prepare_parent(provider)
                before = self.store.get_session(provider)
                for identity in ({"session_id": "native-child"}, {"session_id": provider, "agent_id": "worker"},
                                 {"session_id": provider, "transcript_path": f"/history/{provider}/subagents/agent-abc.jsonl"},
                                 {"session_id": provider, "parent_session_id": provider}):
                    for event in ("SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "SessionEnd"):
                        self.assertEqual(handle(self.event(event, **identity), self.store), {})
                    for path, allowed in ((provider + "/main.py", True), ("other/file.py", False)):
                        result = handle(self.event("PreToolUse", tool_name="Write",
                                            tool_input={"path": path}, **identity), self.store)
                        self.assertEqual(result == {}, allowed)
                self.assertEqual(self.store.get_session(provider), before)
                self.assertEqual(self.store.inbox(provider, unread_only=True, mark_delivered=False)[0]["id"], message["id"])
                with self.store.reading(), self.store._connection() as db:
                    self.assertFalse(db.execute("SELECT 1 FROM message_delivery_attempts WHERE message_id=?", (message["id"],)).fetchone())
                with self.assertRaises(CoordinationError):
                    self.store.get_session("native-child")
                self.store.end_session(provider)

    def test_antigravity_child_checks_targets_and_preserves_parent(self):
        self.prepare_parent("antigravity")
        before = self.store.get_session("antigravity")
        with patch.dict(os.environ, {"AGENT_COORD_CLIENT": "antigravity", "AGENT_COORD_SESSION_ID": "antigravity"}):
            for path, allowed in (("antigravity/main.py", "allow"), ("elsewhere.py", "deny")):
                payload = {"sessionId": "child-native", "toolCall": {"name": "write_to_file", "args": {"TargetFile": path}}}
                self.assertEqual(handle_antigravity(payload, self.store)["decision"], allowed)
        self.assertEqual(self.store.get_session("antigravity"), before)
        self.assertEqual(len(self.store.inbox("antigravity", mark_delivered=False)), 1)

    def run_hook(self, payload, database):
        env = dict(os.environ, AGENT_COORD_DB=str(database), AGENT_COORD_CLIENT="claude", AGENT_COORD_SESSION_ID="claude")
        return subprocess.run([sys.executable, str(SCRIPTS / "agent-coord-hook")], env=env,
                              input=json.dumps(payload), capture_output=True, text=True, timeout=5, check=True)

    def unavailable_owner(self, database):
        directory = desktop_runtime.runtime_directory(database)
        descriptor = desktop_runtime.lock_file(directory / "owner.lock")
        self.addCleanup(os.close, descriptor)
        desktop_runtime.atomic_json(directory / "discovery.json", {"instance": "down", "database": str(database),
            "control_url": "http://127.0.0.1:1/", "control_token": "fixture"})

    def test_unavailable_owner_uses_committed_scope_and_peeks_without_a_receipt(self):
        message = self.prepare_parent("claude")
        before = self.store.get_session("claude")
        database = self.store.database_path
        self.unavailable_owner(database)
        for session, path, expected in (("native-child", "other/main.py", "deny"), ("native-child", "claude/main.py", None),
                ("claude", "claude/main.py", f"Coordination message {message['id']} is pending")):
            result = self.run_hook(self.event("PreToolUse", session_id=session, tool_name="Write",
                                              tool_input={"path": path}), database)
            output = json.loads(result.stdout)
            if expected == "deny":
                self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], expected)
            elif expected:
                self.assertIn(expected, output["hookSpecificOutput"]["additionalContext"])
                self.assertNotIn("Parent inbox work", result.stdout)
            else:
                self.assertEqual(output, {})
        with self.store.reading(), self.store._connection() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM message_delivery_attempts WHERE message_id=?", (message["id"],)).fetchone())
            self.assertEqual(self.store.get_session("claude"), before)

    def test_restart_before_first_registration_allows_sole_writer(self):
        database = self.store.database_path
        self.unavailable_owner(database)
        result = self.run_hook(self.event("PreToolUse", session_id="claude", tool_name="Write",
                                         tool_input={"path": "main.py"}), database)
        self.assertEqual(json.loads(result.stdout), {})
        with self.store.reading(), self.assertRaises(CoordinationError):
            self.store.get_session("claude")

    def test_browser_session_start_omits_prefix_already_supplied_by_provider(self):
        with self.store._connection() as db:
            db.execute("CREATE TABLE browser_sessions (thread_id TEXT PRIMARY KEY)")
        for provider in ("claude", "codex", "kimi", "muse"):
            with self.subTest(provider=provider), patch.dict(os.environ, {
                    "AGENT_COORD_CLIENT": provider, "AGENT_COORD_SESSION_ID": provider, "AGENT_COORD_ZELLIJ_WAKE": "0"}):
                self.prepare_parent(provider)
                with self.store._connection() as db:
                    db.execute("INSERT INTO browser_sessions VALUES (?)", (provider,))
                result = handle(self.event("SessionStart", session_id=provider), self.store)
                if provider in {"kimi", "muse"}:
                    self.assertEqual(result, {})
                else:
                    text = result["hookSpecificOutput"]["additionalContext"]
                    self.assertEqual(text.count("Thread context (saved data)"), 1)
                    self.assertNotIn(self.store.threads.instructions(provider), text)
                self.store.end_session(provider)

    def test_app_first_hook_precedes_browser_registration_and_resume_only_delivers_external_changes(self):
        from agent_coord.runtime_requests import capture_context, execute_request
        for client in ("codex", "claude"):
            with self.subTest(client=client), patch.dict(os.environ, {
                    "AGENT_COORD_CLIENT": client, "AGENT_COORD_SESSION_ID": client,
                    "AGENT_COORD_APP_PROMPT": "1", "AGENT_COORD_ZELLIJ_WAKE": "0"}):
                def start():
                    request = {"kind": "hook", "payload": {"input": self.event("SessionStart", session_id=client)},
                               "context": capture_context()}
                    return execute_request(self.store, request)["result"]
                first = start()["hookSpecificOutput"]["additionalContext"]
                self.assertNotIn(self.store.threads.instructions(client), first)
                self.assertIn("Thread context (saved data)", first)
                self.store.threads.update(client, title="Paused deployment")
                self.store = CoordinationStore(self.store.database_path)
                self.assertEqual(start(), {})
                delta = handle(self.event("UserPromptSubmit", session_id=client, prompt="Continue"), self.store)
                self.assertIn("Paused deployment", delta["hookSpecificOutput"]["additionalContext"])
                self.assertEqual(handle(self.event("UserPromptSubmit", session_id=client, prompt="Continue"), self.store), {})

    def test_failed_or_racing_startup_preserves_snapshot_and_user_edits(self):
        thread = "codex"
        self.prepare_parent(thread)
        def start(text):
            return self.store.threads.startup_context(thread, "native", lambda: text)
        def fail():
            raise RuntimeError("snapshot unavailable")
        with self.assertRaises(RuntimeError):
            self.store.threads.startup_context(thread, "native", fail)
        def competing_start():
            self.assertEqual(start("initial snapshot"), "initial snapshot")
            self.store.threads.update(thread, title="Updated during startup")
            return "obsolete duplicate"
        self.assertEqual(self.store.threads.startup_context(thread, "native", competing_start), "")
        self.assertEqual(self.store.threads.observe_context(thread), {"title": "Updated during startup"})

    def test_exclusive_database_lock_allows_tool_and_reports_to_stderr(self):
        self.prepare_parent("claude")
        database = self.root / "locked.sqlite3"
        with closing(sqlite3.connect(database)) as writer, closing(sqlite3.connect(self.store.database_path)) as source:
            source.backup(writer)
            writer.execute("PRAGMA journal_mode=DELETE")
            writer.execute("BEGIN EXCLUSIVE")
            self.unavailable_owner(database)
            result = self.run_hook(self.event("PreToolUse", session_id="claude", tool_name="Edit",
                                              tool_input={"path": "claude/main.py"}), database)
        self.assertEqual(json.loads(result.stdout), {})
        self.assertIn("lease database busy", result.stderr)

    def test_named_top_level_claude_agent_keeps_its_own_inbox(self):
        self.prepare_parent("claude")
        with patch.dict(os.environ, {"AGENT_COORD_CLIENT": "claude", "AGENT_COORD_SESSION_ID": "claude"}):
            output = handle(self.event("UserPromptSubmit", session_id="claude", agent_type="reviewer"), self.store)
        self.assertIn("Parent inbox work", output["hookSpecificOutput"]["additionalContext"])
