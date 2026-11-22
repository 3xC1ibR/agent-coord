"""Optional real Stoic API to RF engine contract, with only the provider simulated.

Set STOIC_REPOSITORY and use Stoic's Python environment to run this suite.
Its API database is isolated; no account credentials or live jobs are used.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
import json
import os
import socket
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))
from agent_coord.codex_app_server import BrowserBusyError, BrowserSessions
from agent_coord.stoic_integration import StoicIntegration, StoicRequestError, StoicClient
from agent_coord.store import CoordinationStore
from test_codex_app_server import FakeCodex


@unittest.skipUnless(os.environ.get("STOIC_REPOSITORY"), "Set STOIC_REPOSITORY for the cross-repository contract")
class StoicExecutionContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        sys.path.insert(0, os.environ["STOIC_REPOSITORY"])
        os.environ.setdefault("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318")
        os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite://")
        import uvicorn
        from tests import test_external_execution
        from src.services.event_feed.router import router

        self.api_module = test_external_execution
        self.fixture = test_external_execution.ExecutionTests(methodName="runTest")
        await self.fixture.asyncSetUp()
        self.addAsyncCleanup(self.fixture.asyncTearDown)
        self.fixture.app.include_router(router)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        self.addCleanup(listener.close)
        self.server = uvicorn.Server(uvicorn.Config(self.fixture.app, log_level="error", lifespan="off"))
        self.server_task = asyncio.create_task(self.server.serve(sockets=[listener]))
        self.addAsyncCleanup(self.stop_server)
        for _ in range(500):
            if self.server.started:
                break
            await asyncio.sleep(0.01)
        self.assertTrue(self.server.started)
        self.origin = "http://127.0.0.1:" + str(listener.getsockname()[1])
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.token_file = self.root / "test-credential"
        self.token_file.write_text("test-only-credential")
        self.transport = StoicClient(self.origin, str(self.token_file))
        self.store = CoordinationStore(self.root / "coordination.sqlite3")
        self.sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(self.sessions.close)
        self.work = self.sessions.external_work
        self.lose_result_response = False
        self.loop = asyncio.get_running_loop()
        self.definition = {
            **test_external_execution.DEFINITION,
            "settings": {"client": "codex", "model": "available-model", "effort": "high",
                         "permission_mode": "full-access", "cwd": str(self.root)},
        }
        with patch.object(test_external_execution, "DEFINITION", self.definition):
            await self.fixture.add_job()
        self.connector = self.restart()
        self.connector.configure({"enabled": True, "allowed_roots": [str(self.root)],
                                  "base_url": self.origin, "token_file": str(self.token_file),
                                  "allowed_permission_modes": {"codex": ["full-access"]}})

    async def stop_server(self):
        self.server.should_exit = True
        await asyncio.wait_for(self.server_task, 10)

    def request(self, method, path, body=None):
        response = self.transport.request(method, path, body)
        if path.split("?")[0].endswith("/result") and self.lose_result_response:
            self.lose_result_response = False
            raise StoicRequestError("Simulated lost result response after server commit.")
        return response

    def restart(self):
        connector = StoicIntegration(self.work, client_factory=lambda *_: self)
        self.addCleanup(connector.close)
        return connector

    async def synchronize(self):
        await asyncio.to_thread(self.connector.process_once)
        self.assertIsNone(self.connector.last_error, self.connector.last_error)

    async def launch(self):
        await self.synchronize()
        await self.synchronize()
        rows = self.work.list()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], "running", rows[0])
        return rows[0]

    async def finish(self, row):
        result = {"text": "Economic briefing\nExact final text.", "artifacts": ["brief.md"]}
        await asyncio.to_thread(self.sessions._event, {
            "method": "turn/completed", "params": {"threadId": row["thread_id"], "turn": {
                "id": row["turn_id"], "status": "completed", "items": [{
                    "type": "agentMessage", "id": "final", "phase": "final_answer", "text": json.dumps(result)
                }]}}})
        self.assertTrue(await asyncio.to_thread(self.sessions.persistence.flush, key=row["thread_id"]))
        return result

    async def expire_lease(self, run_id):
        from src.models.base import get_utc_now
        from src.models.workflows import ExternalExecutionClaim
        async with self.fixture.sessions() as db:
            claim = await db.get(ExternalExecutionClaim, run_id)
            claim.lease_expires_at = get_utc_now() - timedelta(seconds=1)
            await db.commit()

    async def restart_engine(self):
        self.connector.close()
        await asyncio.to_thread(self.sessions.execution.close)
        self.sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(self.sessions.close)
        self.work = self.sessions.external_work
        self.connector = self.restart()

    async def assert_published_result(self, row, result, *, uncertain_notification=False):
        from sqlalchemy import select
        from src.models import OutboxMessage
        remote = (await self.fixture.client.get("/api/execution/runs/" + row["request_id"])).json()
        self.assertEqual(remote["status"], "completed")
        self.assertEqual(remote["result"], result)
        history = (await self.fixture.client.get("/api/investigative-reviews")).json()
        entry = next(item for item in history if item["id"] == row["request_id"])
        self.assertEqual(entry["result"], result)
        async with self.fixture.sessions() as db:
            notifications = list(await db.scalars(select(OutboxMessage).where(
                OutboxMessage.event_type == "send_notification")))
        terminal = [notice for notice in notifications if notice.payload["title"].endswith("completed")]
        self.assertEqual(len(terminal), 1)
        self.assertEqual(terminal[0].payload["description"], result["text"][:178])
        self.assertEqual(len(notifications), 2 if uncertain_notification else 1)

    async def route_definition(self, role="outcomes"):
        """Name a role on the job before its occurrence is selected for execution."""
        response = await self.fixture.client.put("/api/execution/definitions/job/job",
                                                 json={**self.definition, "route": {"role": role}})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["route"], {"role": role})

    def holder(self, name="outcomes"):
        from agent_coord import workspace_roles
        from agent_coord.supervision import USER
        thread = self.sessions.create({})["session"]["thread_id"]
        role = workspace_roles.create_role(self.store, workspace=str(self.root), name=name, requester_kind=USER)
        workspace_roles.appoint(self.store, role_id=role["role_id"], holder=thread, requester_kind=USER,
                                workspace=str(self.root))
        self.connector.configure({"allowed_permission_modes": {
            "codex": ["full-access", self.sessions._record(thread)["permission_mode"]]}})
        return thread

    async def test_run_without_route_carries_no_routing_fields_through_api_and_engine(self):
        row = await self.launch()
        self.assertNotIn("delivery", row)
        result = await self.finish(row)
        await self.synchronize()
        remote = (await self.fixture.client.get("/api/execution/runs/" + row["request_id"])).json()
        self.assertEqual(remote["result"], result)
        self.assertNotIn("route", remote)
        self.assertNotIn("delivery", remote)
        self.assertEqual(self.connector.status()["uncovered_roles"], [])
        self.assertIsNotNone(self.connector.status()["route_coverage_checked_at"])

    async def test_routed_run_reaches_the_role_holder_and_stoic_records_the_delivery(self):
        await self.route_definition()
        holder = await asyncio.to_thread(self.holder)
        threads = sum(method == "thread/start" for method, _ in self.sessions.rpc.calls)
        row = await self.launch()
        self.assertEqual(row["thread_id"], holder)
        self.assertEqual(sum(method == "thread/start" for method, _ in self.sessions.rpc.calls), threads)
        (native,) = [p for method, p in self.sessions.rpc.calls if method == "turn/start"]
        submitted = "\n".join(item.get("text", "") for item in native["input"])
        self.assertTrue(submitted.startswith("Stoic execution context (immutable occurrence):\n"))
        self.assertTrue(submitted.endswith("Instructions:\n" + self.definition["prompt"]))
        result = await self.finish(row)
        self.lose_result_response = True
        await asyncio.to_thread(self.connector.process_once)
        self.assertIn("lost result response", self.connector.last_error)
        await self.synchronize()
        remote = (await self.fixture.client.get("/api/execution/runs/" + row["request_id"])).json()
        self.assertEqual(remote["status"], "completed")
        self.assertEqual(remote["route"], {"role": "outcomes"})
        self.assertEqual(remote["delivery"], {"route": {"role": "outcomes"}, "outcome": "holder", "thread_id": holder})
        self.assertEqual([remote["thread_id"], remote["turn_id"]], [holder, row["turn_id"]])
        await self.assert_published_result(row, result)
        self.assertEqual(self.connector.status()["uncovered_roles"], [])

    async def test_unheld_role_falls_back_and_stoic_records_the_reason(self):
        await self.route_definition()
        row = await self.launch()
        self.assertEqual(row["delivery"]["outcome"], "fallback")
        (native,) = [p for method, p in self.sessions.rpc.calls if method == "turn/start"]
        self.assertEqual((native["threadId"], native["model"], native["effort"]),
                         (row["thread_id"], "available-model", "high"))
        (gap,) = self.connector.status()["uncovered_roles"]
        self.assertEqual([gap["role"], [item["id"] for item in gap["definitions"]]], ["outcomes", ["job"]])
        result = await self.finish(row)
        await self.synchronize()
        remote = (await self.fixture.client.get("/api/execution/runs/" + row["request_id"])).json()
        self.assertEqual(remote["status"], "completed")
        self.assertEqual(remote["delivery"]["outcome"], "fallback")
        self.assertIn("does not exist", remote["delivery"]["reason"])
        await self.assert_published_result(row, result)

    async def test_actual_api_engine_result_retry_and_notification_idempotency(self):
        from sqlalchemy import func, select
        from src.models import OutboxMessage

        row = await self.launch()
        native = [p for method, p in self.sessions.rpc.calls if method == "turn/start"]
        self.assertEqual(len(native), 1)
        self.assertEqual((native[0]["model"], native[0]["effort"]), ("available-model", "high"))
        self.assertEqual(native[0]["sandboxPolicy"]["type"], "dangerFullAccess")
        submitted = "\n".join(item.get("text", "") for item in native[0]["input"])
        snapshot = (await self.fixture.client.get("/api/execution/runs/" + row["request_id"])).json()
        self.assertIn(json.dumps({"run_id": snapshot["id"], **{key: snapshot[key] for key in (
            "source", "source_id", "occurrence_id", "scheduled_at")}}, sort_keys=True), submitted)
        self.assertIn("Instructions:\n" + self.definition["prompt"], submitted)
        result = await self.finish(row)
        self.lose_result_response = True
        await asyncio.to_thread(self.connector.process_once)
        self.assertIn("lost result response", self.connector.last_error)
        self.connector.close()
        self.connector = self.restart()
        await self.synchronize()
        remote = (await self.fixture.client.get("/api/execution/runs/" + row["request_id"])).json()
        self.assertEqual(remote["status"], "completed")
        self.assertEqual(remote["result"], result)
        self.assertEqual(remote["thread_id"], row["thread_id"])
        self.assertEqual(remote["turn_id"], row["turn_id"])
        self.assertNotIn("claim_token", remote)
        self.assertEqual(self.connector.status()["runs"], {"published": 1})
        await self.assert_published_result(row, result)
        async with self.fixture.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(OutboxMessage).where(
                OutboxMessage.event_type == "send_notification")), 1)
        self.assertEqual(sum(m == "turn/start" for m, _ in self.sessions.rpc.calls), 1)

    async def test_source_cancellation_stops_the_correlated_turn(self):
        row = await self.launch()
        response = await self.fixture.client.put("/api/execution/definitions/job/job", json={"mode": "internal"})
        self.assertEqual(response.status_code, 200)
        await self.synchronize()
        self.assertTrue(await asyncio.to_thread(self.sessions.persistence.flush, key=row["thread_id"]))
        await self.synchronize()
        local = self.work.get(row["source"], row["request_id"])
        self.assertEqual(local["state"], "cancelled", local)
        remote = (await self.fixture.client.get("/api/execution/runs/" + row["request_id"])).json()
        self.assertEqual(remote["status"], "cancelled")
        interrupts = [p for method, p in self.sessions.rpc.calls if method == "turn/interrupt"]
        self.assertEqual(interrupts, [{"threadId": row["thread_id"], "turnId": row["turn_id"]}])

    async def test_runtime_maintenance_preserves_pending_work(self):
        self.sessions.admission.pause()
        await self.synchronize()
        await self.synchronize()
        pending = (await self.fixture.client.get("/api/execution/runs")).json()["runs"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(self.work.list(), [])
        self.sessions.admission.resume()
        row = await self.launch()
        await self.finish(row)
        await self.synchronize()

    async def test_expired_unsubmitted_claim_after_restart_fails_without_execution(self):
        await self.synchronize()
        with patch.object(self.work, "submit", side_effect=BrowserBusyError("Maintenance")):
            await self.synchronize()
        run_id = self.connector._bindings()[0]["run_id"]
        await self.expire_lease(run_id)
        await self.restart_engine()
        await self.synchronize()
        remote = (await self.fixture.client.get("/api/execution/runs/" + run_id)).json()
        self.assertEqual(remote["status"], "failed")
        self.assertIn("did not start", remote["error"])
        self.assertEqual(self.work.list(), [])
        self.assertFalse(any(method == "turn/start" for method, _ in self.sessions.rpc.calls))

    async def test_expired_started_claim_renews_exact_binding_after_connector_restart(self):
        row = await self.launch()
        await self.expire_lease(row["request_id"])
        self.connector.close()
        self.connector = self.restart()
        await self.synchronize()
        remote = (await self.fixture.client.get("/api/execution/runs/" + row["request_id"])).json()
        self.assertEqual(remote["status"], "running")
        self.assertEqual((remote["thread_id"], remote["turn_id"]), (row["thread_id"], row["turn_id"]))
        result = await self.finish(row)
        await self.synchronize()
        await self.assert_published_result(row, result)
        self.assertEqual(sum(method == "turn/start" for method, _ in self.sessions.rpc.calls), 1)

    async def test_uncertain_after_engine_restart_resolves_real_evidence_once(self):
        row = await self.launch()
        self.assertTrue(await asyncio.to_thread(self.sessions.persistence.flush, key=row["thread_id"]))
        await self.expire_lease(row["request_id"])
        await self.restart_engine()
        self.assertEqual(self.work.get(row["source"], row["request_id"])["state"], "uncertain")
        await self.synchronize()
        self.assertEqual(self.connector.status()["runs"], {"uncertain": 1})
        result = await self.finish(row)
        resolved = await asyncio.to_thread(self.work.resolve, row["source"], row["request_id"])
        self.assertEqual(resolved["state"], "succeeded")
        self.assertEqual(self.work.resolve(row["source"], row["request_id"]), resolved)
        self.lose_result_response = True
        await asyncio.to_thread(self.connector.process_once)
        self.connector.close()
        self.connector = self.restart()
        await self.synchronize()
        await self.assert_published_result(row, result, uncertain_notification=True)
        self.assertFalse(any(method == "turn/start" for method, _ in self.sessions.rpc.calls))

    async def test_source_instruction_edit_preserves_inflight_output(self):
        row = await self.launch()
        changed = {**self.definition, "prompt": "Different instructions for future occurrences"}
        self.assertEqual((await self.fixture.client.put("/api/execution/definitions/job/job", json=changed)).status_code, 200)
        result = await self.finish(row)
        await self.synchronize()
        await self.assert_published_result(row, result)
        remote = (await self.fixture.client.get("/api/execution/runs/" + row["request_id"])).json()
        self.assertEqual(remote["prompt"], self.definition["prompt"])

    async def test_local_policy_rejects_unapproved_full_access_before_provider(self):
        self.connector.configure({"allowed_permission_modes": {"codex": ["workspace-write"]}})
        await self.synchronize()
        await asyncio.to_thread(self.connector.process_once)
        self.assertIn("permission mode", self.connector.last_error)
        remote = (await self.fixture.client.get("/api/execution/runs?status=pending")).json()["runs"]
        self.assertEqual(len(remote), 1)
        self.assertIsNone(remote[0]["runner_id"])
        self.assertEqual(self.work.list(), [])
        self.assertFalse(any(method in {"thread/start", "turn/start"} for method, _ in self.sessions.rpc.calls))


if __name__ == "__main__":
    unittest.main()
