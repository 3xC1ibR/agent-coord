"""Provider-owned child events must not poison application update barriers."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest

from test_codex_app_server import FakeCodex
import test_desktop_runtime as runtime_tests
from agent_coord import desktop_runtime as runtime
from agent_coord.codex_app_server import BrowserSessions
from agent_coord.runtime_recovery import RuntimeRecovery
from agent_coord.store import CoordinationStore


class BookkeepingOwnershipTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = CoordinationStore(Path(temporary.name) / "state.sqlite3")
        self.sessions = BrowserSessions(self.store, temporary.name, rpc_factory=FakeCodex)
        self.addCleanup(self.sessions.close)
        self.thread = self.sessions.create({})["session"]["thread_id"]
        self.sessions.send(self.thread, {"message": "Parent work"})
        self.sessions._await_persistence(self.thread)
        self.recovery = RuntimeRecovery(self.sessions)

    def child_event(self, method, identity="native-child", **params):
        turn = {"id": "child-turn", "status": "completed" if method == "turn/completed" else "inProgress", "items": []}
        self.sessions._event({"method": method, "params": {"threadId": identity, "turn": turn, **params}})

    def test_completed_children_do_not_poison_update_or_create_app_conversations(self):
        for index in range(20):
            child = f"native-child-{index}"
            self.child_event("turn/started", child)
            self.child_event("thread/tokenUsage/updated", child, tokenUsage={"total": {"totalTokens": 20}})
            self.child_event("turn/completed", child)
        self.assertTrue(self.sessions.persistence.flush())
        status = self.sessions.persistence.status()
        self.assertEqual(status["failures"], {})
        self.assertEqual(status["optional_failure_count"], 0)
        self.recovery.prepare("after-children")
        with self.store.reading(), self.store._connection() as db:
            self.assertEqual(db.execute("SELECT thread_id FROM runtime_update_recoveries").fetchall()[0][0], self.thread)
            for table, column in (("sessions", "session_id"), ("browser_sessions", "thread_id"), ("execution_snapshots", "thread_id")):
                self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table} WHERE {column} LIKE 'native-child-%'").fetchone()[0], 0)

    def test_active_child_remains_visible_and_prevents_unverifiable_recovery(self):
        self.child_event("turn/started")
        self.sessions._event({"id": 700, "method": "item/commandExecution/requestApproval",
                              "params": {"threadId": "native-child", "turnId": "child-turn"}})
        self.assertTrue(self.sessions.persistence.flush())
        with self.assertRaisesRegex(RuntimeError, "provider-owned.*native-child"):
            self.recovery.prepare("child-still-active")
        self.assertEqual(self.sessions.active["native-child"], "child-turn")
        self.assertTrue(self.sessions.requests)
        self.assertFalse(any(method == "turn/interrupt" for method, _ in self.sessions.rpc.calls))
        self.child_event("turn/completed")
        self.recovery.prepare("child-finished")

    def test_required_app_failure_is_not_suppressed_or_cleared(self):
        def invalid():
            raise sqlite3.IntegrityError("injected application constraint failure")
        with self.assertLogs("agent_coord.browser_persistence", "ERROR"):
            self.sessions.persistence.submit(invalid, key=self.thread)
            self.assertFalse(self.sessions.persistence.flush())
        with self.assertRaisesRegex(RuntimeError, "1 failed.*" + self.thread):
            self.recovery.prepare("real-failure")
        self.assertIn("application constraint failure", self.sessions.persistence.failure(self.thread))

    def test_existing_application_registration_error_still_fails_closed(self):
        with self.store._connection() as db:
            db.execute("INSERT INTO browser_sessions(thread_id,cwd,name,created_at,updated_at) VALUES ('broken-app',?,'Broken',1,1)", (str(self.store.database_path.parent),))
        with self.assertLogs("agent_coord.browser_persistence", "ERROR"):
            self.child_event("turn/started", "broken-app")
            self.assertFalse(self.sessions.persistence.flush())
        self.assertIn("not registered", self.sessions.persistence.failure("broken-app"))


class ShutdownDrainTests(unittest.TestCase):
    def test_provider_events_emitted_while_closing_are_saved_with_foreground_priority(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = CoordinationStore(Path(temporary.name) / "state.sqlite3")
        runtime._OWNED_DATABASES.add(str(store.database_path.resolve()))
        self.addCleanup(runtime._OWNED_DATABASES.discard, str(store.database_path.resolve()))
        sessions = BrowserSessions(store, temporary.name, rpc_factory=FakeCodex)
        self.addCleanup(sessions.close)
        thread = sessions.create({})["session"]["thread_id"]
        sessions.send(thread, {"message": "Interrupted by the update"})
        sessions._await_persistence(thread)
        attempts, delivered = [], threading.Event()

        def late_completion():
            # Provider output delivered after the drain began, first seen busy.
            attempts.append(1)
            if len(attempts) < 3:
                raise sqlite3.OperationalError("database is locked")

        def provider_reader():
            with sessions.persistence.condition:
                self.assertTrue(sessions.persistence.condition.wait_for(lambda: sessions.persistence.closing, timeout=5))
            sessions.persistence.submit(late_completion, key=thread)
            delivered.set()

        close_provider = sessions.rpc.close
        def close_with_late_event():
            close_provider()
            reader = threading.Thread(target=provider_reader)
            reader.start()
            self.addCleanup(reader.join)
            # Earlier bookkeeping is still saving when the late event arrives.
            sessions.persistence.submit(lambda: delivered.wait(5), key="earlier-provider-event")
        sessions.rpc.close = close_with_late_event
        sessions.shutdown_deadline = time.monotonic() + 10
        with self.assertLogs("agent_coord.browser_persistence", "WARNING") as logs:
            sessions.close()
        report = sessions.shutdown_report
        self.assertNotIn("unsaved", "\n".join(logs.output))
        self.assertTrue(report["clean"], report)
        self.assertEqual(len(attempts), 3)
        self.assertGreaterEqual(report["submitted_while_closing"], 1)
        self.assertGreaterEqual(report["retries_while_closing"], 2)
        self.assertLessEqual({"active_operation", "queue_depth", "admission_timeouts"}, set(report["writer"]))
        self.assertIn("persistence.drain", store.database_metrics()["operations"])
        self.assertIsNotNone(store.get_session(thread)["ended_at"])


class BookkeepingUpdateProcessTests(unittest.TestCase):
    def test_two_guarded_updates_after_native_child_activity(self):
        # Exercise copied runtime payloads, real subprocess transport and the
        # public update API. Every database, provider and signal is isolated.
        fixture = runtime_tests.RuntimeTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.env["RIBBON_FAKE_PERSIST"] = "1"
        executable = fixture.root / "bin/codex"
        child_activity = """
    child = 'provider-owned-child-' + turn['id']
    child_turn = {'id':'child-turn','status':'inProgress','items':[]}
    emit({'method':'turn/started','params':{'threadId':child,'turn':child_turn}})
    child_turn['status'] = 'completed'
    emit({'method':'turn/completed','params':{'threadId':child,'turn':child_turn}})
    if turn['items'][0]['content'][0].get('text') == 'held-work': return
"""
        executable.write_text(executable.read_text().replace(
            "    if turn['items'][0]['content'][0].get('text') == 'held-work': return", child_activity))
        first, record, thread, staged, _ = fixture.prepare_requested_update(message="held-work")
        for generation in (2, 3):
            if generation == 3:
                fixture.wait(lambda: not fixture.api(current["url"], f"api/browser/sessions/{thread}")["running"])
                fixture.api(current["url"], f"api/browser/sessions/{thread}/messages", {"message": "held-work"})
                candidate = fixture.root / "SecondCandidate/Contents/Resources"
                shutil.copytree(fixture.resources, candidate)
                metadata = json.loads((candidate / "backend.json").read_text())
                metadata["runtime_version"] = "3+3"
                (candidate / "backend.json").write_text(json.dumps(metadata))
                staged = fixture.launch(candidate)
                record = runtime.discover(fixture.database)
            fixture.wait(lambda: runtime.control(record, "status")["database_metrics"]["persistence"]["pending"] == 0)
            persistence = runtime.control(record, "status")["database_metrics"]["persistence"]
            self.assertEqual(persistence["failure_count"], 0)
            self.assertEqual(persistence["optional_failure_count"], 0)
            runtime.control(record, "activate-update", request_id=f"child-activity-update-{generation}", payload=staged["pending"])
            current = fixture.wait_update("committed")
            self.assertEqual(current["payload"], staged["pending"])
            self.assertNotEqual(current["instance"], first["instance"])
            fixture.wait(lambda: runtime.control(runtime.discover(fixture.database), "status")["update"]["recovery"]["counts"].get("continued") == 1)
            self.assertFalse(runtime.control(runtime.discover(fixture.database), "status")["paused"])
            self.assertEqual(len([call for call in fixture.calls() if call.get("method") == "turn/start"]), (generation - 1) * 2)
        self.assertNotIn("Browser bookkeeping failed", fixture.log())


if __name__ == "__main__":
    unittest.main()
