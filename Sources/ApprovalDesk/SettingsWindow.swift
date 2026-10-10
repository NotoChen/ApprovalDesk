import AppKit
import SwiftUI

// Own the window itself, so SwiftUI hosts the settings content without managing
// or replacing its title bar. Previews and checks use this same controller.
@MainActor
final class SettingsWindowController: NSWindowController {
    init(model: AppModel, initialPane: SettingsPane = .accounts) {
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 900, height: 720),
            // Keep the glass host below the title bar. Extending it behind the
            // controls covers them in the live window compositor on macOS 27.
            styleMask: [.titled, .closable, .miniaturizable],
            backing: .buffered, defer: false
        )
        window.title = "设置"
        window.identifier = NSUserInterfaceItemIdentifier("approvaldesk.settings")
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = false
        window.isReleasedWhenClosed = false
        window.tabbingMode = .disallowed
        window.contentViewController = NSHostingController(
            rootView: SettingsView(initialPane: initialPane).environmentObject(model).tint(.primary)
        )
        super.init(window: window)
        window.center()
    }

    required init?(coder: NSCoder) { return nil }

    func present() {
        showWindow(nil)
        window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }
}
