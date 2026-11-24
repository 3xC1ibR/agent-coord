"""Verify the update waits for native channels the retained transport cannot own."""
from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/agent-coord/scripts"))
from test_codex_app_server import FakeCodex
from agent_coord import desktop_runtime as runtime, runtime_update
from agent_coord.antigravity import AntigravityConnection, AntigravityRPC
from agent_coord.codex_app_server import BrowserSessions
from agent_coord.kimi import KimiConnection, KimiRPC
from agent_coord.muse_code import MuseConnection, MuseRPC
from agent_coord.store import CoordinationStore

FIXTURE = ROOT / "tests/fixtures/update_interruption_provider.py"
PROVIDERS = ("kimi", "antigravity", "muse")
_readiness = runtime.uninterrupted_readiness

class _TransportView:
    projection = SimpleNamespace(error=None)

    def __init__(self, sessions):
        self.__dict__["_sessions"] = sessions

    def __getattr__(self, name):
        return getattr(self._sessions, name)


def readiness_with_transport(sessions):
    return _readiness(sessions if hasattr(sessions, "projection") else _TransportView(sessions))


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
        readiness = patch.object(runtime, "uninterrupted_readiness", readiness_with_transport)
        readiness.start()
        self.addCleanup(readiness.stop)
        self.store = CoordinationStore(self.database)
        self.sessions = self.make_sessions()
        self.directory = runtime.runtime_directory(self.database)
        self.directory.mkdir(parents=True, exist_ok=True)

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

    def test_passive_update_never_interrupts(self):
        thread, turn = self.start("kimi")
        owner = self.owner()
        for _ in range(20):
            with owner.mutex:
                self.assertFalse(owner.prepare_handover())
        self.assertEqual(self.sessions.active.get(thread), turn)
        self.assertFalse(self.events("prompt-ended"))
        self.assertFalse(self.sessions.admission.paused)

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

    def test_explicit_update_leaves_unretained_turns_running_until_natural_completion(self):
        work = {client: self.start(client) for client in PROVIDERS}
        owner = self.owner()
        self.request(owner)
        for _ in range(10):
            with owner.mutex:
                self.assertFalse(owner.prepare_handover())
        update = runtime_update.read(self.directory)
        self.assertEqual(update["phase"], "accepted")
        self.assertCountEqual([row["thread_id"] for row in update["unsupported"]],
                              [thread for thread, _ in work.values()])
        self.assertFalse(self.sessions.admission.paused)
        self.assertFalse(self.events("prompt-ended"))
        for client, (thread, turn) in work.items():
            self.assertEqual(self.sessions.active.get(thread), turn)
            (self.fixture_root / f"release-{self.native(client, thread)}").touch()
        self.wait(lambda: not self.sessions.active)
        self.assertTrue(self.drive(owner))
        self.assertCountEqual([row["stop_reason"] for row in self.events("prompt-ended")],
                              ["end_turn", "end_turn", "completed"])
        self.assertEqual(len(self.events("prompt")), 3)

    def test_pending_approval_can_be_answered_during_the_wait(self):
        thread, _ = self.start("kimi", "ASK then HOLD")
        owner = self.owner()
        self.request(owner)
        key = self.wait(lambda: next((key for key, row in self.sessions.requests.items()
                                     if (row.get("params") or {}).get("threadId") == thread), None))
        self.assertFalse(owner.prepare_handover())
        update = runtime_update.read(self.directory)
        self.assertEqual(update["unsupported"][0]["reason"], "approval")
        self.assertFalse(self.sessions.admission.paused)
        self.sessions.answer(thread, key, {"decision": "accept"})
        self.assertFalse(owner.prepare_handover())
        (self.fixture_root / f"release-{self.native('kimi', thread)}").touch()
        self.wait(lambda: thread not in self.sessions.active)
        self.assertTrue(self.drive(owner))
        self.assertEqual(len(self.events("prompt")), 1)

    def test_background_workflow_requires_exact_retained_ownership(self):
        thread, _ = self.start("muse", "quick answer")
        self.sessions.muse.background_turns[thread] = {"workflow-turn": {"id": "workflow-turn", "items": [
            {"id": "workflow", "nativeKind": "workflow", "status": "inProgress"}]}}
        owner = self.owner()
        self.request(owner)
        self.assertFalse(owner.prepare_handover())
        update = runtime_update.read(self.directory)
        self.assertEqual(update["unsupported"][0]["reason"], "background")
        self.assertFalse(self.sessions.admission.paused)
        self.sessions.projection = SimpleNamespace(error=None, owns_provider=lambda candidate, provider:
                                                   (candidate, provider) == (thread, "muse"))
        try:
            self.assertTrue(self.drive(owner))
        finally:
            del self.sessions.projection  # The fixture transfer owns no real transport.
