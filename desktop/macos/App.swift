import AppKit
import WebKit
import UserNotifications
import Darwin

private struct BackendConfiguration: Decodable {
    let python: String
    let database: String
    let path: String
}

// Only this Process is owned by the app. Never attach to or stop a server found
// on a well-known port: it may belong to a terminal or another installation.
private final class Backend {
    var process: Process?
    var ready: ((URL) -> Void)?
    var exited: ((String) -> Void)?
    var stopped: (() -> Void)?
    private var input: Pipe?
    private var output: Pipe?
    private var log: FileHandle?
    private var buffer = Data()
    private var generation = UUID()
    private var stopping = false
    private var receivedURL = false
    let logURL: URL

    init(directory: URL) { logURL = directory.appendingPathComponent("backend.log") }

    func start(resources: URL, configuration: BackendConfiguration, database: String?) throws {
        guard process == nil else { return }
        generation = UUID()
        let current = generation
        stopping = false
        receivedURL = false
        buffer.removeAll()
        let manager = FileManager.default
        if (try? manager.attributesOfItem(atPath: logURL.path)[.size] as? NSNumber)?.intValue ?? 0 > 1_000_000 {
            let previous = logURL.appendingPathExtension("previous")
            try? manager.removeItem(at: previous)
            try? manager.moveItem(at: logURL, to: previous)
        }
        if !manager.fileExists(atPath: logURL.path) { manager.createFile(atPath: logURL.path, contents: nil) }
        log = try FileHandle(forWritingTo: logURL)
        try log?.seekToEnd()
        let child = Process(), input = Pipe(), output = Pipe()
        child.executableURL = URL(fileURLWithPath: configuration.python)
        child.arguments = ["-u", resources.appendingPathComponent("backend.py").path,
                           "--db", database ?? configuration.database]
        var environment = ProcessInfo.processInfo.environment
        // Finder has a minimal PATH. Capture tool locations at build time, and
        // avoid inheriting the session identity of a terminal that opens the app.
        for key in Array(environment.keys) where key.hasPrefix("AGENT_COORD_") || key.hasPrefix("ZELLIJ") || key == "CODEX_THREAD_ID" {
            environment.removeValue(forKey: key)
        }
        environment["PATH"] = configuration.path
        environment["PYTHONPATH"] = resources.appendingPathComponent("backend").path
        environment["PYTHONNOUSERSITE"] = "1"
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        child.environment = environment
        child.currentDirectoryURL = manager.homeDirectoryForCurrentUser
        child.standardInput = input
        child.standardOutput = output
        child.standardError = log
        child.terminationHandler = { [weak self] child in
            DispatchQueue.main.async {
                guard let self = self, self.generation == current else { return }
                self.process = nil
                self.output?.fileHandleForReading.readabilityHandler = nil
                try? self.output?.fileHandleForReading.close()
                try? self.input?.fileHandleForWriting.close()
                try? self.log?.close()
                self.input = nil
                self.output = nil
                self.log = nil
                if self.stopping { self.stopped?(); self.stopped = nil }
                else { self.exited?("The local server stopped (exit \(child.terminationStatus)). Open the log for details, then try again.") }
            }
        }
        output.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if data.isEmpty { handle.readabilityHandler = nil; return }
            DispatchQueue.main.async {
                guard let self = self, self.generation == current, !self.stopping else { return }
                self.buffer.append(data)
                while let newline = self.buffer.firstIndex(of: 10) {
                    let line = self.buffer.prefix(upTo: newline)
                    self.buffer.removeSubrange(...newline)
                    if let object = try? JSONSerialization.jsonObject(with: line) as? [String: String],
                       object["status"] == "serving", let value = object["url"], let url = URL(string: value),
                       url.scheme == "http", url.host == "127.0.0.1", let port = url.port, (1...65535).contains(port),
                       url.path == "/", url.user == nil, url.password == nil {
                        self.receivedURL = true
                        self.ready?(url)
                    }
                }
                if self.buffer.count > 65536 { self.buffer.removeAll() }
            }
        }
        self.input = input
        self.output = output
        process = child
        do { try child.run() }
        catch {
            process = nil
            output.fileHandleForReading.readabilityHandler = nil
            try? input.fileHandleForWriting.close()
            try? output.fileHandleForReading.close()
            try? log?.close()
            throw error
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 20) { [weak self] in
            guard let self = self, self.generation == current, self.process != nil,
                  !self.receivedURL, !self.stopping else { return }
            self.stop { self.exited?("The local server did not start within 20 seconds. Open the log for details, then try again.") }
        }
    }

    func stop(completion: @escaping () -> Void) {
        guard let child = process else { completion(); return }
        stopping = true
        stopped = completion
        // EOF asks the adapter to close its server and Codex connection cleanly.
        try? input?.fileHandleForWriting.close()
        input = nil
        DispatchQueue.main.asyncAfter(deadline: .now() + 8) {
            if child.isRunning { child.terminate() }
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 12) {
            if child.isRunning { kill(child.processIdentifier, SIGKILL) }
        }
    }
}

private final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate,
    WKUIDelegate, WKScriptMessageHandlerWithReply, UNUserNotificationCenterDelegate {
    private var window: NSWindow!
    private var webView: WKWebView!
    private var status: NSStackView!
    private var statusLabel: NSTextField!
    private var spinner: NSProgressIndicator!
    private var retry: NSButton!
    private var backend: Backend!
    private var serverURL: URL?
    private var configuration: BackendConfiguration!
    private var resources: URL!
    private var quitting = false
    private var checkingQuit = false
    private var lockFile: Int32 = -1
    private var activity: NSObjectProtocol?
    private var preferences = UserDefaults.standard
    private var smokeReport: URL?
    private var smokeDirectory: URL?
    private var smokeStage = 0
    private var smokeResults: [String: Any] = [:]
    private var smokePreferences: String?

    func applicationDidFinishLaunching(_ notification: Notification) {
        do {
            resources = Bundle.main.resourceURL!
            configuration = try JSONDecoder().decode(BackendConfiguration.self,
                from: Data(contentsOf: resources.appendingPathComponent("backend.json")))
            var directory = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
                .appendingPathComponent("Agent Coord", isDirectory: true)
            if let flag = CommandLine.arguments.firstIndex(of: "--smoke-test"), flag + 1 < CommandLine.arguments.count {
                smokeReport = URL(fileURLWithPath: CommandLine.arguments[flag + 1])
                directory = FileManager.default.temporaryDirectory.appendingPathComponent("agent-coord-smoke-" + UUID().uuidString)
                smokeDirectory = directory
                smokePreferences = "com.agentcoord.desktop.smoke." + UUID().uuidString
                preferences = UserDefaults(suiteName: smokePreferences!)!
            }
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            lockFile = open(directory.appendingPathComponent("app.lock").path, O_CREAT | O_RDWR, S_IRUSR | S_IWUSR)
            guard lockFile >= 0 else { throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno)) }
            guard flock(lockFile, LOCK_EX | LOCK_NB) == 0 else {
                NSRunningApplication.runningApplications(withBundleIdentifier: Bundle.main.bundleIdentifier!).first {
                    $0.processIdentifier != ProcessInfo.processInfo.processIdentifier
                }?.activate(options: [.activateAllWindows])
                NSApp.terminate(nil)
                return
            }
            backend = Backend(directory: directory)
            makeMenu()
            makeWindow()
            UNUserNotificationCenter.current().delegate = self
            activity = ProcessInfo.processInfo.beginActivity(options: .userInitiatedAllowingIdleSystemSleep,
                reason: "Receive Agent Coord turn completions while the window is closed")
            backend.ready = { [weak self] url in
                guard let self = self else { return }
                self.serverURL = url
                self.configureScripts()
                self.webView.load(URLRequest(url: url))
            }
            backend.exited = { [weak self] message in
                guard let self = self, !self.quitting else { return }
                self.serverURL = nil
                self.showStatus(message, failed: true)
                if self.smokeReport != nil { self.finishSmoke(error: message) }
            }
            startBackend()
            if smokeReport != nil {
                DispatchQueue.main.asyncAfter(deadline: .now() + 35) { [weak self] in
                    guard let self = self, !self.quitting else { return }
                    self.finishSmoke(error: "Native smoke test timed out")
                }
            }
        } catch {
            if smokeReport != nil { finishSmoke(error: error.localizedDescription); return }
            let alert = NSAlert()
            alert.messageText = "Agent Coord could not start"
            alert.informativeText = error.localizedDescription
            alert.runModal()
            NSApp.terminate(nil)
        }
    }

    private func makeMenu() {
        let main = NSMenu()
        func menu(_ title: String) -> NSMenu {
            let item = NSMenuItem(); main.addItem(item)
            let submenu = NSMenu(title: title); item.submenu = submenu
            return submenu
        }
        let app = menu("Agent Coord")
        app.addItem(withTitle: "About Agent Coord", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        app.addItem(.separator())
        app.addItem(withTitle: "Hide Agent Coord", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        app.addItem(.separator())
        app.addItem(withTitle: "Quit Agent Coord", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        let file = menu("File")
        file.addItem(withTitle: "Show Workspace", action: #selector(showWorkspace), keyEquivalent: "1").target = self
        file.addItem(withTitle: "Close Window", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        let edit = menu("Edit")
        for (title, action, key) in [("Undo", "undo:", "z"), ("Cut", "cut:", "x"), ("Copy", "copy:", "c"),
                                      ("Paste", "paste:", "v"), ("Select All", "selectAll:", "a")] {
            edit.addItem(withTitle: title, action: Selector(action), keyEquivalent: key)
        }
        let redo = edit.insertItem(withTitle: "Redo", action: NSSelectorFromString("redo:"), keyEquivalent: "z", at: 1)
        redo.keyEquivalentModifierMask = [.command, .shift]
        let view = menu("View")
        view.addItem(withTitle: "Reload", action: #selector(reload), keyEquivalent: "r").target = self
        view.addItem(withTitle: "Back", action: #selector(goBack), keyEquivalent: "[").target = self
        view.addItem(withTitle: "Open in Browser", action: #selector(openBrowser), keyEquivalent: "").target = self
        let windows = menu("Window")
        windows.addItem(withTitle: "Minimize", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        windows.addItem(withTitle: "Show Agent Coord", action: #selector(showWindow), keyEquivalent: "").target = self
        NSApp.windowsMenu = windows
        let help = menu("Help")
        help.addItem(withTitle: "Open Backend Log", action: #selector(openLog), keyEquivalent: "").target = self
        NSApp.mainMenu = main
    }

    private func makeWindow() {
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1240, height: 820),
            styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "Agent Coord"
        window.minSize = NSSize(width: 720, height: 520)
        window.isReleasedWhenClosed = false
        if smokeReport == nil { window.setFrameAutosaveName("AgentCoordWorkspace") }
        window.center()
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .nonPersistent()
        config.userContentController.addScriptMessageHandler(self, contentWorld: .page, name: "desktop")
        webView = WKWebView(frame: .zero, configuration: config)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.autoresizingMask = [.width, .height]
        webView.frame = window.contentView!.bounds
        window.contentView!.addSubview(webView)
        spinner = NSProgressIndicator()
        spinner.style = .spinning
        statusLabel = NSTextField(wrappingLabelWithString: "Starting Agent Coord…")
        statusLabel.alignment = .center
        statusLabel.font = .systemFont(ofSize: 17)
        retry = NSButton(title: "Try Again", target: self, action: #selector(retryLoad))
        let log = NSButton(title: "Open Log", target: self, action: #selector(openLog))
        status = NSStackView(views: [spinner, statusLabel, retry, log])
        status.orientation = .vertical
        status.spacing = 18
        status.translatesAutoresizingMaskIntoConstraints = false
        window.contentView!.addSubview(status)
        NSLayoutConstraint.activate([
            status.centerXAnchor.constraint(equalTo: window.contentView!.centerXAnchor),
            status.centerYAnchor.constraint(equalTo: window.contentView!.centerYAnchor),
            status.widthAnchor.constraint(equalToConstant: 480),
        ])
        showWindow()
    }

    @objc private func showWindow() {
        window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }
    @objc private func showWorkspace() {
        showWindow()
        if let url = serverURL { webView.load(URLRequest(url: url)) }
    }
    @objc private func reload() { if serverURL != nil { webView.reload() } else { startBackend() } }
    @objc private func retryLoad() {
        if let url = serverURL {
            showStatus("Opening Agent Coord…", failed: false)
            webView.load(URLRequest(url: url))
        } else { startBackend() }
    }
    @objc private func goBack() { if webView.canGoBack { webView.goBack() } }
    @objc private func openBrowser() { if let url = webView.url, isLocal(url) { NSWorkspace.shared.open(url) } }
    @objc private func openLog() { if let url = backend?.logURL { NSWorkspace.shared.open(url) } }

    private func showStatus(_ message: String, failed: Bool) {
        statusLabel.stringValue = message
        status.isHidden = false
        webView.isHidden = true
        retry.isHidden = !failed
        spinner.isHidden = failed
        if failed { spinner.stopAnimation(nil) } else { spinner.startAnimation(nil) }
    }

    @objc private func startBackend() {
        guard backend.process == nil else { return }
        showStatus("Starting Agent Coord…", failed: false)
        do {
            try backend.start(resources: resources, configuration: configuration,
                database: smokeDirectory?.appendingPathComponent("state.sqlite3").path)
        } catch {
            showStatus("Could not launch Python. Rebuild the app if Python moved.\n\n" + error.localizedDescription, failed: true)
            if smokeReport != nil { finishSmoke(error: error.localizedDescription) }
        }
    }

    private func isLocal(_ url: URL) -> Bool {
        guard let base = serverURL else { return false }
        return url.scheme == base.scheme && url.host == base.host && url.port == base.port && url.user == nil && url.password == nil
    }

    private func configureScripts() {
        guard let source = try? String(contentsOf: resources.appendingPathComponent("bridge.js"), encoding: .utf8),
              let data = try? JSONSerialization.data(withJSONObject: preferences.dictionary(forKey: "webPreferences") ?? [:]),
              let json = String(data: data, encoding: .utf8) else { return }
        let controller = webView.configuration.userContentController
        controller.removeAllUserScripts()
        controller.addUserScript(WKUserScript(source: source.replacingOccurrences(of: "__AGENT_COORD_PREFERENCES__", with: json),
            injectionTime: .atDocumentStart, forMainFrameOnly: true))
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url else { decisionHandler(.cancel); return }
        if isLocal(url) {
            configureScripts()
            decisionHandler(.allow)
        } else {
            decisionHandler(.cancel)
            if navigationAction.navigationType == .linkActivated, ["https", "http", "mailto"].contains(url.scheme ?? "") {
                NSWorkspace.shared.open(url)
            }
        }
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = navigationAction.request.url, isLocal(url) { webView.load(URLRequest(url: url)) }
        return nil
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        status.isHidden = true
        spinner.stopAnimation(nil)
        webView.isHidden = false
        if smokeReport != nil { checkSmoke(attempt: 0) }
    }
    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        if (error as NSError).code != NSURLErrorCancelled { showStatus(error.localizedDescription, failed: true) }
    }
    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        if (error as NSError).code != NSURLErrorCancelled { showStatus(error.localizedDescription, failed: true) }
    }
    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) { webView.reload() }

    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.beginSheetModal(for: window) { response in completionHandler(response == .OK ? panel.urls : nil) }
    }

    private func permission(_ completion: @escaping (String) -> Void) {
        UNUserNotificationCenter.current().getNotificationSettings { settings in
            let value = settings.authorizationStatus == .notDetermined ? "default" :
                settings.authorizationStatus == .denied ? "denied" : "granted"
            DispatchQueue.main.async { completion(value) }
        }
    }

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage,
                               replyHandler: @escaping (Any?, String?) -> Void) {
        guard message.frameInfo.isMainFrame, let url = message.frameInfo.request.url, isLocal(url),
              let body = message.body as? [String: Any], let action = body["action"] as? String else {
            replyHandler(nil, "Only the local Agent Coord window can use desktop features."); return
        }
        let center = UNUserNotificationCenter.current()
        switch action {
        case "permission": permission { replyHandler($0, nil) }
        case "requestPermission":
            center.requestAuthorization(options: [.alert, .sound, .badge]) { [weak self] _, error in
                DispatchQueue.main.async {
                    if let error = error { replyHandler(nil, error.localizedDescription) }
                    else { self?.permission { replyHandler($0, nil) } }
                }
            }
        case "notify":
            guard let id = body["id"] as? String, UUID(uuidString: id) != nil,
                  let title = body["title"] as? String, let text = body["body"] as? String else {
                replyHandler(nil, "Invalid notification."); return
            }
            let content = UNMutableNotificationContent()
            content.title = String(title.prefix(256))
            content.body = String(text.prefix(2000))
            content.sound = .default
            center.add(UNNotificationRequest(identifier: id, content: content, trigger: nil)) { error in
                replyHandler(error == nil, error?.localizedDescription)
            }
        case "closeNotification":
            if let id = body["id"] as? String { center.removeDeliveredNotifications(withIdentifiers: [id]) }
            replyHandler(true, nil)
        case "preference":
            guard let key = body["key"] as? String, key.count < 256,
                  key.hasPrefix("agent-coord.") || key == "agent-coord-group-by" else {
                replyHandler(nil, "Invalid preference."); return
            }
            var values = preferences.dictionary(forKey: "webPreferences") as? [String: String] ?? [:]
            if let value = body["value"] as? String, value.count <= 65536 { values[key] = value }
            else { values.removeValue(forKey: key) }
            preferences.set(values, forKey: "webPreferences")
            replyHandler(true, nil)
        default: replyHandler(nil, "Unknown desktop request.")
        }
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .sound])
    }
    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                withCompletionHandler completionHandler: @escaping () -> Void) {
        DispatchQueue.main.async {
            self.showWindow()
            let id = response.notification.request.identifier
            if let data = try? JSONSerialization.data(withJSONObject: [id]), let json = String(data: data, encoding: .utf8) {
                self.webView?.evaluateJavaScript("window.__agentCoordNotificationClick?.(\(json)[0])")
            }
            completionHandler()
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool { showWindow(); return true }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if quitting { return .terminateLater }
        guard backend?.process != nil else { return .terminateNow }
        if checkingQuit { return .terminateCancel }
        checkingQuit = true
        guard let url = serverURL?.appendingPathComponent("api/browser/sessions") else {
            stopAndQuit(); return .terminateLater
        }
        var request = URLRequest(url: url)
        request.timeoutInterval = 3
        URLSession.shared.dataTask(with: request) { data, response, error in
            let object = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
            let sessions = object?["data"] as? [[String: Any]]
            let count = sessions?.filter { ["running", "needs input"].contains($0["status"] as? String ?? "") }.count
            DispatchQueue.main.async {
                if count == 0 || self.smokeReport != nil { self.stopAndQuit(); return }
                let alert = NSAlert()
                alert.messageText = count == nil ? "Quit Agent Coord?" : "Stop running work and quit?"
                alert.informativeText = count == nil ?
                    "The app could not check for active turns. Quitting stops this app’s local server. Close the window to keep work running." :
                    "\(count!) session(s) in this app are working or waiting for input. Quitting stops them. Close the window to keep work running; saved conversations remain available after a restart."
                alert.addButton(withTitle: "Keep Working")
                alert.addButton(withTitle: "Quit and Stop")
                self.showWindow()
                if alert.runModal() == .alertSecondButtonReturn { self.stopAndQuit() }
                else { self.checkingQuit = false; NSApp.reply(toApplicationShouldTerminate: false) }
            }
        }.resume()
        return .terminateLater
    }

    private func stopAndQuit() {
        quitting = true
        backend.stop { NSApp.reply(toApplicationShouldTerminate: true) }
    }
    func applicationWillTerminate(_ notification: Notification) {
        if let activity = activity { ProcessInfo.processInfo.endActivity(activity) }
        if lockFile >= 0 { flock(lockFile, LOCK_UN); close(lockFile) }
        if let name = smokePreferences { preferences.removePersistentDomain(forName: name) }
        if let directory = smokeDirectory { try? FileManager.default.removeItem(at: directory) }
    }

    // Native smoke mode uses a disposable database and preference domain. It
    // exercises the actual WKWebView and window lifecycle without model turns.
    private func checkSmoke(attempt: Int) {
        guard !quitting, smokeStage < 2 else { return }
        webView.evaluateJavaScript("document.querySelector('#thread-count')?.textContent === '0' && Notification.agentCoordNative === true") { result, error in
            guard !self.quitting else { return }
            guard result as? Bool == true else {
                if attempt < 40 {
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) { self.checkSmoke(attempt: attempt + 1) }
                } else { self.finishSmoke(error: error?.localizedDescription ?? "UI did not boot in WKWebView") }
                return
            }
            if self.smokeStage == 0 {
                self.smokeStage = 1
                self.smokeResults["ui_loaded"] = true
                self.smokeResults["notification_bridge"] = true
                self.smokeResults["backend_pid"] = self.backend.process?.processIdentifier
                self.smokeResults["url"] = self.serverURL?.absoluteString
                self.webView.evaluateJavaScript("localStorage.setItem('agent-coord.smoke', 'persisted')") { _, _ in
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) {
                        self.window.close()
                        self.smokeResults["window_closed"] = !self.window.isVisible
                        self.smokeResults["backend_survived_close"] = self.backend.process?.isRunning == true
                        self.showWindow()
                        self.smokeResults["window_reopened"] = self.window.isVisible
                        self.webView.reload()
                    }
                }
            } else {
                self.smokeStage = 2
                self.smokeResults["preferences_persisted"] =
                    (self.preferences.dictionary(forKey: "webPreferences")?["agent-coord.smoke"] as? String) == "persisted"
                self.webView.takeSnapshot(with: nil) { image, error in
                    if let data = image?.tiffRepresentation, let bitmap = NSBitmapImageRep(data: data),
                       let png = bitmap.representation(using: .png, properties: [:]) {
                        try? png.write(to: self.smokeReport!.appendingPathExtension("png"))
                    }
                    self.finishSmoke(error: error?.localizedDescription)
                }
            }
        }
    }

    private func finishSmoke(error: String?) {
        guard let report = smokeReport, !quitting else { return }
        smokeResults["error"] = error ?? NSNull()
        if let data = try? JSONSerialization.data(withJSONObject: smokeResults, options: [.prettyPrinted, .sortedKeys]) {
            try? data.write(to: report)
        }
        NSApp.terminate(nil)
    }
}

let app = NSApplication.shared
private let delegate = AppDelegate()
app.setActivationPolicy(.regular)
app.delegate = delegate
app.run()
