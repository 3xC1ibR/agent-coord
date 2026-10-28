from __future__ import annotations

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import plistlib
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("macos_distribution", ROOT / "desktop/macos/distribution.py")
distribution = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(distribution)
sys.path.insert(0, str(ROOT / "desktop/macos"))
import sparkle
import verify_distribution


class PortableConfigurationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.app = Path(temporary.name) / "Ribbon Field.app"
        resources = self.app / "Contents/Resources"
        resources.mkdir(parents=True)
        self.runtime = sparkle.runtime_metadata("1.2.3", "42")
        self.info = {"CFBundleShortVersionString": "1.2.3", "CFBundleVersion": "42", **self.runtime}
        self.config = {"python": "runtime/bin/python3", "database": None, "path": None, **self.runtime}
        self.backend = resources / "backend.json"
        self.write()

    def write(self):
        self.backend.write_text(json.dumps(self.config))
        (self.app / "Contents/Info.plist").write_bytes(plistlib.dumps(self.info))

    def test_portable_configuration_matches_shared_runtime_contract(self):
        self.assertEqual(verify_distribution.portable_configuration(self.app), self.config)

    def test_portable_configuration_rejects_builder_paths_and_extra_or_missing_settings(self):
        valid = dict(self.config)
        for field, value in [("python", "/builder/python3"), ("python", "../python3"),
                             ("database", "/builder/state.sqlite3"), ("path", "/builder/bin"),
                             ("unexpected", True)]:
            with self.subTest(field=field, value=value):
                self.config = {**valid, field: value}
                self.write()
                with self.assertRaises(ValueError):
                    verify_distribution.portable_configuration(self.app)
        for field in valid:
            with self.subTest(missing=field):
                self.config = {k: v for k, v in valid.items() if k != field}
                self.write()
                with self.assertRaises(ValueError):
                    verify_distribution.portable_configuration(self.app)

    def test_runtime_metadata_must_match_info_and_current_contract(self):
        valid = dict(self.config)
        for field in sparkle.RUNTIME_KEYS:
            changed = "other" if field == "runtime_version" else 99
            for change_info in (False, True):
                with self.subTest(field=field, change_info=change_info):
                    self.config = {**valid, field: changed}
                    self.info = {"CFBundleShortVersionString": "1.2.3", "CFBundleVersion": "42", **self.runtime}
                    if change_info:
                        self.info[field] = changed
                    self.write()
                    with self.assertRaises(ValueError):
                        verify_distribution.portable_configuration(self.app)

    def test_malformed_config_is_rejected_before_running_bundle_code(self):
        for value in (None, [], {**self.config, "runtime_protocol": True},
                      {**self.config, "runtime_schema_min": 2, "runtime_schema_max": 1}):
            with self.subTest(value=value):
                self.backend.write_text(json.dumps(value))
                with mock.patch.object(verify_distribution.subprocess, "run") as run:
                    with self.assertRaises(ValueError):
                        verify_distribution.verify(self.app)
                    run.assert_not_called()
        self.backend.write_text("{broken")
        with self.assertRaises(ValueError):
            verify_distribution.portable_configuration(self.app)


class RuntimeLicenseTests(unittest.TestCase):
    def archive(self, entries):
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w") as archive:
            for name, kind, content in entries:
                member = tarfile.TarInfo(name)
                member.type = kind
                if kind == tarfile.REGTYPE:
                    payload = content.encode()
                    member.size = len(payload)
                    archive.addfile(member, io.BytesIO(payload))
                else:
                    member.linkname = content
                    archive.addfile(member)
        return data.getvalue()

    def extract(self, entries, root, *, fail_decoder=False):
        data = entries if isinstance(entries, bytes) else self.archive(entries)
        scratch_paths = []
        def decode(command, **kwargs):
            self.assertEqual(command[:4], ["/test/bin/zstd", "--decompress", "--stdout", "--"])
            self.assertTrue(kwargs["check"])
            scratch_paths.append(Path(kwargs["stdout"].name).parent)
            kwargs["stdout"].write(data)
            if fail_decoder:
                raise subprocess.CalledProcessError(1, command, stderr=b"decoder failure")
            return subprocess.CompletedProcess(command, 0)
        try:
            with mock.patch.object(distribution.shutil, "which", return_value="/test/bin/zstd"), \
                 mock.patch.object(distribution.subprocess, "run", side_effect=decode):
                distribution.extract_runtime_licenses(root / "full.tar.zst", root / "licenses")
        finally:
            for scratch in scratch_paths:
                self.assertFalse(scratch.exists(), "decoded archive and extraction scratch must be removed")

    def good_entries(self):
        return [("python/PYTHON.json", tarfile.REGTYPE, '{"version":"test"}'),
                ("python/licenses", tarfile.DIRTYPE, ""),
                ("python/licenses/LICENSE.test", tarfile.REGTYPE, "license text")]

    def test_extracts_only_regular_licenses_and_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entries = self.good_entries() + [("python/bin/python", tarfile.REGTYPE, "not a license"),
                                             ("unrelated/../../escape", tarfile.REGTYPE, "excluded")]
            self.extract(entries, root)
            self.assertEqual({p.name for p in (root / "licenses").iterdir()}, {"PYTHON.json", "LICENSE.test"})
            self.assertEqual((root / "licenses/LICENSE.test").read_text(), "license text")

    def test_rejects_selected_traversal_links_devices_and_duplicates(self):
        bad = [("python/licenses/../../escape", tarfile.REGTYPE, "escape"),
               ("python/licenses/link", tarfile.SYMTYPE, "../../escape"),
               ("python/licenses/hard", tarfile.LNKTYPE, "python/PYTHON.json"),
               ("python/licenses/pipe", tarfile.FIFOTYPE, ""),
               ("python/licenses/LICENSE.test", tarfile.REGTYPE, "duplicate"),
               ("python/licenses/PYTHON.json", tarfile.REGTYPE, "provenance collision")]
        for entry in bad:
            with self.subTest(entry=entry), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                with self.assertRaises(ValueError):
                    self.extract(self.good_entries() + [entry], root)
                self.assertFalse((root / "licenses").exists())
                self.assertFalse((root / "escape").exists())

    def test_missing_required_license_payloads_are_rejected(self):
        for entries in ([], self.good_entries()[:1], self.good_entries()[1:]):
            with self.subTest(entries=entries), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                with self.assertRaisesRegex(ValueError, "missing licenses"):
                    self.extract(entries, root)
                self.assertFalse((root / "licenses").exists())

    def test_decoder_failure_is_fatal_even_after_writing_valid_tar(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(subprocess.CalledProcessError):
                self.extract(self.good_entries(), root, fail_decoder=True)
            self.assertFalse((root / "licenses").exists())

    def test_invalid_decoded_tar_cleans_scratch_without_publishing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(tarfile.ReadError):
                self.extract(b"invalid tar", root)
            self.assertFalse((root / "licenses").exists())

    def test_corrupt_full_archive_is_rejected_before_decoder_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            filename = "runtime-install_only_stripped.tar.gz"
            (root / filename).write_bytes(b"install")
            (root / "runtime-pgo+lto-full.tar.zst").write_bytes(b"corrupted")
            spec = {"filename": filename, "url": "https://example.invalid/" + filename,
                    "sha256": hashlib.sha256(b"install").hexdigest(),
                    "full_sha256": hashlib.sha256(b"expected full archive").hexdigest()}
            with mock.patch.object(distribution, "runtime_spec", return_value=spec), \
                 mock.patch.object(distribution, "extract_runtime"), \
                 mock.patch.object(distribution, "extract_runtime_licenses") as extract:
                with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                    distribution.bundle_runtime(root / "resources", "x86_64", root)
                extract.assert_not_called()

    def test_missing_decoder_fails_clearly_without_extracting(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(distribution.shutil, "which", return_value=None), \
             mock.patch.object(distribution.subprocess, "run") as run:
            root = Path(directory)
            with self.assertRaisesRegex(RuntimeError, "requires zstd on PATH"):
                distribution.extract_runtime_licenses(root / "archive", root / "licenses")
            run.assert_not_called()
            self.assertFalse((root / "licenses").exists())


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

    def test_sparkle_helper_bundles_are_sealed_inside_out(self):
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory) / "Ribbon Field.app"
            framework = app / "Contents/Frameworks/Sparkle.framework"
            helper = framework / "Versions/B/Updater.app"
            executable = helper / "Contents/MacOS/Updater"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(bytes.fromhex("cffaedfe") + b"native")
            (framework / "Versions/Current").symlink_to("B", target_is_directory=True)
            with mock.patch.object(distribution.subprocess, "run") as run:
                distribution.sign_app(app, "Developer ID Application: Test")
            paths = [call.args[0][-1] for call in run.call_args_list]
            self.assertLess(paths.index(str(executable)), paths.index(str(helper)))
            self.assertLess(paths.index(str(helper)), paths.index(str(framework)))
            self.assertLess(paths.index(str(framework)), paths.index(str(app)))
            self.assertEqual(paths.count(str(executable)), 1)
            self.assertFalse(any("Versions/Current/" in path for path in paths))

    def test_notary_rejection_is_not_reported_as_success(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "app.zip"
            response = subprocess.CompletedProcess([], 0, json.dumps({"status": "Invalid", "id": "submission"}))
            with mock.patch.object(distribution.subprocess, "run", return_value=response):
                with self.assertRaisesRegex(ValueError, "not accepted"):
                    distribution.notarize(artifact, "profile")
            self.assertEqual(json.loads(artifact.with_suffix(".zip.notary.json").read_text())["status"], "Invalid")

    def test_stapler_retries_only_transient_connection_loss(self):
        transient = subprocess.CompletedProcess([], 68, "NSURLErrorDomain Code=-1005", "")
        success = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(distribution.subprocess, "run", side_effect=[transient, success]) as run, mock.patch.object(distribution.time, "sleep") as sleep:
            distribution.run_stapler("validate", Path("test.dmg"))
            self.assertEqual(run.call_count, 2)
            sleep.assert_called_once_with(10)
        for result, attempts in [(transient, 5), (subprocess.CompletedProcess([], 65, "invalid ticket", ""), 1)]:
            with mock.patch.object(distribution.subprocess, "run", return_value=result) as run, mock.patch.object(distribution.time, "sleep") as sleep:
                with self.assertRaises(subprocess.CalledProcessError):
                    distribution.run_stapler("validate", Path("test.dmg"))
                self.assertEqual(run.call_count, attempts)
                self.assertEqual(sleep.call_count, attempts - 1)


if __name__ == "__main__":
    unittest.main()
