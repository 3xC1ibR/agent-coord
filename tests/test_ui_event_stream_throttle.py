from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))

from agent_coord.codex_app_server import BrowserSessions
from agent_coord.store import CoordinationStore
from agent_coord.ui import make_ui_server
from test_codex_app_server import FakeCodex


class EventStreamThrottleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = CoordinationStore(self.root / "coord.sqlite3")
        self.store.register(session_id="terminal", client="codex", cwd=str(self.root))
        self.sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.server = make_ui_server(self.store, port=0, cwd=str(self.root), browser_sessions=self.sessions)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self.stop_server)
        self.url = "http://127.0.0.1:" + str(self.server.server_address[1])

    def stop_server(self):
        self.sessions.close()
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(2)

    def test_provider_deltas_stream_without_rereading_coordination_state(self):
        reads = []
        original = self.sessions.completions_after

        def counted(sequence):
            reads.append(time.monotonic())
            return original(sequence)

        received, batches, done = [], [], threading.Event()

        def listen():
            with urllib.request.urlopen(self.url + "/api/browser/events", timeout=5) as response:
                while not done.is_set():
                    line = response.readline().decode().strip()
                    if line.startswith("data: "):
                        batch = json.loads(line[6:])
                        batches.append(batch)
                        received.extend(e["params"]["delta"] for e in batch["events"]
                                        if e.get("method") == "item/agentMessage/delta")
                        if len(received) == 20:
                            done.set()

        with patch.object(self.sessions, "completions_after", side_effect=counted):
            listener = threading.Thread(target=listen, daemon=True)
            listener.start()
            while not reads:
                time.sleep(0.01)
            started = time.monotonic()
            for index in range(20):
                self.sessions._publish("item/agentMessage/delta", {"threadId": "thread", "delta": str(index)})
                time.sleep(0.02)
            self.assertTrue(done.wait(3))
            elapsed = time.monotonic() - started
            listener.join(3)
        # Every delta arrives, in order, while the burst is still in progress.
        self.assertEqual(received, [str(index) for index in range(20)])
        self.assertGreater(len(batches), 3)
        # Durable state is read on connect and then at most once a second.
        self.assertLessEqual(len(reads), 2 + int(elapsed))
        for earlier, later in zip(reads, reads[1:]):
            self.assertGreaterEqual(later - earlier, 0.95)
        # Batches without a coordination read keep the client's cursor shape.
        self.assertTrue(all("completions" in batch and "completionSeq" in batch for batch in batches))


if __name__ == "__main__":
    unittest.main()
