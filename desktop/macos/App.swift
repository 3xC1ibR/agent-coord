import AppKit
import WebKit
import UserNotifications
import Darwin

private struct BackendConfiguration: Decodable {
    let python: String
    let database: String?
    let path: String?

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
        let state = environment["XDG_STATE_HOME"] ?? home + "/.local/state"
        return (state as NSString).expandingTildeInPath + "/agent-coord/state.sqlite3"
    }

    var toolPath: String {
        if let path = path { return path }
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        return ([home + "/.local/bin", home + "/.npm-global/bin", "/opt/homebrew/bin",
                 "/usr/local/bin", ProcessInfo.processInfo.environment["PATH"] ?? "",
                 "/usr/bin", "/bin", "/usr/sbin", "/sbin"]).filter { !$0.isEmpty }.joined(separator: ":")
    }
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
        child.executableURL = configuration.pythonURL(resources: resources)
        child.arguments = ["-u", resources.appendingPathComponent("backend.py").path,
                           "--db", database ?? configuration.databasePath]
        var environment = ProcessInfo.processInfo.environment
        // Finder has a minimal PATH. Portable builds use this user's tool locations.
        // Avoid inheriting the session identity of a terminal that opens the app.
        for key in Array(environment.keys) where key.hasPrefix("AGENT_COORD_") || key.hasPrefix("ZELLIJ") || key == "CODEX_THREAD_ID" {
            environment.removeValue(forKey: key)
        }
        environment.removeValue(forKey: "PYTHONHOME")
        environment["PATH"] = configuration.toolPath
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

private final class SessionWindow: NSWindow {
    var cycleSession: ((Bool) -> Bool)?

    func routeSessionShortcut(_ event: NSEvent) -> Bool {
        let modifiers = event.modifierFlags.intersection([.control, .shift, .command, .option])
        // Control-Tab can be consumed as focus navigation before menu key
        // equivalents run. Route it before AppKit/WebKit handles the key.
        if event.type == .keyDown, event.keyCode == 48, isKeyWindow,
           attachedSheet == nil, NSApp.modalWindow == nil,
           modifiers == [.control] || modifiers == [.control, .shift],
           cycleSession?(modifiers.contains(.shift)) == true {
            return true
        }
        return false
    }

    override func sendEvent(_ event: NSEvent) {
        if routeSessionShortcut(event) { return }
        super.sendEvent(event)
    }
}

private final class DesktopWindow {
    let id = UUID().uuidString.lowercased()
    let window: NSWindow
    let webView: WKWebView
    let status: NSStackView
    let label: NSTextField
    let spinner: NSProgressIndicator
    let retry: NSButton
    var commands = Set<String>()
    var loaded = false
    var navigationReady = false
    var navigating = false
    var pendingLinks: [URL] = []
    let files = FileBrowserController()
    let split = NSSplitViewController()
    var filesItem: NSSplitViewItem?
    var fileTarget = ""

    init(window: NSWindow, webView: WKWebView, status: NSStackView, label: NSTextField,
         spinner: NSProgressIndicator, retry: NSButton) {
        self.window = window; self.webView = webView; self.status = status
        self.label = label; self.spinner = spinner; self.retry = retry
    }
}

private final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, NSMenuItemValidation,
    WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandlerWithReply, UNUserNotificationCenterDelegate {
    private var windows: [DesktopWindow] = []
    // Share local storage (including notification focus) while each web view
    // retains its own session storage, selected thread, and message drafts.
    private let websiteDataStore = WKWebsiteDataStore.nonPersistent()
    private var current: DesktopWindow? {
        windows.first { $0.window === NSApp.keyWindow } ??
        windows.first { $0.window === NSApp.mainWindow } ?? windows.last
    }
    private var window: NSWindow! { current?.window }
    private var webView: WKWebView! { current?.webView }
    private var backend: Backend!
    private var serverURL: URL?
    private var pendingAppLinks: [URL] = []
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
    private var smokePasteboard: NSPasteboard?
    private var sessionKeyMonitor: Any?

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
                application(NSApp, open: [URL(string: "agentcoord://overview")!])
                smokeResults["cold_start_link_queued"] = pendingAppLinks.count == 1
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
            sessionKeyMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { event in
                let target = (event.window ?? NSApp.keyWindow) as? SessionWindow
                return target?.routeSessionShortcut(event) == true ? nil : event
            }
            makeWindow()
            UNUserNotificationCenter.current().delegate = self
            activity = ProcessInfo.processInfo.beginActivity(options: .userInitiatedAllowingIdleSystemSleep,
                reason: "Receive Ribbon Field turn completions while the window is closed")
            backend.ready = { [weak self] url in
                guard let self = self else { return }
                self.serverURL = url
                for item in self.windows {
                    item.files.connect(BackendWorkspaceFiles(baseURL: url, local: true))
                    self.configureScripts(item.webView)
                    item.webView.load(URLRequest(url: url))
                }
            }
            backend.exited = { [weak self] message in
                guard let self = self, !self.quitting else { return }
                self.serverURL = nil
                self.showStatus(message, failed: true)
                if self.smokeReport != nil { self.finishSmoke(error: message) }
            }
            startBackend()
            let links = pendingAppLinks
            pendingAppLinks.removeAll()
            for link in links { openAppLink(link) }
            if smokeReport != nil {
                DispatchQueue.main.asyncAfter(deadline: .now() + 35) { [weak self] in
                    guard let self = self, !self.quitting else { return }
                    self.finishSmoke(error: "Native smoke test timed out")
                }
            }
        } catch {
            if smokeReport != nil { finishSmoke(error: error.localizedDescription); return }
            let alert = NSAlert()
            alert.messageText = "Ribbon Field could not start"
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
        let app = menu("Ribbon Field")
        app.addItem(withTitle: "About Ribbon Field", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        app.addItem(.separator())
        app.addItem(withTitle: "Hide Ribbon Field", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        app.addItem(.separator())
        app.addItem(withTitle: "Quit Ribbon Field", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        let file = menu("File")
        file.addItem(withTitle: "New Session", action: #selector(webCommand(_:)), keyEquivalent: "n").representedObject = "newSession"
        let newWindowItem = file.addItem(withTitle: "New Window", action: #selector(newWindow), keyEquivalent: "N")
        newWindowItem.keyEquivalentModifierMask = [.command, .shift]; newWindowItem.target = self
        let duplicate = file.addItem(withTitle: "Open Current View in New Window", action: #selector(duplicateWindow), keyEquivalent: "n")
        duplicate.keyEquivalentModifierMask = [.command, .option]; duplicate.target = self
        file.addItem(.separator())
        let insert = file.addItem(withTitle: "Insert File or Folder Paths…", action: #selector(webCommand(_:)), keyEquivalent: "O")
        insert.keyEquivalentModifierMask = [.command, .shift]; insert.representedObject = "insertFiles"
        file.addItem(withTitle: "Show Workspace", action: #selector(showWorkspace), keyEquivalent: "1").target = self
        file.addItem(withTitle: "Close Window", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        let edit = menu("Edit")
        for (title, action, key) in [("Undo", "undo:", "z"), ("Cut", "cut:", "x"), ("Copy", "copy:", "c"),
                                      ("Paste", "paste:", "v"), ("Select All", "selectAll:", "a")] {
            edit.addItem(withTitle: title, action: Selector(action), keyEquivalent: key)
        }
        let redo = edit.insertItem(withTitle: "Redo", action: NSSelectorFromString("redo:"), keyEquivalent: "Z", at: 1)
        redo.keyEquivalentModifierMask = [.command, .shift]
        edit.addItem(.separator())
        edit.addItem(withTitle: "Find Thread", action: #selector(webCommand(_:)), keyEquivalent: "f").representedObject = "findThread"
        edit.addItem(withTitle: "Focus Message", action: #selector(webCommand(_:)), keyEquivalent: "l").representedObject = "focusMessage"
        edit.addItem(withTitle: "Send Message", action: #selector(webCommand(_:)), keyEquivalent: "\r").representedObject = "sendMessage"
        let view = menu("View")
        view.addItem(withTitle: "Command Palette…", action: #selector(webCommand(_:)), keyEquivalent: "k").representedObject = "commandPalette"
        let nextSession = view.addItem(withTitle: "Next Session", action: #selector(webCommand(_:)), keyEquivalent: "\t")
        nextSession.keyEquivalentModifierMask = [.control]; nextSession.representedObject = "nextSession"
        let previousSession = view.addItem(withTitle: "Previous Session", action: #selector(webCommand(_:)), keyEquivalent: "\u{19}")
        previousSession.keyEquivalentModifierMask = [.control, .shift]; previousSession.representedObject = "previousSession"
        let rollUp = view.addItem(withTitle: "Roll Up Waiting Threads", action: #selector(webCommand(_:)), keyEquivalent: "r")
        rollUp.keyEquivalentModifierMask = [.command, .option]; rollUp.representedObject = "rollUp"
        let tiles = view.addItem(withTitle: "Toggle Tiled Threads", action: #selector(webCommand(_:)), keyEquivalent: "t")
        tiles.keyEquivalentModifierMask = [.command, .control, .option]; tiles.representedObject = "tileThreads"
        view.addItem(.separator())
        let sidebar = view.addItem(withTitle: "Toggle Sidebar", action: #selector(webCommand(_:)), keyEquivalent: "s")
        sidebar.keyEquivalentModifierMask = [.command, .option]; sidebar.representedObject = "toggleSidebar"
        let files = view.addItem(withTitle: "Toggle File Browser", action: #selector(toggleFiles), keyEquivalent: "b")
        files.keyEquivalentModifierMask = [.command, .option]; files.target = self
        let expand = view.addItem(withTitle: "Expand or Collapse Conversation", action: #selector(webCommand(_:)), keyEquivalent: "F")
        expand.keyEquivalentModifierMask = [.command, .shift]; expand.representedObject = "expandConversation"
        view.addItem(.separator())
        for (title, key, amount) in [("Zoom In", "+", 1), ("Zoom Out", "-", -1), ("Actual Size", "0", 0)] {
            let item = view.addItem(withTitle: title, action: #selector(zoomPage(_:)), keyEquivalent: key)
            item.tag = amount; item.target = self
        }
        view.addItem(.separator())
        view.addItem(withTitle: "Reload", action: #selector(reload), keyEquivalent: "r").target = self
        view.addItem(withTitle: "Back", action: #selector(goBack), keyEquivalent: "[").target = self
        view.addItem(withTitle: "Forward", action: #selector(goForward), keyEquivalent: "]").target = self
        view.addItem(withTitle: "Open in Browser", action: #selector(openBrowser), keyEquivalent: "").target = self
        let windows = menu("Window")
        windows.addItem(withTitle: "Minimize", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        windows.addItem(withTitle: "Zoom", action: #selector(NSWindow.performZoom(_:)), keyEquivalent: "")
        let fullScreen = windows.addItem(withTitle: "Toggle Full Screen", action: #selector(NSWindow.toggleFullScreen(_:)), keyEquivalent: "f")
        fullScreen.keyEquivalentModifierMask = [.command, .control]
        windows.addItem(withTitle: "Bring All to Front", action: #selector(NSApplication.arrangeInFront(_:)), keyEquivalent: "")
        windows.addItem(withTitle: "Show Ribbon Field", action: #selector(showWindow), keyEquivalent: "").target = self
        NSApp.windowsMenu = windows
        let help = menu("Help")
        help.addItem(withTitle: "Open Backend Log", action: #selector(openLog), keyEquivalent: "").target = self
        for submenu in [file, edit, view] {
            for item in submenu.items where item.action == #selector(webCommand(_:)) { item.target = self }
        }
        NSApp.mainMenu = main
    }

    @discardableResult private func makeWindow(url: URL? = nil) -> DesktopWindow {
        let previous = current?.window
        let window = SessionWindow(contentRect: NSRect(x: 0, y: 0, width: 1240, height: 820),
            styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = url?.path == "/monitor" ? "Coordination Monitor — Ribbon Field" : "Ribbon Field"
        window.minSize = NSSize(width: 720, height: 520)
        window.isReleasedWhenClosed = false
        window.delegate = self
        window.tabbingMode = .disallowed
        if smokeReport == nil && windows.isEmpty { window.setFrameAutosaveName("AgentCoordWorkspace") }
        if let previous = previous {
            window.setFrame(previous.frame.offsetBy(dx: 24, dy: -24), display: false)
        } else if smokeReport != nil || !window.setFrameUsingName("AgentCoordWorkspace") { window.center() }
        let config = WKWebViewConfiguration()
        config.websiteDataStore = websiteDataStore
        config.userContentController.addScriptMessageHandler(self, contentWorld: .page, name: "desktop")
        let webView = WKWebView(frame: .zero, configuration: config)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.autoresizingMask = [.width, .height]
        webView.frame = window.contentView!.bounds
        window.contentView!.addSubview(webView)
        let spinner = NSProgressIndicator()
        spinner.style = .spinning
        let statusLabel = NSTextField(wrappingLabelWithString: "Starting Ribbon Field…")
        statusLabel.alignment = .center
        statusLabel.font = .systemFont(ofSize: 17)
        let retry = NSButton(title: "Try Again", target: self, action: #selector(retryLoad))
        let log = NSButton(title: "Open Log", target: self, action: #selector(openLog))
        let status = NSStackView(views: [spinner, statusLabel, retry, log])
        status.orientation = .vertical
        status.spacing = 18
        status.translatesAutoresizingMaskIntoConstraints = false
        window.contentView!.addSubview(status)
        NSLayoutConstraint.activate([
            status.centerXAnchor.constraint(equalTo: window.contentView!.centerXAnchor),
            status.centerYAnchor.constraint(equalTo: window.contentView!.centerYAnchor),
            status.widthAnchor.constraint(equalToConstant: 480),
        ])
        let item = DesktopWindow(window: window, webView: webView, status: status, label: statusLabel,
                                 spinner: spinner, retry: retry)
        window.cycleSession = { [weak item] backwards in
            let command = backwards ? "previousSession" : "nextSession"
            guard let item = item, item.loaded, item.commands.contains(command) else { return false }
            item.webView.callAsyncJavaScript("return window.agentCoordDesktop?.command(command) ?? false",
                arguments: ["command": command], in: nil, in: .page) { _ in }
            return true
        }
        // Keep the native file pane independent of the conversation renderer.
        let chat = NSViewController()
        let content = window.contentView!
        chat.view = content
        item.split.addSplitViewItem(NSSplitViewItem(viewController: chat))
        let filePane = NSSplitViewItem(viewController: item.files)
        filePane.minimumThickness = 260; filePane.maximumThickness = 500
        filePane.canCollapse = true
        filePane.holdingPriority = .defaultHigh
        item.split.addSplitViewItem(filePane); item.filesItem = filePane
        window.contentViewController = item.split
        item.split.splitView.setPosition(max(440, window.frame.width - 310), ofDividerAt: 0)
        filePane.isCollapsed = !preferences.bool(forKey: "fileBrowserVisible")
        item.files.insertPaths = { [weak item] paths in
            guard let item = item, item.loaded else { return }
            item.webView.callAsyncJavaScript("return window.agentCoordDesktop.insertPaths(paths, target)",
                arguments: ["paths": paths, "target": item.fileTarget], in: nil, in: .page) { _ in }
        }
        windows.append(item)
        if let destination = url ?? serverURL {
            if let serverURL = serverURL { item.files.connect(BackendWorkspaceFiles(baseURL: serverURL, local: true)) }
            configureScripts(webView)
            webView.load(URLRequest(url: destination))
        }
        showStatus("Opening Ribbon Field…", failed: false, in: item)
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        return item
    }

    private func item(for view: WKWebView) -> DesktopWindow? { windows.first { $0.webView === view } }

    func windowWillClose(_ notification: Notification) {
        guard windows.count > 1, let closing = notification.object as? NSWindow,
              let item = windows.first(where: { $0.window === closing }) else { return }
        item.webView.stopLoading()
        item.webView.configuration.userContentController.removeScriptMessageHandler(forName: "desktop", contentWorld: .page)
        item.webView.navigationDelegate = nil
        item.webView.uiDelegate = nil
        item.webView.removeFromSuperview()
        closing.contentView = nil
        closing.delegate = nil
        windows.removeAll { $0 === item }
    }

    @objc private func newWindow() { makeWindow() }
    @objc private func duplicateWindow() { makeWindow(url: webView?.url) }
    @objc private func webCommand(_ sender: NSMenuItem) {
        guard let command = sender.representedObject as? String else { return }
        runCommand(command)
    }
    private func runCommand(_ command: String) {
        showWindow()
        webView?.callAsyncJavaScript("return window.agentCoordDesktop?.command(command) ?? false",
                                    arguments: ["command": command], in: nil, in: .page) { _ in }
    }
    @objc private func zoomPage(_ sender: NSMenuItem) {
        guard let view = webView else { return }
        view.pageZoom = sender.tag == 0 ? 1 : min(2, max(0.5, view.pageZoom + Double(sender.tag) * 0.1))
    }
    func validateMenuItem(_ menuItem: NSMenuItem) -> Bool {
        if let command = menuItem.representedObject as? String { return current?.commands.contains(command) == true }
        if menuItem.action == #selector(goBack) { return webView?.canGoBack == true }
        if menuItem.action == #selector(goForward) { return webView?.canGoForward == true }
        if menuItem.action == #selector(duplicateWindow) || menuItem.action == #selector(zoomPage(_:)) {
            return current?.loaded == true
        }
        return true
    }

    @objc private func showWindow() {
        window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }
    @objc private func showWorkspace() {
        showWindow()
        if current?.commands.contains("workspace") == true { runCommand("workspace") }
        else if let url = serverURL, webView?.url?.path != "/" { webView.load(URLRequest(url: url)) }
    }
    @objc private func reload() { if serverURL != nil { webView.reload() } else { startBackend() } }
    @objc private func retryLoad() {
        if let url = serverURL {
            showStatus("Opening Ribbon Field…", failed: false, in: current)
            webView.load(URLRequest(url: url))
        } else { startBackend() }
    }
    // App links also create history outside a web-page gesture. WebKit's native
    // traversal can skip those entries, losing the view/pane return destination.
    @objc private func goBack() { if webView.canGoBack { webView.evaluateJavaScript("history.back()") } }
    @objc private func goForward() { if webView.canGoForward { webView.evaluateJavaScript("history.forward()") } }
    @objc private func openBrowser() { if let url = webView.url, isLocal(url) { NSWorkspace.shared.open(url) } }
    @objc private func openLog() { if let url = backend?.logURL { NSWorkspace.shared.open(url) } }
    @objc private func toggleFiles() {
        guard let item = current, let pane = item.filesItem else { return }
        pane.isCollapsed.toggle()
        preferences.set(!pane.isCollapsed, forKey: "fileBrowserVisible")
        if !pane.isCollapsed { item.files.refreshFiles() }
    }

    private func showStatus(_ message: String, failed: Bool, in target: DesktopWindow? = nil) {
        for item in target.map({ [$0] }) ?? windows {
            item.label.stringValue = message
            item.status.isHidden = false
            item.webView.isHidden = true
            item.retry.isHidden = !failed
            item.spinner.isHidden = failed
            item.loaded = false
            item.commands.removeAll()
            if failed { item.spinner.stopAnimation(nil) } else { item.spinner.startAnimation(nil) }
        }
    }

    @objc private func startBackend() {
        guard backend.process == nil else { return }
        showStatus("Starting Ribbon Field…", failed: false)
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

    func application(_ application: NSApplication, open urls: [URL]) {
        for url in urls { openAppLink(url) }
    }

    private func openAppLink(_ url: URL, preferred: DesktopWindow? = nil) {
        guard url.scheme == "agentcoord", ["overview", "view", "thread"].contains(url.host ?? ""),
              url.user == nil, url.password == nil, url.port == nil,
              url.absoluteString.count <= 8192 else { return }
        guard !windows.isEmpty else { pendingAppLinks.append(url); return }
        let targetID = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems?.first { $0.name == "window" }?.value
        var target = preferred ?? windows.first { $0.id == targetID?.lowercased() } ?? current!
        if target.webView.url?.path == "/monitor" { target = makeWindow() }
        target.pendingLinks.append(url)
        target.window.deminiaturize(nil)
        target.window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        deliverLinks(to: target)
    }

    private func deliverLinks(to item: DesktopWindow) {
        guard item.navigationReady, !item.navigating, !item.pendingLinks.isEmpty else { return }
        let url = item.pendingLinks.removeFirst()
        item.navigating = true
        // The shared router validates the destination against this backend and
        // navigates without reloading, preserving drafts and Back history.
        item.webView.callAsyncJavaScript("return await window.agentCoordNavigate(url)",
            arguments: ["url": url.absoluteString], in: nil, in: .page) { [weak self, weak item] _ in
            guard let self = self, let item = item else { return }
            item.navigating = false
            self.deliverLinks(to: item)
        }
    }

    private func configureScripts(_ webView: WKWebView) {
        guard let source = try? String(contentsOf: resources.appendingPathComponent("bridge.js"), encoding: .utf8),
              let palette = try? String(contentsOf: resources.appendingPathComponent("command-palette.js"), encoding: .utf8),
              let data = try? JSONSerialization.data(withJSONObject: preferences.dictionary(forKey: "webPreferences") ?? [:]),
              let json = String(data: data, encoding: .utf8) else { return }
        let controller = webView.configuration.userContentController
        controller.removeAllUserScripts()
        controller.addUserScript(WKUserScript(source: palette, injectionTime: .atDocumentStart, forMainFrameOnly: true))
        controller.addUserScript(WKUserScript(source: source.replacingOccurrences(of: "__AGENT_COORD_PREFERENCES__", with: json)
            .replacingOccurrences(of: "__AGENT_COORD_WINDOW_ID__", with: item(for: webView)?.id ?? ""),
            injectionTime: .atDocumentStart, forMainFrameOnly: true))
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url else { decisionHandler(.cancel); return }
        if isLocal(url) {
            configureScripts(webView)
            decisionHandler(.allow)
        } else {
            decisionHandler(.cancel)
            if navigationAction.navigationType == .linkActivated, url.scheme == "agentcoord" {
                openAppLink(url, preferred: item(for: webView))
            } else if navigationAction.navigationType == .linkActivated, ["https", "http", "mailto"].contains(url.scheme ?? "") {
                NSWorkspace.shared.open(url)
            }
        }
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = navigationAction.request.url {
            if isLocal(url) { makeWindow(url: url) }
            else if url.scheme == "agentcoord" { openAppLink(url, preferred: item(for: webView)) }
            else if ["https", "http", "mailto"].contains(url.scheme ?? "") { NSWorkspace.shared.open(url) }
        }
        return nil
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        guard let item = item(for: webView) else { return }
        item.status.isHidden = true
        item.spinner.stopAnimation(nil)
        item.loaded = true
        webView.isHidden = false
        if smokeReport != nil { checkSmoke(attempt: 0) }
    }
    func webView(_ webView: WKWebView, didStartProvisionalNavigation navigation: WKNavigation!) {
        item(for: webView)?.navigationReady = false
    }
    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        if (error as NSError).code != NSURLErrorCancelled { showStatus(error.localizedDescription, failed: true, in: item(for: webView)) }
    }
    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        if (error as NSError).code != NSURLErrorCancelled { showStatus(error.localizedDescription, failed: true, in: item(for: webView)) }
    }
    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) { webView.reload() }

    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        guard let window = item(for: webView)?.window else { completionHandler(nil); return }
        panel.beginSheetModal(for: window) { response in completionHandler(response == .OK ? panel.urls : nil) }
    }

    private func permission(_ completion: @escaping (String) -> Void) {
        UNUserNotificationCenter.current().getNotificationSettings { settings in
            let value = settings.authorizationStatus == .notDetermined ? "default" :
                settings.authorizationStatus == .denied ? "denied" : "granted"
            DispatchQueue.main.async { completion(value) }
        }
    }

    private func fileReferences(_ urls: [URL], names: [String]? = nil) throws -> [[String: Any]] {
        let selected = urls.filter { $0.isFileURL && (names == nil || names!.contains($0.lastPathComponent)) }
        guard !selected.isEmpty, selected.count <= 32,
              names == nil || Set(selected.map { $0.lastPathComponent }) == Set(names!) else {
            throw NSError(domain: "AgentCoord", code: 1, userInfo: [NSLocalizedDescriptionKey:
                "Drop up to 32 local files or folders from Finder, or use File → Insert File or Folder Paths."])
        }
        return try selected.map { url in
            var directory: ObjCBool = false
            guard FileManager.default.fileExists(atPath: url.path, isDirectory: &directory) else {
                throw NSError(domain: "AgentCoord", code: 2, userInfo: [NSLocalizedDescriptionKey:
                    "The file is no longer available: \(url.lastPathComponent)"])
            }
            return ["path": url.path, "name": url.lastPathComponent, "directory": directory.boolValue]
        }
    }

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage,
                               replyHandler: @escaping (Any?, String?) -> Void) {
        guard message.frameInfo.isMainFrame, let url = message.frameInfo.request.url, isLocal(url),
              let source = message.webView, let owner = item(for: source),
              let body = message.body as? [String: Any], let action = body["action"] as? String else {
            replyHandler(nil, "Only the local Ribbon Field window can use desktop features."); return
        }
        let center = UNUserNotificationCenter.current()
        switch action {
        case "openFile":
            guard let path = body["path"] as? String, let workspace = body["workspace"] as? String else {
                replyHandler(nil, "Choose a file in the conversation’s working folder."); return
            }
            do {
                let file = try WorkspaceFileLink.resolve(path: path, workspace: workspace)
                owner.files.openFile(file) { error in
                    if let error = error { replyHandler(nil, error) }
                    else { replyHandler(true, nil) }
                }
            } catch { replyHandler(nil, error.localizedDescription) }
        case "navigationReady":
            owner.navigationReady = true
            deliverLinks(to: owner)
            replyHandler(true, nil)
        case "windowState":
            if let title = body["title"] as? String {
                owner.window.title = String(title.prefix(160)) + " — Ribbon Field"
            }
            owner.commands = Set((body["commands"] as? [String] ?? []).prefix(32))
            owner.fileTarget = body["fileTarget"] as? String ?? ""
            owner.files.canInsert = owner.commands.contains("insertFiles")
            owner.files.setConversationRoot(body["workspace"] as? String ?? "")
            replyHandler(true, nil)
        case "toggleFiles":
            if let pane = owner.filesItem {
                pane.isCollapsed.toggle()
                preferences.set(!pane.isCollapsed, forKey: "fileBrowserVisible")
                if !pane.isCollapsed { owner.files.refreshFiles() }
            }
            replyHandler(true, nil)
        case "newWindow":
            guard let value = body["url"] as? String, value.count <= 8192,
                  let destination = URL(string: value, relativeTo: serverURL)?.absoluteURL, isLocal(destination) else {
                replyHandler(nil, "Only Ribbon Field pages can open in an app window."); return
            }
            makeWindow(url: destination)
            replyHandler(true, nil)
        case "dropFiles":
            guard let names = body["names"] as? [String], !names.isEmpty, names.count <= 32 else {
                replyHandler(nil, "Drop up to 32 files or folders at a time."); return
            }
            let pasteboard = smokePasteboard ?? NSPasteboard(name: .drag)
            let urls = pasteboard.readObjects(forClasses: [NSURL.self], options: [.urlReadingFileURLsOnly: true]) as? [URL] ?? []
            do { replyHandler(try fileReferences(urls, names: names), nil) }
            catch { replyHandler(nil, error.localizedDescription) }
        case "chooseFiles":
            guard owner.window.attachedSheet == nil else { replyHandler(nil, "Finish the open file panel first."); return }
            let workspace = body["workspace"] as? Bool == true
            let panel = NSOpenPanel()
            panel.canChooseFiles = !workspace
            panel.canChooseDirectories = true
            panel.allowsMultipleSelection = !workspace
            panel.prompt = workspace ? "Choose Workspace" : "Insert Paths"
            panel.beginSheetModal(for: owner.window) { response in
                guard response == .OK else { replyHandler([], nil); return }
                do { replyHandler(try self.fileReferences(panel.urls), nil) }
                catch { replyHandler(nil, error.localizedDescription) }
            }
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
            content.userInfo = ["desktopWindow": owner.id]
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
            let target = self.windows.first { $0.id == response.notification.request.content.userInfo["desktopWindow"] as? String } ?? self.current
            target?.window.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            let id = response.notification.request.identifier
            if let data = try? JSONSerialization.data(withJSONObject: [id]), let json = String(data: data, encoding: .utf8) {
                target?.webView.evaluateJavaScript("window.__agentCoordNotificationClick?.(\(json)[0])")
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
                alert.messageText = count == nil ? "Quit Ribbon Field?" : "Stop running work and quit?"
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
        if let monitor = sessionKeyMonitor { NSEvent.removeMonitor(monitor) }
        if let activity = activity { ProcessInfo.processInfo.endActivity(activity) }
        if lockFile >= 0 { flock(lockFile, LOCK_UN); close(lockFile) }
        if let name = smokePreferences { preferences.removePersistentDomain(forName: name) }
        smokePasteboard?.releaseGlobally()
        if let directory = smokeDirectory { try? FileManager.default.removeItem(at: directory) }
    }

    // Native smoke mode uses a disposable database and preference domain. It
    // exercises the actual WKWebView and window lifecycle without model turns.
    private func checkSmoke(attempt: Int) {
        guard !quitting, smokeStage == 0, let first = windows.first, first.loaded else { return }
        smokeStage = 1
        Task { @MainActor in
            do { try await self.runSmoke(first) }
            catch { self.finishSmoke(error: String(describing: error)) }
        }
    }

    private func smokeCheck(_ key: String, _ passed: Bool) throws {
        smokeResults[key] = passed
        if !passed { throw NSError(domain: "AgentCoordSmoke", code: 1,
            userInfo: [NSLocalizedDescriptionKey: "Native behavior check failed: " + key]) }
    }

    @MainActor private func smokeJS(_ source: String, in view: WKWebView) async throws -> Any {
        try await withCheckedThrowingContinuation { continuation in
            view.callAsyncJavaScript(source, arguments: [:], in: nil, in: .page) { result in
                continuation.resume(with: result)
            }
        }
    }

    @MainActor private func smokeReady(_ item: DesktopWindow) async throws {
        for _ in 0..<120 {
            if item.loaded { break }
            try await Task.sleep(nanoseconds: 50_000_000)
        }
        try smokeCheck("ui_loaded", item.loaded)
        let ready = try await smokeJS("""
            for (let i = 0; i < 100; i++) {
              if (document.querySelector('#desktop-new-window') &&
                  typeof window.agentCoordNavigate === 'function' &&
                  document.querySelector('#connection')?.dataset.state === 'connected') return true;
              await new Promise(resolve => setTimeout(resolve, 50));
            }
            return false;
            """, in: item.webView)
        try smokeCheck("ui_loaded", ready as? Bool == true)
    }

    @MainActor private func smokeFocus(_ item: DesktopWindow) async throws {
        item.window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        for _ in 0..<80 {
            if NSApp.isActive && NSApp.keyWindow === item.window { return }
            try await Task.sleep(nanoseconds: 25_000_000)
        }
        smokeResults["focus_debug"] = ["active": NSApp.isActive, "target": item.window.windowNumber,
            "key": NSApp.keyWindow?.windowNumber ?? -1, "visible": item.window.isVisible,
            "sheet": item.window.attachedSheet != nil,
            "frontmost": NSWorkspace.shared.frontmostApplication?.bundleIdentifier ?? "none"]
        try smokeCheck("window_focus_ready", false)
    }

    private func smokeKey(_ key: String, flags: NSEvent.ModifierFlags, code: UInt16) -> Bool {
        let characters = flags.contains(.shift) ? key.uppercased() : key
        guard let event = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: flags,
            timestamp: ProcessInfo.processInfo.systemUptime, windowNumber: window.windowNumber, context: nil,
            characters: characters, charactersIgnoringModifiers: characters, isARepeat: false, keyCode: code) else { return false }
        NSApp.mainMenu?.update()
        let handled = NSApp.mainMenu?.performKeyEquivalent(with: event) == true
        if !handled {
            smokeResults["shortcut_debug"] = ["main_thread": Thread.isMainThread, "active": NSApp.isActive,
                "key": characters, "flags": flags.rawValue, "window_count": windows.count,
                "items": NSApp.mainMenu?.items.flatMap { $0.submenu?.items ?? [] }.filter { !$0.keyEquivalent.isEmpty }.map {
                    ["title": $0.title, "key": $0.keyEquivalent, "flags": $0.keyEquivalentModifierMask.rawValue,
                     "enabled": $0.isEnabled, "target": String(describing: $0.target)] as [String: Any]
                } ?? []] as [String: Any]
        }
        return handled
    }

    private func smokeWebKey(_ key: String, flags: NSEvent.ModifierFlags, code: UInt16,
                             throughApplication: Bool = false) -> Bool {
        for type in [NSEvent.EventType.keyDown, .keyUp] {
            guard let event = NSEvent.keyEvent(with: type, location: .zero, modifierFlags: flags,
                timestamp: ProcessInfo.processInfo.systemUptime, windowNumber: window.windowNumber, context: nil,
                characters: key, charactersIgnoringModifiers: key, isARepeat: false, keyCode: code) else { return false }
            if throughApplication { NSApp.postEvent(event, atStart: false) }
            else { window.sendEvent(event) }
        }
        return true;
    }

    @MainActor private func runSmoke(_ first: DesktopWindow) async throws {
        try await smokeReady(first)
        smokeResults["backend_pid"] = backend.process?.processIdentifier
        smokeResults["url"] = serverURL?.absoluteString
        if CommandLine.arguments.contains("--smoke-sessions-only") {
            try await smokeFocus(first)
            try await smokeSessionCycling(first)
            finishSmoke(error: nil)
            return
        }
        let bridge = try await smokeJS("""
            localStorage.setItem('agent-coord.smoke', 'persisted');
            sessionStorage.setItem('desktop-smoke-window', 'first');
            document.querySelector('#message').value = 'First window draft';
            document.querySelector('#home').focus();
            return Notification.agentCoordNative === true;
            """, in: first.webView)
        try smokeCheck("notification_bridge", bridge as? Bool == true)
        try smokeCheck("new_window_shortcut", smokeKey("n", flags: [.command, .shift], code: 45))
        smokeResults["window_count_after_shortcut"] = windows.count
        guard let second = windows.last, second !== first else {
            try smokeCheck("multiple_windows", false); return
        }
        try await smokeReady(second)
        try smokeCheck("multiple_windows", windows.count == 2 && first.webView !== second.webView)
        let independent = try await smokeJS("""
            const independent = sessionStorage.getItem('desktop-smoke-window') === null;
            sessionStorage.setItem('desktop-smoke-window', 'second');
            return independent && localStorage.getItem('agent-coord.smoke') === 'persisted';
            """, in: second.webView)
        try smokeCheck("window_storage_isolated", independent as? Bool == true)
        _ = try await smokeJS("localStorage.setItem('agent-coord.smoke-shared', 'live'); return true;", in: first.webView)
        let shared = try await smokeJS("""
            for (let i = 0; i < 80; i++) {
              if (localStorage.getItem('agent-coord.smoke-shared') === 'live') return true;
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            return false;
            """, in: second.webView)
        try smokeCheck("live_preferences_shared", shared as? Bool == true)
        try smokeCheck("find_thread_shortcut", smokeKey("f", flags: [.command], code: 3))
        let focused = try await smokeJS("""
            for (let i = 0; i < 40; i++) {
              if (document.activeElement?.id === 'search') return true;
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            return false;
            """, in: second.webView)
        let firstUntouched = try await smokeJS("return document.activeElement?.id !== 'search';", in: first.webView)
        try smokeCheck("commands_target_key_window", focused as? Bool == true && firstUntouched as? Bool == true)

        // A private pasteboard exercises the same native resolver without
        // changing the user's clipboard or live drag pasteboard.
        let file = smokeDirectory!.appendingPathComponent("notes with spaces.md")
        let folder = smokeDirectory!.appendingPathComponent("Project folder", isDirectory: true)
        try Data("Native drop smoke".utf8).write(to: file)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        smokePasteboard = NSPasteboard(name: NSPasteboard.Name("com.agentcoord.smoke." + UUID().uuidString))
        smokePasteboard!.clearContents()
        smokePasteboard!.writeObjects([file as NSURL])
        let dropped = try await smokeJS("""
            history.replaceState(null, '', '#desktop-smoke');
            const message = document.querySelector('#message');
            document.querySelector('#conversation').hidden = false;
            document.querySelector('#composer').hidden = false;
            message.value = 'Second window draft'; message.setSelectionRange(message.value.length, message.value.length);
            window.smokeAttachments = new ChatImageAttachments({document, getThread: () => 'smoke', canAttach: () => true,
              onChange() {}, onError(error) { throw error; }});
            window.smokeAttachments.bind();
            const transfer = new DataTransfer();
            transfer.items.add(new File(['Native drop smoke'], 'notes with spaces.md', {type: 'text/markdown'}));
            transfer.items.add(new File([new Uint8Array([97])], 'preview.png', {type: 'image/png'}));
            message.dispatchEvent(new DragEvent('drop', {bubbles: true, cancelable: true, dataTransfer: transfer}));
            for (let i = 0; i < 80; i++) {
              if (message.value.includes('notes with spaces.md') && window.smokeAttachments.snapshot().length === 1)
                return message.value.startsWith('Second window draft\\n') && document.querySelector('#thread-count').textContent === '0';
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            throw new Error(document.querySelector('#error span').textContent || 'Native file drop did not reach the draft');
            """, in: second.webView)
        try smokeCheck("mixed_file_drop", dropped as? Bool == true)
        let firstDraft = try await smokeJS("return document.querySelector('#message').value === 'First window draft' && location.hash === '';", in: first.webView)
        try smokeCheck("window_drafts_and_navigation_isolated", firstDraft as? Bool == true)

        smokePasteboard!.clearContents()
        smokePasteboard!.writeObjects([folder as NSURL])
        let workspaceDrop = try await smokeJS("""
            const message = document.querySelector('#message');
            // The image attachment callback re-renders the empty smoke state.
            // Restore the editable destination for this independent drop check.
            document.querySelector('#conversation').hidden = false;
            document.querySelector('#composer').hidden = false;
            message.value = '/cd '; message.setSelectionRange(4, 4);
            const transfer = new DataTransfer();
            transfer.items.add(new File([], 'Project folder'));
            message.dispatchEvent(new DragEvent('drop', {bubbles: true, cancelable: true, dataTransfer: transfer}));
            for (let i = 0; i < 80; i++) {
              if (message.value.startsWith('/cd "') && message.value.includes('/Project folder')) return true;
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            throw new Error(document.querySelector('#error span').textContent || 'Folder path did not reach /cd: ' + message.value);
            """, in: second.webView)
        try smokeCheck("workspace_folder_drop", workspaceDrop as? Bool == true)
        try await smokeFiles(second, other: first)
        if CommandLine.arguments.contains("--smoke-files-only") { finishSmoke(error: nil); return }

        _ = try await smokeJS("""
            const message = document.querySelector('#message');
            message.focus(); message.setSelectionRange(2, 6);
            window.smokePaletteDraft = message.value;
            return true;
            """, in: second.webView)
        try smokeCheck("command_palette_shortcut", smokeKey("k", flags: [.command], code: 40))
        let paletteOpen = try await smokeJS("""
            for (let i = 0; i < 80; i++) {
              if (document.querySelector('#desktop-command-dialog').open &&
                  document.activeElement?.id === 'desktop-command-input' &&
                  document.querySelector('#desktop-command-status').textContent !== 'Loading threads…')
                return document.querySelectorAll('#desktop-command-results [role=option]').length > 0;
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            return false;
            """, in: second.webView)
        let firstPaletteClosed = try await smokeJS("return !document.querySelector('#desktop-command-dialog').open;", in: first.webView)
        try smokeCheck("command_palette_active_window", paletteOpen as? Bool == true && firstPaletteClosed as? Bool == true)
        let paletteImage: NSImage = try await withCheckedThrowingContinuation { continuation in
            second.webView.takeSnapshot(with: nil) { image, error in
                if let image = image { continuation.resume(returning: image) }
                else { continuation.resume(throwing: error ?? NSError(domain: "AgentCoordSmoke", code: 2)) }
            }
        }
        if let data = paletteImage.tiffRepresentation, let bitmap = NSBitmapImageRep(data: data),
           let png = bitmap.representation(using: .png, properties: [:]) {
            try png.write(to: smokeReport!.appendingPathExtension("palette.png"))
        }
        let paletteAction = try await smokeJS("""
            const input = document.querySelector('#desktop-command-input');
            input.value = 'find thread'; input.dispatchEvent(new Event('input', {bubbles: true}));
            if (document.querySelectorAll('#desktop-command-results [role=option]').length !== 1) return false;
            input.dispatchEvent(new KeyboardEvent('keydown', {key: 'ArrowDown', bubbles: true, cancelable: true}));
            input.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true, cancelable: true}));
            for (let i = 0; i < 40; i++) {
              if (!document.querySelector('#desktop-command-dialog').open && document.activeElement?.id === 'search')
                return document.querySelector('#message').value === window.smokePaletteDraft;
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            return false;
            """, in: second.webView)
        try smokeCheck("command_palette_action", paletteAction as? Bool == true)
        try smokeCheck("command_palette_shortcut", smokeKey("k", flags: [.command], code: 40))
        _ = try await smokeJS("""
            for (let i = 0; i < 40 && !document.querySelector('#desktop-command-dialog').open; i++)
              await new Promise(resolve => setTimeout(resolve, 25));
            return true;
            """, in: second.webView)
        try smokeCheck("command_palette_shortcut", smokeKey("k", flags: [.command], code: 40))
        let paletteToggle = try await smokeJS("""
            for (let i = 0; i < 40; i++) {
              if (!document.querySelector('#desktop-command-dialog').open) return document.activeElement?.id === 'search';
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            return false;
            """, in: second.webView)
        try smokeCheck("command_palette_toggle", paletteToggle as? Bool == true)
        let paletteEscape = try await smokeJS("""
            const search = document.querySelector('#search');
            search.value = 'Saved search'; search.focus(); search.setSelectionRange(2, 6);
            if (!window.agentCoordDesktop.command('commandPalette')) return false;
            document.querySelector('#desktop-command-input').dispatchEvent(new KeyboardEvent('keydown',
              {key: 'Escape', bubbles: true, cancelable: true}));
            const restored = !document.querySelector('#desktop-command-dialog').open && document.activeElement === search &&
              search.value === 'Saved search' && search.selectionStart === 2 && search.selectionEnd === 6;
            search.value = '';
            document.querySelector('#permissions-dialog').showModal();
            const blocked = window.agentCoordDesktop.command('commandPalette') === false;
            document.querySelector('#permissions-dialog').close();
            return restored && blocked;
            """, in: second.webView)
        try smokeCheck("command_palette_focus_and_modal", paletteEscape as? Bool == true)
        try smokeCheck("roll_up_shortcut", smokeKey("r", flags: [.command, .option], code: 15))
        let rollUpStarted = try await smokeJS("""
            for (let i = 0; i < 80; i++) {
              if (!document.querySelector('#roll-up-bar').hidden && !state.rollUp.busy)
                return state.rollUp.active || state.rollUp.done;
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            return false;
            """, in: second.webView)
        let firstRollUpQuiet = try await smokeJS("return !state.rollUp.active && !state.rollUp.done;", in: first.webView)
        try smokeCheck("roll_up_active_window", rollUpStarted as? Bool == true && firstRollUpQuiet as? Bool == true)
        _ = try await smokeJS("document.querySelector('#roll-up-exit').click(); return true;", in: second.webView)
        let rejected = try await smokeJS("""
            try { await webkit.messageHandlers.desktop.postMessage({action: 'newWindow', url: 'https://example.com/'}); }
            catch { return true; }
            return false;
            """, in: second.webView)
        try smokeCheck("external_window_rejected", rejected as? Bool == true)

        _ = try await smokeJS("""
            window.smokeNewSessionCount = 0;
            window.smokeSavedNewSession = newSession;
            newSession = async () => { window.smokeNewSessionCount++; };
            document.querySelector('#message').focus();
            return true;
            """, in: second.webView)
        try smokeCheck("new_session_shortcut", smokeKey("n", flags: [.command], code: 45))
        let sameNewSessionAction = try await smokeJS("""
            for (let i = 0; i < 40 && window.smokeNewSessionCount === 0; i++)
              await new Promise(resolve => setTimeout(resolve, 25));
            const shortcutWorked = window.smokeNewSessionCount === 1;
            document.querySelector('#new-session').click();
            const sameAction = window.smokeNewSessionCount === 2;
            newSession = window.smokeSavedNewSession;
            return shortcutWorked && sameAction && !document.querySelector('dialog[open]');
            """, in: second.webView)
        try smokeCheck("new_session_button_and_shortcut_match", sameNewSessionAction as? Bool == true)
        try await smokeRollUp(first)
        try await smokeNavigation(first, second)

        second.window.close()
        try smokeCheck("secondary_window_closed", windows.count == 1 && second.window.contentView == nil)
        try smokeCheck("one_shared_backend", backend.process?.isRunning == true &&
            backend.process?.processIdentifier == smokeResults["backend_pid"] as? Int32)
        first.window.close()
        try smokeCheck("window_closed", !first.window.isVisible)
        try smokeCheck("backend_survived_close", backend.process?.isRunning == true)
        showWindow()
        try smokeCheck("window_reopened", first.window.isVisible)
        first.loaded = false
        first.webView.reload()
        try await smokeReady(first)
        let persisted = try await smokeJS("return localStorage.getItem('agent-coord.smoke') === 'persisted';", in: first.webView)
        try smokeCheck("preferences_persisted", persisted as? Bool == true &&
            preferences.dictionary(forKey: "webPreferences")?["agent-coord.smoke"] as? String == "persisted")
        smokeStage = 2
        first.webView.takeSnapshot(with: nil) { image, error in
            if let data = image?.tiffRepresentation, let bitmap = NSBitmapImageRep(data: data),
               let png = bitmap.representation(using: .png, properties: [:]) {
                try? png.write(to: self.smokeReport!.appendingPathExtension("png"))
            }
            self.finishSmoke(error: error?.localizedDescription)
        }
    }

    @MainActor private func smokeFiles(_ item: DesktopWindow, other: DesktopWindow) async throws {
        try smokeCheck("file_browser_default_collapsed",
            preferences.object(forKey: "fileBrowserVisible") == nil &&
            item.filesItem?.isCollapsed == true && other.filesItem?.isCollapsed == true)
        item.split.view.layoutSubtreeIfNeeded()
        try smokeCheck("file_browser_collapsed_layout",
            abs(item.webView.bounds.width - item.split.view.bounds.width) < 2)
        try smokeCheck("file_browser_open_shortcut", smokeKey("b", flags: [.command, .option], code: 11))
        try smokeCheck("file_browser_opened",
            item.filesItem?.isCollapsed == false && other.filesItem?.isCollapsed == true)
        let visibleWindow = makeWindow()
        try smokeCheck("file_browser_visible_preference_restored", visibleWindow.filesItem?.isCollapsed == false)
        visibleWindow.window.close()
        item.window.makeKeyAndOrderFront(nil)
        let base = smokeDirectory!.resolvingSymlinksInPath().appendingPathComponent("Browser fixture", isDirectory: true)
        try FileManager.default.createDirectory(at: base.appendingPathComponent("Sources"), withIntermediateDirectories: true)
        let file = base.appendingPathComponent("README.md")
        try "# Native files\nReady for local and remote workspaces.\n".write(to: file, atomically: true, encoding: .utf8)
        try "print(\"hello\")\n".write(to: base.appendingPathComponent("Sources/main.swift"), atomically: true, encoding: .utf8)
        _ = try smokePython("""
            import json, sys
            from agent_coord.store import CoordinationStore
            store = CoordinationStore(sys.argv[1])
            store.register(session_id='files-smoke', client='codex', cwd=sys.argv[2])
            print(json.dumps({'ready': True}))
            """, arguments: [base.path])
        _ = try await smokeJS("""
            // Keep the UI-only draft editable through metadata refreshes without
            // creating a provider session or starting a model turn.
            window.smokeFileRenderStatus = renderStatus;
            renderStatus = function () {
              if (state.selected === 'files-smoke' && state.detail?.work_thread)
                state.detail.work_thread.browser_session = true;
              return window.smokeFileRenderStatus();
            };
            await refreshList(); await select('files-smoke');
            // Make a draft destination without starting a model or importing a terminal.
            document.querySelector('#composer').hidden = false;
            document.querySelector('#message').value = 'Review this';
            document.querySelector('#message').setSelectionRange(11, 11);
            return true;
            """, in: item.webView)
        for _ in 0..<100 {
            if item.files.outline.numberOfRows == 2 { break }
            try await Task.sleep(nanoseconds: 25_000_000)
        }
        smokeResults["file_browser_debug"] = ["root": item.files.root, "expected": base.path,
            "rows": item.files.outline.numberOfRows]
        try smokeCheck("file_browser_follows_conversation",
            URL(fileURLWithPath: item.files.root).resolvingSymlinksInPath().path == base.resolvingSymlinksInPath().path && item.files.outline.numberOfRows == 2)
        try smokeCheck("file_browser_window_isolation", other.files.root != base.path)
        let tree = item.files.outline
        guard let folder = tree.item(atRow: 0) as? FileBrowserNode else { try smokeCheck("file_browser_folder", false); return }
        tree.expandItem(folder)
        for _ in 0..<100 {
            if tree.numberOfRows == 3 { break }
            try await Task.sleep(nanoseconds: 25_000_000)
        }
        try smokeCheck("file_browser_lazy_expansion", tree.numberOfRows == 3 && folder.children.first?.file.path == "Sources/main.swift")
        tree.selectRowIndexes(IndexSet(integer: 2), byExtendingSelection: false)
        for _ in 0..<100 {
            if item.files.previewText.string.hasPrefix("# Native files") { break }
            try await Task.sleep(nanoseconds: 25_000_000)
        }
        try smokeCheck("file_browser_preview", item.files.previewText.string.hasPrefix("# Native files"))
        let windowCount = windows.count
        let linkHandled = try await smokeJS("""
            const message = document.createElement('div');
            message.className = 'markdown';
            message.innerHTML = messageMarkdown.render('[Missing file](./missing-link-test.txt:12)');
            document.querySelector('#timeline').append(message);
            document.querySelector('#error').hidden = true;
            message.querySelector('a').click();
            try {
              for (let i = 0; i < 60; i++) {
                if (!document.querySelector('#error').hidden)
                  return document.querySelector('#error span').textContent.includes('missing-link-test.txt');
                await new Promise(resolve => setTimeout(resolve, 25));
              }
              return false;
            } finally { document.querySelector('#error').hidden = true; }
            """, in: item.webView)
        try smokeCheck("file_browser_chat_link_native_error", linkHandled as? Bool == true && windows.count == windowCount)
        item.files.insertSelected()
        let inserted = try await smokeJS("""
            for (let i = 0; i < 60; i++) {
              if (document.querySelector('#message').value.includes('README.md'))
                return document.querySelector('#message').value.startsWith('Review this') && state.detail.thread.turns.length === 0;
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            return false;
            """, in: item.webView)
        if inserted as? Bool != true {
            smokeResults["file_browser_insert_debug"] = try await smokeJS("""
                return {value: document.querySelector('#message').value,
                        error: document.querySelector('#error span').textContent,
                        composerHidden: document.querySelector('#composer').hidden,
                        selected: state.selected};
                """, in: item.webView)
        }
        try smokeCheck("file_browser_inserts_without_sending", inserted as? Bool == true)
        try "Changed by an agent\n".write(to: file, atomically: true, encoding: .utf8)
        item.files.refreshFiles()
        for _ in 0..<100 {
            if item.files.previewText.string == "Changed by an agent\n" { break }
            try await Task.sleep(nanoseconds: 25_000_000)
        }
        try smokeCheck("file_browser_refresh", item.files.previewText.string == "Changed by an agent\n")
        // Exercise the native chooser after web editing checks; the file panel
        // changes native focus independently of the web-view test fixture.
        item.files.outline.deselectAll(nil)
        func editorPicker(in view: NSView) -> NSPopUpButton? {
            if let picker = view as? NSPopUpButton { return picker }
            return view.subviews.lazy.compactMap { editorPicker(in: $0) }.first
        }
        guard let picker = editorPicker(in: item.files.view) else {
            try smokeCheck("file_browser_editor_menu_enabled", false); return
        }
        picker.menu?.update()
        try smokeCheck("file_browser_editor_menu_enabled",
            item.files.outline.selectedRow == -1 && picker.itemArray.filter { !$0.isSeparatorItem }.allSatisfy { $0.isEnabled })
        let responderBeforePicker = item.window.firstResponder
        picker.selectItem(withTitle: "Choose Application…")
        picker.sendAction(picker.action, to: picker.target)
        for _ in 0..<60 {
            if item.window.attachedSheet is NSOpenPanel { break }
            try await Task.sleep(nanoseconds: 25_000_000)
        }
        let editorPanel = item.window.attachedSheet as? NSOpenPanel
        try smokeCheck("file_browser_editor_picker_opens",
            editorPanel?.title == "Choose File Editor" && editorPanel?.canChooseFiles == true)
        editorPanel?.cancel(nil)
        for _ in 0..<60 {
            if item.window.attachedSheet == nil { break }
            try await Task.sleep(nanoseconds: 25_000_000)
        }
        try smokeCheck("file_browser_editor_picker_cancels", item.window.attachedSheet == nil)
        item.window.makeFirstResponder(responderBeforePicker)
        if let content = item.window.contentView, let bitmap = content.bitmapImageRepForCachingDisplay(in: content.bounds) {
            content.cacheDisplay(in: content.bounds, to: bitmap)
            if let data = bitmap.representation(using: .png, properties: [:]) {
                try data.write(to: smokeReport!.appendingPathExtension("files.png"))
            }
        }
        try smokeCheck("file_browser_shortcut", smokeKey("b", flags: [.command, .option], code: 11))
        try smokeCheck("file_browser_collapsed", item.filesItem?.isCollapsed == true && other.filesItem?.isCollapsed == true)
        let hiddenWindow = makeWindow()
        try smokeCheck("file_browser_hidden_preference_restored", hiddenWindow.filesItem?.isCollapsed == true)
        hiddenWindow.window.close()
        item.window.makeKeyAndOrderFront(nil)
        item.split.view.layoutSubtreeIfNeeded()
        if let content = item.window.contentView, let bitmap = content.bitmapImageRepForCachingDisplay(in: content.bounds) {
            content.cacheDisplay(in: content.bounds, to: bitmap)
            if let data = bitmap.representation(using: .png, properties: [:]) {
                try data.write(to: smokeReport!.appendingPathExtension("files-collapsed.png"))
            }
        }
        _ = try await smokeJS("renderStatus = window.smokeFileRenderStatus; delete window.smokeFileRenderStatus; return true;", in: item.webView)
    }

    @MainActor private func smokeRollUp(_ window: DesktopWindow) async throws {
        _ = try smokePython("""
            import json, sys
            from agent_coord.store import CoordinationStore
            now = 10.0
            store = CoordinationStore(sys.argv[1], clock=lambda: now)
            store.threads.update('files-smoke', attention='archived')
            for name in ['roll-old-session', 'roll-new-session', 'roll-later']:
                store.register(session_id=name, client='codex', cwd=str(store.database_path.parent))
                now += 10
            for name, stamp in [('roll-old-session', 300), ('roll-new-session', 200), ('roll-later', 100)]:
                now = stamp
                store.threads.checkpoint(name, {'phase': 'finished', 'summary': 'A recommendation to review.', 'next_actor': 'nobody', 'next_action': ''})
            store.threads.update('roll-later', attention='later')
            print(json.dumps({'ready': True}))
            """)
        let oldest = try await smokeJS("""
            await state.rollUp.start();
            return state.selected === 'roll-new-session' && state.rollUp.queue.length === 2 &&
                !document.querySelector('#roll-up-bar').hidden;
            """, in: window.webView)
        try smokeCheck("roll_up_oldest_waiting", oldest as? Bool == true)
        let picture: NSImage = try await withCheckedThrowingContinuation { continuation in
            window.webView.takeSnapshot(with: nil) { image, error in
                if let image = image { continuation.resume(returning: image) }
                else { continuation.resume(throwing: error ?? NSError(domain: "AgentCoordSmoke", code: 2)) }
            }
        }
        if let data = picture.tiffRepresentation, let bitmap = NSBitmapImageRep(data: data),
           let png = bitmap.representation(using: .png, properties: [:]) {
            try png.write(to: smokeReport!.appendingPathExtension("roll-up.png"))
        }
        let behavior = try await smokeJS("""
            document.querySelector('#roll-up-handle').click();
            for (let i = 0; i < 80 && (state.selected !== 'roll-old-session' || state.rollUp.busy); i++)
              await new Promise(resolve => setTimeout(resolve, 25));
            if (state.selected !== 'roll-old-session') return false;
            const handled = await api('threads/roll-new-session');
            if (handled.needs_attention || handled.attention !== 'now') return false;
            document.querySelector('#roll-up-skip').click();
            for (let i = 0; i < 80 && state.rollUp.active; i++)
              await new Promise(resolve => setTimeout(resolve, 25));
            const skipped = await api('threads/roll-old-session');
            const passed = state.rollUp.done && skipped.needs_attention &&
              document.querySelector('#roll-up-label').textContent === 'Pass complete · 1 skipped';
            document.querySelector('#roll-up-exit').click();
            return passed && document.querySelector('#roll-up-bar').hidden;
            """, in: window.webView)
        try smokeCheck("roll_up_queue_flow", behavior as? Bool == true)
        _ = try await smokeJS("""
            for (const id of ['roll-old-session', 'roll-new-session', 'roll-later'])
              await api('threads/' + id, {attention: 'archived'});
            goHome(); await refreshList(); return true;
            """, in: window.webView)
    }

    private func smokePython(_ source: String, arguments: [String] = []) throws -> [String: Any] {
        let task = Process(), output = Pipe()
        task.executableURL = configuration.pythonURL(resources: resources)
        task.arguments = ["-c", source, smokeDirectory!.appendingPathComponent("state.sqlite3").path] + arguments
        task.environment = ["PATH": configuration.toolPath, "PYTHONPATH": resources.appendingPathComponent("backend").path,
                            "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1"]
        task.currentDirectoryURL = smokeDirectory
        task.standardOutput = output
        try task.run()
        let data = output.fileHandleForReading.readDataToEndOfFile()
        task.waitUntilExit()
        guard task.terminationStatus == 0,
              let result = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw NSError(domain: "AgentCoordSmoke", code: 3,
                userInfo: [NSLocalizedDescriptionKey: "Navigation fixture process failed"])
        }
        return result
    }

    @MainActor private func smokeNavigation(_ first: DesktopWindow, _ second: DesktopWindow) async throws {
        // The actual CLI runs outside the checkout against the isolated database.
        // Capture its launch URL instead of sending it to the user's installed app.
        let fixture = try smokePython("""
            import contextlib, io, json, subprocess, sys
            from unittest.mock import patch
            from agent_coord.store import CoordinationStore
            from agent_coord.navigation import NavigationStore
            from agent_coord.cli import main
            store = CoordinationStore(sys.argv[1])
            nav = NavigationStore(store)
            billing = store.threads.organization.create_project('Billing')
            other = store.threads.organization.create_project('Other')
            for name, project in [('navigation-manager', None), ('navigation-billing', billing['id']), ('navigation-other', other['id'])]:
                store.register(session_id=name, client='codex', cwd=str(store.database_path.parent))
                store.threads.update(name, title=name, project_id=project)
            view = nav.views.create({'name': 'Review', 'filters': {'project': other['id'], 'search': 'keep'}})
            nav.bind('navigation-manager', sys.argv[2])
            output = io.StringIO()
            with patch('agent_coord.navigation.subprocess.run', return_value=subprocess.CompletedProcess([], 0, '', '')) as opener, contextlib.redirect_stdout(output):
                assert main(['--db', sys.argv[1], 'ui', 'open', '--project', 'Billing', '--from-session', 'navigation-manager', '--wait', '0']) == 0
            print(json.dumps({'url': opener.call_args.args[0][-1], 'result': json.loads(output.getvalue()),
                              'billing': billing['id'], 'other': other['id'], 'view': view['id']}))
            """, arguments: [first.id])
        let data = try JSONSerialization.data(withJSONObject: fixture)
        let json = String(data: data, encoding: .utf8)!
        _ = try await smokeJS("""
            window.navigationFixture = \(json);
            await refreshList(); await select('navigation-manager');
            document.querySelector('#message').value = 'Keep my navigation draft';
            return true;
            """, in: first.webView)
        // The fixture assigns the draft directly. Also interact with its page:
        // native Back can skip script-created history entries without a gesture.
        try await smokeFocus(first)
        _ = smokeWebKey("\t", flags: [], code: 48)
        _ = try await smokeJS("""
            window.navigationFixture = \(json);
            savedViews.sync((await api('views')).data);
            await savedViews.activate(window.navigationFixture.view);
            return true;
            """, in: second.webView)
        try await smokeFocus(second)
        application(NSApp, open: [URL(string: fixture["url"] as! String)!])
        let displayed = try await smokeJS("""
            for (let i = 0; i < 100; i++) {
              if (state.selected === null && document.querySelector('#project').value === navigationFixture.billing &&
                  !document.querySelector('#welcome').hidden && !navigation.applying) {
                const cards = [...document.querySelectorAll('#overview [data-thread]')].map(el => el.dataset.thread);
                return cards.includes('navigation-billing') && !cards.includes('navigation-other');
              }
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            throw new Error(document.querySelector('#error span').textContent || 'App link did not display Billing');
            """, in: first.webView)
        try smokeCheck("app_link_project_filter", displayed as? Bool == true)
        let otherUntouched = try await smokeJS("return savedViews.activeId === navigationFixture.view && document.querySelector('#search').value === 'keep';", in: second.webView)
        for _ in 0..<40 {
            if current === first { break }
            try await Task.sleep(nanoseconds: 25_000_000)
        }
        smokeResults["navigation_source_debug"] = ["key_window": NSApp.keyWindow?.title ?? "none",
            "current_is_first": current === first, "other_untouched": otherUntouched as? Bool ?? false]
        try smokeCheck("app_link_source_window", current === first && otherUntouched as? Bool == true)
        let acknowledgement = try smokePython("""
            import json, sys
            from agent_coord.store import CoordinationStore
            from agent_coord.navigation import NavigationStore
            print(json.dumps(NavigationStore(CoordinationStore(sys.argv[1])).request(sys.argv[2])))
            """, arguments: [(fixture["result"] as! [String: Any])["request_id"] as! String])
        try smokeCheck("app_link_acknowledged", acknowledgement["status"] as? String == "displayed" && acknowledgement["window_id"] as? String == first.id)
        try smokeCheck("app_link_back_shortcut", smokeKey("[", flags: [.command], code: 33))
        // Let WebKit finish the history transition before starting an async
        // evaluation in the destination document.
        try await Task.sleep(nanoseconds: 200_000_000)
        let returned = try await smokeJS("""
            for (let i = 0; i < 100; i++) {
              if (state.selected === 'navigation-manager' && !navigation.applying &&
                  document.querySelector('#message').value === 'Keep my navigation draft') return true;
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            return false;
            """, in: first.webView)
        smokeResults["navigation_back_debug"] = try await smokeJS("""
            return {selected: state.selected, draft: document.querySelector('#message').value,
              stored: state.drafts.get('navigation-manager') || '', applying: navigation.applying,
              hash: location.hash, history: history.state, error: document.querySelector('#error span').textContent};
            """, in: first.webView)
        try smokeCheck("app_link_back_preserves_draft", returned as? Bool == true)
        let saved = try await smokeJS("""
            const anchor = document.createElement('a');
            anchor.href = 'agentcoord://view/' + navigationFixture.view;
            anchor.textContent = 'Review'; document.body.append(anchor); anchor.click(); anchor.remove();
            for (let i = 0; i < 100; i++) {
              if (savedViews.activeId === navigationFixture.view && !navigation.applying) {
                const view = (await api('views')).data.find(view => view.id === navigationFixture.view);
                return view.filters.project === navigationFixture.other && view.filters.search === 'keep' && view.version === 1;
              }
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            return false;
            """, in: first.webView)
        if saved as? Bool != true {
            smokeResults["navigation_saved_view_debug"] = try await smokeJS("""
                return {active: savedViews.activeId, expected: navigationFixture.view,
                  applying: navigation.applying, filters: savedViews.read(),
                  view: (await api('views')).data.find(view => view.id === navigationFixture.view),
                  error: document.querySelector('#error span').textContent};
                """, in: first.webView)
        }
        try smokeCheck("app_link_saved_view_unchanged", saved as? Bool == true)
        try await smokeFocus(second)
        application(NSApp, open: [URL(string: "agentcoord://overview?project=\(fixture["billing"] as! String)&window=\(UUID().uuidString)")!])
        let fallback = try await smokeJS("""
            for (let i = 0; i < 100; i++) {
              if (savedViews.activeId === 'all' && document.querySelector('#project').value === navigationFixture.billing && !navigation.applying) return true;
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            return false;
            """, in: second.webView)
        try smokeCheck("app_link_closed_window_fallback", fallback as? Bool == true && current === second)
        try await smokeFocus(first)
        _ = try await smokeJS("await agentCoordNavigate('agentcoord://overview'); return true;", in: first.webView)
        try await smokeFocus(first)
        try smokeCheck("tile_threads_shortcut", smokeKey("t", flags: [.command, .control, .option], code: 17))
        let paneReady = try await smokeJS("""
            for (let i = 0; i < 100; i++) {
              const shell = window.agentCoordPanes;
              if (shell.active && shell.records.get('navigation-manager')?.api) {
                await shell.openThread('navigation-manager');
                const doc = shell.focusedRecord.frame.contentDocument;
                doc.querySelector('#composer').hidden = false;
                const message = doc.querySelector('#message'); message.disabled = false;
                message.value = 'Keep my pane draft'; message.dispatchEvent(new Event('input', {bubbles: true})); message.focus();
                if (doc.activeElement === message) return true;
              }
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            return false;
            """, in: first.webView)
        if paneReady as? Bool != true {
            smokeResults["pane_native_debug"] = ["current_is_first": current === first,
                "key_window": NSApp.keyWindow?.windowNumber ?? -1,
                "first_window": first.window.windowNumber, "second_window": second.window.windowNumber]
            smokeResults["pane_composer_debug"] = try await smokeJS("""
                const shell = agentCoordPanes;
                return {active: shell.active, selected: shell.selected,
                  filters: savedViews.read(), busy: state.busy, dialog: document.querySelector('dialog[open]')?.id || '',
                  sessions: state.sessions.map(thread => ({id: thread.thread_id, attention: thread.attention})),
                  records: [...shell.records].map(([id, record]) => {
                    const doc = record.frame?.contentDocument, message = doc?.querySelector('#message');
                    return {id, ready: Boolean(record.api), hidden: record.frame?.hidden,
                      composerHidden: doc?.querySelector('#composer')?.hidden,
                      disabled: message?.disabled, activeElement: doc?.activeElement?.id || doc?.activeElement?.tagName,
                      error: doc?.querySelector('#error span')?.textContent || ''};
                  }), error: document.querySelector('#error span').textContent};
                """, in: first.webView)
            smokeResults["pane_second_window_debug"] = try await smokeJS("return {active: agentCoordPanes.active, selected: agentCoordPanes.selected};", in: second.webView)
        }
        try smokeCheck("pane_composer_focused", paneReady as? Bool == true)
        try smokeCheck("pane_maximize_shortcut", smokeWebKey("\r", flags: [.control, .option], code: 36))
        let maximized = try await smokeJS("""
            for (let i = 0; i < 80; i++) {
              if (agentCoordPanes.layout.maximized != null) return true;
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            return false;
            """, in: first.webView)
        try smokeCheck("pane_maximized_from_composer", maximized as? Bool == true)
        _ = smokeWebKey("\u{1b}", flags: [], code: 53)
        let restored = try await smokeJS("""
            for (let i = 0; i < 80; i++) {
              if (agentCoordPanes.layout.maximized == null) return true;
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            return false;
            """, in: first.webView)
        try smokeCheck("pane_escape_restores_layout", restored as? Bool == true)
        _ = try await smokeJS("await agentCoordNavigate('agentcoord://overview?project=' + navigationFixture.billing); return true;", in: first.webView)
        try smokeCheck("app_link_pane_back_shortcut", smokeKey("[", flags: [.command], code: 33))
        try await Task.sleep(nanoseconds: 200_000_000)
        let paneBack = try await smokeJS("""
            for (let i = 0; i < 100; i++) {
              if (!navigation.applying && agentCoordPanes.active && agentCoordPanes.selected === 'navigation-manager' &&
                  agentCoordPanes.focusedRecord.frame.contentDocument.querySelector('#message')?.value === 'Keep my pane draft') return true;
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            return false;
            """, in: first.webView)
        smokeResults["navigation_pane_back_debug"] = try await smokeJS("""
            return {active: agentCoordPanes.active, selected: agentCoordPanes.selected,
              draft: agentCoordPanes.focusedRecord?.frame.contentDocument.querySelector('#message')?.value || '',
              stored: state.drafts.get('navigation-manager') || '', applying: navigation.applying,
              history: history.state, error: document.querySelector('#error span').textContent};
            """, in: first.webView)
        try smokeCheck("app_link_back_restores_pane_draft", paneBack as? Bool == true)
        _ = try await smokeJS("""
            window.smokePaneLayout = agentCoordPanes.layout;
            smokePaneLayout.widths = smokePaneLayout.columns.map((_, i) => 1 + i / 4);
            agentCoordPanes.render(); return true;
            """, in: first.webView)
        _ = smokeKey("t", flags: [.command, .control, .option], code: 17)
        let toggleOff = try await smokeJS("""
            for (let i = 0; i < 100; i++) {
              if (!agentCoordPanes.active && state.selected === null) return true;
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            return false;
            """, in: first.webView)
        try smokeCheck("pane_toggle_returns_to_overview", toggleOff as? Bool == true)
        _ = smokeKey("t", flags: [.command, .control, .option], code: 17)
        let toggleOn = try await smokeJS("""
            for (let i = 0; i < 100; i++) {
              if (agentCoordPanes.active)
                return agentCoordPanes.layout === smokePaneLayout && smokePaneLayout.widths[0] === 1 &&
                  agentCoordPanes.focusedRecord.frame.contentDocument.querySelector('#message').value === 'Keep my pane draft';
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            return false;
            """, in: first.webView)
        try smokeCheck("pane_toggle_restores_layout_and_draft", toggleOn as? Bool == true)
        _ = try await smokeJS("goHome(); await select('navigation-manager'); return true;", in: first.webView)
        _ = smokeKey("t", flags: [.command, .control, .option], code: 17)
        let fromThread = try await smokeJS("""
            for (let i = 0; i < 100; i++) {
              if (agentCoordPanes.active) return agentCoordPanes.layout.returnTarget.thread === 'navigation-manager';
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            return false;
            """, in: first.webView)
        try smokeCheck("pane_toggle_remembers_single_thread", fromThread as? Bool == true)
        _ = smokeKey("t", flags: [.command, .control, .option], code: 17)
        let toThread = try await smokeJS("""
            for (let i = 0; i < 100; i++) {
              if (!agentCoordPanes.active && state.detail?.work_thread?.thread_id === 'navigation-manager')
                return document.querySelector('#message').value === 'Keep my pane draft';
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            return false;
            """, in: first.webView)
        try smokeCheck("pane_toggle_returns_to_single_thread", toThread as? Bool == true)
        try await smokeSessionCycling(first)
        _ = try await smokeJS("goHome(); return true;", in: first.webView)
    }

    @MainActor private func smokeSessionCycling(_ item: DesktopWindow) async throws {
        _ = try smokePython("""
            import sys
            from agent_coord.store import CoordinationStore
            store = CoordinationStore(sys.argv[1])
            for name in ['cycling-one', 'cycling-two', 'cycling-three']:
                store.register(session_id=name, client='codex', cwd=str(store.database_path.parent))
                store.threads.update(name, title=name)
            print('{}')
            """)
        _ = try await smokeJS("""
            goHome();
            document.querySelector('#search').value = 'cycling-';
            await refreshList();
            // A pane's read receipt can trigger a newer list request, superseding
            // the awaited refresh. Wait for the filtered cards to be displayed.
            for (let i = 0; i < 100; i++) {
              window.cycleIDs = Array.from(document.querySelectorAll('#sessions button.session[data-thread]'), b => b.dataset.thread);
              if (cycleIDs.length === 3 && cycleIDs.every(id => id.startsWith('cycling-'))) break;
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            if (cycleIDs.length !== 3 || cycleIDs.some(id => !id.startsWith('cycling-'))) throw new Error('Cycling filter did not settle');
            await select(cycleIDs[cycleIDs.length - 1]);
            document.querySelector('#message').value = 'Preserve cycling draft';
            document.querySelector('#composer').hidden = false;
            document.querySelector('#message').disabled = false;
            document.querySelector('#message').focus();
            return true;
            """, in: item.webView)
        try await smokeFocus(item)
        try smokeCheck("next_session_shortcut", smokeWebKey("\t", flags: [.control], code: 48, throughApplication: true))
        let next = try await smokeJS("""
            for (let i = 0; i < 100; i++) {
              if (state.detail?.work_thread.thread_id === cycleIDs[0]) return true;
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            return false;
            """, in: item.webView)
        if next as? Bool != true {
            smokeResults["next_session_debug"] = try await smokeJS("""
                return {selected: state.selected, detail: state.detail?.work_thread?.thread_id, initial: cycleIDs,
                  current: Array.from(document.querySelectorAll('#sessions button.session[data-thread]'), b => ({id: b.dataset.thread, current: b.getAttribute('aria-current')})),
                  panes: agentCoordPanes.active, dialog: document.querySelector('dialog[open]')?.id || '',
                  error: document.querySelector('#error span').textContent};
                """, in: item.webView)
        }
        try smokeCheck("next_session_wraps_in_filtered_view", next as? Bool == true)
        try smokeCheck("previous_session_shortcut", smokeWebKey("\u{19}", flags: [.control, .shift], code: 48))
        let previous = try await smokeJS("""
            for (let i = 0; i < 100; i++) {
              if (state.detail?.work_thread.thread_id === cycleIDs[cycleIDs.length - 1])
                return document.querySelector('#message').value === 'Preserve cycling draft';
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            return false;
            """, in: item.webView)
        smokeResults["session_cycle_debug"] = try await smokeJS("""
            return {selected: state.selected, draft: document.querySelector('#message').value,
              stored: Array.from(state.drafts), initial: cycleIDs,
              current: Array.from(document.querySelectorAll('#sessions button.session[data-thread]'), b => b.dataset.thread),
              error: document.querySelector('#error span').textContent};
            """, in: item.webView)
        try smokeCheck("previous_session_wraps_and_preserves_draft", previous as? Bool == true)
        _ = smokeWebKey("\t", flags: [], code: 48, throughApplication: true)
        let plainTab = try await smokeJS("""
            await new Promise(resolve => setTimeout(resolve, 100));
            return state.selected === cycleIDs[cycleIDs.length - 1];
            """, in: item.webView)
        try smokeCheck("next_session_plain_tab_keeps_selection", plainTab as? Bool == true)
        _ = try await smokeJS("""
            window.cycleDialog = document.createElement('dialog');
            cycleDialog.innerHTML = '<input autofocus><button>Close</button>';
            document.body.append(cycleDialog); cycleDialog.showModal();
            return true;
            """, in: item.webView)
        _ = smokeWebKey("\t", flags: [.control], code: 48, throughApplication: true)
        let modal = try await smokeJS("""
            await new Promise(resolve => setTimeout(resolve, 100));
            const kept = cycleDialog.open && state.selected === cycleIDs[cycleIDs.length - 1];
            cycleDialog.close(); cycleDialog.remove(); return kept;
            """, in: item.webView)
        try smokeCheck("next_session_dialog_keeps_selection", modal as? Bool == true)
        _ = try await smokeJS("""
            agentCoordPanes.tileCurrent();
            const layout = agentCoordPanes.layout;
            // Stack all sessions in one pane to cover inactive tabs as well.
            layout.columns = [1]; layout.groups = [cycleIDs.slice()]; layout.tabs = [cycleIDs[0]];
            layout.selected = cycleIDs[0]; layout.maximized = null;
            agentCoordPanes.render(); agentCoordPanes.focus(cycleIDs[0]);
            for (let i = 0; i < 100; i++) {
              const record = agentCoordPanes.focusedRecord;
              if (record?.api) {
                const doc = record.frame.contentDocument;
                doc.querySelector('#composer').hidden = false;
                const field = doc.querySelector('#message');
                field.disabled = false; field.value = 'Keep tiled keyboard draft'; field.focus();
                if (doc.activeElement === field) return true;
              }
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            throw new Error('Tiled composer did not become focused');
            """, in: item.webView)
        try smokeCheck("next_session_tiled_shortcut", smokeWebKey("\t", flags: [.control], code: 48, throughApplication: true))
        let tiled = try await smokeJS("""
            for (let i = 0; i < 100; i++) {
              if (agentCoordPanes.selected === cycleIDs[1])
                return agentCoordPanes.layout.tabs[0] === cycleIDs[1];
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            return false;
            """, in: item.webView)
        try smokeCheck("next_session_selects_stacked_tab", tiled as? Bool == true)
        _ = smokeWebKey("\u{19}", flags: [.control, .shift], code: 48, throughApplication: true)
        let paneDraft = try await smokeJS("""
            for (let i = 0; i < 100; i++) {
              if (agentCoordPanes.selected === cycleIDs[0])
                return agentCoordPanes.focusedRecord.frame.contentDocument.querySelector('#message').value === 'Keep tiled keyboard draft';
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            return false;
            """, in: item.webView)
        try smokeCheck("previous_session_preserves_tiled_draft", paneDraft as? Bool == true)
    }

    private func finishSmoke(error: String?) {
        guard let report = smokeReport, !quitting else { return }
        smokeResults["error"] = error ?? NSNull()
        if let data = try? JSONSerialization.data(withJSONObject: smokeResults, options: [.prettyPrinted, .sortedKeys]) {
            try? data.write(to: report)
        }
        // Terminate outside the Swift task/GCD callback. AppKit's deferred
        // termination loop must remain able to drain main-queue cleanup work.
        RunLoop.main.perform { NSApp.terminate(nil) }
    }
}

@main private struct AgentCoordApplication {
    static func main() {
        let app = NSApplication.shared
        let delegate = AppDelegate()
        app.setActivationPolicy(.regular)
        app.delegate = delegate
        withExtendedLifetime(delegate) { app.run() }
    }
}
