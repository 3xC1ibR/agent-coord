from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))
from agent_coord.attention import AttentionClassifier, ClassificationError, classify, load_config
from agent_coord.jev_settings import JevSettings, Keychain, read_key
from agent_coord.store import CoordinationError
import test_remote_access as remote


class JevSettingsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.database = self.root / "state.sqlite3"
        self.service = JevSettings(self.database)
        self.request = patch("agent_coord.attention.classify").start()
        self.addCleanup(patch.stopall)

    def enable(self):
        return self.service.configure({"action": "test_enable", "api_key": "test-only-secret"})

    def test_default_is_off_and_skip_is_durable_without_network(self):
        self.assertFalse(self.service.settings()["enabled"])
        self.assertFalse(self.service.settings()["onboarding_dismissed"])
        self.service.configure({"action": "dismiss"})
        self.assertTrue(JevSettings(self.database).settings()["onboarding_dismissed"])
        self.assertIsNone(load_config(self.database))
        self.request.assert_not_called()

    def test_private_file_enable_disable_reenable_and_remove_without_restart(self):
        with patch("agent_coord.jev_settings.sys.platform", "linux"):
            result = self.enable()
            self.assertTrue(result["enabled"])
            self.assertEqual(result["storage"], "file")
            config = load_config(self.database)
            self.assertEqual(read_key(config), "test-only-secret")
            self.assertNotIn("test-only-secret", self.service.path.read_text())
            self.assertNotIn("test-only-secret", json.dumps(result))
            self.assertEqual(self.service.key_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.service.path.stat().st_mode & 0o777, 0o600)
            args, kwargs = self.request.call_args
            self.assertEqual(args[0]["original_request"], "Say hello.")
            self.assertEqual(kwargs["api_key"], "test-only-secret")
            self.service.configure({"action": "disable"})
            self.assertIsNone(load_config(self.database))
            self.assertTrue(self.service.settings()["configured"])
            self.service.configure({"action": "test_enable"})
            self.assertTrue(self.service.settings()["enabled"])
            self.service.configure({"action": "remove"})
            self.assertFalse(self.service.key_path.exists())
            self.assertFalse(self.service.settings()["configured"])
            self.assertTrue(self.service.settings()["onboarding_dismissed"])

    def test_macos_uses_keychain_and_never_writes_a_plaintext_key(self):
        with patch("agent_coord.jev_settings.sys.platform", "darwin"), patch("agent_coord.jev_settings.Keychain") as factory:
            factory.return_value.read.return_value = "test-only-secret"
            result = self.enable()
            self.assertEqual(result["storage"], "keychain")
            factory.return_value.write.assert_called_once_with(self.service.account, "test-only-secret")
            self.assertFalse(self.service.key_path.exists())
            self.assertNotIn("test-only-secret", self.service.path.read_text())
            self.assertEqual(read_key(load_config(self.database)), "test-only-secret")
            self.service.configure({"action": "remove"})
            factory.return_value.delete.assert_called_once_with(self.service.account)

    def test_failure_preserves_saved_config_and_never_enables_or_saves_a_bad_key(self):
        for code in ("http_401", "http_402", "http_403", "http_429", "request_failed", "invalid_response"):
            self.request.side_effect = ClassificationError(code)
            with self.subTest(code=code), self.assertRaises(CoordinationError) as caught:
                self.enable()
            self.assertNotIn("test-only-secret", str(caught.exception))
            self.assertFalse(self.service.path.exists())
            self.assertFalse(self.service.key_path.exists())
        self.request.side_effect = None
        with patch("agent_coord.jev_settings.sys.platform", "linux"):
            self.enable()
            before = self.service.path.read_bytes(), self.service.key_path.read_bytes()
            self.request.side_effect = ClassificationError("http_401")
            with self.assertRaises(CoordinationError):
                self.service.configure({"action": "test_enable", "api_key": "bad-replacement"})
            self.assertEqual(before, (self.service.path.read_bytes(), self.service.key_path.read_bytes()))

    def test_legacy_key_file_is_supported_and_never_deleted(self):
        key = self.root / "my-own-key"
        key.write_text("legacy-secret")
        self.service.path.write_text(json.dumps({"enabled": True, "api_key_file": str(key)}))
        self.assertTrue(self.service.settings()["enabled"])
        self.assertTrue(self.service.settings()["onboarding_dismissed"])
        self.service.configure({"action": "remove"})
        self.assertEqual(key.read_text(), "legacy-secret")
        self.assertIsNone(load_config(self.database))

    def test_storage_failures_are_sanitized_and_remove_disables_before_deletion(self):
        with patch("agent_coord.jev_settings.sys.platform", "darwin"), patch("agent_coord.jev_settings.Keychain") as factory:
            factory.return_value.write.side_effect = OSError("test-only-secret")
            with self.assertRaises(CoordinationError) as caught:
                self.enable()
            self.assertNotIn("test-only-secret", str(caught.exception))
            self.assertIsNone(load_config(self.database))
            factory.return_value.write.side_effect = None
            self.enable()
            factory.return_value.delete.side_effect = OSError("test-only-secret")
            with self.assertRaises(CoordinationError):
                self.service.configure({"action": "remove"})
            self.assertIsNone(load_config(self.database))

    def test_invalid_key_and_actions_never_send_network_requests(self):
        with self.assertRaises(TypeError):
            read_key(None)
        for key in (None, 10, "", "bad\nkey", "bad\x00key", "é", "a" * 4097):
            with self.subTest(key=key), self.assertRaises(CoordinationError):
                self.service.configure({"action": "test_enable", "api_key": key})
        for value in ({}, {"action": "other"}, {"action": []}, {"action": "disable", "api_key": "secret"},
                      {"action": "test_enable", "url": "https://attacker.test"}):
            with self.assertRaises(CoordinationError):
                self.service.configure(value)
        self.request.assert_not_called()

    def test_dynamic_worker_observes_enable_and_disable_between_threads(self):
        sessions = Mock()
        sessions.store.database_path = self.database
        sessions.list_work_threads.return_value = [{"turn_completion": None}, {"turn_completion": None}]
        worker = AttentionClassifier(sessions, start=False)
        worker.scan()
        sessions.list_work_threads.assert_not_called()
        with patch("agent_coord.jev_settings.sys.platform", "linux"):
            self.enable()
        worker.scan()
        sessions.list_work_threads.assert_called_once()
        self.service.configure({"action": "disable"})
        sessions.list_work_threads.reset_mock()
        worker.scan()
        sessions.list_work_threads.assert_not_called()

    def test_config_override_applies_to_settings_and_worker(self):
        override = self.root / "override" / "jev.json"
        with patch.dict("os.environ", {"AGENT_COORD_JEV_CONFIG": str(override)}), patch("agent_coord.jev_settings.sys.platform", "linux"):
            service = JevSettings(self.database)
            service.configure({"action": "test_enable", "api_key": "override-secret"})
            self.assertEqual(service.path, override)
            self.assertEqual(read_key(load_config(self.database)), "override-secret")

    @unittest.skipUnless(sys.platform == "darwin", "macOS frameworks required")
    def test_keychain_bindings_load_without_reading_or_modifying_user_credentials(self):
        Keychain()


class JevHTTPTests(unittest.TestCase):
    def setUp(self):
        self.fixture = remote.RemoteHTTPTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def test_settings_and_assets_are_local_only_and_mutations_require_csrf(self):
        f = self.fixture
        for asset in ("/jev.js", "/jev.css"):
            self.assertEqual(f.request("GET", asset, local=True)[0], 200)
        self.assertEqual(f.request("GET", "/api/browser/jev/settings", local=True)[0], 200)
        self.assertEqual(f.request("GET", "/api/browser/jev/settings")[0], 401)
        cookie = f.pair()
        self.assertEqual(f.request("GET", "/api/browser/jev/settings", cookie=cookie)[0], 403)
        self.assertEqual(f.request("POST", "/api/browser/jev/settings", {"action": "dismiss"},
                                   cookie=cookie, origin=f.origin, csrf=f.csrf)[0], 403)
        self.assertEqual(f.request("POST", "/api/browser/jev/settings", {"action": "dismiss"},
                                  local=True, csrf="wrong")[0], 403)
        with patch("agent_coord.attention.classify") as request, patch("agent_coord.jev_settings.sys.platform", "linux"):
            code, _, raw = f.request("POST", "/api/browser/jev/settings", {"action": "test_enable", "api_key": "http-test-secret"},
                                      local=True, csrf=f.csrf)
            result = json.loads(raw)
            self.assertEqual(code, 200)
            self.assertTrue(result["enabled"])
            self.assertNotIn("http-test-secret", json.dumps(result))
            request.assert_called_once()
            self.assertEqual(f.request("POST", "/api/browser/jev/settings", {"action": "disable"}, local=True, csrf=f.csrf)[0], 200)
