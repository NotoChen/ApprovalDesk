import AppKit

@main
struct CreateIcon {
    static func main() throws {
        let output = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
        try FileManager.default.createDirectory(at: output, withIntermediateDirectories: true)
        for size in [16, 32, 128, 256, 512] {
            try draw(size, to: output.appendingPathComponent("icon_\(size)x\(size).png"))
            try draw(size * 2, to: output.appendingPathComponent("icon_\(size)x\(size)@2x.png"))
        }
    }

    static func draw(_ size: Int, to output: URL) throws {
        let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: size, pixelsHigh: size,
            bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
            colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
        let context = NSGraphicsContext(bitmapImageRep: bitmap)!
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = context
        context.cgContext.scaleBy(x: CGFloat(size) / 1024, y: CGFloat(size) / 1024)
        let background = NSBezierPath(roundedRect: NSRect(x: 60, y: 60, width: 904, height: 904), xRadius: 206, yRadius: 206)
        let shadow = NSShadow()
        shadow.shadowColor = NSColor.black.withAlphaComponent(0.16)
        shadow.shadowBlurRadius = 34
        shadow.shadowOffset = NSSize(width: 0, height: -12)
        shadow.set()
        NSColor(calibratedWhite: 0.97, alpha: 1).setFill()
        background.fill()
        NSShadow().set()
        NSGradient(starting: NSColor(calibratedWhite: 0.99, alpha: 1), ending: NSColor(calibratedWhite: 0.83, alpha: 1))!.draw(in: background, angle: -70)
        NSColor.white.withAlphaComponent(0.9).setStroke()
        background.lineWidth = 4
        background.stroke()
        let rim = NSBezierPath(roundedRect: NSRect(x: 83, y: 83, width: 858, height: 858), xRadius: 188, yRadius: 188)
        NSColor.white.withAlphaComponent(0.52).setStroke()
        rim.lineWidth = 3
        rim.stroke()
        ApprovalMark.draw(in: ApprovalMark.bounds, color: NSColor(calibratedWhite: 0.22, alpha: 1))
        NSGraphicsContext.restoreGraphicsState()
        try bitmap.representation(using: .png, properties: [:])!.write(to: output)
    }
}
