from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))

from agent_coord.antigravity_skills import prepare_skills
from agent_coord.store import CoordinationError


class AntigravitySkillTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.home, self.workspace, self.bundle = [self.root / name for name in ("home", "workspace", "payload/skills")]
        self.workspace.mkdir()
        self.store = SimpleNamespace(database_path=self.root / "state.sqlite3")
        self.skill(self.bundle, "agent-coordination", "Coordinate sessions.")
        self.skill(self.bundle, "dispatch", "Route requests.")

    def skill(self, root, name, description):
        path = root / name / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\nname: {name}\ndescription: {description}\n---\nRead references/details.md.\n")
        return path

    def prepare(self):
        return prepare_skills(self.store, self.workspace, home=self.home, bundled=self.bundle)

    def inventory(self, context):
        result = {}
        for line in context.instructions.splitlines()[1:]:
            if line.startswith("Directory: "):
                directory = Path(line.removeprefix("Directory: "))
            else:
                name, entry = line.removeprefix("- ").split(": ", 1)
                description, path = entry.removesuffix(")").rsplit(" (", 1)
                result[name] = {"description": description, "path": directory / path}
        return result

    def test_symlinked_personal_skill_and_resources_are_readable_real_files(self):
        source = self.skill(self.root / "external-source", "refine-prompt", ">\n  Interview the user.\n  Rewrite their prompt.")
        resource = source.parent / "references/details.md"
        resource.parent.mkdir()
        resource.write_text("Interview steps")
        script = source.parent / "scripts/interview.py"
        script.parent.mkdir()
        script.write_text("print('interview')")
        script.chmod(0o755)
        personal = self.home / ".agents/skills"
        personal.mkdir(parents=True)
        (personal / "refine-prompt").symlink_to(source.parent, target_is_directory=True)
        context = self.prepare()
        inventory = self.inventory(context)
        copied = Path(inventory["refine-prompt"]["path"])
        allowed = self.root / "providers/antigravity/profile/antigravity-acp"
        self.assertTrue(copied.resolve().is_relative_to(allowed))
        self.assertFalse(copied.parent.is_symlink())
        self.assertEqual(copied.read_bytes(), source.read_bytes())
        self.assertEqual((copied.parent / "references/details.md").read_text(), "Interview steps")
        self.assertTrue((copied.parent / "scripts/interview.py").stat().st_mode & 0o111)
        self.assertEqual(inventory["refine-prompt"]["description"], "Interview the user.")
        self.assertTrue((context.coordination_skill.parent.parent / "dispatch/SKILL.md").is_file())

    def test_updates_publish_new_copy_without_changing_existing_conversations(self):
        source = self.skill(self.home / ".agents/skills", "refine-prompt", "Original")
        first = self.prepare()
        first_path = Path(self.inventory(first)["refine-prompt"]["path"])
        original = first_path.read_bytes()
        source.write_text(source.read_text().replace("Original", "Revised"))
        second = self.prepare()
        second_path = Path(self.inventory(second)["refine-prompt"]["path"])
        self.assertNotEqual(first_path, second_path)
        self.assertEqual(first_path.read_bytes(), original)
        self.assertEqual(second_path.read_bytes(), source.read_bytes())
        self.assertEqual(self.prepare(), second)

    def test_workspace_skill_takes_precedence_and_existing_native_files_are_preserved(self):
        self.skill(self.workspace / ".agents/skills", "refine-prompt", "Workspace")
        self.skill(self.home / ".agents/skills", "refine-prompt", "Personal")
        self.skill(self.home / ".gemini/config/skills", "native-skill", "Native")
        native = self.root / "providers/antigravity/profile/config/skills/user-skill/SKILL.md"
        native.parent.mkdir(parents=True)
        native.write_text("Preserve me")
        inventory = self.inventory(self.prepare())
        self.assertEqual(inventory["refine-prompt"]["description"], "Workspace")
        self.assertIn("native-skill", inventory)
        self.assertEqual(native.read_text(), "Preserve me")

    def test_concurrent_connections_publish_complete_identical_snapshots(self):
        self.skill(self.home / ".agents/skills", "refine-prompt", "Interview")
        with ThreadPoolExecutor(max_workers=4) as workers:
            contexts = list(workers.map(lambda _: self.prepare(), range(8)))
        self.assertTrue(all(context == contexts[0] for context in contexts))
        for item in self.inventory(contexts[0]).values():
            self.assertTrue(Path(item["path"]).is_file())

    def test_missing_required_coordination_skill_fails_before_submission(self):
        (self.bundle / "agent-coordination/SKILL.md").unlink()
        with self.assertRaisesRegex(CoordinationError, "coordination skill is missing"):
            self.prepare()

    def test_directory_link_cycle_does_not_hang_skill_preparation(self):
        source = self.skill(self.home / ".agents/skills", "refine-prompt", "Interview")
        (source.parent / "cycle").symlink_to(source.parent, target_is_directory=True)
        with self.assertRaisesRegex(CoordinationError, "directory link cycle"):
            self.prepare()


if __name__ == "__main__":
    unittest.main()
