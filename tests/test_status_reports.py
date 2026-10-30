from __future__ import annotations

from contextlib import nullcontext
import http.client
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))
from agent_coord.status_reports import StatusReports
from agent_coord.store import CoordinationError, CoordinationStore
from agent_coord.ui import _handler


def publish(root, identifier="a" * 64, workspace="/project-one", provider="codex"):
    directory = root / "reports" / identifier
    directory.mkdir(parents=True)
    manifest = {"version": 1, "id": identifier, "title": "Status", "subject": "Worker", "summary": "Complete",
                "generated_at": "2026-10-30T10:00:00Z", "received_at": "2026-10-30T10:01:00Z",
                "origin": {"workspace": workspace, "agent": "owner", "provider": provider, "host": "machine"}}
    (directory / "manifest.json").write_text(json.dumps(manifest))
    (directory / "report.html").write_text('<h1>Retained report</h1><script>parent.compromised=true</script>')
    return identifier


class StatusReportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.reports = StatusReports(self.root)

    def test_all_origins_and_persistent_dismissal_without_deleting_content(self):
        first = publish(self.root)
        second = publish(self.root, "b" * 64, "/project-two", "claude")
        self.assertEqual(len(self.reports.list()["reports"]), 2)
        self.reports.dismiss(first, True)
        reopened = StatusReports(self.root)
        records = {report["id"]: report for report in reopened.list()["reports"]}
        self.assertTrue(records[first]["dismissed_at"])
        self.assertIsNone(records[second]["dismissed_at"])
        self.assertIn("Retained report", reopened.content(first))
        reopened.dismiss(first, False)
        self.assertIsNone(reopened._manifest(first)["dismissed_at"])

    def test_new_report_from_dismissed_agent_is_visible_and_partial_records_are_ignored(self):
        first = publish(self.root)
        self.reports.dismiss(first, True)
        next_identifier = publish(self.root, "b" * 64)
        (self.root / "reports" / ".pending-test").mkdir()
        (self.root / "reports" / ("c" * 64)).mkdir()
        result = self.reports.list()
        self.assertEqual(result["unreadable"], 1)
        self.assertEqual([report["id"] for report in result["reports"] if not report["dismissed_at"]], [next_identifier])

    def test_traversal_symlinks_and_special_files_are_not_served(self):
        identifier = publish(self.root)
        for value in ("../private", "", "a" * 65, []):
            with self.subTest(value=value), self.assertRaises(CoordinationError):
                self.reports.content(value)
        report = self.root / "reports" / identifier / "report.html"
        report.unlink()
        secret = self.root / "private.txt"
        secret.write_text("private")
        report.symlink_to(secret)
        with self.assertRaises(CoordinationError):
            self.reports.content(identifier)
        report.unlink()
        os.mkfifo(report)
        with self.assertRaises(CoordinationError):
            self.reports.content(identifier)


class StatusReportHTTPTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.inbox = self.root / "inbox"
        self.identifier = publish(self.inbox, workspace="/another-project")
        environment = patch.dict(os.environ, {"AGENT_STATUS_INBOX": str(self.inbox)})
        environment.start()
        self.addCleanup(environment.stop)
        self.store = CoordinationStore(self.root / "state.sqlite3")
        sessions = SimpleNamespace(cwd="/current-project", admission=SimpleNamespace(enter=lambda: nullcontext(True)), inventory_changed=lambda: None)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(self.store, None, "/current-project", sessions, csrf_token="test-token", provider_limits=object()))
        worker = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        worker.start()
        self.addCleanup(worker.join, 2)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, route, body=None, *, token="test-token", origin=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            headers = {"Content-Type": "application/json", "X-Agent-Coord-Token": token}
            if origin:
                headers["Origin"] = origin
            connection.request("GET" if body is None else "POST", route, body=None if body is None else json.dumps(body), headers=headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_global_listing_ignores_current_workspace_and_assets_load(self):
        status, _, body = self.request("/api/browser/status-reports")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["reports"][0]["origin"]["workspace"], "/another-project")
        for route in ("/status", "/status-reports.js", "/status-reports.css"):
            self.assertEqual(self.request(route)[0], 200)

    def test_dismissal_requires_csrf_and_persists(self):
        route = "/api/browser/status-reports/dismiss"
        body = {"id": self.identifier, "dismissed": True}
        self.assertEqual(self.request(route, body, token="")[0], 403)
        self.assertEqual(self.request(route, body, origin="https://untrusted.example")[0], 403)
        self.assertEqual(self.request(route, body)[0], 200)
        self.assertTrue(StatusReports(self.inbox)._manifest(self.identifier)["dismissed_at"])
        self.assertEqual(self.request(route, {**body, "dismissed": "false"})[0], 400)
        self.assertEqual(self.request(route, {**body, "dismissed": False})[0], 200)

    def test_report_content_is_isolated_and_embedded_page_allowed(self):
        status, headers, body = self.request("/api/browser/status-report-content/" + self.identifier)
        self.assertEqual(status, 200)
        self.assertIn(b"Retained report", body)
        self.assertIn("sandbox", headers["Content-Security-Policy"])
        self.assertNotIn("allow-scripts", headers["Content-Security-Policy"])
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertEqual(self.request("/status?embedded=1")[1]["X-Frame-Options"], "SAMEORIGIN")
