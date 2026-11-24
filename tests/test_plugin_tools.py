from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import signal
import shutil
import tempfile
import time
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

    def development_homes(self):
        codex = self.root / "codex"
        claude = self.root / "claude"
        codex_cache = codex / "plugins/cache/personal/agent-coord"
        claude_cache = claude / "plugins/cache/agent-coord/agent-coord"
        old_claude = claude_cache / "0.1.0"
        old_claude.mkdir(parents=True)
        registry = claude / "plugins/installed_plugins.json"
        registry.write_text(json.dumps({"version": 2, "plugins": {
            "agent-coord@agent-coord": [{"scope": "user", "version": "0.1.0", "installPath": str(old_claude)}],
            "another@catalog": [{"scope": "project"}]}}))
        previous = codex_cache / "0.1.0+codex.1"
        (previous / ".codex-plugin").mkdir(parents=True)
        (previous / ".codex-plugin/plugin.json").write_text('{"name":"agent-coord","version":"0.1.0+codex.1"}')
        (previous / "scripts/web").mkdir(parents=True)
        hook = previous / "scripts/hook"
        hook.write_text("#!/bin/sh\nprintf 'original hook'\n")
        hook.chmod(0o755)
        (previous / "scripts/web/openui.js").write_text("original assets")
        return {"codex_home": codex, "claude_home": claude, "share": self.root / "shared"}, previous, registry

    def test_dev_link_shares_live_assets_without_rewriting_source_or_pinned_code(self):
        homes, previous, registry = self.development_homes()
        manifest = self.plugin / ".codex-plugin/plugin.json"
        source_manifest = manifest.read_bytes()
        second = previous.with_name("0.1.0+codex.2")
        shutil.copytree(previous, second)
        (second / "scripts/hook").write_text("#!/bin/sh\nprintf 'second hook'\n")
        old_directory = previous.stat().st_ino
        original_config = json.loads(registry.read_bytes())
        result = tools.dev_link(self.plugin, prune=True, **homes)
        self.assertEqual(result["prunedCodexDirectories"], 2)
        self.assertGreater(result["deduplicatedBytes"], 0)
        self.assertEqual(manifest.read_bytes(), source_manifest)
        self.assertTrue(previous.is_symlink())
        self.assertEqual(old_directory, previous.stat().st_ino)
        self.assertEqual(subprocess.check_output([previous / "scripts/hook"]), b"original hook")
        self.assertEqual(subprocess.check_output([second / "scripts/hook"]), b"second hook")
        self.assertTrue((previous / "scripts/web/openui.js").samefile(second / "scripts/web/openui.js"))
        self.assertEqual((previous / "scripts/web/openui.js").read_text(), "original assets")
        updated = json.loads(registry.read_bytes())
        self.assertEqual(updated["plugins"]["another@catalog"], original_config["plugins"]["another@catalog"])
        self.assertEqual(updated["plugins"]["agent-coord@agent-coord"][0]["installPath"], result["claude"])
        self.assertTrue(Path(original_config["plugins"]["agent-coord@agent-coord"][0]["installPath"]).is_dir())
        source = self.plugin / "skills/agent-coordination/SKILL.md"
        source.write_text(source.read_text() + "\nUpdated development text.\n")
        assets = Path("scripts/agent_coord/web/vendor/openui/openui-bundle.min.js")
        for key in ("codex", "claude"):
            cache = Path(result[key])
            self.assertFalse((cache / ".codex-plugin").is_symlink())
            self.assertFalse((cache / ".codex-plugin/plugin.json").is_symlink())
            self.assertTrue((cache / assets).samefile(self.plugin / assets))
            self.assertEqual((cache / source.relative_to(self.plugin)).read_text(), source.read_text())
            self.assertGreater(tools.verify(self.plugin, cache), 10)
        retry = tools.dev_link(self.plugin, prune=True, **homes)
        self.assertEqual(retry["version"], result["version"])
        self.assertEqual(retry["prunedCodexDirectories"], 0)

    @unittest.skipUnless(shutil.which("codex"), "requires the Codex CLI")
    def test_codex_app_server_discovers_development_skills(self):
        from test_browser_providers import CoordinationStore
        from agent_coord.codex_app_server import CodexRPC

        homes, _, _ = self.development_homes()
        tools.dev_link(self.plugin, **homes)
        home = homes["codex_home"]
        (home / "config.toml").write_text(
            '[marketplaces.personal]\nsource_type = "local"\nsource = ' + json.dumps(str(ROOT)) +
            '\n[plugins."agent-coord@personal"]\nenabled = true\n')
        with patch.dict(os.environ, {"CODEX_HOME": str(home)}):
            rpc = CodexRPC(CoordinationStore(self.root / "test.sqlite3"), lambda *args: None)
            try:
                result = rpc.request("skills/list", {"cwds": [str(self.root)], "forceReload": True})
            finally:
                rpc.close()
        skills = {skill["name"]: Path(skill["path"]).resolve() for group in result["data"]
                  for skill in group["skills"] if skill["enabled"]}
        for skill in (self.plugin / "skills").iterdir():
            self.assertEqual(skills["agent-coord:" + skill.name], (skill / "SKILL.md").resolve())

    def test_interrupted_cache_exchange_keeps_pinned_hook_and_can_resume(self):
        homes, previous, _ = self.development_homes()
        with patch.object(tools, "_exchange_paths", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                tools.dev_link(self.plugin, prune=True, **homes)
        self.assertEqual(subprocess.check_output([previous / "scripts/hook"]), b"original hook")
        result = tools.dev_link(self.plugin, prune=True, **homes)
        self.assertEqual(result["prunedCodexDirectories"], 1)
        self.assertEqual(subprocess.check_output([previous / "scripts/hook"]), b"original hook")

    def test_install_never_exposes_pinned_paths_to_installer_pruning(self):
        cache = self.root / "plugins/cache/personal/agent-coord"
        old = cache / "old-version"
        old.mkdir(parents=True)
        hook = old / "hook"
        hook.write_text("#!/bin/sh\nprintf 'old hook'\n")
        hook.chmod(0o755)
        (old / "index.html").write_text("old assets")
        (cache / "compatibility").symlink_to(old)
        config = self.root / "config.toml"
        config.write_text('original settings\n')
        original_run = subprocess.run
        for failure in (None, subprocess.CalledProcessError(1, "codex", stderr="Install failed"), KeyboardInterrupt()):
            def installer(*args, **kwargs):
                staged_home = Path(kwargs["env"]["CODEX_HOME"])
                self.assertNotEqual(staged_home, self.root)
                self.assertEqual((staged_home / "config.toml").read_bytes(), config.read_bytes())
                staged_cache = staged_home / "plugins/cache/personal/agent-coord"
                staged_cache.mkdir(parents=True)
                shutil.rmtree(staged_cache)  # Codex prunes only its isolated home.
                self.assertEqual(original_run([str(hook)], capture_output=True, text=True, check=True).stdout, "old hook")
                self.assertEqual((cache / "compatibility/index.html").read_text(), "old assets")
                installed = staged_cache / "0.1.0+codex.123"
                installed.mkdir(parents=True)
                (installed / "hook").write_text("new hook")
                (staged_home / "config.toml").write_text('installer enabled plugin\n')
                if failure:
                    raise failure
                return subprocess.CompletedProcess(args[0], 0, stdout=json.dumps({
                    "version": installed.name, "installedPath": str(installed)}))
            config.write_text('original settings\n')
            with patch.object(tools.subprocess, "run", side_effect=installer):
                if failure:
                    with self.assertRaises(type(failure)):
                        tools.install_codex("personal", codex_home=self.root)
                    self.assertEqual(config.read_text(), 'original settings\n')
                else:
                    result = tools.install_codex("personal", codex_home=self.root)
                    self.assertEqual(Path(result["installedPath"]), cache.resolve() / "0.1.0+codex.123")
                    self.assertEqual(config.read_text(), 'installer enabled plugin\n')
            self.assertEqual(original_run([str(hook)], capture_output=True, text=True, check=True).stdout, "old hook")
            self.assertEqual((cache / "compatibility/index.html").read_text(), "old assets")

    def test_install_rejects_concurrent_configuration_change(self):
        config = self.root / "config.toml"
        config.write_text("original\n")
        def installer(*args, **kwargs):
            home = Path(kwargs["env"]["CODEX_HOME"])
            installed = home / "plugins/cache/personal/agent-coord/0.1.0+codex.123"
            installed.mkdir(parents=True)
            config.write_text("unrelated concurrent change\n")
            return subprocess.CompletedProcess(args[0], 0, stdout=json.dumps({
                "version": installed.name, "installedPath": str(installed)}))
        with patch.object(tools.subprocess, "run", side_effect=installer):
            with self.assertRaisesRegex(ValueError, "configuration changed"):
                tools.install_codex("personal", codex_home=self.root)
        self.assertEqual(config.read_text(), "unrelated concurrent change\n")
        self.assertFalse((self.root / "plugins/cache/personal/agent-coord/0.1.0+codex.123").exists())

    def test_killed_refresh_leaves_live_hooks_and_assets_available(self):
        cache = self.root / "plugins/cache/personal/agent-coord/old"
        cache.mkdir(parents=True)
        (cache / "hook").write_text("#!/bin/sh\nprintf alive\n")
        (cache / "hook").chmod(0o755)
        (cache / "index.html").write_text("assets")
        config = self.root / "config.toml"
        config.write_text("original\n")
        marker = self.root / "installer-started"
        binary = self.root / "bin/codex"
        binary.parent.mkdir()
        binary.write_text("#!" + os.sys.executable + "\n" +
                          "import os,pathlib,shutil,time\n" +
                          "cache=pathlib.Path(os.environ['CODEX_HOME'])/'plugins/cache'\n" +
                          "cache.mkdir(parents=True)\nshutil.rmtree(cache)\n" +
                          "pathlib.Path(" + repr(str(marker)) + ").touch()\ntime.sleep(30)\n")
        binary.chmod(0o755)
        code = ("import importlib.util,pathlib; "
                "s=importlib.util.spec_from_file_location('plugin_tools'," + repr(str(ROOT / "scripts/plugin_tools.py")) + "); "
                "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
                "m.install_codex('personal',codex_home=pathlib.Path(" + repr(str(self.root)) + "))")
        environment = {**os.environ, "CODEX_HOME": str(self.root),
                       "PATH": str(binary.parent) + os.pathsep + os.environ.get("PATH", os.defpath)}
        process = subprocess.Popen([os.sys.executable, "-c", code], env=environment,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        try:
            deadline = time.monotonic() + 5
            while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(marker.exists(), "Installer never reached pruning boundary")
            self.assertEqual(subprocess.check_output([str(cache / "hook")]), b"alive")
            self.assertEqual((cache / "index.html").read_text(), "assets")
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
            self.assertEqual(subprocess.check_output([str(cache / "hook")]), b"alive")
            self.assertEqual(config.read_text(), "original\n")
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=5)

    def test_install_cannot_replace_an_existing_same_version_payload(self):
        cache = self.root / "plugins/cache/personal/agent-coord/0.1.0+codex.123"
        cache.mkdir(parents=True)
        (cache / "hook").write_text("old")
        config = self.root / "config.toml"
        config.write_text("original\n")
        def installer(*args, **kwargs):
            home = Path(kwargs["env"]["CODEX_HOME"])
            installed = home / "plugins/cache/personal/agent-coord/0.1.0+codex.123"
            installed.mkdir(parents=True)
            (installed / "hook").write_text("different")
            return subprocess.CompletedProcess(args[0], 0, stdout=json.dumps({
                "version": installed.name, "installedPath": str(installed)}))
        with patch.object(tools.subprocess, "run", side_effect=installer):
            with self.assertRaisesRegex(ValueError, "does not match source"):
                tools.install_codex("personal", codex_home=self.root)
        self.assertEqual((cache / "hook").read_text(), "old")
        self.assertEqual(config.read_text(), "original\n")
