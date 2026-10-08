from pathlib import Path
import shutil
import subprocess
import unittest


class ThemeTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is required for theme behavior tests")
    def test_theme_preferences(self):
        result = subprocess.run(
            ["node", "--test", str(Path(__file__).with_name("test_web_theme.js"))],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
