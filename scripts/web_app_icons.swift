// Regenerate the committed web icons on macOS:
// xcrun swift scripts/web_app_icons.swift
// Matches desktop/macos/Icon.swift, with an opaque, full-bleed background
// so iOS and maskable-icon launchers can apply their own shape.
import AppKit

let directory = URL(fileURLWithPath: "plugins/agent-coord/scripts/agent_coord/web/app-icons", isDirectory: true)
try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
for size in [180, 192, 512] {
    let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: size, pixelsHigh: size,
        bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
        colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: bitmap)
    let s = CGFloat(size)
    NSColor(calibratedRed: 0.13, green: 0.23, blue: 0.20, alpha: 1).setFill()
    NSRect(x: 0, y: 0, width: s, height: s).fill()
    let text = "rf" as NSString
    let attributes: [NSAttributedString.Key: Any] = [
        .font: NSFont.systemFont(ofSize: s * 0.43, weight: .semibold),
        .foregroundColor: NSColor(calibratedRed: 0.97, green: 0.96, blue: 0.91, alpha: 1)
    ]
    let textSize = text.size(withAttributes: attributes)
    text.draw(at: NSPoint(x: (s - textSize.width) / 2, y: (s - textSize.height) / 2 + s * 0.03), withAttributes: attributes)
    NSColor(calibratedRed: 0.82, green: 0.65, blue: 0.35, alpha: 1).setFill()
    NSBezierPath(ovalIn: NSRect(x: s * 0.70, y: s * 0.21, width: s * 0.09, height: s * 0.09)).fill()
    NSGraphicsContext.restoreGraphicsState()
    let name = size == 180 ? "apple-touch-icon.png" : "icon-\(size).png"
    try bitmap.representation(using: .png, properties: [:])!.write(to: directory.appendingPathComponent(name))
}
