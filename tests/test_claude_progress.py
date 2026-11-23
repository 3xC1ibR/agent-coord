import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import uuid
from unittest.mock import patch

from agent_coord.claude_progress import TranscriptProgress, read_process_table, tool_cpu_times


class NativeProgressTests(unittest.TestCase):
    def test_process_sample_attributes_cpu_and_excludes_checkpoint_helper_and_zombie(self):
        table = """101 1 0:25.04 Ss claude
102 101 0:01.20 S /runtime/python3
103 102 01:15.04 S /runtime/python3
104 101 00:01.03 Z /bin/bash
110 1 00:00.01 S /runtime/python3
"""
        arguments = """102 /runtime/python3 -I -B -u -c import sys; from agent_coord.checkpoint_mcp import main; raise SystemExit(main())
103 /runtime/python3 /tmp/tool.py
"""
        samples = [subprocess.CompletedProcess([], 0, table), subprocess.CompletedProcess([], 0, arguments)]
        with patch("agent_coord.claude_progress.subprocess.run", side_effect=samples) as command:
            observed = read_process_table({101})
        self.assertEqual(command.call_args_list[1].args[0][3], "102,103")
        tools = tool_cpu_times(observed, 101)
        self.assertEqual(set(tools), {103})
        self.assertAlmostEqual(tools[103], 75.04)
        self.assertIsNone(tool_cpu_times(observed, 999))

    def test_failed_process_observation_is_unknown(self):
        with patch("agent_coord.claude_progress.subprocess.run", side_effect=subprocess.TimeoutExpired("ps", 2)):
            self.assertIsNone(read_process_table({101}))

    def test_only_growth_in_the_selected_native_session_advances_progress(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": temporary}):
            identity = str(uuid.uuid4())
            project = Path(temporary) / "projects" / "workspace"
            nested = project / identity / "subagents"
            nested.mkdir(parents=True)
            main = project / (identity + ".jsonl")
            main.write_bytes(b"first\n")
            foreign = project / "unrelated.jsonl"
            foreign.write_bytes(b"unrelated")
            os.utime(main, (10, 10))
            observations = TranscriptProgress(identity)
            self.assertEqual(observations.sample(), 10)
            os.utime(main, (20, 20))
            self.assertIsNone(observations.sample())
            (nested / "unrelated.jsonl").symlink_to(foreign)
            self.assertIsNone(observations.sample())
            subagent = nested / "agent-one.jsonl"
            subagent.write_bytes(b"result\n")
            os.utime(subagent, (25, 25))
            self.assertEqual(observations.sample(), 25)
            subagent.unlink()
            self.assertIsNone(observations.sample())
            with main.open("ab") as stream:
                stream.write(b"next\n")
            os.utime(main, (30, 30))
            self.assertEqual(observations.sample(), 30)
