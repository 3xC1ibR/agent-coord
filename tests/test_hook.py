from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PLUGIN_SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"
sys.path.insert(0, str(PLUGIN_SCRIPTS))

from agent_coord.hook import handle
from agent_coord.store import CoordinationError, CoordinationStore


class HookTests(unittest.TestCase):
    def test_claude_native_hooks_keep_stable_app_identity_and_ignore_children(self):
        self.store.register(session_id="app-claude", client="claude", cwd=str(self.root))
        with self.store._connection() as db:
            db.execute("CREATE TABLE browser_sessions (thread_id TEXT PRIMARY KEY)")
            db.execute("INSERT INTO browser_sessions VALUES ('app-claude')")
            db.execute("CREATE TABLE claude_sessions (thread_id TEXT PRIMARY KEY,native_id TEXT)")
            db.execute("INSERT INTO claude_sessions VALUES ('app-claude','native-claude')")
        self.store.register(session_id="sender", client="codex", cwd=str(self.root))
        message = self.store.send_message(sender_session_id="sender", recipient_session_id="app-claude",
                                         classification="action_required", reply_required=False, body="Read this")
        with patch.dict("os.environ", {"AGENT_COORD_CLIENT": "claude", "AGENT_COORD_SESSION_ID": "app-claude"}):
            self.assertEqual(handle(self.payload("UserPromptSubmit", session_id="native-child", prompt="child"), self.store), {})
            self.assertEqual(self.store.inbox("app-claude", mark_delivered=False)[0]["id"], message["id"])
            result = handle(self.payload("UserPromptSubmit", session_id="native-claude", prompt="top level"), self.store)
        self.assertIn("Read this", result["hookSpecificOutput"]["additionalContext"])
        for phantom in ("native-claude", "native-child"):
            with self.assertRaises(CoordinationError):
                self.store.get_session(phantom)

    def test_ordinary_terminal_stop_and_end_capture_thread_usage_idempotently(self):
        handle(self.payload("SessionStart"), self.store)
        path = self.root / "terminal.jsonl"
        path.write_text(json.dumps({"type": "session_meta", "payload": {"id": "codex-one"}}) + "\n"
                        + json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {
                            "total_token_usage": {"input_tokens": 90, "output_tokens": 10, "total_tokens": 100}}}}) + "\n")
        for event in ("Stop", "SessionEnd"):
            handle(self.payload(event, transcript_path=str(path)), self.store)
        self.assertEqual(self.store.usage.summary("codex-one")["totals"]["total_tokens"], 100)
        self.assertEqual(self.store.list_delegations(), [])

    def test_thread_usage_capture_failure_does_not_block_stop_or_end(self):
        handle(self.payload("SessionStart"), self.store)
        with patch.object(self.store.usage, "backfill", side_effect=RuntimeError("database unavailable")):
            self.assertEqual(handle(self.payload("Stop"), self.store), {})
            self.assertEqual(handle(self.payload("SessionEnd"), self.store), {})
        self.assertEqual(self.store.get_session("codex-one")["presence"], "offline")

    def setUp(self) -> None:
        wake_environment = patch.dict(
            "os.environ",
            {
                "AGENT_COORD_CLIENT": "codex",
                "AGENT_COORD_ZELLIJ_WAKE": "0",
            },
        )
        wake_environment.start()
        self.addCleanup(wake_environment.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = CoordinationStore(self.root / "state.sqlite3")
        self.outside = tempfile.TemporaryDirectory()
        self.addCleanup(self.outside.cleanup)

    def payload(self, event: str, session_id: str = "codex-one", **extra: object):
        return {
            "session_id": session_id,
            "cwd": str(self.root),
            "hook_event_name": event,
            **extra,
        }

    def test_session_start_registers_and_announces_identity(self) -> None:
        result = handle(self.payload("SessionStart"), self.store)

        context = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn("codex-one", context)
        self.assertEqual(self.store.get_session("codex-one")["presence"], "online")

    def test_each_prompt_receives_domain_ownership_and_workspace_vocabulary(self) -> None:
        handle(self.payload("SessionStart"), self.store)
        self.store.threads.update("codex-one", domain="Web")
        self.store.register(session_id="peer", client="claude", cwd=str(self.root))
        self.store.threads.update("peer", domain="docs")
        for event in ("SessionStart", "UserPromptSubmit"):
            result = handle(self.payload(event, prompt="Continue."), self.store)
            context = result["hookSpecificOutput"]["additionalContext"]
            names = json.loads(context.split("Workspace domains: ", 1)[1].splitlines()[0])
            self.assertEqual(names, ["docs", "web"])
            saved = json.loads(context.split("Thread context (saved data): ", 1)[1])
            self.assertEqual((saved["domain"], saved["domain_source"]), ("web", "user"))
            self.assertIsNone(saved["scope_cluster"])
            self.assertIn("once per assignment", context)
        self.store.threads.update("peer", domain="backend")
        result = handle(self.payload("UserPromptSubmit", prompt="Continue."), self.store)
        self.assertIn('Workspace domains: ["backend", "web"]', result["hookSpecificOutput"]["additionalContext"])

    def test_hooks_capture_intent_and_preserve_checkpoint_after_stop(self) -> None:
        handle(self.payload("SessionStart"), self.store)
        handle(self.payload("UserPromptSubmit", prompt="Investigate the write path."), self.store)
        self.store.threads.checkpoint("codex-one", {"phase": "investigation", "summary": "Two options remain."})
        self.store.threads.update("codex-one", attention="later")
        handle(self.payload("Stop"), self.store)
        handle(self.payload("SessionEnd"), self.store)
        resumed = handle(self.payload("SessionStart"), self.store)
        self.assertIn("Two options remain.", resumed["hookSpecificOutput"]["additionalContext"])
        thread = self.store.threads.get("codex-one")
        self.assertEqual(thread["attention"], "later")
        self.assertEqual(thread["original_request"], "Investigate the write path.")

    def test_browser_automatic_wake_does_not_capture_human_request(self):
        from agent_coord.browser_wake import WAKE_PROMPT as BROWSER_WAKE_PROMPT
        from agent_coord.zellij_wake import WAKE_PROMPT
        handle(self.payload("SessionStart"), self.store)
        with self.store._connection() as db:
            db.execute("CREATE TABLE browser_sessions (thread_id TEXT PRIMARY KEY)")
            db.execute("INSERT INTO browser_sessions VALUES ('codex-one')")
        for prompt in (WAKE_PROMPT, BROWSER_WAKE_PROMPT, "  " + BROWSER_WAKE_PROMPT + "\nContinue the assignment."):
            handle(self.payload("UserPromptSubmit", prompt=prompt), self.store)
            self.assertEqual(self.store.threads.get("codex-one")["original_request"], "")
        handle(self.payload("UserPromptSubmit", prompt="Repair the message hook."), self.store)
        self.assertEqual(self.store.threads.get("codex-one")["original_request"], "Repair the message hook.")

    def test_session_start_attaches_inherited_delegation(self) -> None:
        self.store.register(
            session_id="parent",
            client="claude",
            cwd=str(self.root),
        )
        self.store.create_delegation(
            parent_session_id="parent",
            cwd=str(self.root),
            bead_id="work-a",
            scopes=["src/**"],
            instructions="Implement work-a.",
            mode="reviewed",
            delegation_id="delegation-a",
        )

        with patch.dict(
            "os.environ", {"AGENT_COORD_DELEGATION_ID": "delegation-a"}
        ):
            result = handle(self.payload("SessionStart"), self.store)

        context = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn("attached to delegation delegation-a", context)
        delegation = self.store.get_delegation("delegation-a")
        self.assertEqual(delegation["child_session_id"], "codex-one")
        self.assertEqual(delegation["status"], "attached")

    def test_session_start_attaches_a_claude_delegation(self) -> None:
        self.store.register(
            session_id="parent",
            client="codex",
            cwd=str(self.root),
        )
        self.store.create_delegation(
            parent_session_id="parent",
            cwd=str(self.root),
            bead_id="work-a",
            scopes=["src/**"],
            instructions="Implement work-a.",
            mode="reviewed",
            client="claude",
            delegation_id="delegation-claude",
        )

        with patch.dict(
            "os.environ",
            {
                "AGENT_COORD_CLIENT": "claude",
                "AGENT_COORD_DELEGATION_ID": "delegation-claude",
            },
        ):
            result = handle(
                self.payload("SessionStart", session_id="claude-child"), self.store
            )

        context = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn("attached to delegation delegation-claude", context)
        delegation = self.store.get_delegation("delegation-claude")
        self.assertEqual(delegation["child_session_id"], "claude-child")
        self.assertEqual(self.store.get_session("claude-child")["client"], "claude")

    def test_session_start_attaches_a_delegation_without_an_issue(self) -> None:
        self.store.register(session_id="parent", client="codex", cwd=str(self.root))
        self.store.create_delegation(
            parent_session_id="parent", cwd=str(self.root), scopes=["src/**"],
            instructions="Validate a change.", mode="reviewed", delegation_id="direct",
        )

        with patch.dict("os.environ", {"AGENT_COORD_DELEGATION_ID": "direct"}):
            result = handle(self.payload("SessionStart"), self.store)

        context = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn("attached to delegation direct. Authorized scopes: src/**", context)
        self.assertNotIn("for Bead", context)
        self.assertIsNone(self.store.get_delegation("direct")["bead_id"])
        self.assertEqual(self.store.get_delegation("direct")["child_session_id"], "codex-one")

    @patch(
        "agent_coord.hook.enable_from_environment",
        side_effect=CoordinationError("zellij unavailable"),
    )
    def test_optional_wake_failure_keeps_session_start_context(self, _enable) -> None:
        self.store.register(
            session_id="claude-two",
            client="claude",
            cwd=str(self.root),
        )
        self.store.register(
            session_id="codex-one",
            client="codex",
            cwd=str(self.root),
        )
        self.store.send_message(
            sender_session_id="claude-two",
            recipient_session_id="codex-one",
            body="preserve this message",
        )

        result = handle(self.payload("SessionStart"), self.store)

        context = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn("preserve this message", context)
        self.assertIn("Zellij wake could not start", context)

    def test_solo_write_without_work_declaration_is_allowed(self) -> None:
        result = handle(
            self.payload(
                "PreToolUse",
                tool_name="apply_patch",
                tool_input={
                    "command": "*** Begin Patch\n*** Update File: src/app.py\n*** End Patch"
                },
            ),
            self.store,
        )

        self.assertEqual(result, {})
        self.assertEqual(self.store.get_session("codex-one")["activity"], "implementing")
        handle(self.payload("Stop"), self.store)
        self.assertEqual(self.store.get_session("codex-one")["activity"], "waiting")

    def test_idle_peer_does_not_require_a_scope(self) -> None:
        handle(self.payload("SessionStart", session_id="claude-two"), self.store)

        result = handle(
            self.payload(
                "PreToolUse",
                tool_name="apply_patch",
                tool_input={
                    "command": "*** Begin Patch\n*** Update File: src/app.py\n*** End Patch"
                },
            ),
            self.store,
        )

        self.assertEqual(result, {})
        self.assertEqual(self.store.get_session("codex-one")["activity"], "implementing")

    def test_scope_request_clears_when_the_competing_session_exits(self) -> None:
        incumbent_write = {
            "command": "*** Begin Patch\n*** Update File: src/app.py\n*** End Patch"
        }
        handle(
            self.payload(
                "PreToolUse", tool_name="apply_patch", tool_input=incumbent_write
            ),
            self.store,
        )
        handle(self.payload("Stop"), self.store)
        handle(self.payload("SessionStart", session_id="claude-two"), self.store)
        handle(
            self.payload(
                "PreToolUse",
                session_id="claude-two",
                tool_name="Write",
                tool_input={"file_path": str(self.root / "tests/test_app.py")},
            ),
            self.store,
        )
        self.assertTrue(self.store.get_session("codex-one")["scope_required"])

        handle(self.payload("SessionEnd", session_id="claude-two"), self.store)
        result = handle(
            self.payload(
                "PreToolUse", tool_name="apply_patch", tool_input=incumbent_write
            ),
            self.store,
        )

        self.assertEqual(result, {})
        self.assertFalse(self.store.get_session("codex-one")["scope_required"])
        self.assertEqual(
            self.store.inbox(
                "codex-one", unread_only=True, mark_delivered=False
            ),
            [],
        )

    def test_newcomer_stops_and_requests_an_incumbent_scope(self) -> None:
        write = {
            "command": "*** Begin Patch\n*** Update File: src/app.py\n*** End Patch"
        }
        self.assertEqual(
            handle(
                self.payload(
                    "PreToolUse", tool_name="apply_patch", tool_input=write
                ),
                self.store,
            ),
            {},
        )
        handle(self.payload("Stop"), self.store)
        handle(self.payload("SessionStart", session_id="claude-two"), self.store)

        blocked = handle(
            self.payload(
                "PreToolUse",
                session_id="claude-two",
                tool_name="apply_patch",
                tool_input={
                    "command": "*** Begin Patch\n*** Update File: tests/test_app.py\n*** End Patch"
                },
            ),
            self.store,
        )

        output = blocked["hookSpecificOutput"]
        self.assertEqual(output["permissionDecision"], "deny")
        self.assertIn("--bead is optional", output["permissionDecisionReason"])
        self.assertIn("Wait for the unscoped incumbent", output["permissionDecisionReason"])
        self.assertEqual(self.store.get_session("claude-two")["activity"], "waiting")
        self.assertTrue(self.store.get_session("codex-one")["scope_required"])
        request = self.store.inbox(
            "codex-one", unread_only=True, mark_delivered=False
        )
        self.assertEqual(len(request), 1)
        self.assertIn("declare the smallest current scope", request[0]["body"])
        self.assertFalse(request[0]["reply_required"])

        incumbent_blocked = handle(
            self.payload("PreToolUse", tool_name="apply_patch", tool_input=write),
            self.store,
        )
        self.assertEqual(
            incumbent_blocked["hookSpecificOutput"]["permissionDecision"], "deny"
        )
        self.assertIn(
            "declare the smallest current scope",
            incumbent_blocked["hookSpecificOutput"]["additionalContext"],
        )

        incumbent = self.store.begin_work(
            session_id="codex-one", scopes=["src/**"]
        )
        self.assertIsNone(incumbent["bead_id"])
        self.assertFalse(incumbent["scope_required"])
        self.assertEqual(
            handle(
                self.payload("PreToolUse", tool_name="apply_patch", tool_input=write),
                self.store,
            ),
            {},
        )

    def test_write_inside_declared_scope_is_allowed(self) -> None:
        handle(self.payload("SessionStart"), self.store)
        self.store.begin_work(
            session_id="codex-one", bead_id="work-a", scopes=["src/**"]
        )

        result = handle(
            self.payload(
                "PreToolUse",
                tool_name="apply_patch",
                tool_input={
                    "command": "*** Begin Patch\n*** Update File: src/app.py\n*** End Patch"
                },
            ),
            self.store,
        )

        self.assertEqual(result, {})

    def test_validation_lease_denies_write_tools(self) -> None:
        self.store.register(
            session_id="codex-one", client="codex", cwd=str(self.root)
        )
        self.store.begin_work(
            session_id="codex-one",
            bead_id="work-a",
            scopes=["src/**"],
            activity="validating",
            lease_mode="validation",
        )

        result = handle(
            self.payload(
                "PreToolUse",
                tool_name="apply_patch",
                tool_input={
                    "command": "*** Begin Patch\n*** Update File: src/app.py\n*** End Patch"
                },
            ),
            self.store,
        )

        self.assertIn(
            "validation lease", result["hookSpecificOutput"]["permissionDecisionReason"]
        )

    def test_validating_activity_on_write_lease_remains_editable(self) -> None:
        self.store.register(
            session_id="codex-one", client="codex", cwd=str(self.root)
        )
        self.store.begin_work(
            session_id="codex-one",
            bead_id="work-a",
            scopes=["src/**"],
            activity="validating",
        )

        result = handle(
            self.payload(
                "PreToolUse",
                tool_name="Write",
                tool_input={"file_path": str(self.root / "src/app.py")},
            ),
            self.store,
        )

        self.assertEqual(result, {})

    def test_write_outside_declared_scope_is_denied(self) -> None:
        handle(self.payload("SessionStart"), self.store)
        self.store.begin_work(
            session_id="codex-one", bead_id="work-a", scopes=["src/**"]
        )

        result = handle(
            self.payload(
                "PreToolUse",
                tool_name="Write",
                tool_input={"file_path": str(self.root / "tests/test_app.py")},
            ),
            self.store,
        )

        self.assertIn(
            "outside the declared scope",
            result["hookSpecificOutput"]["permissionDecisionReason"],
        )

    def test_outside_repository_write_passes_through_without_declaration(self) -> None:
        result = handle(
            self.payload(
                "PreToolUse",
                tool_name="Write",
                tool_input={"file_path": str(Path(self.outside.name) / "note.txt")},
            ),
            self.store,
        )

        self.assertEqual(result, {})

    def test_outside_repository_write_passes_through_with_declaration(self) -> None:
        handle(self.payload("SessionStart"), self.store)
        self.store.begin_work(
            session_id="codex-one", bead_id="work-a", scopes=["src/**"]
        )

        result = handle(
            self.payload(
                "PreToolUse",
                tool_name="Edit",
                tool_input={"file_path": str(Path(self.outside.name) / "note.txt")},
            ),
            self.store,
        )

        self.assertEqual(result, {})

    def test_relative_escape_is_treated_as_outside_the_repository(self) -> None:
        result = handle(
            self.payload(
                "PreToolUse",
                tool_name="Write",
                tool_input={"file_path": "../elsewhere.txt"},
            ),
            self.store,
        )

        self.assertEqual(result, {})

    def test_mixed_targets_still_gate_the_in_repository_path(self) -> None:
        handle(self.payload("SessionStart"), self.store)
        self.store.begin_work(
            session_id="codex-one", bead_id="work-a", scopes=["docs/**"]
        )

        command = (
            "*** Begin Patch\n"
            f"*** Update File: {Path(self.outside.name) / 'note.txt'}\n"
            "*** Update File: src/app.py\n"
            "*** End Patch"
        )
        result = handle(
            self.payload(
                "PreToolUse",
                tool_name="apply_patch",
                tool_input={"command": command},
            ),
            self.store,
        )

        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("src/app.py", reason)
        self.assertNotIn("note.txt", reason)

    def test_mixed_targets_allow_in_scope_in_repository_path(self) -> None:
        handle(self.payload("SessionStart"), self.store)
        self.store.begin_work(
            session_id="codex-one", bead_id="work-a", scopes=["src/**"]
        )

        command = (
            "*** Begin Patch\n"
            f"*** Update File: {Path(self.outside.name) / 'note.txt'}\n"
            "*** Update File: src/app.py\n"
            "*** End Patch"
        )
        result = handle(
            self.payload(
                "PreToolUse",
                tool_name="apply_patch",
                tool_input={"command": command},
            ),
            self.store,
        )

        self.assertEqual(result, {})

    def test_user_prompt_delivers_a_message_once(self) -> None:
        handle(self.payload("SessionStart"), self.store)
        self.store.register(
            session_id="claude-two",
            client="claude",
            cwd=str(self.root),
            name="peer",
        )
        self.store.send_message(
            sender_session_id="claude-two",
            recipient_session_id="codex-one",
            body="I need src/app.py.",
        )

        first = handle(self.payload("UserPromptSubmit"), self.store)
        self.assertTrue(self.store.get_session("codex-one")["turn_active"])
        second = handle(self.payload("UserPromptSubmit"), self.store)

        self.assertIn(
            "I need src/app.py.", first["hookSpecificOutput"]["additionalContext"]
        )
        self.assertNotIn("I need src/app.py.", second["hookSpecificOutput"]["additionalContext"])

    def test_actionable_messages_precede_large_bootstrap_and_remain_recoverable(self):
        handle(self.payload("SessionStart"), self.store)
        self.store.register(session_id="peer", client="claude", cwd=str(self.root))
        for event in ("SessionStart", "UserPromptSubmit"):
            with self.subTest(event=event):
                body = "URGENT: scoped handoff " + "complete body " * 500 + "END-OF-REQUEST"
                sent = self.store.send_message(sender_session_id="peer", recipient_session_id="codex-one",
                                               body=body, reply_required=True)
                preamble = "LONG-BOOTSTRAP " * 2000
                with patch("agent_coord.hook.coordination_bootstrap", return_value=preamble), \
                     patch.object(self.store.threads, "instructions", return_value=preamble):
                    result = handle(self.payload(event), self.store)
                text = result["hookSpecificOutput"]["additionalContext"]
                self.assertLess(text.index("URGENT:"), 1000)
                self.assertLess(text.index("END-OF-REQUEST"), text.index("LONG-BOOTSTRAP"))
                self.assertEqual(text.count(body), 1)
                again = handle(self.payload("PreToolUse", tool_name="Read"), self.store)
                self.assertNotIn("URGENT:", json.dumps(again))
                unread = self.store.inbox("codex-one", unread_only=True, mark_delivered=False)
                self.assertEqual(next(m["body"] for m in unread if m["id"] == sent["id"]), body)

    def test_nested_repo_hooks_preserve_declared_base_and_resolve_relative_targets(self):
        (self.root / ".git").mkdir()
        nested = self.root / "private"
        (nested / ".git").mkdir(parents=True)
        target = nested / "orchestration/h.md"
        handle(self.payload("SessionStart"), self.store)
        self.store.begin_work(session_id="codex-one", scopes=["private/orchestration/h.md"])
        for path in (str(target), "orchestration/h.md"):
            result = handle(self.payload("PreToolUse", cwd=str(nested), tool_name="Write",
                                         tool_input={"file_path": path}), self.store)
            self.assertEqual(result, {})
        session = self.store.get_session("codex-one")
        self.assertEqual(session["cwd"], str(self.root.resolve()))
        self.assertEqual(session["write_scope"], ["private/orchestration/h.md"])
        denied = handle(self.payload("PreToolUse", cwd=str(nested), tool_name="Write",
                                     tool_input={"file_path": "orchestration/other.md"}), self.store)
        self.assertIn("outside the declared scope", denied["hookSpecificOutput"]["permissionDecisionReason"])
        self.store.register(session_id="peer", client="claude", cwd=str(self.root))
        from agent_coord.store import ConflictError
        with self.assertRaises(ConflictError):
            self.store.begin_work(session_id="peer", scopes=["private/orchestration/h.md"])
        self.store.end_work("codex-one")
        handle(self.payload("UserPromptSubmit", cwd=str(nested)), self.store)
        self.assertEqual(self.store.get_session("codex-one")["cwd"], str(nested.resolve()))

    def test_hooks_surface_only_action_required_messages(self) -> None:
        self.store.register(
            session_id="codex-one", client="codex", cwd=str(self.root)
        )
        self.store.register(
            session_id="claude-two", client="claude", cwd=str(self.root), name="peer"
        )
        self.store.send_message(
            sender_session_id="claude-two",
            recipient_session_id="codex-one",
            body="FYI only",
            classification="informational",
            thread_id="thread-a",
        )
        self.store.send_message(
            sender_session_id="claude-two",
            recipient_session_id="codex-one",
            body="Thread complete",
            classification="closure",
            thread_id="thread-b",
        )
        actionable = self.store.send_message(
            sender_session_id="claude-two",
            recipient_session_id="codex-one",
            body="Please validate",
            classification="action_required",
            thread_id="thread-c",
            reply_required=False,
        )

        result = handle(self.payload("UserPromptSubmit"), self.store)
        context = result["hookSpecificOutput"]["additionalContext"]

        self.assertIn("Please validate", context)
        self.assertNotIn("FYI only", context)
        self.assertNotIn("Thread complete", context)
        self.assertIn("thread thread-c", context)
        self.assertIn("reply_required=false", context)
        self.assertIn("only where reply_required=true", context)
        self.assertEqual(
            self.store.inbox("codex-one", mark_delivered=False),
            [
                message
                for message in self.store.inbox(
                    "codex-one", include_delivered=True, mark_delivered=False
                )
                if message["id"] != actionable["id"]
                and message["classification"] != "closure"
            ],
        )

    def test_closure_before_hook_delivery_suppresses_actionable_context(self) -> None:
        self.store.register(
            session_id="codex-one", client="codex", cwd=str(self.root)
        )
        self.store.register(
            session_id="claude-two", client="claude", cwd=str(self.root)
        )
        pending = self.store.send_message(
            sender_session_id="claude-two",
            recipient_session_id="codex-one",
            body="Please validate",
            thread_id="thread-a",
        )
        self.store.send_message(
            sender_session_id="claude-two",
            recipient_session_id="codex-one",
            body="Resolved before delivery",
            classification="closure",
            thread_id="thread-a",
        )

        result = handle(self.payload("UserPromptSubmit"), self.store)

        self.assertNotIn("Please validate", result["hookSpecificOutput"]["additionalContext"])
        history = self.store.inbox(
            "codex-one", include_delivered=True, mark_delivered=False
        )
        self.assertEqual(history[0]["id"], pending["id"])
        self.assertIsNotNone(history[0]["delivered_at"])

    def test_stop_preserves_work_as_waiting_and_session_end_marks_offline(self) -> None:
        handle(self.payload("SessionStart"), self.store)
        self.store.begin_work(
            session_id="codex-one", bead_id="work-a", scopes=["src/**"]
        )
        handle(self.payload("UserPromptSubmit"), self.store)
        self.store.register_zellij_wake(
            session_id="codex-one",
            zellij_session="test-session",
            pane_id="terminal_4",
        )

        self.assertEqual(handle(self.payload("Stop"), self.store), {})
        self.assertEqual(self.store.get_session("codex-one")["activity"], "waiting")
        self.assertFalse(self.store.get_session("codex-one")["turn_active"])
        self.assertEqual(handle(self.payload("SessionEnd"), self.store), {})
        self.assertEqual(self.store.get_session("codex-one")["presence"], "offline")
        self.assertFalse(self.store.get_zellij_wake("codex-one")["enabled"])

    def test_completed_delegation_stop_captures_token_usage(self) -> None:
        self.store.register(
            session_id="parent",
            client="claude",
            cwd=str(self.root),
        )
        self.store.create_delegation(
            parent_session_id="parent",
            cwd=str(self.root),
            bead_id="work-a",
            scopes=["src/**"],
            instructions="Implement work-a.",
            mode="reviewed",
            delegation_id="delegation-a",
        )
        transcript = self.root / "codex.jsonl"
        transcript.write_text(
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {
                                "input_tokens": 40,
                                "cached_input_tokens": 25,
                                "output_tokens": 10,
                                "total_tokens": 50,
                            }
                        },
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        with patch.dict(
            "os.environ", {"AGENT_COORD_DELEGATION_ID": "delegation-a"}
        ):
            handle(self.payload("SessionStart"), self.store)
            self.store.finish_delegation(
                "delegation-a",
                child_session_id="codex-one",
                outcome="completed",
                message="Done.",
            )
            result = handle(
                self.payload(
                    "Stop",
                    transcript_path=str(transcript),
                    model="gpt-test",
                ),
                self.store,
            )

        self.assertEqual(result, {})
        delegation = self.store.get_delegation("delegation-a")
        self.assertEqual(delegation["token_usage"]["normalized"]["total_tokens"], 50)
        self.assertEqual(delegation["token_usage_capture_event"], "Stop")
        self.assertTrue(Path(delegation["token_usage_artifact_path"]).is_file())

    def test_token_usage_failure_does_not_break_stop(self) -> None:
        self.store.register(
            session_id="parent",
            client="claude",
            cwd=str(self.root),
        )
        self.store.create_delegation(
            parent_session_id="parent",
            cwd=str(self.root),
            bead_id="work-a",
            scopes=["src/**"],
            instructions="Implement work-a.",
            mode="reviewed",
            delegation_id="delegation-a",
        )
        with patch.dict(
            "os.environ", {"AGENT_COORD_DELEGATION_ID": "delegation-a"}
        ):
            handle(self.payload("SessionStart"), self.store)
            self.store.finish_delegation(
                "delegation-a",
                child_session_id="codex-one",
                outcome="completed",
                message="Done.",
            )
            result = handle(self.payload("Stop"), self.store)

        delegation = self.store.get_delegation("delegation-a")
        self.assertEqual(result, {})
        self.assertIsNone(delegation["token_usage"])
        self.assertIn("transcript_path", delegation["token_usage_error"])

    def test_session_end_reports_an_unfinished_delegation(self) -> None:
        self.store.register(
            session_id="parent",
            client="claude",
            cwd=str(self.root),
        )
        self.store.create_delegation(
            parent_session_id="parent",
            cwd=str(self.root),
            bead_id="work-a",
            scopes=["src/**"],
            instructions="Implement work-a.",
            mode="reviewed",
            delegation_id="delegation-a",
        )
        handle(self.payload("SessionStart"), self.store)
        self.store.attach_delegation("delegation-a", "codex-one")
        transcript = self.root / "codex.jsonl"
        transcript.write_text(
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {
                                "input_tokens": 8,
                                "output_tokens": 2,
                                "total_tokens": 10,
                            }
                        },
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

        handle(
            self.payload("SessionEnd", transcript_path=str(transcript)), self.store
        )

        delegation = self.store.get_delegation("delegation-a")
        self.assertEqual(delegation["status"], "failed")
        self.assertEqual(delegation["token_usage_capture_event"], "SessionEnd")
        self.assertEqual(delegation["token_usage"]["normalized"]["total_tokens"], 10)
        self.assertIn("ended before", self.store.inbox("parent")[0]["body"])


if __name__ == "__main__":
    unittest.main()


class ToolActivityHookTests(HookTests):
    """Tool events leave a best-effort activity record for the overview lens."""

    def test_tool_events_record_what_the_session_is_doing(self) -> None:
        handle(self.payload("SessionStart"), self.store)
        handle(self.payload("PreToolUse", tool_name="Bash",
                            tool_input={"command": "python3 -m unittest   -q\necho done"}), self.store)
        tool = self.store.get_session("codex-one")["last_tool"]
        self.assertEqual((tool["name"], tool["kind"], tool["label"]), ("Bash", "run", "python3 -m unittest -q"))
        self.assertIsInstance(tool["at"], float)
        (self.root / "notes.md").write_text("draft")
        handle(self.payload("PostToolUse", tool_name="Edit",
                            tool_input={"file_path": str(self.root / "notes.md"), "old_string": "a", "new_string": "b"}), self.store)
        tool = self.store.get_session("codex-one")["last_tool"]
        self.assertEqual((tool["name"], tool["kind"]), ("Edit", "edit"))
        self.assertTrue(tool["label"].endswith("notes.md"))
        self.assertNotIn(str(self.root), tool["label"])

    def test_activity_recording_never_blocks_a_tool_call(self) -> None:
        handle(self.payload("SessionStart"), self.store)
        with patch.object(CoordinationStore, "record_tool", side_effect=RuntimeError("disk full")):
            result = handle(self.payload("PostToolUse", tool_name="Bash", tool_input={"command": "ls"}), self.store)
        self.assertIsInstance(result, dict)
        self.assertNotIn("decision", json.dumps(result))
        self.assertIsNone(self.store.get_session("codex-one")["last_tool"])
