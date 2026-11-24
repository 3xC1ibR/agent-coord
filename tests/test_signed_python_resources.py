"""Bytecode policy survives native provider environment scrubbing."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"
sys.path.insert(0, str(SCRIPTS))
from agent_coord import antigravity, jobs, kimi_setup, managed_pty, muse_setup, desktop_runtime


class PythonChildLaunchFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="signed Python resources ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.interpreter = self.root / "python executable"
        self.interpreter.symlink_to(sys.executable)
        self.hook = self.root / "agent-coord-hook"
        (self.root / "resource_probe.py").write_text("VALUE = 1\n")
        self.hook.write_text("import json, sys, resource_probe\nprint(json.dumps({'bytecode_disabled':sys.dont_write_bytecode,'executable':sys.executable}))\n")
        self.store = SimpleNamespace(database_path=self.root / "state.sqlite3")
        self.environment = {"HOME": str(self.root), "PATH": os.defpath,
            "AGENT_COORD_CLIENT": "kimi", "AGENT_COORD_SESSION_ID": "private-probe"}

    def check_child(self, command):
        result = subprocess.run(command, shell=True, cwd=self.root, env=self.environment,
            text=True, capture_output=True, check=True, timeout=15)
        report = json.loads(result.stdout)
        self.assertTrue(report["bytecode_disabled"], command)
        self.assertTrue(Path(report["executable"]).samefile(self.interpreter))
        self.assertFalse((self.root / "__pycache__").exists())



class PythonChildLaunchTests(PythonChildLaunchFixture):
    def test_muse_generated_hook_survives_a_scrubbed_environment(self):
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root / "config")}), \
             patch.object(muse_setup, "cli_path", return_value=self.hook.with_name("agent-coord")), \
             patch.object(sys, "executable", str(self.interpreter)):
            hooks = json.loads(muse_setup.hook_config(self.store, "private-probe").removeprefix("hooks="))
        self.check_child(hooks["UserPromptSubmit"][-1]["hooks"][0]["command"])

    def test_kimi_normal_setup_replaces_an_old_managed_command_and_keeps_user_hooks(self):
        home = self.root / "kimi"
        home.mkdir()
        old_command = shlex.join([str(self.interpreter), str(self.hook)])
        before = 'default_model = "mine"\n[[hooks]]\nevent = "Stop"\ncommand = "true"\n'
        (home / "config.toml").write_text(before + kimi_setup.START + '\n[[hooks]]\nevent = "UserPromptSubmit"\ncommand = ' + json.dumps(old_command) + '\n' + kimi_setup.END + '\n')
        with patch.dict(os.environ, {"KIMI_CODE_HOME": str(home)}), \
             patch.object(kimi_setup, "cli_path", return_value=self.hook.with_name("agent-coord")), \
             patch.object(sys, "executable", str(self.interpreter)):
            kimi_setup.ensure_hooks()
            configured = (home / "config.toml").read_text()
            kimi_setup.ensure_hooks()
        self.assertEqual((home / "config.toml").read_text(), configured)
        self.assertTrue(configured.startswith(before))
        command = json.loads(next(line[len("command = "):] for line in configured.splitlines() if line.startswith('command = "if ')))
        self.check_child(command)

    def test_antigravity_normal_setup_replaces_old_commands_without_removing_user_hooks(self):
        profile = self.root / "profile"
        (profile / "config").mkdir(parents=True)
        path = profile / "config/hooks.json"
        path.write_text(json.dumps({"mine": {"Stop": []}, "ribbon-field-coordination": {"PreToolUse": []}}))
        with patch.object(antigravity, "cli_path", return_value=self.hook.with_name("agent-coord")), \
             patch.object(sys, "executable", str(self.interpreter)):
            antigravity.ensure_coordination_hooks(profile)
            configured = path.read_text()
            antigravity.ensure_coordination_hooks(profile)
        self.assertEqual(path.read_text(), configured)
        value = json.loads(configured)
        self.assertEqual(value["mine"], {"Stop": []})
        self.check_child(value["ribbon-field-coordination"]["PreToolUse"][0]["hooks"][0]["command"])

    def test_supervisor_children_keep_normal_executable_and_disable_bytecode_without_environment(self):
        resources = self.root / "resources"
        resources.mkdir()
        (resources / "backend.json").write_text(json.dumps({"python": str(self.interpreter)}))
        with patch.object(sys, "executable", str(self.interpreter)):
            commands = [jobs.build_supervisor_command(self.store, "job", "token"),
                managed_pty.build_supervisor_command(self.store, "delegation"), desktop_runtime.payload_command(resources)]
        for command in commands:
            with self.subTest(command=command):
                # Execute the actual interpreter prefix, replacing only the job
                # body so the regression never starts an agent or supervisor.
                index = command.index("-m") if "-m" in command else next(i for i, part in enumerate(command[1:], 1) if not part.startswith("-"))
                self.check_child(shlex.join([*command[:index], str(self.hook)]))

class PackagedSupervisorEntryTests(PythonChildLaunchFixture):
    def test_packaged_supervisor_uses_python_source_beside_the_shell_launcher(self):
        backend = self.root / "backend"
        (backend / "agent_coord").mkdir(parents=True)
        (backend / "agent-coord").write_text("#!/bin/sh\nexit 99\n")
        (backend / "agent-coord.py").write_text(self.hook.read_text())
        (backend / "resource_probe.py").write_text("VALUE=1\n")
        with patch.object(managed_pty, "__file__", str(backend / "agent_coord/managed_pty.py")), \
             patch.object(sys, "executable", str(self.interpreter)):
            command = managed_pty.build_supervisor_command(self.store, "unused")
        self.assertIn(str((backend / "agent-coord.py").resolve()), command)
        self.check_child(shlex.join(command))

    def test_antigravity_browser_helper_does_not_depend_on_inherited_python_flags(self):
        captured = {}
        def initialize(connection, callback, **options):
            captured.update(options)
            connection.channel = None
        with patch.object(antigravity, "resolve_runtime", return_value=(self.root / "native", self.root / "harness")), \
             patch.object(antigravity.ACPConnection, "__init__", initialize), \
             patch.object(sys, "executable", str(self.interpreter)):
            connection = antigravity.AntigravityConnection(self.store, lambda event: None, {"cwd": str(self.root)})
        try:
            command = shlex.split(captured['env']['BROWSER'])
            self.check_child(shlex.join([*command[:command.index('-c')], str(self.hook)]))
        finally:
            connection.temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
