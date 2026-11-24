"""Visit writes in a separate connection pool still receive the owner's canonical decision."""
from contextlib import ExitStack, closing
import http.client
import json
from pathlib import Path
import sys
import tempfile
import unittest
import historical_gateway_fixture
from unittest.mock import patch
from urllib.parse import urlsplit

import test_ui
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))
from historical_gateway import runtime_interaction_gateway as gateway


class VisitTransportTests(unittest.TestCase):
    stop_server = test_ui.BrowserHTTPTests.stop_server
    request = test_ui.BrowserHTTPTests.request

    def setUp(self):
        test_ui.BrowserHTTPTests.setUp(self)
        self.addCleanup(self.sessions.close)
        self.thread = self.sessions.create({})["session"]["thread_id"]
        self.sessions.activity.configure(True)
        g = self.g = gateway.InteractionGateway(self.store.database_path, self.root / "gateway", self.root)
        self.addCleanup(g.delivery_pool.shutdown)
        resources = ExitStack()
        self.addCleanup(resources.close)
        self.primary = resources.enter_context(gateway.GatewayServer(("127.0.0.1", 0), gateway.GatewayHandler))
        self.primary.gateway = g
        g.url = f"http://127.0.0.1:{self.primary.server_port}/"
        g.token, g.control_token = "primary", "control"
        g.backend = (self.url + "/", self.token, "previous", str(self.root))
        record = {"database":str(g.database), "url":g.backend[0], "instance":"previous", "payload":g.backend[3]}
        g.source = g._authority(record)
        gateway.runtime.atomic_json(g.runtime_directory / "discovery.json", record)
        resources.enter_context(patch.object(gateway.runtime, "discover", return_value=record))
        resources.enter_context(gateway.visit_listener(g))

    def send_visit(self, event, **kwargs):
        return self.exchange("POST", body={"events":[event]}, **kwargs)

    def exchange(self, method, *, body=None, path="/api/browser/activity/events", **headers):
        defaults = {"Content-Type":"application/json", "Origin":self.g.url.rstrip("/"),
                    "X-Agent-Coord-Token":self.g.visit_token}
        with closing(http.client.HTTPConnection("127.0.0.1", urlsplit(self.g.visit_origin).port, timeout=3)) as connection:
            connection.request(method, path, None if body is None else json.dumps(body), {**defaults, **headers})
            reply = connection.getresponse()
            data = reply.read()
            return reply.status, dict(reply.getheaders()), json.loads(data) if data else {}

    def event(self, key):
        return {"event_id":key, "kind":"visit_started", "source":"navigation", "surface":"sidebar",
                "window_id":"window-one", "visit_id":"original-visit", "occurred_at":self.store.clock(),
                "thread_id":self.thread}

    def test_visit_endpoint_authorizes_only_its_app_origin_credential_and_route(self):
        allow = {"Access-Control-Request-Method":"POST", "Access-Control-Request-Headers":"content-type,x-agent-coord-token"}
        status, headers, _ = self.exchange("OPTIONS", **allow)
        self.assertEqual((status,headers["Access-Control-Allow-Origin"]), (204,self.g.url.rstrip("/")))
        self.assertEqual(self.exchange("GET", path="/api/browser/config")[0], 404)
        self.assertEqual(self.exchange("POST", path="/api/browser/sessions/one/messages", body={})[0], 403)
        result = self.send_visit(self.event("first"), Origin="http://127.0.0.1:1")
        self.assertEqual(result[0], 403)
        self.assertNotIn("Access-Control-Allow-Origin", result[1])
        self.assertEqual(self.send_visit(self.event("second"), **{"X-Agent-Coord-Token":self.g.token})[0], 403)
        self.g.stop.set()
        self.assertEqual(self.send_visit(self.event("retired"))[0], 503)

    def test_native_receipts_preserve_attribution_consent_scope_clear_and_stop(self):
        started = self.event("visit-one")
        for _ in range(2):
            status, _, result = self.send_visit(started)
            self.assertEqual(status, 200)
            self.assertEqual(result["status"], "ok")
        with self.store.reading(), self.store._connection() as db:
            rows = db.execute("SELECT actor,window_id,visit_id,source,thread_id FROM activity_events WHERE event_id=?",
                              ("visit-one",)).fetchall()
            self.assertEqual([tuple(row) for row in rows], [("human","window-one","original-visit","navigation",self.thread)])
        self.sessions.activity.clear()
        self.assertEqual(self.send_visit(started)[2]["receipts"][0]["reason"],"history_cleared")
        self.sessions.activity.configure(False)
        self.assertEqual(self.send_visit(self.event("paused"))[2]["receipts"][0]["reason"],"recording_paused")
        with tempfile.TemporaryDirectory() as outside:
            self.store.register(session_id="outside", cwd=outside, client="codex")
            self.assertEqual(self.send_visit({**self.event("outside"), "thread_id":"outside"})[0],400)
        self.sessions.activity.configure(True)
        self.g.fence = {"state":"prepared"}
        self.assertEqual(self.send_visit(self.event("offline"))[0],503)
        self.g.fence = {}
        gateway.runtime.atomic_json(self.g.runtime_directory/"stop-proof.json", {"instance":"previous"})
        self.assertEqual(self.send_visit(self.event("stopped"))[0],409)
        with self.store.reading(), self.store._connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM activity_events WHERE kind='visit_started'").fetchone()[0],0)
