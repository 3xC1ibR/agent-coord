from __future__ import annotations

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("macos_distribution", ROOT / "desktop/macos/distribution.py")
distribution = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(distribution)


class DistributionTests(unittest.TestCase):
    def test_ad_hoc_signing_does_not_enable_team_based_library_validation(self):
        self.assertNotIn("runtime", distribution.signing_command("-"))
        self.assertNotIn("--timestamp", distribution.signing_command("-"))

    def test_cached_runtime_is_rejected_if_checksum_changed(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            archive = cache / "runtime.tar.gz"
            archive.write_bytes(b"corrupted archive")
            spec = {"filename": archive.name, "sha256": hashlib.sha256(b"original").hexdigest()}
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                distribution.verified_archive(spec, cache)

    def test_failed_download_does_not_publish_or_keep_partial_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            spec = {"filename": "runtime.tar.gz", "sha256": "0" * 64, "url": "https://example.invalid/python"}
            with mock.patch.object(distribution.urllib.request, "urlopen", return_value=io.BytesIO(b"wrong")):
                with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                    distribution.verified_archive(spec, Path(directory))
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_runtime_extraction_preserves_relative_python_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "runtime.tar.gz"
            with tarfile.open(archive, "w:gz") as output:
                executable = tarfile.TarInfo("python/bin/python3.12")
                executable.size, executable.mode = 6, 0o755
                output.addfile(executable, io.BytesIO(b"python"))
                link = tarfile.TarInfo("python/bin/python3")
                link.type, link.linkname = tarfile.SYMTYPE, "python3.12"
                output.addfile(link)
            distribution.extract_runtime(archive, root / "runtime")
            self.assertTrue((root / "runtime/bin/python3").is_symlink())
            self.assertEqual((root / "runtime/bin/python3").read_bytes(), b"python")

    def test_runtime_rejects_paths_and_links_outside_its_root(self):
        for name, target in (("../escape", None), ("/escape", None),
                             ("python/bin/link", "../../escape"),
                             ("python/bin/link", "/usr/bin/python3")):
            with self.subTest(name=name, target=target), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                archive = root / "runtime.tar.gz"
                with tarfile.open(archive, "w:gz") as output:
                    member = tarfile.TarInfo(name)
                    if target:
                        member.type, member.linkname = tarfile.SYMTYPE, target
                    output.addfile(member)
                with self.assertRaisesRegex(ValueError, "Unsafe runtime archive"):
                    distribution.extract_runtime(archive, root / "runtime")
                self.assertFalse((root / "runtime").exists())

    def test_nested_native_libraries_are_signed_before_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory) / "Ribbon Field.app"
            library = app / "Contents/Resources/runtime/lib/libpython.dylib"
            library.parent.mkdir(parents=True)
            library.write_bytes(bytes.fromhex("cffaedfe") + b"library")
            library.with_name("alias.dylib").symlink_to(library.name)
            library.with_name("LICENSE").write_text("license")
            identity = "Developer ID Application: Example (TEAM)"
            with mock.patch.object(distribution.subprocess, "run") as run:
                distribution.sign_app(app, identity)
            commands = [call.args[0] for call in run.call_args_list]
            self.assertEqual(len(commands), 3)
            self.assertEqual(commands[0][-1], str(library))
            self.assertIn("--timestamp", commands[0])
            self.assertIn("runtime", commands[0])
            self.assertEqual(commands[1][-1], str(app))
            self.assertEqual(commands[2][1:4], ["--verify", "--deep", "--strict"])

    def test_notary_rejection_is_not_reported_as_success(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "app.zip"
            response = subprocess.CompletedProcess([], 0, json.dumps({"status": "Invalid", "id": "submission"}))
            with mock.patch.object(distribution.subprocess, "run", return_value=response):
                with self.assertRaisesRegex(ValueError, "not accepted"):
                    distribution.notarize(artifact, "profile")
            self.assertEqual(json.loads(artifact.with_suffix(".zip.notary.json").read_text())["status"], "Invalid")


if __name__ == "__main__":
    unittest.main()
