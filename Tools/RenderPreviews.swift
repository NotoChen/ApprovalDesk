import AppKit
import SwiftUI

// Renders this project's own SwiftUI views to files; no desktop capture or UI automation.
@main
struct RenderPreviews {
    @MainActor static func main() async throws {
        _ = NSApplication.shared
        let output = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
        try FileManager.default.createDirectory(at: output, withIntermediateDirectories: true)
        let appIconURL = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .deletingLastPathComponent().appendingPathComponent("Resources/AppIcon.icns")
        guard let appIcon = NSImage(contentsOf: appIconURL) else {
            throw NSError(domain: "Previews", code: 8, userInfo: [NSLocalizedDescriptionKey: "App icon missing"])
        }
        let model = AppModel(configURL: output.appendingPathComponent("preview-settings.json"), demo: true)
        // An optional read-only probe supplies an actual sender/avatar for
        // visual verification. It is never bundled with the application.
        var avatarProbe: [String: Any] = [:]
        if CommandLine.arguments.count > 2 {
            let data = try Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[2]))
            avatarProbe = try JSONSerialization.jsonObject(with: data) as? [String: Any] ?? [:]
        }
        let avatarURL = avatarProbe["thumbnail_url"] as? String ?? ""
        if let url = URL(string: avatarURL), !avatarURL.isEmpty {
            guard let image = await AvatarImageCache.shared.image(for: url) else {
                throw NSError(domain: "Previews", code: 6, userInfo: [NSLocalizedDescriptionKey: "Real avatar did not load"])
            }
            print("Loaded real avatar: \(Int(image.size.width)) × \(Int(image.size.height))")
        }
        for mode in ["light", "dark"] {
            model.stop()
            model.settings.appearance = mode
            model.loadDemo()
            if let person = avatarProbe["person"] as? String, !person.isEmpty, !model.tasks.isEmpty {
                let index = model.tasks.firstIndex(where: { $0.sender == person }) ?? 0
                model.tasks[index].sender = person
                model.tasks[index].senderAvatarURL = avatarURL
                model.selectedTaskId = model.tasks[index].id
            }
            model.applyAppearance()
            try render(IconFamilyPreview(appIcon: appIcon), size: NSSize(width: 700, height: 330),
                       to: output.appendingPathComponent("icon-family-\(mode).png"))
            try render(MainView().environmentObject(model).tint(.brand), size: NSSize(width: 1180, height: 780),
                       to: output.appendingPathComponent("workspace-\(mode).png"))
            for pane in SettingsPane.allCases {
                try renderSettings(model, pane: pane,
                                   to: output.appendingPathComponent("settings-\(pane.rawValue)-\(mode).png"))
            }
            let originalSettings = model.settings
            model.beginGroupSearch(kind: "approval", query: originalSettings.approvalGroup)
            guard let currentGroup = model.groupOptions.first else { throw NSError(domain: "Previews", code: 7) }
            model.selectGroup(currentGroup)
            precondition(!model.connectionNeedsRestart)
            try renderSettings(model, pane: .sources,
                               to: output.appendingPathComponent("settings-sources-unchanged-\(mode).png"))
            model.settings = originalSettings
            model.beginGroupSearch(kind: "approval", query: "审批群（新选择示例）")
            guard let group = model.groupOptions.first else { throw NSError(domain: "Previews", code: 7) }
            model.selectGroup(group)
            precondition(model.connectionNeedsRestart)
            try renderSettings(model, pane: .sources,
                               to: output.appendingPathComponent("settings-sources-pending-\(mode).png"))
            model.settings = originalSettings
            model.selectedTaskId = "demo-2"
            try render(MainView().environmentObject(model).tint(.brand), size: NSSize(width: 1020, height: 640),
                       to: output.appendingPathComponent("workspace-partial-\(mode).png"))
            let mergedTask = ApprovalTask(["id": "merge-confirmed-preview", "sender": "测试成员", "status": "completed",
                                          "summary": "1 个 MR，审批 1 个，已合并 1 个", "merged_count": 1,
                                          "mrs": [["key": "erp/example!42", "title": "合并状态重新核验（演示）",
                                                   "url": "https://example.test/merge_requests/42", "status": "completed",
                                                   "source_branch": "feature/example", "target_branch": "main", "changes_count": "3",
                                                   "commit_count": 2, "actor": "当前账号", "merged_by": "协作成员",
                                                   "owned_merge": false, "approved": true, "elapsed": 2.4]]])
            model.tasks.insert(mergedTask, at: 0)
            model.selectedTaskId = mergedTask.id
            try render(MainView().environmentObject(model).tint(.brand), size: NSSize(width: 1180, height: 780),
                       to: output.appendingPathComponent("workspace-merge-confirmed-\(mode).png"))
            try render(MenuPanel().environmentObject(model).tint(.brand), size: NSSize(width: 315, height: 250),
                       to: output.appendingPathComponent("menubar-\(mode).png"))
            model.tasks = []; model.checks = []; model.queueCount = 0; model.status = "idle"; model.connected = false
            try render(MainView().environmentObject(model).tint(.brand), size: NSSize(width: 1180, height: 780),
                       to: output.appendingPathComponent("workspace-empty-\(mode).png"))
            for state in [AccountStatus.checking, .error] {
                model.dingtalkAccount = AccountConnection(status: state, detail: state == .error ? "钉钉登录已失效，请重新登录后刷新账号。" : "")
                try renderSettings(model, pane: .accounts,
                                   to: output.appendingPathComponent("settings-accounts-\(state.rawValue)-\(mode).png"))
            }
            model.searchingGroups = false
            model.errorMessage = nil
            model.groupOptions = [
                GroupOption(id: "demo-group-1", title: "审批群（演示）", count: 9),
                GroupOption(id: "demo-group-2", title: "审批群（演示）", count: 16)
            ]
            try render(GroupPickerSheet().environmentObject(model).tint(.primary), size: NSSize(width: 520, height: 380),
                       to: output.appendingPathComponent("group-picker-\(mode).png"))
        }
        print("Rendered 34 native view previews to \(output.path)")
    }

    @MainActor static func renderSettings(_ model: AppModel, pane: SettingsPane, to url: URL) throws {
        let controller = SettingsWindowController(model: model, initialPane: pane)
        defer { controller.close() }
        guard let window = controller.window else { throw NSError(domain: "Previews", code: 4) }
        window.appearance = NSApp.appearance
        try capture(window, to: url, includingWindowControls: true)
    }

    @MainActor static func render<V: View>(_ view: V, size: NSSize, to url: URL) throws {
        let host = NSHostingView(rootView: view.frame(width: size.width, height: size.height))
        host.frame = NSRect(origin: .zero, size: size)
        host.appearance = NSApp.appearance
        // Native glass and split views need a window-backed layout even when
        // rendering only our own views. This window is never shown on the desktop.
        let window = NSWindow(contentRect: host.frame, styleMask: [.borderless], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.appearance = NSApp.appearance
        window.contentView = host
        defer { window.contentView = nil; window.close() }
        try capture(window, to: url, includingWindowControls: false)
    }

    @MainActor static func capture(_ window: NSWindow, to url: URL, includingWindowControls: Bool) throws {
        guard let host = window.contentView else { throw NSError(domain: "Previews", code: 5) }
        RunLoop.main.run(until: Date().addingTimeInterval(0.1))
        host.layoutSubtreeIfNeeded()
        host.displayIfNeeded()
        RunLoop.main.run(until: Date().addingTimeInterval(0.1))
        let surface = includingWindowControls ? (host.superview ?? host) : host
        surface.layoutSubtreeIfNeeded()
        guard let bitmap = surface.bitmapImageRepForCachingDisplay(in: surface.bounds) else {
            throw NSError(domain: "Previews", code: 1, userInfo: [NSLocalizedDescriptionKey: "Cannot allocate preview bitmap"])
        }
        surface.cacheDisplay(in: surface.bounds, to: bitmap)
        var low: CGFloat = 1, high: CGFloat = 0
        for y in stride(from: 0, to: bitmap.pixelsHigh, by: max(1, bitmap.pixelsHigh / 40)) {
            for x in stride(from: 0, to: bitmap.pixelsWide, by: max(1, bitmap.pixelsWide / 40)) {
                guard let color = bitmap.colorAt(x: x, y: y)?.usingColorSpace(.deviceRGB), color.alphaComponent > 0.1 else { continue }
                let light = (color.redComponent + color.greenComponent + color.blueComponent) / 3
                low = min(low, light); high = max(high, light)
            }
        }
        guard high - low > 0.08 else {
            throw NSError(domain: "Previews", code: 3, userInfo: [NSLocalizedDescriptionKey: "Empty render: \(url.lastPathComponent), luminance \(low)...\(high)"])
        }
        guard let data = bitmap.representation(using: .png, properties: [:]) else {
            throw NSError(domain: "Previews", code: 2)
        }
        try data.write(to: url)
    }
}

private struct IconFamilyPreview: View {
    let appIcon: NSImage

    var body: some View {
        VStack(alignment: .leading, spacing: 25) {
            HStack(alignment: .firstTextBaseline, spacing: 12) {
                Text("审批台").font(.system(size: 23, weight: .semibold))
                Text("统一图标").font(.system(size: 13)).foregroundStyle(.secondary)
            }
            HStack(spacing: 38) {
                VStack(spacing: 10) {
                    Image(nsImage: appIcon).resizable().frame(width: 132, height: 132)
                    Text("应用图标").font(.system(size: 12)).foregroundStyle(.secondary)
                }.frame(width: 160)
                Rectangle().fill(Color.primary.opacity(0.1)).frame(width: 1, height: 154)
                VStack(alignment: .leading, spacing: 18) {
                    Text("菜单栏").font(.system(size: 13, weight: .medium))
                    HStack(spacing: 22) {
                        sample("监听中", image: ApprovalMark.image)
                        sample("暂停 / 锁屏", image: ApprovalMark.pausedImage)
                        sample("连接异常", image: ApprovalMark.attentionImage)
                    }
                    Text("上方放大预览 · 下方为 18 pt 实际尺寸")
                        .font(.system(size: 10)).foregroundStyle(.tertiary)
                }
            }
        }.padding(32).frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
            .foregroundStyle(.primary).background(Color(nsColor: .windowBackgroundColor))
    }

    private func sample(_ title: String, image: NSImage) -> some View {
        VStack(spacing: 13) {
            Image(nsImage: image).renderingMode(.template).resizable().frame(width: 48, height: 48)
            Image(nsImage: image).renderingMode(.template).frame(width: 18, height: 18)
            Text(title).font(.system(size: 10)).foregroundStyle(.secondary)
        }.frame(width: 80)
    }
}
