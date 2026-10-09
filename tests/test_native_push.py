from __future__ import annotations

import base64
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))
from agent_coord.native_push import APNsTransport, NativePush, validate_registration
from agent_coord.store import CoordinationError
import test_web_push as web
import test_remote_access as remote_tests


def registration(token="ab" * 32, environment="development"):
    return {"token": token, "environment": environment}


class NativePushTests(unittest.TestCase):
    def setUp(self):
        self.fixture = web.WebPushTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        self.push = NativePush(f.store, f.sessions, f.remote, transport=f.transport, clock=lambda: f.now)
        self.addCleanup(self.push.close)

    def test_registration_validation(self):
        self.assertEqual(validate_registration(registration("AB" * 32))["token"], "ab" * 32)
        for value in [None, {}, registration("abc"), registration("xz" * 32), registration(environment="sandbox"),
                      {**registration(), "topic": "other"}, registration("ab" * 257), registration("a" * 33)]:
            with self.subTest(value=value), self.assertRaises(CoordinationError):
                validate_registration(value)

    def test_delivery_retry_dedupe_and_rotation(self):
        f = self.fixture
        f.complete("before")
        self.push.subscribe(f.device, registration())
        f.complete("after")
        f.transport.response = (503, "30")
        self.push.tick()
        self.assertEqual(len(f.transport.sent), 1)
        self.assertEqual(f.transport.sent[0]["subscription"], registration())
        restarted = NativePush(f.store, f.sessions, f.remote, transport=f.transport, clock=lambda: f.now)
        self.addCleanup(restarted.close)
        restarted.subscribe(f.device, registration())
        f.now += 31
        f.transport.response = (200, None)
        restarted.tick()
        restarted.tick()
        self.assertEqual(len(f.transport.sent), 2)
        self.assertTrue(restarted.status(f.device)["enabled"])
        restarted.subscribe(f.device, registration("cd" * 32))
        restarted.tick()
        self.assertEqual(len(f.transport.sent), 2)
        f.complete("next")
        restarted.tick()
        self.assertEqual(f.transport.sent[-1]["subscription"]["token"], "cd" * 32)
        self.assertEqual(self.push.path.stat().st_mode & 0o777, 0o600)
        self.assertNotEqual(self.push.path, f.push.path)

    def test_device_revocation_and_unsubscribe_stop_pending_notifications(self):
        f = self.fixture
        for action in ("revoke", "unsubscribe", "expire"):
            device = f.pair()
            self.push.subscribe(device, registration())
            self.push.test(device)
            if action == "revoke": f.remote.revoke(device)
            elif action == "unsubscribe": self.push.unsubscribe(device)
            else: f.now += 31 * 86400
            self.push.tick()
        self.assertEqual(f.transport.sent, [])

    def test_token_ownership_and_repair_after_revocation(self):
        f = self.fixture
        self.push.subscribe(f.device, registration())
        other = f.pair()
        with self.assertRaises(CoordinationError): self.push.subscribe(other, registration())
        f.remote.revoke(f.device)
        self.push.subscribe(other, registration())
        self.assertTrue(self.push.status(other)["enabled"])

    def test_approval_and_invalid_token(self):
        f = self.fixture
        self.push.subscribe(f.device, registration())
        f.approval()
        f.transport.response = (410, None)
        self.push.tick()
        self.assertEqual(len(f.transport.sent), 1)
        self.assertIn("approval", f.transport.sent[0]["payload"]["title"])
        self.assertFalse(self.push.status(f.device)["enabled"])


class NativePushHTTPTests(unittest.TestCase):
    def setUp(self):
        self.transport = web.FakeTransport()
        self.patcher = patch("agent_coord.native_push.APNsTransport", return_value=self.transport)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.fixture = remote_tests.RemoteHTTPTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def test_native_routes_require_pairing_origin_csrf_and_strict_fields(self):
        f = self.fixture
        route = "/api/browser/native-push/"
        self.assertEqual(f.request("GET", route + "status")[0], 401)
        self.assertEqual(f.request("GET", route + "status", local=True)[0], 403)
        cookie = f.pair()
        self.assertEqual(f.request("GET", route + "status", cookie=cookie)[0], 200)
        body = {"registration": registration()}
        for origin, csrf in ((f.origin, None), ("https://evil.test", f.csrf)):
            self.assertEqual(f.request("POST", route + "subscribe", body, cookie=cookie, origin=origin, csrf=csrf)[0], 403)
        status, _, data = f.request("POST", route + "subscribe", body, cookie=cookie, origin=f.origin, csrf=f.csrf)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(data)["enabled"])
        self.assertNotIn(registration()["token"], data.decode())
        self.assertEqual(f.request("POST", route + "subscribe", {**body, "device":"other"}, cookie=cookie, origin=f.origin, csrf=f.csrf)[0], 400)
        self.assertEqual(f.request("POST", route + "test", {}, cookie=cookie, origin=f.origin, csrf=f.csrf)[0], 200)
        status, _, data = f.request("POST", route + "unsubscribe", {}, cookie=cookie, origin=f.origin, csrf=f.csrf)
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(data)["enabled"])
        f.remote.revoke(f.remote.device_id(cookie))
        self.assertEqual(f.request("GET", route + "status", cookie=cookie)[0], 401)


class APNsTransportTests(unittest.TestCase):
    def setUp(self):
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric import ec
        except ImportError:
            self.skipTest("Optional desktop cryptography package required")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.key = ec.generate_private_key(ec.SECP256R1())
        key_path = self.root / "key.p8"
        key_path.write_bytes(self.key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        key_path.chmod(0o600)
        self.config = self.root / "apns.json"
        self.config.write_text(json.dumps({"team_id":"ABCDEFGHIJ", "key_id":"0123456789", "topic":"com.agentcoord.ios", "key_path":str(key_path)}))
        self.config.chmod(0o600)
        self.calls = []
        self.now = 10000
        self.response = 'HTTP/2 200\r\napns-id: example\r\n\r\n\n200'
        def run(args, **kwargs):
            if "--version" in args: return subprocess.CompletedProcess(args, 0, "Features: HTTP2", "")
            self.calls.append((args, kwargs))
            return subprocess.CompletedProcess(args, 0, self.response, "")
        self.transport = APNsTransport(self.config, runner=run, clock=lambda: self.now)
        self.assertTrue(self.transport.available)
        self.payload = {"title":"Ribbon Field", "body":"Approval needed", "url":"/#thread-one", "tag":"agent-coord-one"}

    def test_signed_payload_fixed_hosts_no_redirects_and_no_process_argument_secrets(self):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec, utils
        self.assertEqual(self.transport.send(registration(), self.payload, None, "https://mac.tail.ts.net", 30), (200, None))
        args, kwargs = self.calls[-1]
        self.assertNotIn(registration()["token"], " ".join(args))
        self.assertEqual(args[1], "--disable")
        self.assertNotIn("--location", args)
        self.assertIn("--http2", args)
        self.assertIn("--proxy", args)
        self.assertLessEqual(kwargs["timeout"], 15)
        lines = kwargs["input"].splitlines()
        values = [(line.split(" = ",1)[0], json.loads(line.split(" = ",1)[1])) for line in lines]
        self.assertEqual(values[0][1], "https://api.sandbox.push.apple.com/3/device/" + registration()["token"])
        headers = [value for name,value in values if name == "header"]
        self.assertIn("apns-topic: com.agentcoord.ios", headers)
        self.assertIn("apns-push-type: alert", headers)
        self.assertIn("apns-expiration: 10030", headers)
        jwt = headers[0].removeprefix("authorization: bearer ")
        decode = lambda value: base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        header, claims, signature = jwt.split(".")
        self.assertEqual(json.loads(decode(header)), {"alg":"ES256", "kid":"0123456789"})
        self.assertEqual(json.loads(decode(claims)), {"iss":"ABCDEFGHIJ", "iat":10000})
        raw = decode(signature)
        der = utils.encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
        self.key.public_key().verify(der, (header + "." + claims).encode(), ec.ECDSA(hashes.SHA256()))
        payload = json.loads(next(value for name,value in values if name == "data-binary"))
        self.assertEqual(payload["agentcoord"], {"origin":"https://mac.tail.ts.net", "url":"/#thread-one"})
        self.assertNotIn("content-available", payload["aps"])
        self.transport.send(registration(environment="production"), self.payload, None, "https://mac.tail.ts.net", 30)
        self.assertIn("https://api.push.apple.com/", self.calls[-1][1]["input"])

    def test_jwt_cache_errors_and_private_key_permissions(self):
        old = self.transport._authorization()
        self.now += 1200
        self.assertEqual(self.transport._authorization(), old)
        self.now += 1801
        self.assertNotEqual(self.transport._authorization(), old)
        for code, reason, expected in [(400,"BadDeviceToken",410), (400,"DeviceTokenNotForTopic",410), (410,"Unregistered",410), (403,"ExpiredProviderToken",503), (429,"TooManyRequests",429), (403,"InvalidProviderToken",403)]:
            self.response = f'HTTP/2 {code}\r\nretry-after: 60\r\n\r\n{{"reason":"{reason}"}}\n{code}'
            self.assertEqual(self.transport.send(registration(), self.payload, None, "https://mac.tail.ts.net", 30)[0], expected)
        self.config.chmod(0o644)
        invalid = APNsTransport(self.config)
        self.assertFalse(invalid.available)
        self.assertNotIn(str(self.root), invalid.error)

    def test_oversize_and_bad_configuration_fail_closed(self):
        self.assertEqual(self.transport.send(registration(), {**self.payload,"body":"x" * 4096}, None, "https://mac.tail.ts.net", 30)[0], 413)
        self.assertEqual(self.calls, [])
        self.response = 'HTTP/2 000\r\n\r\n\n000'
        self.assertEqual(self.transport.send(registration(), self.payload, None, "https://mac.tail.ts.net", 30)[0], 503)
        from cryptography.exceptions import UnsupportedAlgorithm
        with patch("cryptography.hazmat.primitives.serialization.load_pem_private_key", side_effect=UnsupportedAlgorithm("unsupported")):
            self.assertFalse(APNsTransport(self.config).available)
        self.config.write_text("{}")
        self.assertFalse(APNsTransport(self.config).available)
