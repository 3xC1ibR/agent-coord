from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))

from agent_coord.cli import main
from agent_coord.event_subscriptions import EventSubscriptions
from agent_coord.store import CoordinationStore


class EventCommandTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = CoordinationStore(self.root / "state.sqlite3")
        for identity in ("subscriber", "worker", "other"):
            self.store.register(session_id=identity, client="codex", cwd=str(self.root))
        self.store.threads.start_turn("worker", turn_id="work-1")
        environment = patch.dict(os.environ, {"AGENT_COORD_DB": str(self.store.database_path),
                                              "AGENT_COORD_SESSION_ID": "subscriber"})
        environment.start()
        self.addCleanup(environment.stop)

    def invoke(self, *arguments):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = main(arguments)
        return code, json.loads(output.getvalue() or errors.getvalue())

    def test_subscribe_current_turn_and_inspect_cancel_with_caller_identity(self):
        code, subscription = self.invoke("subscribe", "--session", "worker", "--continue", "Review the result")
        self.assertEqual(code, 0)
        identity = subscription["id"]
        self.assertEqual(self.invoke("subscription", "show", identity)[1]["id"], identity)
        self.assertEqual([row["id"] for row in self.invoke("subscription", "list")[1]], [identity])
        self.assertEqual(self.invoke("subscription", "cancel", identity)[1]["state"], "cancelled")
        self.assertEqual(self.invoke("subscription", "show", "--session-id", "other", identity)[0], 2)

    def test_exact_turn_subscription_catches_existing_completion(self):
        self.store.threads.finish_turn("worker", turn_id="work-1")
        self.store.threads.start_turn("worker", turn_id="work-2")
        code, subscription = self.invoke("subscribe", "--turn", "worker:work-1")
        self.assertEqual(code, 0)
        subscriptions = EventSubscriptions(self.store)
        subscriptions.process_once()
        self.assertEqual(subscriptions.get("subscriber", subscription["id"])["state"], "fired")
        self.assertEqual(len(self.store.inbox("subscriber", mark_delivered=False)), 1)

    def test_invalid_subscription_inputs_are_reported_without_tracebacks(self):
        for arguments in (("subscribe",), ("subscribe", "--turn", "malformed"),
                          ("subscribe", "--session", "worker", "--timeout", "nan")):
            with self.subTest(arguments=arguments):
                code, result = self.invoke(*arguments)
                self.assertEqual(code, 2)
                self.assertIn("error", result)

    def test_job_completion_subscription_is_saved_before_launch_and_keeps_argv(self):
        events = []
        record = {"id": "command-1", "state": "queued"}
        with patch("agent_coord.jobs.ManagedJobs") as manager, \
                patch("agent_coord.event_subscriptions.EventSubscriptions") as subscriptions:
            manager.return_value.create.side_effect = lambda *a, **k: events.append(("create", a, k)) or record
            subscriptions.return_value.create.side_effect = lambda *a, **k: events.append(("subscribe", a, k)) or {"id": "wait-1"}
            manager.return_value.launch.side_effect = lambda *a, **k: events.append(("launch", a, k)) or record
            code, result = self.invoke("job", "start", "--wake-self", "--continue", "Read logs", "--",
                                       "printf", "literal $(false)", "two words")
        self.assertEqual(code, 0)
        self.assertEqual([event[0] for event in events], ["create", "subscribe", "launch"])
        self.assertEqual(events[0][1], ("subscriber", ["printf", "literal $(false)", "two words"]))
        self.assertEqual(events[1][2]["jobs"], ["command-1"])
        self.assertEqual(events[1][2]["continuation"], "Read logs")
        self.assertEqual(result["subscription"]["id"], "wait-1")

    def test_job_continuation_requires_a_wake_and_missing_identity_is_clear(self):
        self.assertEqual(self.invoke("job", "start", "--continue", "next", "--", "true")[0], 2)
        with patch.dict(os.environ, {"AGENT_COORD_DB": str(self.store.database_path)}, clear=True):
            code, result = self.invoke("subscribe", "--session", "worker")
        self.assertEqual(code, 2)
        self.assertIn("No caller session", result["error"])


if __name__ == "__main__":
    unittest.main()
