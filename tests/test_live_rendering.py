import shutil
import subprocess
import unittest
from pathlib import Path


class LiveRenderingTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is required for live rendering tests")
    def test_streaming_rendering(self):
        result = subprocess.run(
            ["node", "--test", str(Path(__file__).with_suffix(".js")),
             str(Path(__file__).with_name("test_web_timeline.js")),
             str(Path(__file__).with_name("test_response_actions.js")),
             str(Path(__file__).with_name("test_conversation_scroll.js")),
             str(Path(__file__).with_name("test_web_thread_header.js")),
             str(Path(__file__).with_name("test_web_outgoing_messages.js"))],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
