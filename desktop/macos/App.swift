import AppKit
import WebKit
import UserNotifications
import UniformTypeIdentifiers
import Darwin

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
    let id: String
    var folderSlot = 0
    var isSettings = false
    var isConversationWindow = false
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
    var incomingConversation: [String: Any]?
    var conversationReply: ((Any?, String?) -> Void)?
    var conversationTransferId: String?
    var closeAfterConversationTransfer = false
    let files = FileBrowserController()
    let split = NSSplitViewController()
    var filesItem: NSSplitViewItem?
    var fileTarget = ""
    // Increases whenever this window becomes key; ranks most recent use.
    var activation = 0
    var restorable = true
    var launchRoute = "/"
    var restoredWorkspace: [String: Any]?
    var restoringWorkspace = false
    var workspaceJSON: String?
    var workspaceVersion = 0
    var restorationAttempted = false

    init(window: NSWindow, webView: WKWebView, status: NSStackView, label: NSTextField,
         spinner: NSProgressIndicator, retry: NSButton, id: String = UUID().uuidString.lowercased()) {
        self.id = id
        self.window = window; self.webView = webView; self.status = status
        self.label = label; self.spinner = spinner; self.retry = retry
    }
}

private final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, NSMenuItemValidation,
    WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandlerWithReply, UNUserNotificationCenterDelegate {
    private var windows: [DesktopWindow] = []
    private struct ConversationTransfer {
        weak var source: DesktopWindow?
        weak var target: DesktopWindow?
        let created: Bool
        var finishing = false
    }
    private var conversationTransfers: [String: ConversationTransfer] = [:]
    private var activations = 0
    // Share local storage (including notification focus) while each web view
    // retains its own session storage, selected thread, and message drafts.
    private let websiteDataStore = WKWebsiteDataStore.nonPersistent()
    private var current: DesktopWindow? {
        windows.first { $0.window === NSApp.keyWindow } ??
        windows.first { $0.window === NSApp.mainWindow } ?? windows.last { !$0.isSettings } ?? windows.last
    }
    private var window: NSWindow! { current?.window }
    private var webView: WKWebView! { current?.webView }
    private var backend: Backend!
    private var updater: RibbonUpdater!
    private var explicitStop = false
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
    private var windowStore: WindowRestorationStore?
    private var restoringWindows = false
    private var nextFolderSlot = 0
    private var windowSaveScheduled = false
    private let windowPersistenceQueue = DispatchQueue(label: "com.agentcoord.desktop.windows", qos: .utility)

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
                if let flag = CommandLine.arguments.firstIndex(of: "--smoke-directory"), flag + 1 < CommandLine.arguments.count {
                    directory = URL(fileURLWithPath: CommandLine.arguments[flag + 1])
                }
                smokeDirectory = directory
                if let flag = CommandLine.arguments.firstIndex(of: "--smoke-preferences"), flag + 1 < CommandLine.arguments.count,
                   CommandLine.arguments[flag + 1].hasPrefix("com.agentcoord.desktop.smoke.") {
                    smokePreferences = CommandLine.arguments[flag + 1]
                } else { smokePreferences = "com.agentcoord.desktop.smoke." + UUID().uuidString }
                preferences = UserDefaults(suiteName: smokePreferences!)!
                if !CommandLine.arguments.contains("--smoke-window-restoration") {
                    application(NSApp, open: [URL(string: "agentcoord://overview")!])
                }
                smokeResults["cold_start_link_queued"] = pendingAppLinks.count == 1
                smokeResults["launch_working_directory_matches_environment"] =
                    ProcessInfo.processInfo.environment["PWD"] == FileManager.default.currentDirectoryPath
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
            updater = RibbonUpdater(compatibility: { [weak self] target in
                guard let backend = self?.backend else { throw Backend.failure("Runtime unavailable.") }
                try backend.validateCompatibility(target)
            }, preflight: { [weak self] target, completion in
                guard let backend = self?.backend else { completion(Backend.failure("Runtime unavailable.")); return }
                backend.preflight(target, completion: completion)
            })
            makeMenu()
            sessionKeyMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { event in
                let target = (event.window ?? NSApp.keyWindow) as? SessionWindow
                return target?.routeSessionShortcut(event) == true ? nil : event
            }
            let database = smokeDirectory?.appendingPathComponent("state.sqlite3").path ?? configuration.databasePath
            windowStore = WindowRestorationStore(url: directory.appendingPathComponent("windows/state.json"), database: database)
            restoreWindows()
            UNUserNotificationCenter.current().delegate = self
            activity = ProcessInfo.processInfo.beginActivity(options: .userInitiatedAllowingIdleSystemSleep,
                reason: "Receive Ribbon Field turn completions while the window is closed")
            backend.ready = { [weak self] url in
                guard let self = self else { return }
                self.serverURL = url
                let links = self.pendingAppLinks
                self.pendingAppLinks.removeAll()
                for link in links { self.openAppLink(link) }
                for item in self.windows {
                    if !item.isSettings { item.files.connect(BackendWorkspaceFiles(baseURL: url, local: true)) }
                    self.configureScripts(item.webView)
                    item.webView.load(URLRequest(url: self.destination(for: item, base: url)))
                }
            }
            backend.exited = { [weak self] message in
                guard let self = self, !self.quitting else { return }
                self.serverURL = nil
                self.showStatus(message, failed: true)
                if self.smokeReport != nil { self.finishSmoke(error: message) }
            }
            startBackend()
            if smokeReport == nil { updater.start() }
            let links = pendingAppLinks
            pendingAppLinks.removeAll()
            for link in links { openAppLink(link) }
            if smokeReport != nil {
                DispatchQueue.main.asyncAfter(deadline: .now() + 90) { [weak self] in
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
        app.addItem(updater.makeMenuItem())
        app.addItem(withTitle: "Settings…", action: #selector(showSettings), keyEquivalent: ",").target = self
        app.addItem(.separator())
        app.addItem(withTitle: "Hide Ribbon Field", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        app.addItem(.separator())
        app.addItem(withTitle: "Stop Work and Quit…", action: #selector(confirmStopAndQuit), keyEquivalent: "").target = self
        app.addItem(withTitle: "Quit Ribbon Field", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        let file = menu("File")
        file.addItem(withTitle: "New Session", action: #selector(webCommand(_:)), keyEquivalent: "n").representedObject = "newSession"
        file.addItem(withTitle: "Open Project…", action: #selector(webCommand(_:)), keyEquivalent: "o").representedObject = "openFolder"
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
        let sendWork = view.addItem(withTitle: "Send Work…", action: #selector(webCommand(_:)), keyEquivalent: "K")
        sendWork.keyEquivalentModifierMask = [.command, .shift]; sendWork.representedObject = "sendWork"
        let scratchpad = view.addItem(withTitle: "Toggle Project Scratchpad", action: #selector(webCommand(_:)), keyEquivalent: "S")
        scratchpad.keyEquivalentModifierMask = [.command, .shift]; scratchpad.representedObject = "projectScratchpad"
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

    @discardableResult private func makeWindow(url: URL? = nil, restoring saved: RestoredWindow? = nil) -> DesktopWindow {
        let settings = saved?.kind == .settings || url?.path == "/settings"
        let conversation = saved?.kind == .conversation || URLComponents(url: url ?? serverURL ?? URL(fileURLWithPath: "/"), resolvingAgainstBaseURL: false)?
            .queryItems?.contains { $0.name == "detached" && $0.value == "1" } == true
        if settings, let existing = windows.first(where: { $0.isSettings }) {
            existing.window.deminiaturize(nil)
            existing.window.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            return existing
        }
        let previous = current?.window
        let window = SessionWindow(contentRect: NSRect(x: 0, y: 0, width: settings ? 720 : conversation ? 880 : 1240, height: settings || conversation ? 760 : 820),
            styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = settings ? "Settings — Ribbon Field" : url?.path == "/monitor" ? "Coordination Monitor — Ribbon Field" : "Ribbon Field"
        window.minSize = settings || conversation ? NSSize(width: 480, height: 420) : NSSize(width: 720, height: 520)
        window.isReleasedWhenClosed = false
        window.delegate = self
        window.tabbingMode = .disallowed
        if saved == nil && smokeReport == nil && (settings || windows.isEmpty) { window.setFrameAutosaveName(settings ? "AgentCoordSettings" : "AgentCoordWorkspace") }
        if settings {
            if smokeReport != nil || !window.setFrameUsingName("AgentCoordSettings") { window.center() }
        } else if let previous = previous, !conversation {
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
                                 spinner: spinner, retry: retry, id: saved?.id ?? UUID().uuidString.lowercased())
        item.isSettings = settings
        item.isConversationWindow = conversation
        item.files.preferences = preferences
        item.folderSlot = saved?.folderSlot ?? nextFolderSlot
        nextFolderSlot = max(nextFolderSlot, item.folderSlot + 1)
        let initialRoute = saved?.route ?? url.map(localRoute) ?? (settings ? "/settings" : "/")
        item.launchRoute = URL(string: initialRoute).map(localRoute) ?? "/"
        item.restoredWorkspace = saved?.workspaceState
        item.workspaceJSON = saved?.workspaceJSON
        if let saved = saved {
            var frame = saved.frame
            if let displayID = saved.displayID, let oldScreen = saved.screen,
               let screen = NSScreen.screens.first(where: { ($0.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber)?.uint32Value == displayID }) {
                frame.origin.x += screen.frame.minX - oldScreen.minX
                frame.origin.y += screen.frame.minY - oldScreen.minY
            }
            frame = WindowRestorationStore.usableFrame(frame, screens: NSScreen.screens.map(\.visibleFrame), minimum: window.minSize)
            window.setFrame(frame, display: false)
        }
        let restoredFrame = saved == nil ? nil : window.frame
        window.cycleSession = { [weak item] backwards in
            let command = backwards ? "previousSession" : "nextSession"
            guard let item = item, item.loaded, item.commands.contains(command) else { return false }
            item.webView.callAsyncJavaScript("return window.agentCoordDesktop?.command(command) ?? false",
                arguments: ["command": command], in: nil, in: .page) { _ in }
            return true
        }
        // Settings shares the web preferences store without a conversation or Files pane.
        if !settings && !conversation {
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
        }
        // Attaching NSSplitViewController adopts its children's preferred size.
        // Apply the saved outer frame after that attachment has completed.
        if let frame = restoredFrame {
            window.setFrame(frame, display: false)
            if !settings && !conversation { item.split.splitView.setPosition(max(440, frame.width - 310), ofDividerAt: 0) }
        }
        windows.append(item)
        if let destination = url ?? serverURL.map({ self.destination(for: item, base: $0) }) {
            if !settings, let serverURL = serverURL { item.files.connect(BackendWorkspaceFiles(baseURL: serverURL, local: true)) }
            configureScripts(webView)
            webView.load(URLRequest(url: destination))
        }
        showStatus("Opening Ribbon Field…", failed: false, in: item)
        if saved == nil { window.makeKeyAndOrderFront(nil) }
        else { window.orderFront(nil) }
        NSApp.activate(ignoringOtherApps: true)
        if saved?.minimized == true { minimizeWhenOnScreen(window) }
        scheduleWindowSave()
        return item
    }

    private func item(for view: WKWebView) -> DesktopWindow? { windows.first { $0.webView === view } }

    private func localRoute(_ url: URL) -> String {
        guard let source = URLComponents(url: url, resolvingAgainstBaseURL: false) else { return "/" }
        var route = URLComponents()
        route.percentEncodedPath = source.percentEncodedPath
        route.percentEncodedQuery = source.percentEncodedQuery
        route.percentEncodedFragment = source.percentEncodedFragment
        route.queryItems = route.queryItems?.filter { !["directory", "new_project"].contains($0.name) }
        if route.queryItems?.isEmpty == true { route.queryItems = nil }
        let value = route.string ?? "/"
        return WindowRestorationStore.validRoute(value) ? value : "/"
    }

    private func destination(for item: DesktopWindow, base: URL) -> URL {
        URL(string: item.launchRoute, relativeTo: base)?.absoluteURL ?? base
    }

    /// AppKit ignores miniaturize while a window is still being ordered in during
    /// launch, so retry briefly until the restored window is actually minimized.
    private func minimizeWhenOnScreen(_ window: NSWindow, attempts: Int = 20) {
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) { [weak self, weak window] in
            guard let window = window, !window.isMiniaturized, attempts > 0 else { return }
            window.miniaturize(nil)
            self?.minimizeWhenOnScreen(window, attempts: attempts - 1)
        }
    }

    private func restoreWindows() {
        restoringWindows = true
        let saved = windowStore?.load()
        nextFolderSlot = saved?.nextFolderSlot ?? 0
        for record in saved?.windows ?? [] { makeWindow(restoring: record) }
        if windows.isEmpty { makeWindow() }
        let focus = { [weak self] in
            guard let self = self else { return }
            if let focused = self.windows.first(where: { $0.id == saved?.focusedWindow && !$0.window.isMiniaturized }) {
                focused.window.makeKeyAndOrderFront(nil)
            }
        }
        focus()
        // Deferred miniaturizing and launch activation can move key status; the
        // main queue runs this after every restored window has been minimized.
        DispatchQueue.main.async(execute: focus)
        restoringWindows = false
        saveWindowSet()
    }

    private func scheduleWindowSave() {
        guard !quitting, !restoringWindows, !windowSaveScheduled else { return }
        windowSaveScheduled = true
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) { [weak self] in
            self?.windowSaveScheduled = false
            self?.saveWindowSet()
        }
    }

    private func saveWindowSet(_ completion: ((Error?) -> Void)? = nil) {
        guard !quitting, !restoringWindows, conversationTransfers.isEmpty, let store = windowStore else { completion?(nil); return }
        let records = windows.filter(\.restorable).map { item -> RestoredWindow in
            let kind: RestoredWindow.Kind = item.isSettings ? .settings : item.isConversationWindow ? .conversation :
                item.launchRoute.hasPrefix("/monitor") ? .monitor : .workspace
            let screen = item.window.screen
            return RestoredWindow(id: item.id, folderSlot: item.folderSlot, kind: kind, route: item.launchRoute,
                frame: item.window.frame, screen: screen?.frame,
                displayID: (screen?.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber)?.uint32Value,
                minimized: item.window.isMiniaturized, workspaceJSON: item.workspaceJSON)
        }
        let focused = current.flatMap { $0.restorable ? $0.id : nil }, nextSlot = nextFolderSlot
        windowPersistenceQueue.async {
            var failure: Error?
            do { try store.save(windows: records, focusedWindow: focused, nextFolderSlot: nextSlot) }
            catch { failure = error; NSLog("Could not save Ribbon Field window state: %@", error.localizedDescription) }
            if let completion = completion {
                let error = failure
                DispatchQueue.main.async { completion(error) }
            }
        }
    }

    private func acceptWorkspaceState(_ state: [String: Any], for item: DesktopWindow, completion: (() -> Void)? = nil) {
        guard JSONSerialization.isValidJSONObject(state) else { completion?(); return }
        item.workspaceVersion += 1
        let version = item.workspaceVersion
        if let route = state["route"] as? String, WindowRestorationStore.validRoute(route), let url = URL(string: route) {
            item.launchRoute = localRoute(url)
        }
        windowPersistenceQueue.async { [weak self, weak item] in
            let json = (try? JSONSerialization.data(withJSONObject: state, options: [.sortedKeys]))
                .flatMap { String(data: $0, encoding: .utf8) }
            DispatchQueue.main.async {
                if let item = item, let json = json, item.workspaceVersion == version {
                    item.workspaceJSON = json
                    self?.scheduleWindowSave()
                }
                completion?()
            }
        }
    }

    private func captureWindowSetForQuit(_ completion: @escaping (Error?) -> Void) {
        let group = DispatchGroup()
        var completed = false
        let finish = { [self] in
            guard !completed else { return }
            completed = true
            saveWindowSet(completion)
        }
        for item in windows where item.restorable && item.loaded && (item.restoredWorkspace == nil || item.restorationAttempted) && !item.restoringWorkspace {
            group.enter()
            item.webView.callAsyncJavaScript("return window.agentCoordDesktop?.exportWindowState?.() ?? null",
                arguments: [:], in: nil, in: .page) { [weak self] result in
                if !completed, case .success(let value) = result, let state = value as? [String: Any] {
                    self?.acceptWorkspaceState(state, for: item) { group.leave() }
                } else { group.leave() }
            }
        }
        group.notify(queue: .main, execute: finish)
        // A crashed web process must not prevent interface Quit. Its last
        // durable snapshot is retained instead of replacing it with emptiness.
        DispatchQueue.main.asyncAfter(deadline: .now() + 5, execute: finish)
    }

    private func prepareWindowsForQuit(_ completion: @escaping (Error?) -> Void) {
        let deadline = Date().addingTimeInterval(10)
        func check() {
            let group = DispatchGroup()
            var busy = false
            var answered = false
            for item in self.windows where item.restorable && item.loaded {
                group.enter()
                item.webView.callAsyncJavaScript("return window.agentCoordDesktop?.windowStateBusy?.() ?? false",
                    arguments: [:], in: nil, in: .page) { result in
                    if case .success(let value) = result, value as? Bool == true { busy = true }
                    group.leave()
                }
            }
            group.notify(queue: .main) {
                guard !answered else { return }
                answered = true
                if !busy { self.captureWindowSetForQuit(completion) }
                else if Date() >= deadline {
                    completion(Backend.failure("A message or image is still being accepted. Try quitting again when it finishes."))
                } else { DispatchQueue.main.asyncAfter(deadline: .now() + 0.1, execute: check) }
            }
            DispatchQueue.main.asyncAfter(deadline: .now() + 5) {
                guard !answered else { return }
                answered = true
                completion(Backend.failure("A window is not responding while saving drafts. Try quitting again when it responds."))
            }
        }
        check()
    }

    private func restoreWorkspace(in item: DesktopWindow, completion: (() -> Void)? = nil) {
        guard !item.restoringWorkspace else { return }
        guard let snapshot = item.restoredWorkspace else { completion?(); return }
        item.restoringWorkspace = true
        item.restorationAttempted = true
        var finished = false
        let finish: (Bool) -> Void = { [weak self, weak item] restored in
            guard !finished, let item = item else { return }
            finished = true
            item.restoringWorkspace = false
            if restored {
                item.restoredWorkspace = nil
            } else {
                // Keep the snapshot available for a later Reload/retry.
                item.restoredWorkspace = snapshot
                NSLog("Could not restore Ribbon Field window state; retaining its saved workspace.")
            }
            self?.scheduleWindowSave()
            completion?()
        }
        item.webView.callAsyncJavaScript("return await window.agentCoordDesktop.restoreWindowState(snapshot)",
            arguments: ["snapshot": snapshot], in: nil, in: .page) { result in
            if case .success(let value) = result, value as? Bool == true { finish(true) } else { finish(false) }
        }
        // A restore waiting on an unresponsive runtime must not stop this window
        // from saving its state or receiving links and moved conversations.
        DispatchQueue.main.asyncAfter(deadline: .now() + 20) { finish(false) }
    }

    private func windowStateError(_ message: String) {
        webView?.callAsyncJavaScript("const box = document.getElementById('error'); if (box) { box.querySelector('span').textContent = message; box.hidden = false; }",
            arguments: ["message": message], in: nil, in: .page) { _ in }
    }

    private func positionConversationWindow(_ window: NSWindow, near drop: NSPoint) {
        let screen = NSScreen.screens.first { $0.frame.contains(drop) } ?? window.screen ?? NSScreen.main
        var frame = window.frame
        frame.origin = NSPoint(x: drop.x - 100, y: drop.y - frame.height + 20)
        if let visible = screen?.visibleFrame {
            frame.origin.x = max(visible.minX, min(frame.minX, visible.maxX - frame.width))
            frame.origin.y = max(visible.minY, min(frame.minY, visible.maxY - frame.height))
        }
        window.setFrame(frame, display: true)
    }

    private func receiveConversation(_ payload: [String: Any], in target: DesktopWindow, from source: DesktopWindow, created: Bool,
                                     reply: @escaping (Any?, String?) -> Void) {
        guard target.conversationTransferId == nil else { reply(nil, "This window is already receiving a tab."); return }
        let transferId = UUID().uuidString.lowercased()
        conversationTransfers[transferId] = ConversationTransfer(source: source, target: target, created: created)
        target.conversationTransferId = transferId
        target.incomingConversation = payload
        target.conversationReply = reply
        deliverConversation(to: target)
        DispatchQueue.main.asyncAfter(deadline: .now() + 30) { [weak self, weak target] in
            guard let self = self, let target = target, let transfer = self.conversationTransfers[transferId], !transfer.finishing else { return }
            let reply = target.conversationReply
            target.conversationReply = nil
            target.incomingConversation = nil
            self.finishConversationTransfer(transferId, committed: transfer.source == nil) { _, _ in }
            reply?(nil, "Moving the conversation timed out. The original tab is still available.")
        }
    }

    private func deliverConversation(to target: DesktopWindow) {
        guard target.navigationReady, !target.restoringWorkspace, let payload = target.incomingConversation, let transferId = target.conversationTransferId,
              target.conversationReply != nil else { return }
        target.incomingConversation = nil
        target.webView.callAsyncJavaScript("return await window.agentCoordConversationTransfer.stage(payload, transferId)",
            arguments: ["payload": payload, "transferId": transferId], in: nil, in: .page) { [weak self, weak target] result in
            guard let self = self, let target = target, let reply = target.conversationReply else { return }
            target.conversationReply = nil
            switch result {
            case .success(let value) where value as? Bool == true:
                target.window.makeKeyAndOrderFront(nil)
                NSApp.activate(ignoringOtherApps: true)
                if self.conversationTransfers[transferId]?.source == nil {
                    self.finishConversationTransfer(transferId, committed: true) { _, _ in }
                }
                reply(["transferId": transferId], nil)
            default:
                self.finishConversationTransfer(transferId, committed: false) { _, _ in
                    reply(nil, "The conversation could not be moved. The original tab is still available.")
                }
            }
        }
    }

    private func finishConversationTransfer(_ transferId: String, committed: Bool,
                                            reply: @escaping (Any?, String?) -> Void) {
        guard let transfer = conversationTransfers[transferId], let target = transfer.target,
              windows.contains(where: { $0 === target }), !transfer.finishing else {
            reply(nil, "The destination window is no longer available."); return
        }
        conversationTransfers[transferId]?.finishing = true
        target.webView.callAsyncJavaScript("return await window.agentCoordConversationTransfer.finish(transferId, committed)",
            arguments: ["transferId": transferId, "committed": committed], in: nil, in: .page) { [weak self] result in
            self?.conversationTransfers.removeValue(forKey: transferId)
            self?.scheduleWindowSave()
            target.conversationTransferId = nil
            guard case .success(let value) = result, value as? Bool == true else {
                transfer.source?.closeAfterConversationTransfer = false
                if transfer.created { target.window.close() }
                reply(nil, "The destination could not confirm the move. The original tab is being restored.")
                return
            }
            if !committed && transfer.created { target.window.close() }
            if committed, let source = transfer.source, source.closeAfterConversationTransfer {
                source.closeAfterConversationTransfer = false
                source.window.performClose(nil)
            } else { transfer.source?.closeAfterConversationTransfer = false }
            reply(true, nil)
        }
    }

    func windowDidBecomeKey(_ notification: Notification) {
        guard let item = windows.first(where: { $0.window === notification.object as? NSWindow }) else { return }
        activations += 1
        item.activation = activations
        if !quitting { item.restorable = true; scheduleWindowSave() }
    }

    func windowDidMove(_ notification: Notification) { scheduleWindowSave() }
    func windowDidResize(_ notification: Notification) { scheduleWindowSave() }
    func windowDidMiniaturize(_ notification: Notification) { scheduleWindowSave() }
    func windowDidDeminiaturize(_ notification: Notification) { scheduleWindowSave() }

    func windowWillClose(_ notification: Notification) {
        guard let closing = notification.object as? NSWindow,
              let item = windows.first(where: { $0.window === closing }) else { return }
        // Closing the final normal window keeps it, so it stays restorable with
        // its folder slot and tabs; only windows that are released stay closed.
        let keptAsFinalWindow = !item.isSettings && windows.filter({ !$0.isSettings }).count <= 1
        if !quitting && !keptAsFinalWindow { item.restorable = false; saveWindowSet() }
        guard !keptAsFinalWindow else { return }
        for (transferId, transfer) in conversationTransfers where transfer.target === item {
            transfer.source?.closeAfterConversationTransfer = false
            conversationTransfers.removeValue(forKey: transferId)
        }
        let reply = item.conversationReply
        item.conversationReply = nil
        reply?(nil, "The new window closed before the conversation was ready. The original tab is still available.")
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
    @objc private func showSettings() {
        guard let url = serverURL else { return }
        makeWindow(url: url.appendingPathComponent("settings"))
    }
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
        if menuItem.action == #selector(showSettings) { return serverURL != nil }
        if menuItem.action == #selector(toggleFiles) { return current?.filesItem != nil }
        if menuItem.action == #selector(duplicateWindow) && current?.isSettings == true { return false }
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
        if current?.isSettings == true || current?.isConversationWindow == true {
            let workspace = windows.last { !$0.isSettings && !$0.isConversationWindow && $0.launchRoute != "/monitor" } ?? makeWindow()
            workspace.window.makeKeyAndOrderFront(nil)
        }
        showWindow()
        if current?.commands.contains("workspace") == true { runCommand("workspace") }
        else if let url = serverURL, webView?.url?.path != "/" { webView.load(URLRequest(url: url)) }
    }
    @objc private func reload() { if serverURL != nil { webView.reload() } else { startBackend() } }
    @objc private func retryLoad() {
        if let url = serverURL {
            showStatus("Opening Ribbon Field…", failed: false, in: current)
            webView.load(URLRequest(url: current?.isSettings == true ? url.appendingPathComponent("settings") : url))
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
        if url.scheme == "agentcoord", url.host == "attribution", url.user == nil,
           url.password == nil, url.port == nil, url.path.isEmpty,
           let parts = URLComponents(url: url, resolvingAgainstBaseURL: false),
           parts.queryItems?.count == 1, let query = parts.queryItems?.first,
           query.name == "download_token", let token = query.value,
           let identifier = UUID(uuidString: token), identifier.uuidString.lowercased() == token {
            // Explicit user-initiated attribution; never inspect the clipboard.
            guard let base = serverURL else { pendingAppLinks.append(url); return }
            Task {
                do {
                    let (data, _) = try await URLSession.shared.data(from: base.appendingPathComponent("api/browser/config"))
                    guard let config = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                          let csrf = config["token"] as? String else { return }
                    var request = URLRequest(url: base.appendingPathComponent("api/browser/launch-metrics/attribution"))
                    request.httpMethod = "POST"
                    request.setValue("application/json", forHTTPHeaderField: "Content-Type")
                    request.setValue(csrf, forHTTPHeaderField: "X-Agent-Coord-Token")
                    request.httpBody = try JSONSerialization.data(withJSONObject: ["download_token": token])
                    _ = try await URLSession.shared.data(for: request)
                } catch { /* Attribution cannot interrupt opening the app. */ }
            }
            NSApp.activate(ignoringOtherApps: true)
            return
        }
        guard url.scheme == "agentcoord", ["overview", "view", "thread"].contains(url.host ?? ""),
              url.user == nil, url.password == nil, url.port == nil,
              url.absoluteString.count <= 8192 else { return }
        guard !windows.isEmpty else { pendingAppLinks.append(url); return }
        let targetID = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems?.first { $0.name == "window" }?.value
        var target = preferred ?? windows.first { $0.id == targetID?.lowercased() } ?? current!
        if target.isSettings || target.launchRoute.hasPrefix("/monitor") {
            target = windows.last { !$0.isSettings && !$0.launchRoute.hasPrefix("/monitor") } ?? makeWindow()
        }
        target.pendingLinks.append(url)
        target.window.deminiaturize(nil)
        target.window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        deliverLinks(to: target)
    }

    private func deliverLinks(to item: DesktopWindow) {
        guard item.navigationReady, !item.restoringWorkspace, !item.navigating, !item.pendingLinks.isEmpty else { return }
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
              let workspace = try? String(contentsOf: resources.appendingPathComponent("workspace-state.js"), encoding: .utf8),
              let palette = try? String(contentsOf: resources.appendingPathComponent("command-palette.js"), encoding: .utf8),
              let data = try? JSONSerialization.data(withJSONObject: preferences.dictionary(forKey: "webPreferences") ?? [:]),
              let json = String(data: data, encoding: .utf8) else { return }
        let snapshot = item(for: webView)?.restoredWorkspace
        let restoration = snapshot.flatMap { try? JSONSerialization.data(withJSONObject: $0) }
            .flatMap { String(data: $0, encoding: .utf8) } ?? "null"
        let controller = webView.configuration.userContentController
        controller.removeAllUserScripts()
        controller.addUserScript(WKUserScript(source: palette, injectionTime: .atDocumentStart, forMainFrameOnly: true))
        controller.addUserScript(WKUserScript(source: workspace, injectionTime: .atDocumentStart, forMainFrameOnly: true))
        let bridge = WindowRestorationStore.script(source, values: [
            "__AGENT_COORD_PREFERENCES__": json, "__AGENT_COORD_WINDOW_STATE__": restoration,
            "__AGENT_COORD_FOLDER_SLOT__": String(item(for: webView)?.folderSlot ?? 0),
            "__AGENT_COORD_CONVERSATION_WINDOW__": item(for: webView)?.isConversationWindow == true ? "true" : "false",
            "__AGENT_COORD_WINDOW_ID__": item(for: webView)?.id ?? "",
        ])
        controller.addUserScript(WKUserScript(source: bridge,
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
        case "openSettings":
            showSettings()
            replyHandler(true, nil)
        case "closeSettings":
            guard owner.isSettings else { replyHandler(nil, "This is not the Settings window."); return }
            replyHandler(true, nil)
            DispatchQueue.main.async { owner.window.performClose(nil) }
        case "fileEditor":
            guard owner.isSettings, let operation = body["operation"] as? String,
                  ["get", "choose", "reset"].contains(operation) else {
                replyHandler(nil, "Open Settings to choose a file editor."); return
            }
            let result = { [self] () -> [String: String] in
                let path = preferences.string(forKey: FileBrowserController.editorKey) ?? ""
                return ["path": path, "name": path.isEmpty ? "System default" : FileManager.default.displayName(atPath: path)]
            }
            if operation == "reset" { preferences.removeObject(forKey: FileBrowserController.editorKey) }
            guard operation == "choose" else { replyHandler(result(), nil); return }
            guard owner.window.attachedSheet == nil else { replyHandler(nil, "Finish the open file panel first."); return }
            let panel = NSOpenPanel()
            panel.title = "Choose File Editor"; panel.prompt = "Use Editor"
            panel.canChooseFiles = true; panel.canChooseDirectories = false
            panel.allowsMultipleSelection = false; panel.treatsFilePackagesAsDirectories = false
            panel.allowedContentTypes = [.applicationBundle]
            panel.directoryURL = URL(fileURLWithPath: "/Applications", isDirectory: true)
            panel.beginSheetModal(for: owner.window) { [self] response in
                if response == .OK, let url = panel.url { preferences.set(url.path, forKey: FileBrowserController.editorKey) }
                replyHandler(result(), nil)
            }
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
        case "closeConversationWindow":
            guard owner.isConversationWindow else { replyHandler(false, nil); return }
            replyHandler(true, nil)
            if conversationTransfers.values.contains(where: { $0.source === owner }) {
                owner.closeAfterConversationTransfer = true
                return
            }
            DispatchQueue.main.async { owner.window.performClose(nil) }
        case "completeConversationTransfer":
            guard let transferId = body["transferId"] as? String,
                  conversationTransfers[transferId]?.source === owner,
                  let committed = body["committed"] as? Bool else {
                replyHandler(nil, "This conversation transfer is no longer available."); return
            }
            finishConversationTransfer(transferId, committed: committed, reply: replyHandler)
        case "navigationReady":
            owner.navigationReady = true
            replyHandler(true, nil)
            restoreWorkspace(in: owner) { [weak self, weak owner] in
                guard let self = self, let owner = owner else { return }
                self.deliverLinks(to: owner)
                self.deliverConversation(to: owner)
            }
        case "saveWindowState":
            if (owner.restoredWorkspace == nil || owner.restorationAttempted) && !owner.restoringWorkspace, let state = body["state"] as? [String: Any] {
                owner.restoredWorkspace = nil
                acceptWorkspaceState(state, for: owner)
            }
            replyHandler(true, nil)
        case "windowState":
            if let route = body["route"] as? String, WindowRestorationStore.validRoute(route), let url = URL(string: route) {
                owner.launchRoute = localRoute(url); scheduleWindowSave()
            } else if let url = source.url { owner.launchRoute = localRoute(url); scheduleWindowSave() }
            if let title = body["title"] as? String {
                owner.window.title = String(title.prefix(160)) + " — Ribbon Field"
            }
            owner.commands = Set((body["commands"] as? [String] ?? []).prefix(32))
            if !owner.isSettings {
                owner.fileTarget = body["fileTarget"] as? String ?? ""
                owner.files.canInsert = owner.commands.contains("insertFiles")
                owner.files.setConversationRoot(body["workspace"] as? String ?? "")
            }
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
        case "detachConversation":
            guard !owner.isSettings, owner.window.attachedSheet == nil,
                  let x = body["x"] as? Double, let y = body["y"] as? Double,
                  x.isFinite, y.isFinite, abs(x) < 100_000, abs(y) < 100_000,
                  let payload = body["payload"] as? [String: Any],
                  let thread = payload["thread"] as? [String: Any],
                  let id = thread["thread_id"] as? String, !id.isEmpty, id.count <= 200, id != "new-session",
                  let cwd = payload["cwd"] as? String,
                  JSONSerialization.isValidJSONObject(payload),
                  let data = try? JSONSerialization.data(withJSONObject: payload), data.count <= 32 * 1024 * 1024,
                  let base = serverURL else {
                replyHandler(nil, "The conversation cannot be detached yet."); return
            }
            // DOM coordinates are relative to the web view, including points
            // captured outside it. AppKit handles negative/multiple-display origins.
            let local = NSPoint(x: x * source.pageZoom,
                y: source.isFlipped ? y * source.pageZoom : source.bounds.height - y * source.pageZoom)
            let drop = owner.window.convertPoint(toScreen: source.convert(local, to: nil))
            guard !owner.window.frame.contains(drop) else { replyHandler(false, nil); return }
            // A tab can move into any existing conversation window, including
            // the original workspace. No second copy/window is needed.
            let destinationWindow = NSApp.orderedWindows.first { window in
                window !== owner.window && window.isVisible && !window.isMiniaturized && window.frame.contains(drop) &&
                windows.contains { $0.window === window && !$0.isSettings && $0.webView.url?.path == "/" && $0.navigationReady }
            }
            if let destinationWindow = destinationWindow,
               let target = windows.first(where: { $0.window === destinationWindow }) {
                receiveConversation(payload, in: target, from: owner, created: false, reply: replyHandler)
                return
            }
            // Dragging the sole tab out of an auxiliary window moves that
            // window itself, preserving its document and live UI exactly.
            if owner.isConversationWindow, payload["tabCount"] as? Int == 1 {
                positionConversationWindow(owner.window, near: drop)
                owner.window.makeKeyAndOrderFront(nil)
                NSApp.activate(ignoringOtherApps: true)
                replyHandler(false, nil)
                return
            }
            var destination = URLComponents(url: base, resolvingAgainstBaseURL: false)!
            destination.path = "/"
            destination.queryItems = [URLQueryItem(name: "directory", value: cwd), URLQueryItem(name: "detached", value: "1")]
            destination.fragment = nil
            let target = makeWindow(url: destination.url!)
            positionConversationWindow(target.window, near: drop)
            receiveConversation(payload, in: target, from: owner, created: true, reply: replyHandler)
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
            panel.prompt = workspace ? "Open Project" : "Insert Paths"
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
            var info: [String: Any] = ["desktopWindow": owner.id]
            if let thread = body["thread"] as? String, isThreadIdentifier(thread) { info["thread"] = thread }
            content.userInfo = info
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
            self.activateNotification(response.notification.request.identifier,
                info: response.notification.request.content.userInfo)
            completionHandler()
        }
    }

    private func activateNotification(_ id: String, info: [AnyHashable: Any], completion: ((DesktopWindow?) -> Void)? = nil) {
        let poster = windows.first { $0.id == info["desktopWindow"] as? String }
        let click = { (routed: Bool) in
            poster?.webView.callAsyncJavaScript("window.__agentCoordNotificationClick?.(id, routed)",
                arguments: ["id": id, "routed": routed], in: nil, in: .page) { _ in }
        }
        // The posting window only won the delivery claim. Open the thread
        // where its tab already is instead of in that window.
        if let thread = info["thread"] as? String, isThreadIdentifier(thread) {
            UNUserNotificationCenter.current().removeDeliveredNotifications(withIdentifiers: [id])
            click(true)
            revealThread(thread, completion: completion)
        } else {
            let target = poster ?? current
            target?.window.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            if target === poster { click(false) }
            completion?(target)
        }
    }

    private func isThreadIdentifier(_ value: String) -> Bool {
        !value.isEmpty && value.count <= 200 &&
            value.unicodeScalars.allSatisfy { $0.isASCII && (CharacterSet.alphanumerics.contains($0) || "_.~-".unicodeScalars.contains($0)) }
    }

    // Prefer the most recently focused window that already has the thread's
    // tab; otherwise use the most recently focused main workspace window.
    private func revealThread(_ thread: String, completion: ((DesktopWindow?) -> Void)? = nil) {
        let candidates = windows.enumerated()
            .filter { !$0.element.isSettings && !$0.element.launchRoute.hasPrefix("/monitor") }
            .sorted { ($0.element.activation, $0.offset) > ($1.element.activation, $1.offset) }
            .map(\.element)
        var owners = Set<ObjectIdentifier>(), remaining = 1, finished = false
        let finish = { [weak self] in
            guard let self = self, !finished else { return }
            finished = true
            let live = candidates.filter { item in self.windows.contains { $0 === item } }
            let target = live.first { owners.contains(ObjectIdentifier($0)) } ?? live.first { !$0.isConversationWindow } ?? live.first
            self.openAppLink(URL(string: "agentcoord://thread/" + thread)!, preferred: target)
            completion?(target)
        }
        let answered = { remaining -= 1; if remaining == 0 { finish() } }
        for item in candidates where item.navigationReady {
            remaining += 1
            item.webView.callAsyncJavaScript("return window.agentCoordHasConversation?.(thread) === true",
                arguments: ["thread": thread], in: nil, in: .page) { result in
                if case .success(let value) = result, value as? Bool == true { owners.insert(ObjectIdentifier(item)) }
                answered()
            }
        }
        answered()
        // A busy page must not make the notification appear to do nothing.
        DispatchQueue.main.asyncAfter(deadline: .now() + 2, execute: finish)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool { showWindow(); return true }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if quitting { return .terminateNow }
        guard backend != nil, updater != nil else { return .terminateNow }
        if checkingQuit { return .terminateCancel }
        checkingQuit = true
        let authorize = {
            self.updater.canTerminateForUpdate { allowed in
                if allowed {
                    self.prepareWindowsForQuit { error in
                        self.checkingQuit = false
                        // Window state is best-effort. Quit, logout and update must
                        // never be refused; the last durable snapshot is retained.
                        if let error = error {
                            NSLog("Ribbon Field quit with its last saved window state: %@", error.localizedDescription)
                        }
                        self.quitting = true
                        self.backend.detach()
                        // Release before Sparkle launches the replacement desktop.
                        if self.lockFile >= 0 { flock(self.lockFile, LOCK_UN); close(self.lockFile); self.lockFile = -1 }
                        NSApp.reply(toApplicationShouldTerminate: true)
                    }
                } else {
                    self.checkingQuit = false
                    self.explicitStop = false
                    let prefix = self.backend.process == nil ? "Work has stopped. Restart the local runtime to continue.\n\n" : ""
                    let reason = self.updater.lastDeferral?.localizedDescription ?? "The update decision changed. Try quitting again."
                    self.showStatus(prefix + reason, failed: true)
                    NSApp.reply(toApplicationShouldTerminate: false)
                }
            }
        }
        let continueQuit = {
            if self.explicitStop || (self.smokeReport != nil && !CommandLine.arguments.contains("--smoke-preserve-runtime")) {
                self.backend.stop { error in
                    if let error = error {
                        self.checkingQuit = false
                        self.explicitStop = false
                        self.showStatus(error.localizedDescription, failed: true)
                        NSApp.reply(toApplicationShouldTerminate: false)
                    } else { authorize() }
                }
            } else { authorize() }
        }
        let deadline = Date().addingTimeInterval(10)
        func waitForMoves() {
            // An unfinished move keeps the conversation in its source window.
            if self.conversationTransfers.isEmpty || Date() >= deadline { continueQuit() }
            else { DispatchQueue.main.asyncAfter(deadline: .now() + 0.05, execute: waitForMoves) }
        }
        waitForMoves()
        return .terminateLater
    }

    @objc private func confirmStopAndQuit() {
        let alert = NSAlert()
        alert.messageText = "Stop work and quit?"
        alert.informativeText = "This stops active agents and tools. Saved conversations remain available. Quit Ribbon Field leaves work running."
        alert.addButton(withTitle: "Keep Working")
        alert.addButton(withTitle: "Stop Work and Quit")
        if alert.runModal() == .alertSecondButtonReturn {
            explicitStop = true
            NSApp.terminate(nil)
        }
    }
    func applicationWillTerminate(_ notification: Notification) {
        if let monitor = sessionKeyMonitor { NSEvent.removeMonitor(monitor) }
        if let activity = activity { ProcessInfo.processInfo.endActivity(activity) }
        if lockFile >= 0 { flock(lockFile, LOCK_UN); close(lockFile) }
        if let name = smokePreferences, !CommandLine.arguments.contains("--smoke-preferences") { preferences.removePersistentDomain(forName: name) }
        smokePasteboard?.releaseGlobally()
        if let directory = smokeDirectory, !CommandLine.arguments.contains("--smoke-preserve-runtime") {
            try? FileManager.default.removeItem(at: directory)
        }
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

    @MainActor private func smokeJS(_ source: String, in view: WKWebView, arguments: [String: Any] = [:]) async throws -> Any {
        try await withCheckedThrowingContinuation { continuation in
            view.callAsyncJavaScript(source, arguments: arguments, in: nil, in: .page) { result in
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
        if CommandLine.arguments.contains("--smoke-window-restoration") {
            try await smokeWindowRestoration()
            finishSmoke(error: nil)
            return
        }
        if CommandLine.arguments.contains("--smoke-runtime-only") {
            smokeResults["runtime_attached"] = backend.process?.isRunning == true
            let image: NSImage = try await withCheckedThrowingContinuation { continuation in
                first.webView.takeSnapshot(with: nil) { image, error in
                    if let image = image { continuation.resume(returning: image) }
                    else { continuation.resume(throwing: error ?? Backend.failure("Screenshot unavailable.")) }
                }
            }
            if let tiff = image.tiffRepresentation, let bitmap = NSBitmapImageRep(data: tiff),
               let png = bitmap.representation(using: .png, properties: [:]) {
                try png.write(to: URL(fileURLWithPath: smokeReport!.path + ".png"))
            }
            finishSmoke(error: nil)
            return
        }
        if CommandLine.arguments.contains("--smoke-attribution-only") {
            try await smokeAttribution()
            finishSmoke(error: nil)
            return
        }
        if CommandLine.arguments.contains("--smoke-sessions-only") {
            try await smokeFocus(first)
            try await smokeSessionCycling(first)
            finishSmoke(error: nil)
            return
        }
        if CommandLine.arguments.contains("--smoke-detach-only") {
            try await smokeDetach(first)
            finishSmoke(error: nil)
            return
        }
        let bridge = try await smokeJS("""
            localStorage.setItem('agent-coord.smoke', 'persisted');
            sessionStorage.setItem('desktop-smoke-window', 'first');
            document.querySelector('#message').value = 'First window draft';
            document.querySelector('#back-home').focus();
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
        try await smokeWorkingFolders(first, second)
        if CommandLine.arguments.contains("--smoke-notifications-only") {
            try await smokeNotificationRouting(first, second)
            try await smokeProjectLinkTabs(first)
            finishSmoke(error: nil)
            return
        }
        if CommandLine.arguments.contains("--smoke-scratchpad-only") {
            _ = try await smokeJS("await switchProject(); return true;", in: second.webView)
            try await smokeFocus(second)
            try await smokeScratchpad(second, other: first)
            finishSmoke(error: nil)
            return
        }
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
                return message.value.startsWith('Second window draft\\n') && document.querySelectorAll('#sessions button[data-thread]').length === 0;
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
        try await smokeSettings(first, second)
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
            const options = Array.from(document.querySelectorAll('#desktop-command-results [role=option]'));
            const actions = options.filter(option => option.dataset.entry === 'findThread');
            if (actions.length !== 1) return false;
            input.dispatchEvent(new KeyboardEvent('keydown', {key: 'ArrowDown', bubbles: true, cancelable: true}));
            for (let i = 0; i < options.length && input.getAttribute('aria-activedescendant') !== actions[0].id; i++)
              input.dispatchEvent(new KeyboardEvent('keydown', {key: 'ArrowDown', bubbles: true, cancelable: true}));
            if (input.getAttribute('aria-activedescendant') !== actions[0].id) return false;
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
        _ = try await smokeJS("""
            const work = window.agentCoordSendWork;
            window.smokeWorkOriginal = {api: work.api, workspace: work.workspace, threads: work.threads};
            work.workspace = () => '/tmp/send-work-smoke';
            work.threads = () => [{thread_id: 'release-smoke', title: 'Release deployment', cwd: '/tmp/send-work-smoke',
              browser_session: {}, checkpoint: {summary: 'Build, verify, and deploy Ribbon Field'}}];
            work.api = async (path, body) => {
              if (path === 'send-work/suggest') return {candidates: []};
              window.smokeWorkSent = body;
              return {receipt: {id: 1, cwd: body.cwd, target_id: 'release-smoke', target_title: 'Release deployment', state: 'accepted'}};
            };
            const input = document.querySelector('#search'); input.focus(); input.value = 'Unrelated draft'; input.setSelectionRange(2, 6);
            window.smokeWorkHash = location.hash;
            return true;
            """, in: second.webView)
        try smokeCheck("send_work_shortcut", smokeKey("k", flags: [.command, .shift], code: 40))
        let sendWorkOpen = try await smokeJS("""
            for (let i = 0; i < 40 && !document.querySelector('#send-work-dialog').open; i++)
              await new Promise(resolve => setTimeout(resolve, 25));
            const input = document.querySelector('#send-work-input');
            input.value = 'Redeploy'; input.dispatchEvent(new Event('input', {bubbles: true}));
            input.dispatchEvent(new KeyboardEvent('keydown', {key: 'Tab', bubbles: true, cancelable: true}));
            return document.querySelector('#send-work-dialog').open && document.activeElement === input &&
              document.querySelector('#send-work-target-name').textContent === 'Release deployment' &&
              document.querySelectorAll('#send-work-results [role=option]').length === 3;
            """, in: second.webView)
        try smokeCheck("send_work_selection", sendWorkOpen as? Bool == true)
        let sendWorkImage: NSImage = try await withCheckedThrowingContinuation { continuation in
            second.webView.takeSnapshot(with: nil) { image, error in
                if let image = image { continuation.resume(returning: image) }
                else { continuation.resume(throwing: error ?? NSError(domain: "AgentCoordSmoke", code: 2)) }
            }
        }
        if let data = sendWorkImage.tiffRepresentation, let bitmap = NSBitmapImageRep(data: data),
           let png = bitmap.representation(using: .png, properties: [:]) {
            try png.write(to: smokeReport!.appendingPathExtension("send-work.png"))
        }
        let sendWorkSent = try await smokeJS("""
            const work = window.agentCoordSendWork;
            await work.send();
            const input = document.querySelector('#search');
            const passed = !work.dialog.open && document.activeElement === input && input.value === 'Unrelated draft' &&
              input.selectionStart === 2 && input.selectionEnd === 6 && location.hash === window.smokeWorkHash &&
              window.smokeWorkSent.message === 'Redeploy' && window.smokeWorkSent.destination.id === 'release-smoke' &&
              Object.keys(window.smokeWorkSent).sort().join(',') === 'cwd,destination,message,requestId';
            Object.assign(work, window.smokeWorkOriginal); work.notice.hidden = true; input.value = '';
            return passed;
            """, in: second.webView)
        try smokeCheck("send_work_typed_only_and_focus", sendWorkSent as? Bool == true)
        try await smokeScratchpad(second, other: first)
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
        try await smokeNotificationRouting(first, second)
        try await smokeProjectLinkTabs(first)

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

    @MainActor private func smokeScratchpad(_ item: DesktopWindow, other: DesktopWindow) async throws {
        _ = try await smokeJS("""
            const search = document.querySelector('#search');
            search.value = 'Resume here'; search.focus(); search.setSelectionRange(2, 6);
            return true;
            """, in: item.webView)
        try smokeCheck("project_scratchpad_shortcut", smokeKey("s", flags: [.command, .shift], code: 1))
        let opened = try await smokeJS("""
            for (let i = 0; i < 80; i++) {
              const editor = document.querySelector('#project-scratchpad-text');
              if (document.querySelector('#project-scratchpad-dialog').open && document.activeElement === editor && !editor.disabled) {
                editor.value = 'Remember the launch feedback';
                editor.dispatchEvent(new Event('input', {bubbles: true}));
                return true;
              }
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            return false;
            """, in: item.webView)
        let quiet = try await smokeJS("return !document.querySelector('#project-scratchpad-dialog').open;", in: other.webView)
        try smokeCheck("project_scratchpad_active_window", opened as? Bool == true && quiet as? Bool == true)
        try smokeCheck("project_scratchpad_shortcut", smokeKey("s", flags: [.command, .shift], code: 1))
        let restored = try await smokeJS("""
            for (let i = 0; i < 80; i++) {
              const search = document.querySelector('#search');
              if (!document.querySelector('#project-scratchpad-dialog').open) {
                const saved = await api('workspaces/scratchpad?path=' + encodeURIComponent(state.folders.selected.cwd));
                if (saved.text === 'Remember the launch feedback') {
                  const restored = document.activeElement === search && search.value === 'Resume here' &&
                    search.selectionStart === 2 && search.selectionEnd === 6;
                  search.value = '';
                  return restored;
                }
              }
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            return false;
            """, in: item.webView)
        try smokeCheck("project_scratchpad_saved_and_focus_restored", restored as? Bool == true)
        try smokeCheck("project_scratchpad_shortcut", smokeKey("s", flags: [.command, .shift], code: 1))
        let escaped = try await smokeJS("""
            for (let i = 0; i < 80; i++) {
              const editor = document.querySelector('#project-scratchpad-text');
              if (document.activeElement === editor && !editor.disabled) {
                editor.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true, cancelable: true}));
                return !document.querySelector('#project-scratchpad-dialog').open && document.activeElement?.id === 'search';
              }
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            return false;
            """, in: item.webView)
        try smokeCheck("project_scratchpad_escape", escaped as? Bool == true)
        let sharedTab = try await smokeJS("""
            if (document.querySelector('[data-thread-tab="project-scratchpad"]')) return false;
            document.querySelector('#open-project-scratchpad').click();
            for (let i = 0; i < 80; i++) {
              const editor = document.querySelector('#project-scratchpad-text');
              if (!document.querySelector('#project-scratchpad-page').hidden && !editor.disabled &&
                  editor.value === 'Remember the launch feedback') {
                const tab = document.querySelector('[data-thread-tab="project-scratchpad"]');
                tab.parentElement.querySelector('.conversation-tab-close').click();
                // Tab close awaits pane navigation before hiding the scratchpad.
                for (let j = 0; j < 80; j++) {
                  if (!document.querySelector('[data-thread-tab="project-scratchpad"]') &&
                      document.querySelector('#project-scratchpad-page').hidden) break;
                  await new Promise(resolve => setTimeout(resolve, 25));
                }
                if (document.querySelector('[data-thread-tab="project-scratchpad"]') ||
                    !document.querySelector('#project-scratchpad-page').hidden) return false;
                if (!window.agentCoordDesktop.command('openProjectScratchpad')) return false;
                for (let j = 0; j < 80; j++) {
                  if (!editor.disabled && editor.value === 'Remember the launch feedback') {
                    const reopened = Boolean(document.querySelector('[data-thread-tab="project-scratchpad"]')) &&
                      !document.querySelector('#project-scratchpad-page').hidden;
                    document.querySelector('[data-thread-tab=""]').click();
                    return reopened;
                  }
                  await new Promise(resolve => setTimeout(resolve, 25));
                }
                return false;
              }
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            return false;
            """, in: item.webView)
        let separate = try await smokeJS("""
            return (await api('workspaces/scratchpad?path=' + encodeURIComponent(state.folders.selected.cwd))).text === '';
            """, in: other.webView)
        try smokeCheck("project_scratchpad_tab_and_isolation", sharedTab as? Bool == true && separate as? Bool == true)
    }

    @MainActor private func smokeWorkingFolders(_ first: DesktopWindow, _ second: DesktopWindow) async throws {
        let folder = smokeDirectory!.appendingPathComponent("Working folder", isDirectory: true).resolvingSymlinksInPath()
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        let data = try JSONSerialization.data(withJSONObject: [folder.path, smokeDirectory!.resolvingSymlinksInPath().path])
        let paths = String(data: data, encoding: .utf8)!
        let opened = try await smokeJS("""
            const paths = \(paths);
            const expected = (await api('workspaces/open', {path: paths[0]})).cwd;
            const original = state.folderPicker.desktop;
            state.folderPicker.desktop = {chooseFolder: async () => paths[0]};
            try {
              await state.folderPicker.choose();
              const selected = state.folders.selected.cwd;
              const displayed = document.querySelector('#working-folder').value;
              const welcomeHidden = document.querySelector('#folder-welcome').hidden;
              const groupLabel = document.querySelector('#project').options[0].text;
              return {selected, displayed, welcomeHidden, groupLabel, expected,
                ok: selected === expected && displayed === expected && welcomeHidden && groupLabel === 'All groups'};
            } finally { state.folderPicker.desktop = original; }
            """, in: first.webView)
        smokeResults["working_folder_debug"] = opened
        try smokeCheck("working_folder_open", (opened as? [String: Any])?["ok"] as? Bool == true)
        let independent = try await smokeJS("""
            const paths = \(paths);
            if (state.folders.selected) return false;
            const folder = await state.folders.open(paths[1]); renderFolders();
            return state.folders.selected.cwd === folder.cwd && folder.cwd.endsWith(paths[1].split('/').pop());
            """, in: second.webView)
        try smokeCheck("working_folder_window_isolation", independent as? Bool == true)
        let restored = try await smokeJS("""
            const paths = \(paths);
            const expected = (await api('workspaces/open', {path: paths[0]})).cwd;
            const restored = new WorkingFolders({storage: localStorage, preferences: localStorage,
              key: window.agentCoordDesktop.folderSlot, scope: state.config.workspaceRoot || '', api});
            await restored.restore();
            return restored.selected.cwd === expected && state.folders.selected.cwd === expected;
            """, in: first.webView)
        try smokeCheck("working_folder_restored", restored as? Bool == true)
    }

    @MainActor private func smokeNotificationRouting(_ first: DesktopWindow, _ second: DesktopWindow) async throws {
        let root = smokeDirectory!.resolvingSymlinksInPath()
        let folder = root.appendingPathComponent("Working folder", isDirectory: true)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        _ = try smokePython("""
            import sys
            from agent_coord.store import CoordinationStore
            store = CoordinationStore(sys.argv[1])
            for name, cwd in [('notify-owned', sys.argv[2]), ('notify-other', sys.argv[3]), ('notify-fresh', sys.argv[3])]:
                store.register(session_id=name, client='codex', cwd=cwd)
                store.threads.update(name, title=name)
            print('{}')
            """, arguments: [folder.path, root.path])
        try await smokeInventoryReady(second, present: ["notify-owned", "notify-other", "notify-fresh"])
        let data = try JSONSerialization.data(withJSONObject: [folder.path, root.path])
        let paths = String(data: data, encoding: .utf8)!
        // The second window owns the thread's tab but currently shows another project.
        _ = try await smokeJS("""
            const paths = \(paths);
            await refreshList(); await openProjectDirectory(paths[0]); await select('notify-owned');
            const field = document.querySelector('#message');
            field.value = 'Owned notification draft'; field.dispatchEvent(new Event('input', {bubbles: true}));
            await openProjectDirectory(paths[1]); await select('notify-other');
            return true;
            """, in: second.webView)
        _ = try await smokeJS("const paths = \(paths); await refreshList(); await openProjectDirectory(paths[0]); goHome(); return true;", in: first.webView)
        func activate(_ thread: String, poster: DesktopWindow) async -> DesktopWindow? {
            await withCheckedContinuation { continuation in
                activateNotification(UUID().uuidString, info: ["desktopWindow": poster.id, "thread": thread]) {
                    continuation.resume(returning: $0)
                }
            }
        }
        let shown = """
            for (let i = 0; i < 120; i++) {
              if (state.selected === thread && !navigation.applying) break;
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            return {selected: state.selected, project: state.projectCwd, draft: document.querySelector('#message').value,
              tabs: state.threadTabs.items.filter(item => item.thread_id === thread).length};
            """
        let absent = "return state.threadTabs.has(thread) === false && state.selected !== thread;"
        // The first window is focused and posted the alert: the old destination.
        try await smokeFocus(first)
        let owner = await activate("notify-owned", poster: first)
        let owned = try await smokeJS(shown, in: second.webView, arguments: ["thread": "notify-owned"]) as? [String: Any]
        let untouched = try await smokeJS(absent, in: first.webView, arguments: ["thread": "notify-owned"])
        smokeResults["notification_owner_debug"] = owned ?? [:]
        try smokeCheck("notification_reveals_existing_tab", owner === second && current === second &&
            owned?["selected"] as? String == "notify-owned" && owned?["tabs"] as? Int == 1 &&
            (owned?["project"] as? String)?.hasSuffix("/Working folder") == true && untouched as? Bool == true)
        try smokeCheck("notification_preserves_draft", owned?["draft"] as? String == "Owned notification draft")
        try await smokeFocus(first)
        second.window.miniaturize(nil)
        for _ in 0..<80 where !second.window.isMiniaturized { try await Task.sleep(nanoseconds: 25_000_000) }
        let restored = await activate("notify-owned", poster: first)
        for _ in 0..<80 where second.window.isMiniaturized || current !== second { try await Task.sleep(nanoseconds: 25_000_000) }
        try smokeCheck("notification_restores_minimized_owner", restored === second && !second.window.isMiniaturized && current === second)
        // Without an existing tab, the most recently used main window opens it.
        try await smokeFocus(first)
        let fallback = await activate("notify-fresh", poster: second)
        let fresh = try await smokeJS(shown, in: first.webView, arguments: ["thread": "notify-fresh"]) as? [String: Any]
        let elsewhere = try await smokeJS(absent, in: second.webView, arguments: ["thread": "notify-fresh"])
        smokeResults["notification_fallback_debug"] = fresh ?? [:]
        try smokeCheck("notification_recent_window_fallback", fallback === first && current === first &&
            fresh?["selected"] as? String == "notify-fresh" && fresh?["tabs"] as? Int == 1 && elsewhere as? Bool == true)
    }

    @MainActor private func smokeProjectLinkTabs(_ item: DesktopWindow) async throws {
        let root = smokeDirectory!.resolvingSymlinksInPath()
        let folder = root.appendingPathComponent("Working folder", isDirectory: true)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        // A closed conversation is absent from the open list, so its project is
        // unknown until its detail loads: the route that left misplaced tabs.
        _ = try smokePython("""
            import sys
            from agent_coord.store import CoordinationStore
            store = CoordinationStore(sys.argv[1])
            store.register(session_id='link-elsewhere', client='codex', cwd=sys.argv[2])
            store.threads.update('link-elsewhere', title='link-elsewhere')
            print('{}')
            """, arguments: [root.path])
        try await smokeInventoryReady(item, present: ["link-elsewhere"])
        let data = try JSONSerialization.data(withJSONObject: [folder.path, root.path])
        let paths = String(data: data, encoding: .utf8)!
        // Use the backend's canonical directories; Foundation strips /private.
        let directories = "const cwd = await Promise.all(\(paths).map(async path => (await api('workspaces/open', {path})).cwd));"
        let placement = """
            {
            \(directories)
            const tabs = state.threadTabs;
            for (let i = 0; i < 120; i++) {
              if (state.selected === 'link-elsewhere' && state.projectCwd === cwd[1] && !navigation.applying) break;
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            const saved = JSON.parse(localStorage.getItem(tabs.baseKey + ':' + cwd[0]) || '{}');
            const result = {selected: state.selected, project: state.projectCwd, expected: cwd[1],
              own: tabs.items.filter(item => item.thread_id === 'link-elsewhere').length,
              other: (tabs.projects.get(cwd[0]) || []).filter(item => item.thread_id === 'link-elsewhere').length,
              saved: (saved.items || []).filter(item => item.thread_id === 'link-elsewhere').length};
            result.ok = result.selected === 'link-elsewhere' && result.project === cwd[1] &&
              result.own === 1 && result.other === 0 && result.saved === 0;
            return result;
            }
            """
        let unknown = try await smokeJS("""
            \(directories)
            await api('threads/link-elsewhere', {attention: 'archived'});
            await refreshList(); await openProjectDirectory(cwd[1]); goHome();
            state.threadTabs.items.splice(0, state.threadTabs.items.length); state.threadTabs.save(true); state.threadTabs.render();
            await openProjectDirectory(cwd[0]); goHome();
            return !state.sessions.some(thread => thread.thread_id === 'link-elsewhere');
            """, in: item.webView)
        try await smokeFocus(item)
        application(NSApp, open: [URL(string: "agentcoord://thread/link-elsewhere")!])
        let linked = try await smokeJS(placement, in: item.webView) as? [String: Any]
        smokeResults["project_link_debug"] = linked ?? [:]
        try smokeCheck("project_link_tab_in_own_project", unknown as? Bool == true && linked?["ok"] as? Bool == true)
        // A tab saved in the wrong project by an earlier version moves when clicked.
        let misplaced = try await smokeJS("""
            {
            \(directories)
            const tabs = state.threadTabs;
            tabs.items.splice(0, tabs.items.length); tabs.save(true); tabs.render();
            await openProjectDirectory(cwd[0]); goHome();
            tabs.items.push({thread_id: 'link-elsewhere', title: 'link-elsewhere'}); tabs.save(); tabs.render();
            await tabs.activate('link-elsewhere', {force: true});
            }
            """ + placement, in: item.webView) as? [String: Any]
        smokeResults["project_link_misplaced_debug"] = misplaced ?? [:]
        try smokeCheck("project_link_misplaced_tab_removed", misplaced?["ok"] as? Bool == true)
    }

    @MainActor private func smokeSettings(_ first: DesktopWindow, _ second: DesktopWindow) async throws {
        let snapshot = "return JSON.stringify([location.href, state.selected, document.querySelector('#message').value]);"
        let firstBefore = try await smokeJS(snapshot, in: first.webView) as? String
        let secondBefore = try await smokeJS(snapshot, in: second.webView) as? String
        _ = try await smokeJS("document.querySelector('#open-settings').click(); return true;", in: second.webView)
        for _ in 0..<80 {
            if windows.contains(where: { $0.isSettings && $0.loaded }) { break }
            try await Task.sleep(nanoseconds: 25_000_000)
        }
        guard let settings = windows.first(where: { $0.isSettings }) else {
            try smokeCheck("settings_opened", false); return
        }
        let ready = try await smokeJS("""
            for (let i = 0; i < 80; i++) {
              if (document.querySelector('#file-settings')?.hidden === false &&
                  document.querySelector('#file-editor-name')?.textContent) return true;
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            return false;
            """, in: settings.webView)
        try smokeCheck("settings_opened", ready as? Bool == true && settings.filesItem == nil && windows.count == 3)
        try await smokeFocus(settings)
        let shortcut = smokeKey(",", flags: [.command], code: 43)
        _ = try await smokeJS("await window.agentCoordDesktop.openSettings(); return true;", in: first.webView)
        try smokeCheck("settings_single_window", shortcut && windows.count == 3 &&
            windows.first(where: { $0.isSettings }) === settings && NSApp.keyWindow === settings.window)
        _ = try await smokeJS("""
            const theme = document.querySelector('#settings-theme');
            theme.value = 'dark'; theme.dispatchEvent(new Event('change'));
            localStorage.setItem('agent-coord.smoke-settings', 'shared');
            return true;
            """, in: settings.webView)
        var shared = true
        for conversation in [first, second] {
            let updated = try await smokeJS("""
                for (let i = 0; i < 80; i++) {
                  if (document.documentElement.dataset.theme === 'dark' &&
                      localStorage.getItem('agent-coord.theme') === 'dark' &&
                      localStorage.getItem('agent-coord.smoke-settings') === 'shared') return true;
                  await new Promise(resolve => setTimeout(resolve, 25));
                }
                return false;
                """, in: conversation.webView)
            shared = shared && updated as? Bool == true
        }
        try smokeCheck("settings_preferences_shared", shared &&
            preferences.dictionary(forKey: "webPreferences")?["agent-coord.theme"] as? String == "dark")
        let editorBefore = preferences.string(forKey: FileBrowserController.editorKey)
        _ = try await smokeJS("document.querySelector('#choose-file-editor').click(); return true;", in: settings.webView)
        for _ in 0..<60 {
            if settings.window.attachedSheet is NSOpenPanel { break }
            try await Task.sleep(nanoseconds: 25_000_000)
        }
        let panel = settings.window.attachedSheet as? NSOpenPanel
        try smokeCheck("settings_editor_picker_opens", panel?.title == "Choose File Editor" && panel?.canChooseFiles == true)
        panel?.cancel(nil)
        for _ in 0..<60 {
            if settings.window.attachedSheet == nil { break }
            try await Task.sleep(nanoseconds: 25_000_000)
        }
        try smokeCheck("settings_editor_picker_cancels", settings.window.attachedSheet == nil &&
            preferences.string(forKey: FileBrowserController.editorKey) == editorBefore)
        _ = try await smokeJS("document.querySelector('#close-settings').click(); return true;", in: settings.webView)
        for _ in 0..<60 {
            if !windows.contains(where: { $0.isSettings }) { break }
            try await Task.sleep(nanoseconds: 25_000_000)
        }
        try smokeCheck("settings_closed", windows.count == 2 && settings.window.contentView == nil)
        let firstAfter = try await smokeJS(snapshot, in: first.webView) as? String
        let secondAfter = try await smokeJS(snapshot, in: second.webView) as? String
        try smokeCheck("settings_conversations_preserved", firstBefore != nil && firstBefore == firstAfter &&
            secondBefore != nil && secondBefore == secondAfter && first.window.isVisible && second.window.isVisible &&
            backend.process?.isRunning == true)
        try await smokeFocus(second)
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
        try await smokeInventoryReady(item, present: ["files-smoke"])
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
        try await smokeInventoryReady(window, present: ["roll-old-session", "roll-new-session", "roll-later"],
                                      absent: ["files-smoke"], attention: ["roll-old-session": "now", "roll-new-session": "now", "roll-later": "later"])
        let oldest = try await smokeJS("""
            await state.rollUp.start();
            return state.selected === 'roll-new-session' && state.rollUp.queue.length === 2 &&
                !document.querySelector('#roll-up-bar').hidden;
            """, in: window.webView)
        if oldest as? Bool != true {
            smokeResults["roll_up_debug"] = try await smokeJS("""
                return {selected: state.selected, project: state.projectCwd, active: state.rollUp.active,
                  done: state.rollUp.done, busy: state.rollUp.busy,
                  queue: state.rollUp.queue.map(thread => thread.thread_id),
                  barHidden: document.querySelector('#roll-up-bar').hidden,
                  threads: (await api('threads?archived=false')).data.map(thread => ({
                    id: thread.thread_id, cwd: thread.cwd, attention: thread.attention,
                    needsAttention: thread.needs_attention, since: thread.attention_since,
                    response: thread.response_state, checkpointStale: thread.checkpoint_stale}))};
                """, in: window.webView)
        }
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

    @MainActor private func smokeInventoryReady(_ item: DesktopWindow, present: [String],
                                                absent: [String] = [], attention: [String: String] = [:]) async throws {
        _ = try await smokeJS("""
            for (let i = 0; i < 120; i++) {
              const threads = (await api('threads?archived=false')).data;
              const visible = new Map(threads.map(thread => [thread.thread_id, thread]));
              if (present.every(id => visible.has(id)) && absent.every(id => !visible.has(id)) &&
                  Object.entries(attention).every(([id, value]) => visible.get(id)?.attention === value)) return true;
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            throw new Error('Native fixture did not become visible through the thread inventory: ' + present.join(', '));
            """, in: item.webView, arguments: ["present": present, "absent": absent, "attention": attention])
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

    @MainActor private func smokeAttribution() async throws {
        let fixture = try smokePython("""
            import json, sys, uuid
            from agent_coord.store import CoordinationStore
            from agent_coord import launch_metrics
            launch_metrics.initialize(CoordinationStore(sys.argv[1]))
            print(json.dumps({'download_token': str(uuid.uuid4())}))
            """, arguments: [])
        let token = fixture["download_token"] as! String
        application(NSApp, open: [URL(string: "agentcoord://attribution?download_token=\(token)")!])
        var attributed = false
        for _ in 0..<20 {
            try await Task.sleep(nanoseconds: 100_000_000)
            let receipt = try smokePython("""
                import json, sqlite3, sys
                with sqlite3.connect(sys.argv[1]) as db:
                    token = db.execute('SELECT download_token FROM launch_install').fetchone()[0]
                print(json.dumps({'received': token == sys.argv[2]}))
                """, arguments: [token])
            if receipt["received"] as? Bool == true { attributed = true; break }
        }
        try smokeCheck("download_attribution_link", attributed)
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
            directory = str(store.database_path.parent.resolve())
            billing = store.threads.organization.create_project('Billing', cwd=directory)
            other = store.threads.organization.create_project('Other', cwd=directory)
            for name, project in [('navigation-manager', None), ('navigation-billing', billing['id']), ('navigation-other', other['id'])]:
                store.register(session_id=name, client='codex', cwd=str(store.database_path.parent))
                store.threads.update(name, title=name, project_id=project)
            view = nav.views.create({'name': 'Review', 'cwd': directory, 'filters': {'project': other['id'], 'search': 'keep'}})
            nav.bind('navigation-manager', sys.argv[2])
            output = io.StringIO()
            with patch('agent_coord.navigation.subprocess.run', return_value=subprocess.CompletedProcess([], 0, '', '')) as opener, contextlib.redirect_stdout(output):
                assert main(['--db', sys.argv[1], 'ui', 'open', '--project', 'Billing', '--from-session', 'navigation-manager', '--wait', '0']) == 0
            print(json.dumps({'url': opener.call_args.args[0][-1], 'result': json.loads(output.getvalue()),
                              'billing': billing['id'], 'other': other['id'], 'view': view['id'], 'directory': directory}))
            """, arguments: [first.id])
        try await smokeInventoryReady(first, present: ["navigation-manager", "navigation-billing", "navigation-other"])
        try await smokeAttribution()
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
            await openProjectDirectory(navigationFixture.directory);
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
              if (['all', 'group-' + navigationFixture.billing].includes(savedViews.activeId) && document.querySelector('#project').value === navigationFixture.billing && !navigation.applying) return true;
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

    @MainActor private func smokeDetach(_ first: DesktopWindow) async throws {
        _ = try smokePython("""
            import sys
            from agent_coord.store import CoordinationStore
            store = CoordinationStore(sys.argv[1])
            for name in ['detach-one', 'detach-two']:
                store.register(session_id=name, client='claude', cwd=str(store.database_path.parent))
                store.threads.update(name, title=name)
            for index in range(40):
                store.send_message(sender_session_id='detach-two', recipient_session_id='detach-one',
                    body=f'Saved message {index}\\n' + 'History preserved through detachment. ' * 25,
                    classification='informational', reply_required=False)
            print('{}')
            """)
        try await smokeInventoryReady(first, present: ["detach-one", "detach-two"])
        _ = try await smokeJS("""
            await refreshList(); await select('detach-one'); await select('detach-two');
            window.detachEvents = [];
            for (const kind of ['pointerdown', 'pointerup', 'pointermove', 'click'])
              document.addEventListener(kind, e => detachEvents.push({kind, x: e.clientX, y: e.clientY, target: e.target.outerHTML?.slice(0, 240)}), true);
            return true;
            """, in: first.webView)
        try await smokeFocus(first)
        func pointer(_ type: NSEvent.EventType, _ x: Double, _ y: Double, in item: DesktopWindow? = nil) {
            let item = item ?? first, view = item.webView
            let location = view.convert(NSPoint(x: x, y: view.isFlipped ? y : view.bounds.height - y), to: nil)
            if let event = NSEvent.mouseEvent(with: type, location: location, modifierFlags: [],
                timestamp: ProcessInfo.processInfo.systemUptime, windowNumber: item.window.windowNumber,
                context: nil, eventNumber: 0, clickCount: 1, pressure: type == .leftMouseUp ? 0 : 1) {
                item.window.sendEvent(event)
            }
        }
        var tab = try await smokeJS("const rect = document.querySelector('[data-thread-tab=\"detach-one\"]').getBoundingClientRect(); return {x: rect.x + rect.width / 2, y: rect.y + rect.height / 2};", in: first.webView) as! [String: Double]
        pointer(.leftMouseDown, tab["x"]!, tab["y"]!)
        pointer(.leftMouseUp, tab["x"]!, tab["y"]!)
        for _ in 0..<100 {
            if (try await smokeJS("return state.selected === 'detach-one' && state.detail?.work_thread.thread_id === 'detach-one' && !state.detailRefreshing;", in: first.webView)) as? Bool == true { break }
            try await Task.sleep(nanoseconds: 50_000_000)
        }
        smokeResults["detach_click_debug"] = try await smokeJS("return {selected: state.selected, events: detachEvents, width: innerWidth, height: innerHeight};", in: first.webView)
        smokeResults["detach_click_target"] = tab
        try smokeCheck("detach_normal_tab_click", (try await smokeJS("return state.selected === 'detach-one';", in: first.webView)) as? Bool == true)
        pointer(.leftMouseDown, tab["x"]!, tab["y"]!)
        pointer(.leftMouseDragged, -250, 80)
        _ = try await smokeJS("return state.threadTabs.dragging;", in: first.webView)
        _ = smokeWebKey("\u{1b}", flags: [], code: 53)
        _ = try await smokeJS("return true;", in: first.webView)
        pointer(.leftMouseUp, -250, 80)
        try await Task.sleep(nanoseconds: 200_000_000)
        let cancelled = try await smokeJS("return !state.threadTabs.dragging && state.threadTabs.items.length === 2 && !document.querySelector('.pane-drop-surface');", in: first.webView)
        try smokeCheck("detach_escape_cancelled", cancelled as? Bool == true && windows.count == 1)
        let other = try await smokeJS("const rect = document.querySelector('[data-thread-tab=\"detach-two\"]').getBoundingClientRect(); return {x: rect.right - 3, y: rect.y + rect.height / 2};", in: first.webView) as! [String: Double]
        pointer(.leftMouseDown, tab["x"]!, tab["y"]!)
        pointer(.leftMouseDragged, other["x"]!, other["y"]!)
        pointer(.leftMouseUp, other["x"]!, other["y"]!)
        try await Task.sleep(nanoseconds: 200_000_000)
        let reordered = try await smokeJS("return state.threadTabs.items.map(item => item.thread_id).join(',') === 'detach-two,detach-one';", in: first.webView)
        try smokeCheck("detach_reorder_preserved", reordered as? Bool == true && windows.count == 1)
        tab = try await smokeJS("const rect = document.querySelector('[data-thread-tab=\"detach-one\"]').getBoundingClientRect(); return {x: rect.x + rect.width / 2, y: rect.y + rect.height / 2};", in: first.webView) as! [String: Double]
        _ = try await smokeJS("""
            const input = document.querySelector('#message');
            input.value = 'Keep detached draft'; input.setSelectionRange(2, 8);
            state.drafts.set('detach-one', input.value);
            state.attachments.drafts.set('detach-one', [{name: 'pixel.png', url: 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLttAAAAABJRU5ErkJggg=='}]);
            const timeline = document.querySelector('#timeline');
            const firstMessage = timeline.querySelector('details[data-id]');
            firstMessage.open = true; rememberDisclosure(firstMessage, timelineDisclosures());
            await new Promise(resolve => requestAnimationFrame(resolve));
            state.timelineScroll.restore(260);
            return true;
            """, in: first.webView)
        pointer(.leftMouseDown, tab["x"]!, tab["y"]!)
        pointer(.leftMouseDragged, -250, 80)
        pointer(.leftMouseUp, -250, 80)
        for _ in 0..<150 {
            if windows.count == 2 && windows.last?.navigationReady == true && conversationTransfers.isEmpty,
               (try await smokeJS("return !state.threadTabs.items.some(item => item.thread_id === 'detach-one');", in: first.webView)) as? Bool == true { break }
            try await Task.sleep(nanoseconds: 100_000_000)
        }
        guard let second = windows.last, second !== first else { try smokeCheck("detach_window_created", false); return }
        try smokeCheck("detach_window_created", windows.count == 2)
        let restored = try await smokeJS("""
            const input = document.querySelector('#message');
            return state.selected === 'detach-one' && state.detail.work_thread.thread_id === 'detach-one' &&
              input.value === 'Keep detached draft' && input.selectionStart === 2 && input.selectionEnd === 8 &&
              state.attachments.items('detach-one')[0]?.name === 'pixel.png' &&
              state.threadTabs.items.length === 1 && state.threadTabs.items[0].thread_id === 'detach-one';
            """, in: second.webView)
        try smokeCheck("detach_state_restored", restored as? Bool == true)
        let reading = try await smokeJS("""
            return document.querySelectorAll('#timeline .agent-message').length === 40 &&
              document.querySelector('#timeline details[data-id]')?.open === true &&
              Math.abs(state.timelineScroll.position() - 260) < 3;
            """, in: second.webView)
        smokeResults["detach_reading_debug"] = try await smokeJS("return {scroll: state.timelineScroll.position(), messages: document.querySelectorAll('#timeline .agent-message').length, expanded: document.querySelector('#timeline details[data-id]')?.open};", in: second.webView)
        try smokeCheck("detach_history_and_reading", reading as? Bool == true)
        try smokeCheck("detach_window_focused", NSApp.keyWindow === second.window)
        let remaining = try await smokeJS("""
            return state.selected === 'detach-two' &&
              !state.threadTabs.items.some(item => item.thread_id === 'detach-one') &&
              state.threadTabs.items.some(item => item.thread_id === 'detach-two');
            """, in: first.webView)
        try smokeCheck("detach_remaining_tabs", remaining as? Bool == true)
        let inside = try await smokeJS("""
            return await webkit.messageHandlers.desktop.postMessage({action: 'detachConversation',
              payload: agentCoordConversationTransfer.capture('detach-one'), x: 100, y: 80}) === false;
            """, in: second.webView)
        try smokeCheck("detach_inside_window_ignored", inside as? Bool == true && windows.count == 2)
        let compact = try await smokeJS("""
            return agentCoordDesktop.conversationWindow && document.documentElement.classList.contains('conversation-window') &&
              getComputedStyle(document.querySelector('.sidebar')).display === 'none' &&
              getComputedStyle(document.querySelector('.workspace-bar')).display === 'none' &&
              getComputedStyle(document.querySelector('.projects-bar')).display === 'none' &&
              getComputedStyle(document.querySelector('#conversation-add-tab')).display !== 'none' &&
              document.querySelector('#conversation-tabs').getBoundingClientRect().top < 2;
            """, in: second.webView)
        try smokeCheck("detach_compact_host", compact as? Bool == true && second.filesItem == nil)
        func localPoint(_ screen: NSPoint, in item: DesktopWindow) -> NSPoint {
            let local = item.webView.convert(item.window.convertPoint(fromScreen: screen), from: nil)
            return NSPoint(x: local.x, y: item.webView.isFlipped ? local.y : item.webView.bounds.height - local.y)
        }
        func drag(_ id: String, from item: DesktopWindow, to point: NSPoint) async throws {
            try await smokeFocus(item)
            let tab = try await smokeJS("const rect = document.querySelector('[data-thread-tab=\"' + thread + '\"]').getBoundingClientRect(); return {x: rect.x + rect.width / 2, y: rect.y + rect.height / 2};", in: item.webView, arguments: ["thread": id]) as! [String: Double]
            pointer(.leftMouseDown, tab["x"]!, tab["y"]!, in: item)
            pointer(.leftMouseDragged, point.x, point.y, in: item)
            pointer(.leftMouseUp, point.x, point.y, in: item)
        }
        let outside = NSPoint(x: (NSScreen.main?.frame.maxX ?? first.window.frame.maxX) + 400, y: first.window.frame.midY)
        let oldOrigin = second.window.frame.origin
        try await drag("detach-one", from: second, to: localPoint(outside, in: second))
        try await Task.sleep(nanoseconds: 300_000_000)
        let singleton = try await smokeJS("return state.selected === 'detach-one' && state.threadTabs.items.length === 1 && document.querySelector('#message').value === 'Keep detached draft';", in: second.webView)
        try smokeCheck("detach_singleton_moves_window", singleton as? Bool == true && windows.count == 2 && second.window.frame.origin != oldOrigin)
        // Keep a visible destination point outside the original source frame.
        second.window.setFrameOrigin(NSPoint(x: first.window.frame.maxX + 40, y: first.window.frame.minY))
        let targetPoint = NSPoint(x: second.window.frame.maxX - 10, y: second.window.frame.maxY - 80)
        _ = try await smokeJS("document.querySelector('#message').value = 'Second tab draft'; return true;", in: first.webView)
        try await drag("detach-two", from: first, to: localPoint(targetPoint, in: first))
        for _ in 0..<100 {
            if (try await smokeJS("return state.threadTabs.items.length === 2 && !state.transferLocks?.size;", in: second.webView)) as? Bool == true { break }
            try await Task.sleep(nanoseconds: 50_000_000)
        }
        let added = try await smokeJS("return state.threadTabs.items.some(t => t.thread_id === 'detach-one') && state.threadTabs.items.some(t => t.thread_id === 'detach-two') && document.querySelector('#message').value === 'Second tab draft';", in: second.webView)
        try smokeCheck("detach_drop_into_existing", added as? Bool == true && windows.count == 2)
        let emptyMain = try await smokeJS("return state.selected === null && !state.threadTabs.items.length;", in: first.webView)
        try smokeCheck("detach_last_tab_overview", emptyMain as? Bool == true && first.window.isVisible)
        // An outward drag from a multi-tab host moves just that tab to a new host.
        let multiOutside = NSPoint(x: (windows.map { $0.window.frame.maxX }.max() ?? outside.x) + 400, y: outside.y)
        try await drag("detach-one", from: second, to: localPoint(multiOutside, in: second))
        for _ in 0..<100 {
            if windows.count == 3 && windows.last?.navigationReady == true && conversationTransfers.isEmpty { break }
            try await Task.sleep(nanoseconds: 50_000_000)
        }
        guard let third = windows.last, third !== second else { try smokeCheck("detach_repeat_moves_tab", false); return }
        let repeatSource = try await smokeJS("return state.threadTabs.items.map(t => t.thread_id).join(',') === 'detach-two';", in: second.webView)
        let repeatTarget = try await smokeJS("return state.threadTabs.items.map(t => t.thread_id).join(',') === 'detach-one' && document.querySelector('#message').value === 'Keep detached draft';", in: third.webView)
        try smokeCheck("detach_repeat_moves_tab", repeatSource as? Bool == true && repeatTarget as? Bool == true && windows.count == 3)
        // Dock the singleton back into the original window; its empty host closes.
        first.window.setFrameOrigin(NSPoint(x: second.window.frame.minX - first.window.frame.width - 40, y: second.window.frame.minY))
        let rootPoint = NSPoint(x: first.window.frame.minX + 100, y: first.window.frame.maxY - 80)
        try await drag("detach-one", from: third, to: localPoint(rootPoint, in: third))
        for _ in 0..<100 {
            if windows.count == 2 && conversationTransfers.isEmpty { break }
            try await Task.sleep(nanoseconds: 50_000_000)
        }
        let docked = try await smokeJS("return state.threadTabs.items.map(t => t.thread_id).join(',') === 'detach-one' && document.querySelector('#message').value === 'Keep detached draft';", in: first.webView)
        try smokeCheck("detach_dock_back_closes_empty_host", docked as? Bool == true && windows.count == 2 && !windows.contains { $0 === third })
        // Native identity, rather than the navigation URL, survives Reload.
        second.loaded = false
        second.navigationReady = false
        second.webView.reload()
        try await smokeReady(second)
        for _ in 0..<100 {
            if second.navigationReady { break }
            try await Task.sleep(nanoseconds: 50_000_000)
        }
        let reloaded = try await smokeJS("return agentCoordDesktop.conversationWindow && state.threadTabs.items.map(t => t.thread_id).join(',') === 'detach-two' && getComputedStyle(document.querySelector('.sidebar')).display === 'none';", in: second.webView)
        try smokeCheck("detach_host_survives_reload", reloaded as? Bool == true)
        try smokeCheck("detach_shared_backend", backend.process?.isRunning == true)
        second.window.close()
    }

    @MainActor private func smokeSessionCycling(_ item: DesktopWindow) async throws {
        let fixture = try smokePython("""
            import json, sys
            from agent_coord.store import CoordinationStore
            store = CoordinationStore(sys.argv[1])
            for name in ['cycling-one', 'cycling-two', 'cycling-three']:
                store.register(session_id=name, client='codex', cwd=str(store.database_path.parent))
                store.threads.update(name, title=name)
            print(json.dumps({'cwd': str(store.database_path.parent.resolve())}))
            """)
        try await smokeInventoryReady(item, present: ["cycling-one", "cycling-two", "cycling-three"])
        _ = try await smokeJS("""
            await openProjectDirectory(cyclingDirectory);
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
            """, in: item.webView, arguments: ["cyclingDirectory": fixture["cwd"]!])
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

    @MainActor private func smokeWindowRestoration() async throws {
        let flag = CommandLine.arguments.firstIndex(of: "--smoke-restore-stage")!
        let stage = CommandLine.arguments[flag + 1]
        smokeResults["restore_stage"] = stage
        if stage == "seed" {
            let firstProject = smokeDirectory!.resolvingSymlinksInPath().appendingPathComponent("Project A")
            let secondProject = smokeDirectory!.resolvingSymlinksInPath().appendingPathComponent("Project B")
            for project in [firstProject, secondProject] {
                try FileManager.default.createDirectory(at: project, withIntermediateDirectories: true)
            }
            let first = windows[0]
            let ids = try await smokeJS("""
                const result = [];
                for (const [name, cwd] of [['Restore A1', firstProject], ['Restore A2', firstProject], ['Restore B', secondProject], ['Restore detached', firstProject]]) {
                  const created = await api('sessions', {client: 'claude', name, cwd});
                  result.push(created.session.thread_id);
                }
                await refreshList(); return result;
                """, in: first.webView, arguments: ["firstProject": firstProject.path, "secondProject": secondProject.path]) as! [String]
            _ = try smokePython("""
                import sys
                from agent_coord.store import CoordinationStore
                store = CoordinationStore(sys.argv[1])
                for index in range(30):
                    store.send_message(sender_session_id=sys.argv[2], recipient_session_id=sys.argv[3],
                        body=f'Restored history {index} ' + 'Keep the saved reading position. ' * 15,
                        classification='informational', reply_required=False)
                print('{}')
                """, arguments: [ids[0], ids[3]])
            _ = try await smokeJS("""
                await select(firstID); document.querySelector('#message').value = 'background draft';
                document.querySelector('#message').dispatchEvent(new Event('input', {bubbles: true}));
                await select(secondID); document.querySelector('#message').value = 'alpha draft';
                document.querySelector('#message').dispatchEvent(new Event('input', {bubbles: true}));
                return true;
                """, in: first.webView, arguments: ["firstID": ids[0], "secondID": ids[1]])
            var route = URLComponents(url: serverURL!, resolvingAgainstBaseURL: false)!
            route.queryItems = [URLQueryItem(name: "directory", value: secondProject.path)]
            let second = makeWindow(url: route.url!)
            try await smokeReady(second)
            _ = try await smokeJS("""
                await select(thread); document.querySelector('#message').value = 'beta conversation draft';
                document.querySelector('#message').dispatchEvent(new Event('input', {bubbles: true}));
                await newSession(); document.querySelector('#message').value = 'beta new-session draft';
                document.querySelector('#message').dispatchEvent(new Event('input', {bubbles: true}));
                return true;
                """, in: second.webView, arguments: ["thread": ids[2]])
            route.queryItems = [URLQueryItem(name: "directory", value: firstProject.path), URLQueryItem(name: "detached", value: "1")]
            let third = makeWindow(url: route.url!)
            try await smokeReady(third)
            _ = try await smokeJS("""
                await select(thread);
                document.querySelector('#message').value = 'gamma detached draft';
                document.querySelector('#message').dispatchEvent(new Event('input', {bubbles: true}));
                document.querySelector('#message').setSelectionRange(2, 7);
                state.attachments.drafts.set(thread, [{name:'pixel.png', url:'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLttAAAAABJRU5ErkJggg=='}]);
                state.timelineScroll.restore(240); return true;
                """, in: third.webView, arguments: ["thread": ids[3]])
            let visible = NSScreen.main!.visibleFrame
            for (index, item) in [first, second, third].enumerated() {
                let frame = CGRect(x: visible.minX + 20 + CGFloat(index) * 70, y: visible.minY + 20 + CGFloat(index) * 40,
                    width: item.isConversationWindow ? 880 : 1000, height: 720)
                item.window.setFrame(WindowRestorationStore.usableFrame(frame, screens: [visible], minimum: item.window.minSize), display: true)
            }
            makeWindow(url: serverURL!.appendingPathComponent("settings"))
            let monitor = makeWindow(url: serverURL!.appendingPathComponent("monitor"))
            monitor.window.miniaturize(nil)
            // Record the expected state only after the minimize animation has finished.
            for _ in 0..<60 where !monitor.window.isMiniaturized { try await Task.sleep(nanoseconds: 50_000_000) }
            try smokeCheck("restore_seed_minimized_monitor", monitor.window.isMiniaturized)
            third.window.makeKeyAndOrderFront(nil)
            let expected: [[String: Any]] = [
                ["id": first.id, "threads": [ids[0], ids[1]], "selected": ids[1], "draft": "alpha draft", "project": try await smokeJS("return state.projectCwd;", in: first.webView)],
                ["id": second.id, "threads": [ids[2], "new-session"], "selected": "new-session", "draft": "beta new-session draft", "project": try await smokeJS("return state.projectCwd;", in: second.webView)],
                ["id": third.id, "threads": [ids[3]], "selected": ids[3], "draft": "gamma detached draft", "project": try await smokeJS("return state.projectCwd;", in: third.webView)],
            ]
            preferences.set(expected, forKey: "restoreSmokeExpected")
            preferences.set(second.id, forKey: "restoreSmokeClosed")
            preferences.set(windows.map { ["id": $0.id, "frame": NSStringFromRect($0.window.frame), "slot": $0.folderSlot, "minimized": $0.window.isMiniaturized] }, forKey: "restoreSmokeFrames")
            try smokeCheck("restore_seed_five_windows", windows.count == 5)
            for (index, item) in [first, second, third].enumerated() { try await smokeWindowImage(item, suffix: "before-\(index)") }
        } else if stage == "empty" {
            // Closing the final window keeps it, so relaunch restores that window.
            try smokeCheck("restore_final_closed_window_reopens", windows.count == 1 &&
                           windows[0].id == preferences.string(forKey: "restoreSmokeFinal") &&
                           windows[0].folderSlot == preferences.integer(forKey: "restoreSmokeFinalSlot"))
            try await smokeReady(windows[0])
            let tabs = try await smokeJS("return state.threadTabs.items.length;", in: windows[0].webView)
            try smokeCheck("restore_final_window_keeps_tabs", (tabs as? Int) == preferences.integer(forKey: "restoreSmokeFinalTabs"))
        } else {
            for item in windows where !item.isSettings && item.launchRoute != "/monitor" {
                try await smokeReady(item)
                for _ in 0..<200 {
                    if !item.restoringWorkspace && item.restoredWorkspace == nil { break }
                    try await Task.sleep(nanoseconds: 50_000_000)
                }
                try smokeCheck("restore_workspace_finished", !item.restoringWorkspace && item.restoredWorkspace == nil)
            }
            let closed = preferences.string(forKey: "restoreSmokeClosed")!
            let expectedCount = ["closed", "again", "lastclose"].contains(stage) ? 4 : 5
            try smokeCheck("restore_window_count", windows.count == expectedCount)
            let expected = preferences.array(forKey: "restoreSmokeExpected") as! [[String: Any]]
            for (index, record) in expected.enumerated() {
                let id = record["id"] as! String
                if expectedCount == 4 && id == closed { continue }
                guard let item = windows.first(where: { $0.id == id }) else { try smokeCheck("restore_stable_ids", false); return }
                let matched = try await smokeJS("""
                    const threads = state.threadTabs.items.map(item => item.thread_id);
                    return JSON.stringify(threads) === JSON.stringify(expectedThreads) && state.selected === expectedSelection &&
                      document.querySelector('#message').value === expectedDraft && state.projectCwd === expectedProject;
                    """, in: item.webView, arguments: ["expectedThreads": record["threads"]!, "expectedSelection": record["selected"]!,
                        "expectedDraft": record["draft"]!, "expectedProject": record["project"]!])
                smokeResults["restore_window_\(index)_debug"] = try await smokeJS("return {tabs: state.threadTabs.items.map(item => item.thread_id), selected: state.selected, draft: document.querySelector('#message').value, project: state.projectCwd, newDraft: !!state.newSessionDraft};", in: item.webView)
                smokeResults["restore_window_\(index)_expected"] = record
                try smokeCheck("restore_window_\(index)_state", matched as? Bool == true)
                if index < 2 {
                    let background = try await smokeJS("return state.drafts.get(thread) === text;", in: item.webView,
                        arguments: ["thread": (record["threads"] as! [String])[0], "text": index == 0 ? "background draft" : "beta conversation draft"])
                    try smokeCheck("restore_background_draft_\(index)", background as? Bool == true)
                }
                if index == 2 {
                    let extra = try await smokeJS("return agentCoordDesktop.conversationWindow && document.querySelector('#message').selectionStart === 2 && document.querySelector('#message').selectionEnd === 7 && state.attachments.items()[0]?.name === 'pixel.png' && Math.abs(state.timelineScroll.position() - 240) < 3;", in: item.webView)
                    try smokeCheck("restore_auxiliary_draft_image_scroll", extra as? Bool == true)
                }
                try await smokeWindowImage(item, suffix: "after-\(index)")
            }
            let frames = preferences.array(forKey: "restoreSmokeFrames") as! [[String: Any]]
            for frame in frames {
                guard let item = windows.first(where: { $0.id == frame["id"] as? String }) else { continue }
                let saved = NSRectFromString(frame["frame"] as! String)
                smokeResults["restore_frame_\(item.folderSlot)_debug"] = ["saved": frame["frame"]!, "actual": NSStringFromRect(item.window.frame)]
                try smokeCheck("restore_geometry_\(item.folderSlot)", abs(item.window.frame.minX - saved.minX) < 2 && abs(item.window.frame.minY - saved.minY) < 2 &&
                    abs(item.window.frame.width - saved.width) < 2 && abs(item.window.frame.height - saved.height) < 2 && item.folderSlot == frame["slot"] as? Int)
                smokeResults["restore_minimized_\(item.folderSlot)_debug"] = ["saved": frame["minimized"] as? Bool ?? false, "actual": item.window.isMiniaturized]
                try smokeCheck("restore_minimized_\(item.folderSlot)", item.window.isMiniaturized == frame["minimized"] as? Bool)
            }
            try smokeCheck("restore_settings_and_monitor", windows.contains(where: \.isSettings) && windows.contains { $0.launchRoute == "/monitor" })
            // macOS cooperative activation keeps a probe launched in the background
            // inactive, with no key window to compare; record that instead of guessing.
            if NSApp.isActive {
                try smokeCheck("restore_focused_window", current?.id == expected[2]["id"] as? String)
            } else {
                smokeResults["restore_focus_unverified_inactive_app"] = expected[2]["id"] as? String ?? ""
            }
            if stage == "verify", let first = windows.first(where: { $0.id == expected[0]["id"] as? String }) {
                _ = try await smokeJS("document.querySelector('#message').value = 'alpha final keystroke'; return true;", in: first.webView)
                var changed = expected
                changed[0]["draft"] = "alpha final keystroke"
                preferences.set(changed, forKey: "restoreSmokeExpected")
            }
            if stage == "close" {
                windows.first { $0.id == closed }!.window.close()
                try smokeCheck("restore_explicit_close", !windows.contains { $0.id == closed })
            }
            if stage == "closed" {
                let fresh = makeWindow()
                try await smokeReady(fresh)
                let empty = try await smokeJS("return state.threadTabs.items.length === 0 && state.selected === null;", in: fresh.webView)
                try smokeCheck("restore_new_window_does_not_adopt_closed_tabs", empty as? Bool == true)
                fresh.window.close()
            }
            if stage == "lastclose" {
                let final = windows.last { !$0.isSettings && $0.launchRoute != "/monitor" }!
                let tabs = try await smokeJS("return state.threadTabs.items.length;", in: final.webView)
                for item in Array(windows) where item !== final { item.window.close() }
                final.window.close()
                try smokeCheck("restore_released_windows_stay_closed",
                               windows.count == 1 && windows[0] === final && final.restorable)
                preferences.set(final.id, forKey: "restoreSmokeFinal")
                preferences.set(final.folderSlot, forKey: "restoreSmokeFinalSlot")
                preferences.set(tabs as? Int ?? -1, forKey: "restoreSmokeFinalTabs")
            }
        }
        smokeResults["restored_ids"] = windows.map(\.id)
        smokeResults["runtime_survives_interface_quit"] = backend.process?.isRunning == true
        if let ready = CommandLine.arguments.firstIndex(of: "--smoke-restore-ready"),
           let proceed = CommandLine.arguments.firstIndex(of: "--smoke-restore-continue") {
            try Data("ready".utf8).write(to: URL(fileURLWithPath: CommandLine.arguments[ready + 1]))
            let deadline = Date().addingTimeInterval(20)
            while !FileManager.default.fileExists(atPath: CommandLine.arguments[proceed + 1]) && Date() < deadline {
                try await Task.sleep(nanoseconds: 50_000_000)
            }
            try smokeCheck("restore_duplicate_launch_keeps_window_set", windows.count == (stage == "verify" ? 5 : 4))
        }
    }

    @MainActor private func smokeWindowImage(_ item: DesktopWindow, suffix: String) async throws {
        let image: NSImage? = try await withCheckedThrowingContinuation { continuation in
            item.webView.takeSnapshot(with: nil) { image, error in
                if let error = error { continuation.resume(throwing: error) }
                else { continuation.resume(returning: image) }
            }
        }
        if let data = image?.tiffRepresentation, let bitmap = NSBitmapImageRep(data: data),
           let png = bitmap.representation(using: .png, properties: [:]) {
            try png.write(to: URL(fileURLWithPath: smokeReport!.path + "." + suffix + ".png"))
        }
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
        // Launch Services can preserve a shell's PWD after its temporary build
        // directory disappears. Editors such as JetBrains use it instead of
        // their actual working directory, so do not propagate that stale path.
        setenv("PWD", FileManager.default.currentDirectoryPath, 1)
        let app = NSApplication.shared
        let delegate = AppDelegate()
        app.setActivationPolicy(.regular)
        app.delegate = delegate
        withExtendedLifetime(delegate) { app.run() }
    }
}
