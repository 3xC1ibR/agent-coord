import AppKit
import WebKit
import Network
import UserNotifications
import UniformTypeIdentifiers
import Darwin

final class SessionWindow: NSWindow {
    var cycleSession: ((Bool) -> Bool)?
    /// The press that a web title band drag request continues.
    private(set) var lastMouseDown: NSEvent?

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
        if event.type == .leftMouseDown { lastMouseDown = event }
        super.sendEvent(event)
    }
}

final class DesktopWindow {
    let id: String
    var folderSlot = 0
    var isSettings = false
    var isConversationWindow = false
    var integratedTitlebar = false
    var titlebarLeading: CGFloat?
    var titlebarObservers: [NSObjectProtocol] = []
    var aligningTitlebar = false
    let window: NSWindow
    let webView: WKWebView
    let status: NSStackView
    let label: NSTextField
    let spinner: NSProgressIndicator
    let retry: NSButton
    var commands = Set<String>()
    var loaded = false
    var navigationReady = false
    var documentOrigin: URL?
    var nextOrigin: URL?
    var replacement: (navigation: WKNavigation, origin: URL)?
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
    var workspaceRestoreGeneration = 0
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

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, NSMenuItemValidation,
    WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandlerWithReply, UNUserNotificationCenterDelegate {
    var windows: [DesktopWindow] = []
    struct ConversationTransfer {
        weak var source: DesktopWindow?
        weak var target: DesktopWindow?
        let created: Bool
        var finishing = false
    }
    var conversationTransfers: [String: ConversationTransfer] = [:]
    private var activations = 0
    // The permanent loopback origin keeps IndexedDB drafts across interface
    // exits. The window ID scopes composers within this persistent store.
    private let websiteDataStore = WKWebsiteDataStore.default()
    var current: DesktopWindow? {
        windows.first { $0.window === NSApp.keyWindow } ??
        windows.first { $0.window === NSApp.mainWindow } ?? windows.last { !$0.isSettings } ?? windows.last
    }
    var window: NSWindow! { current?.window }
    var webView: WKWebView! { current?.webView }
    var backend: Backend!
    private var updater: RibbonUpdater!
    private var explicitStop = false
    var serverURL: URL?
    var pendingAppLinks: [URL] = []
    var configuration: BackendConfiguration!
    var resources: URL!
    var quitting = false
    private var checkingQuit = false
    private var lockFile: Int32 = -1
    private var activity: NSObjectProtocol?
    var preferences = UserDefaults.standard
    #if RIBBON_SMOKE_HARNESS
    var smoke = SmokeHarness()
    #endif
    var smokeTesting: Bool {
        #if RIBBON_SMOKE_HARNESS
        return smoke.report != nil
        #else
        return false
        #endif
    }
    private var databasePath: String!
    private var sessionKeyMonitor: Any?
    private var windowStore: WindowRestorationStore?
    private var restoringWindows = false
    private var nextFolderSlot = 0
    private var windowSaveScheduled = false
    private let windowPersistenceQueue = DispatchQueue(label: "com.agentcoord.desktop.windows", qos: .utility)
    private var windowOriginListener: NWListener?

    // Only a release observer launching the native app requests this local socket.
    // Each connection reads the committed WebKit documents on the main thread.
    private func startWindowOriginObservation() {
        let arguments = CommandLine.arguments
        guard let flag = arguments.firstIndex(of: "--observe-window-origins"),
              flag + 1 < arguments.count else { return }
        let path = arguments[flag + 1]
        let parent = URL(fileURLWithPath: path).deletingLastPathComponent()
        var info = stat()
        guard path.hasPrefix("/"), path.utf8.count < MemoryLayout.size(ofValue: sockaddr_un().sun_path),
              lstat(parent.path, &info) == 0, info.st_uid == geteuid(),
              (info.st_mode & S_IFMT) == S_IFDIR, (info.st_mode & 0o777) == 0o700,
              lstat(path, &info) != 0, errno == ENOENT else { return }
        let parameters = NWParameters.tcp
        parameters.requiredLocalEndpoint = .unix(path: path)
        guard let listener = try? NWListener(using: parameters) else { return }
        listener.newConnectionHandler = { [weak self] connection in
            guard let self = self else { connection.cancel(); return }
            let observations = self.windows.map { item -> [String: Any] in
                let url = item.webView.url
                let origin: String? = url.flatMap { address in
                    guard address.scheme == "http", let host = address.host,
                          ["localhost", "127.0.0.1"].contains(host), let port = address.port,
                          address.user == nil, address.password == nil else { return nil }
                    return "http://\(host):\(port)/"
                }
                let ready = origin != nil && (item.isSettings || url?.path != "/" || item.navigationReady) &&
                    item.loaded && !item.webView.isHidden && !item.webView.isLoading &&
                    item.nextOrigin == nil && item.replacement == nil && !item.restoringWorkspace &&
                    url.map { self.isLocal($0, at: item.documentOrigin) } == true && item.documentOrigin != nil
                return ["id": item.id, "origin": origin as Any, "ready": ready]
            }
            guard let bytes = try? JSONSerialization.data(withJSONObject: ["windows": observations]) else {
                connection.cancel()
                return
            }
            connection.start(queue: .main)
            connection.send(content: bytes, contentContext: .finalMessage, isComplete: true,
                            completion: .contentProcessed { _ in connection.cancel() })
        }
        windowOriginListener = listener
        listener.start(queue: .main)
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        do {
            resources = Bundle.main.resourceURL!
            configuration = try JSONDecoder().decode(BackendConfiguration.self,
                from: Data(contentsOf: resources.appendingPathComponent("backend.json")))
            var directory = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
                .appendingPathComponent("Agent Coord", isDirectory: true)
            #if RIBBON_SMOKE_HARNESS
            guard configureSmoke(directory: &directory) else { NSApp.terminate(nil); return }
            #endif
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
            startWindowOriginObservation()
            updater = RibbonUpdater(compatibility: { [weak self] target in
                guard let backend = self?.backend else { throw Backend.failure("Runtime unavailable.") }
                try backend.validateCompatibility(target)
            }, preflight: { [weak self] target, completion in
                guard let backend = self?.backend else { completion(Backend.failure("Runtime unavailable.")); return }
                backend.preflight(target, completion: completion)
            })
            updater.backendUpdateAvailable = { [weak self] in
                self?.backend.supportsRuntimeUpdates == true && self?.backend.pendingUpdate != nil
            }
            updater.backendUpdateInProgress = { [weak self] in self?.backend.updateInProgress == true }
            updater.backendUpdateIdentifier = { [weak self] in self?.backend.pendingUpdate }
            updater.backendUpdateVersion = { [weak self] in self?.backend.pendingUpdateVersion }
            updater.backendUpdateRequiresConfirmation = { [weak self] in self?.backend.pendingUpdateRequiresConfirmation == true }
            updater.backendUpdateContinuity = { [weak self] in
                guard let backend = self?.backend else { return .restartsWork }
                return backend.continuesInterruptedProviders ? .pausesOtherProviders
                    : backend.supportsUninterruptedUpdates ? .waitsForOtherProviders : .restartsWork
            }
            updater.applyBackendUpdate = { [weak self] completion in
                guard let backend = self?.backend else { completion(Backend.failure("Runtime unavailable.")); return }
                backend.applyUpdate(completion: completion)
            }
            updater.cancelBackendAuthorization = { [weak self] in self?.backend.cancelUpdateAuthorization() }
            backend.updateAvailable = { [weak self] in
                guard let self = self, !self.smokeTesting, !self.quitting, NSApp.isActive else { return }
                self.updater.notifyBackendUpdate()
            }
            backend.updateStatus = { [weak self] message, failed in
                guard let self = self else { return }
                self.updater.backendUpdateNotice = message
                if failed { NSApp.presentError(Backend.failure(message)) }
                else if self.backend.updateInProgress && self.backend.interactionURL == nil { self.showStatus(message, failed: false) }
            }
            makeMenu()
            sessionKeyMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { event in
                let target = (event.window ?? NSApp.keyWindow) as? SessionWindow
                return target?.routeSessionShortcut(event) == true ? nil : event
            }
            databasePath = configuration.databasePath
            #if RIBBON_SMOKE_HARNESS
            if let directory = smoke.directory { databasePath = directory.appendingPathComponent("state.sqlite3").path }
            #endif
            windowStore = WindowRestorationStore(url: directory.appendingPathComponent("windows/state.json"), database: databasePath)
            restoreWindows()
            #if RIBBON_SMOKE_HARNESS
            if smokeTesting { try prepareOfflineSmokeFixtures() }
            #endif
            UNUserNotificationCenter.current().delegate = self
            activity = ProcessInfo.processInfo.beginActivity(options: .userInitiatedAllowingIdleSystemSleep,
                reason: "Receive Ribbon Field turn completions while the window is closed")
            backend.ready = { [weak self] url in
                guard let self = self else { return }
                self.serverURL = url
                let links = self.pendingAppLinks
                self.pendingAppLinks.removeAll()
                for link in links { self.openAppLink(link) }
                for item in self.windows { self.moveWindow(item, to: url) }
            }
            backend.exited = { [weak self] message in
                guard let self = self, !self.quitting else { return }
                self.serverURL = nil
                self.showStatus(message, failed: true)
                #if RIBBON_SMOKE_HARNESS
                if self.smokeTesting { self.finishSmoke(error: message) }
                #endif
            }
            startBackend()
            if !smokeTesting { updater.start() }
            let links = pendingAppLinks
            pendingAppLinks.removeAll()
            for link in links { openAppLink(link) }
            #if RIBBON_SMOKE_HARNESS
            scheduleSmokeTimeout()
            #endif
        } catch {
            #if RIBBON_SMOKE_HARNESS
            if smokeTesting { finishSmoke(error: error.localizedDescription); return }
            #endif
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

    @discardableResult func makeWindow(url: URL? = nil, restoring saved: RestoredWindow? = nil) -> DesktopWindow {
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
        // Workspace windows show their project tabs in the title bar.
        if !settings && !conversation { IntegratedTitlebar.configure(window) }
        if saved == nil && !smokeTesting && (settings || windows.isEmpty) { window.setFrameAutosaveName(settings ? "AgentCoordSettings" : "AgentCoordWorkspace") }
        if settings {
            if smokeTesting || !window.setFrameUsingName("AgentCoordSettings") { window.center() }
        } else if let previous = previous, !conversation {
            window.setFrame(previous.frame.offsetBy(dx: 24, dy: -24), display: false)
        } else if smokeTesting || !window.setFrameUsingName("AgentCoordWorkspace") { window.center() }
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
            item.files.titlebarInset = IntegratedTitlebar.height
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
        if !settings && !conversation { installTitlebar(item) }
        if let destination = url ?? serverURL.map({ self.destination(for: item, base: $0) }) {
            item.documentOrigin = serverURL
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

    func item(for view: WKWebView) -> DesktopWindow? { windows.first { $0.webView === view } }

    /// Keeps the window controls inside the web title band. AppKit relayouts the
    /// titlebar after resizing and full-screen changes, so frame changes realign it.
    private func installTitlebar(_ item: DesktopWindow) {
        item.integratedTitlebar = true
        alignTitlebar(item)
        let views = IntegratedTitlebar.controlTypes.compactMap { item.window.standardWindowButton($0) }
            + [item.window.standardWindowButton(.closeButton)?.superview].compactMap { $0 }
        for view in views {
            view.postsFrameChangedNotifications = true
            item.titlebarObservers.append(NotificationCenter.default.addObserver(
                forName: NSView.frameDidChangeNotification, object: view, queue: .main) { [weak self, weak item] _ in
                guard let self = self, let item = item else { return }
                self.alignTitlebar(item)
            })
        }
    }

    private func alignTitlebar(_ item: DesktopWindow) {
        guard item.integratedTitlebar, !item.aligningTitlebar else { return }
        item.aligningTitlebar = true
        let leading = IntegratedTitlebar.align(item.window)
        item.aligningTitlebar = false
        guard leading != item.titlebarLeading else { return }
        item.titlebarLeading = leading
        if item.loaded { pushTitlebarState(item) }
    }

    private func pushTitlebarState(_ item: DesktopWindow) {
        guard let state = titlebarState(item) else { return }
        item.webView.callAsyncJavaScript("window.agentCoordDesktop?.titlebar?.(state)",
            arguments: ["state": state], in: nil, in: .page) { _ in }
    }

    private func titlebarState(_ item: DesktopWindow) -> [String: Any]? {
        guard item.integratedTitlebar else { return nil }
        return ["height": IntegratedTitlebar.height, "leading": item.titlebarLeading ?? 0,
                "controls": item.titlebarLeading != nil]
    }

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

    func destination(for item: DesktopWindow, base: URL) -> URL {
        URL(string: item.launchRoute, relativeTo: base)?.absoluteURL ?? base
    }

    private func moveWindow(_ item: DesktopWindow, to origin: URL) {
        if !item.isSettings { item.files.connect(BackendWorkspaceFiles(baseURL: origin, local: true)) }
        // The main workspace and detached conversations expose the workspace bridge.
        // Other routes (Settings, Usage, Activity, previews and Monitor) load their own pages.
        guard item.documentOrigin != nil, item.webView.url?.path == "/", !item.isSettings else {
            item.documentOrigin = origin
            configureScripts(item.webView)
            item.webView.load(URLRequest(url: destination(for: item, base: origin)))
            return
        }
        guard item.nextOrigin != origin else { return }
        item.nextOrigin = origin
        if item.replacement != nil { item.webView.stopLoading() }
        prepareOriginChange(in: item)
    }

    private func retryOriginChange(in item: DesktopWindow) {
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) { [weak self, weak item] in
            guard let self = self, let item = item else { return }
            self.prepareOriginChange(in: item)
        }
    }

    private let requireMovableDocument = """
        const bridge = window.agentCoordDesktop;
        if (bridge?.windowStateBusy?.() !== false || document.querySelector('dialog[open]') ||
            (typeof state === 'object' && (state.busy || state.transferLocks?.size))) return null;
        for (const pane of window.agentCoordPanes?.records?.values() || [])
            if (pane.frame?.contentDocument?.querySelector('dialog[open]')) return null;
        """

    private func canMoveWorkspace(_ item: DesktopWindow) -> Bool {
        item.navigationReady && !item.restoringWorkspace && !item.navigating && item.window.attachedSheet == nil &&
            !conversationTransfers.values.contains { $0.source === item || $0.target === item }
    }

    private func resumeOriginalDocument(in item: DesktopWindow, navigation: WKNavigation) {
        guard item.replacement?.navigation === navigation else { return }
        item.replacement = nil
        item.webView.evaluateJavaScript("document.documentElement.inert = false")
        retryOriginChange(in: item)
    }

    private func prepareOriginChange(in item: DesktopWindow) {
        guard !quitting, windows.contains(where: { $0 === item }), let origin = item.nextOrigin,
              origin == serverURL, item.replacement == nil else { return }
        guard canMoveWorkspace(item) else {
            retryOriginChange(in: item)
            return
        }
        item.webView.callAsyncJavaScript(requireMovableDocument + "\nreturn true;",
            arguments: [:], in: nil, in: .page) { [weak self, weak item] result in
            guard let self = self, let item = item, item.nextOrigin == origin, item.replacement == nil,
                  self.windows.contains(where: { $0 === item }), !self.quitting else { return }
            if case .success(let ready) = result, ready as? Bool == true {
                // The original document remains interactive while WebKit fetches the destination.
                if let navigation = item.webView.load(URLRequest(url: self.destination(for: item, base: origin))) {
                    item.replacement = (navigation, origin)
                } else { self.retryOriginChange(in: item) }
            } else { self.retryOriginChange(in: item) }
        }
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
        let durableState = state.filter { $0.key != "originTransfer" }
        guard JSONSerialization.isValidJSONObject(durableState) else { completion?(); return }
        item.workspaceVersion += 1
        let version = item.workspaceVersion
        if let route = state["route"] as? String, WindowRestorationStore.validRoute(route), let url = URL(string: route) {
            item.launchRoute = localRoute(url)
        }
        windowPersistenceQueue.async { [weak self, weak item] in
            let json = (try? JSONSerialization.data(withJSONObject: durableState, options: [.sortedKeys]))
                .flatMap { String(data: $0, encoding: .utf8) }
            DispatchQueue.main.async {
                if let item = item, let json = json, item.workspaceVersion == version {
                    let firstSnapshot = item.workspaceJSON == nil
                    item.workspaceJSON = json
                    if firstSnapshot { self?.saveWindowSet() }
                    else { self?.scheduleWindowSave() }
                }
                completion?()
            }
        }
    }

    private func restoreWorkspace(in item: DesktopWindow, completion: (() -> Void)? = nil) {
        guard !item.restoringWorkspace else { return }
        guard let snapshot = item.restoredWorkspace else { completion?(); return }
        item.restoringWorkspace = true
        item.restorationAttempted = true
        item.workspaceRestoreGeneration += 1
        let generation = item.workspaceRestoreGeneration
        var finished = false
        let finish: (Bool) -> Void = { [weak self, weak item] restored in
            guard !finished, let item = item, item.workspaceRestoreGeneration == generation else { return }
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
    func windowDidResize(_ notification: Notification) {
        if let item = windows.first(where: { $0.window === notification.object as? NSWindow }) { alignTitlebar(item) }
        scheduleWindowSave()
    }
    func windowDidEnterFullScreen(_ notification: Notification) { windowDidResize(notification) }
    func windowDidExitFullScreen(_ notification: Notification) { windowDidResize(notification) }
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
        item.titlebarObservers.forEach(NotificationCenter.default.removeObserver)
        item.titlebarObservers = []
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

    @objc func showWindow() {
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
    @objc private func openBrowser() {
        if let url = webView.url, let currentURL = currentBackendURL(for: url, from: current) { NSWorkspace.shared.open(currentURL) }
    }
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
                database: smokeTesting ? databasePath : nil)
        } catch {
            showStatus("Could not launch Python. Rebuild the app if Python moved.\n\n" + error.localizedDescription, failed: true)
            #if RIBBON_SMOKE_HARNESS
            if smokeTesting { finishSmoke(error: error.localizedDescription) }
            #endif
        }
    }

    private func isLocal(_ url: URL, at origin: URL? = nil) -> Bool {
        guard serverURL != nil, let base = origin ?? serverURL else { return false }
        return url.scheme == base.scheme && url.host == base.host && url.port == base.port && url.user == nil && url.password == nil
    }

    private func currentBackendURL(for url: URL, from item: DesktopWindow?) -> URL? {
        guard isLocal(url, at: item?.documentOrigin), let serverURL = serverURL,
              var parts = URLComponents(url: url, resolvingAgainstBaseURL: false) else { return nil }
        parts.host = serverURL.host
        parts.port = serverURL.port
        return parts.url
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
        #if RIBBON_SMOKE_HARNESS
        if let item = item(for: webView) { addRestorationSmokeScript(to: controller, item: item) }
        #endif
        if let composer = try? String(contentsOf: resources.appendingPathComponent("backend/agent_coord/web/composer-drafts.js"), encoding: .utf8) {
            controller.addUserScript(WKUserScript(source: composer, injectionTime: .atDocumentStart, forMainFrameOnly: true))
        }
        controller.addUserScript(WKUserScript(source: palette, injectionTime: .atDocumentStart, forMainFrameOnly: true))
        controller.addUserScript(WKUserScript(source: workspace, injectionTime: .atDocumentStart, forMainFrameOnly: true))
        let bridge = WindowRestorationStore.script(source, values: [
            "__AGENT_COORD_PREFERENCES__": json, "__AGENT_COORD_WINDOW_STATE__": restoration,
            "__AGENT_COORD_FOLDER_SLOT__": String(item(for: webView)?.folderSlot ?? 0),
            "__AGENT_COORD_CONVERSATION_WINDOW__": item(for: webView)?.isConversationWindow == true ? "true" : "false",
            "__AGENT_COORD_TITLEBAR__": item(for: webView).flatMap(titlebarState)
                .flatMap { try? JSONSerialization.data(withJSONObject: $0) }
                .flatMap { String(data: $0, encoding: .utf8) } ?? "null",
            "__AGENT_COORD_WINDOW_ID__": item(for: webView)?.id ?? "",
        ])
        controller.addUserScript(WKUserScript(source: bridge,
            injectionTime: .atDocumentStart, forMainFrameOnly: true))
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url else { decisionHandler(.cancel); return }
        if isLocal(url) || isLocal(url, at: item(for: webView)?.documentOrigin) {
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

    func webView(_ webView: WKWebView, decidePolicyFor navigationResponse: WKNavigationResponse,
                 decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        guard navigationResponse.isForMainFrame, let item = item(for: webView),
              let replacement = item.replacement else { decisionHandler(.allow); return }
        guard let origin = item.nextOrigin, origin == replacement.origin, let url = navigationResponse.response.url,
              isLocal(url, at: origin), canMoveWorkspace(item) else {
            decisionHandler(.cancel)
            resumeOriginalDocument(in: item, navigation: replacement.navigation)
            return
        }
        // Capture at response arrival, so typing during the destination fetch is included.
        // Recheck acceptance in this same JS turn. Lock input only while admitting the new document.
        webView.callAsyncJavaScript(requireMovableDocument + "\n" + """
            const snapshot = bridge.exportWindowState();
            if (snapshot) document.documentElement.inert = true;
            return snapshot;
            """, arguments: [:], in: nil, in: .page) { [weak self, weak item] result in
                guard let self = self, let item = item, !self.quitting, item.nextOrigin == origin,
                      self.windows.contains(where: { $0 === item }), self.canMoveWorkspace(item),
                      item.replacement?.navigation === replacement.navigation,
                      case .success(let value) = result, let snapshot = value as? [String: Any] else {
                    decisionHandler(.cancel)
                    if let item = item { self?.resumeOriginalDocument(in: item, navigation: replacement.navigation) }
                    return
                }
                var transfer = snapshot
                transfer["originTransfer"] = true
                item.restoredWorkspace = transfer
                item.restorationAttempted = false
                self.acceptWorkspaceState(snapshot, for: item)
                self.configureScripts(webView)
                decisionHandler(.allow)
            }
    }

    func webView(_ webView: WKWebView, didCommit navigation: WKNavigation!) {
        guard let item = item(for: webView) else { return }
        item.navigationReady = false
        if let replacement = item.replacement, replacement.navigation === navigation {
            item.documentOrigin = replacement.origin
            item.replacement = nil
            if item.nextOrigin == replacement.origin { item.nextOrigin = nil }
            else { retryOriginChange(in: item) }
        }
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = navigationAction.request.url {
            if let currentURL = currentBackendURL(for: url, from: item(for: webView)) { makeWindow(url: currentURL) }
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
        pushTitlebarState(item)
        webView.isHidden = false
        #if RIBBON_SMOKE_HARNESS
        if smokeTesting { checkSmoke(attempt: 0) }
        #endif
    }
    func webView(_ webView: WKWebView, didStartProvisionalNavigation navigation: WKNavigation!) {
        if let item = item(for: webView), item.replacement?.navigation !== navigation { item.navigationReady = false }
    }
    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        if let item = item(for: webView), let replacement = item.replacement, replacement.navigation === navigation {
            resumeOriginalDocument(in: item, navigation: replacement.navigation)
            return
        }
        if (error as NSError).code != NSURLErrorCancelled { showStatus(error.localizedDescription, failed: true, in: item(for: webView)) }
    }
    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        if (error as NSError).code != NSURLErrorCancelled { showStatus(error.localizedDescription, failed: true, in: item(for: webView)) }
    }
    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        guard !quitting, let item = item(for: webView) else { return }
        item.workspaceRestoreGeneration += 1
        item.restoringWorkspace = false
        item.restorationAttempted = false
        if item.restoredWorkspace == nil, let json = item.workspaceJSON {
            item.restoredWorkspace = (try? JSONSerialization.jsonObject(with: Data(json.utf8))) as? [String: Any]
        }
        webView.reload()
    }

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
        guard message.frameInfo.isMainFrame, let url = message.frameInfo.request.url,
              let source = message.webView, let owner = item(for: source), isLocal(url, at: owner.documentOrigin),
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
                  let link = URL(string: value, relativeTo: owner.documentOrigin ?? serverURL)?.absoluteURL,
                  let destination = currentBackendURL(for: link, from: owner) else {
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
            #if RIBBON_SMOKE_HARNESS
            let pasteboard = smoke.pasteboard ?? NSPasteboard(name: .drag)
            #else
            let pasteboard = NSPasteboard(name: .drag)
            #endif
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
        case "windowDrag":
            // The page reports a press on empty title band space; continue it as a window drag.
            guard owner.integratedTitlebar else { replyHandler(false, nil); return }
            #if RIBBON_SMOKE_HARNESS
            smoke.results["titlebar_drag_requests"] = (smoke.results["titlebar_drag_requests"] as? Int ?? 0) + 1
            #endif
            guard let window = owner.window as? SessionWindow, let press = window.lastMouseDown,
                  NSEvent.pressedMouseButtons & 1 == 1 else { replyHandler(false, nil); return }
            window.performDrag(with: press)
            // AppKit's drag loop consumes the release; finish WebKit's press so the page stops tracking it.
            if let release = NSEvent.mouseEvent(with: .leftMouseUp, location: press.locationInWindow, modifierFlags: [],
                    timestamp: ProcessInfo.processInfo.systemUptime, windowNumber: window.windowNumber, context: nil,
                    eventNumber: press.eventNumber, clickCount: press.clickCount, pressure: 0) {
                owner.webView.mouseUp(with: release)
            }
            replyHandler(true, nil)
        case "windowDoubleClick":
            guard owner.integratedTitlebar else { replyHandler(false, nil); return }
            #if RIBBON_SMOKE_HARNESS
            smoke.results["titlebar_double_click_requests"] = (smoke.results["titlebar_double_click_requests"] as? Int ?? 0) + 1
            #endif
            if !smokeTesting { IntegratedTitlebar.performDoubleClick(owner.window) }
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

    func activateNotification(_ id: String, info: [AnyHashable: Any], completion: ((DesktopWindow?) -> Void)? = nil) {
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
                    self.checkingQuit = false
                    self.quitting = true
                    for item in self.windows { item.window.orderOut(nil) }
                    self.backend.detach()
                    // Release before Sparkle launches the replacement desktop.
                    if self.lockFile >= 0 { flock(self.lockFile, LOCK_UN); close(self.lockFile); self.lockFile = -1 }
                    NSApp.reply(toApplicationShouldTerminate: true)
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
        var stopRuntime = explicitStop
        #if RIBBON_SMOKE_HARNESS
        stopRuntime = stopRuntime || (smokeTesting && !smoke.preservesRuntime)
        #endif
        guard stopRuntime else {
            // Drafts are written while editing. A transfer that has not
            // committed keeps its source window in the last saved window set.
            authorize()
            return .terminateLater
        }
        let deadline = Date().addingTimeInterval(10)
        func waitForMoves() {
            if !self.conversationTransfers.isEmpty, Date() < deadline {
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.05, execute: waitForMoves)
                return
            }
            self.backend.stop { error in
                if let error = error {
                    self.checkingQuit = false
                    self.explicitStop = false
                    self.showStatus(error.localizedDescription, failed: true)
                    NSApp.reply(toApplicationShouldTerminate: false)
                } else { authorize() }
            }
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
        #if RIBBON_SMOKE_HARNESS
        recordSmokeQuit()
        #endif
        windowOriginListener?.cancel()
        if let monitor = sessionKeyMonitor { NSEvent.removeMonitor(monitor) }
        if let activity = activity { ProcessInfo.processInfo.endActivity(activity) }
        if lockFile >= 0 { flock(lockFile, LOCK_UN); close(lockFile) }
        #if RIBBON_SMOKE_HARNESS
        cleanUpSmoke()
        #endif
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
