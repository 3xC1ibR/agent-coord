from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import subprocess

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("plugin_tools", ROOT / "scripts/plugin_tools.py")
tools = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tools)


class PluginToolsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.plugin = self.root / "plugin"
        shutil.copytree(tools.PLUGIN, self.plugin, ignore=shutil.ignore_patterns("__pycache__"))

    def test_project_layout_and_marketplace(self):
        tools.validate(self.plugin)
        self.assertEqual(tools.marketplace_name(ROOT / ".agents/plugins/marketplace.json"), "personal")

    def test_cachebuster_preserves_base_and_other_metadata_and_never_stacks(self):
        path = self.plugin / ".codex-plugin/plugin.json"
        before = json.loads(path.read_text())
        claude = (self.plugin / ".claude-plugin/plugin.json").read_bytes()
        for stamp in ("20261007123456", "20261007123457"):
            version = tools.cachebuster(self.plugin, stamp=stamp)
            self.assertEqual(version, "0.1.0+codex." + stamp)
        after = json.loads(path.read_text())
        self.assertEqual({k: v for k, v in after.items() if k != "version"},
                         {k: v for k, v in before.items() if k != "version"})
        self.assertEqual((self.plugin / ".claude-plugin/plugin.json").read_bytes(), claude)
        with self.assertRaises(ValueError):
            tools.cachebuster(self.plugin, stamp="20261007123457")

    def test_invalid_layout_cannot_mutate_version(self):
        path = self.plugin / ".codex-plugin/plugin.json"
        before = path.read_bytes()
        (self.plugin / "scripts/agent-coord-hook").unlink()
        with self.assertRaisesRegex(ValueError, "Missing shared runtime"):
            tools.cachebuster(self.plugin)
        self.assertEqual(path.read_bytes(), before)

    def test_invalid_versions_are_rejected_without_normalizing_them(self):
        path = self.plugin / ".codex-plugin/plugin.json"
        for version in ("0.1.0+codex.1+codex.2", "0.1.0+other", None):
            manifest = json.loads(path.read_text())
            manifest["version"] = version
            path.write_text(json.dumps(manifest))
            before = path.read_bytes()
            with self.assertRaises(ValueError):
                tools.cachebuster(self.plugin)
            self.assertEqual(path.read_bytes(), before)

    def test_cache_verification_detects_missing_and_changed_files(self):
        installed = self.root / "installed"
        shutil.copytree(self.plugin, installed)
        self.assertGreater(tools.verify(self.plugin, installed), 10)
        target = installed / "scripts/agent-coord"
        target.write_text("wrong")
        with self.assertRaisesRegex(ValueError, "does not match"):
            tools.verify(self.plugin, installed)
        target.unlink()
        with self.assertRaisesRegex(ValueError, "does not match"):
            tools.verify(self.plugin, installed)

    def test_marketplace_name_rejects_invalid_input(self):
        path = self.root / "marketplace.json"
        for data in ([], {}, {"name": "bad@name"}, {"name": None}):
            path.write_text(json.dumps(data))
            with self.assertRaises(ValueError):
                tools.marketplace_name(path)

    def test_install_restores_removed_hook_paths_even_when_installation_fails(self):
        cache = self.root / "plugins/cache/personal/agent-coord"
        old = cache / "old-version"
        old.mkdir(parents=True)
        (old / "hook").write_text("old hook")
        (cache / "compatibility").symlink_to(old)
        for fail in (False, True):
            def installer(*args, **kwargs):
                shutil.rmtree(cache)
                if fail:
                    raise subprocess.CalledProcessError(1, "codex", stderr="Install failed")
                return subprocess.CompletedProcess(args[0], 0, stdout='{"installedPath":"new"}')
            with patch.object(tools.subprocess, "run", side_effect=installer):
                if fail:
                    with self.assertRaises(subprocess.CalledProcessError):
                        tools.install_codex("personal", codex_home=self.root)
                else:
                    self.assertEqual(tools.install_codex("personal", codex_home=self.root)["installedPath"], "new")
            self.assertEqual((old / "hook").read_text(), "old hook")
            self.assertEqual((cache / "compatibility/hook").read_text(), "old hook")
