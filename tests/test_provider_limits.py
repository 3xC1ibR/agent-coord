from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))
from agent_coord import provider_limits as limits
from agent_coord.store import CoordinationStore, CoordinationError
from agent_coord.ui import _handler


class WindowTests(unittest.TestCase):
    def test_codex_multiple_buckets_duration_and_unknown_values(self):
        data = {"rateLimits": {"primary": {"usedPercent": 99}}, "rateLimitsByLimitId": {
            "codex": {"primary": {"usedPercent": 0, "windowDurationMins": 300, "resetsAt": 1800000000},
                      "secondary": {"usedPercent": None, "windowDurationMins": 10080}},
            "review": {"limitName": "Reviews", "primary": {"usedPercent": 120, "windowDurationMins": 60}}}}
        rows = limits.codex_windows(data)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["used_percent"], 0)
        self.assertIn("5-hour", rows[0]["label"])
        self.assertIsNone(rows[1]["used_percent"])
        self.assertIn("Weekly", rows[1]["label"])
        self.assertEqual(rows[2]["used_percent"], 120)
        self.assertEqual(rows[2]["label"], "Reviews · 1-hour")

    def test_codex_legacy_and_invalid_counts(self):
        for value in (None, True, "42", -1, float("nan"), float("inf")):
            row = limits.codex_windows({"rateLimits": {"primary": {"usedPercent": value}}})[0]
            self.assertIsNone(row["used_percent"])
        self.assertEqual(limits.codex_windows({"rateLimits": {"primary": None}}), [])

    def test_claude_scoped_names_nulls_and_reset_times(self):
        data = {"five_hour": {"utilization": 25, "resets_at": "2026-10-26T18:00:00Z"},
                "seven_day": None, "seven_day_opus": {"utilization": 99}, "limits": [None,
                    {"kind": "session", "percent": 25},
                    {"kind": "weekly_scoped", "scope": None},
                    {"kind": "weekly_scoped", "percent": 0, "scope": {"model": {"display_name": "Fable"}}}]}
        rows = limits.claude_windows(data)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["label"], "Fable · Weekly")
        self.assertEqual(rows[1]["used_percent"], 0)
        self.assertIsInstance(rows[0]["resets_at"], float)
        self.assertIsNone(limits._timestamp("2026-10-26T18:00:00"))
        self.assertIsNone(limits._timestamp("not a time"))

    def test_antigravity_preserves_pools_and_deduplicates_buckets(self):
        rows = limits.antigravity_windows({"groups": [None, {"buckets": [
            {"bucketId": "gemini-5h", "remainingFraction": 1},
            {"bucketId": "3p-weekly", "remainingFraction": 0},
            {"bucketId": "gemini-5h", "remainingFraction": .2},
            {"bucketId": "new", "displayName": "New pool", "remainingFraction": 2},
            {"bucketId": "unknown"}]}]})
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]["used_percent"], 0)
        self.assertEqual(rows[1]["used_percent"], 100)
        self.assertIn("Claude and GPT", rows[1]["label"])
        self.assertIsNone(rows[2]["used_percent"])
        self.assertIsNone(rows[3]["used_percent"])
        with self.assertRaises(limits.QuotaError):
            limits.antigravity_windows({"unexpected": []})


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000
        self.read = Mock(return_value={"windows": [{"id": "five_hour", "used_percent": 35}], "plan": "Pro"})
        self.service = limits.ProviderLimits(None, clock=lambda: self.now, collectors={"claude": self.read})

    def row(self):
        return next(p for p in self.service.snapshot()["providers"] if p["provider"] == "claude")

    def test_cache_is_lazy_coalesces_tabs_and_returns_copies(self):
        self.assertEqual(self.row()["state"], "not_loaded")
        self.read.assert_not_called()
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(self.service.refresh, ["claude"] * 8))
        self.read.assert_called_once()
        self.assertEqual(self.row()["state"], "ready")
        self.row()["windows"].clear()
        self.assertEqual(len(self.row()["windows"]), 1)
        self.now += 120
        self.assertTrue(self.row()["stale"])
        self.service.refresh("claude")
        self.assertEqual(self.read.call_count, 2)

    def test_failed_refresh_preserves_reading_but_never_raw_errors(self):
        self.service.refresh("claude")
        self.now += 121
        self.read.side_effect = RuntimeError("secret-token full upstream response")
        self.service.refresh("claude")
        row = self.row()
        self.assertEqual(row["state"], "error")
        self.assertTrue(row["stale"])
        self.assertEqual(row["observed_at"], 1000)
        self.assertEqual(row["windows"][0]["used_percent"], 35)
        self.assertNotIn("secret-token", json.dumps(self.service.snapshot()))
        self.service.refresh("claude")
        self.assertEqual(self.read.call_count, 2)

    def test_auth_failure_and_account_switch_clear_previous_usage(self):
        self.service._identity("claude", "account-one")
        self.service.refresh("claude")
        self.service._identity("claude", "account-two")
        self.assertEqual(self.row()["windows"], [])
        self.now += 121
        self.service.refresh("claude")
        self.now += 121
        self.read.side_effect = limits.QuotaError("Sign in again", state="sign_in_required")
        self.service.refresh("claude")
        self.assertEqual(self.row()["windows"], [])
        self.assertIsNone(self.row()["observed_at"])

    def test_empty_payload_is_unavailable_and_retry_after_is_respected(self):
        self.read.return_value = {"windows": []}
        self.service.refresh("claude")
        self.assertEqual(self.row()["state"], "unavailable")
        self.now += 61
        self.read.side_effect = limits.QuotaError("Wait", retry_after=600)
        self.service.refresh("claude")
        self.assertEqual(self.row()["next_refresh_at"], self.now + 600)
        with self.assertRaises(CoordinationError):
            self.service.refresh(["claude"])


class CollectorTests(unittest.TestCase):
    def test_claude_file_credentials_match_runtime_ignoring_inherited_api_key(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "file-token"}}))
            with patch.dict(limits.os.environ, {"CLAUDE_CONFIG_DIR": directory, "ANTHROPIC_API_KEY": "inherited"}, clear=True):
                self.assertEqual(limits._claude_credential()["accessToken"], "file-token")
            with patch.dict(limits.os.environ, {"ANTHROPIC_BASE_URL": "https://example.com"}, clear=True):
                with self.assertRaises(limits.QuotaError) as error:
                    limits._claude_credential()
                self.assertEqual(error.exception.state, "unsupported")

    def test_codex_reads_account_and_limits_without_starting_a_thread(self):
        rpc = Mock()
        rpc.request.side_effect = [{"account": {"type": "chatgpt", "email": "test@example.com", "planType": "pro"}},
                                  {"rateLimits": {"primary": {"usedPercent": 12}}}]
        service = limits.ProviderLimits(SimpleNamespace(rpc=rpc))
        row = service.refresh("codex")["providers"][0]
        self.assertEqual(row["state"], "ready")
        self.assertEqual([c.args[0] for c in rpc.request.call_args_list], ["account/read", "account/rateLimits/read"])
        self.assertEqual(row["account"], "test@example.com")

    def test_claude_uses_current_access_token_without_rotating_credentials(self):
        service = limits.ProviderLimits(None)
        with patch.object(limits, "_claude_credential", return_value={"accessToken": "private", "refreshToken": "refresh", "subscriptionType": "max"}), \
             patch.object(limits, "_http_json", return_value={"five_hour": {"utilization": 0}}) as http:
            result = service.refresh("claude")
        self.assertEqual(http.call_count, 1)
        self.assertEqual(http.call_args.args[0], "https://api.anthropic.com/api/oauth/usage")
        self.assertNotIn("private", json.dumps(result))
        self.assertNotIn("refreshToken", json.dumps(result))

    def test_antigravity_uses_profile_project_and_keeps_access_only_in_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "profile/antigravity-acp/acp_token.json"
            path.parent.mkdir(parents=True)
            original = json.dumps({"client_id": "id", "client_secret": "private", "refresh_token": "refresh", "project_id": "project"})
            path.write_text(original)
            service = limits.ProviderLimits(SimpleNamespace(store=None))
            quota = {"groups": [{"buckets": [{"bucketId": "3p-5h", "remainingFraction": .5}]}]}
            with patch.object(limits, "provider_directory", return_value=root), patch.object(limits, "_http_json", side_effect=[
                {"access_token": "private-access", "expires_in": 3600}, quota, quota]) as http:
                first = service._antigravity()
                service._antigravity()
            self.assertEqual(http.call_count, 3)
            self.assertEqual(http.call_args_list[1].kwargs["body"], {"project": "project"})
            self.assertEqual(path.read_text(), original)
            self.assertEqual(first["windows"][0]["used_percent"], 50)
            self.assertNotIn("private", json.dumps(first))

    def test_claude_expired_credentials_do_not_make_network_requests(self):
        service = limits.ProviderLimits(None, clock=lambda: 100)
        with patch.object(limits, "_claude_credential", return_value={"accessToken": "private", "expiresAt": 99000}), \
             patch.object(limits, "_http_json") as http:
            result = service.refresh("claude")
        http.assert_not_called()
        self.assertEqual(result["providers"][1]["state"], "sign_in_required")


class UsageHTTPTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = CoordinationStore(Path(temp.name) / "state.sqlite3")
        self.sessions = SimpleNamespace(store=self.store, cwd=temp.name)
        self.collect = Mock(return_value={"windows": [{"id": "5h", "label": "5-hour", "used_percent": 20, "resets_at": None}]})
        self.limits = limits.ProviderLimits(self.sessions, collectors={"claude": self.collect})
        with patch("agent_coord.ui.ProviderLimits", return_value=self.limits):
            self.server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(self.store, None, None, self.sessions, "test-csrf"))
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self.stop)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(2)

    def request(self, path, body=None, headers=None):
        headers = {"Content-Type": "application/json", "X-Agent-Coord-Token": "test-csrf", **(headers or {})}
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode() if body is not None else None, headers=headers)
        try:
            response = urllib.request.urlopen(req, timeout=2)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.status, response.headers, response.read().decode()

    def test_page_assets_and_framing(self):
        for path in ("/usage", "/provider-usage.js", "/provider-usage.css"):
            code, _, body = self.request(path)
            self.assertEqual(code, 200)
            self.assertTrue(body)
        self.assertEqual(self.request("/usage")[1]["X-Frame-Options"], "DENY")
        self.assertEqual(self.request("/usage?embedded=1")[1]["X-Frame-Options"], "SAMEORIGIN")
        self.assertEqual(self.request("/usage?embedded=1", headers={"Host": "attacker.example"})[0], 403)

    def test_snapshot_is_passive_refresh_requires_csrf_and_known_provider(self):
        self.assertEqual(self.request("/api/browser/provider-usage")[0], 200)
        self.collect.assert_not_called()
        path = "/api/browser/provider-usage/refresh"
        self.assertEqual(self.request(path, {"provider": "claude"}, {"X-Agent-Coord-Token": ""})[0], 403)
        self.collect.assert_not_called()
        self.assertEqual(self.request(path, {"provider": "bad"})[0], 400)
        self.assertEqual(self.request(path, {"provider": "claude", "url": "https://evil.test"})[0], 400)
        code, _, body = self.request(path, {"provider": "claude"})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["providers"][1]["state"], "ready")
        self.collect.assert_called_once()

    def test_http_collector_refuses_redirects_and_sanitizes_errors(self):
        received = []
        class Redirect(BaseHTTPRequestHandler):
            def do_GET(self):
                received.append(self.path)
                self.send_response(302)
                self.send_header("Location", "/secret-target")
                self.end_headers()
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with self.assertRaises(limits.QuotaError):
                limits._http_json(f"http://127.0.0.1:{server.server_port}/source", headers={"Authorization": "Bearer secret"})
            self.assertEqual(received, ["/source"])
        finally:
            server.shutdown(); server.server_close(); worker.join(2)

    @unittest.skipUnless(shutil.which("node"), "Node is required")
    def test_usage_browser_behavior(self):
        result = subprocess.run(["node", "--test", str(Path(__file__).with_name("test_web_provider_usage.js"))], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
