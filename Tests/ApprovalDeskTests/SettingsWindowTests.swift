import AppKit
#if canImport(XCTest)
import XCTest
@testable import ApprovalDesk
#endif

@MainActor
final class SettingsWindowTests: XCTestCase {
    private final class CloseObserver: NSObject, NSWindowDelegate {
        var closeCount = 0
        func windowWillClose(_ notification: Notification) { closeCount += 1 }
    }

    func testHostedSettingsKeepsNativeCloseButtonReachableAndClosesOnlySettings() {
        _ = NSApplication.shared
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        let model = AppModel(configURL: directory.appendingPathComponent("settings.json"), demo: true)
        // Use the same controller and hosted SettingsView as every app entry point.
        // Neither window is displayed and the demo model starts no real worker.
        let controller = SettingsWindowController(model: model)
        guard let settings = controller.window else { preconditionFailure("Settings window is missing") }
        let workbench = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1180, height: 780),
                                 styleMask: [.titled, .closable], backing: .buffered, defer: false)
        workbench.isReleasedWhenClosed = false
        defer { workbench.close() }
        let settingsObserver = CloseObserver(), workbenchObserver = CloseObserver()
        settings.delegate = settingsObserver
        workbench.delegate = workbenchObserver
        XCTAssertTrue(settings.styleMask.contains([.titled, .closable]))
        for appearance in [NSAppearance.Name.aqua, .darkAqua] {
            settings.appearance = NSAppearance(named: appearance)
            RunLoop.main.run(until: Date().addingTimeInterval(0.1))
            settings.contentView?.layoutSubtreeIfNeeded()
            guard let close = settings.standardWindowButton(.closeButton),
                  let frame = settings.contentView?.superview else {
                preconditionFailure("Native close button or window frame is missing")
            }
            frame.layoutSubtreeIfNeeded()
            XCTAssertTrue(!close.isHiddenOrHasHiddenAncestor)
            XCTAssertTrue(close.isEnabled)
            XCTAssertTrue(close.alphaValue > 0.9)
            let buttonBounds = close.convert(close.bounds, to: frame)
            XCTAssertTrue(frame.bounds.contains(buttonBounds))
            XCTAssertTrue(buttonBounds.midX < 100)
            XCTAssertTrue(buttonBounds.midY > frame.bounds.maxY - 60)
            // A full-window glass host can cover these controls when the window
            // is displayed even though hidden-window hit testing still finds them.
            let contentBounds = settings.contentView!.convert(settings.contentView!.bounds, to: frame)
            XCTAssertTrue(!contentBounds.intersects(buttonBounds))
            let hit = frame.hitTest(NSPoint(x: buttonBounds.midX, y: buttonBounds.midY))
            XCTAssertTrue(hit === close || hit?.isDescendant(of: close) == true)
            XCTAssertTrue(!settings.isVisible)
        }
        let statusBeforeClose = model.status
        guard let close = settings.standardWindowButton(.closeButton) else { preconditionFailure("Native close button is missing") }
        close.performClick(nil)
        XCTAssertEqual(settingsObserver.closeCount, 1)
        XCTAssertEqual(workbenchObserver.closeCount, 0)
        XCTAssertTrue(controller.window === settings)
        XCTAssertNotNil(settings.contentViewController)
        XCTAssertEqual(model.status, statusBeforeClose)
    }
}
