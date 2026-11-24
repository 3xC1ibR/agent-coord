import Foundation
import Darwin

struct BackendConfiguration: Decodable {
    let python: String
    let database: String?
    let path: String?
    let runtime_protocol: Int
    let runtime_schema_min: Int
    let runtime_schema_max: Int

    func pythonURL(resources: URL) -> URL {
        python.hasPrefix("/") ? URL(fileURLWithPath: python) : resources.appendingPathComponent(python)
    }
    var databasePath: String {
        if let database = database { return database }
        let environment = ProcessInfo.processInfo.environment
        if let configured = environment["AGENT_COORD_DB"], !configured.isEmpty {
            return (configured as NSString).expandingTildeInPath
        }
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        return ((environment["XDG_STATE_HOME"] ?? home + "/.local/state") as NSString).expandingTildeInPath + "/agent-coord/state.sqlite3"
    }
    var toolPath: String {
        if let path = path { return path }
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        return ([home + "/.local/bin", home + "/.npm-global/bin", "/opt/homebrew/bin", "/usr/local/bin",
                 ProcessInfo.processInfo.environment["PATH"] ?? "", "/usr/bin", "/bin", "/usr/sbin", "/sbin"])
            .filter { !$0.isEmpty }.joined(separator: ":")
    }
}

/// A discovered process identity is observational. All controls go through the
/// authenticated runtime helper; the desktop never signals this PID.
struct RuntimeProcess {
    let processIdentifier: Int32
    var isRunning: Bool { kill(processIdentifier, 0) == 0 }
}

private final class RuntimeHealthDelegate: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil)
    }
}

final class Backend {
    var process: RuntimeProcess?
    var ready: ((URL) -> Void)?
    var exited: ((String) -> Void)?
    var updateStatus: ((String, Bool) -> Void)?
    var updateAvailable: (() -> Void)?
    private(set) var pendingUpdate: String?
    private(set) var pendingUpdateVersion: String?
    private(set) var pendingUpdateRequiresConfirmation = false
    private(set) var supportsRuntimeUpdates = false
    private(set) var supportsUninterruptedUpdates = false
    private(set) var continuesInterruptedProviders = false
    var updateInProgress: Bool { expectedUpdate != nil }
    private var expectedUpdate: (id: String, payload: String)?
    private var reportedUpdate: String?
    private var lastStatusFailure: String?
    private(set) var logURL: URL
    private var resources: URL?
    private var configuration: BackendConfiguration?
    private var database: String?
    private var python: URL?
    private var helperResources: URL?
    private var interactionResources: URL?
    private var interactionRequestID: String?
    private var legacyInteractionURL: URL?
    private var edgeURL: URL?
    private var edgeObserver: String?
    var interactionURL: URL? { edgeURL ?? legacyInteractionURL }
    private let healthSession: URLSession = {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        configuration.timeoutIntervalForRequest = 1
        configuration.timeoutIntervalForResource = 2
        configuration.connectionProxyDictionary = [:]
        return URLSession(configuration: configuration, delegate: RuntimeHealthDelegate(), delegateQueue: nil)
    }()
    private var lastWebURL: URL?
    private var timer: Timer?
    private var polling = false
    private var starting = false
    private var stopping = false
    private var stoppedInstance: String?
    private var instance: String?
    private var failures = 0
    private let worker = DispatchQueue(label: "com.agentcoord.desktop.runtime")

    init(directory: URL) { logURL = directory.appendingPathComponent("backend.log") }

    static func failure(_ message: String) -> NSError {
        NSError(domain: "com.agentcoord.desktop.runtime", code: 1,
                userInfo: [NSLocalizedDescriptionKey: message])
    }

    func validateCompatibility(_ target: [String: Any]) throws {
        guard let config = configuration,
              target["runtime_protocol"] as? Int == config.runtime_protocol,
              let minimum = target["ui_protocol_min"] as? Int,
              let maximum = target["ui_protocol_max"] as? Int,
              minimum <= config.runtime_protocol, maximum >= config.runtime_protocol,
              let schemaMin = target["runtime_schema_min"] as? Int,
              let schemaMax = target["runtime_schema_max"] as? Int,
              schemaMin <= config.runtime_schema_min, schemaMax >= config.runtime_schema_max else {
            throw Self.failure("The update cannot attach to this runtime protocol or database schema.")
        }
    }

    private func command(_ arguments: [String], input: [String: Any]? = nil) throws -> [String: Any] {
        guard let defaultPython = python, let retained = helperResources,
              let config = configuration, let database = database else {
            throw Self.failure("The runtime has not been configured.")
        }
        // The serving predecessor may predate interaction continuity. Its
        // control helper must never authorize an upgrade around the gateway.
        let continuity = arguments.contains("--runtime-interaction-continuity") || arguments.contains("--runtime-stable-origin")
        let gatewayHelper = arguments.contains("--runtime-stable-origin") || arguments.contains("--runtime-bootstrap")
            ? self.resources : (interactionResources ?? self.resources)
        let resources = continuity ? (gatewayHelper ?? retained) : retained
        let python = continuity ? try JSONDecoder().decode(BackendConfiguration.self,
            from: Data(contentsOf: resources.appendingPathComponent("backend.json"))).pythonURL(resources: resources) : defaultPython
        let child = Process(), output = Pipe(), errors = Pipe(), stdin = Pipe()
        child.executableURL = python
        child.arguments = ["-B", "-u", resources.appendingPathComponent("backend.py").path, "--db", database] + arguments
        var environment = ProcessInfo.processInfo.environment
        for key in Array(environment.keys) where key.hasPrefix("AGENT_COORD_") || key.hasPrefix("RIBBON_RUNTIME_") || key.hasPrefix("ZELLIJ") || key == "CODEX_THREAD_ID" {
            environment.removeValue(forKey: key)
        }
        environment.removeValue(forKey: "PYTHONHOME")
        environment["PATH"] = config.toolPath
        environment["PYTHONPATH"] = resources.appendingPathComponent("backend").path
        environment["PYTHONNOUSERSITE"] = "1"
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        child.environment = environment
        child.currentDirectoryURL = FileManager.default.homeDirectoryForCurrentUser
        child.standardInput = stdin
        child.standardOutput = output
        child.standardError = errors
        try child.run()
        let timeout: TimeInterval = arguments.contains("--runtime-bootstrap") || arguments.contains("activate-update") ? 90 : (arguments.contains("stop") ? 35 : 10)
        let stateLock = NSLock()
        var finished = false
        var expired = false
        let deadline = DispatchWorkItem {
            stateLock.lock()
            if finished { stateLock.unlock(); return }
            expired = true
            stateLock.unlock()
            // This is our short-lived helper, never the detached runtime PID.
            if child.isRunning { child.terminate() }
            DispatchQueue.global().asyncAfter(deadline: .now() + 1) {
                if child.isRunning { kill(child.processIdentifier, SIGKILL) }
            }
        }
        DispatchQueue.global().asyncAfter(deadline: .now() + timeout, execute: deadline)
        if let input = input { stdin.fileHandleForWriting.write(try JSONSerialization.data(withJSONObject: input)) }
        try? stdin.fileHandleForWriting.close()
        let data = output.fileHandleForReading.readDataToEndOfFile()
        let error = errors.fileHandleForReading.readDataToEndOfFile()
        child.waitUntilExit()
        stateLock.lock()
        finished = true
        let timedOut = expired
        stateLock.unlock()
        deadline.cancel()
        try? output.fileHandleForReading.close()
        try? errors.fileHandleForReading.close()
        if timedOut { throw Self.failure("The runtime helper did not respond before its deadline. Try again shortly.") }
        guard child.terminationStatus == 0,
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw Self.failure(String(data: error, encoding: .utf8) ?? "The runtime did not respond.")
        }
        return object
    }

    func start(resources: URL, configuration: BackendConfiguration, database: String?) throws {
        guard !starting, process == nil else { return }
        self.resources = resources
        self.configuration = configuration
        self.database = database ?? configuration.databasePath
        let canonicalDB = URL(fileURLWithPath: self.database!).resolvingSymlinksInPath()
        logURL = canonicalDB.deletingLastPathComponent().appendingPathComponent("." + canonicalDB.lastPathComponent + ".runtime/runtime.log")
        helperResources = resources
        python = configuration.pythonURL(resources: resources)
        starting = true
        stopping = false
        stoppedInstance = nil
        worker.async {
            do {
                let status = try self.command(["--runtime-bootstrap", "--resources", resources.path, "--runtime-confirm-updates", "--runtime-interaction-continuity", "--runtime-stable-origin"])
                DispatchQueue.main.async {
                    self.starting = false
                    do { try self.accept(status) }
                    catch { self.exited?(error.localizedDescription); return }
                    self.timer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in self?.poll() }
                }
            } catch { DispatchQueue.main.async { self.starting = false; self.exited?(error.localizedDescription) } }
        }
    }

    private func loopbackURL(_ value: Any?) throws -> URL {
        guard let text = value as? String, let url = URL(string: text), let port = url.port,
              (1...65535).contains(port), text == "http://127.0.0.1:\(port)/" else {
            throw Self.failure("Invalid local interaction address.")
        }
        return url
    }

    private func accept(_ status: [String: Any]) throws {
        let wasBound = legacyInteractionURL != nil
        var gatewaySettled = edgeURL != nil
        if status["edge_url"] != nil {
            let url = try loopbackURL(status["edge_url"])
            guard edgeURL == nil || edgeURL == url else {
                throw Self.failure("The stable local runtime address changed.")
            }
            if edgeURL == nil {
                guard let observer = status["edge_observer"] as? String,
                      observer.range(of: "^[A-Za-z0-9_-]{43}$", options: .regularExpression) != nil else {
                    throw Self.failure("Invalid local runtime observer.")
                }
                edgeURL = url
                edgeObserver = observer
                legacyInteractionURL = nil
                interactionResources = nil
                interactionRequestID = nil
            }
            gatewaySettled = true
        } else if status["interaction_url"] != nil {
            let url = try loopbackURL(status["interaction_url"])
            guard let settled = status["interaction_settled"] as? Bool, let path = status["interaction_payload"] as? String,
                  let db = database.map({ URL(fileURLWithPath: $0).resolvingSymlinksInPath() }) else {
                throw Self.failure("Invalid interaction gateway handshake.")
            }
            let helper = URL(fileURLWithPath: path).resolvingSymlinksInPath()
            let payloads = db.deletingLastPathComponent().appendingPathComponent("." + db.lastPathComponent + ".runtime/payloads")
            guard helper.deletingLastPathComponent().resolvingSymlinksInPath().path == payloads.resolvingSymlinksInPath().path,
                  helper.lastPathComponent.range(of: "^[0-9a-f]{64}$", options: .regularExpression) != nil,
                  legacyInteractionURL == nil || legacyInteractionURL == url else {
                throw Self.failure("Interaction gateway identity changed before the previous intake settled.")
            }
            interactionResources = helper
            interactionRequestID = settled ? nil : status["interaction_request_id"] as? String
            legacyInteractionURL = url
            gatewaySettled = settled
        } else if wasBound {
            throw Self.failure("The interaction gateway has not confirmed delivery of pending messages.")
        }
        // During the interval without a coordinator, reopening the app still
        // restores the stable gateway. Normal status overlays hide web views.
        if let origin = interactionURL, !gatewaySettled, lastWebURL != origin {
            lastWebURL = origin
            ready?(origin)
        }
        var completedUpdate = false
        var terminalUpdate: (key: String, message: String, failed: Bool)?
        if let update = status["update"] as? [String: Any],
           update["superseded_by"] == nil,
           let identity = update["id"] as? String, let phase = update["phase"] as? String,
           (interactionURL == nil || gatewaySettled || interactionRequestID == identity ||
            !["committed", "rolled-back", "failed"].contains(phase)) {
            if status["status"] as? String == "preparing" {
                if let target = update["target"] as? String { expectedUpdate = (identity, target) }
                if interactionURL == nil { updateStatus?("Updating backend…", false) }
                return
            }
            if !["committed", "rolled-back", "failed"].contains(phase) {
                if let target = update["target"] as? String { expectedUpdate = (identity, target) }
                // The backend names what the update is waiting for or pausing.
                if let notice = update["notice"] as? String { updateStatus?(notice, false) }
                else if interactionURL == nil { updateStatus?("Updating backend…", false) }
                if status["status"] as? String != "serving" { return }
            } else if reportedUpdate != identity + phase {
                let matches = phase == "committed" && status["status"] as? String == "serving"
                    && update["cancelled"] as? Bool != true
                    && status["payload"] as? String == update["target"] as? String
                if let expected = expectedUpdate, expected.id != identity || (matches && expected.payload != status["payload"] as? String) {
                    throw Self.failure("The active backend does not match the requested update.")
                }
                if phase == "committed" && status["payload"] as? String != update["target"] as? String {
                    throw Self.failure("The active backend does not match the committed update.")
                }
                if interactionURL != nil && !gatewaySettled { return }
                completedUpdate = expectedUpdate != nil
                let review = update["requires_review"] as? Bool == true
                let message = matches
                    ? (review ? "Backend updated. Work remains paused; inspect native tools before resuming." : "Backend updated. Eligible conversations are continuing.")
                    : (update["error"] as? String ?? "The backend update did not apply.")
                terminalUpdate = (identity + phase, message, !matches || review)
            }
        }
        if status["status"] as? String == "preparing" { return }
        guard let value = status["url"] as? String, let url = URL(string: value),
              url.scheme == "http", url.host == "127.0.0.1", url.port != nil,
              url.user == nil, url.password == nil, url.path == "/",
              let pid = status["pid"] as? Int32, let instance = status["instance"] as? String,
              let payload = status["payload"] as? String else { throw Self.failure("Invalid runtime handshake.") }
        // A failed connection can hide every web view while the same runtime
        // keeps working. Its next valid handshake must restore those views.
        let direct = edgeURL == nil
            && (legacyInteractionURL == nil || (gatewaySettled && status["status"] as? String == "serving"))
        let destination = edgeURL ?? (direct ? url : legacyInteractionURL!)
        let changed = lastWebURL != destination || (direct && (process == nil || self.instance != instance || completedUpdate))
        if direct { legacyInteractionURL = nil }
        // Use retained code even if Sparkle has already replaced the bundle.
        let retained = URL(fileURLWithPath: payload)
        let config = try JSONDecoder().decode(BackendConfiguration.self, from: Data(contentsOf: retained.appendingPathComponent("backend.json")))
        let pending = status["pending"] as? String
        if pending != pendingUpdate {
            pendingUpdateVersion = pending.flatMap { path in
                guard let data = try? Data(contentsOf: URL(fileURLWithPath: path).appendingPathComponent("backend.json")),
                      let metadata = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return nil }
                return metadata["runtime_version"] as? String
            }
        }
        pendingUpdate = pending
        supportsRuntimeUpdates = (status["runtime_updates"] as? Int ?? 0) >= 1
        supportsUninterruptedUpdates = (status["uninterrupted_updates"] as? Int ?? 0) >= 1
        continuesInterruptedProviders = (status["interrupted_provider_continuation"] as? Int ?? 0) >= 1
        pendingUpdateRequiresConfirmation = (status["runtime_update_prompts"] as? Int ?? 0) >= 1
            && status["pending_confirmation"] as? Bool == true
        self.instance = instance
        process = RuntimeProcess(processIdentifier: pid)
        failures = 0
        lastStatusFailure = nil
        helperResources = retained
        python = config.pythonURL(resources: retained)
        if let database = database {
            let db = URL(fileURLWithPath: database).resolvingSymlinksInPath()
            logURL = db.deletingLastPathComponent().appendingPathComponent("." + db.lastPathComponent + ".runtime/runtime.log")
        }
        if let terminal = terminalUpdate {
            reportedUpdate = terminal.key
            expectedUpdate = nil
            if completedUpdate { updateStatus?(terminal.message, terminal.failed) }
        }
        if changed { lastWebURL = destination; ready?(destination) }
        updateAvailable?()
    }

    private func poll() {
        guard !polling, !stopping else { return }
        polling = true
        if let edgeURL = edgeURL, let observer = edgeObserver {
            var request = URLRequest(url: edgeURL.appendingPathComponent("_runtime/health"))
            request.setValue(observer, forHTTPHeaderField: "X-Ribbon-Runtime-Observer")
            healthSession.dataTask(with: request) { [weak self] data, response, error in
                DispatchQueue.main.async {
                    guard let self = self else { return }
                    self.polling = false
                    guard !self.stopping else { return }
                    do {
                        if let error = error { throw error }
                        guard let response = response as? HTTPURLResponse, response.statusCode == 200,
                              let data = data, let status = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                              status["status"] as? String != "unavailable" else {
                            throw Self.failure("The local coordinator is unavailable.")
                        }
                        if status["status"] as? String == "updating" {
                            self.failures = 0
                        } else {
                            try self.accept(status)
                        }
                    } catch {
                        self.failures += 1
                        if self.failures == 15 { self.updateStatus?(error.localizedDescription, true) }
                    }
                }
            }.resume()
            return
        }
        worker.async {
            do {
                let status = try self.command(["--runtime-command", "status", "--resources", self.resources!.path, "--runtime-interaction-continuity", "--runtime-stable-origin"])
                DispatchQueue.main.async {
                    self.polling = false
                    do { try self.accept(status) }
                    catch {
                        if self.lastStatusFailure != error.localizedDescription {
                            self.lastStatusFailure = error.localizedDescription
                            self.updateStatus?(error.localizedDescription, true)
                        }
                    }
                }
            } catch {
                DispatchQueue.main.async {
                    self.polling = false
                    self.failures += 1
                    if self.failures == 15 { self.process = nil; self.lastWebURL = nil; self.exited?(error.localizedDescription) }
                }
            }
        }
    }

    func preflight(_ target: [String: Any], completion: @escaping (Error?) -> Void) {
        do { try validateCompatibility(target) } catch { completion(error); return }
        worker.async {
            do {
                var input: [String: Any] = ["target": target, "authorize_restart": true]
                if let proof = self.stoppedInstance { input["stopped_instance"] = proof }
                _ = try self.command(["--runtime-command", "decision"], input: input)
                DispatchQueue.main.async { completion(nil) }
            } catch { DispatchQueue.main.async { completion(error) } }
        }
    }

    func applyUpdate(completion: @escaping (Error?) -> Void) {
        guard supportsRuntimeUpdates, let payload = pendingUpdate else {
            completion(Self.failure("No supported backend update is staged.")); return
        }
        if expectedUpdate != nil { completion(Self.failure("The requested backend update is still being observed.")); return }
        let request = interactionRequestID ?? UUID().uuidString
        if interactionURL != nil { interactionRequestID = request }
        expectedUpdate = (request, payload)
        worker.async {
            do {
                let result = try self.command(["--runtime-command", "activate-update", "--runtime-interaction-continuity", "--runtime-stable-origin"],
                    input: ["request_id": request, "payload": payload])
                guard let update = result["update"] as? [String: Any], update["id"] as? String == request else {
                    throw Self.failure("The runtime did not confirm the update request.")
                }
                DispatchQueue.main.async {
                    self.pendingUpdate = nil
                    if self.interactionURL == nil { self.updateStatus?("Updating backend…", false) }
                    completion(nil)
                }
            } catch { DispatchQueue.main.async { self.expectedUpdate = nil; completion(error) } }
        }
    }

    func cancelUpdateAuthorization() {
        worker.async { _ = try? self.command(["--runtime-command", "cancel-update-authorization"]) }
    }

    func stop(completion: @escaping (Error?) -> Void) {
        stopping = true
        timer?.invalidate()
        timer = nil
        worker.async {
            do {
                let status = try self.command(["--runtime-command", "stop"])
                guard status["status"] as? String == "stopped" else { throw Self.failure("Runtime exit was not confirmed.") }
                DispatchQueue.main.async {
                    self.stoppedInstance = status["instance"] as? String
                    self.process = nil
                    completion(nil)
                }
            } catch { DispatchQueue.main.async {
                self.stopping = false
                self.timer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in self?.poll() }
                completion(error)
            } }
        }
    }

    func detach() { timer?.invalidate(); timer = nil }
}
