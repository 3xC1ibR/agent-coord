import shutil
import subprocess
import unittest
from pathlib import Path


class ComposerDeliveryTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is required for composer delivery tests")
    def test_durable_composer_delivery(self):
        result = subprocess.run(
            ["node", "--test", str(Path(__file__).with_suffix(".js"))],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
