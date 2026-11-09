import AppKit

/// Workspace windows draw their project tabs in the title bar. The web page owns
/// the band's contents; this type places the native window controls inside it
/// and reports the width they occupy.
enum IntegratedTitlebar {
    /// Height of the shared title band, in points. The web header uses the same value.
    static let height: CGFloat = 40
    /// Distance from the top edge to the vertical center of the project tab labels.
    static let controlCenter: CGFloat = 24
    /// Space between the last window control and the first web control.
    static let controlSpacing: CGFloat = 14

    static let controlTypes: [NSWindow.ButtonType] = [.closeButton, .miniaturizeButton, .zoomButton]

    static func configure(_ window: NSWindow) {
        window.styleMask.insert(.fullSizeContentView)
        window.titlebarAppearsTransparent = true
        window.titleVisibility = .hidden
    }

    /// Moves the window controls to the band center and returns the leading
    /// width they occupy, or nil when the controls are not shown in the window.
    @discardableResult static func align(_ window: NSWindow) -> CGFloat? {
        guard !window.styleMask.contains(.fullScreen), let theme = window.contentView?.superview else { return nil }
        let buttons = controlTypes.compactMap { window.standardWindowButton($0) }
        guard let titlebar = buttons.first?.superview else { return nil }
        // Extend the titlebar views to the band height so the moved controls are not clipped.
        var chain: [NSView] = []
        var view: NSView? = titlebar
        while let current = view, current !== theme { chain.append(current); view = current.superview }
        guard view === theme else { return nil }
        for view in chain.reversed() {
            guard let parent = view.superview else { continue }
            var frame = view.frame
            frame.size.height = height
            frame.origin.y = parent.isFlipped ? 0 : parent.bounds.height - height
            if view.frame != frame { view.frame = frame }
        }
        let center = titlebar.convert(NSPoint(x: 0, y: window.frame.height - controlCenter), from: nil).y
        for button in buttons where button.superview === titlebar {
            let origin = NSPoint(x: button.frame.minX, y: (center - button.frame.height / 2).rounded())
            if button.frame.origin != origin { button.setFrameOrigin(origin) }
        }
        guard let last = buttons.last else { return nil }
        return (last.convert(last.bounds, to: nil).maxX + controlSpacing).rounded()
    }

    /// The control centers measured from the window's top edge, for verification.
    static func controlCenters(_ window: NSWindow) -> [CGFloat] {
        controlTypes.compactMap { window.standardWindowButton($0) }.map {
            window.frame.height - $0.convert($0.bounds, to: nil).midY
        }
    }

    /// Applies the user's System Settings choice for double-clicking a title bar.
    static func performDoubleClick(_ window: NSWindow) {
        switch UserDefaults.standard.string(forKey: "AppleActionOnDoubleClick") {
        case "Minimize": window.performMiniaturize(nil)
        case "None": break
        default: window.performZoom(nil)
        }
    }
}

/// A native area of the title band that moves the window like a title bar.
final class TitlebarDragView: NSView {
    override var mouseDownCanMoveWindow: Bool { false }

    override func mouseDown(with event: NSEvent) {
        guard let window = window else { return }
        if event.clickCount == 2 { IntegratedTitlebar.performDoubleClick(window) }
        else { window.performDrag(with: event) }
    }
}
