import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("macos_asset_builder", ROOT / "desktop/macos/build.py")
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


class CompiledWorkspaceAssetsTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.web = Path(directory.name)
        self.dist = self.web / "dist"
        self.dist.mkdir()
        self.assets = {"js": b"const compiledWorkspace = true;", "css": b"body{color:currentColor}"}
        self.filenames = {
            extension: f"app-{hashlib.sha256(data).hexdigest()[:20]}.{extension}"
            for extension, data in self.assets.items()
        }
        for extension, data in self.assets.items():
            (self.dist / self.filenames[extension]).write_bytes(data)
        self.index = (
            f'<link rel="stylesheet" href="/dist/{self.filenames["css"]}">'
            f'<link rel="preload" as="script" href="/dist/{self.filenames["js"]}">'
            f'<script defer src="/dist/{self.filenames["js"]}"></script>'
        ).encode()
        (self.dist / "index.html").write_bytes(self.index)

    def test_predecessor_aliases_match_canonical_distribution(self):
        snapshot = {}
        builder.stage_legacy_web_entry(self.web, snapshot)
        legacy = (self.web / "index.html").read_bytes()
        self.assertEqual(legacy.count(b'"/app.js"'), 2)
        self.assertIn(b'"/styles.css"', legacy)
        self.assertNotIn(b"/dist/", legacy)
        self.assertEqual((self.dist / "index.html").read_bytes(), self.index)
        for extension, alias in (("js", "app.js"), ("css", "styles.css")):
            self.assertEqual((self.web / alias).read_bytes(), self.assets[extension])
            self.assertEqual((self.dist / self.filenames[extension]).read_bytes(), self.assets[extension])
        for name in ("index.html", "app.js", "styles.css"):
            self.assertEqual(snapshot["agent_coord/web/" + name], hashlib.sha256((self.web / name).read_bytes()).hexdigest())

    def test_rejects_a_content_hash_that_does_not_match_its_filename(self):
        (self.dist / self.filenames["js"]).write_bytes(b"corrupted bundle")
        with self.assertRaisesRegex(ValueError, "does not match its content hash"):
            builder.stage_legacy_web_entry(self.web, {})
