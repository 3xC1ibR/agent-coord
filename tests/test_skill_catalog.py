import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))
from agent_coord.skill_catalog import skill_catalog
from agent_coord.store import CoordinationError


class SkillCatalogTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.sessions = SimpleNamespace(cwd=str(self.root), rpc=Mock())

    def test_enabled_native_inventory_scoped_to_cwd_without_starting_a_thread(self):
        self.sessions.rpc.request.return_value = {"data": [{"cwd": str(self.root), "skills": [
            {"name": "zebra", "enabled": True, "description": "Long description", "scope": "repo",
             "interface": {"shortDescription": "Short description"}},
            {"name": "agent:alpha", "enabled": True, "description": "Alpha"},
            {"name": "zebra", "enabled": True, "description": "Duplicate"},
            {"name": "disabled", "enabled": False},
        ]}, {"cwd": str(self.root.parent), "skills": [{"name": "outside", "enabled": True}]}]}
        result = skill_catalog(self.sessions, {"cwd": str(self.root), "client": "codex"})
        self.assertEqual([s["name"] for s in result], ["agent:alpha", "zebra"])
        self.assertEqual(result[1]["description"], "Short description")
        self.sessions.rpc.request.assert_called_once_with("skills/list", {"cwds": [str(self.root)], "forceReload": True})

    def test_workspace_boundary_and_invalid_fields_rejected_before_provider_call(self):
        for body in ({"cwd": str(self.root.parent)}, {"cwd": "relative"}, {"cwd": None},
                     {"client": "unknown"}, {"client": []}, {"extra": True}):
            with self.subTest(body=body), self.assertRaises(CoordinationError):
                skill_catalog(self.sessions, body)
        self.sessions.rpc.request.assert_not_called()

    def test_other_providers_do_not_advertise_codex_skills(self):
        for client in ("claude", "antigravity", "kimi", "muse"):
            self.assertEqual(skill_catalog(self.sessions, {"client": client}), [])
        self.sessions.rpc.request.assert_not_called()
