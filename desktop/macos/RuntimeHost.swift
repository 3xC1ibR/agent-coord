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

final class Backend {
    var process: RuntimeProcess?
    var ready: ((URL) -> Void)?
    var exited: ((String) -> Void)?
    private(set) var logURL: URL
    private var resources: URL?
    private var configuration: BackendConfiguration?
    private var database: String?
    private var python: URL?
    private var helperResources: URL?
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
        guard let python = python, let resources = helperResources,
              let config = configuration, let database = database else {
            throw Self.failure("The runtime has not been configured.")
        }
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
        let timeout: TimeInterval = arguments.contains("--runtime-bootstrap") ? 90 : (arguments.contains("stop") ? 35 : 10)
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
                let status = try self.command(["--runtime-bootstrap", "--resources", resources.path])
                DispatchQueue.main.async {
                    self.starting = false
                    do { try self.accept(status) }
                    catch { self.exited?(error.localizedDescription); return }
                    self.timer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in self?.poll() }
                }
            } catch { DispatchQueue.main.async { self.starting = false; self.exited?(error.localizedDescription) } }
        }
    }

    private func accept(_ status: [String: Any]) throws {
        guard let value = status["url"] as? String, let url = URL(string: value),
              url.scheme == "http", url.host == "127.0.0.1", url.port != nil,
              url.user == nil, url.password == nil, url.path == "/",
              let pid = status["pid"] as? Int32, let instance = status["instance"] as? String,
              let payload = status["payload"] as? String else { throw Self.failure("Invalid runtime handshake.") }
        let changed = self.instance != instance
        self.instance = instance
        process = RuntimeProcess(processIdentifier: pid)
        failures = 0
        // Use retained code even if Sparkle has already replaced the bundle.
        let retained = URL(fileURLWithPath: payload)
        let config = try JSONDecoder().decode(BackendConfiguration.self, from: Data(contentsOf: retained.appendingPathComponent("backend.json")))
        helperResources = retained
        python = config.pythonURL(resources: retained)
        if let database = database {
            let db = URL(fileURLWithPath: database).resolvingSymlinksInPath()
            logURL = db.deletingLastPathComponent().appendingPathComponent("." + db.lastPathComponent + ".runtime/runtime.log")
        }
        if changed { ready?(url) }
    }

    private func poll() {
        guard !polling, !stopping else { return }
        polling = true
        worker.async {
            do {
                let status = try self.command(["--runtime-command", "status"])
                DispatchQueue.main.async { self.polling = false; try? self.accept(status) }
            } catch {
                DispatchQueue.main.async {
                    self.polling = false
                    self.failures += 1
                    if self.failures == 15 { self.process = nil; self.exited?(error.localizedDescription) }
                }
            }
        }
    }

    func preflight(_ target: [String: Any], completion: @escaping (Error?) -> Void) {
        do { try validateCompatibility(target) } catch { completion(error); return }
        worker.async {
            do {
                var input: [String: Any] = ["target": target]
                if let proof = self.stoppedInstance { input["stopped_instance"] = proof }
                _ = try self.command(["--runtime-command", "decision"], input: input)
                DispatchQueue.main.async { completion(nil) }
            } catch { DispatchQueue.main.async { completion(error) } }
        }
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
            } catch { DispatchQueue.main.async { self.stopping = false; completion(error) } }
        }
    }

    func detach() { timer?.invalidate(); timer = nil }
}
