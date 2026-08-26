from __future__ import annotations

import json
import unittest
from pathlib import Path


class PluginLayoutTests(unittest.TestCase):
    def test_codex_and_claude_hook_manifests_cover_the_same_events(self) -> None:
        plugin = Path(__file__).resolve().parents[1] / "plugins/agent-coord"
        codex = json.loads((plugin / "hooks.json").read_text(encoding="utf-8"))
        claude = json.loads(
            (plugin / "hooks/hooks.json").read_text(encoding="utf-8")
        )

        self.assertEqual(set(codex["hooks"]), set(claude["hooks"]))
        for event in codex["hooks"]:
            codex_matchers = [item.get("matcher") for item in codex["hooks"][event]]
            claude_matchers = [
                item.get("matcher") for item in claude["hooks"][event]
            ]
            self.assertEqual(codex_matchers, claude_matchers)
        codex_commands = [
            hook["command"]
            for groups in codex["hooks"].values()
            for group in groups
            for hook in group["hooks"]
        ]
        self.assertTrue(
            all("AGENT_COORD_CLIENT=codex" in command for command in codex_commands)
        )


if __name__ == "__main__":
    unittest.main()
