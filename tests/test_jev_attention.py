from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))

from agent_coord.attention import (AttentionClassifier, AttentionStore, ClassificationError,
                                   CHOICES, MODEL, classify, context_for, history_messages, load_config, terminal_messages)
from agent_coord.codex_app_server import BrowserSessions
from agent_coord.store import CoordinationError, CoordinationStore
from test_codex_app_server import FakeCodex


def decision(choice="update", confidence=0.9):
    return {"choice": choice, "confidence": confidence, "probabilities": {
        key: 0.92 if key == choice else 0.02 for key in CHOICES}}


class JevAPITests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.key = self.root / "key"
        self.key.write_text("test-only-secret\n")
        self.config = {"api_key_file": str(self.key)}
        self.connection = Mock()
        self.response = self.connection.getresponse.return_value
        self.response.status = 200
        self.payload = {"model": MODEL, "answers": {"handoff": {"type": "choice", **decision()}}}
        self.factory = patch("agent_coord.attention.http.client.HTTPSConnection", return_value=self.connection).start()
        self.addCleanup(patch.stopall)

    def call(self):
        self.response.read.return_value = json.dumps(self.payload).encode()
        return classify({"reply": "test-only-secret"}, self.config)

    def test_fixed_endpoint_bounded_response_and_key_only_in_header(self):
        self.assertEqual(self.call(), decision())
        self.factory.assert_called_once_with("api.typesafe.ai", timeout=10)
        args, kwargs = self.connection.request.call_args
        self.assertEqual(args, ("POST", "/v1/systemone"))
        self.assertNotIn(b"test-only-secret", kwargs["body"])
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-only-secret")
        self.response.read.assert_called_once_with(65537)
        self.connection.close.assert_called_once()

    def test_redirect_auth_failure_and_network_exception_never_expose_content(self):
        for code in (302, 401, 429, 500):
            self.response.status = code
            with self.assertRaisesRegex(ClassificationError, "^http_" + str(code) + "$"):
                self.call()
        self.connection.request.side_effect = OSError("test-only-secret")
        with self.assertRaisesRegex(ClassificationError, "^request_failed$"):
            self.call()
        self.assertEqual(self.connection.close.call_count, 5)

    def test_malformed_and_nonfinite_answers_are_rejected(self):
        answer = self.payload["answers"]["handoff"]
        for key, value in (("choice", "hidden"), ("confidence", float("nan")),
                           ("confidence", True), ("probabilities", {"update": 1}),
                           ("probabilities", {"update": 0.9, "findings": -0.1, "review": 0.2})):
            original = answer[key]
            answer[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ClassificationError):
                self.call()
            answer[key] = original
        self.response.read.return_value = b"x" * 65537
        with self.assertRaisesRegex(ClassificationError, "invalid_response"):
            classify({}, self.config)

    def test_configuration_requires_explicit_opt_in_and_stores_a_path(self):
        self.assertIsNone(load_config(self.root / "state.sqlite3"))
        config = self.root / "jev.json"
        config.write_text(json.dumps({"enabled": True, **self.config}))
        self.assertEqual(load_config(self.root / "state.sqlite3"), self.config)
        config.write_text('{"enabled":false}')
        self.assertIsNone(load_config(self.root / "state.sqlite3"))
        self.key.unlink()
        with self.assertRaisesRegex(ClassificationError, "credential_unavailable"):
            classify({}, self.config)
        self.factory.assert_not_called()


class JevAttentionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.now = 1000.0
        self.store = CoordinationStore(self.root / "state.sqlite3", clock=lambda: self.now)
        self.store.register(session_id="terminal", client="codex", cwd=str(self.root))
        self.sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(self.sessions.close)
        self.request = Mock(return_value=decision())
        self.worker = AttentionClassifier(self.sessions, config={"api_key_file": "unused"}, classify_fn=self.request, start=False)
        self.addCleanup(self.worker.close)

    def finish(self, turn="one", status="completed"):
        self.now += 10
        self.store.threads.start_turn("terminal", turn_id=turn, prompt="Check the backfill status.")
        self.now += 1
        self.store.threads.checkpoint("terminal", {"phase": "finished", "summary": "Backfill complete.", "next_actor": "nobody", "next_action": ""})
        self.store.threads.finish_turn("terminal", status=status)
        self.sessions.histories["terminal"] = {"id": "terminal", "turns": [{"id": turn, "status": status, "items": [
            {"type": "userMessage", "content": [{"type": "text", "text": "Check the backfill status."}]},
            {"type": "commandExecution", "aggregatedOutput": "PRIVATE TOOL OUTPUT"},
            {"type": "reasoning", "text": "PRIVATE REASONING"},
            {"type": "agentMessage", "phase": "commentary", "text": "PRIVATE PROGRESS"},
            {"type": "agentMessage", "phase": "final_answer", "text": "The backfill finished. All records are present."},
        ]}]}
        self.sessions._save_history("terminal")
        return self.thread()

    def thread(self):
        return self.sessions.work_thread("terminal")

    def handle(self):
        item = self.thread()
        return self.sessions.update_work_thread("terminal", {"handled": True,
            "handled_checkpoint_id": item["checkpoint"]["id"], "handled_completion_id": item["turn_completion"]["id"]})

    def legacy_write(self, item, *, policy=1, choice="conversation", status="classified"):
        # The pre-upgrade worker unconditionally replaces its one row per reply.
        with self.store._connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS response_classifications (
                completion_id INTEGER PRIMARY KEY, checkpoint_id INTEGER NOT NULL,
                model TEXT NOT NULL, policy_version INTEGER NOT NULL, status TEXT NOT NULL,
                choice TEXT, confidence REAL, probabilities_json TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                retry_at REAL NOT NULL DEFAULT 0, lease_token TEXT, lease_until REAL NOT NULL DEFAULT 0,
                error TEXT, updated_at REAL NOT NULL)""")
            db.execute("""INSERT OR REPLACE INTO response_classifications
                (completion_id, checkpoint_id, model, policy_version, status, choice, confidence,
                 probabilities_json, attempts, lease_token, lease_until, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 0.9, ?, 1, ?, ?, ?)""",
                (item["turn_completion"]["id"], item["checkpoint"]["id"], MODEL, policy, status, choice,
                 json.dumps(decision(choice)["probabilities"]), "legacy" if status == "pending" else None,
                 self.now + 45 if status == "pending" else 0, self.now))

    def test_information_enters_attention_and_reading_returns_to_its_stage(self):
        self.finish()
        self.worker.scan()
        item = self.thread()
        self.assertEqual(item["response_state"], "update")
        self.assertTrue(item["needs_attention"])
        self.assertEqual(item["attention_reason"], "update")
        self.assertFalse(item["can_handle_response"])
        item = self.sessions.update_work_thread("terminal", {"seen": True})
        self.assertEqual(item["response_state"], "available")
        self.assertEqual(item["attention"], "now")
        self.assertTrue(item["unhandled_response"])
        self.assertNotIn("PRIVATE", json.dumps(self.request.call_args.args[0]))
        self.assertEqual(self.sessions.rpc.calls, [])

    def test_review_and_blocker_survive_reading_but_optional_results_recede(self):
        for i, choice in enumerate(("blocked", "review", "update", "findings", "done")):
            with self.subTest(choice=choice):
                self.finish(str(i))
                self.request.return_value = decision(choice)
                self.worker.scan()
                self.assertTrue(self.thread()["needs_attention"])
                self.assertEqual(self.thread()["attention_reason"], choice)
                item = self.sessions.update_work_thread("terminal", {"seen": True})
                self.assertEqual(item["needs_attention"], choice in {"blocked", "review"})
                self.assertEqual(item["can_handle_response"], choice == "review")
                if choice == "blocked":
                    with self.assertRaises(CoordinationError):
                        self.handle()
                elif choice == "review":
                    self.assertEqual(self.handle()["response_state"], "available")
                else:
                    self.assertEqual(item["response_state"], "available")
                self.assertEqual(item["attention"], "now")

    def test_classification_keeps_inquiries_and_status_reports_out_of_done(self):
        for choice, prior, expected in (("findings", "investigation", "investigation"),
                                        ("update", "deployment", "deployment"),
                                        ("done", "implementation", "finished")):
            with self.subTest(choice=choice):
                self.store.threads.checkpoint("terminal", {"phase": prior, "summary": "Work in progress."})
                self.finish(choice)
                self.request.return_value = decision(choice)
                self.worker.scan()
                self.assertEqual(self.thread()["work_phase"], expected)
                self.sessions.update_work_thread("terminal", {"seen": True})
                self.assertEqual(self.thread()["work_phase"], expected)
                self.assertFalse(self.thread()["needs_attention"])

    def test_policy_upgrade_reclassifies_without_changing_receipts_or_placement(self):
        self.finish()
        self.worker.scan()
        before = self.sessions.update_work_thread("terminal", {"seen": True, "pinned": True, "attention": "later"})
        with patch("agent_coord.attention.POLICY_VERSION", 3):
            self.assertIsNone(self.thread()["response_classification"])
            self.worker.scan()
        self.worker.scan()  # The old policy also retains its own settled result.
        self.assertEqual(self.request.call_count, 2)
        for key in ("attention", "pinned", "seen_completion_id", "handled_completion_id"):
            self.assertEqual(self.thread()[key], before[key])

    def test_legacy_writes_cannot_change_reason_stage_or_revive_a_read_reply(self):
        item = self.finish()
        self.request.return_value = decision("done")
        self.worker.scan()
        for seen in (False, True):
            if seen:
                self.sessions.update_work_thread("terminal", {"seen": True})
            for policy, choice, status in ((1, "conversation", "classified"), (2, "findings", "uncertain"),
                                            (1, "informational", "classified")):
                with self.subTest(seen=seen, policy=policy, choice=choice):
                    self.legacy_write(item, policy=policy, choice=choice, status=status)
                    AttentionStore(self.store)  # Another app process initializes/migrates.
                    self.worker.scan()
                    current = self.thread()
                    self.assertEqual(current["response_classification"]["choice"], "done")
                    self.assertEqual(current["work_phase"], "finished")
                    self.assertEqual(current["needs_attention"], not seen)
                    self.assertEqual(current["attention_reason"], None if seen else "done")
        self.request.assert_called_once()

    def test_policy_workers_keep_independent_leases_and_results(self):
        item = self.finish()
        cache = self.store.threads.attention
        with patch("agent_coord.attention.POLICY_VERSION", 1):
            older = cache.claim(item)
        current = cache.claim(item)
        self.assertIsNotNone(older)
        self.assertIsNotNone(current)
        with patch("agent_coord.attention.POLICY_VERSION", 1):
            self.assertTrue(cache.save(item, older, result=decision("findings")))
        self.assertEqual(self.thread()["response_classification"]["status"], "pending")
        self.assertTrue(cache.save(item, current, result=decision("done")))
        with patch("agent_coord.attention.POLICY_VERSION", 1):
            self.assertIsNone(cache.claim(item))
            self.assertEqual(self.thread()["response_classification"]["choice"], "findings")
        self.assertIsNone(cache.claim(item))
        self.assertEqual(self.thread()["attention_reason"], "done")

    def test_settled_legacy_results_migrate_once_without_copying_pending_leases(self):
        item = self.finish()
        self.legacy_write(item, policy=2, choice="done")
        AttentionStore(self.store)
        self.worker.scan()
        self.request.assert_not_called()
        self.assertEqual(self.thread()["attention_reason"], "done")
        self.legacy_write(item, policy=2, choice="findings")
        AttentionStore(self.store)
        self.assertEqual(self.thread()["attention_reason"], "done")
        item = self.finish("next")
        self.legacy_write(item, policy=2, status="pending")
        AttentionStore(self.store)
        self.assertIsNone(self.thread()["response_classification"])
        self.worker.scan()
        self.request.assert_called_once()

    def test_missing_uncertain_and_unavailable_results_never_hide_replies(self):
        self.finish()
        self.assertEqual(self.thread()["response_state"], "reply")
        self.request.return_value = decision(confidence=0.3)
        self.worker.scan()
        self.assertEqual(self.thread()["response_classification"]["status"], "uncertain")
        self.assertEqual(self.thread()["response_state"], "reply")
        self.worker.scan()
        self.request.assert_called_once()
        self.finish("two")
        self.request.side_effect = ClassificationError("request_failed")
        for _ in range(5):
            self.worker.scan()
            self.now += 500
        self.assertEqual(self.request.call_count, 4)  # Three bounded failure attempts.
        self.assertEqual(self.thread()["response_state"], "reply")

    def test_new_turn_and_new_checkpoint_invalidate_old_result(self):
        old = self.finish()
        token = self.store.threads.attention.claim(old)
        self.finish("two")
        self.store.threads.attention.save(old, token, result=decision())
        self.assertIsNone(self.thread()["response_classification"])
        self.worker.scan()
        self.assertEqual(self.thread()["response_state"], "update")
        self.store.threads.checkpoint("terminal", {"phase": "discussion", "summary": "A new proposal.", "next_actor": "nobody", "next_action": ""})
        self.assertIsNone(self.thread()["response_classification"])
        self.assertEqual(self.thread()["response_state"], "reply")

    def test_required_input_running_failures_and_handled_receipts_take_priority(self):
        self.finish()
        self.worker.scan()
        self.sessions.requests["question"] = {"id": "q", "params": {"threadId": "terminal"}}
        self.assertEqual(self.thread()["response_state"], "input")
        self.sessions.requests.clear()
        self.sessions.active["terminal"] = "one"
        self.assertEqual(self.thread()["response_state"], "working")
        self.sessions.active.clear()
        self.store.threads.checkpoint("terminal", {"phase": "planning", "summary": "Approval needed.", "next_actor": "user", "next_action": "Choose a scope."})
        self.assertEqual(self.thread()["response_state"], "input")
        self.worker.scan()
        self.assertEqual(self.request.call_count, 2)
        self.finish("two", status="failed")
        self.worker.scan()
        self.assertEqual(self.thread()["response_state"], "failed")
        self.assertEqual(self.request.call_count, 2)
        old = self.finish("three")
        token = self.store.threads.attention.claim(old)
        self.handle()
        self.store.threads.attention.save(old, token, result=decision("review"))
        self.assertEqual(self.thread()["response_state"], "available")

    def test_pins_placement_and_restart_do_not_reclassify_or_reorganize(self):
        self.finish()
        self.store.threads.update("terminal", attention="later", pinned=True)
        self.worker.scan()
        other = BrowserSessions(CoordinationStore(self.store.database_path), rpc_factory=FakeCodex)
        self.addCleanup(other.close)
        AttentionClassifier(other, config={"api_key_file": "unused"}, classify_fn=self.request, start=False).scan()
        self.request.assert_called_once()
        item = other.work_thread("terminal")
        self.assertEqual(item["attention"], "later")
        self.assertTrue(item["pinned"])
        self.assertFalse(item["needs_attention"])
        self.assertEqual(item["response_state"], "update")

    def test_two_process_claims_and_expired_lease_recovery(self):
        item = self.finish()
        stores = [self.store.threads.attention, AttentionStore(CoordinationStore(self.store.database_path, clock=lambda: self.now))]
        barrier = threading.Barrier(2)
        claims = []
        def claim(store):
            barrier.wait()
            claims.append(store.claim(item))
        workers = [threading.Thread(target=claim, args=(store,)) for store in stores]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        tokens = [token for token in claims if token]
        self.assertEqual(len(tokens), 1)
        self.now += 50
        recovered = stores[1].claim(item)
        self.assertIsNotNone(recovered)
        self.assertFalse(stores[0].save(item, tokens[0], result=decision("review")))
        self.assertTrue(stores[1].save(item, recovered, result=decision()))
        self.assertEqual(self.thread()["response_state"], "update")

    def test_incomplete_or_different_history_does_not_classify(self):
        item = self.finish()
        history = self.sessions.histories["terminal"]
        history["turns"][0]["status"] = "inProgress"
        self.assertIsNone(history_messages(history, item))
        history["turns"][0]["status"] = "completed"
        history["turns"][0]["items"].pop()
        self.assertIsNone(history_messages(history, item))
        self.sessions._save_history("terminal")
        with self.assertRaisesRegex(ClassificationError, "context_unavailable"):
            context_for(self.store, item)

    def test_truncated_answer_cannot_be_silently_dismissed_and_keeps_actual_confidence(self):
        self.finish()
        self.sessions.histories["terminal"]["turns"][0]["items"][-1]["text"] = "Result. " * 1000 + "Please approve the remaining work."
        self.sessions._save_history("terminal")
        self.worker.scan()
        item = self.thread()
        self.assertEqual(item["response_state"], "reply")
        self.assertEqual(item["response_classification"]["error"], "context_truncated")
        self.assertEqual(item["response_classification"]["confidence"], 0.9)

    def test_background_worker_publishes_completion_and_stops_cleanly(self):
        self.finish()
        published = threading.Event()
        original = self.sessions._publish
        def publish(method, params):
            original(method, params)
            if method == "thread/classified":
                published.set()
        with patch.object(self.sessions, "_publish", side_effect=publish):
            worker = AttentionClassifier(self.sessions, config={"api_key_file": "unused"}, classify_fn=self.request)
            try:
                self.assertTrue(published.wait(3))
                self.assertEqual(self.thread()["response_state"], "update")
            finally:
                worker.close()
            self.assertFalse(worker.worker.is_alive())

    def test_terminal_context_is_bounded_to_current_reply_and_excludes_setup(self):
        item = self.finish()
        item.update(thread_id=str(uuid.uuid4()), turn_started_at=10, turn_completion={"completed_at": 20})
        directory = self.root / "sessions"
        directory.mkdir()
        path = directory / ("rollout-" + item["thread_id"] + ".jsonl")
        def record(role, text, timestamp, phase=None):
            return json.dumps({"type": "response_item", "timestamp": f"1970-01-01T00:00:{timestamp:02}Z", "payload": {
                "type": "message", "role": role, "phase": phase, "content": [{"type": "input_text", "text": text}]}}) + "\n"
        data = record("user", "# AGENTS.md instructions PRIVATE", 10) + record("user", "Status?", 10)
        data += record("assistant", "PRIVATE PROGRESS", 15, "commentary") + record("assistant", "Complete.", 20, "final_answer")
        path.write_text(data)
        with patch.dict("os.environ", {"CODEX_HOME": str(self.root)}):
            messages = terminal_messages(item)
            self.assertEqual([m["text"] for m in messages], ["Status?", "Complete."])
            item["turn_started_at"] = 21
            self.assertIsNone(terminal_messages(item))
            item["turn_started_at"] = 10
            path.write_text(data + record("user", "A later request.", 30))
            self.assertIsNone(terminal_messages(item))
