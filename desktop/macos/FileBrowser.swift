import AppKit
import UniformTypeIdentifiers

// References: CodeEdit's ProjectNavigator and Apple's outline/split-view sample.
// See FILE_BROWSER.md. AppKit supplies selection, disclosure, accessibility,
// scrolling and keyboard navigation; file access stays in the shared backend.
final class FileBrowserNode {
    let file: WorkspaceFile
    var children: [FileBrowserNode] = []
    var loaded = false
    var loading = false
    init(_ file: WorkspaceFile) { self.file = file }
}

private final class FileOutlineView: NSOutlineView {
    var activate: (() -> Void)?
    override func keyDown(with event: NSEvent) {
        if event.keyCode == 36 { activate?() } else { super.keyDown(with: event) }
    }
    override func menu(for event: NSEvent) -> NSMenu? {
        let row = row(at: convert(event.locationInWindow, from: nil))
        if row >= 0 { selectRowIndexes(IndexSet(integer: row), byExtendingSelection: false) }
        return super.menu(for: event)
    }
}

private final class FileBrowserBackground: NSView {
    override func draw(_ dirtyRect: NSRect) {
        NSColor.windowBackgroundColor.setFill()
        dirtyRect.fill()
    }
}

@MainActor final class FileBrowserController: NSViewController, NSOutlineViewDataSource, NSOutlineViewDelegate, NSMenuItemValidation {
    private let tree = FileOutlineView()
    var outline: NSOutlineView { tree }
    private let heading = NSTextField(labelWithString: "Files")
    private let location = NSTextField(labelWithString: "Select a conversation or choose a folder")
    private let status = NSTextField(wrappingLabelWithString: "")
    private let previewTitle = NSTextField(labelWithString: "Preview")
    let previewText = NSTextView()
    private let follow = NSButton(checkboxWithTitle: "Follow conversation", target: nil, action: nil)
    private let hidden = NSButton(checkboxWithTitle: "Hidden files", target: nil, action: nil)
    private let insert = NSButton(title: "Insert Path", target: nil, action: nil)
    private let open = NSButton(title: "Open", target: nil, action: nil)
    private let editor = NSPopUpButton(frame: .zero, pullsDown: false)
    private static let editorKey = "fileBrowserEditorApplication"
    var insertPaths: (([String]) -> Void)?
    var canInsert = false { didSet { updateActions() } }
    private(set) var root = ""
    private var conversationRoot = ""
    private var provider: WorkspaceFileProvider?
    private var rootNode = FileBrowserNode(WorkspaceFile(name: "", path: "", directory: true, symlink: false))
    private var generation = UUID()
    private var previewGeneration = UUID()
    private var timer: Timer?
    private var pendingPreview = false

    override func loadView() {
        view = FileBrowserBackground()
        let title = NSTextField(labelWithString: "FILES")
        title.font = .systemFont(ofSize: 11, weight: .semibold)
        title.textColor = .secondaryLabelColor
        let choose = NSButton(title: "Choose…", target: self, action: #selector(chooseFolder))
        let refresh = NSButton(image: NSImage(systemSymbolName: "arrow.clockwise", accessibilityDescription: "Refresh files")!, target: self, action: #selector(refreshFiles))
        refresh.bezelStyle = .texturedRounded
        let spacer = NSView(); spacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
        let toolbar = NSStackView(views: [title, spacer, choose, refresh])
        toolbar.spacing = 6
        heading.font = .systemFont(ofSize: 13, weight: .semibold)
        heading.lineBreakMode = .byTruncatingMiddle
        location.font = .systemFont(ofSize: 10)
        location.textColor = .secondaryLabelColor
        location.lineBreakMode = .byTruncatingMiddle
        follow.state = .on; follow.target = self; follow.action = #selector(followChanged)
        hidden.target = self; hidden.action = #selector(hiddenChanged)
        follow.font = .systemFont(ofSize: 11); hidden.font = .systemFont(ofSize: 11)
        let options = NSStackView(views: [follow, hidden]); options.spacing = 8
        let column = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("name"))
        column.title = "Name"
        tree.addTableColumn(column); tree.outlineTableColumn = column
        tree.headerView = nil; tree.rowSizeStyle = .default; tree.style = .sourceList
        tree.rowHeight = 24; tree.indentationPerLevel = 13
        tree.dataSource = self; tree.delegate = self
        tree.target = self; tree.doubleAction = #selector(openSelected)
        tree.activate = { [weak self] in self?.openSelected() }
        tree.setAccessibilityLabel("Workspace files")
        tree.setDraggingSourceOperationMask(.copy, forLocal: false)
        let menu = NSMenu()
        for (name, action) in [("Insert Path in Conversation", #selector(insertSelected)),
                               ("Copy Path", #selector(copyPath)),
                               ("Open in Editor", #selector(openSelected)),
                               ("Reveal in Finder", #selector(revealSelected))] {
            menu.addItem(withTitle: name, action: action, keyEquivalent: "").target = self
        }
        tree.menu = menu
        let scroll = NSScrollView(); scroll.documentView = tree; scroll.hasVerticalScroller = true
        scroll.autohidesScrollers = true; scroll.drawsBackground = false
        status.font = .systemFont(ofSize: 11); status.textColor = .secondaryLabelColor
        previewTitle.font = .systemFont(ofSize: 11, weight: .medium)
        previewTitle.lineBreakMode = .byTruncatingMiddle
        previewText.isEditable = false; previewText.isSelectable = true
        previewText.font = .monospacedSystemFont(ofSize: 11, weight: .regular)
        previewText.textContainerInset = NSSize(width: 8, height: 8)
        previewText.isVerticallyResizable = true
        previewText.autoresizingMask = [.width]
        previewText.textContainer?.widthTracksTextView = true
        previewText.setAccessibilityLabel("File preview")
        let previewScroll = NSScrollView(); previewScroll.documentView = previewText
        previewScroll.hasVerticalScroller = true; previewScroll.borderType = .bezelBorder
        insert.target = self; insert.action = #selector(insertSelected)
        open.target = self; open.action = #selector(openSelected)
        let actions = NSStackView(views: [insert, open]); actions.spacing = 8
        editor.target = self; editor.action = #selector(editorChanged)
        editor.setAccessibilityLabel("File editor")
        editor.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        let editorRow = NSStackView(views: [NSTextField(labelWithString: "Editor:"), editor])
        editorRow.spacing = 6
        refreshEditor()
        NotificationCenter.default.addObserver(self, selector: #selector(refreshEditor), name: UserDefaults.didChangeNotification, object: nil)
        let stack = NSStackView(views: [toolbar, heading, location, options, scroll, status, previewTitle, previewScroll, editorRow, actions])
        stack.orientation = .vertical; stack.alignment = .leading; stack.spacing = 8
        stack.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 12),
            stack.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -12),
            stack.topAnchor.constraint(equalTo: view.topAnchor, constant: 12),
            stack.bottomAnchor.constraint(equalTo: view.bottomAnchor, constant: -12),
            scroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 110),
            previewScroll.heightAnchor.constraint(equalToConstant: 180),
        ])
        for child in [toolbar, heading, location, options, scroll, status, previewTitle, previewScroll, editorRow] {
            child.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
        }
        updateActions()
        timer = Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { [weak self] _ in
            Task { @MainActor [weak self] in
                guard let self = self, self.view.window?.isVisible == true, !self.view.isHidden else { return }
                self.refreshFiles()
            }
        }
    }
    deinit { timer?.invalidate(); NotificationCenter.default.removeObserver(self) }

    private var editorURL: URL? {
        guard let path = UserDefaults.standard.string(forKey: Self.editorKey), !path.isEmpty else { return nil }
        return URL(fileURLWithPath: path)
    }
    @objc private func refreshEditor() {
        editor.removeAllItems()
        editor.addItem(withTitle: "System Default")
        if let url = editorURL {
            editor.addItem(withTitle: FileManager.default.displayName(atPath: url.path))
            editor.lastItem?.toolTip = url.path
            editor.selectItem(at: 1)
        }
        editor.menu?.addItem(.separator())
        editor.addItem(withTitle: "Choose Application…")
        editor.lastItem?.tag = 1
    }
    @objc private func editorChanged() {
        if editor.selectedItem?.tag != 1 {
            if editor.indexOfSelectedItem == 0 { UserDefaults.standard.removeObject(forKey: Self.editorKey) }
            refreshEditor()
            return
        }
        refreshEditor()
        guard let window = view.window, window.attachedSheet == nil else { return }
        let panel = NSOpenPanel()
        panel.title = "Choose File Editor"; panel.prompt = "Use Editor"
        panel.canChooseFiles = true; panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false; panel.treatsFilePackagesAsDirectories = false
        panel.allowedContentTypes = [.applicationBundle]
        panel.directoryURL = URL(fileURLWithPath: "/Applications", isDirectory: true)
        panel.beginSheetModal(for: window) { [weak self] response in
            guard response == .OK, let url = panel.url else { return }
            UserDefaults.standard.set(url.path, forKey: Self.editorKey)
            self?.refreshEditor()
        }
    }
    private func showOpenError(_ message: String) {
        let alert = NSAlert()
        alert.messageText = "Couldn’t open file"
        alert.informativeText = message
        if let window = view.window, window.attachedSheet == nil { alert.beginSheetModal(for: window) }
        else { alert.runModal() }
    }

    func connect(_ provider: WorkspaceFileProvider) {
        self.provider = provider
        if !root.isEmpty { setRoot(root, force: true) }
    }
    func setConversationRoot(_ path: String) {
        conversationRoot = path
        if follow.state == .on { setRoot(path) }
    }
    func setRoot(_ path: String, force: Bool = false) {
        guard force || path != root else { return }
        root = path; generation = UUID(); previewGeneration = UUID(); pendingPreview = false
        rootNode = FileBrowserNode(WorkspaceFile(name: "", path: "", directory: true, symlink: false))
        tree.reloadData(); previewText.string = ""; previewTitle.stringValue = "Preview"
        heading.stringValue = path.isEmpty ? "Files" : (path as NSString).lastPathComponent
        location.stringValue = path.isEmpty ? "Select a conversation or choose a folder" : path
        location.toolTip = path
        status.stringValue = ""; updateActions()
        if !path.isEmpty { load(rootNode) }
    }
    @objc private func chooseFolder() {
        guard let window = view.window, window.attachedSheet == nil else { return }
        let panel = NSOpenPanel(); panel.canChooseFiles = false; panel.canChooseDirectories = true
        panel.prompt = "Browse Folder"
        if let url = provider?.localURL(root: root, path: ""), !root.isEmpty { panel.directoryURL = url }
        panel.beginSheetModal(for: window) { [weak self] response in
            guard response == .OK, let self = self, let url = panel.url else { return }
            self.follow.state = .off; self.setRoot(url.path)
        }
    }
    @objc private func followChanged() { if follow.state == .on { setRoot(conversationRoot) } }
    @objc private func hiddenChanged() { setRoot(root, force: true) }
    @objc func refreshFiles() {
        guard !root.isEmpty else { return }
        load(rootNode)
        for row in 0..<tree.numberOfRows {
            if let node = tree.item(atRow: row) as? FileBrowserNode, node.file.directory, tree.isItemExpanded(node) { load(node) }
        }
        previewSelection()
    }
    private func load(_ node: FileBrowserNode) {
        guard !node.loading, let provider = provider, !root.isEmpty else { return }
        node.loading = true
        let current = generation, base = root, showHidden = hidden.state == .on
        if !node.loaded { status.stringValue = "Loading…" }
        Task { [weak self] in
            do {
                let result = try await provider.list(root: base, path: node.file.path, hidden: showHidden)
                guard let self = self, self.generation == current else { return }
                node.loading = false
                let previous = Dictionary(uniqueKeysWithValues: node.children.map { ($0.file.path, $0) })
                let children = result.entries.map { file -> FileBrowserNode in
                    if let old = previous[file.path], old.file.directory == file.directory, old.file.symlink == file.symlink { return old }
                    return FileBrowserNode(file)
                }
                let changed = !node.loaded || children.map(ObjectIdentifier.init) != node.children.map(ObjectIdentifier.init)
                node.children = children; node.loaded = true
                if changed {
                    let selection = self.selected?.file.path
                    self.tree.reloadItem(node === self.rootNode ? nil : node, reloadChildren: true)
                    if let selection = selection {
                        for row in 0..<self.tree.numberOfRows where (self.tree.item(atRow: row) as? FileBrowserNode)?.file.path == selection {
                            self.tree.selectRowIndexes(IndexSet(integer: row), byExtendingSelection: false)
                        }
                    }
                }
                self.status.stringValue = result.truncated ? "Showing the first 5,000 entries." :
                    (self.rootNode.children.isEmpty ? "This folder is empty." : "")
            } catch {
                guard let self = self, self.generation == current else { return }
                node.loading = false
                self.status.stringValue = error.localizedDescription
            }
        }
    }
    var selected: FileBrowserNode? { tree.item(atRow: tree.selectedRow) as? FileBrowserNode }
    private var selectedPath: String? {
        guard let file = selected?.file else { return nil }
        // Native actions are local-only; a remote transport supplies reference paths.
        return provider?.localURL(root: root, path: file.path)?.path
    }
    private func updateActions() {
        insert.isEnabled = selectedPath != nil && canInsert
        open.isEnabled = selectedPath != nil
    }
    func outlineViewSelectionDidChange(_ notification: Notification) {
        previewGeneration = UUID(); pendingPreview = false
        previewText.string = ""; previewTitle.stringValue = selected?.file.name ?? "Preview"
        updateActions(); previewSelection()
    }
    private func previewSelection() {
        guard !pendingPreview, let file = selected?.file, !file.directory, let provider = provider else { return }
        let current = previewGeneration, base = root
        pendingPreview = true
        Task { [weak self] in
            do {
                let result = try await provider.preview(root: base, path: file.path)
                guard let self = self, self.previewGeneration == current else { return }
                self.pendingPreview = false
                if self.previewText.string != result.text { self.previewText.string = result.text }
                self.previewTitle.stringValue = file.name + (result.truncated ? " · first 128 KiB" : "")
            } catch {
                guard let self = self, self.previewGeneration == current else { return }
                self.pendingPreview = false; self.previewText.string = error.localizedDescription
            }
        }
    }
    @objc func insertSelected() { if canInsert, let path = selectedPath { insertPaths?([path]) } }
    @objc private func copyPath() {
        if let path = selectedPath { NSPasteboard.general.clearContents(); NSPasteboard.general.setString(path, forType: .string) }
    }
    @objc private func revealSelected() {
        if let path = selectedPath { NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: path)]) }
    }
    @objc private func openSelected() {
        guard let node = selected else { return }
        if node.file.directory { tree.isItemExpanded(node) ? tree.collapseItem(node) : tree.expandItem(node) }
        else if let path = selectedPath {
            let url = URL(fileURLWithPath: path)
            if let application = editorURL {
                guard FileManager.default.fileExists(atPath: application.path) else {
                    showOpenError("The selected editor is no longer at \(application.path). Choose another application from the Editor menu.")
                    return
                }
                NSWorkspace.shared.open([url], withApplicationAt: application, configuration: NSWorkspace.OpenConfiguration()) { [weak self] _, error in
                    if let error = error {
                        DispatchQueue.main.async { self?.showOpenError(error.localizedDescription) }
                    }
                }
            } else if !NSWorkspace.shared.open(url) {
                showOpenError("macOS could not open this file. Choose an application from the Editor menu.")
            }
        }
    }
    func validateMenuItem(_ menuItem: NSMenuItem) -> Bool {
        selectedPath != nil && (menuItem.action != #selector(insertSelected) || canInsert)
    }
    func outlineView(_ outlineView: NSOutlineView, numberOfChildrenOfItem item: Any?) -> Int {
        ((item as? FileBrowserNode) ?? rootNode).children.count
    }
    func outlineView(_ outlineView: NSOutlineView, child index: Int, ofItem item: Any?) -> Any {
        ((item as? FileBrowserNode) ?? rootNode).children[index]
    }
    func outlineView(_ outlineView: NSOutlineView, isItemExpandable item: Any) -> Bool {
        (item as? FileBrowserNode)?.file.directory == true
    }
    func outlineView(_ outlineView: NSOutlineView, shouldExpandItem item: Any) -> Bool {
        if let node = item as? FileBrowserNode, !node.loaded { load(node) }
        return true
    }
    func outlineView(_ outlineView: NSOutlineView, viewFor tableColumn: NSTableColumn?, item: Any) -> NSView? {
        guard let file = (item as? FileBrowserNode)?.file else { return nil }
        let id = NSUserInterfaceItemIdentifier("file")
        let cell = (tree.makeView(withIdentifier: id, owner: self) as? NSTableCellView) ?? NSTableCellView()
        if cell.textField == nil {
            cell.identifier = id
            let label = NSTextField(labelWithString: ""), icon = NSImageView()
            label.lineBreakMode = .byTruncatingMiddle; label.font = .systemFont(ofSize: 12)
            label.translatesAutoresizingMaskIntoConstraints = false; icon.translatesAutoresizingMaskIntoConstraints = false
            cell.addSubview(icon); cell.addSubview(label); cell.textField = label; cell.imageView = icon
            NSLayoutConstraint.activate([
                icon.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: 2),
                icon.centerYAnchor.constraint(equalTo: cell.centerYAnchor), icon.widthAnchor.constraint(equalToConstant: 16),
                icon.heightAnchor.constraint(equalToConstant: 16), label.leadingAnchor.constraint(equalTo: icon.trailingAnchor, constant: 6),
                label.trailingAnchor.constraint(equalTo: cell.trailingAnchor, constant: -4), label.centerYAnchor.constraint(equalTo: cell.centerYAnchor),
            ])
        }
        cell.textField?.stringValue = file.name
        cell.imageView?.image = NSImage(systemSymbolName: file.symlink ? "link" : file.directory ? "folder.fill" : "doc.text", accessibilityDescription: nil)
        cell.imageView?.contentTintColor = file.directory ? .systemBlue : .secondaryLabelColor
        cell.toolTip = file.path
        return cell
    }
    func outlineView(_ outlineView: NSOutlineView, pasteboardWriterForItem item: Any) -> NSPasteboardWriting? {
        guard let file = (item as? FileBrowserNode)?.file else { return nil }
        return provider?.localURL(root: root, path: file.path) as NSURL?
    }
}
