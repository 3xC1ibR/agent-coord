from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("macos_release", ROOT / "scripts/publish_macos_release.py")
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)
COMMIT = "a" * 40


class MacOSReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        for arch in release.ARCHITECTURES:
            name = f"Ribbon-Field-1.2.3-{arch}.dmg"
            image = self.directory / name
            image.write_bytes(b"test disk image " + arch.encode())
            sha = release.file_digest(image)
            self.write(name + ".json", {
                "version": "1.2.3", "architecture": arch, "sha256": sha,
                "notarized": True, "signing_identity": "Developer ID Application: Test (TEAM)",
            })
            (self.directory / (name + ".sha256")).write_text(f"{sha}  {name}\n")
            self.write(name + ".notary.json", {"status": "Accepted"})
            self.write(name + ".validation.json", {
                "architecture": arch, "disk_image": "/build/" + name,
                "relocated": True, "backend": True, "cli": True,
            })
            self.write(name + ".provenance.json", {"version": "1.2.3", "source_commit": COMMIT})

    def write(self, name, value):
        (self.directory / name).write_text(json.dumps(value))

    def files(self):
        return release.release_files(self.directory, "1.2.3", COMMIT)

    def test_valid_release_contains_both_architectures_and_exact_urls(self):
        files, manifest = self.files()
        self.assertEqual(len(files), 8)
        self.assertEqual(set(manifest["downloads"]), {"arm64", "x86_64"})
        self.assertEqual(manifest["source_commit"], COMMIT)
        self.assertEqual(manifest["downloads"]["arm64"]["url"],
                         "https://downloads.ribbonfield.com/releases/1.2.3/Ribbon-Field-1.2.3-arm64.dmg")

    def test_corrupt_image_is_rejected(self):
        (self.directory / "Ribbon-Field-1.2.3-arm64.dmg").write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "Checksum mismatch"):
            self.files()

    def test_unsigned_rejected_notary_and_failed_validation_are_rejected(self):
        name = "Ribbon-Field-1.2.3-arm64.dmg"
        cases = [(".json", "notarized", False), (".json", "signing_identity", "-"),
                 (".notary.json", "status", "Invalid"), (".validation.json", "backend", False),
                 (".validation.json", "architecture", "x86_64"),
                 (".provenance.json", "source_commit", "b" * 40)]
        for suffix, key, value in cases:
            with self.subTest(suffix=suffix, key=key):
                path = self.directory / (name + suffix)
                original = path.read_text()
                data = json.loads(original)
                data[key] = value
                path.write_text(json.dumps(data))
                with self.assertRaises(ValueError):
                    self.files()
                path.write_text(original)

    def test_missing_architecture_and_symlink_are_rejected(self):
        path = self.directory / "Ribbon-Field-1.2.3-x86_64.dmg"
        path.unlink()
        with self.assertRaisesRegex(ValueError, "Missing or unsafe"):
            self.files()
        path.symlink_to(self.directory / "Ribbon-Field-1.2.3-arm64.dmg")
        with self.assertRaisesRegex(ValueError, "Missing or unsafe"):
            self.files()

    def test_path_injection_and_non_commit_are_rejected(self):
        for version, commit in (("../1.2.3", COMMIT), ("1.2.3", "main")):
            with self.subTest(version=version, commit=commit), self.assertRaises(ValueError):
                release.release_files(self.directory, version, commit)

    def test_preflight_and_completion_marker_order(self):
        files, manifest = self.files()
        r2, events = mock.Mock(), []
        r2.exists_identical.side_effect = lambda key, path: events.append(("inspect", key)) or False
        r2.put.side_effect = lambda key, path: events.append(("put", key))
        with mock.patch.object(release, "verify_download", side_effect=lambda key, path: events.append(("verify", key))):
            release.publish(files, manifest, r2)
        self.assertEqual([action for action, _ in events[:9]], ["inspect"] * 9)
        self.assertEqual(events[-2:], [("put", "releases/1.2.3/release.json"),
                                      ("verify", "releases/1.2.3/release.json")])

    def test_conflicting_release_uploads_nothing(self):
        files, manifest = self.files()
        r2 = mock.Mock()
        r2.exists_identical.side_effect = [False, False, ValueError("existing release differs")]
        with self.assertRaisesRegex(ValueError, "differs"):
            release.publish(files, manifest, r2)
        r2.put.assert_not_called()

    def test_partial_upload_can_resume_without_replacing_existing_bytes(self):
        files, manifest = self.files()
        r2 = mock.Mock()
        r2.exists_identical.side_effect = [True] * 8 + [False]
        with mock.patch.object(release, "verify_download") as verify:
            release.publish(files, manifest, r2)
        self.assertEqual(r2.put.call_count, 1)
        self.assertEqual(r2.put.call_args.args[0], "releases/1.2.3/release.json")
        self.assertEqual(verify.call_count, 9)

    def test_failed_download_does_not_publish_completion_marker(self):
        files, manifest = self.files()
        r2 = mock.Mock()
        r2.exists_identical.return_value = False
        with mock.patch.object(release, "verify_download", side_effect=ValueError("bad download")):
            with self.assertRaisesRegex(ValueError, "bad download"):
                release.publish(files, manifest, r2)
        self.assertEqual(r2.put.call_count, 1)
        self.assertNotEqual(r2.put.call_args.args[0], "releases/1.2.3/release.json")

    def test_public_download_checks_actual_bytes(self):
        path = self.directory / "Ribbon-Field-1.2.3-arm64.dmg"
        with mock.patch.object(release.urllib.request, "urlopen", return_value=io.BytesIO(b"wrong")):
            with self.assertRaisesRegex(ValueError, "Public download checksum mismatch"):
                release.verify_download("key", path)

    def test_r2_errors_fail_closed_and_only_missing_keys_allow_upload(self):
        r2 = release.R2()
        path = self.directory / "Ribbon-Field-1.2.3-arm64.dmg"
        for error in ("(403) Forbidden", "(500) InternalError", "connection timed out"):
            with mock.patch.object(r2, "run", return_value=subprocess.CompletedProcess([], 1, "", error)):
                with self.assertRaisesRegex(RuntimeError, "Cannot inspect"):
                    r2.exists_identical("key", path)
        with mock.patch.object(r2, "run", return_value=subprocess.CompletedProcess([], 1, "", "(404) Not Found")):
            self.assertFalse(r2.exists_identical("key", path))

    def test_existing_object_is_compared_by_content_not_metadata(self):
        r2 = release.R2()
        path = self.directory / "Ribbon-Field-1.2.3-arm64.dmg"
        def run(*args):
            if args[0] == "get-object":
                Path(args[-1]).write_bytes(b"different published bytes")
            return subprocess.CompletedProcess([], 0, "{}", "")
        with mock.patch.object(r2, "run", side_effect=run):
            with self.assertRaisesRegex(ValueError, "Refusing to replace"):
                r2.exists_identical("key", path)

    def test_put_uses_atomic_create_and_reports_conflicts(self):
        r2 = release.R2()
        path = self.directory / "Ribbon-Field-1.2.3-arm64.dmg"
        with mock.patch.object(r2, "run", return_value=subprocess.CompletedProcess([], 1, "", "(412) PreconditionFailed")) as run:
            with self.assertRaisesRegex(RuntimeError, "Upload failed"):
                r2.put("key", path)
        args = run.call_args.args
        self.assertEqual(args[args.index("--if-none-match") + 1], "*")


if __name__ == "__main__":
    unittest.main()
