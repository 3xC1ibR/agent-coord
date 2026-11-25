"""Stage 2.4 & Stage 4: Brokered providers transfer online without forced turn-interruption blockers."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
import sys  # noqa: E402
sys.path.insert(0, str(ROOT / "plugins/agent-coord/scripts"))

from agent_coord import desktop_runtime as runtime  # noqa: E402
from agent_coord import runtime_recovery  # noqa: E402
from agent_coord import runtime_update  # noqa: E402
import test_desktop_runtime as desktop_tests  # noqa: E402


class BrokeredProviderReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="ribbon-brokered-readiness-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / "state.sqlite3"
        self.directory = runtime.runtime_directory(self.database)

    def test_forced_interruption_constants_and_methods_are_removed(self):
        for obsolete in ("UPDATE_UNSUPPORTED_GRACE_SECONDS", "UPDATE_INTERRUPTION_SECONDS",
                         "UPDATE_CONFIRMATION_SECONDS", "HANDOVER_EXIT_CODE"):
            self.assertFalse(hasattr(runtime, obsolete), f"{obsolete} should be deleted in Stage 2.4 / 4.2")
        for obsolete_method in ("INTERRUPTIBLE_PROVIDERS", "plan_interruption",
                                "settle_interruption", "abandon_interruptions"):
            self.assertFalse(hasattr(runtime_recovery.RuntimeRecovery, obsolete_method),
                             f"RuntimeRecovery.{obsolete_method} should be deleted")

    def test_uninterrupted_readiness_transfers_all_five_brokered_providers_including_muse_workflows(self):
        owned = {("codex", "t-codex"), ("claude", "t-claude"), ("kimi", "t-kimi"),
                 ("antigravity", "t-antigravity"), ("muse", "t-muse")}
        sessions = SimpleNamespace(
            admission=runtime.AdmissionGate(),
            lock=runtime.threading.RLock(),
            active={tid: f"turn-{client}" for client, tid in owned},
            loaded_clients={tid: client for client, tid in owned},
            requests={"req-1": {"params": {"threadId": "t-kimi"}}},
            muse=SimpleNamespace(
                lock=runtime.threading.RLock(),
                turns={"t-muse": {"items": [{"id": "wf", "nativeKind": "workflow", "status": "inProgress"}]}},
                background_turns={"t-muse": {"bg": {"items": [{"id": "wf2", "nativeKind": "workflow", "status": "inProgress"}]}}},
            ),
            projection=SimpleNamespace(
                error=None,
                owns_provider=lambda provider, thread_id: (provider, thread_id) in owned,
            ),
            queue=SimpleNamespace(wake=Mock()),
            dispatch=SimpleNamespace(wake=Mock()),
        )
        self.assertEqual(runtime.uninterrupted_readiness(sessions), [])

        owner = runtime.Runtime(self.database, self.root / "previous", -1)
        owner.pending = self.root / "candidate"
        owner.server = SimpleNamespace(browser_sessions=sessions)
        owner.request_update("all-five-brokered", str(owner.pending))
        self.assertTrue(owner.prepare_handover())
        self.assertTrue(sessions.admission.paused)

    def test_unbrokered_active_provider_defers_handover_without_interrupting_turn(self):
        sessions = SimpleNamespace(
            admission=runtime.AdmissionGate(),
            lock=runtime.threading.RLock(),
            active={"t-custom": "turn-1"},
            loaded_clients={"t-custom": "kimi"},
            projection=SimpleNamespace(error=None, owns_provider=lambda provider, thread_id: False),
            queue=SimpleNamespace(wake=Mock()),
            dispatch=SimpleNamespace(wake=Mock()),
        )
        owner = runtime.Runtime(self.database, self.root / "previous", -1)
        owner.pending = self.root / "candidate"
        owner.server = SimpleNamespace(browser_sessions=sessions)
        owner.request_update("unbrokered-deferred", str(owner.pending))

        self.assertFalse(owner.prepare_handover())
        self.assertFalse(sessions.admission.paused)
        self.assertEqual(sessions.active["t-custom"], "turn-1")
        self.assertIn("active unbrokered provider: kimi", runtime.uninterrupted_readiness(sessions))

    def test_candidate_validation_failure_rejects_update_without_pausing_work(self):
        sessions = SimpleNamespace(
            admission=runtime.AdmissionGate(),
            lock=runtime.threading.RLock(),
            active={"t-codex": "turn-1"},
            loaded_clients={"t-codex": "codex"},
            projection=SimpleNamespace(error=None, owns_provider=lambda provider, thread_id: True),
            queue=SimpleNamespace(wake=Mock()),
            dispatch=SimpleNamespace(wake=Mock()),
        )
        owner = runtime.Runtime(self.database, self.root / "previous", -1)
        owner.pending = self.root / "candidate"
        owner.server = SimpleNamespace(browser_sessions=sessions, server_port=12345)
        owner.validate_candidate = lambda _candidate, capacity=False: "syntax error in candidate"
        owner.request_update("invalid-candidate", str(owner.pending))

        outcome, frozen = owner._execute_blue_green_handover(owner.pending)
        self.assertEqual((outcome, frozen), ("failed", False))
        update = runtime_update.read(self.directory)
        self.assertEqual(update["phase"], "failed")
        self.assertIn("Candidate validation failed: syntax error in candidate", update["error"])
        self.assertFalse(sessions.admission.paused)
        self.assertEqual(sessions.active["t-codex"], "turn-1")


class UninterruptedProcessUpdateTests(unittest.TestCase):
    setUp = desktop_tests.RuntimeTests.setUp
    tearDown = desktop_tests.RuntimeTests.tearDown
    wait = desktop_tests.RuntimeTests.wait
    launch = desktop_tests.RuntimeTests.launch
    log = desktop_tests.RuntimeTests.log
    api = desktop_tests.RuntimeTests.api
    calls = desktop_tests.RuntimeTests.calls
    wait_update = desktop_tests.RuntimeTests.wait_update
    prepare_requested_update = desktop_tests.RuntimeTests.prepare_requested_update

    def test_explicit_update_transfers_running_turn_without_turn_interrupt(self):
        self.env["RIBBON_FAKE_PERSIST"] = "1"
        first, record, thread, staged, _candidate = self.prepare_requested_update(message="held-work")
        provider_pid = int((self.root / "provider.pid").read_text())
        original_turn = self.api(first["url"], f"api/browser/sessions/{thread}")["activeTurn"]

        runtime.control(record, "activate-update", request_id="stage24-live-transfer", payload=staged["pending"])
        current = self.wait_update("committed")
        self.assertEqual(current["url"], first["url"])
        self.assertNotEqual(current["pid"], first["pid"])
        self.assertEqual(int((self.root / "provider.pid").read_text()), provider_pid)
        self.assertTrue(current["update"]["uninterrupted"])
        self.assertEqual(self.api(current["url"], f"api/browser/sessions/{thread}")["activeTurn"], original_turn)
        self.assertNotIn("turn/interrupt", [c.get("method") for c in self.calls()])

        (self.root / "release-held-work").touch()
        self.wait(lambda: not self.api(current["url"], f"api/browser/sessions/{thread}")["running"])
        self.assertNotIn("ResourceWarning", self.log())


if __name__ == "__main__":
    unittest.main()
