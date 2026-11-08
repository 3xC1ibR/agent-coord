from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))

from agent_coord.antigravity import AntigravityRPC
from agent_coord.claude_code import ClaudeRPC
from agent_coord.kimi import KimiRPC
from agent_coord.muse_code import MuseRPC
from agent_coord.store import CoordinationStore


class ProviderHistoryReconciliationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = CoordinationStore(self.root / "state.sqlite3")
        self.providers = [kind(self.store, lambda event: None)
                          for kind in (ClaudeRPC, AntigravityRPC, KimiRPC, MuseRPC)]
        for provider in self.providers:
            self.addCleanup(provider.close)

    @staticmethod
    def history(status="inProgress"):
        return {"id": "thread", "turns": [
            {"id": "earlier", "status": "completed", "items": []},
            {"id": "turn", "status": status, "items": [
                {"id": "answer", "type": "agentMessage", "text": "saved text"}]}]}

    def test_metadata_reads_and_resumes_never_load_or_copy_transcripts(self):
        class Uncopiable:
            def __deepcopy__(self, memo):
                raise AssertionError("Metadata requests must not copy turn items")

        for provider in self.providers:
            with self.subTest(provider=type(provider).__name__):
                with patch(type(provider).__module__ + ".read_history", side_effect=AssertionError("Unexpected history read")):
                    for active in (False, True):
                        provider.turns = {"thread": {"id": "turn", "items": Uncopiable()}} if active else {}
                        for method, flag in (("thread/read", {"includeTurns": False}),
                                             ("thread/resume", {"excludeTurns": True})):
                            result = provider.request(method, {"threadId": "thread", "cwd": str(self.root), **flag})
                            self.assertEqual(result["thread"], {"id": "thread", "turns": [],
                                             "status": {"type": "active" if active else "idle"}})
                provider.turns.clear()

    def test_default_reads_and_resumes_still_load_saved_turns(self):
        for provider in self.providers:
            for method in ("thread/read", "thread/resume"):
                with self.subTest(provider=type(provider).__name__, method=method):
                    with patch(type(provider).__module__ + ".read_history", return_value=self.history("completed")) as read:
                        result = provider.request(method, {"threadId": "thread", "cwd": str(self.root)})
                    read.assert_called_once_with(self.store, "thread")
                    self.assertEqual(result["thread"]["turns"], self.history("completed")["turns"])

    def test_mirror_reconciliation_preserves_live_items_without_database_access(self):
        for provider in self.providers:
            with self.subTest(provider=type(provider).__name__):
                live = copy.deepcopy(self.history()["turns"][-1])
                live["items"][0]["text"] = "new streamed text"
                provider.turns["thread"] = live
                with patch(type(provider).__module__ + ".read_history", side_effect=AssertionError("Unexpected history read")):
                    result = provider.reconcile_history("thread", self.history())
                self.assertEqual([turn["id"] for turn in result["turns"]], ["earlier", "turn"])
                self.assertEqual(result["status"]["type"], "active")
                self.assertEqual(result["turns"][-1]["items"][0]["text"], "new streamed text")
                result["turns"][-1]["items"][0]["text"] = "caller edit"
                self.assertEqual(live["items"][0]["text"], "new streamed text")
                provider.turns.clear()

    def test_restart_recovery_keeps_terminal_turns_and_interrupts_unsaved_running_state(self):
        for provider in self.providers:
            with self.subTest(provider=type(provider).__name__):
                result = provider.reconcile_history("thread", self.history())
                self.assertEqual([turn["status"] for turn in result["turns"]], ["completed", "interrupted"])
                self.assertEqual(result["status"]["type"], "idle")

    def test_terminal_snapshot_is_available_before_completion_callback_finishes(self):
        for provider in self.providers:
            with self.subTest(provider=type(provider).__name__):
                observed = []
                provider.turns["thread"] = copy.deepcopy(self.history()["turns"][-1])

                def on_event(event):
                    if event["method"] == "turn/completed":
                        observed.append(provider.reconcile_history("thread", self.history()))

                provider.on_event = on_event
                with patch.object(provider, "_capture_usage", create=True):
                    provider._finish("thread", "failed", "provider failed")
                self.assertEqual(len(observed), 1)
                self.assertEqual(observed[0]["turns"][-1]["status"], "failed")
                self.assertEqual(observed[0]["turns"][-1]["error"]["message"], "provider failed")

    def test_retained_completion_does_not_replace_a_newer_terminal_mirror(self):
        for provider in self.providers:
            with self.subTest(provider=type(provider).__name__):
                provider.completed["thread"] = self.history("completed")["turns"][-1]
                history = self.history("failed")
                history["turns"][-1]["items"][0]["text"] = "newer mirror content"
                history["turns"][-1]["error"] = {"message": "later failure detail"}
                history["turns"].append({"id": "next", "status": "completed", "items": []})
                result = provider.reconcile_history("thread", history)
                self.assertEqual([turn["id"] for turn in result["turns"]], ["earlier", "turn", "next"])
                self.assertEqual(result["turns"][1]["status"], "failed")
                self.assertEqual(result["turns"][1]["items"][0]["text"], "newer mirror content")
                self.assertEqual(result["turns"][1]["error"]["message"], "later failure detail")

    def test_completion_eviction_requires_the_same_durable_terminal_outcome(self):
        for provider in self.providers:
            if not isinstance(provider, (ClaudeRPC, AntigravityRPC)):
                continue
            with self.subTest(provider=type(provider).__name__):
                completed = self.history('completed')['turns'][-1]
                provider.completed['thread'] = completed
                for terminal in ({}, {'turn': 'inProgress'}, {'earlier': 'completed'}, {'turn': 'failed'}):
                    provider.discard_persisted_completion('thread', terminal)
                    self.assertIs(provider.completed['thread'], completed)
                provider.discard_persisted_completion('thread', {'turn': 'completed'})
                self.assertNotIn('thread', provider.completed)
                newer = {**completed, 'id': 'next'}
                provider.completed['thread'] = newer
                provider.discard_persisted_completion('thread', {'turn': 'completed'})
                self.assertIs(provider.completed['thread'], newer)

    def test_antigravity_restart_retains_uncertain_tool_outcomes(self):
        provider = next(provider for provider in self.providers if isinstance(provider, AntigravityRPC))
        history = self.history()
        history["turns"][-1]["items"].append({"id": "tool", "type": "mcpToolCall", "status": "inProgress"})
        result = provider.reconcile_history("thread", history)
        self.assertEqual(result["turns"][-1]["termination"],
                         {"origin": "runtime", "reason": "runtime_state_lost", "execution": "unknown"})
        self.assertEqual(result["turns"][-1]["items"][-1]["status"], "unknown")

    def test_muse_background_updates_survive_foreground_reconciliation(self):
        provider = next(provider for provider in self.providers if isinstance(provider, MuseRPC))
        background = {"id": "earlier", "status": "completed", "items": [
            {"id": "workflow", "nativeKind": "workflow", "status": "inProgress"}]}
        provider.background_turns["thread"] = {"earlier": background}
        provider.completed["thread"] = self.history("completed")["turns"][-1]
        result = provider.reconcile_history("thread", self.history())
        self.assertEqual([turn["id"] for turn in result["turns"]], ["earlier", "turn"])
        self.assertEqual(result["turns"][0]["items"][0]["status"], "inProgress")
        self.assertEqual(result["turns"][-1]["status"], "completed")
        result["turns"][0]["items"][0]["status"] = "caller edit"
        self.assertEqual(background["items"][0]["status"], "inProgress")
