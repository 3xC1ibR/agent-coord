import shutil
import subprocess
import unittest
from pathlib import Path


class LiveRenderingTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is required for live rendering tests")
    def test_streaming_rendering(self):
        result = subprocess.run(
            ["node", "--test", str(Path(__file__).with_suffix(".js"))],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
