from __future__ import annotations

from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("xcrun"), "requires native AppKit")
class UpdateNotificationTests(unittest.TestCase):
    def test_install_deferral_deduplication_and_changed_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            main = root / "main.swift"
            main.write_text(r'''import Cocoa
let application = NSApplication.shared
application.setActivationPolicy(.prohibited)
var identifier: String? = "first"
var supported = true
var inProgress = false
var choice = NSApplication.ModalResponse.alertSecondButtonReturn
var alerts = 0, installs = 0
var replaceDuringPrompt = false
let updater = RibbonUpdater(presentUpdate: { alert in
    alerts += 1
    let expectedButtons = supported ? ["Install", "Skip for Now"] : ["Update and Continue", "Cancel"]
    precondition(alert.buttons.map { $0.title } == expectedButtons)
    precondition(alert.informativeText.contains("0.1.5 (build 41)"))
    precondition(alert.buttons[1].keyEquivalent == "\u{1b}")
    if replaceDuringPrompt { identifier = "replacement" }
    return choice
}, preflight: { _, reply in reply(nil) })
updater.backendUpdateIdentifier = { identifier }
updater.backendUpdateVersion = { "0.1.5+41" }
updater.backendUpdateAvailable = { identifier != nil }
updater.backendUpdateRequiresConfirmation = { supported }
updater.backendUpdateInProgress = { inProgress }
updater.applyBackendUpdate = { reply in installs += 1; inProgress = true; reply(nil) }
precondition(RibbonUpdatePrompt.continuity(retainedProviders: true, continuingInterruptedProviders: true, uninterruptedUpdates: true) == .retainsProviderSessions)
precondition(RibbonUpdatePrompt.continuity(retainedProviders: false, continuingInterruptedProviders: true, uninterruptedUpdates: true) == .pausesOtherProviders)
precondition(RibbonUpdatePrompt.continuity(retainedProviders: false, continuingInterruptedProviders: false, uninterruptedUpdates: true) == .waitsForOtherProviders)
precondition(RibbonUpdatePrompt.continuity(retainedProviders: false, continuingInterruptedProviders: false, uninterruptedUpdates: false) == .restartsWork)
let retention = RibbonUpdatePrompt.alert(version: "0.1.35+101", restartingAgents: true, continuity: .retainsProviderSessions)
precondition(retention.informativeText.contains("Codex, Claude, Kimi, Muse and Antigravity"))
precondition(retention.informativeText.contains("current backend"))
precondition(!retention.informativeText.contains("waits for running Kimi"))
updater.start()
updater.notifyBackendUpdate()
precondition(alerts == 1 && installs == 0)
for _ in 0..<5 { updater.notifyBackendUpdate() }
precondition(alerts == 1 && installs == 0, "Repeated polls reopened a skipped update")
let menu = updater.makeMenuItem()
precondition(updater.validateMenuItem(menu), "Skip removed the manual update action")
choice = .alertFirstButtonReturn
_ = updater.perform(menu.action, with: nil)
precondition(alerts == 2 && installs == 1)
identifier = "second"
updater.notifyBackendUpdate()
precondition(alerts == 2, "A second dialog appeared while updating")
inProgress = false
supported = false
updater.notifyBackendUpdate()
precondition(alerts == 2, "An older runtime offered a deferral it cannot enforce")
supported = true
replaceDuringPrompt = true
updater.notifyBackendUpdate()
precondition(alerts == 3 && installs == 1, "Install accepted a target that changed while the dialog was open")
replaceDuringPrompt = false
updater.notifyBackendUpdate()
precondition(alerts == 4 && installs == 2, "A newer available target was not offered")
// Manual restart remains available on old runtimes without claiming Skip works.
inProgress = false
supported = false
identifier = "legacy"
choice = .alertSecondButtonReturn
_ = updater.perform(menu.action, with: nil)
precondition(alerts == 5 && installs == 2)
''')
            binary = root / "update-notification-test"
            result = subprocess.run(
                ["xcrun", "swiftc", "-swift-version", "5", "-module-cache-path", str(root / "modules"),
                 str(ROOT / "desktop/macos/Updater.swift"), str(main), "-o", str(binary)],
                capture_output=True, text=True, timeout=90,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)


@unittest.skipUnless(sys.platform == "darwin" and os.environ.get("RIBBON_SPARKLE_TEST_CACHE"),
                     "set RIBBON_SPARKLE_TEST_CACHE for signed feed acceptance")
class PublishedUpdateNotificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import json
        from unittest.mock import patch
        from test_macos_updater import RealSparkleTests
        # Framework extraction replaces its destination; keep this acceptance
        # fixture independent of any other test process using the shared cache.
        cache = tempfile.TemporaryDirectory(prefix="ribbon-notification-framework-")
        cls.addClassCleanup(cache.cleanup)
        specification = json.loads((ROOT / "desktop/macos/sparkle-runtime.json").read_text())
        shutil.copyfile(Path(os.environ["RIBBON_SPARKLE_TEST_CACHE"]) / specification["filename"],
                        Path(cache.name) / specification["filename"])
        with patch.dict(os.environ, RIBBON_SPARKLE_TEST_CACHE=cache.name):
            RealSparkleTests.setUpClass.__func__(cls)
        main = cls.root / "main.swift"
        main.write_text(r'''import Cocoa
import Sparkle
let application = NSApplication.shared
application.setActivationPolicy(.prohibited)
var prompts = 0, cycles = 0
let ribbon = RibbonUpdater { _, _ in fatalError("Deferring discovery must not request installation") }
let driver = RibbonUpdateUserDriver(hostBundle: Bundle.main, delegate: nil)
driver.presentUpdate = { alert in
    prompts += 1
    precondition(alert.buttons.map { $0.title } == ["Install", "Skip for Now"])
    return .alertSecondButtonReturn
}
var updater: SPUUpdater!
func finish(_ event: String) {
    let data = try! JSONSerialization.data(withJSONObject: ["event": event, "prompts": prompts, "cycles": cycles])
    print(String(data: data, encoding: .utf8)!)
    fflush(stdout)
    exit(0)
}
final class Completion: NSObject, SPUUpdaterDelegate {
    func updater(_ updater: SPUUpdater, shouldProceedWithUpdate item: SUAppcastItem, updateCheck: SPUUpdateCheck) throws {
        try ribbon.updater(updater, shouldProceedWithUpdate: item, updateCheck: updateCheck)
    }
    func updater(_ updater: SPUUpdater, didFinishUpdateCycleFor updateCheck: SPUUpdateCheck, error: Error?) {
        guard error == nil else { finish("error"); return }
        cycles += 1
        if cycles == 2 {
            precondition(prompts == 2, "Skip permanently suppressed the available release")
            finish("temporarily-dismissed")
        } else {
            DispatchQueue.main.async { updater.checkForUpdatesInBackground() }
        }
    }
}
let completion = Completion()
precondition(RibbonUpdater.acceptsConfiguration(Bundle.main.infoDictionary!))
updater = SPUUpdater(hostBundle: Bundle.main, applicationBundle: Bundle.main, userDriver: driver, delegate: completion)
try! updater.start()
updater.checkForUpdates()
DispatchQueue.main.asyncAfter(deadline: .now() + 25) { finish("timeout") }
application.run()
''')
        binary = cls.root / "notification-driver"
        result = subprocess.run([
            "xcrun", "swiftc", "-swift-version", "5", "-D", "RIBBON_UPDATER_TESTING",
            "-module-cache-path", str(cls.root / "modules"), "-F", str(cls.dist), "-framework", "Sparkle",
            "-Xlinker", "-rpath", "-Xlinker", "@executable_path/../Frameworks",
            str(ROOT / "desktop/macos/Updater.swift"), str(main), "-o", str(binary),
        ], capture_output=True, text=True, timeout=90)
        if result.returncode:
            raise AssertionError(result.stderr)
        cls.binaries[True] = binary

    def test_skip_for_now_allows_the_same_signed_release_to_be_offered_again(self):
        from test_macos_updater import RealSparkleTests
        result = RealSparkleTests.run_case(self, mode="notification")
        self.assertEqual(result["event"], "temporarily-dismissed", result)
        self.assertEqual(result["prompts"], 2)
