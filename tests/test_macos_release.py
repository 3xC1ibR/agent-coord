from __future__ import annotations

import importlib.util
import base64
import hashlib
import plistlib
import zipfile
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
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
            self.write(name + ".provenance.json", {"version": "1.2.3", "source_commit": COMMIT, "build_number": "1", "run_url": "https://github.com/3xC1ibR/agent-coord/actions/runs/123"})

    def write(self, name, value):
        (self.directory / name).write_text(json.dumps(value))

    def files(self):
        return release.release_files(self.directory, "1.2.3", COMMIT)

    def add_updates(self):
        public = base64.b64encode(b"p" * 32).decode()
        signature = base64.b64encode(b"s" * 64).decode()
        for arch in release.ARCHITECTURES:
            archive = self.directory / f"Ribbon-Field-1.2.3-{arch}.zip"
            runtime = release.sparkle.runtime_metadata("1.2.3", "1")
            url = f"{release.DOWNLOAD_URL}/updates/alpha/{arch}.xml"
            info = {"CFBundleIdentifier": "com.agentcoord.desktop", "CFBundleVersion": "1",
                    "CFBundleShortVersionString": "1.2.3", "SUPublicEDKey": public,
                    "SUFeedURL": url, "SURequireSignedFeed": True, "SUVerifyUpdateBeforeExtraction": True,
                    "SUSignedFeedFailureExpirationInterval": 0, **runtime}
            with zipfile.ZipFile(archive, "w") as bundle:
                prefix = "Ribbon Field.app/Contents/"
                bundle.writestr(prefix + "Info.plist", plistlib.dumps(info))
                bundle.writestr(prefix + "Resources/backend.json", json.dumps({"python": "runtime/bin/python3", **runtime}))
                bundle.writestr(prefix + "Resources/python-runtime.json", json.dumps({
                    "triple": ("aarch64" if arch == "arm64" else "x86_64") + "-apple-darwin"}))
            feed = archive.with_suffix(".xml")
            release.sparkle.create_appcast(feed, version="1.2.3", build="1", architecture=arch,
                                          url=f"{release.DOWNLOAD_URL}/releases/1.2.3/{archive.name}",
                                          size=archive.stat().st_size, signature=signature, runtime=runtime)
            self.write(archive.name + ".json", {"version": "1.2.3", "build_number": "1", "architecture": arch,
                "channel": "alpha", "feed_url": url, "public_key": public, "runtime": runtime,
                "signature": signature, "size": archive.stat().st_size, "sha256": release.file_digest(archive),
                "appcast_sha256": release.file_digest(feed)})
        return public

    def update_files(self, public):
        # Signature mechanics use real RFC vectors in test_macos_updater; these
        # tests isolate packaging consistency without network or private keys.
        with mock.patch.object(release.sparkle, "verify_signature"), mock.patch.object(
                release.sparkle, "verify_appcast", side_effect=lambda path, key: release.ET.parse(path).getroot()):
            return release.release_files(self.directory, "1.2.3", COMMIT,
                                         require_updates=True, update_public_key=public)

    def test_updates_require_both_architectures_and_trusted_key(self):
        with self.assertRaisesRegex(ValueError, "trusted"):
            release.release_files(self.directory, "1.2.3", COMMIT, require_updates=True)
        key = self.add_updates()
        files, manifest = self.update_files(key)
        self.assertEqual(len(files), 14)
        self.assertEqual(set(manifest["updates"]), set(release.ARCHITECTURES))
        (self.directory / "Ribbon-Field-1.2.3-x86_64.zip").unlink()
        with self.assertRaisesRegex(ValueError, "Missing or unsafe update"):
            self.update_files(key)

    def test_update_metadata_and_signed_bundle_must_agree(self):
        key = self.add_updates()
        metadata = self.directory / "Ribbon-Field-1.2.3-arm64.zip.json"
        original = metadata.read_text()
        for field, value in (("architecture", "x86_64"), ("build_number", "2"), ("channel", "stable"),
                             ("public_key", "other"), ("size", 0), ("sha256", "0" * 64)):
            metadata.write_text(json.dumps({**json.loads(original), field: value}))
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "metadata mismatch"):
                self.update_files(key)
        metadata.write_text(original)
        changed = json.loads(original)
        changed["runtime"]["ui_protocol_max"] = 2
        metadata.write_text(json.dumps(changed))
        with self.assertRaisesRegex(ValueError, "archive metadata mismatch"):
            self.update_files(key)

    def test_archive_signature_failure_stops_release_validation(self):
        key = self.add_updates()
        with mock.patch.object(release.sparkle, "verify_signature", side_effect=ValueError("signature invalid")):
            with self.assertRaisesRegex(ValueError, "signature invalid"):
                release.release_files(self.directory, "1.2.3", COMMIT, require_updates=True, update_public_key=key)

    def test_signed_feed_wrong_enclosure_length_is_rejected_before_publication(self):
        key = self.add_updates()
        feed = self.directory / "Ribbon-Field-1.2.3-arm64.xml"
        root = ET.parse(feed)
        enclosure = root.find("./channel/item/enclosure")
        enclosure.set("length", str(int(enclosure.get("length")) + 1))
        root.write(feed, encoding="utf-8", xml_declaration=True)
        metadata = feed.with_suffix(".zip.json")
        values = json.loads(metadata.read_text())
        values["appcast_sha256"] = hashlib.sha256(feed.read_bytes()).hexdigest()
        metadata.write_text(json.dumps(values))
        with self.assertRaisesRegex(ValueError, "feed metadata mismatch"):
            self.update_files(key)

    def test_signed_feeds_promote_after_immutable_artifacts_and_before_website(self):
        key = self.add_updates()
        files, manifest = self.update_files(key)
        r2, events = mock.Mock(), []
        r2.exists_identical.return_value = False
        r2.promote_feed.side_effect = lambda key, path: events.append(("feed", key))
        r2.promote.side_effect = lambda path: events.append(("website", ""))
        with mock.patch.object(release, "verify_download", side_effect=lambda key, path: events.append(("verify", key))):
            release.publish(files, manifest, r2, promote_current=True)
        marker = events.index(("verify", "releases/1.2.3/release.json"))
        self.assertGreater(events.index(("feed", "updates/alpha/arm64.xml")), marker)
        self.assertGreater(events.index(("website", "")), events.index(("feed", "updates/alpha/x86_64.xml")))
        r2.reset_mock()
        with mock.patch.object(release, "verify_download", side_effect=ValueError("bad download")):
            with self.assertRaises(ValueError):
                release.publish(files, manifest, r2, promote_current=True)
        r2.promote_feed.assert_not_called()

    def test_feed_promotion_rejects_downgrade_and_compares_etag(self):
        self.add_updates()
        path = self.directory / "Ribbon-Field-1.2.3-arm64.xml"
        r2 = release.R2()
        for previous_build in ("0", "2"):
            def run(*args):
                if args[0] == "get-object":
                    data = path.read_text().replace("<sparkle:version>1<", f"<sparkle:version>{previous_build}<")
                    Path(args[-1]).write_text(data)
                return subprocess.CompletedProcess([], 0, '{"ETag": "old-etag"}', "")
            with mock.patch.object(r2, "run", side_effect=run) as call:
                if previous_build == "2":
                    with self.assertRaisesRegex(ValueError, "downgrade"):
                        r2.promote_feed("updates/alpha/arm64.xml", path)
                else:
                    r2.promote_feed("updates/alpha/arm64.xml", path)
                    self.assertIn("--if-match", call.call_args.args)
                    self.assertIn("old-etag", call.call_args.args)
                    self.assertIn("no-store", call.call_args.args)

    def test_valid_release_contains_both_architectures_and_exact_urls(self):
        files, manifest = self.files()
        self.assertEqual(len(files), 8)
        self.assertEqual(set(manifest["downloads"]), {"arm64", "x86_64"})
        self.assertEqual(manifest["source_commit"], COMMIT)
        self.assertEqual(manifest["downloads"]["arm64"]["url"],
                         "https://downloads.ribbonfield.com/releases/1.2.3/Ribbon-Field-1.2.3-arm64.dmg")

    def test_promotion_occurs_only_after_all_public_bytes_verify(self):
        files, manifest = self.files()
        r2, events = mock.Mock(), []
        r2.exists_identical.return_value = False
        r2.promote.side_effect = lambda path: events.append("promote")
        with mock.patch.object(release, "verify_download", side_effect=lambda key, path: events.append(key)):
            release.publish(files, manifest, r2, promote_current=True)
        self.assertEqual(events[-3:], ["releases/1.2.3/release.json", "promote", release.CURRENT_KEY])
        r2.reset_mock()
        with mock.patch.object(release, "verify_download", side_effect=ValueError("bad download")):
            with self.assertRaises(ValueError):
                release.publish(files, manifest, r2, promote_current=True)
        r2.promote.assert_not_called()

    def test_promotion_uses_compare_and_swap_and_rejects_downgrade(self):
        _, manifest = self.files()
        path = self.directory / "candidate.json"
        path.write_text(json.dumps(manifest))
        r2 = release.R2()
        for previous_version in ("1.2.2", "1.2.4"):
            def run(*args):
                if args[0] == "get-object":
                    Path(args[-1]).write_text(json.dumps({**manifest, "version": previous_version}))
                return subprocess.CompletedProcess([], 0, '{"ETag": "previous-etag"}', "")
            with mock.patch.object(r2, "run", side_effect=run) as call:
                if previous_version == "1.2.4":
                    with self.assertRaisesRegex(ValueError, "downgrade"):
                        r2.promote(path)
                    self.assertEqual(call.call_count, 1)
                else:
                    r2.promote(path)
                    args = call.call_args.args
                    self.assertEqual(args[args.index("--if-match") + 1], "previous-etag")
                    self.assertIn("no-store", args)

    def test_architectures_from_different_ci_runs_are_rejected(self):
        path = self.directory / "Ribbon-Field-1.2.3-x86_64.dmg.provenance.json"
        data = json.loads(path.read_text())
        data["build_number"] = "2"
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "different CI runs"):
            self.files()

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
        with mock.patch.object(release.urllib.request, "urlopen", return_value=io.BytesIO(b"wrong")) as urlopen:
            with self.assertRaisesRegex(ValueError, "Public download checksum mismatch"):
                release.verify_download("updates/alpha/arm64.xml", path)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://downloads.ribbonfield.com/updates/alpha/arm64.xml")
        # The download host returns 403 to Python's default urllib agent on updates/.
        self.assertEqual(request.get_header("User-agent"), "Ribbon Field release verifier")

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
