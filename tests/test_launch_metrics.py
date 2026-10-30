import json
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))
from agent_coord import launch_metrics as metrics
from agent_coord.store import CoordinationStore


class LaunchMetricsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 1_800_000_000
        self.store = CoordinationStore(Path(self.temp.name) / "state.sqlite3", clock=lambda: self.now)

    def events(self):
        with self.store._connection() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT payload FROM launch_outbox ORDER BY created_at, rowid")]

    def register(self, name):
        self.store.register(session_id=name, client="codex", cwd=self.temp.name)

    def test_hooks_do_not_initialize_measurement(self):
        self.register("one")
        self.register("two")
        self.store.threads.checkpoint("one", {"phase": "discussion", "summary": "Private content"})
        self.assertEqual(metrics.settings(self.store), {"available": False, "enabled": False})

    def test_activation_is_once_and_contains_no_session_or_content(self):
        metrics.initialize(self.store)
        self.register("private-session")
        self.store.threads.checkpoint("private-session", {"phase": "implementation", "summary": "secret prompt"})
        self.store.begin_work(session_id="private-session", scopes=["secret-file.py"])
        self.assertEqual([e["event"] for e in self.events()], ["app_first_launch", "first_coordinated_action"])
        encoded = json.dumps(self.events())
        for private in ("private-session", "secret prompt", "secret-file", self.temp.name):
            self.assertNotIn(private, encoded)
        self.assertEqual(self.events()[1]["criterion"], "checkpoint")

    def test_two_sessions_and_return_window(self):
        metrics.initialize(self.store)
        self.register("one")
        self.register("two")
        self.assertEqual(self.events()[1]["criterion"], "two_sessions")
        self.now += 3 * metrics.DAY - 1
        metrics.record_action(self.store, "checkpoint")
        self.assertEqual(len(self.events()), 2)
        self.now += 1
        metrics.record_action(self.store, "write_scope")
        metrics.record_action(self.store, "checkpoint")
        self.assertEqual([e["event"] for e in self.events()][-1], "d3_active_session")
        self.assertEqual(len(self.events()), 3)

    def test_activation_and_return_outside_window(self):
        metrics.initialize(self.store)
        self.now += metrics.DAY + 1
        metrics.record_action(self.store, "checkpoint")
        self.now += 3 * metrics.DAY
        metrics.record_action(self.store, "checkpoint")
        self.assertEqual(len(self.events()), 1)

    def test_existing_install_and_restart_identity(self):
        self.register("existing")
        metrics.initialize(self.store)
        first = self.events()[0]
        self.assertTrue(first["existing_install"])
        metrics.initialize(self.store)
        self.assertEqual(self.events(), [first])

    def test_optout_clears_queue_and_stops_new_events(self):
        metrics.initialize(self.store)
        metrics.configure(self.store, False)
        metrics.record_action(self.store, "checkpoint")
        metrics.attribute(self.store, str(uuid.uuid4()))
        metrics.initialize(self.store)
        metrics.flush(self.store, send=lambda _: self.fail("Sent after opt-out"))
        self.assertEqual(self.events(), [])

    def test_delivery_retry_preserves_event_id_and_attribution(self):
        metrics.initialize(self.store)
        token = str(uuid.uuid4())
        metrics.attribute(self.store, token)
        metrics.attribute(self.store, str(uuid.uuid4()))
        original = self.events()
        metrics.flush(self.store, send=lambda _: (_ for _ in ()).throw(OSError("offline")))
        self.assertEqual(self.events(), original)
        received = []
        metrics.flush(self.store, send=received.append)
        self.assertEqual(received, original)
        self.assertEqual(received[1]["download_token"], token)
        self.assertEqual(self.events(), [])

    def test_measurement_failure_does_not_break_action(self):
        with patch.object(self.store, "_connection", side_effect=OSError("disk full")):
            metrics.record_action(self.store, "checkpoint")

    def test_old_outbox_events_are_pruned(self):
        metrics.initialize(self.store)
        self.now += 8 * metrics.DAY
        received = []
        metrics.flush(self.store, send=received.append)
        self.assertEqual(received, [])
        self.assertEqual(self.events(), [])

    def test_development_and_mounted_builds_never_start_transport(self):
        for path, config in (("/Applications/Ribbon Field.app/Contents/Resources", {"database": "/dev/db"}),
                             ("/Volumes/Ribbon Field/Ribbon Field.app/Contents/Resources", {}),
                             ("/tmp/Ribbon Field.app/Contents/Resources", {})):
            with self.subTest(path=path), patch.object(Path, "read_text", return_value=json.dumps(config)), \
                    patch.object(metrics.threading, "Thread") as worker:
                metrics.start(self.store, threading.Event(), path)
                worker.assert_not_called()
                self.assertFalse(metrics.settings(self.store)["available"])


if __name__ == "__main__":
    unittest.main()
