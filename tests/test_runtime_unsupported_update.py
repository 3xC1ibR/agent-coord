"""Retained provider adoption and compatibility with saved recovery records.

Regression for the November 23, 2026 report: an installed build stayed
"accepted" indefinitely while coordinator-owned provider turns ran. Unbrokered
work must now finish naturally; retained work survives without cancellation or
continuation. Unit cases use real adapters against independent wire fixtures
with transport ownership substituted. The process case uses the actual runtime,
transport and supervisor. Prior-format durable rows are seeded independently of
the removed interruption controller to protect recovery compatibility.
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
from agent_coord.codex_app_server import BrowserSessions  # noqa: E402
from agent_coord.kimi import KimiConnection, KimiRPC  # noqa: E402
from agent_coord.muse_code import MuseConnection, MuseRPC  # noqa: E402
from agent_coord.runtime_recovery import RuntimeRecovery  # noqa: E402
from agent_coord.store import CoordinationStore  # noqa: E402

FIXTURE = ROOT / "tests/fixtures/update_interruption_provider.py"
PROVIDERS = ("kimi", "antigravity", "muse")
_readiness = runtime.uninterrupted_readiness


class _TransportView:
    """The real sessions as a coordinator with a healthy provider transport sees them."""
    def __init__(self, sessions, retained=False):
        self.__dict__["_sessions"] = sessions
        self.projection = SimpleNamespace(error=None, owns_provider=lambda identity, client:
            retained and sessions.loaded_clients.get(identity) == client)

    def __getattr__(self, name):
        return getattr(self._sessions, name)


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
        self.retained = False
        patcher = patch.object(runtime, "uninterrupted_readiness", lambda sessions:
            _readiness(sessions) if hasattr(sessions, "projection") else
            _readiness(_TransportView(sessions, self.retained)))
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
        owner.server = SimpleNamespace(browser_sessions=sessions or self.sessions, server_port=0)
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
        update = runtime_update.write(self.directory, owner.update_requested or runtime_update.read(self.directory), phase="stopping", uninterrupted=True)
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

    def release(self, client, thread):
        (self.fixture_root / f"release-{self.native(client, thread)}").touch()
        self.wait(lambda: thread not in self.sessions.active)
        self.sessions._await_persistence(thread)

    def assert_no_update_interruption(self, work):
        self.assertFalse([e for e in self.events("request") if e["method"] in {"session/cancel", "turn/interrupt"}])
        self.assertFalse([e for e in self.events("prompt") if e["continuation"]])
        for client, (thread, turn) in work.items():
            self.assertEqual(self.sessions.active.get(thread), turn, client)
            self.assertEqual(self.recovery(thread), (None, None), client)
            self.assert_not_user_stopped(thread, turn)

    def replace_after_restart(self, owner):
        previous = owner.server.browser_sessions
        previous.close()
        self.store = CoordinationStore(self.database)
        self.sessions = self.make_sessions()
        self.adopt_queue(previous, self.sessions)
        return self.serve(self.sessions)

    def seed_legacy_update(self, client, *, interruption="confirmed", phase="committed"):
        """Write the prior positional schema, without the deleted controller."""
        thread, turn = self.start(client)
        owner = self.owner()
        identity = self.request(owner, client + "-legacy-update")
        recovery = RuntimeRecovery(self.sessions)  # Creates the backward-compatible tables only.
        record = self.store.threads.get(thread, history=False)
        settings = recovery.settings(thread)
        if interruption == "confirmed":
            self.sessions.runtime_recovery_turns = {thread: turn}
            self.sessions._rpc(thread).request("turn/interrupt", {
                "threadId": thread, "turnId": turn, "runtimeUpdate": True})
            self.wait(lambda: thread not in self.sessions.active)
            self.sessions._await_persistence(thread)
            self.assertEqual(self.store.threads.get(thread, history=False)["turn_completion"]["status"], "interrupted")
        state = "prepared" if interruption == "confirmed" else "interrupting"
        now = self.store.clock()
        with self.store._connection() as db:
            db.execute("INSERT INTO runtime_update_interruptions VALUES (?,?,?,?,?,?,?,?)",
                (identity, thread, turn, client, interruption, None, now,
                 now if interruption == "confirmed" else None))
            db.execute("INSERT INTO runtime_update_recoveries VALUES (?,?,?,?,?,?,?,?,?)",
                (identity, thread, turn, record["turn_key"], settings, state, None, None, now))
        owner.update_requested = runtime_update.write(self.directory, owner.update_requested,
            phase=phase, interrupted_unsupported=True)
        return owner, thread, turn, self.native(client, thread)

    def test_every_adapter_reports_a_finished_turn_with_the_recognized_phrase(self):
        for client in PROVIDERS:
            thread, turn = self.start(client, "quick answer")
            with self.assertRaisesRegex(Exception, "changed or finished"):
                self.sessions._rpc(thread).request("turn/interrupt", {"threadId": thread, "turnId": turn,
                                                                     "runtimeUpdate": True})

    def test_every_adapter_refuses_an_update_interrupt_with_a_pending_request(self):
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

    def test_explicit_update_waits_for_each_unbrokered_provider_without_interrupting(self):
        work = {client: self.start(client) for client in PROVIDERS}
        processes = {client: self.sessions._rpc(thread).connections[thread].process
                     for client, (thread, _) in work.items()}
        owner = self.owner()
        self.request(owner)
        for _ in range(3):
            with owner.mutex:
                self.assertFalse(owner.prepare_handover())
        self.assert_no_update_interruption(work)
        self.assertFalse(self.sessions.admission.paused)
        update = runtime_update.read(self.directory)
        self.assertEqual(update["phase"], "accepted")
        self.assertIn("Running work remains available", update["notice"])
        self.assertEqual({e["client"] for e in update["unsupported"]}, set(PROVIDERS))
        for client, (thread, _) in work.items():
            self.assertIsNone(processes[client].poll())
            self.release(client, thread)
        self.assertTrue(self.drive(owner))
        self.assertNotIn("interrupted_unsupported", runtime_update.read(self.directory))
        self.assertFalse([e for e in self.events("prompt") if e["continuation"]])

    def test_explicit_update_transfers_each_retained_provider_without_interrupting(self):
        self.retained = True
        work = {client: self.start(client) for client in PROVIDERS}
        processes = {client: self.sessions._rpc(thread).connections[thread].process
                     for client, (thread, _) in work.items()}
        owner = self.owner()
        self.request(owner)
        self.assertTrue(self.drive(owner))
        self.assert_no_update_interruption(work)
        self.assertTrue(self.sessions.admission.paused)
        self.sessions.admission.resume()
        for client, (thread, _) in work.items():
            self.assertIsNone(processes[client].poll())
            self.release(client, thread)
        self.assertEqual(len(self.events("prompt")), 3)

    def test_passive_update_keeps_unbrokered_turns_and_approvals_answerable(self):
        work = {client: self.start(client, "ASK then HOLD" if client == "kimi" else "HOLD the assignment")
                for client in PROVIDERS}
        self.wait(lambda: bool(self.sessions.requests))
        owner = self.owner()
        for _ in range(3):
            with owner.mutex:
                self.assertFalse(owner.prepare_handover())
        self.assertFalse(self.sessions.admission.paused)
        self.assert_no_update_interruption(work)
        self.assertIsNone(runtime_update.read(self.directory))

    def test_unbrokered_approvals_are_never_withdrawn_and_remain_answerable(self):
        for client in ("kimi", "antigravity"):
            thread, turn = self.start(client, "ASK then HOLD")
            self.wait(lambda: any((v.get("params") or {}).get("threadId") == thread for v in self.sessions.requests.values()))
            owner = self.owner()
            self.request(owner, client + "-approval")
            with owner.mutex:
                self.assertFalse(owner.prepare_handover())
            self.assertFalse(self.sessions.admission.paused)
            key = next(k for k,v in self.sessions.requests.items() if (v.get("params") or {}).get("threadId") == thread)
            self.assertEqual(runtime_update.read(self.directory)["unsupported"][0]["reason"], "approval")
            self.sessions.answer(thread, key, {"decision": "accept"})
            self.release(client, thread)
            self.assertTrue(self.drive(owner))
            runtime_update.write(self.directory, owner.update_requested, phase="committed", uninterrupted=True)
            self.sessions.admission.resume()
        self.assertFalse([e for e in self.events("request") if e["method"] == "session/cancel"])

    def test_retained_approval_transfers_and_user_answer_is_not_lost(self):
        self.retained = True
        thread, turn = self.start("kimi", "ASK then HOLD")
        self.wait(lambda: bool(self.sessions.requests))
        key = next(iter(self.sessions.requests))
        owner = self.owner()
        self.request(owner)
        self.assertTrue(self.drive(owner))
        self.assertIn(key, self.sessions.requests)
        self.sessions.admission.resume()
        self.sessions.answer(thread, key, {"decision": "accept"})
        self.release("kimi", thread)
        self.assertFalse(self.sessions.requests)
        self.assertEqual(len(self.events("client-response")), 1)
        self.assertIsNone(self.recovery(thread)[0])

    def test_unbrokered_muse_workflows_wait_and_retained_workflows_transfer(self):
        thread, turn = self.start("muse")
        with self.sessions.muse.lock:
            self.sessions.muse.turns[thread]["items"].append(
                {"id": "workflow", "type": "mcpToolCall", "nativeKind": "workflow", "status": "inProgress"})
        self.sessions.muse.background_turns[thread] = {"background": {"id": "background", "items": [
            {"id": "background", "nativeKind": "workflow", "status": "inProgress"}]}}
        owner = self.owner()
        self.request(owner)
        with owner.mutex:
            self.assertFalse(owner.prepare_handover())
        self.assertFalse(self.sessions.admission.paused)
        self.assertTrue(all(e["reason"] == "background" for e in runtime_update.read(self.directory)["unsupported"]))
        self.retained = True
        self.assertTrue(self.drive(owner))
        self.assertEqual(self.sessions.active.get(thread), turn)
        self.assertFalse([e for e in self.events("request") if e["method"] == "turn/interrupt"])

    def test_user_stop_retains_authority_while_an_update_waits(self):
        for client in PROVIDERS:
            thread, turn = self.start(client)
            owner = self.owner()
            self.request(owner, client + "-stop")
            with owner.mutex:
                self.assertFalse(owner.prepare_handover())
            self.sessions.interrupt(thread)
            self.wait(lambda: thread not in self.sessions.active)
            self.assertTrue(self.store.threads.stop_requested(thread, turn))
            self.assertIsNone(self.recovery(thread)[0])
            self.assertTrue(self.drive(owner))
            runtime_update.write(self.directory, owner.update_requested, phase="committed", uninterrupted=True)
            self.sessions.admission.resume()
        self.assertFalse([e for e in self.events("prompt") if e["continuation"]])

    def test_legacy_confirmed_interruption_continues_once_after_commit_and_restart(self):
        for client in PROVIDERS:
            owner, thread, turn, native = self.seed_legacy_update(client)
            replacement = self.replace_coordinator(owner)
            self.wait(lambda: (self.recovery(thread)[0] or {}).get("state") == "continued")
            self.assertEqual(len(self.prompts(native)), 2)
            self.assertTrue(self.prompts(native)[1]["continuation"])
            self.assert_not_user_stopped(thread, turn)
            self.replace_after_restart(replacement)
            self.assertEqual(len(self.prompts(native)), 2)
            # The next simulated old update belongs to the replacement instance.
            runtime_update.write(self.directory, runtime_update.read(self.directory), phase="committed")

    def test_legacy_rollback_restores_snapshot_and_continues_once(self):
        owner, thread, turn, native = self.seed_legacy_update("kimi")
        self.replace_coordinator(owner, rollback=True)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='trial_migration'").fetchone())
        self.assertEqual(runtime_update.read(self.directory)["phase"], "rolled-back")
        self.wait(lambda: (self.recovery(thread)[0] or {}).get("state") == "continued")
        self.assertEqual(len(self.prompts(native)), 2)

    def test_legacy_confirmed_interruption_continues_after_failed_pretransfer_restart(self):
        owner, thread, turn, native = self.seed_legacy_update("muse", phase="stopping")
        runtime_update.write(self.directory, owner.update_requested, uninterrupted=True)
        self.sessions.close()
        self.store = CoordinationStore(self.database)
        self.sessions = self.make_sessions()
        restarted = self.owner()
        restarted.abandon_unstarted_update(runtime_update.read(self.directory))
        with restarted.mutex:
            restarted.resume_work()
        self.wait(lambda: not restarted.recovery_running)
        self.assertEqual(runtime_update.read(self.directory)["phase"], "failed")
        self.wait(lambda: (self.recovery(thread)[0] or {}).get("state") == "continued")
        self.assertEqual(len(self.prompts(native)), 2)

    def test_legacy_mid_interruption_restart_preserves_uncertainty(self):
        owner, thread, turn, native = self.seed_legacy_update("kimi", interruption="interrupting", phase="interrupting")
        self.sessions.close()
        self.store = CoordinationStore(self.database)
        self.sessions = self.make_sessions()
        restarted = self.owner()
        restarted.abandon_unstarted_update(runtime_update.read(self.directory))
        with restarted.mutex:
            restarted.resume_work()
        self.wait(lambda: not restarted.recovery_running)
        row, interruption = self.recovery(thread)
        self.assertEqual((row["state"], interruption["state"]), ("uncertain", "uncertain"))
        self.assertEqual(len(self.prompts(native)), 1)

    def test_legacy_uncertain_submission_is_not_replayed(self):
        owner, thread, turn, native = self.seed_legacy_update("kimi")
        with self.store._connection() as db:
            db.execute("UPDATE runtime_update_recoveries SET state='submitting' WHERE thread_id=?", (thread,))
        self.replace_coordinator(owner)
        self.wait(lambda: self.recovery(thread)[0]["state"] == "uncertain")
        self.assertEqual(len(self.prompts(native)), 1)

    def test_legacy_stop_work_and_quit_cancels_continuation_durably(self):
        owner, thread, turn, native = self.seed_legacy_update("antigravity")
        owner.dispatch({"action": "stop"})
        self.assertTrue(runtime_update.read(self.directory)["cancelled"])
        replacement = self.replace_coordinator(owner)
        self.assertFalse(replacement.start_recovery())
        self.assertEqual(len(self.prompts(native)), 1)
        self.assertEqual(self.recovery(thread)[0]["state"], "prepared")

    def test_legacy_user_stop_prevents_continuation(self):
        for client in PROVIDERS:
            owner, thread, turn, native = self.seed_legacy_update(client)
            self.store.threads.request_stop(thread, turn)
            self.replace_coordinator(owner)
            self.wait(lambda: self.recovery(thread)[0]["state"] == "skipped")
            self.assertEqual(self.recovery(thread)[0]["reason"], "The user stopped this turn.")
            self.assertEqual(len(self.prompts(native)), 1)

    def test_legacy_recovery_never_answers_a_pending_approval(self):
        owner, thread, turn, native = self.seed_legacy_update("kimi")
        self.sessions.requests['legacy-approval'] = {'params': {'threadId': thread}}
        with owner.mutex:
            owner.resume_work()
        self.wait(lambda: not owner.recovery_running)
        self.assertEqual(self.recovery(thread)[0]["state"], "skipped")
        self.assertEqual(self.recovery(thread)[0]["reason"], "An approval or answer requires the user.")
        self.assertIn('legacy-approval', self.sessions.requests)
        self.assertEqual(len(self.prompts(native)), 1)
        self.assertFalse(self.events('client-response'))

    def test_legacy_queued_user_input_precedes_continuation(self):
        owner, thread, turn, native = self.seed_legacy_update("kimi")
        self.sessions.queue.enqueue(thread, {"message": "user follow-up"})
        self.replace_coordinator(owner)
        self.wait(lambda: len(self.prompts(native)) == 2)
        self.assertFalse(self.prompts(native)[1]["continuation"])
        self.assertIn("user follow-up", self.prompts(native)[1]["text"])
        self.assertEqual(self.recovery(thread)[0]["reason"], "Queued user input takes precedence.")

    def test_legacy_failed_continuation_remains_visible(self):
        owner, thread, turn, native = self.seed_legacy_update("muse")
        (self.fixture_root / "fail-continuations").touch()
        replacement = self.replace_coordinator(owner)
        self.wait(lambda: (self.recovery(thread)[0] or {}).get("state") == "continued")
        self.wait(lambda: next((c for c in replacement.update_status()["recovery"]["conversations"]
                  if c["thread_id"] == thread and c.get("continuation", {}).get("status") == "failed"), None))
        with closing(sqlite3.connect(self.database)) as db:
            pause = db.execute("SELECT paused,pause_kind FROM browser_inbox_wake WHERE thread_id=?", (thread,)).fetchone()
        self.assertEqual(tuple(pause), (1, "failure"))

    def test_update_status_reports_legacy_rows_without_writing(self):
        owner, thread, turn, native = self.seed_legacy_update("kimi")
        with closing(sqlite3.connect(self.database)) as db:
            before = db.execute("PRAGMA data_version").fetchone()[0]
            result = owner.dispatch({"action": "update-status"})
            self.assertEqual(db.execute("PRAGMA data_version").fetchone()[0], before)
        self.assertNotIn("interrupted_provider_continuation", result)
        self.assertEqual(result["unbrokered_provider_wait"], 1)
        conversation = result["update"]["recovery"]["conversations"][0]
        self.assertEqual(conversation["interruption"]["state"], "confirmed")
        self.assertEqual(conversation["state"], "prepared")


import test_desktop_runtime as desktop_tests  # noqa: E402


class UnsupportedProviderProcessTests(unittest.TestCase):
    """Actual coordinator/transport/supervisor adoption for all three adapters."""
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

    def test_installed_update_adopts_all_three_providers_without_interrupting(self):
        (self.root / "fixture").mkdir()
        for client in PROVIDERS:
            executable = self.root / "bin" / ("agy_acp_server.par" if client == "antigravity" else client)
            executable.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FIXTURE}" {client} "$@"\n')
            executable.chmod(0o755)
            self.env['AGENT_COORD_' + client.upper() + '_BIN'] = str(executable)
            if client == 'antigravity':
                helper = executable.with_name('localharness_external')
                helper.write_text('#!/bin/sh\nexit 1\n')
                helper.chmod(0o755)
        self.env.update(RIBBON_UPDATE_FIXTURE_ROOT=str(self.root / "fixture"),
                        KIMI_CODE_HOME=str(self.root / "kimi-home"))
        first = self.launch()
        work = {}
        for client in PROVIDERS:
            thread = self.api(first["url"], "api/browser/sessions", {
                "cwd": str(self.root), "client": client})["session"]["thread_id"]
            message = "ASK then HOLD" if client == "kimi" else "HOLD the assignment"
            self.api(first["url"], f"api/browser/sessions/{thread}/messages", {"message": message})
            original = self.wait(lambda: next((e for e in self.fixture_events()
                if e["kind"] == "prompt" and e["mode"] == client), None), timeout=20)
            work[client] = (thread, original)
        snapshots = {client: self.api(first['url'], f'api/browser/sessions/{thread}')
                     for client, (thread, _) in work.items()}
        original_transport = runtime.control(runtime.discover(self.database), 'status')['transport']
        candidate = self.root / "Candidate/Contents/Resources"
        import shutil
        shutil.copytree(self.resources, candidate)
        metadata = json.loads((candidate / "backend.json").read_text())
        metadata["runtime_version"] = "2+2"
        (candidate / "backend.json").write_text(json.dumps(metadata))
        staged = self.launch(candidate, confirm_updates=True)
        self.assertTrue(staged["pending_confirmation"])
        runtime.control(runtime.discover(self.database), "activate-update",
                        request_id="retained-provider-update", payload=staged["pending"])
        current = self.wait_update("committed")
        self.assertNotIn("interrupted_provider_continuation", current)
        self.assertEqual(current["unbrokered_provider_wait"], 1)
        self.assertEqual(current["payload"], staged["pending"])
        self.assertNotEqual(current["pid"], first["pid"])
        self.assertTrue(current["update"]["uninterrupted"])
        self.assertFalse(current["update"].get("interrupted_unsupported"))
        self.assertEqual(current['transport']['pid'], original_transport['pid'])
        identities = {thread for thread, _ in work.values()}
        channels = lambda status: {entry['thread_id']: (entry['channel'], entry['pid'], entry['pgid'])
                                   for entry in status['providers'] if entry.get('thread_id') in identities}
        self.assertEqual(channels(current['transport']), channels(original_transport))
        self.assertEqual(set(channels(current['transport'])), identities)
        self.assertFalse([e for e in self.fixture_events() if e["kind"] == "request"
                          and e["method"] in {"session/cancel", "turn/interrupt"}])
        self.assertFalse([e for e in self.fixture_events() if e["kind"] == "prompt" and e["continuation"]])
        for client, (thread, original) in work.items():
            os.kill(original["pid"], 0)
            detail = self.api(current['url'], f'api/browser/sessions/{thread}')
            self.assertTrue(detail['running'])
            self.assertEqual(detail['activeTurn'], snapshots[client]['activeTurn'])
            self.assertEqual(len([e for e in self.fixture_events()
                                  if e["kind"] == "prompt" and e["mode"] == client]), 1)
        asking, original = work['kimi']
        snapshot = self.api(current['url'], f'api/browser/sessions/{asking}')
        self.assertTrue(snapshot['requests'])
        self.api(current['url'], f"api/browser/sessions/{asking}/requests/{snapshot['requests'][0]['key']}",
                 {'decision': 'accept'})
        # Stop remains authoritative after adoption; only the user issues cancel.
        stopping, stopped = work['antigravity']
        self.api(current['url'], f'api/browser/sessions/{stopping}/interrupt', {})
        self.wait(lambda: any(e['kind'] == 'prompt-ended' and e['session'] == stopped['session']
                             and e['stop_reason'] == 'cancelled' for e in self.fixture_events()))
        for client in ('kimi', 'muse'):
            thread, original = work[client]
            (self.root / 'fixture' / ('release-' + original['session'])).touch()
            self.wait(lambda: any(e['kind'] == 'prompt-ended' and e['session'] == original['session']
                                 and e['stop_reason'] in {'end_turn', 'completed'} for e in self.fixture_events()))
        for thread, _ in work.values():
            self.wait(lambda: not self.api(current['url'], f'api/browser/sessions/{thread}')['running'])
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM turn_stop_requests WHERE thread_id=?', (stopping,)).fetchone()[0], 1)
        self.assertEqual(len([e for e in self.fixture_events() if e['kind'] == 'prompt']), 3)
        self.assertNotIn('ResourceWarning', self.log())


if __name__ == "__main__":
    unittest.main()
