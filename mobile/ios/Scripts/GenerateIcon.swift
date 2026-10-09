// Adapted from desktop/macos/Icon.swift. iOS applies its own mask; source is opaque.
import AppKit
import ImageIO
import UniformTypeIdentifiers
let output = URL(fileURLWithPath: CommandLine.arguments[1])
let size = 1024
let context = CGContext(data: nil, width: size, height: size, bitsPerComponent: 8,
    bytesPerRow: size * 4, space: CGColorSpaceCreateDeviceRGB(),
    bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue)!
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(cgContext: context, flipped: false)
let s = CGFloat(size)
NSColor(calibratedRed: 0.13, green: 0.23, blue: 0.20, alpha: 1).setFill()
NSBezierPath(rect: NSRect(x: 0, y: 0, width: s, height: s)).fill()
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
let destination = CGImageDestinationCreateWithURL(output as CFURL, UTType.png.identifier as CFString, 1, nil)!
CGImageDestinationAddImage(destination, context.makeImage()!, nil)
guard CGImageDestinationFinalize(destination) else { fatalError("Could not write app icon") }
