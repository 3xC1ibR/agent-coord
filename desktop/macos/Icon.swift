import AppKit

let directory = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
for (points, scale) in [(16, 1), (16, 2), (32, 1), (32, 2), (128, 1), (128, 2), (256, 1), (256, 2), (512, 1), (512, 2)] {
    let size = points * scale
    let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: size, pixelsHigh: size,
        bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
        colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: bitmap)
    let s = CGFloat(size)
    NSColor(calibratedRed: 0.13, green: 0.23, blue: 0.20, alpha: 1).setFill()
    NSBezierPath(roundedRect: NSRect(x: s * 0.06, y: s * 0.06, width: s * 0.88, height: s * 0.88),
        xRadius: s * 0.19, yRadius: s * 0.19).fill()
    let text = "a/c" as NSString
    let attributes: [NSAttributedString.Key: Any] = [
        .font: NSFont.systemFont(ofSize: s * 0.43, weight: .semibold),
        .foregroundColor: NSColor(calibratedRed: 0.97, green: 0.96, blue: 0.91, alpha: 1)
    ]
    let textSize = text.size(withAttributes: attributes)
    text.draw(at: NSPoint(x: (s - textSize.width) / 2, y: (s - textSize.height) / 2 + s * 0.03), withAttributes: attributes)
    NSColor(calibratedRed: 0.82, green: 0.65, blue: 0.35, alpha: 1).setFill()
    NSBezierPath(ovalIn: NSRect(x: s * 0.70, y: s * 0.21, width: s * 0.09, height: s * 0.09)).fill()
    NSGraphicsContext.restoreGraphicsState()
    let suffix = scale == 2 ? "@2x" : ""
    try bitmap.representation(using: .png, properties: [:])!.write(to:
        directory.appendingPathComponent("icon_\(points)x\(points)\(suffix).png"))
}
