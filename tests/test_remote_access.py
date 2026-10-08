from __future__ import annotations

import copy
import http.client
import json
import os
from pathlib import Path
import subprocess
import shutil
import struct
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))
from agent_coord.remote_access import RemoteAccess, Tailscale, COOKIE_NAME
from agent_coord.store import CoordinationError


class FakeTailscale:
    def __init__(self):
        self.mapping = {}
        self.calls = []

    def hostname(self):
        return "mac.test.ts.net"

    def config(self):
        return copy.deepcopy(self.mapping)

    def enable(self, port, target):
        self.calls.append(("enable", port, target))
        self.mapping.setdefault("TCP", {})[str(port)] = {"HTTPS": True}
        self.mapping.setdefault("Web", {})[f"mac.test.ts.net:{port}"] = {"Handlers": {"/": {"Proxy": target}}}

    def disable(self, port):
        self.calls.append(("disable", port))
        self.mapping.get("TCP", {}).pop(str(port), None)
        self.mapping.get("Web", {}).pop(f"mac.test.ts.net:{port}", None)


class RemoteAccessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.ts = FakeTailscale()
        self.now = 1000
        self.remote = self.new_remote()

    def new_remote(self):
        remote = RemoteAccess(self.path, tailscale=self.ts, clock=lambda: self.now)
        remote.target = "http://127.0.0.1:9876"
        self.addCleanup(remote.close)
        return remote

    def pair(self):
        self.remote.enable()
        link = self.remote.pairing()["url"]
        cookie = self.remote.redeem(link.split("#pair=")[1], "My phone")
        return cookie.split(";")[0]

    def test_private_route_and_secure_one_time_pairing(self):
        self.remote.enable()
        token = self.remote.pairing()["url"].split("#pair=")[1]
        cookie = self.remote.redeem(token, "My phone")
        self.assertIn("Secure; HttpOnly; SameSite=Strict", cookie)
        self.assertTrue(self.remote.authenticated(cookie.split(";")[0]))
        self.assertFalse(self.remote.authenticated(f"{COOKIE_NAME}=wrong"))
        with self.assertRaises(CoordinationError):
            self.remote.redeem(token, "Other phone")
        saved = self.remote.path.read_text()
        self.assertNotIn(token, saved)
        self.assertNotIn(cookie.split(";")[0].split("=")[1], saved)
        self.assertEqual(self.remote.path.stat().st_mode & 0o777, 0o600)

    def test_expired_pair_and_superseded_invitation(self):
        self.remote.enable()
        old = self.remote.pairing()["url"].split("#pair=")[1]
        new = self.remote.pairing()["url"].split("#pair=")[1]
        with self.assertRaises(CoordinationError):
            self.remote.redeem(old, "phone")
        self.now += 301
        with self.assertRaises(CoordinationError):
            self.remote.redeem(new, "phone")

    def test_revoke_and_expiry_deny_existing_cookie(self):
        cookie = self.pair()
        device = self.remote.status()["devices"][0]
        self.remote.revoke(device["id"])
        self.assertFalse(self.remote.authenticated(cookie))
        cookie = self.pair()
        self.now += 30 * 24 * 3600
        self.assertFalse(self.remote.authenticated(cookie))

    def test_restart_updates_backend_port_and_preserves_grant(self):
        cookie = self.pair()
        self.remote.close()
        next_remote = self.new_remote()
        next_remote.restore("http://127.0.0.1:9999")
        self.assertTrue(next_remote.authenticated(cookie))
        self.assertEqual(self.ts.calls[-1], ("enable", 443, "http://127.0.0.1:9999"))

    def test_two_servers_cannot_steal_route_or_revoke_grants(self):
        cookie = self.pair()
        other = self.new_remote()
        with self.assertRaisesRegex(CoordinationError, "another Ribbon Field"):
            other.enable()
        with self.assertRaises(CoordinationError):
            other.disable()
        self.assertTrue(self.remote.authenticated(cookie))

    def test_disable_revokes_and_does_not_restore(self):
        cookie = self.pair()
        self.remote.disable()
        self.assertFalse(self.remote.authenticated(cookie))
        other = self.new_remote()
        other.restore("http://127.0.0.1:9999")
        self.assertFalse(other.status()["enabled"])

    def test_unrelated_route_is_never_overwritten(self):
        self.ts.enable(443, "http://127.0.0.1:1234")
        with self.assertRaisesRegex(CoordinationError, "already used"):
            self.remote.enable()
        self.assertEqual(len(self.ts.calls), 1)
        self.remote.enable(8443)
        self.remote.disable()
        self.assertIn("443", self.ts.mapping["TCP"])

    def test_modified_route_not_removed_on_disable(self):
        cookie = self.pair()
        self.ts.enable(443, "http://127.0.0.1:1234")
        result = self.remote.disable()
        self.assertIn("left it untouched", result["error"])
        self.assertFalse(self.remote.authenticated(cookie))
        self.assertEqual(self.ts.calls[-1][0], "enable")

    def test_existing_funnel_is_rejected_even_for_saved_route(self):
        self.remote.enable()
        self.ts.mapping["AllowFunnel"] = {"mac.test.ts.net:443": True}
        with self.assertRaisesRegex(CoordinationError, "Funnel"):
            self.remote.enable()
        self.assertFalse(self.remote.status()["enabled"])

    def test_foreground_routes_are_not_overwritten(self):
        self.ts.mapping = {"Foreground": {"other": {"TCP": {"443": {"HTTPS": True}}}}}
        with self.assertRaises(CoordinationError):
            self.remote.enable()

    def test_invalid_ports_do_not_run_commands(self):
        for port in (True, 0, -1, 65536, "443"):
            with self.subTest(port=port), self.assertRaises(CoordinationError):
                self.remote.enable(port)
        self.assertEqual(self.ts.calls, [])

    def test_failed_restore_is_visible_locally(self):
        self.remote.enable()
        self.remote.close()
        other = self.new_remote()
        with patch.object(self.ts, "hostname", side_effect=CoordinationError("Disconnected")):
            other.restore("http://127.0.0.1:9999")
        self.assertEqual(other.status()["error"], "Disconnected")
        self.assertFalse(other.status()["enabled"])


class TailscaleTests(unittest.TestCase):
    def test_desktop_launch_forces_cli_mode_without_changing_parent_environment(self):
        ts = Tailscale()
        desktop_environment = {"PATH": "/usr/bin:/bin", "TAILSCALE_BE_CLI": "0"}
        with patch.dict(os.environ, desktop_environment, clear=True), \
                patch.object(ts, "executable", return_value="/Applications/Tailscale.app/Contents/MacOS/Tailscale"), \
                patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, '{"BackendState":"Running"}', "")
            self.assertEqual(ts.json("status", "--json"), {"BackendState": "Running"})
            self.assertEqual(run.call_args.kwargs["env"],
                             dict(desktop_environment, TAILSCALE_BE_CLI="1"))
            self.assertEqual(dict(os.environ), desktop_environment)

    def test_status_normalizes_and_rejects_non_tailnet_names(self):
        ts = Tailscale()
        with patch.object(ts, "json", return_value={"BackendState": "Running", "Self": {"DNSName": "Mac.test.ts.net."}}):
            self.assertEqual(ts.hostname(), "mac.test.ts.net")
        for name in ("evil.example", "mac.ts.net/path", "user@mac.ts.net", "mac.ts.net:443", "a..ts.net"):
            with patch.object(ts, "json", return_value={"BackendState": "Running", "Self": {"DNSName": name}}):
                with self.assertRaises(CoordinationError):
                    ts.hostname()

    def test_commands_are_bounded_and_do_not_leak_output(self):
        ts = Tailscale()
        with patch.object(ts, "executable", return_value="/bin/tailscale"), patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 1, "tskey-secret", "auth tskey-secret")
            with self.assertRaises(CoordinationError) as error:
                ts.enable(443, "http://127.0.0.1:9876")
            self.assertNotIn("secret", str(error.exception))
            self.assertEqual(run.call_args.args[0], ["/bin/tailscale", "serve", "--bg", "--https=443", "http://127.0.0.1:9876"])
            self.assertEqual(run.call_args.kwargs["timeout"], 15)

    def test_timeout_and_malformed_json_are_actionable(self):
        ts = Tailscale()
        with patch.object(ts, "executable", return_value="tailscale"), patch("subprocess.run", side_effect=subprocess.TimeoutExpired("tailscale", 5)):
            with self.assertRaisesRegex(CoordinationError, "timed out"):
                ts.config()
        with patch.object(ts, "run", return_value="[]"):
            with self.assertRaisesRegex(CoordinationError, "invalid status"):
                ts.config()


class RemoteHTTPTests(unittest.TestCase):
    def setUp(self):
        from agent_coord.store import CoordinationStore
        from agent_coord.ui import make_ui_server
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.remote = RemoteAccess(self.path, tailscale=FakeTailscale())
        self.server = make_ui_server(CoordinationStore(self.path), port=0, remote_access=self.remote)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01})
        self.thread.start()
        self.host = f"127.0.0.1:{self.server.server_port}"
        self.remote.enable()
        self.authority = "mac.test.ts.net"
        self.origin = "https://" + self.authority
        _, _, body = self.request("GET", "/api/browser/config", local=True)
        self.csrf = json.loads(body)["token"]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp.cleanup()

    def request(self, method, path, body=None, *, local=False, cookie=None, origin=None, csrf=None, extra=None):
        headers = {"Host": self.host if local else self.authority}
        if cookie: headers["Cookie"] = cookie
        if origin: headers["Origin"] = origin
        if csrf: headers["X-Agent-Coord-Token"] = csrf
        if body is not None: headers["Content-Type"] = "application/json"
        headers.update(extra or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request(method, path, json.dumps(body) if body is not None else None, headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def pair(self):
        link = self.remote.pairing()["url"]
        status, headers, _ = self.request("POST", "/api/remote/pair", {"token": link.split("#pair=")[1], "name": "iPhone"}, origin=self.origin)
        self.assertEqual(status, 200)
        return headers["Set-Cookie"].split(";")[0]

    def test_all_remote_reads_require_pairing_and_local_ui_remains_available(self):
        for path in ("/api/browser/config", "/api/browser/threads", "/api/snapshot", "/api/browser/events", "/monitor", "/app.js"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0], 401)
        status, _, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Pair this device", body)
        self.assertEqual(self.request("GET", "/api/browser/config", local=True)[0], 200)

    def test_home_screen_assets_are_public_but_host_gated(self):
        status, headers, body = self.request("GET", "/manifest.webmanifest")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/manifest+json")
        manifest = json.loads(body)
        self.assertEqual(manifest["display"], "standalone")
        self.assertEqual(manifest["id"], "/")
        self.assertEqual(manifest["start_url"], "/")
        self.assertEqual(manifest["scope"], "/")
        icons = [(icon["src"], tuple(map(int, icon["sizes"].split("x")))) for icon in manifest["icons"]]
        icons.append(("/app-icons/apple-touch-icon.png", (180, 180)))
        for path, dimensions in icons:
            with self.subTest(path=path):
                status, headers, data = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertEqual(headers["Content-Type"], "image/png")
                self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
                self.assertEqual(struct.unpack(">II", data[16:24]), dimensions)
                self.assertEqual(self.request("GET", path, extra={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.request("GET", "/manifest.webmanifest", extra={"Host": "evil.example"})[0], 403)
        for path in ("/app-icons/../app.js", "/app-icons/missing.png", "/app.js", "/api/browser/config"):
            self.assertEqual(self.request("GET", path)[0], 401)

    def test_home_screen_metadata_on_paired_and_unpaired_launch(self):
        cookie = self.pair()
        for options in ({}, {"cookie": cookie}, {"local": True}):
            with self.subTest(options=options):
                status, _, body = self.request("GET", "/", **options)
                self.assertEqual(status, 200)
                self.assertIn(b'rel="manifest" href="/manifest.webmanifest"', body)
                self.assertIn(b'name="apple-mobile-web-app-capable" content="yes"', body)
                self.assertIn(b'rel="apple-touch-icon"', body)

    def test_pairing_requires_exact_https_origin_and_cannot_be_replayed(self):
        body = {"token": self.remote.pairing()["url"].split("#pair=")[1], "name": "iPhone"}
        for origin in (None, "https://evil.example", "http://mac.test.ts.net", "null"):
            self.assertEqual(self.request("POST", "/api/remote/pair", body, origin=origin)[0], 403)
        status, headers, _ = self.request("POST", "/api/remote/pair", body, origin=self.origin)
        self.assertEqual(status, 200)
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertEqual(self.request("POST", "/api/remote/pair", body, origin=self.origin)[0], 400)

    def test_paired_device_reads_and_mutates_with_csrf(self):
        cookie = self.pair()
        status, _, body = self.request("GET", "/api/browser/config", cookie=cookie)
        self.assertEqual(status, 200)
        config = json.loads(body)
        self.assertTrue(config["remote"])
        self.assertEqual(config["token"], self.csrf)
        status, _, _ = self.request("POST", "/api/browser/projects", {"name": "Phone project"}, cookie=cookie, origin=self.origin, csrf=self.csrf)
        self.assertEqual(status, 201)
        self.assertEqual(self.request("POST", "/api/browser/projects", {"name": "Denied"}, cookie=cookie, origin=self.origin)[0], 403)

    def test_remote_cannot_administer_even_when_paired(self):
        cookie = self.pair()
        self.assertEqual(self.request("GET", "/api/remote/status", cookie=cookie)[0], 403)
        for action in ("enable", "disable", "pairing", "revoke"):
            self.assertEqual(self.request("POST", "/api/remote/" + action, {}, cookie=cookie, origin=self.origin, csrf=self.csrf)[0], 403)

    def test_forwarded_headers_and_invalid_hosts_never_grant_local_access(self):
        for header in ("Forwarded", "X-Forwarded-Host", "X-Forwarded-Proto", "Tailscale-User-Login"):
            self.assertEqual(self.request("GET", "/api/browser/config", local=True, extra={header: "spoofed"})[0], 403)
        self.assertEqual(self.request("GET", "/api/browser/config", extra={"Host": "evil.example"})[0], 403)

    def test_remote_writes_reject_foreign_missing_or_cross_site_origin(self):
        cookie = self.pair()
        for origin in (None, "https://evil.example", "http://" + self.host):
            self.assertEqual(self.request("POST", "/api/browser/projects", {"name": "No"}, cookie=cookie, origin=origin, csrf=self.csrf)[0], 403)
        self.assertEqual(self.request("POST", "/api/browser/projects", {"name": "No"}, cookie=cookie, origin=self.origin, csrf=self.csrf, extra={"Sec-Fetch-Site": "cross-site"})[0], 403)

    def test_revocation_blocks_subsequent_requests_and_pair_page_is_always_available(self):
        cookie = self.pair()
        self.assertIn(b"Pair this device", self.request("GET", "/pair", cookie=cookie)[2])
        self.remote.revoke(self.remote.status()["devices"][0]["id"])
        self.assertEqual(self.request("GET", "/api/browser/config", cookie=cookie)[0], 401)

    def test_local_administration_requires_existing_csrf(self):
        self.assertEqual(self.request("POST", "/api/remote/pairing", {}, local=True)[0], 403)
        status, _, body = self.request("POST", "/api/remote/pairing", {}, local=True, csrf=self.csrf)
        self.assertEqual(status, 200)
        self.assertIn("#pair=", json.loads(body)["url"])
        self.assertEqual(self.request("GET", "/api/remote/status", local=True)[0], 200)

    def test_revocation_terminates_existing_event_stream(self):
        cookie = self.pair()
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=4)
        try:
            connection.request("GET", "/api/browser/events", headers={"Host": self.authority, "Cookie": cookie})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.readline(), b"retry: 1500\n")
            self.assertEqual(response.readline(), b"\n")
            self.remote.revoke(self.remote.status()["devices"][0]["id"])
            self.assertEqual(response.read(), b"")
        finally:
            connection.close()


class RemoteBrowserTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is required for browser tests")
    def test_remote_access_controls(self):
        result = subprocess.run(["node", "--test", str(Path(__file__).with_name("test_web_remote_access.js")),
                                 str(Path(__file__).with_name("test_web_pairing.js"))],
                                text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_cli_tailscale_alias_and_port(self):
        from agent_coord.cli import _parser
        for flag in ("--tailscale", "--tailscale-serve"):
            args = _parser().parse_args(["ui", flag, "--tailscale-port", "8443"])
            self.assertTrue(args.tailscale)
            self.assertEqual(args.tailscale_port, 8443)
