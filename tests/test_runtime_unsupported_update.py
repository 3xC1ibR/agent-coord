"""Explicit backend updates bound Kimi, Antigravity and Muse work.

Regression for the November 23, 2026 report: an installed build stayed
"accepted" indefinitely while coordinator-owned provider turns ran. These tests
use the real browser sessions, provider adapters and stdio connections against
an independent wire-level fixture process for each provider. Only the provider
transport's presence and the candidate probe are substituted; a separate
process-level test exercises the actual runtime, transport and supervisor.
"""
from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/agent-coord/scripts"))
from test_codex_app_server import FakeCodex  # noqa: E402
from agent_coord import desktop_runtime as runtime  # noqa: E402
from agent_coord import runtime_update  # noqa: E402
from agent_coord.antigravity import AntigravityConnection, AntigravityRPC  # noqa: E402
from agent_coord.codex_app_server import BrowserBusyError, BrowserSessions  # noqa: E402
from agent_coord.kimi import KimiConnection, KimiRPC  # noqa: E402
from agent_coord.muse_code import MuseConnection, MuseRPC  # noqa: E402
from agent_coord.runtime_recovery import RuntimeRecovery  # noqa: E402
from agent_coord.store import CoordinationStore  # noqa: E402

FIXTURE = ROOT / "tests/fixtures/update_interruption_provider.py"
PROVIDERS = ("kimi", "antigravity", "muse")
CONTINUATION = "[Automatic continuation after a Ribbon Field backend update]"
_readiness = runtime.uninterrupted_readiness


class _TransportView:
    """The real sessions as a coordinator with a healthy provider transport sees them."""
    projection = SimpleNamespace(error=None)

    def __init__(self, sessions):
        self.__dict__["_sessions"] = sessions

    def __getattr__(self, name):
        return getattr(self._sessions, name)


def readiness_with_transport(sessions):
    if hasattr(sessions, "projection"):
        return _readiness(sessions)
    return _readiness(_TransportView(sessions))


class UnsupportedProviderUpdateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ribbon-unsupported-update-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.fixture_root = self.root / "fixture"
        self.fixture_root.mkdir()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.database = self.root / "state.sqlite3"
        environment = patch.dict(os.environ, {"RIBBON_UPDATE_FIXTURE_ROOT": str(self.fixture_root),
                                              "KIMI_CODE_HOME": str(self.root / "kimi-home")})
        environment.start()
        self.addCleanup(environment.stop)
        for name, value in (("uninterrupted_readiness", readiness_with_transport),
                            ("UPDATE_UNSUPPORTED_GRACE_SECONDS", 0), ("UPDATE_INTERRUPTION_SECONDS", 10),
                            ("UPDATE_CONFIRMATION_SECONDS", 10)):
            # create=True lets the unchanged baseline reach the behavioral checks.
            patcher = patch.object(runtime, name, value, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.store = CoordinationStore(self.database)
        self.sessions = self.make_sessions()
        self.directory = runtime.runtime_directory(self.database)
        self.directory.mkdir(parents=True, exist_ok=True)

    # -- harness ---------------------------------------------------------------

    def connection(self, kind, mode):
        def create(store, callback, options):
            return kind(store, callback, options, command=[sys.executable, str(FIXTURE), mode])
        return create

    def make_sessions(self):
        sessions = BrowserSessions(self.store, str(self.workspace), rpc_factory=FakeCodex,
            kimi_factory=lambda store, event: KimiRPC(store, event,
                connection_factory=self.connection(KimiConnection, "kimi")),
            antigravity_factory=lambda store, event: AntigravityRPC(store, event,
                connection_factory=self.connection(AntigravityConnection, "antigravity")),
            muse_factory=lambda store, event: MuseRPC(store, event,
                connection_factory=self.connection(MuseConnection, "muse")))
        self.addCleanup(self.close_sessions, sessions)
        return sessions

    @staticmethod
    def close_sessions(sessions):
        if not sessions.closed:
            sessions.close()

    def owner(self, sessions=None, *, validation=None):
        owner = runtime.Runtime(self.database, self.root / "previous", -1)
        owner.supervised = True
        owner.server = SimpleNamespace(browser_sessions=sessions or self.sessions)
        owner.pending = self.root / "candidate"
        owner.validate_candidate = validation or (lambda candidate, capacity: None)
        return owner

    def start(self, client, message="HOLD the assignment"):
        thread = self.sessions.create({"client": client, "cwd": str(self.workspace)})["session"]["thread_id"]
        self.sessions.send(thread, {"message": message})
        if "HOLD" not in message and "ASK" not in message:
            # The fixture answers at once; read the finished turn identity.
            self.wait(lambda: (self.store.threads.get(thread, history=False)["turn_completion"] or {}).get("status"))
            return thread, self.store.threads.get(thread, history=False)["turn_id"]
        turn = self.wait(lambda: self.sessions.active.get(thread))
        self.wait(lambda: any(entry["kind"] == "prompt" and entry["session"] == self.native(client, thread)
                              for entry in self.events()))
        return thread, turn

    def native(self, client, thread):
        table = {"kimi": "kimi_sessions", "antigravity": "antigravity_sessions", "muse": "muse_sessions"}[client]
        with closing(sqlite3.connect(self.database)) as db:
            return db.execute(f"SELECT native_id FROM {table} WHERE thread_id=?", (thread,)).fetchone()[0]

    def events(self, kind=None):
        path = self.fixture_root / "provider.jsonl"
        if not path.exists():
            return []
        values = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        return [value for value in values if kind is None or value["kind"] == kind]

    def prompts(self, session):
        return [entry for entry in self.events("prompt") if entry["session"] == session]

    def wait(self, predicate, timeout=15):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            value = predicate()
            if value:
                return value
            time.sleep(.02)
        self.fail("Condition was not met. Provider log: " + json.dumps(self.events())[-3000:])

    def drive(self, owner, *, timeout=15):
        """Run the serve loop's handover decision until it permits transfer."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with owner.mutex:
                if owner.prepare_handover():
                    return True
            time.sleep(.02)
        update = runtime_update.read(self.directory)
        self.fail(f"The explicit update did not reach handover within {timeout} s; update={update}")

    def request(self, owner, identity="explicit-update"):
        owner.request_update(identity, str(owner.pending))
        return identity

    def replace_coordinator(self, owner, *, rollback=False):
        """Model the uninterrupted handover: exit, snapshot, trial, then commit or roll back."""
        sessions = owner.server.browser_sessions
        update = runtime_update.write(self.directory, owner.update_requested, phase="stopping", uninterrupted=True)
        sessions.transport_handover = True
        sessions.runtime_handover = True
        sessions.close()
        update = runtime_update.snapshot(str(self.database), self.directory, update)
        if rollback:
            with closing(sqlite3.connect(self.database)) as db:
                db.execute("CREATE TABLE trial_migration (id INTEGER)")
                db.commit()
            runtime_update.restore(str(self.database), self.directory, update)
        else:
            runtime_update.write(self.directory, update, phase="committed")
        self.store = CoordinationStore(self.database)
        replacement = self.make_sessions()
        self.sessions = replacement
        self.adopt_queue(sessions, replacement)
        return self.serve(replacement)

    def adopt_queue(self, previous, replacement):
        # serve() adopts accepted queued messages from RIBBON_RUNTIME_QUEUE_OWNER.
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("UPDATE browser_message_queue SET owner=? WHERE owner=? AND state='queued'",
                       (replacement.queue.owner, previous.queue.owner))
            db.commit()

    def serve(self, sessions):
        owner = self.owner(sessions)
        with owner.mutex:
            owner.resume_work()
        self.wait(lambda: not owner.recovery_running)
        return owner

    def recovery(self, thread):
        with closing(sqlite3.connect(self.database)) as db:
            db.row_factory = sqlite3.Row
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='runtime_update_recoveries'").fetchone():
                return None, None
            row = db.execute("SELECT * FROM runtime_update_recoveries WHERE thread_id=?", (thread,)).fetchone()
            interruption = db.execute("SELECT * FROM runtime_update_interruptions WHERE thread_id=?",
                                      (thread,)).fetchone()
        return (dict(row) if row else None), (dict(interruption) if interruption else None)

    def assert_not_user_stopped(self, thread, turn):
        self.assertFalse(self.store.threads.stop_requested(thread, turn))
        with closing(sqlite3.connect(self.database)) as db:
            pause = db.execute("SELECT paused, pause_kind FROM browser_inbox_wake WHERE thread_id=?", (thread,)).fetchone()
        self.assertFalse(pause and pause[0], f"Inbox wake was paused: {pause}")

    # -- behavior --------------------------------------------------------------

    def test_explicit_update_pauses_and_continues_each_provider_once(self):
        work = {client: self.start(client) for client in PROVIDERS}
        natives = {client: self.native(client, thread) for client, (thread, _) in work.items()}
        original_processes = {client: self.sessions._rpc(thread).connections[thread].process
                              for client, (thread, _) in work.items()}
        for client, process in original_processes.items():
            self.assertIsNone(process.poll(), client)
        owner = self.owner()
        self.request(owner)
        self.assertTrue(self.drive(owner))
        update = runtime_update.read(self.directory)
        self.assertTrue(update["interrupted_unsupported"])
        for client, (thread, turn) in work.items():
            row, interruption = self.recovery(thread)
            self.assertEqual((interruption["state"], interruption["turn_id"]), ("confirmed", turn), client)
            self.assertEqual(row["state"], "prepared", client)
            # The adapter's native stop ended the original process; its turn never
            # completed. Kimi and Antigravity write session/cancel and close at
            # once, so the fixture may exit before reading that notification.
            original = self.prompts(natives[client])[0]
            if client == "muse":
                self.assertIn("turn/interrupt", [e["method"] for e in self.events("request") if e["pid"] == original["pid"]])
            process = original_processes[client]
            self.assertEqual(process.pid, original["pid"], client)
            self.assertIsNotNone(process.poll(), f"{client} still owns its original process at handover")
            self.assertNotIn(original["turn"], [e["turn"] for e in self.events("prompt-ended")
                                                if e["stop_reason"] in {"end_turn", "completed"}])
            self.assertEqual(self.store.threads.get(thread, history=False)["turn_completion"]["status"], "interrupted")
            self.assert_not_user_stopped(thread, turn)
        replacement = self.replace_coordinator(owner)
        for client, (thread, turn) in work.items():
            native = natives[client]
            self.wait(lambda: len(self.prompts(native)) == 2)
            continuation = self.prompts(native)[1]
            self.assertTrue(continuation["continuation"], client)
            resumed = [e for e in self.events("request") if e["session"] == native
                       and e["method"] in {"session/resume", "session/load"}]
            self.assertTrue(resumed, f"{client} did not resume its saved native session")
            self.assertNotEqual(continuation["pid"], self.prompts(native)[0]["pid"])
            self.wait(lambda: self.recovery(thread)[0]["state"] == "continued")
        # A later restart of the serving coordinator never repeats a continuation.
        self.replace_after_restart(replacement)
        time.sleep(.5)
        for client, (thread, _) in work.items():
            self.assertEqual(len(self.prompts(natives[client])), 2, client)
        status = runtime_update.read(self.directory)
        self.assertEqual(status["phase"], "committed")

    def replace_after_restart(self, owner):
        previous = owner.server.browser_sessions
        previous.close()
        self.store = CoordinationStore(self.database)
        self.sessions = self.make_sessions()
        self.adopt_queue(previous, self.sessions)
        return self.serve(self.sessions)

    def test_supported_codex_turn_is_not_interrupted(self):
        codex = self.sessions.create({"cwd": str(self.workspace)})["session"]["thread_id"]
        self.sessions.send(codex, {"message": "long codex work"})
        codex_turn = self.wait(lambda: self.sessions.active.get(codex))
        thread, _ = self.start("kimi")
        owner = self.owner()
        self.request(owner)
        self.assertTrue(self.drive(owner))
        self.assertEqual(self.sessions.active.get(codex), codex_turn)
        self.assertNotIn("turn/interrupt", [call[0] for call in self.sessions.rpc.calls])
        self.assertIsNone(self.recovery(codex)[0])
        self.assertEqual(self.recovery(thread)[1]["state"], "confirmed")

    def test_passive_update_never_interrupts(self):
        thread, turn = self.start("kimi")
        owner = self.owner()
        for _ in range(20):
            with owner.mutex:
                self.assertFalse(owner.prepare_handover())
        self.assertEqual(self.sessions.active.get(thread), turn)
        self.assertFalse(self.events("prompt-ended"))
        self.assertFalse(self.sessions.admission.paused)

    def test_grace_period_lets_a_turn_finish_and_reports_why_it_waits(self):
        thread, turn = self.start("antigravity")
        native = self.native("antigravity", thread)
        owner = self.owner()
        with patch.object(runtime, "UPDATE_UNSUPPORTED_GRACE_SECONDS", 60):
            self.request(owner)
            with owner.mutex:
                self.assertFalse(owner.prepare_handover())
            update = runtime_update.read(self.directory)
            self.assertEqual(update["phase"], "accepted")
            self.assertIn("Turns still running after 60 seconds pause for the update", update["notice"])
            self.assertEqual([entry["thread_id"] for entry in update["unsupported"]], [thread])
            self.assertFalse(self.sessions.admission.paused)
            (self.fixture_root / f"release-{native}").touch()
            self.wait(lambda: thread not in self.sessions.active)
            self.assertTrue(self.drive(owner))
        self.assertIsNone(self.recovery(thread)[0])
        self.assertEqual([e["stop_reason"] for e in self.events("prompt-ended")], ["end_turn"])

    def test_pending_approval_waits_with_admission_open_and_is_never_withdrawn(self):
        thread, turn = self.start("kimi", "ASK then HOLD")
        self.wait(lambda: any((v.get("params") or {}).get("threadId") == thread for v in self.sessions.requests.values()))
        owner = self.owner()
        self.request(owner)
        for _ in range(10):
            with owner.mutex:
                self.assertFalse(owner.prepare_handover())
        update = runtime_update.read(self.directory)
        self.assertEqual(update["phase"], "accepted")
        self.assertIn("waiting for you to answer the request", update["notice"])
        self.assertFalse(self.sessions.admission.paused)
        self.assertFalse(self.events("prompt-ended"))
        # The user can still answer: admission stayed open for exactly this.
        key = next(k for k, v in self.sessions.requests.items() if (v.get("params") or {}).get("threadId") == thread)
        self.sessions.answer(thread, key, {"decision": "accept"})
        self.assertTrue(self.drive(owner))
        self.assertEqual(self.recovery(thread)[1]["state"], "confirmed")

    def test_approval_after_plan_withdraws_interruption_and_paused_turns_continue_without_candidate(self):
        asking, asking_turn = self.start("kimi")
        other, other_turn = self.start("muse")
        asking_native = self.native("kimi", asking)
        plan = RuntimeRecovery.plan_interruption
        planned = []

        def plan_then_request_approval(recovery, update_id, thread_id, turn_id, client):
            plan(recovery, update_id, thread_id, turn_id, client)
            planned.append(thread_id)
            if len(planned) == 2:
                # Both turns are durably planned; an approval now arrives
                # before the update signals the asking provider.
                (self.fixture_root / f"ask-{asking_native}").touch()
                self.wait(lambda: any((v.get("params") or {}).get("threadId") == asking
                                      for v in self.sessions.requests.values()))
        owner = self.owner()
        self.request(owner)
        with patch.object(RuntimeRecovery, "plan_interruption", plan_then_request_approval):
            end = time.monotonic() + 15
            while runtime_update.read(self.directory)["phase"] != "failed" and time.monotonic() < end:
                with owner.mutex:
                    self.assertFalse(owner.prepare_handover())
                time.sleep(.02)
        update = runtime_update.read(self.directory)
        self.assertEqual(update["phase"], "failed")
        self.assertIn("could not pause every conversation it needed to (active unsupported provider: kimi)",
                      update["error"])
        row, interruption = self.recovery(asking)
        self.assertEqual((interruption["state"], row["state"]), ("withdrawn", "skipped"))
        self.assertEqual(self.sessions.active.get(asking), asking_turn)
        self.assertTrue(any((v.get("params") or {}).get("threadId") == asking for v in self.sessions.requests.values()))
        # The paused Muse turn continues now in the current coordinator; no candidate ever ran.
        other_native = self.native("muse", other)
        self.wait(lambda: len(self.prompts(other_native)) == 2)
        self.assertTrue(self.prompts(other_native)[1]["continuation"])
        self.wait(lambda: self.recovery(other)[0]["state"] == "continued")
        self.wait(lambda: not self.sessions.admission.paused)
        self.assertEqual(len(self.prompts(asking_native)), 1)

    def test_natural_completion_before_interrupt_gets_no_continuation(self):
        thread, turn = self.start("kimi")
        native = self.native("kimi", thread)
        plan = RuntimeRecovery.plan_interruption

        def plan_then_finish(recovery, update_id, thread_id, turn_id, client):
            plan(recovery, update_id, thread_id, turn_id, client)
            (self.fixture_root / f"release-{native}").touch()
            self.wait(lambda: thread not in self.sessions.active)
        owner = self.owner()
        self.request(owner)
        with patch.object(RuntimeRecovery, "plan_interruption", plan_then_finish):
            self.assertTrue(self.drive(owner))
        row, interruption = self.recovery(thread)
        self.assertEqual((interruption["state"], row["state"]), ("finished", "skipped"))
        self.replace_coordinator(owner)
        time.sleep(.5)
        self.assertEqual(len(self.prompts(native)), 1)
        self.assertEqual([e["stop_reason"] for e in self.events("prompt-ended")], ["end_turn"])

    def test_natural_failure_racing_the_interrupt_keeps_its_failure_guard(self):
        thread, turn = self.start("antigravity")
        native = self.native("antigravity", thread)
        original = self.sessions.antigravity.request

        def fail_first(method, params=None):
            if method == "turn/interrupt":
                (self.fixture_root / f"fail-{native}").touch()
                self.wait(lambda: self.store.threads.get(thread, history=False)["turn_completion"])
            return original(method, params)
        owner = self.owner()
        self.request(owner)
        with patch.object(self.sessions.antigravity, "request", fail_first):
            self.assertTrue(self.drive(owner))
        row, interruption = self.recovery(thread)
        self.assertEqual((interruption["state"], row["state"]), ("finished", "skipped"))
        with closing(sqlite3.connect(self.database)) as db:
            pause = db.execute("SELECT paused, pause_kind FROM browser_inbox_wake WHERE thread_id=?", (thread,)).fetchone()
        self.assertEqual(tuple(pause), (1, "failure"))
        self.replace_coordinator(owner)
        time.sleep(.5)
        self.assertEqual(len(self.prompts(native)), 1)

    def run_until_failed(self, owner, timeout=15):
        end = time.monotonic() + timeout
        while runtime_update.read(self.directory)["phase"] != "failed" and time.monotonic() < end:
            with owner.mutex:
                self.assertFalse(owner.prepare_handover())
            time.sleep(.02)
        update = runtime_update.read(self.directory)
        self.assertEqual(update["phase"], "failed", update)
        return update

    def test_slow_interrupts_never_drop_a_turn_after_the_signalling_budget(self):
        first, first_turn = self.start("kimi")
        second, second_turn = self.start("kimi")
        original = self.sessions.kimi.request

        def slow(method, params=None):
            result = original(method, params)
            if method == "turn/interrupt":
                time.sleep(.6)  # Kimi closes its process synchronously after the native cancel.
            return result
        owner = self.owner()
        self.request(owner)
        with patch.object(runtime, "UPDATE_INTERRUPTION_SECONDS", .3), \
                patch.object(self.sessions.kimi, "request", slow):
            update = self.run_until_failed(owner)
        stopped, kept = (first, second) if self.recovery(first)[1]["state"] == "confirmed" else (second, first)
        kept_turn = second_turn if kept == second else first_turn
        self.assertEqual(self.recovery(stopped)[1]["state"], "confirmed")
        self.assertEqual(self.recovery(kept)[1]["state"], "withdrawn")
        self.assertIn("ran out of time", self.recovery(kept)[1]["reason"])
        self.assertIn("active unsupported provider: kimi", update["error"])
        self.assertEqual(self.sessions.active.get(kept), kept_turn)  # Left running, not dropped.
        stopped_native = self.native("kimi", stopped)
        self.wait(lambda: len(self.prompts(stopped_native)) == 2)
        self.assertTrue(self.prompts(stopped_native)[1]["continuation"])
        self.assertNotIn(kept, getattr(self.sessions, "runtime_recovery_turns", {}))

    def test_interrupt_that_does_not_stop_the_turn_fails_the_update_visibly(self):
        thread, turn = self.start("muse")
        original = self.sessions.muse.request

        def ignored(method, params=None):
            if method == "turn/interrupt":
                return {}
            return original(method, params)
        owner = self.owner()
        self.request(owner)
        with patch.object(runtime, "UPDATE_CONFIRMATION_SECONDS", .3), \
                patch.object(self.sessions.muse, "request", ignored):
            update = self.run_until_failed(owner)
        row, interruption = self.recovery(thread)
        self.assertEqual((interruption["state"], row["state"]), ("unconfirmed", "skipped"))
        self.assertIn("still running", interruption["reason"])
        self.assertIn("unconfirmed interruption", update["error"])
        self.assertEqual(self.sessions.active.get(thread), turn)
        self.assertNotIn(thread, getattr(self.sessions, "runtime_recovery_turns", {}))
        self.assertFalse(self.sessions.admission.paused)

    def test_approval_before_the_adapter_check_is_preserved_by_atomic_refusal(self):
        thread, turn = self.start("antigravity")
        native = self.native("antigravity", thread)
        original = self.sessions.antigravity.request

        def approval_first(method, params=None):
            if method == "turn/interrupt":
                (self.fixture_root / f"ask-{native}").touch()
                self.wait(lambda: any((v.get("params") or {}).get("threadId") == thread
                                      for v in self.sessions.requests.values()))
            return original(method, params)
        owner = self.owner()
        self.request(owner)
        with patch.object(self.sessions.antigravity, "request", approval_first):
            self.run_until_failed(owner)
        row, interruption = self.recovery(thread)
        self.assertEqual((interruption["state"], interruption["reason"]),
                         ("withdrawn", "An approval or answer requires the user."))
        self.assertEqual(self.sessions.active.get(thread), turn)
        self.assertTrue(any((v.get("params") or {}).get("threadId") == thread for v in self.sessions.requests.values()))

    def test_approval_requested_after_the_adapter_began_stopping_is_never_shown(self):
        thread, turn = self.start("kimi")
        native = self.native("kimi", thread)
        finish = self.sessions.kimi._finish

        def provider_asks_while_stopping(thread_id, status, error=None, **kwargs):
            if status == "interrupted" and thread_id == thread:
                (self.fixture_root / f"ask-{native}").touch()
                self.wait(lambda: [e for e in self.events("client-response")
                                   if str(e["id"]).startswith("approval-")])
            return finish(thread_id, status, error, **kwargs)
        owner = self.owner()
        self.request(owner)
        with patch.object(self.sessions.kimi, "_finish", provider_asks_while_stopping):
            self.assertTrue(self.drive(owner))
        response = [e for e in self.events("client-response") if str(e["id"]).startswith("approval-")][0]
        self.assertIsNone(response["result"])  # Refused to the provider with an error, never presented.
        self.assertFalse(any((v.get("params") or {}).get("threadId") == thread for v in self.sessions.requests.values()))
        self.assertEqual(self.recovery(thread)[1]["state"], "confirmed")
        self.replace_coordinator(owner)
        self.wait(lambda: len(self.prompts(native)) == 2)
        self.assertTrue(self.prompts(native)[1]["continuation"])

    def test_muse_foreground_workflow_waits_instead_of_being_interrupted(self):
        thread, turn = self.start("muse")
        with self.sessions.muse.lock:
            self.sessions.muse.turns[thread]["items"].append(
                {"id": "workflow", "type": "mcpToolCall", "nativeKind": "workflow", "status": "inProgress"})
        owner = self.owner()
        self.request(owner)
        for _ in range(10):
            with owner.mutex:
                self.assertFalse(owner.prepare_handover())
        update = runtime_update.read(self.directory)
        self.assertIn("Muse background workflow", update["notice"])
        self.assertEqual(self.sessions.active.get(thread), turn)
        self.assertIsNone(self.recovery(thread)[1])
        self.assertFalse(self.sessions.admission.paused)

    def test_new_work_after_a_completed_round_fails_and_continues_paused_turns(self):
        paused, _ = self.start("kimi")
        owner = self.owner()
        self.request(owner)
        self.assertTrue(self.drive(owner))
        # Admission reopens (for example, maintenance resume) and new provider
        # work starts before the handover; no second, unrecorded round runs.
        self.sessions.admission.resume()
        later, later_turn = self.start("antigravity")
        update = self.run_until_failed(owner)
        self.assertIn("New Kimi, Muse or Antigravity work started", update["error"])
        self.assertEqual(self.sessions.active.get(later), later_turn)
        self.assertIsNone(self.recovery(later)[1])
        native = self.native("kimi", paused)
        self.wait(lambda: len(self.prompts(native)) == 2)
        self.assertTrue(self.prompts(native)[1]["continuation"])

    def test_crash_after_stopping_restarts_previous_coordinator_and_continues(self):
        thread, turn = self.start("muse")
        native = self.native("muse", thread)
        owner = self.owner()
        self.request(owner)
        self.assertTrue(self.drive(owner))
        runtime_update.write(self.directory, owner.update_requested, phase="stopping", uninterrupted=True,
                             previous=str(owner.payload))
        self.sessions.close()  # An exit other than the handover status.
        self.store = CoordinationStore(self.database)
        self.sessions = self.make_sessions()
        restarted = self.owner(self.sessions)
        restarted.abandon_unstarted_update(runtime_update.read(self.directory))
        with restarted.mutex:
            restarted.resume_work()
        self.wait(lambda: not restarted.recovery_running)
        self.assertEqual(runtime_update.read(self.directory)["phase"], "failed")
        self.wait(lambda: len(self.prompts(native)) == 2)
        self.assertTrue(self.prompts(native)[1]["continuation"])

    def test_antigravity_user_stop_carried_out_by_the_update_keeps_user_origin(self):
        thread, turn = self.start("antigravity")
        plan = RuntimeRecovery.plan_interruption

        def plan_then_user_stop(recovery, update_id, thread_id, turn_id, client):
            plan(recovery, update_id, thread_id, turn_id, client)
            with self.assertRaises(BrowserBusyError):
                self.sessions.interrupt(thread_id)
        owner = self.owner()
        self.request(owner)
        with patch.object(RuntimeRecovery, "plan_interruption", plan_then_user_stop):
            self.assertTrue(self.drive(owner))
        self.assertEqual(self.recovery(thread)[1]["state"], "withdrawn")
        # Admission is closed for the handover; read the saved conversation history.
        from agent_coord.history_persistence import read_history
        self.sessions._await_persistence(thread)
        self.assertEqual(read_history(self.store, thread)["turns"][-1]["termination"]["origin"], "user")

    def test_user_stop_before_interrupt_is_respected(self):
        thread, turn = self.start("muse")
        native = self.native("muse", thread)
        plan = RuntimeRecovery.plan_interruption

        def plan_then_user_stop(recovery, update_id, thread_id, turn_id, client):
            plan(recovery, update_id, thread_id, turn_id, client)
            # Admission is closed: the user's Stop is recorded, but its native
            # request is refused, exactly as the HTTP route reports it.
            with self.assertRaises(BrowserBusyError):
                self.sessions.interrupt(thread_id)
        owner = self.owner()
        self.request(owner)
        with patch.object(RuntimeRecovery, "plan_interruption", plan_then_user_stop):
            self.assertTrue(self.drive(owner))
        row, interruption = self.recovery(thread)
        self.assertEqual((interruption["state"], row["state"]), ("withdrawn", "skipped"))
        self.assertEqual(interruption["reason"], "The user stopped this turn.")
        self.assertNotIn(thread, self.sessions.active)  # The update carried out the Stop.
        with closing(sqlite3.connect(self.database)) as db:
            pause = db.execute("SELECT paused, pause_kind FROM browser_inbox_wake WHERE thread_id=?", (thread,)).fetchone()
        self.assertEqual(tuple(pause), (1, "stop"))
        self.replace_coordinator(owner)
        time.sleep(.5)
        self.assertEqual(len(self.prompts(native)), 1)
        self.assertTrue(self.store.threads.stop_requested(thread, turn))

    def test_rollback_restores_snapshot_and_continues_once_on_previous_coordinator(self):
        thread, turn = self.start("kimi")
        native = self.native("kimi", thread)
        owner = self.owner()
        self.request(owner)
        self.assertTrue(self.drive(owner))
        self.replace_coordinator(owner, rollback=True)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='trial_migration'").fetchone())
        self.assertEqual(runtime_update.read(self.directory)["phase"], "rolled-back")
        self.wait(lambda: len(self.prompts(native)) == 2)
        self.assertTrue(self.prompts(native)[1]["continuation"])
        self.wait(lambda: self.recovery(thread)[0]["state"] == "continued")

    def test_candidate_validation_failure_interrupts_nothing(self):
        thread, turn = self.start("kimi")
        owner = self.owner(validation=lambda candidate, capacity: "probe failed")
        self.request(owner)
        end = time.monotonic() + 10
        while runtime_update.read(self.directory)["phase"] != "failed" and time.monotonic() < end:
            with owner.mutex:
                owner.prepare_handover()
            time.sleep(.02)
        update = runtime_update.read(self.directory)
        self.assertEqual((update["phase"], update["error"]), ("failed", "Candidate validation failed: probe failed"))
        self.assertEqual(self.sessions.active.get(thread), turn)
        self.assertFalse(self.events("prompt-ended"))
        self.assertFalse(self.sessions.admission.paused)

    def test_restart_mid_interruption_is_uncertain_and_never_continued(self):
        thread, turn = self.start("kimi")
        native = self.native("kimi", thread)
        owner = self.owner()
        self.request(owner)
        RuntimeRecovery(self.sessions).plan_interruption("explicit-update", thread, turn, "kimi")
        runtime_update.write(self.directory, owner.update_requested, phase="interrupting", interrupted_unsupported=True)
        self.sessions.close()
        self.store = CoordinationStore(self.database)
        self.sessions = self.make_sessions()
        restarted = self.owner(self.sessions)
        restarted.abandon_unstarted_update(runtime_update.read(self.directory))
        with restarted.mutex:
            restarted.resume_work()
        self.wait(lambda: not restarted.recovery_running)
        update = runtime_update.read(self.directory)
        self.assertEqual(update["phase"], "failed")
        row, interruption = self.recovery(thread)
        self.assertEqual((interruption["state"], row["state"]), ("uncertain", "uncertain"))
        time.sleep(.3)
        self.assertEqual(len(self.prompts(native)), 1)

    def test_stop_work_and_quit_cancels_continuation(self):
        thread, turn = self.start("antigravity")
        native = self.native("antigravity", thread)
        owner = self.owner()
        self.request(owner)
        self.assertTrue(self.drive(owner))
        runtime_update.write(self.directory, owner.update_requested, cancelled=True)
        self.replace_coordinator(owner)
        time.sleep(.5)
        self.assertEqual(len(self.prompts(native)), 1)
        self.assertEqual(self.recovery(thread)[0]["state"], "prepared")

    def test_queued_user_message_takes_precedence_over_continuation(self):
        thread, turn = self.start("kimi")
        native = self.native("kimi", thread)
        owner = self.owner()
        self.request(owner)
        self.sessions.queue.enqueue(thread, {"message": "user follow-up"})
        self.assertTrue(self.drive(owner))
        self.replace_coordinator(owner)
        self.wait(lambda: len(self.prompts(native)) == 2)
        time.sleep(.5)
        prompts = self.prompts(native)
        self.assertEqual(len(prompts), 2)
        self.assertFalse(prompts[1]["continuation"])
        self.assertIn("user follow-up", prompts[1]["text"])
        self.assertEqual(self.recovery(thread)[0]["reason"], "Queued user input takes precedence.")

    def test_failed_continuation_stays_visible_and_is_not_reported_as_running_work(self):
        thread, turn = self.start("muse")
        native = self.native("muse", thread)
        (self.fixture_root / "fail-continuations").touch()
        owner = self.owner()
        self.request(owner)
        self.assertTrue(self.drive(owner))
        replacement = self.replace_coordinator(owner)
        self.wait(lambda: len(self.prompts(native)) == 2)
        self.wait(lambda: (self.recovery(thread)[0] or {}).get("state") == "continued")
        conversation = self.wait(lambda: next((c for c in replacement.update_status()["recovery"]["conversations"]
                                               if c["thread_id"] == thread and c.get("continuation", {}).get("status") == "failed"), None))
        self.assertEqual(conversation["interruption"]["state"], "confirmed")
        with closing(sqlite3.connect(self.database)) as db:
            pause = db.execute("SELECT paused, pause_kind FROM browser_inbox_wake WHERE thread_id=?", (thread,)).fetchone()
        self.assertEqual(tuple(pause), (1, "failure"))

    def test_muse_background_workflow_waits_with_a_named_reason(self):
        thread, turn = self.start("muse", "quick answer")
        self.sessions.muse.background_turns[thread] = {"workflow-turn": {"id": "workflow-turn", "items": [
            {"id": "workflow", "nativeKind": "workflow", "status": "inProgress"}]}}
        owner = self.owner()
        self.request(owner)
        for _ in range(5):
            with owner.mutex:
                self.assertFalse(owner.prepare_handover())
        update = runtime_update.read(self.directory)
        self.assertIn("Muse background workflow", update["notice"])
        self.assertFalse(self.sessions.admission.paused)

    def test_update_status_is_read_only_and_reports_interruptions(self):
        thread, turn = self.start("kimi")
        owner = self.owner()
        self.request(owner)
        self.assertTrue(self.drive(owner))
        before = (self.directory / "update.json").read_bytes()
        result = owner.dispatch({"action": "update-status"})
        self.assertEqual((self.directory / "update.json").read_bytes(), before)
        self.assertEqual(result["interrupted_provider_continuation"], 1)
        conversation = next(c for c in result["update"]["recovery"]["conversations"] if c["thread_id"] == thread)
        self.assertEqual((conversation["state"], conversation["interruption"]["state"]), ("prepared", "confirmed"))

    def test_every_adapter_reports_a_finished_turn_with_the_recognized_phrase(self):
        # interrupt_turn classifies a natural completion by this shared phrase.
        for client in PROVIDERS:
            thread, turn = self.start(client, "quick answer")
            with self.assertRaisesRegex(Exception, "changed or finished"):
                self.sessions._rpc(thread).request("turn/interrupt", {"threadId": thread, "turnId": turn,
                                                                     "runtimeUpdate": True})

    def test_every_adapter_refuses_an_update_interrupt_with_a_pending_request(self):
        # interrupt_turn recognizes this shared refusal phrase.
        for client in ("kimi", "antigravity"):
            thread, turn = self.start(client, "ASK then HOLD")
            self.wait(lambda: any((v.get("params") or {}).get("threadId") == thread
                                  for v in self.sessions.requests.values()))
            with self.assertRaisesRegex(Exception, "waiting for an approval or answer"):
                self.sessions._rpc(thread).request("turn/interrupt", {"threadId": thread, "turnId": turn,
                                                                     "runtimeUpdate": True})
            self.assertEqual(self.sessions.active.get(thread), turn)
        # The fixture's MSP mode does not issue approvals; register one directly.
        thread, turn = self.start("muse")
        with self.sessions.muse.lock:
            self.sessions.muse.requests["muse:fixture"] = {"thread": thread}
        with self.assertRaisesRegex(Exception, "waiting for an approval or answer"):
            self.sessions.muse.request("turn/interrupt", {"threadId": thread, "turnId": turn, "runtimeUpdate": True})


import test_desktop_runtime as desktop_tests  # noqa: E402  (module import: its tests are not re-collected)


class UnsupportedProviderProcessTests(unittest.TestCase):
    """The actual runtime, provider transport and supervisor with a Kimi fixture executable."""
    setUp = desktop_tests.RuntimeTests.setUp
    tearDown = desktop_tests.RuntimeTests.tearDown
    wait = desktop_tests.RuntimeTests.wait
    launch = desktop_tests.RuntimeTests.launch
    log = desktop_tests.RuntimeTests.log
    api = desktop_tests.RuntimeTests.api
    calls = desktop_tests.RuntimeTests.calls
    wait_update = desktop_tests.RuntimeTests.wait_update

    def fixture_events(self):
        path = self.root / "fixture/provider.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_installed_update_pauses_kimi_and_continues_it_once_in_the_committed_backend(self):
        (self.root / "fixture").mkdir()
        kimi = self.root / "bin/kimi"
        kimi.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FIXTURE}" kimi "$@"\n')
        kimi.chmod(0o755)
        self.env.update(AGENT_COORD_KIMI_BIN=str(kimi), RIBBON_UPDATE_FIXTURE_ROOT=str(self.root / "fixture"),
                        KIMI_CODE_HOME=str(self.root / "kimi-home"))
        source = self.resources / "backend/agent_coord/desktop_runtime.py"
        source.write_text(source.read_text() + "\nUPDATE_UNSUPPORTED_GRACE_SECONDS = 1\n")
        first = self.launch()
        thread = self.api(first["url"], "api/browser/sessions", {"cwd": str(self.root), "client": "kimi"})["session"]["thread_id"]
        self.api(first["url"], f"api/browser/sessions/{thread}/messages", {"message": "HOLD the assignment"})
        original = self.wait(lambda: next((e for e in self.fixture_events() if e["kind"] == "prompt"), None), timeout=20)
        candidate = self.root / "Candidate/Contents/Resources"
        import shutil
        shutil.copytree(self.resources, candidate)
        metadata = json.loads((candidate / "backend.json").read_text())
        metadata["runtime_version"] = "2+2"
        (candidate / "backend.json").write_text(json.dumps(metadata))
        staged = self.launch(candidate, confirm_updates=True)
        self.assertTrue(staged["pending_confirmation"])
        record = runtime.discover(self.database)
        runtime.control(record, "activate-update", request_id="kimi-explicit-update", payload=staged["pending"])
        current = self.wait_update("committed")  # Builds 62-70 remain "accepted" here indefinitely.
        self.assertEqual(current.get("interrupted_provider_continuation"), 1)
        self.assertEqual(current["payload"], staged["pending"])
        self.assertNotEqual(current["pid"], first["pid"])
        self.assertTrue(current["update"]["uninterrupted"])
        self.assertTrue(current["update"]["interrupted_unsupported"])
        continuation = self.wait(lambda: next((e for e in self.fixture_events() if e["kind"] == "prompt"
                                               and e["continuation"]), None), timeout=20)
        self.assertEqual(continuation["session"], original["session"])
        self.assertNotEqual(continuation["pid"], original["pid"])
        self.assertIn(original["session"], [e.get("session") for e in self.fixture_events()
                                            if e["kind"] == "request" and e["method"] == "session/resume"])
        self.wait(lambda: runtime.control(runtime.discover(self.database), "status")["update"]["recovery"]["counts"]
                  == {"continued": 1})
        # The supported read-only command reports the same outcome.
        result = subprocess.run([sys.executable, "-B", str(candidate / "backend.py"), "--db", str(self.database),
                                 "--runtime-command", "update-status"], env=self.env, text=True,
                                capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        conversation = json.loads(result.stdout)["update"]["recovery"]["conversations"][0]
        self.assertEqual((conversation["thread_id"], conversation["state"], conversation["interruption"]["state"]),
                         (thread, "continued", "confirmed"))
        time.sleep(1)
        self.assertEqual(len([e for e in self.fixture_events() if e["kind"] == "prompt"]), 2)
        self.assertNotIn("ResourceWarning", self.log())


if __name__ == "__main__":
    unittest.main()
