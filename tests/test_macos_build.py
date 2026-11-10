from __future__ import annotations

import importlib.util
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location(
    "macos_build_signing", Path(__file__).resolve().parents[1] / "desktop/macos/build.py")
build = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build)

IDENTITY = "Developer ID Application: Example (TEAM)"
REQUIREMENT = 'identifier "com.agentcoord.desktop" and anchor apple generic and certificate leaf[subject.OU] = TEAM'


class SigningIdentityTests(unittest.TestCase):
    def resolve(self, output, *, install=True):
        with mock.patch.object(build.subprocess, "run", return_value=
                               subprocess.CompletedProcess([], 0, output, "")):
            return build.resolve_signing_identity(None, install=install)

    def test_local_install_uses_available_developer_id_instead_of_development_certificate(self):
        output = ('  1) AAA "Apple Development: Example (OTHER)"\n'
                  f'  2) BBB "{IDENTITY}"\n  2 valid identities found\n')
        self.assertEqual(self.resolve(output), IDENTITY)

    def test_duplicate_certificate_names_do_not_make_selection_ambiguous(self):
        self.assertEqual(self.resolve(f'1) AAA "{IDENTITY}"\n2) BBB "{IDENTITY}"'), IDENTITY)

    def test_multiple_teams_require_an_explicit_choice(self):
        with self.assertRaisesRegex(ValueError, "Multiple Developer ID"):
            self.resolve(f'1) AAA "{IDENTITY}"\n2) BBB "Developer ID Application: Other (OTHER)"')

    def test_install_does_not_silently_fall_back_to_a_changing_identity(self):
        with self.assertRaisesRegex(ValueError, "retain macOS privacy approvals"):
            self.resolve("0 valid identities found")
        self.assertEqual(self.resolve("0 valid identities found", install=False), "-")

    def test_explicit_identity_and_ad_hoc_builds_do_not_query_keychain(self):
        with mock.patch.object(build.subprocess, "run") as run:
            for identity in (IDENTITY, "-", "certificate fingerprint"):
                self.assertEqual(build.resolve_signing_identity(identity, install=True), identity)
            run.assert_not_called()

    def test_keychain_failure_cannot_be_mistaken_for_no_certificate(self):
        with mock.patch.object(build.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "security")):
            with self.assertRaises(subprocess.CalledProcessError):
                build.resolve_signing_identity(None, install=True)


class SigningContinuityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "candidate.app"
        self.destination = self.root / "installed.app"
        executable = self.destination / "Contents/MacOS" / build.EXECUTABLE_NAME
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"installed executable")
        self.installed_code = executable

    def inspect(self, details, *, accepted=True):
        return mock.patch.object(build.subprocess, "run", side_effect=[
            subprocess.CompletedProcess([], 0, "", details),
            subprocess.CompletedProcess([], 0 if accepted else 1, "", "requirement mismatch"),
        ])

    def test_compatible_rebuild_checks_original_privacy_requirement(self):
        with self.inspect("Authority=" + IDENTITY + "\ndesignated => " + REQUIREMENT + "\n") as run:
            build.check_signing_continuity(self.source, self.destination)
        verification = run.call_args_list[1].args[0]
        self.assertEqual(verification[-2:], ["=" + REQUIREMENT, str(self.source)])
        self.assertIn("--strict", verification)
        self.assertIn("--deep", verification)

    def test_incompatible_replacement_leaves_installed_files_untouched(self):
        with self.inspect("designated => " + REQUIREMENT + "\n", accepted=False), \
             mock.patch.object(build, "check_destination"), \
             mock.patch.object(build, "check_not_running"):
            with self.assertRaisesRegex(ValueError, "discard macOS privacy approvals"):
                build.replace_app(self.source, self.destination)
        self.assertEqual(self.installed_code.read_bytes(), b"installed executable")
        self.assertEqual(list(self.root.iterdir()), [self.destination])

    def test_old_ad_hoc_install_can_be_repaired_with_certificate_signing(self):
        with self.inspect('Signature=adhoc\n# designated => cdhash H"123"\n') as run:
            build.check_signing_continuity(self.source, self.destination)
        self.assertEqual(run.call_count, 1)

    def test_unreadable_or_missing_requirement_cannot_allow_replacement(self):
        with self.inspect("Authority=" + IDENTITY):
            with self.assertRaisesRegex(ValueError, "Cannot establish"):
                build.check_signing_continuity(self.source, self.destination)
        with mock.patch.object(build.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "codesign")):
            with self.assertRaises(subprocess.CalledProcessError):
                build.check_signing_continuity(self.source, self.destination)

    def test_new_install_does_not_require_an_existing_signature(self):
        with mock.patch.object(build.subprocess, "run") as run:
            build.check_signing_continuity(self.source, self.root / "new.app")
        run.assert_not_called()

    @unittest.skipUnless(sys.platform == "darwin", "macOS code signing tools")
    def test_native_codesign_evaluates_requirement_text_instead_of_a_filename(self):
        executable = self.source / "Contents/MacOS" / build.EXECUTABLE_NAME
        executable.parent.mkdir(parents=True)
        shutil.copyfile("/usr/bin/true", executable)
        executable.chmod(0o755)
        info = {"CFBundleIdentifier": build.BUNDLE_ID, "CFBundleExecutable": build.EXECUTABLE_NAME,
                "CFBundlePackageType": "APPL"}
        (self.source / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
        subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", str(self.source)],
                       check=True, capture_output=True)
        run = subprocess.run

        def inspect_installed(command, **kwargs):
            if "--display" in command:
                # The actual native verifier evaluates an inline requirement;
                # this fixture needs no private certificate or Keychain access.
                return subprocess.CompletedProcess(command, 0,
                    f'designated => identifier "{build.BUNDLE_ID}"\n', "Signature size=1\n")
            return run(command, **kwargs)

        with mock.patch.object(build.subprocess, "run", side_effect=inspect_installed):
            build.check_signing_continuity(self.source, self.destination)


if __name__ == "__main__":
    unittest.main()
