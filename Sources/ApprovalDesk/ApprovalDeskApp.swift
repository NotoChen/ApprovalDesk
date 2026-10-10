import SwiftUI
import AppKit
import UserNotifications

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate {
    weak var model: AppModel?
    private var settingsWindowController: SettingsWindowController?

    func showSettings() {
        guard let model else { return }
        NSApp.unhide(nil)
        if settingsWindowController == nil {
            settingsWindowController = SettingsWindowController(model: model)
        }
        settingsWindowController?.present()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        UNUserNotificationCenter.current().delegate = self
        model?.openSettingsWindow = { [weak self] in self?.showSettings() }
        model?.launch()
        // macOS 15+ suppresses the window at the scene level. Keep the macOS 14
        // fallback in the background until an explicit window action unhides it.
        if #unavailable(macOS 15), model?.settings.showWorkbenchOnLaunch != true {
            NSApp.hide(nil)
        }
    }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool { false }
    func applicationShouldSaveSecureApplicationState(_ app: NSApplication) -> Bool { false }
    func applicationShouldRestoreSecureApplicationState(_ app: NSApplication) -> Bool { false }
    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse, withCompletionHandler completionHandler: @escaping () -> Void) {
        let taskId = response.notification.request.content.userInfo["task_id"] as? String ?? ""
        Task { @MainActor in self.model?.showTask(taskId); completionHandler() }
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard let model else { return .terminateNow }
        if !model.client.isRunning { return .terminateNow }
        model.requestQuit { sender.reply(toApplicationShouldTerminate: true) }
        return .terminateLater
    }
}

@main
struct ApprovalDeskApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var delegate
    @StateObject private var model: AppModel

    init() {
        let model = AppModel()
        _model = StateObject(wrappedValue: model)
        delegate.model = model
    }

    var body: some Scene {
        launchControlledWorkbench
        MenuBarExtra {
            MenuPanel().environmentObject(model).tint(.primary)
        } label: {
            MenuBarIcon().environmentObject(model)
        }
        .menuBarExtraStyle(.window)
    }

    private var launchControlledWorkbench: some Scene {
        // SceneBuilder has no buildEither. Its public availability wrapper keeps
        // both versions the same scene type while preserving macOS 14 support.
        if #available(macOS 15, *) {
            return SceneBuilder.buildOptional(SceneBuilder.buildLimitedAvailability(
                workbench.defaultLaunchBehavior(.suppressed).restorationBehavior(.disabled)
            ))
        } else {
            return SceneBuilder.buildOptional(SceneBuilder.buildLimitedAvailability(workbench))
        }
    }

    private var workbench: some Scene {
        Window("审批台", id: "main") {
            MainView().environmentObject(model).tint(.primary)
        }
        .defaultSize(width: 1180, height: 780)
        .windowResizability(.contentMinSize)
        .windowToolbarStyle(.unifiedCompact)
        .commands {
            CommandGroup(replacing: .appSettings) {
                Button("设置…") { delegate.showSettings() }
                    .keyboardShortcut(",", modifiers: .command)
            }
            CommandMenu("监听") {
                Button("开始监听") { model.start() }.keyboardShortcut("r", modifiers: [.command, .shift]).disabled(model.isActive || model.isBusy)
                Button(model.paused ? "恢复处理" : "暂停处理") { model.togglePause() }.keyboardShortcut("p", modifiers: [.command, .shift]).disabled(!model.isActive)
                Button("停止监听") { model.stop() }.disabled(!model.isActive && !model.isBusy)
                Divider()
                Button("查看运行详情") { model.openMainWindow?(); model.showDiagnostics = true }
            }
        }
    }
}

private struct MenuBarIcon: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        Image(nsImage: ApprovalMark.menuImage(paused: model.paused, status: model.status))
            .renderingMode(.template)
            .accessibilityLabel("审批台")
            .accessibilityValue(statusTitle(model.status))
            .help("审批台 · \(statusTitle(model.status))")
            .onAppear {
                model.installMainWindowOpener {
                    NSApp.unhide(nil)
                    openWindow(id: "main")
                    NSApp.activate(ignoringOtherApps: true)
                }
            }
    }
}
