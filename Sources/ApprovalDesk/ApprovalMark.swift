import AppKit

// One vector master for the app icon, menu bar, and workspace identity.
// AppKit templates keep the small symbol legible on any menu-bar background.
enum ApprovalMark {
    enum State { case ready, paused, attention }

    static let bounds = NSRect(x: 253, y: 218, width: 486, height: 570)
    static let image = template(state: .ready)
    static let pausedImage = template(state: .paused)
    static let attentionImage = template(state: .attention)

    static func menuImage(paused: Bool, status: String) -> NSImage {
        if paused || status == "locked" { return pausedImage }
        if status == "error" { return attentionImage }
        return image
    }

    private static func template(state: State) -> NSImage {
        let image = NSImage(size: NSSize(width: 18, height: 18), flipped: false) { rect in
            draw(in: rect.insetBy(dx: 0.5, dy: 0.5), color: .black, state: state)
            return true
        }
        image.isTemplate = true
        return image
    }

    static func draw(in rect: NSRect, color: NSColor, state: State = .ready) {
        guard let context = NSGraphicsContext.current?.cgContext else { return }
        let scale = min(rect.width / bounds.width, rect.height / bounds.height)
        context.saveGState()
        context.translateBy(x: rect.midX - bounds.width * scale / 2,
                            y: rect.midY - bounds.height * scale / 2)
        context.scaleBy(x: scale, y: scale)
        context.translateBy(x: -bounds.minX, y: -bounds.minY)
        // Holes belong to the symbol's alpha, not to a painted white background.
        // Isolate them so the app icon's glass base remains intact underneath.
        context.beginTransparencyLayer(auxiliaryInfo: nil)
        context.setStrokeColor(color.cgColor)
        context.setFillColor(color.cgColor)
        context.setLineCap(.round)
        context.setLineJoin(.round)
        context.setLineWidth(58)
        context.move(to: CGPoint(x: 355, y: 332))
        context.addLine(to: CGPoint(x: 355, y: 710))
        context.strokePath()
        context.move(to: CGPoint(x: 674, y: 710))
        context.addLine(to: CGPoint(x: 674, y: 648))
        context.addCurve(to: CGPoint(x: 355, y: 400),
                         control1: CGPoint(x: 674, y: 488), control2: CGPoint(x: 355, y: 563))
        context.strokePath()
        for x in [CGFloat(355), CGFloat(674)] {
            context.fillEllipse(in: CGRect(x: x - 65, y: 658, width: 130, height: 130))
        }
        context.fillEllipse(in: CGRect(x: 253, y: 218, width: 204, height: 204))

        context.setBlendMode(.clear)
        for x in [CGFloat(355), CGFloat(674)] {
            context.fillEllipse(in: CGRect(x: x - 27, y: 696, width: 54, height: 54))
        }
        switch state {
        case .ready:
            // A slightly stronger check survives the menu bar's 18-point size.
            context.setLineWidth(rect.height <= 32 ? 26 : 19)
            context.move(to: CGPoint(x: 310, y: 323))
            context.addLine(to: CGPoint(x: 342, y: 292))
            context.addLine(to: CGPoint(x: 403, y: 351))
            context.strokePath()
        case .paused:
            context.setLineWidth(27)
            for x in [CGFloat(327), CGFloat(383)] {
                context.move(to: CGPoint(x: x, y: 281))
                context.addLine(to: CGPoint(x: x, y: 359))
                context.strokePath()
            }
        case .attention:
            context.setLineWidth(28)
            context.move(to: CGPoint(x: 355, y: 324))
            context.addLine(to: CGPoint(x: 355, y: 366))
            context.strokePath()
            context.fillEllipse(in: CGRect(x: 340, y: 272, width: 30, height: 30))
        }
        context.endTransparencyLayer()
        context.restoreGState()
    }
}
