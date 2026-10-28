import Cocoa
#if canImport(Sparkle)
import Sparkle
#endif

/// Discovery checks trust and static compatibility only. Live decisions belong
/// to installation: compatible desktop replacement may leave a busy runtime alive.
final class RibbonUpdateGate {
    typealias Preflight = ([String: Any], @escaping (Error?) -> Void) -> Void
    private let compatibility: ([String: Any]) throws -> Void
    private let preflight: Preflight
    private let decisionTimeout: TimeInterval
    private struct StagedItem: Equatable {
        let metadata: String?
        let authenticated: Bool
        let architecture: String?
    }
    private var stagedItem: StagedItem?
    private var generation = 0
    private var stagingError: Error?
    private(set) var pending: [String: Any]?

    init(compatibility: @escaping ([String: Any]) throws -> Void = { _ in },
         decisionTimeout: TimeInterval = 15, preflight: @escaping Preflight) {
        self.compatibility = compatibility
        self.preflight = preflight
        self.decisionTimeout = decisionTimeout
    }

    func validate(metadata: String?, authenticated: Bool, architecture: String?) throws -> [String: Any] {
        guard authenticated, architecture == Self.architecture,
              let data = metadata?.data(using: .utf8),
              let target = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              let version = target["runtime_version"] as? String, !version.isEmpty else {
            throw Self.failure("This update has no verified runtime compatibility information.", permanent: true)
        }
        for key in ["runtime_protocol", "ui_protocol_min", "ui_protocol_max", "runtime_schema_min", "runtime_schema_max"] {
            guard let value = target[key] as? NSNumber,
                  CFGetTypeID(value) != CFBooleanGetTypeID(),
                  let integer = target[key] as? Int, integer > 0 else {
                throw Self.failure("This update has invalid runtime compatibility information.", permanent: true)
            }
        }
        guard let uiMin = target["ui_protocol_min"] as? Int,
              let uiMax = target["ui_protocol_max"] as? Int,
              let schemaMin = target["runtime_schema_min"] as? Int,
              let schemaMax = target["runtime_schema_max"] as? Int,
              uiMin <= uiMax, schemaMin <= schemaMax else {
            throw Self.failure("This update has invalid runtime compatibility ranges.", permanent: true)
        }
        try compatibility(target)
        return target
    }

    /// Even an invalid armed installer must block quit until Sparkle aborts it.
    func stage(metadata: String?, authenticated: Bool, architecture: String?) {
        let item = StagedItem(metadata: metadata, authenticated: authenticated, architecture: architecture)
        guard item != stagedItem else { return }
        clear()
        stagedItem = item
        do { pending = try validate(metadata: metadata, authenticated: authenticated, architecture: architecture) }
        catch { stagingError = error }
    }

    func clear() {
        generation += 1
        stagedItem = nil
        pending = nil
        stagingError = nil
    }

    /// Main-queue, exactly-once completion. Never cache an approval between quits
    /// or accept a response to a superseded target/decision. A stalled endpoint
    /// fails closed without blocking the main thread or holding logout forever.
    func recheck(completion: @escaping (Error?) -> Void) {
        generation += 1
        let requestGeneration = generation
        let target = pending
        let invalid = stagingError
        var finished = false
        let finish: (Error?) -> Void = { error in
            DispatchQueue.main.async {
                guard !finished else { return }
                finished = true
                guard self.generation == requestGeneration else {
                    completion(Self.failure("The update changed while its compatibility was being checked. Please try again."))
                    return
                }
                completion(error)
            }
        }
        DispatchQueue.main.async {
            if let invalid = invalid { finish(invalid); return }
            guard let target = target else { finish(nil); return }
            guard self.generation == requestGeneration else { finish(nil); return }
            self.preflight(target, finish)
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + decisionTimeout) {
            finish(Self.failure("The runtime did not respond. The update will wait until its compatibility can be verified."))
        }
    }

    static var architecture: String {
        #if arch(arm64)
        return "arm64"
        #else
        return "x86_64"
        #endif
    }

    static func failure(_ message: String, permanent: Bool = false) -> NSError {
        NSError(domain: "com.agentcoord.desktop.updates", code: permanent ? 2 : 1,
                userInfo: [NSLocalizedDescriptionKey: permanent ? "Update unavailable" : "Update postponed",
                           NSLocalizedRecoverySuggestionErrorKey: message])
    }
}

final class RibbonUpdater: NSObject, NSMenuItemValidation {
    private let gate: RibbonUpdateGate
    private var deferredInstall: (() -> Void)?
    private var started = false
    private var checking = false
    private var resumeRequested = false
    private var resumeUserInitiated = false
    private var decision = 0
    private(set) var lastDeferral: Error?
    #if canImport(Sparkle)
    private var controller: SPUStandardUpdaterController?
    #endif
    #if RIBBON_UPDATER_TESTING
    // Only isolated harness builds expose observation. No alternate trust path.
    var decisionObserver: ((Bool) -> Void)?
    #endif

    init(compatibility: @escaping ([String: Any]) throws -> Void = { _ in },
         preflight: @escaping RibbonUpdateGate.Preflight) {
        gate = RibbonUpdateGate(compatibility: compatibility, preflight: preflight)
        super.init()
    }

    func start() {
        guard !started else { return }
        started = true
        #if canImport(Sparkle)
        guard Self.acceptsConfiguration(Bundle.main.infoDictionary ?? [:]) else { return }
        controller = SPUStandardUpdaterController(startingUpdater: false, updaterDelegate: self, userDriverDelegate: nil)
        controller?.startUpdater()
        #endif
    }

    static func acceptsConfiguration(_ info: [String: Any]) -> Bool {
        guard let feed = info["SUFeedURL"] as? String,
              let url = URL(string: feed), acceptsFeedURL(url),
              let key = info["SUPublicEDKey"] as? String, Data(base64Encoded: key)?.count == 32,
              info["SURequireSignedFeed"] as? Bool == true,
              info["SUVerifyUpdateBeforeExtraction"] as? Bool == true,
              info["SUSignedFeedFailureExpirationInterval"] as? Int == 0 else { return false }
        return true
    }

    private static func acceptsFeedURL(_ url: URL) -> Bool {
        guard url.host != nil, url.user == nil, url.password == nil, url.fragment == nil else { return false }
        #if RIBBON_UPDATER_TESTING
        // build.py never defines this; production always requires HTTPS.
        if url.scheme == "http", ["127.0.0.1", "localhost", "[::1]"].contains(url.host!) { return true }
        #endif
        return url.scheme == "https"
    }

    func makeMenuItem() -> NSMenuItem {
        let item = NSMenuItem(title: "Check for Updates…", action: #selector(checkForUpdates(_:)), keyEquivalent: "")
        item.target = self
        return item
    }

    func validateMenuItem(_ menuItem: NSMenuItem) -> Bool {
        menuItem.title = deferredInstall == nil ? "Check for Updates…" : "Install Update and Relaunch…"
        menuItem.toolTip = lastDeferral.map { ($0 as NSError).localizedRecoverySuggestion ?? $0.localizedDescription }
        #if canImport(Sparkle)
        return !checking && (deferredInstall != nil || controller?.updater.canCheckForUpdates == true)
        #else
        menuItem.toolTip = "Updates are not configured for this development build."
        return false
        #endif
    }

    @objc private func checkForUpdates(_ sender: Any?) {
        if deferredInstall != nil {
            resumeDeferredInstall(userInitiated: true)
            return
        }
        #if canImport(Sparkle)
        controller?.checkForUpdates(sender)
        #endif
    }

    private func resumeDeferredInstall(userInitiated: Bool) {
        guard deferredInstall != nil else { return }
        if checking {
            resumeRequested = true
            resumeUserInitiated = resumeUserInitiated || userInitiated
            return
        }
        checking = true
        canTerminateForUpdate { allowed in
            self.checking = false
            if self.resumeRequested {
                let requestedByUser = userInitiated || self.resumeUserInitiated
                self.resumeRequested = false
                self.resumeUserInitiated = false
                self.resumeDeferredInstall(userInitiated: requestedByUser)
                return
            }
            if allowed, let resume = self.deferredInstall {
                self.deferredInstall = nil
                resume()
            } else if userInitiated, let error = self.lastDeferral {
                NSApp.presentError(error)
            }
        }
    }

    /// AppDelegate returns terminateLater and replies with this fresh result for
    /// every termination, including Sparkle, ordinary Quit, and Stop and Quit.
    /// Stop and Quit's preflight must use backend-held authenticated exit proof.
    /// Automatic paths record the reason without a modal alert during logout.
    func canTerminateForUpdate(completion: @escaping (Bool) -> Void) {
        decision += 1
        let request = decision
        gate.recheck { error in
            if self.decision == request { self.lastDeferral = error }
            let allowed = error == nil
            #if RIBBON_UPDATER_TESTING
            self.decisionObserver?(allowed)
            #endif
            completion(allowed)
        }
    }
}

#if canImport(Sparkle)
extension RibbonUpdater: SPUUpdaterDelegate {
    private func stage(_ item: SUAppcastItem) {
        gate.stage(metadata: item.propertiesDictionary["ribbonRuntime"] as? String,
                   authenticated: item.signingValidationStatus == .succeeded,
                   architecture: item.propertiesDictionary["ribbonArchitecture"] as? String)
    }

    func updater(_ updater: SPUUpdater, shouldProceedWithUpdate updateItem: SUAppcastItem,
                 updateCheck: SPUUpdateCheck) throws {
        // Sparkle rejects downgrades; this layer verifies signed target metadata.
        _ = try gate.validate(metadata: updateItem.propertiesDictionary["ribbonRuntime"] as? String,
                              authenticated: updateItem.signingValidationStatus == .succeeded,
                              architecture: updateItem.propertiesDictionary["ribbonArchitecture"] as? String)
    }

    func updater(_ updater: SPUUpdater, shouldPostponeRelaunchForUpdate item: SUAppcastItem,
                 untilInvokingBlock installHandler: @escaping () -> Void) -> Bool {
        stage(item)
        deferredInstall = installHandler
        resumeDeferredInstall(userInitiated: false)
        return true // Sparkle must wait for the asynchronous decision.
    }

    func updater(_ updater: SPUUpdater, willInstallUpdateOnQuit item: SUAppcastItem,
                 immediateInstallationBlock immediateInstallHandler: @escaping () -> Void) -> Bool {
        stage(item)
        deferredInstall = immediateInstallHandler
        // Retain the signed target for ordinary Quit. Do not activate an update
        // merely because a background download completed.
        return true
    }

    func updater(_ updater: SPUUpdater, willInstallUpdate item: SUAppcastItem) {
        stage(item)
        // This callback cannot veto installation. applicationShouldTerminate
        // always requests a fresh decision before Sparkle may replace the app.
    }

    func updater(_ updater: SPUUpdater, didAbortWithError error: Error) {
        deferredInstall = nil
        resumeRequested = false
        resumeUserInitiated = false
        decision += 1
        lastDeferral = nil
        gate.clear()
    }
}
#endif
