import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))
from agent_coord.session_defaults import session_defaults
from agent_coord.store import CoordinationError


class SessionDefaultsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.provider = Mock()
        self.sessions = SimpleNamespace(cwd=str(self.root), _provider=Mock(return_value=self.provider), models=Mock())
        self.sessions.models.return_value = [
            {"model": "catalog-default", "isDefault": True, "defaultReasoningEffort": "low"},
            {"model": "configured", "displayName": "Configured model", "defaultReasoningEffort": "medium",
             "supportedReasoningEfforts": [{"reasoningEffort": "high"}]}]
        self.provider.request.return_value = {"config": {"model": "configured", "model_reasoning_effort": "high"}}

    def test_codex_reads_workspace_configuration_before_catalog_defaults(self):
        result = session_defaults(self.sessions, {})
        self.assertEqual(result, {"model": "configured", "modelLabel": "Configured model", "effort": "high", "supportsEffort": True})
        self.provider.request.assert_called_once_with("config/read", {"cwd": str(self.root), "includeLayers": False})

    def test_explicit_model_keeps_configured_effort_and_missing_config_uses_catalog(self):
        result = session_defaults(self.sessions, {"model": "catalog-default"})
        self.assertEqual((result["model"], result["effort"]), ("catalog-default", "high"))
        self.provider.request.return_value = {"config": {}}
        result = session_defaults(self.sessions, {})
        self.assertEqual((result["model"], result["effort"]), ("catalog-default", "low"))

    def test_claude_uses_native_applied_model_and_model_specific_effort(self):
        self.provider.request.return_value = {"config": {"model": "concrete-model", "model_reasoning_effort": "high"}}
        self.sessions.models.return_value = [
            {"model": "default", "resolvedModel": "concrete-model", "displayName": "Default", "isDefault": True},
            {"model": "alias", "resolvedModel": "concrete-model", "displayName": "Concrete model"}]
        result = session_defaults(self.sessions, {"client": "claude", "model": "alias"})
        self.assertEqual((result["model"], result["modelLabel"], result["effort"]), ("concrete-model", "Concrete model", "high"))
        self.provider.request.assert_called_once_with("config/read", {"cwd": str(self.root), "model": "alias"})

    def test_other_providers_use_their_advertised_default_and_unknown_stays_unknown(self):
        for client in ("kimi", "muse", "antigravity"):
            self.assertEqual(session_defaults(self.sessions, {"client": client})["model"], "catalog-default")
        self.sessions._provider.assert_not_called()
        self.sessions.models.return_value = []
        self.assertIsNone(session_defaults(self.sessions, {"client": "kimi"})["model"])

    def test_invalid_input_and_workspace_boundary_fail_before_provider_calls(self):
        for body in ({"cwd": str(self.root.parent)}, {"cwd": None}, {"client": []}, {"client": "unknown"}, {"model": []}, {"extra": True}):
            with self.subTest(body=body), self.assertRaises(CoordinationError):
                session_defaults(self.sessions, body)
        self.sessions._provider.assert_not_called()

    def test_provider_error_is_not_replaced_with_an_incorrect_catalog_default(self):
        self.provider.request.side_effect = CoordinationError("Provider unavailable")
        with self.assertRaisesRegex(CoordinationError, "unavailable"):
            session_defaults(self.sessions, {})
