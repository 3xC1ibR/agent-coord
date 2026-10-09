from __future__ import annotations

import base64
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))

from agent_coord.codex_app_server import BrowserSessions
from agent_coord.hook import handle
from agent_coord.remote_access import RemoteAccess
from agent_coord.store import CoordinationError, CoordinationStore
from agent_coord.web_push import WebPush, PushTransport, validate_subscription
from test_codex_app_server import FakeCodex
from test_remote_access import FakeTailscale, RemoteHTTPTests


def subscription(suffix="one"):
    return {"endpoint": "https://web.push.apple.com/" + suffix,
            "keys": {"auth": base64.urlsafe_b64encode(b"a" * 16).decode().rstrip("="),
                     "p256dh": base64.urlsafe_b64encode(b"\x04" + b"b" * 64).decode().rstrip("=")}}


class FakeTransport:
    available = True

    def __init__(self):
        self.sent = []
        self.response = (201, None)

    def generate_keys(self):
        return "private-test-key", "public-test-key"

    def validate_key(self, value):
        pass

    def send(self, subscription, payload, private, origin, ttl):
        self.sent.append({"subscription": subscription, "payload": payload, "ttl": ttl})
        return self.response


class WebPushTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 1000.0
        self.store = CoordinationStore(Path(self.temp.name) / "coord.sqlite3", clock=lambda: self.now)
        self.sessions = BrowserSessions(self.store, self.temp.name, rpc_factory=FakeCodex)
        self.addCleanup(self.sessions.close)
        self.remote = RemoteAccess(self.store.database_path, tailscale=FakeTailscale(), clock=lambda: self.now)
        self.remote.target = "http://127.0.0.1:9876"
        self.remote.enable()
        self.addCleanup(self.remote.close)
        self.transport = FakeTransport()
        self.push = self.manager()
        self.device = self.pair()

    def manager(self):
        push = WebPush(self.store, self.sessions, self.remote, transport=self.transport, clock=lambda: self.now)
        self.addCleanup(push.close)
        return push

    def pair(self):
        token = self.remote.pairing()["url"].split("#pair=")[1]
        cookie = self.remote.redeem(token, "Test phone")
        return self.remote.device_id(cookie)

    def complete(self, turn="one", status="completed"):
        with patch.dict("os.environ", {"AGENT_COORD_CLIENT": "codex", "AGENT_COORD_ZELLIJ_WAKE": "0"}):
            handle({"session_id": "terminal", "cwd": self.temp.name, "hook_event_name": "UserPromptSubmit",
                    "prompt": "Secret project content", "turn_id": turn}, self.store)
        self.store.threads.finish_turn("terminal", turn_id=turn, status=status)

    def approval(self):
        thread = self.sessions.create({"name": "Secret title"})["session"]["thread_id"]
        self.sessions._event({"id": 42, "method": "item/commandExecution/requestApproval", "params": {"threadId": thread}})
        return thread, next(reversed(self.sessions.requests))

    def test_no_historical_delivery_and_each_phone_is_independent_of_desktop(self):
        self.complete("old")
        self.push.subscribe(self.device, subscription())
        other = self.pair()
        self.push.subscribe(other, subscription("two"))
        self.complete("new")
        self.store.threads.claim_notification(self.store.threads.completion_cursor())
        self.push.tick()
        self.assertEqual(len(self.transport.sent), 2)
        for item in self.transport.sent:
            self.assertEqual(item["payload"]["title"], "Ribbon Field · Turn finished")
            self.assertEqual(item["payload"]["body"], "Secret project content")
            self.assertEqual(item["payload"]["url"], "/#terminal")
        self.push.tick()
        self.assertEqual(len(self.transport.sent), 2)
        self.assertEqual(self.push.path.stat().st_mode & 0o777, 0o600)

    def test_retry_survives_restart_and_resubscribe_without_replay(self):
        self.push.subscribe(self.device, subscription())
        self.complete(status="failed")
        self.transport.response = (503, "60")
        self.push.tick()
        self.assertEqual(len(self.transport.sent), 1)
        restarted = self.manager()
        restarted.subscribe(self.device, subscription())
        self.now += 30
        restarted.tick()
        self.assertEqual(len(self.transport.sent), 1)
        self.transport.response = (201, None)
        self.now += 31
        restarted.tick()
        self.assertEqual(len(self.transport.sent), 2)
        self.assertEqual(self.transport.sent[-1]["payload"]["title"], "Ribbon Field · Turn failed")
        self.assertEqual(self.transport.sent[-1]["payload"]["body"], "Secret project content")
        restarted.tick()
        self.assertEqual(len(self.transport.sent), 2)

    def test_revocation_expiry_and_unsubscribe_discard_pending_sends(self):
        for action in ("revoke", "expire", "unsubscribe", "disable"):
            with self.subTest(action=action):
                if not self.remote.origin:
                    self.remote.enable()
                device = self.pair()
                self.push.subscribe(device, subscription(action))
                self.push.test(device)
                if action == "revoke": self.remote.revoke(device)
                elif action == "expire": self.now += 31 * 86400
                elif action == "unsubscribe": self.push.unsubscribe(device)
                else: self.remote.disable()
                self.push.tick()
        self.assertEqual(self.transport.sent, [])

    def test_approval_still_pushes_after_desktop_claim_and_stops_after_answer(self):
        self.push.subscribe(self.device, subscription())
        thread, key = self.approval()
        self.sessions.claim_approval_notification(key)
        self.transport.response = (429, "30")
        self.push.tick()
        self.assertEqual(len(self.transport.sent), 1)
        self.assertEqual(self.transport.sent[0]["payload"]["title"], "Ribbon Field · Codex is requesting approval")
        self.assertEqual(self.transport.sent[0]["payload"]["body"], "Secret title")
        self.assertEqual(self.transport.sent[0]["payload"]["url"], "/#" + thread)
        self.sessions.answer(thread, key, {"decision": "decline"})
        self.now += 31
        self.push.tick()
        self.assertEqual(len(self.transport.sent), 1)

    def test_approval_ttl_does_not_create_repeated_notifications(self):
        self.push.subscribe(self.device, subscription())
        self.approval()
        self.push.tick()
        self.now += 2 * 86400
        self.push.tick()
        self.assertEqual(len(self.transport.sent), 1)

    def test_gone_subscription_is_removed_and_permanent_error_does_not_retry(self):
        for status in (404, 410, 403):
            with self.subTest(status=status):
                self.push.subscribe(self.device, subscription())
                self.push.test(self.device)
                self.transport.response = (status, None)
                self.push.tick()
                count = len(self.transport.sent)
                self.now += 30
                self.push.tick()
                self.assertEqual(len(self.transport.sent), count)
                self.assertEqual(self.push.status(self.device)["enabled"], status == 403)

    def test_subscription_cannot_be_taken_over_by_another_paired_device(self):
        self.push.subscribe(self.device, subscription())
        other = self.pair()
        with self.assertRaises(CoordinationError):
            self.push.subscribe(other, subscription())
        self.remote.revoke(self.device)
        self.push.subscribe(other, subscription())

    def test_interrupted_and_archived_turns_are_not_sent(self):
        self.push.subscribe(self.device, subscription())
        self.complete(status="interrupted")
        self.complete("closed")
        self.store.threads.update("terminal", attention="archived")
        self.push.tick()
        self.assertEqual(self.transport.sent, [])

    def test_provider_allowlist_and_encryption_key_validation(self):
        for endpoint in ("http://web.push.apple.com/a", "https://127.0.0.1/a", "https://example.com/a",
                         "https://push.apple.com.evil.test/a", "https://web.push.apple.com:8443/a",
                         "https://user:pass@web.push.apple.com/a", "https://web.push.apple.com/a#frag"):
            with self.subTest(endpoint=endpoint), self.assertRaises(CoordinationError):
                validate_subscription({**subscription(), "endpoint": endpoint})
        for key in ("auth", "p256dh"):
            value = subscription()
            value["keys"][key] = "bad"
            with self.assertRaises(CoordinationError): validate_subscription(value)

    def test_notification_context_uses_project_then_repository_and_bounds_metadata(self):
        for context, title in (
            ({"project_name": "Launch", "repository_name": "repo"}, "Launch · Turn finished"),
            ({"repository_name": "repo"}, "repo · Turn finished"),
            ({}, "Ribbon Field · Turn finished"),
        ):
            with self.subTest(context=context), self.push.connection() as db:
                self.push.subscribe(self.device, subscription())
                self.push._enqueue(db, self.device, title, "completed", "thread_one.2", self.now + 3600,
                                   {**context, "title": "Thread title", "command": "private command"})
                payload = json.loads(db.execute("SELECT payload FROM outbox WHERE event=?", (title,)).fetchone()[0])
                self.assertEqual(payload["title"], title)
                self.assertEqual(payload["body"], "Thread title")
                self.assertEqual(payload["url"], "/#thread_one.2")
                self.assertNotIn("private command", json.dumps(payload))
        with self.push.connection() as db:
            self.push._enqueue(db, self.device, "long", "approval", "thread", self.now + 900,
                               {"client": "claude", "project_name": "\U0001f600" * 5000, "title": "\U0001f600" * 5000})
            payload = json.loads(db.execute("SELECT payload FROM outbox WHERE event='long'").fetchone()[0])
            self.assertTrue(payload["title"].endswith(" · Claude is requesting approval"))
            self.assertLess(len(json.dumps(payload).encode()), 4096)

    def test_test_notification_has_a_useful_fallback_without_a_thread(self):
        self.push.subscribe(self.device, subscription())
        self.push.test(self.device)
        self.push.tick()
        payload = self.transport.sent[0]["payload"]
        self.assertEqual(payload["title"], "Ribbon Field")
        self.assertEqual(payload["body"], "Phone notifications are working")
        self.assertEqual(payload["url"], "/")


class WebPushHTTPTests(unittest.TestCase):
    def setUp(self):
        self.transport_patch = patch("agent_coord.web_push.PushTransport", FakeTransport)
        self.transport_patch.start()
        self.addCleanup(self.transport_patch.stop)
        self.fixture = RemoteHTTPTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def test_push_routes_require_pairing_origin_and_csrf(self):
        f = self.fixture
        self.assertEqual(f.request("GET", "/api/browser/push/status")[0], 401)
        cookie = f.pair()
        self.assertEqual(f.request("GET", "/api/browser/push/status", local=True)[0], 403)
        self.assertEqual(f.request("GET", "/api/browser/push/status", cookie=cookie)[0], 200)
        body = {"subscription": subscription()}
        for origin, csrf in ((f.origin, None), ("https://evil.example", f.csrf)):
            self.assertEqual(f.request("POST", "/api/browser/push/subscribe", body, cookie=cookie,
                                       origin=origin, csrf=csrf)[0], 403)
        status, _, data = f.request("POST", "/api/browser/push/subscribe", body, cookie=cookie, origin=f.origin, csrf=f.csrf)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(data)["enabled"])
        status, _, data = f.request("POST", "/api/browser/push/unsubscribe", {}, cookie=cookie, origin=f.origin, csrf=f.csrf)
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(data)["enabled"])

    def test_public_worker_is_host_gated_and_cannot_read_conversations(self):
        f = self.fixture
        status, headers, data = f.request("GET", "/push-worker.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", headers["Content-Type"])
        self.assertIn(b'showNotification', data)
        self.assertEqual(f.request("GET", "/push-worker.js", extra={"Host": "evil.example"})[0], 403)
        self.assertEqual(f.request("GET", "/api/browser/threads")[0], 401)


class WebPushBrowserTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node required")
    def test_phone_controls_and_worker(self):
        result = subprocess.run(["node", "--test", str(Path(__file__).with_name("test_web_push.js"))],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


@unittest.skipUnless(PushTransport().available, "Optional Web Push packages required for encryption test")
class WebPushEncryptionTests(unittest.TestCase):
    def test_real_library_encrypts_payload_and_disables_redirects(self):
        import http_ece
        import requests
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        private = ec.generate_private_key(ec.SECP256R1())
        public = private.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        encode = lambda value: base64.urlsafe_b64encode(value).decode().rstrip("=")
        sub = subscription()
        sub["keys"]["p256dh"] = encode(public)
        payload = {"body": "Turn finished", "url": "/#thread-id"}
        transport = PushTransport()
        vapid, _ = transport.generate_keys()
        captured = {}
        def post(_session, url, **kwargs):
            captured.update(kwargs)
            response = requests.Response()
            response.status_code = 201
            response._content = b""
            return response
        with patch.object(requests.Session, "post", post):
            self.assertEqual(transport.send(sub, payload, vapid, "https://mac.test.ts.net", 30)[0], 201)
        self.assertFalse(captured["allow_redirects"])
        self.assertEqual(captured["timeout"], 10)
        self.assertNotIn(b"Turn finished", captured["data"])
        decrypted = http_ece.decrypt(captured["data"], private_key=private, auth_secret=b"a" * 16, version="aes128gcm")
        self.assertEqual(json.loads(decrypted), payload)
        self.assertIn("vapid", captured["headers"]["authorization"])
