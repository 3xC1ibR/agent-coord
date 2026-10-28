from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/agent-coord/scripts"))
sys.path.insert(0, str(ROOT / "desktop/macos"))
import sparkle
import distribution

# Public RFC 8032 test vector, never a production signing key.
PUBLIC = base64.b64encode(bytes.fromhex("3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c")).decode()
SIGNATURE = base64.b64encode(bytes.fromhex("92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00")).decode()


class UpdaterTests(unittest.TestCase):
    def test_development_build_omits_updater_without_signing_settings(self):
        self.assertEqual(sparkle.configuration(None, None, standalone=False, identity="-"), {})

    def test_partial_or_unsigned_release_configuration_fails_closed(self):
        for feed, key, standalone, identity in [
            ("https://example.com/feed", None, True, "Developer ID Application: Test"),
            (None, PUBLIC, True, "Developer ID Application: Test"),
            ("http://example.com/feed", PUBLIC, True, "Developer ID Application: Test"),
            ("https://user:pass@example.com/feed", PUBLIC, True, "Developer ID Application: Test"),
            ("https://example.com/feed", PUBLIC, True, "-"),
            ("https://example.com/feed", PUBLIC, False, "Developer ID Application: Test"),
            ("https://example.com/feed", "invalid", True, "Developer ID Application: Test"),
        ]:
            with self.subTest(feed=feed, standalone=standalone, identity=identity), self.assertRaises(ValueError):
                sparkle.configuration(feed, key, standalone=standalone, identity=identity)
        info = sparkle.configuration("https://example.com/feed", PUBLIC, standalone=True,
                                     identity="Developer ID Application: Test")
        self.assertTrue(info["SURequireSignedFeed"])
        self.assertTrue(info["SUVerifyUpdateBeforeExtraction"])
        self.assertEqual(info["SUSignedFeedFailureExpirationInterval"], 0)
        self.assertTrue(info["SUEnableAutomaticChecks"])
        self.assertFalse(info["SUAutomaticallyUpdate"])

    def test_feed_has_exact_architecture_version_signature_and_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "feed.xml"
            for arch in ("arm64", "x86_64"):
                runtime = sparkle.runtime_metadata("1.2.3", "42")
                sparkle.create_appcast(path, version="1.2.3", build="42", architecture=arch,
                                       url=f"https://example.com/{arch}.zip", size=123,
                                       signature=SIGNATURE, runtime=runtime)
                item = ET.parse(path).find("./channel/item")
                self.assertEqual(item.findtext("ribbonArchitecture"), arch)
                self.assertEqual(json.loads(item.findtext("ribbonRuntime")), runtime)
                self.assertEqual(item.findtext("{" + sparkle.SPARKLE_NS + "}version"), "42")
                self.assertEqual(item.find("enclosure").get("length"), "123")
                self.assertEqual(item.find("enclosure").get("{" + sparkle.SPARKLE_NS + "}edSignature"), SIGNATURE)
            with self.assertRaises(ValueError):
                sparkle.validate_runtime({**runtime, "ui_protocol_min": True})
            with self.assertRaises(ValueError):
                sparkle.validate_runtime({**runtime, "runtime_schema_min": 2})

    def test_bundle_and_feed_share_runtime_constants_and_reject_drift(self):
        from agent_coord import desktop_runtime
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory) / "Test.app"
            resources = app / "Contents/Resources"
            resources.mkdir(parents=True)
            # Simulate a protocol bump: every artifact must use the generator.
            with mock.patch.object(desktop_runtime, "PROTOCOL_VERSION", 7), mock.patch.object(desktop_runtime, "SCHEMA_VERSION", 4):
                runtime = sparkle.runtime_metadata("1.2.3", "42")
                self.assertEqual(runtime, desktop_runtime.compatibility_metadata("1.2.3+42"))
                self.assertEqual(runtime["runtime_protocol"], 7)
                self.assertEqual(runtime["runtime_schema_min"], 4)
                info = {"CFBundleShortVersionString": "1.2.3", "CFBundleVersion": "42", **runtime}
                (app / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
                config = resources / "backend.json"
                config.write_text(json.dumps({"python": "runtime/bin/python3", **runtime}))
                checked = sparkle.bundle_runtime_metadata(app)
                feed = Path(directory) / "feed.xml"
                sparkle.create_appcast(feed, version="1.2.3", build="42", architecture="arm64",
                                       url="https://example.com/app.zip", size=123, signature=SIGNATURE, runtime=checked)
                self.assertEqual(json.loads(ET.parse(feed).findtext("./channel/item/ribbonRuntime")), runtime)
                for field in sparkle.RUNTIME_KEYS:
                    changed = dict(runtime)
                    changed[field] = "different" if field == "runtime_version" else 99
                    config.write_text(json.dumps(changed))
                    with self.subTest(field=field), self.assertRaises(ValueError):
                        sparkle.bundle_runtime_metadata(app)

    def test_checksum_failure_never_extracts_framework(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            spec = json.loads((sparkle.HERE / "sparkle-runtime.json").read_text())
            (cache / spec["filename"]).write_bytes(b"tampered")
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                sparkle.distribution(cache)
            self.assertEqual(len(list(cache.iterdir())), 1)

    def test_extraction_rejects_escaping_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            archive = cache / "archive.tar.xz"
            with tarfile.open(archive, "w:xz") as tar:
                member = tarfile.TarInfo("Sparkle.framework/link")
                member.type, member.linkname = tarfile.SYMTYPE, "../../escape"
                tar.addfile(member)
            with mock.patch.object(distribution, "verified_archive", return_value=archive):
                with self.assertRaisesRegex(ValueError, "Unsafe Sparkle archive link"):
                    sparkle.distribution(cache)

    def test_private_key_permissions_and_signing_failure_are_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / "test-key"
            key.write_text("TEST-ONLY-SECRET")
            key.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "mode 600"):
                sparkle.sign_file(Path("tool"), Path("archive"), key)
            key.chmod(0o600)
            result = subprocess.CompletedProcess([], 1, "", "TEST-ONLY-SECRET")
            with mock.patch.object(sparkle.subprocess, "run", return_value=result):
                with self.assertRaises(ValueError) as error:
                    sparkle.sign_file(Path("tool"), Path("archive"), key)
            self.assertNotIn("TEST-ONLY-SECRET", str(error.exception))

    def test_update_packaging_retries_ticket_connection_loss_and_fails_closed(self):
        for code, diagnostic, failures, attempts, succeeds in (
            (68, "NSURLErrorDomain Code=-1005", 1, 2, True),
            (68, "NSURLErrorDomain Code=-1005", 5, 5, False),
            (65, "invalid ticket", 1, 1, False),
            (68, "other ticket error", 1, 1, False),
        ):
            with self.subTest(code=code, diagnostic=diagnostic, failures=failures), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                app = root / "Test.app"
                resources = app / "Contents/Resources"
                resources.mkdir(parents=True)
                runtime = sparkle.runtime_metadata("1.2.3", "42")
                info = {"CFBundleShortVersionString": "1.2.3", "CFBundleVersion": "42",
                        "SUFeedURL": "https://downloads.ribbonfield.com/updates/alpha/x86_64.xml",
                        "SUPublicEDKey": PUBLIC, **runtime}
                (app / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
                (resources / "backend.json").write_text(json.dumps(runtime))
                archive = root / "update.zip"
                tickets = []
                def run(command, **kwargs):
                    result = subprocess.CompletedProcess(command, 0, "", "")
                    if command[:3] == ["xcrun", "stapler", "validate"]:
                        tickets.append(command)
                        if len(tickets) <= failures:
                            result = subprocess.CompletedProcess(command, code, diagnostic, "")
                    elif command[0] == "ditto":
                        archive.write_bytes(b"test update archive")
                    if kwargs.get("check"):
                        result.check_returncode()
                    return result
                with (mock.patch.object(sparkle.subprocess, "run", side_effect=run),
                      mock.patch.object(distribution.time, "sleep") as sleep,
                      mock.patch.object(sparkle, "sign_file", return_value=SIGNATURE) as sign,
                      mock.patch.object(sparkle, "verify_signature"),
                      mock.patch.object(sparkle, "verify_appcast")):
                    if succeeds:
                        result = sparkle.package_update(app, archive, tools=root / "tools", key_file=None,
                                                        architecture="x86_64", channel="alpha")
                        self.assertEqual(result["size"], len(b"test update archive"))
                        self.assertEqual(sign.call_count, 2)
                        self.assertTrue(archive.with_suffix(".zip.json").is_file())
                    else:
                        with self.assertRaises(subprocess.CalledProcessError):
                            sparkle.package_update(app, archive, tools=root / "tools", key_file=None,
                                                   architecture="x86_64", channel="alpha")
                        self.assertFalse(archive.exists())
                        self.assertFalse(archive.with_suffix(".xml").exists())
                        sign.assert_not_called()
                    self.assertEqual(len(tickets), attempts)
                    self.assertEqual(sleep.call_args_list, [mock.call(10 * 2 ** n) for n in range(attempts - 1)])

    def test_signed_feed_parser_rejects_unsigned_tampered_and_trailing_content(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "feed.xml"
            content = b"<rss><channel /></rss>\n"
            block = f"<!-- sparkle-signatures:\nedSignature: {SIGNATURE}\nlength: {len(content)}\n-->\n".encode()
            path.write_bytes(content + block)
            with mock.patch.object(sparkle, "verify_signature") as verify:
                self.assertEqual(sparkle.verify_appcast(path, PUBLIC).tag, "rss")
                verify.assert_called_once()
            with mock.patch.object(sparkle, "verify_signature", side_effect=ValueError("invalid signature")):
                with self.assertRaisesRegex(ValueError, "invalid signature"):
                    sparkle.verify_appcast(path, PUBLIC)
            for invalid in (content, content + b"x" + block, content + block + b"<item />", content + block + block):
                path.write_bytes(invalid)
                with self.assertRaises(ValueError):
                    sparkle.verify_appcast(path, PUBLIC)

    @unittest.skipUnless(shutil.which("openssl") or sys.platform == "darwin", "OS crypto unavailable")
    def test_os_crypto_accepts_rfc8032_and_rejects_changed_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "archive"
            path.write_bytes(b"\x72")
            sparkle.verify_signature(path, SIGNATURE, PUBLIC)
            path.write_bytes(b"\x73")
            with self.assertRaisesRegex(ValueError, "verification failed"):
                sparkle.verify_signature(path, SIGNATURE, PUBLIC)

    @unittest.skipUnless(sys.platform == "darwin" and shutil.which("xcrun"), "native gate test requires macOS")
    def test_native_install_gate_defers_and_rechecks_without_running_sparkle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            harness = root / "main.swift"
            harness.write_text(r'''import Cocoa
var compatible = true
var calls = 0
var replies: [(Error?) -> Void] = []
let gate = RibbonUpdateGate(decisionTimeout: 0.2) { target, reply in
    calls += 1
    replies.append(reply)
}
let target = "{\"runtime_protocol\":1,\"runtime_version\":\"1.2.3+42\",\"ui_protocol_min\":1,\"ui_protocol_max\":1,\"runtime_schema_min\":1,\"runtime_schema_max\":1}"
func rejects(_ action: () throws -> Void) {
    do { try action(); fatalError("Expected refusal") } catch {}
}
func pump(_ until: () -> Bool) {
    let deadline = Date().addingTimeInterval(2)
    while !until() && Date() < deadline { RunLoop.main.run(until: Date().addingTimeInterval(0.005)) }
    precondition(until(), "Asynchronous decision did not finish")
}
rejects { _ = try gate.validate(metadata: target, authenticated: false, architecture: RibbonUpdateGate.architecture) }
rejects { _ = try gate.validate(metadata: target, authenticated: true, architecture: "other") }
rejects { _ = try gate.validate(metadata: "{}", authenticated: true, architecture: RibbonUpdateGate.architecture) }
for invalid in ["true", "1.5", "1e100", "9223372036854775808", "null"] {
    let malformed = target.replacingOccurrences(of: "\"ui_protocol_min\":1", with: "\"ui_protocol_min\":" + invalid)
    rejects { _ = try gate.validate(metadata: malformed, authenticated: true, architecture: RibbonUpdateGate.architecture) }
}
_ = try gate.validate(metadata: target, authenticated: true, architecture: RibbonUpdateGate.architecture)
precondition(calls == 0 && gate.pending == nil, "Discovery must not query runtime readiness")
gate.stage(metadata: target, authenticated: true, architecture: RibbonUpdateGate.architecture)
var results: [Bool] = []
gate.recheck { results.append($0 == nil) }
precondition(results.isEmpty && calls == 0, "Decision must be asynchronous")
pump { calls == 1 }
// Sparkle repeats the same signed item across delegate callbacks. This must
// preserve an in-flight decision for that exact metadata/authentication/arch.
gate.stage(metadata: target, authenticated: true, architecture: RibbonUpdateGate.architecture)
replies[0](nil)
pump { results.count == 1 }
precondition(results == [true])
// Every attempt makes a new request; a previous approval cannot be reused.
gate.recheck { results.append($0 == nil) }
pump { calls == 2 }
replies[1](RibbonUpdateGate.failure("Runtime unavailable"))
pump { results.count == 2 }
precondition(results == [true, false] && gate.pending != nil)
// Late successful replies must not approve a new target or an aborted cycle.
gate.recheck { results.append($0 == nil) }
pump { calls == 3 }
gate.clear()
replies[2](nil)
replies[2](nil)
pump { results.count == 3 }
precondition(results == [true, false, false])
// Stalled endpoints terminate the asynchronous request with a refusal.
gate.stage(metadata: target, authenticated: true, architecture: RibbonUpdateGate.architecture)
gate.recheck { results.append($0 == nil) }
pump { results.count == 4 }
precondition(!results[3])
// An invalid armed item must not accidentally become an unguarded Quit.
gate.stage(metadata: "{}", authenticated: true, architecture: RibbonUpdateGate.architecture)
gate.recheck { results.append($0 == nil) }
pump { results.count == 5 }
precondition(!results[4])
precondition(RibbonUpdateGate.failure("bad signature", permanent: true).localizedDescription == "Update unavailable")
''')
            binary = root / "gate-test"
            result = subprocess.run(["xcrun", "swiftc", "-module-cache-path", str(root / "modules"),
                                     str(ROOT / "desktop/macos/Updater.swift"), str(harness), "-o", str(binary)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            subprocess.run([str(binary)], check=True, capture_output=True)


# Opt-in acceptance uses the real pinned framework and loopback only. It never
# enables the testing compiler flag in build.py or uses production signing keys.
SPARKLE_HARNESS = r'''
import Cocoa
import Sparkle
let mode = CommandLine.arguments[1]
let decisionURL = URL(string: CommandLine.arguments[2])!
let app = NSApplication.shared
app.setActivationPolicy(.prohibited)
var requests = 0
var observed: [Bool] = []
let ribbon = RibbonUpdater { target, reply in
    requests += 1
    URLSession.shared.dataTask(with: decisionURL) { data, response, error in
        guard error == nil, let http = response as? HTTPURLResponse, http.statusCode == 200,
              let data = data, let value = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              value["allowed"] as? Bool == true else {
            reply(RibbonUpdateGate.failure("Loopback runtime decision unavailable or incompatible")); return
        }
        reply(nil)
    }.resume()
}
ribbon.decisionObserver = { observed.append($0) }
var updater: SPUUpdater!
func finish(_ event: String, _ extra: [String: Any] = [:]) {
    var value = extra
    value["event"] = event
    value["requests"] = requests
    value["observed"] = observed
    let data = try! JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
    print(String(data: data, encoding: .utf8)!)
    fflush(stdout)
    exit(0)
}
final class Driver: NSObject, SPUUserDriver {
    func show(_ request: SPUUpdatePermissionRequest, reply: @escaping (SUUpdatePermissionResponse) -> Void) {
        reply(SUUpdatePermissionResponse(automaticUpdateChecks: false, sendSystemProfile: false))
    }
    func showUserInitiatedUpdateCheck(cancellation: @escaping () -> Void) {}
    func showUpdateFound(with appcastItem: SUAppcastItem, state: SPUUserUpdateState, reply: @escaping (SPUUserUpdateChoice) -> Void) {
        precondition(requests == 0, "Discovery queried runtime readiness")
        if mode == "race" { exerciseInstallRace(appcastItem); return }
        if mode == "archive" { reply(.install); return }
        if mode == "discover" { finish("found"); return }
        // These are real delegate methods receiving Sparkle-authenticated items.
        // The resume block records permission; it never replaces a real app.
        let retained = ribbon.updater(updater, willInstallUpdateOnQuit: appcastItem, immediateInstallationBlock: {})
        precondition(retained)
        let menu = ribbon.makeMenuItem()
        precondition(ribbon.validateMenuItem(menu))
        precondition(menu.title == "Install Update and Relaunch…")
        ribbon.canTerminateForUpdate { first in
            precondition(Thread.isMainThread)
            var resumed = false
            let postponed = ribbon.updater(updater, shouldPostponeRelaunchForUpdate: appcastItem,
                                             untilInvokingBlock: { resumed = true })
            precondition(postponed)
            func awaitSecond() {
                if observed.count < 2 { DispatchQueue.main.asyncAfter(deadline: .now() + 0.01, execute: awaitSecond); return }
                precondition(resumed == first)
                // The final willInstall callback cannot cancel; ordinary quit
                // still asks the runtime again instead of reusing the approval.
                ribbon.updater(updater, willInstallUpdate: appcastItem)
                ribbon.canTerminateForUpdate { last in
                    finish("decision", ["allowed": first, "resumed": resumed, "quitAllowed": last])
                }
            }
            DispatchQueue.main.async(execute: awaitSecond)
        }
    }
    func showUpdateReleaseNotes(with downloadData: SPUDownloadData) {}
    func showUpdateReleaseNotesFailedToDownloadWithError(_ error: Error) {}
    func showUpdateNotFoundWithError(_ error: Error, acknowledgement: @escaping () -> Void) { finish("not-found", ["code": (error as NSError).code]) }
    func showUpdaterError(_ error: Error, acknowledgement: @escaping () -> Void) {
        finish("error", ["code": (error as NSError).code, "description": String(describing: error)])
    }
    func showDownloadInitiated(cancellation: @escaping () -> Void) {}
    func showDownloadDidReceiveExpectedContentLength(_ expectedContentLength: UInt64) {}
    func showDownloadDidReceiveData(ofLength length: UInt64) {}
    func showDownloadDidStartExtractingUpdate() {}
    func showExtractionReceivedProgress(_ progress: Double) {}
    func showReady(toInstallAndRelaunch reply: @escaping (SPUUserUpdateChoice) -> Void) { reply(.skip); finish("ready") }
    func showInstallingUpdate(withApplicationTerminated applicationTerminated: Bool, retryTerminatingApplication: @escaping () -> Void) {}
    func showUpdateInstalledAndRelaunched(_ relaunched: Bool, acknowledgement: @escaping () -> Void) { finish("unexpected-installed") }
    func dismissUpdateInstallation() {}

    private func exerciseInstallRace(_ item: SUAppcastItem) {
        var replies: [(Error?) -> Void] = []
        let candidate = RibbonUpdater { _, reply in replies.append(reply) }
        var resumed: [String] = []
        func when(_ condition: @escaping () -> Bool, _ action: @escaping () -> Void) {
            if condition() { action() }
            else { DispatchQueue.main.asyncAfter(deadline: .now() + 0.005) { when(condition, action) } }
        }
        _ = candidate.updater(updater, shouldPostponeRelaunchForUpdate: item, untilInvokingBlock: { resumed.append("old") })
        when({ replies.count == 1 }) {
            _ = candidate.updater(updater, shouldPostponeRelaunchForUpdate: item, untilInvokingBlock: { resumed.append("latest") })
            replies[0](nil)
            when({ replies.count == 2 }) {
                precondition(resumed.isEmpty, "Superseded handler resumed")
                replies[1](nil)
                when({ resumed == ["latest"] }) {
                    var oldFinished = false
                    var newestFinished = false
                    candidate.canTerminateForUpdate { allowed in precondition(!allowed); oldFinished = true }
                    when({ replies.count == 3 }) {
                        candidate.canTerminateForUpdate { allowed in precondition(allowed); newestFinished = true }
                        when({ replies.count == 4 }) {
                            replies[3](nil)
                            when({ newestFinished }) {
                                replies[2](RibbonUpdateGate.failure("Stale refusal"))
                                when({ oldFinished }) {
                                    precondition(candidate.lastDeferral == nil, "Stale result replaced current status")
                                    finish("race", ["resumed": resumed, "raceRequests": replies.count])
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}
let driver = Driver()
guard RibbonUpdater.acceptsConfiguration(Bundle.main.infoDictionary!) else { finish("configuration-refused"); fatalError() }
updater = SPUUpdater(hostBundle: Bundle.main, applicationBundle: Bundle.main, userDriver: driver, delegate: ribbon)
do { try updater.start(); updater.checkForUpdates() }
catch { finish("start-error", ["description": String(describing: error)]) }
DispatchQueue.main.asyncAfter(deadline: .now() + 25) { finish("timeout") }
app.run()
'''


@unittest.skipUnless(sys.platform == "darwin" and os.environ.get("RIBBON_SPARKLE_TEST_CACHE"),
                     "set RIBBON_SPARKLE_TEST_CACHE for isolated real Sparkle acceptance")
class RealSparkleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import http.server
        import threading
        cls.temporary = tempfile.TemporaryDirectory(prefix="ribbon-sparkle-test-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        # Recheck archive SHA-256 and re-extract; an unpacked cache is not trust.
        cls.dist = sparkle.distribution(Path(os.environ["RIBBON_SPARKLE_TEST_CACHE"]))
        cls.key = cls.root / "test-only-key"
        cls.key.write_text(base64.b64encode(bytes.fromhex("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb")).decode())
        cls.key.chmod(0o600)
        cls.response_status, cls.response_allowed = 200, True
        class Handler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=str(cls.root), **kwargs)
            def log_message(self, *args):
                pass
            def do_GET(self):
                if self.path == "/decision":
                    self.send_response(cls.response_status)
                    self.end_headers()
                    self.wfile.write(json.dumps({"allowed": cls.response_allowed}).encode())
                else:
                    super().do_GET()
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        def close_server():
            cls.server.shutdown()
            cls.server.server_close()
            cls.thread.join()
        cls.addClassCleanup(close_server)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        main = cls.root / "main.swift"
        main.write_text(SPARKLE_HARNESS)
        cls.binaries = {}
        for testing in (False, True):
            binary = cls.root / ("testing" if testing else "production")
            command = ["xcrun", "swiftc", "-swift-version", "5", "-target", f"{os.uname().machine}-apple-macosx12.0",
                       "-module-cache-path", str(cls.root / "modules"), "-F", str(cls.dist), "-framework", "Sparkle",
                       "-Xlinker", "-rpath", "-Xlinker", "@executable_path/../Frameworks"]
            if testing:
                command += ["-D", "RIBBON_UPDATER_TESTING"]
            # The production compile has no observer property; omit the test line.
            main.write_text(SPARKLE_HARNESS if testing else SPARKLE_HARNESS.replace(
                "ribbon.decisionObserver = { observed.append($0) }", ""))
            result = subprocess.run(command + [str(ROOT / "desktop/macos/Updater.swift"), str(main), "-o", str(binary)],
                                    text=True, capture_output=True, timeout=180)
            if result.returncode:
                raise AssertionError(result.stderr)
            cls.binaries[testing] = binary

    def run_case(self, *, mode="discover", testing=True, version="3", feed_change=None,
                 signature=None, archive_change=None, decision=None, foreign_feed=False):
        import uuid
        bundle_id = "com.ribbonfield.updater-test." + uuid.uuid4().hex
        app = self.root / (bundle_id + ".app")
        macos = app / "Contents/MacOS"
        macos.mkdir(parents=True)
        frameworks = app / "Contents/Frameworks"
        frameworks.mkdir()
        shutil.copytree(self.dist / "Sparkle.framework", frameworks / "Sparkle.framework", symlinks=True)
        shutil.copyfile(self.binaries[testing], macos / "Harness")
        (macos / "Harness").chmod(0o755)
        info = {"CFBundleIdentifier": bundle_id, "CFBundleName": "Updater Test", "CFBundleExecutable": "Harness",
                "CFBundleVersion": "2", "CFBundleShortVersionString": "1.0.0", "CFBundlePackageType": "APPL",
                "SUFeedURL": self.base + "/feed.xml", "SUPublicEDKey": PUBLIC, "SUEnableAutomaticChecks": False,
                "SUAutomaticallyUpdate": False, "SUEnableInstallerLauncherService": False,
                "SURequireSignedFeed": True, "SUVerifyUpdateBeforeExtraction": True,
                "SUSignedFeedFailureExpirationInterval": 0,
                "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True}}
        (app / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
        before = (macos / "Harness").read_bytes()
        archive = self.root / "update.zip"
        # Sign an actual higher-version candidate, then restore the host version.
        # The driver always skips at ready-to-install; it never replaces a bundle.
        candidate_info = {**info, "CFBundleVersion": version, "CFBundleShortVersionString": "1.0.1"}
        (app / "Contents/Info.plist").write_bytes(plistlib.dumps(candidate_info))
        subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(app)], check=True, capture_output=True)
        subprocess.run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(app), str(archive)], check=True)
        (app / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
        subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(app)], check=True, capture_output=True)
        before = (macos / "Harness").read_bytes()
        tool = self.dist / "bin/sign_update"
        signed = sparkle.sign_file(tool, archive, self.key)
        if archive_change:
            archive_change(archive)
        feed = self.root / "feed.xml"
        sparkle.create_appcast(feed, version="1.0.1", build=version, architecture=os.uname().machine,
                               url="https://example.invalid/update.zip", size=archive.stat().st_size,
                               signature=signature or signed, runtime=sparkle.runtime_metadata("1.0.1", version))
        feed.write_text(feed.read_text().replace("https://example.invalid/update.zip", self.base + "/update.zip"))
        if feed_change:
            feed_change(feed)
        if mode != "unsigned":
            key = self.key
            if foreign_feed:
                key = self.root / "foreign-test-only-key"
                key.write_text(base64.b64encode(bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")).decode())
                key.chmod(0o600)
            sparkle.sign_file(tool, feed, key)
        # Observe signed bytes from Sparkle itself, not a mocked verifier.
        try:
            result = subprocess.run([str(macos / "Harness"), mode, decision or self.base + "/decision"],
                                    capture_output=True, text=True, timeout=35)
            self.assertEqual(result.returncode, 0, result.stderr)
            events = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
            self.assertEqual(len(events), 1, result.stdout + result.stderr)
            self.assertEqual((macos / "Harness").read_bytes(), before, "Test unexpectedly replaced its bundle")
            self.assertEqual(plistlib.loads((app / "Contents/Info.plist").read_bytes()), info)
            return events[0]
        finally:
            # Only this generated bundle identifier is touched; never app state.
            subprocess.run(["defaults", "delete", bundle_id], capture_output=True)
            cache = Path.home() / "Library/Caches" / bundle_id
            if cache.exists():
                # Sparkle's helper may remove its own failed-install files as
                # it exits. Ignore only that benign concurrent disappearance.
                def ignore_missing(function, path, error):
                    if not isinstance(error, FileNotFoundError):
                        raise error
                shutil.rmtree(cache, onexc=ignore_missing)
            shutil.rmtree(app)

    def test_real_signed_discovery_does_not_contact_runtime(self):
        self.__class__.response_status = 503
        value = self.run_case()
        self.assertEqual(value["event"], "found", value)
        self.assertEqual(value["requests"], 0)

    def test_all_install_delegate_paths_use_fresh_loopback_decisions(self):
        for status, allowed in ((200, True), (200, False), (503, False)):
            self.__class__.response_status, self.__class__.response_allowed = status, allowed
            with self.subTest(status=status, allowed=allowed):
                value = self.run_case(mode="decision")
                self.assertEqual(value["event"], "decision", value)
                self.assertEqual(value["requests"], 3)
                self.assertEqual(value["observed"], [allowed] * 3)
                self.assertEqual(value["resumed"], allowed)
                self.assertEqual(value["quitAllowed"], allowed)
        import socket
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            closed_port = listener.getsockname()[1]
        value = self.run_case(mode="decision", decision=f"http://127.0.0.1:{closed_port}/decision")
        self.assertEqual(value["observed"], [False] * 3, value)

    def test_pending_install_retries_latest_handler_and_ignores_stale_status(self):
        value = self.run_case(mode="race")
        self.assertEqual(value["event"], "race", value)
        self.assertEqual(value["resumed"], ["latest"])
        self.assertEqual(value["raceRequests"], 4)

    def test_real_framework_rejects_unsigned_lower_version_and_production_http(self):
        for kwargs, expected in (({"mode": "unsigned"}, "error"),
                                 ({"foreign_feed": True}, "error"),
                                 ({"version": "1"}, "not-found"),
                                 ({"testing": False}, "configuration-refused")):
            with self.subTest(kwargs=kwargs):
                value = self.run_case(**kwargs)
                self.assertEqual(value["event"], expected, value)
                self.assertEqual(value["requests"], 0)

    def test_real_framework_rejects_tampered_archive_and_foreign_signature(self):
        def tamper(path):
            raw = bytearray(path.read_bytes())
            raw[len(raw) // 2] ^= 1
            path.write_bytes(raw)
        for kwargs in ({"archive_change": tamper}, {"signature": SIGNATURE}):
            with self.subTest(kwargs=kwargs):
                value = self.run_case(mode="archive", **kwargs)
                self.assertEqual(value["event"], "error", value)
                self.assertEqual(value["requests"], 0)
                # The installer wraps SUValidationError (3002) in its own
                # signature-error (4005); assert the underlying trust failure.
                self.assertEqual(value["code"], 4005, value)
                self.assertIn("Code=3002", value["description"])

    def test_real_framework_treats_enclosure_length_as_advisory(self):
        # Sparkle authenticates archive bytes, not the feed's size estimate.
        # The publisher separately refuses any mismatch before promotion.
        def wrong_length(path):
            root = ET.parse(path)
            enclosure = root.find("./channel/item/enclosure")
            enclosure.set("length", str(int(enclosure.get("length")) + 1))
            root.write(path, encoding="utf-8", xml_declaration=True)
        value = self.run_case(mode="archive", feed_change=wrong_length)
        self.assertEqual(value["event"], "ready", value)
        self.assertEqual(value["requests"], 0)


if __name__ == "__main__":
    unittest.main()
