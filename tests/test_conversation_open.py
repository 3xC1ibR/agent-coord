from __future__ import annotations

import http.client
import json
import sqlite3
import statistics
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))

from agent_coord.app_control import AppControl, AppControlWorker
from agent_coord.codex_app_server import BrowserSessions
from agent_coord.history_window import RECENT_TURNS
from agent_coord.store import CoordinationStore
from agent_coord.ui import make_ui_server
from test_codex_app_server import FakeCodex


class ConversationOpenTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / ".git").mkdir()
        self.store = CoordinationStore(self.root / "state.sqlite3")
        self.addCleanup(self.store.close_idle_connections)
        self.sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.server = make_ui_server(self.store, port=0, cwd=str(self.root), browser_sessions=self.sessions)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self.stop_server)
        self.host, self.port = self.server.server_address[:2]
        self.url = f"http://127.0.0.1:{self.port}"
        status, config = self.request("/api/browser/config")
        self.assertEqual(status, 200)
        self.assertIs(config.get("compositeOpen"), True)
        self.token = config["token"]

    def stop_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(2)
        self.sessions.close()

    def request(self, path: str, body=None, headers: dict[str, str] | None = None):
        req_headers = dict(headers or {})
        if body is not None:
            req_headers.setdefault("Content-Type", "application/json")
            req_headers.setdefault("X-Agent-Coord-Token", getattr(self, "token", ""))
        req = urllib.request.Request(
            self.url + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers=req_headers,
        )
        try:
            response = urllib.request.urlopen(req, timeout=5)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            raw = response.read().decode()
            return (
                response.status,
                json.loads(raw) if response.headers.get_content_type() == "application/json" else raw,
            )

    @contextmanager
    def held_writer(self):
        writer = sqlite3.connect(self.store.database_path)
        try:
            writer.execute("BEGIN IMMEDIATE")
            yield writer
        finally:
            writer.rollback()
            writer.close()

    def test_composite_open_matches_individual_routes_without_writes_or_provider_calls(self) -> None:
        # Clear any pre-cached model catalog so cold /open verifies models=None lazy behavior first.
        self.sessions.model_catalogs.clear()
        parent = self.sessions.create({"name": "Supervisor"})["session"]["thread_id"]
        self.sessions.model_catalogs.clear()

        control_worker = AppControlWorker(self.sessions)
        self.addCleanup(control_worker.close)
        create_req = control_worker.control.request(
            "create",
            parent,
            {"cwd": str(self.root), "prompt": "Inspect schema.", "mode": "orchestration"},
        )
        control_worker.process_once()
        child = control_worker.control.status(create_req["request_id"])["thread_id"]

        self.store.asks.create(
            parent,
            "Approve staging rollout?",
            blocking=True,
            kind="approval",
        )
        schedule = self.sessions.schedules.create(
            {
                "id": str(uuid.uuid4()),
                "message": "Check nightly status",
                "run_at": self.store.clock() + 3600,
                "timezone": "UTC",
                "settings": {"cwd": str(self.root), "model": "available-model"},
            }
        )
        # Populate model catalog in memory once via /models, then clear on a subtest to verify both states.
        self.sessions.model_catalogs.clear()

        # Insert a stale running sidecar request to prove /open projects it read-only without mutating SQLite.
        app_control = AppControl(self.store)
        sidecar_req = app_control.request(
            "sidecar",
            parent,
            {"target_id": parent, "purpose": "Verify test assertions."},
            request_id="stale-sidecar",
        )
        self.assertEqual(sidecar_req["status"], "queued")
        with self.store._connection() as db:
            db.execute(
                "UPDATE app_control_requests SET status = 'running', owner = 'gone-owner', updated_at = ? WHERE request_id = ?",
                (self.store.clock() - 3600, "stale-sidecar"),
            )

        turns = [
            {
                "id": f"turn-{idx}",
                "status": "completed",
                "items": [
                    {"id": f"u-{idx}", "type": "userMessage", "content": [{"type": "text", "text": f"Q {idx}"}]},
                    {"id": f"a-{idx}", "type": "agentMessage", "text": f"A {idx}"},
                ],
            }
            for idx in range(50)
        ]
        self.sessions._remember_thread({"id": parent, "turns": turns, "status": {"type": "idle"}})
        self.assertTrue(self.sessions.persistence.flush(timeout=3, key=parent))

        query_only_flags: list[int] = []
        original_conn = self.store._connection

        @contextmanager
        def checked_connection():
            with original_conn() as db:
                query_only_flags.append(db.execute("PRAGMA query_only").fetchone()[0])
                yield db

        with self.held_writer(), patch.object(self.store, "_connection", checked_connection), patch.object(
            self.sessions.rpc, "request", side_effect=AssertionError("Composite open called provider RPC")
        ):
            status, opened_cold_models = self.request(f"/api/browser/conversations/{parent}/open")

        self.assertEqual(status, 200, opened_cold_models)
        self.assertTrue(query_only_flags)
        self.assertTrue(all(flag == 1 for flag in query_only_flags), query_only_flags)
        self.assertIsNone(opened_cold_models["models"])
        self.assertEqual(len(opened_cold_models["recent_turns"]), RECENT_TURNS)
        self.assertEqual(opened_cold_models["recent_turns"], opened_cold_models["thread"]["turns"])
        self.assertEqual(opened_cold_models["thread"]["earlierTurns"], 50 - RECENT_TURNS)
        self.assertEqual(opened_cold_models["work_thread"]["thread_id"], parent)
        self.assertIn("close_when_done", opened_cold_models["work_thread"])
        self.assertEqual(opened_cold_models["close_when_done"], opened_cold_models["work_thread"]["close_when_done"])
        self.assertEqual(len(opened_cold_models["asks"]), 1)
        self.assertEqual(opened_cold_models["asks"][0]["session_id"], parent)
        self.assertEqual(opened_cold_models["agent_tree"]["root_thread_id"], parent)
        self.assertEqual(
            {item["thread_id"] for item in opened_cold_models["agent_tree"]["data"]},
            {parent, child},
        )
        self.assertEqual(opened_cold_models["sidecars"]["watches"], [])
        self.assertEqual(len(opened_cold_models["sidecars"]["requests"]), 1)
        self.assertEqual(opened_cold_models["sidecars"]["requests"][0]["status"], "uncertain")
        self.assertEqual([item["id"] for item in opened_cold_models["schedules"]], [schedule["id"]])

        # Confirm the stale sidecar row in SQLite was NOT mutated during /open.
        with self.store.reading(), self.store._connection() as db:
            raw_status = db.execute(
                "SELECT status FROM app_control_requests WHERE request_id = 'stale-sidecar'"
            ).fetchone()[0]
        self.assertEqual(raw_status, "running")

        # Warm the model catalog and verify /open returns cached models and supports ?full=1.
        expected_models = self.request("/api/browser/models?client=codex")[1]["data"]
        expected_tree = self.request(f"/api/browser/threads/{parent}/agent-tree")[1]
        with self.held_writer(), patch.object(
            self.sessions.rpc, "request", side_effect=AssertionError("Composite open called provider RPC")
        ):
            status, opened_full = self.request(f"/api/browser/conversations/{parent}/open?full=1")
        self.assertEqual(status, 200)
        self.assertEqual(opened_full["models"], expected_models)
        self.assertEqual(len(opened_full["thread"]["turns"]), 50)
        self.assertEqual(opened_full["agent_tree"], expected_tree)

        # Pagination and invalid parameter checks.
        status, paged = self.request(f"/api/browser/conversations/{parent}/open?before_seq=10&limit=4")
        self.assertEqual(status, 200)
        self.assertEqual([t["id"] for t in paged["recent_turns"]], ["turn-6", "turn-7", "turn-8", "turn-9"])
        for bad_query in ("?view=invalid", "?full=maybe", "?limit=0", "?before_seq=-1", "?extra=1"):
            self.assertEqual(self.request(f"/api/browser/conversations/{parent}/open{bad_query}")[0], 400)

    def test_external_terminal_thread_falls_back_cleanly_without_spawning_history_rpc(self) -> None:
        self.store.register(session_id="terminal-ext", client="codex", cwd=str(self.root), name="Terminal session")
        self.store.register(session_id="peer-ext", client="claude", cwd=str(self.root), name="Peer")
        self.store.send_message(
            sender_session_id="peer-ext",
            recipient_session_id="terminal-ext",
            body="Status check from peer.",
        )
        with self.held_writer(), patch.object(
            self.sessions, "_stored_thread", side_effect=AssertionError("External open spawned history RPC")
        ), patch.object(
            self.sessions.rpc, "request", side_effect=AssertionError("External open contacted provider")
        ):
            status, opened = self.request("/api/browser/conversations/terminal-ext/open")
        self.assertEqual(status, 200, opened)
        self.assertEqual(opened["session"]["cwd"], str(self.root))
        self.assertEqual(opened["thread"]["turns"], [])
        self.assertEqual(opened["recent_turns"], [])
        self.assertEqual(opened["requests"], [])
        self.assertFalse(opened["running"])
        self.assertEqual(len(opened["coordinationMessages"]), 1)
        self.assertEqual(opened["sidecars"], {"watches": [], "requests": []})
        self.assertIsNone(opened["models"])
        self.assertIsNone(self.sessions.history_rpc)

    def test_benchmark_100_turn_conversation_open_under_5ms_with_zero_write_scopes(self) -> None:
        thread_id = self.sessions.create({"name": "Hundred-turn benchmark"})["session"]["thread_id"]
        self.sessions.models("codex")
        large_output = "benchmark-line-" * 120
        turns = [
            {
                "id": f"turn-{index}",
                "status": "completed",
                "items": [
                    {
                        "id": f"user-{index}",
                        "type": "userMessage",
                        "content": [{"type": "text", "text": f"Investigate step {index}"}],
                    },
                    {
                        "id": f"cmd-{index}",
                        "type": "commandExecution",
                        "command": "pytest -q",
                        "status": "completed",
                        "aggregatedOutput": large_output,
                    },
                    {
                        "id": f"agent-{index}",
                        "type": "agentMessage",
                        "text": f"Completed analysis for step {index}.",
                    },
                ],
            }
            for index in range(100)
        ]
        self.sessions._remember_thread({"id": thread_id, "turns": turns, "status": {"type": "idle"}})
        self.assertTrue(self.sessions.persistence.flush(timeout=5, key=thread_id))
        for component in (self.server.app_control, self.sessions.inbox_wake, self.sessions.sidecars):
            component.close()

        path = f"/api/browser/conversations/{thread_id}/open"
        for mode in ("resident", "saved"):
            with self.subTest(mode=mode):
                if mode == "saved":
                    self.sessions.histories.pop(thread_id, None)
                    self.sessions.loaded.discard(thread_id)
                conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
                try:
                    # Warm up worker thread-local SQLite connection and window cache once.
                    conn.request("GET", path)
                    warm_resp = conn.getresponse()
                    warm_body = json.loads(warm_resp.read())
                    self.assertEqual(warm_resp.status, 200)
                    self.assertEqual(len(warm_body["recent_turns"]), RECENT_TURNS)
                    self.assertEqual(warm_body["thread"]["earlierTurns"], 100 - RECENT_TURNS)

                    def write_scope_total() -> int:
                        metrics = self.store.database_metrics()
                        return metrics["completed_scopes"] + metrics["failed_scopes"] + metrics["admission_timeouts"]

                    durations_ms: list[float] = []
                    with self.held_writer(), patch.object(
                        self.sessions.rpc, "request", side_effect=AssertionError("Unexpected provider RPC")
                    ):
                        before_writes = write_scope_total()
                        for _ in range(35):
                            started = time.perf_counter()
                            conn.request("GET", path)
                            resp = conn.getresponse()
                            raw = resp.read()
                            elapsed_ms = (time.perf_counter() - started) * 1000.0
                            self.assertEqual(resp.status, 200)
                            self.assertGreater(len(raw), 100)
                            durations_ms.append(elapsed_ms)
                        after_writes = write_scope_total()
                finally:
                    conn.close()

                median_ms = statistics.median(durations_ms)
                self.assertEqual(after_writes - before_writes, 0, "Expected 0 SQLite write scopes during /open")
                self.assertLess(
                    median_ms,
                    5.0,
                    f"Expected median /open latency < 5 ms ({mode}), got {median_ms:.3f} ms",
                )


if __name__ == "__main__":
    unittest.main()
