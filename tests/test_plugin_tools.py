from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shlex
import signal
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("plugin_tools", ROOT / "scripts/plugin_tools.py")
tools = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tools)


class PluginToolsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.plugin = self.root / "plugin"
        shutil.copytree(tools.PLUGIN, self.plugin, ignore=shutil.ignore_patterns("__pycache__"))

    def _reap_process_group(self, pgid: int | None) -> None:
        if not pgid or pgid <= 0:
            return
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(pgid, sig)
            except (ProcessLookupError, PermissionError):
                break
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                result = subprocess.run(
                    ["ps", "-axo", "pgid=,stat="],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if not any(
                    len(line.split()) == 2 and int(line.split()[0]) == pgid and not line.split()[1].startswith("Z")
                    for line in result.stdout.splitlines()
                ):
                    return
                time.sleep(0.02)

    def _reap_workspace_descendants(self) -> None:
        marker = str(self.root)
        result = subprocess.run(
            ["ps", "-axo", "pid=,pgid=,command="],
            capture_output=True,
            text=True,
            check=False,
        )
        own_pid = os.getpid()
        for line in result.stdout.splitlines():
            parts = line.strip().split(None, 2)
            if len(parts) < 3:
                continue
            pid, pgid, cmd = int(parts[0]), int(parts[1]), parts[2]
            if pid == own_pid or marker not in cmd:
                continue
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

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

    def test_dev_link_snapshots_codex_without_rewriting_source_or_pinned_code(self):
        homes, previous, registry = self.development_homes()
        manifest = self.plugin / ".codex-plugin/plugin.json"
        source_manifest = manifest.read_bytes()
        second = previous.with_name("0.1.0+codex.2")
        shutil.copytree(previous, second)
        (second / "scripts/hook").write_text("#!/bin/sh\nprintf 'second hook'\n")
        old_directory = previous.stat().st_ino
        original_config = json.loads(registry.read_bytes())
        # Without --prune, previous Codex snapshots remain intact.
        result = tools.dev_link(self.plugin, prune=False, **homes)
        self.assertEqual(result["repairedCodexDirectories"], 0)
        self.assertNotIn("prunedDirectories", result)
        self.assertEqual(manifest.read_bytes(), source_manifest)
        self.assertFalse(previous.is_symlink())
        self.assertEqual(old_directory, previous.stat().st_ino)
        self.assertEqual(subprocess.check_output([previous / "scripts/hook"]), b"original hook")
        self.assertEqual(subprocess.check_output([second / "scripts/hook"]), b"second hook")
        # Excluded vendored openui bundle is omitted from Codex snapshots.
        openui_rel = Path("scripts/agent_coord/web/vendor/openui")
        self.assertTrue((self.plugin / openui_rel).is_dir())
        self.assertFalse((Path(result["codex"]) / openui_rel).exists())
        updated = json.loads(registry.read_bytes())
        self.assertEqual(updated["plugins"]["another@catalog"], original_config["plugins"]["another@catalog"])
        self.assertEqual(updated["plugins"]["agent-coord@agent-coord"][0]["installPath"], result["claude"])
        self.assertTrue(Path(original_config["plugins"]["agent-coord@agent-coord"][0]["installPath"]).is_dir())
        source = self.plugin / "skills/agent-coordination/SKILL.md"
        original_skill = source.read_text()
        for key in ("codex", "claude"):
            self.assertGreater(tools.verify(self.plugin, Path(result[key])), 10)
        # With --prune, superseded version directories are removed from caches, keeping only active version.
        retry = tools.dev_link(self.plugin, prune=True, **homes)
        self.assertEqual(retry["version"], result["version"])
        self.assertEqual(retry["repairedCodexDirectories"], 0)
        self.assertEqual(retry["prunedDirectories"], 3)  # previous, second, and old_claude (0.1.0)
        self.assertFalse(previous.exists())
        self.assertFalse(second.exists())
        self.assertFalse(Path(original_config["plugins"]["agent-coord@agent-coord"][0]["installPath"]).exists())
        self.assertTrue(Path(result["codex"]).is_dir())
        self.assertTrue(Path(result["claude"]).is_dir())
        source.write_text(source.read_text() + "\nUpdated development text.\n")
        relative_skill = source.relative_to(self.plugin)
        self.assertEqual((Path(result["codex"]) / relative_skill).read_text(), original_skill)
        self.assertEqual((Path(result["claude"]) / relative_skill).read_text(), source.read_text())
        updated = tools.dev_link(self.plugin, prune=True, **homes)
        self.assertNotEqual(updated["version"], result["version"])
        self.assertGreater(tools.verify(self.plugin, Path(updated["codex"])), 10)
        self.assertFalse(Path(result["codex"]).exists())
        self.assertFalse(Path(result["claude"]).exists())
        self.assertFalse((Path(updated["codex"]) / openui_rel).exists())
        self.assertFalse(any(path.is_symlink() for path in Path(updated["codex"]).rglob("*")))

    def test_dev_link_repairs_legacy_component_links_in_place(self):
        homes, previous, _ = self.development_homes()
        current = homes["share"] / "current"
        current.parent.mkdir()
        current.symlink_to(self.plugin)
        legacy = previous.with_name("0.1.0+codex.20261124000000")
        tools._publish_development_shim(self.plugin, current, legacy)
        original_inode = legacy.stat().st_ino
        hook = (legacy / "scripts/agent-coord-hook").read_bytes()
        result = tools.dev_link(self.plugin, **homes)
        self.assertEqual(result["repairedCodexDirectories"], 1)
        self.assertEqual(Path(result["codex"]), legacy)
        self.assertEqual(legacy.stat().st_ino, original_inode)
        self.assertEqual((legacy / "scripts/agent-coord-hook").read_bytes(), hook)
        self.assertFalse(any(path.is_symlink() for path in legacy.rglob("*")))

    @unittest.skipUnless(shutil.which("codex"), "requires the Codex CLI")
    def test_codex_native_session_runs_development_hook(self):
        receipt = self.root / "hook-events.jsonl"
        (self.plugin / "scripts/agent-coord-hook").write_text(
            "import json, pathlib, sys\n"
            "event = json.load(sys.stdin)\n"
            f"with pathlib.Path({str(receipt)!r}).open('a') as output:\n"
            "    output.write(json.dumps(event) + '\\n')\n"
            "print(json.dumps({'decision': 'block', 'reason': 'Fixture stops before inference.'} "
            "if event['hook_event_name'] == 'UserPromptSubmit' else {}))\n")
        hook = {"hooks": {event: [{"hooks": [{"type": "command",
            "command": shlex.quote(sys.executable) + ' "${CLAUDE_PLUGIN_ROOT}/scripts/agent-coord-hook"',
            "timeout": 5}]}] for event in ("SessionStart", "UserPromptSubmit")}}
        for path in ("hooks.json", "hooks/hooks.json"):
            (self.plugin / path).write_text(json.dumps(hook))
        manifest = self.plugin / ".codex-plugin/plugin.json"
        data = json.loads(manifest.read_text())
        data.pop("mcpServers", None)
        manifest.write_text(json.dumps(data))
        marketplace = self.root / ".agents/plugins/marketplace.json"
        marketplace.parent.mkdir(parents=True)
        data = json.loads((ROOT / ".agents/plugins/marketplace.json").read_text())
        data["plugins"][0]["source"]["path"] = "./plugin"
        marketplace.write_text(json.dumps(data))
        homes, _, _ = self.development_homes()
        tools.dev_link(self.plugin, **homes)
        home = homes["codex_home"]
        (home / "config.toml").write_text(
            'model_provider = "fixture"\nmodel = "fixture"\n'
            '[marketplaces.personal]\nsource_type = "local"\nsource = ' + json.dumps(str(self.root)) +
            '\n[plugins."agent-coord@personal"]\nenabled = true\n'
            '[model_providers.fixture]\nname = "Local fixture"\n'
            'base_url = "http://127.0.0.1:9/v1"\nwire_api = "responses"\n'
            'request_max_retries = 0\nstream_max_retries = 0\n')
        # Only this test's inert receipt hook is trusted. It blocks before inference;
        # even a broken hook can only contact the closed loopback fixture port.
        from test_browser_providers import CoordinationStore
        from agent_coord.codex_app_server import CodexRPC
        self.addCleanup(self._reap_workspace_descendants)
        with patch.dict(os.environ, {"CODEX_HOME": str(home)}):
            rpc = CodexRPC(CoordinationStore(self.root / "test.sqlite3"), lambda event: None)
            rpc_pgid = None
            try:
                inventory = rpc.request("hooks/list", {"cwds": [str(self.root)]})
                rpc_pgid = rpc.process.pid if rpc.process is not None else None
            finally:
                rpc_pgid = rpc_pgid or (rpc.process.pid if rpc.process is not None else None)
                rpc.close()
                self._reap_process_group(rpc_pgid)
        self.assertNotIn("unsupported plugin entry type", json.dumps(inventory))
        self.assertFalse(inventory["data"][0]["errors"], inventory)
        with (home / "config.toml").open("a") as config:
            for hook in inventory["data"][0]["hooks"]:
                command_hash, content_hash = hook["currentHash"].split("/")
                config.write('\n[hooks.state.' + json.dumps(hook["key"]) + ']\n'
                    'enabled = true\ntrusted_hash = ' + json.dumps(command_hash) + '\n'
                    'trusted_content_hash = ' + json.dumps(content_hash) + '\n')
        exec_proc = subprocess.Popen(
            ["codex", "exec", "--trust", "--skip-git-repo-check", "--sandbox", "read-only", "--json", "Hook execution fixture."],
            cwd=self.root,
            env={**os.environ, "CODEX_HOME": str(home)},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            stdout_text, stderr_text = exec_proc.communicate(timeout=30)
        finally:
            self._reap_process_group(exec_proc.pid)
            for stream in (exec_proc.stdout, exec_proc.stderr):
                if stream is not None:
                    stream.close()
        evidence = stdout_text + stderr_text + json.dumps(inventory)
        self.assertNotIn("unsupported plugin entry type", evidence)
        self.assertTrue(receipt.exists(), evidence)
        events = [json.loads(line)["hook_event_name"] for line in receipt.read_text().splitlines()]
        self.assertIn("SessionStart", events)
        self.assertIn("UserPromptSubmit", events)

    @unittest.skipUnless(shutil.which("codex"), "requires the Codex CLI")
    def test_codex_app_server_discovers_development_skills(self):
        from test_browser_providers import CoordinationStore
        from agent_coord.codex_app_server import CodexRPC

        self.addCleanup(self._reap_workspace_descendants)
        homes, _, _ = self.development_homes()
        installed = tools.dev_link(self.plugin, **homes)
        home = homes["codex_home"]
        (home / "config.toml").write_text(
            '[marketplaces.personal]\nsource_type = "local"\nsource = ' + json.dumps(str(ROOT)) +
            '\n[plugins."agent-coord@personal"]\nenabled = true\n')
        with patch.dict(os.environ, {"CODEX_HOME": str(home)}):
            rpc = CodexRPC(CoordinationStore(self.root / "test.sqlite3"), lambda *args: None)
            rpc_pgid = None
            try:
                result = rpc.request("skills/list", {"cwds": [str(self.root)], "forceReload": True})
                rpc_pgid = rpc.process.pid if rpc.process is not None else None
            finally:
                rpc_pgid = rpc_pgid or (rpc.process.pid if rpc.process is not None else None)
                rpc.close()
                self._reap_process_group(rpc_pgid)
        skills = {skill["name"]: Path(skill["path"]).resolve() for group in result["data"]
                  for skill in group["skills"] if skill["enabled"]}
        for skill in (self.plugin / "skills").iterdir():
            self.assertEqual(skills["agent-coord:" + skill.name],
                             Path(installed["codex"], "skills", skill.name, "SKILL.md").resolve())

    def test_interrupted_cache_exchange_keeps_pinned_hook_and_can_resume(self):
        homes, previous, _ = self.development_homes()
        retained = homes["share"] / "retained/personal" / previous.name
        retained.parent.mkdir(parents=True)
        previous.rename(retained)
        previous.symlink_to(retained)
        with patch.object(tools, "_exchange_paths", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                tools.dev_link(self.plugin, prune=False, **homes)
        self.assertEqual(subprocess.check_output([previous / "scripts/hook"]), b"original hook")
        result = tools.dev_link(self.plugin, prune=False, **homes)
        self.assertEqual(result["repairedCodexDirectories"], 1)
        self.assertFalse(previous.is_symlink())
        self.assertEqual(subprocess.check_output([retained / "scripts/hook"]), b"original hook")
        self.assertEqual(subprocess.check_output([previous / "scripts/hook"]), b"original hook")
        pruned = tools.dev_link(self.plugin, prune=True, **homes)
        self.assertFalse(previous.exists())
        self.assertFalse(retained.exists())
        self.assertTrue(Path(pruned["codex"]).is_dir())

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
        fixture = ("import os,pathlib,shutil,time\n" +
                          "cache=pathlib.Path(os.environ['CODEX_HOME'])/'plugins/cache'\n" +
                          "cache.mkdir(parents=True)\nshutil.rmtree(cache)\n" +
                          "pathlib.Path(" + repr(str(marker)) + ").touch()\ntime.sleep(30)\n")
        binary.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + " -c " + shlex.quote(fixture) + "\n")
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
