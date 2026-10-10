import AppKit
import SwiftUI

enum SettingsPane: String, CaseIterable, Identifiable {
    case accounts, sources, execution, approval, appearance, advanced
    var id: String { rawValue }
    var title: String {
        switch self { case .accounts: "连接与账号"; case .sources: "消息来源"; case .execution: "执行与队列"; case .approval: "审批与回执"; case .appearance: "通知与外观"; case .advanced: "高级设置" }
    }
    var icon: String {
        switch self { case .accounts: "person.crop.circle"; case .sources: "bubble.left.and.bubble.right"; case .execution: "slider.horizontal.3"; case .approval: "checkmark.bubble"; case .appearance: "bell"; case .advanced: "gearshape.2" }
    }
    var subtitle: String {
        switch self {
        case .accounts: "连接工作账号，检查服务是否可用。"
        case .sources: "决定从哪里接收请求，以及允许哪些人触发审批。"
        case .execution: "让审批按照你的工作节奏运行。"
        case .approval: "设置执行账号、消息已读与收到表态。"
        case .appearance: "选择需要的反馈，让应用融入你的桌面。"
        case .advanced: "调整等待与重试参数，以及本机组件位置。"
        }
    }
}

struct SettingsView: View {
    @EnvironmentObject var model: AppModel
    @State private var pane: SettingsPane = .accounts
    @State private var beforeRestore: AppSettings?
    @Environment(\.accessibilityReduceMotion) var reduceMotion

    init(initialPane: SettingsPane = .accounts) { _pane = State(initialValue: initialPane) }

    var body: some View {
        GlassGroup {
            HStack(spacing: 12) {
                VStack(alignment: .leading, spacing: 5) {
                    Text("设置").font(.system(size: 22, weight: .semibold)).padding(.horizontal, 13).padding(.top, 24).padding(.bottom, 25)
                    ForEach(SettingsPane.allCases) { item in
                        Button { pane = item } label: {
                            HStack(spacing: 10) {
                                Image(systemName: item.icon).font(.system(size: 14)).frame(width: 19)
                                Text(item.title).font(.system(size: 12, weight: pane == item ? .medium : .regular))
                                Spacer(minLength: 0)
                            }.padding(.horizontal, 12).frame(height: 39)
                                .foregroundStyle(pane == item ? Color.ink : Color.ink.opacity(0.7))
                                .background { if pane == item { GlassSelection() } }
                        }.buttonStyle(NavigationButtonStyle())
                    }
                    Spacer()
                    Text("ApprovalDesk").font(.system(size: 10)).foregroundStyle(.tertiary).padding(14)
                }.padding(.horizontal, 10).frame(width: 180).glassChrome()
                VStack(spacing: 0) {
                    ScrollView {
                        VStack(alignment: .leading, spacing: 23) {
                            VStack(alignment: .leading, spacing: 8) {
                                Text(pane.title).font(.system(size: 24, weight: .semibold)).tracking(-0.5)
                                Text(pane.subtitle).font(.system(size: 12)).foregroundStyle(.secondary)
                            }.padding(.bottom, 2)
                            if model.isDemo {
                                Label("演示设置，与正式配置独立", systemImage: "play.rectangle").font(.system(size: 11)).foregroundStyle(.secondary)
                            }
                            if model.connectionNeedsRestart {
                                Label("账号或消息来源已修改，重新开始监听后生效。", systemImage: "arrow.clockwise").font(.system(size: 11)).foregroundStyle(.orange)
                            }
                            paneContent
                        }.padding(26).frame(maxWidth: .infinity, alignment: .leading)
                    }
                    Divider().opacity(0.5).padding(.horizontal, 20)
                    HStack(spacing: 7) {
                        Image(systemName: model.configurationIssue == nil ? "checkmark.circle" : "exclamationmark.circle")
                            .foregroundStyle(model.configurationIssue == nil ? Color.secondary : Color.orange)
                        Text(model.configurationIssue ?? (model.savedAt == nil ? "配置自动保存" : "所有更改已保存")).font(.system(size: 10)).lineLimit(2)
                        Spacer(minLength: 8)
                        Button("查看配置文件") { NSWorkspace.shared.activateFileViewerSelecting([model.configURL]) }.font(.system(size: 10)).buttonStyle(.borderless)
                    }.foregroundStyle(.secondary).padding(.horizontal, 24).frame(height: 40)
                }.frostedPanel()
            }.padding(12)
        }.background(GlassWindowBackground(transparentTitlebar: false)).frame(width: 900, height: 720)
            .sheet(isPresented: $model.showGroupPicker) { GroupPickerSheet().environmentObject(model) }
            .sheet(isPresented: $model.showAuth) { AuthSheet().environmentObject(model) }
    }

    @ViewBuilder var paneContent: some View {
        switch pane {
        case .accounts: accounts
        case .sources: sources
        case .execution: execution
        case .approval: approval
        case .appearance: appearance
        case .advanced: advanced
        }
    }

    private var accounts: some View {
        VStack(alignment: .leading, spacing: 22) {
            SettingsSection("钉钉") {
                SettingsRow("当前账号", detail: model.dingtalkAccount.explanation) {
                    accountLabel(model.dingtalkAccount)
                    Button("登录钉钉") { model.login("dingtalk") }.disabled(model.isActive || model.isBusy)
                }
                Divider()
                SettingsRow("组织与账号", detail: "选择已登录的钉钉账号") {
                    Picker("组织与账号", selection: Binding(get: { model.settings.profile }, set: model.selectProfile)) {
                        Text("使用当前登录账号").tag("")
                        ForEach(model.profileOptions) { option in Text(option.title).tag(option.id) }
                        if !model.settings.profile.isEmpty && !model.profileOptions.contains(where: { $0.id == model.settings.profile }) {
                            Text(model.settings.profile).tag(model.settings.profile)
                        }
                    }.labelsHidden().frame(width: 250)
                    Button { model.loadProfiles() } label: { Image(systemName: "arrow.clockwise") }
                        .help("刷新已登录账号").disabled(model.isActive || model.isBusy || model.loadingProfiles)
                }
            }.onAppear { model.loadProfiles() }
            SettingsSection("GitLab") {
                SettingsRow("服务地址", detail: "填写主机名") { TextField("git2.superboss.cc", text: $model.settings.gitlabHost).textFieldStyle(.roundedBorder).frame(width: 255) }
                Divider()
                SettingsRow("当前账号", detail: model.gitlabAccount.explanation) {
                    accountLabel(model.gitlabAccount)
                    Button("浏览器登录") { model.login("gitlab") }.disabled(model.isActive || model.isBusy)
                }
                Divider()
                VStack(alignment: .leading, spacing: 10) {
                    Text("当前账号 Token（可选）").font(.system(size: 12, weight: .medium))
                    Text("留空使用已有登录，也可以直接填写 Token。内容保存在本地 JSON 配置中。").font(.system(size: 10)).foregroundStyle(.secondary)
                    TextField("GitLab 访问令牌", text: $model.settings.gitlabToken).textFieldStyle(.roundedBorder).font(.system(size: 11, design: .monospaced))
                }.padding(.vertical, 12)
            }
            HStack {
                Button("刷新账号") { model.refreshAccounts() }.deskButtonStyle().disabled(model.isBusy || model.isActive)
                Button { model.checkEnvironment() } label: { Label(model.status == "checking" ? "正在检查" : "检查全部连接", systemImage: "arrow.clockwise") }.deskButtonStyle(prominent: true).disabled(model.isBusy || model.isActive)
                Text("只检查连接与配置，不执行审批").font(.system(size: 10)).foregroundStyle(.secondary)
            }
            if !model.checks.isEmpty { SettingsSection("连接检查") { VStack(spacing: 15) { ForEach(model.checks) { CheckRow(item: $0) } }.padding(.vertical, 10) } }
            if let error = model.errorMessage { Text(error).font(.system(size: 11)).foregroundStyle(.orange).textSelection(.enabled) }
        }
    }

    private func accountLabel(_ account: AccountConnection) -> some View {
        HStack(spacing: 6) {
            if account.status == .checking { ProgressView().controlSize(.small) }
            if account.status == .error { Image(systemName: "exclamationmark.circle").foregroundStyle(.orange) }
            Text(account.displayName).font(.system(size: 12)).foregroundStyle(.secondary)
        }.help(account.explanation)
    }

    private var sources: some View {
        VStack(alignment: .leading, spacing: 22) {
            SettingsSection("监听范围") {
                groupRow("接收审批请求的群", detail: "仅处理这个群中的 @我 消息", kind: "approval")
                Divider()
                groupRow("允许发送人的来源群", detail: "读取群成员，并自动排除本人", kind: "sender")
                if model.isActive {
                    Text("监听中可查找并选择群聊。切换到其他群后，重新开始监听生效。")
                        .font(.system(size: 10)).foregroundStyle(.secondary).padding(.bottom, 12)
                }
            }
            SettingsSection("消息内容") {
                SettingsRow("读取引用消息", detail: "从被引用的消息中提取 MR 链接") { SwitchControl(value: $model.settings.parseQuotedMessages) }
                Divider()
                SettingsRow("单条消息的 MR 上限", detail: "超出上限时跳过该消息，并显示原因") { NumberControl(value: $model.settings.maxMrsPerMessage, range: 1...200, suffix: "个") }
            }
            SettingsSection("当前人员名单") {
                VStack(alignment: .leading, spacing: 12) {
                    if model.members.isEmpty { Text("检查连接后读取人员名单。群成员在每次开始监听时刷新。").font(.system(size: 11)).foregroundStyle(.secondary) }
                    else {
                        Text("已纳入 \(model.members.count) 位成员").font(.system(size: 11)).foregroundStyle(.secondary)
                        Text(model.members.joined(separator: "、")).font(.system(size: 12)).lineSpacing(6)
                    }
                    if model.isActive {
                        Text("以上为本次监听使用的名单，重新开始监听时按所选来源群刷新。")
                            .font(.system(size: 10)).foregroundStyle(.secondary)
                    }
                    Button("刷新名单") { model.checkEnvironment() }.disabled(model.isBusy || model.isActive)
                }.padding(.vertical, 10)
            }
        }
    }

    private func groupRow(_ title: String, detail: String, kind: String) -> some View {
        VStack(alignment: .leading, spacing: 9) {
            Text(title).font(.system(size: 12, weight: .medium))
            Text(detail).font(.system(size: 10)).foregroundStyle(.secondary)
            HStack {
                TextField("群名称", text: Binding(get: { kind == "approval" ? model.settings.approvalGroup : model.settings.senderGroup }, set: { value in
                    model.updateGroupName(value, kind: kind)
                })).textFieldStyle(.roundedBorder)
                Button("查找并选择") { model.beginGroupSearch(kind: kind, query: kind == "approval" ? model.settings.approvalGroup : model.settings.senderGroup) }.disabled(model.isBusy)
            }
            let selected = kind == "approval" ? model.settings.approvalGroupId : model.settings.senderGroupId
            if !selected.isEmpty { Label("已选择具体群聊", systemImage: "checkmark.circle").font(.system(size: 10)).foregroundStyle(Color.brand) }
        }.padding(.vertical, 12)
    }

    private var execution: some View {
        VStack(alignment: .leading, spacing: 22) {
            SettingsSection("锁屏时") {
                SettingsRow("继续处理审批", detail: "开启后，电脑锁屏时仍然执行审批与合并") { SwitchControl(value: $model.settings.processWhileLocked) }
                if !model.settings.processWhileLocked {
                    Divider()
                    SettingsRow("期间收到的新消息", detail: "暂存的消息在解锁后继续处理") { messagePolicy($model.settings.lockedMessagePolicy) }
                }
                Text(model.settings.processWhileLocked ? "当前策略：锁屏时照常处理，手动暂停仍然有效。" : model.settings.lockedMessagePolicy == "queue" ? "当前策略：锁屏后暂停，新消息入队，解锁后继续。" : "当前策略：锁屏后暂停，期间收到的新消息直接丢弃。")
                    .font(.system(size: 10)).foregroundStyle(.secondary).padding(.vertical, 11)
            }.animation(reduceMotion ? nil : .easeInOut(duration: 0.18), value: model.settings.processWhileLocked)
            SettingsSection("手动暂停时") {
                SettingsRow("期间收到的新消息", detail: "手动暂停独立于锁屏设置") { messagePolicy($model.settings.pausedMessagePolicy) }
                Text("已有队列会保留。丢弃的新消息不会在恢复后补做，也不会标记已读或添加收到。").font(.system(size: 10)).foregroundStyle(.secondary).padding(.vertical, 10)
            }
            SettingsSection("队列与并发") {
                SettingsRow("每条消息的 MR 并发数量", detail: "同一条消息内并发；不同消息依次处理") { NumberControl(value: $model.settings.maxConcurrentMrs, range: 1...16, suffix: "个") }
                Divider()
                SettingsRow("待处理队列容量", detail: "按钉钉消息数量计算") { NumberControl(value: $model.settings.queueCapacity, range: 1...10000, suffix: "条") }
                Divider()
                SettingsRow("队列已满时") {
                    Picker("队列已满时", selection: $model.settings.queueOverflowPolicy) { Text("丢弃新消息").tag("reject_new"); Text("移除最早的未开始消息").tag("drop_oldest") }.labelsHidden().frame(width: 235)
                }
                Divider()
                SettingsRow("最长等待时间", detail: "0 分钟表示不过期；恢复后会先检查是否过期") {
                    NumberControl(value: Binding(get: { model.settings.maxQueueAgeSeconds / 60 }, set: { model.settings.maxQueueAgeSeconds = $0 * 60 }), range: 0...10080, suffix: "分钟")
                }
            }
            Text("队列仅保留在本次运行中。停止监听或退出应用后，未执行的请求不会自动恢复。").font(.system(size: 10)).foregroundStyle(.secondary)
        }
    }

    private func messagePolicy(_ value: Binding<String>) -> some View {
        Picker("新消息处置", selection: value) { Text("入队待处理").tag("queue"); Text("直接丢弃").tag("discard") }.labelsHidden().frame(width: 180)
    }

    private var approval: some View {
        VStack(alignment: .leading, spacing: 22) {
            SettingsSection("执行账号") {
                SettingsRow("为自己创建的 MR 使用备用账号", detail: "未配置备用账号时，自己创建的 MR 会跳过") { SwitchControl(value: $model.settings.useFallbackForOwnMr) }
                Divider()
                VStack(alignment: .leading, spacing: 10) {
                    Text("备用账号 Token").font(.system(size: 12, weight: .medium))
                    Text("此账号负责审批和合并自己创建的 MR。Token 保存在普通 JSON 配置中。").font(.system(size: 10)).foregroundStyle(.secondary)
                    TextField("可选", text: $model.settings.fallbackToken).textFieldStyle(.roundedBorder).font(.system(size: 11, design: .monospaced))
                }.padding(.vertical, 12)
            }
            SettingsSection("消息回执") {
                SettingsRow("开始处理时标记已读", detail: "排队和丢弃时不标记已读") { SwitchControl(value: $model.settings.markRead) }
                Divider()
                SettingsRow("处理后添加收到表态") { SwitchControl(value: $model.settings.sendReceived) }
                if model.settings.sendReceived {
                    Divider()
                    SettingsRow("添加收到的条件") {
                        Picker("收到条件", selection: $model.settings.receivedCondition) {
                            Text("至少一个 MR 由本人合并").tag("any_owned_merge")
                            Text("全部成功且包含本人合并").tag("all_succeeded_and_owned")
                        }.labelsHidden().frame(width: 245)
                    }
                    Divider()
                    SettingsRow("将备用账号的合并计入本人结果", detail: "用于判断上方的收到条件") { SwitchControl(value: $model.settings.includeFallbackForReceived) }
                }
            }
            Text("每个 MR 都会查询当前状态、审批、请求合并，并确认 GitLab 的实际结果。").font(.system(size: 10)).foregroundStyle(.secondary)
        }
    }

    private var appearance: some View {
        VStack(alignment: .leading, spacing: 22) {
            SettingsSection("外观") {
                SettingsRow("界面外观") { Picker("界面外观", selection: $model.settings.appearance) { Text("跟随系统").tag("system"); Text("浅色").tag("light"); Text("深色").tag("dark") }.labelsHidden().frame(width: 180) }
            }
            SettingsSection("通知") {
                SettingsRow("允许系统通知", detail: "首次开始监听时申请 macOS 通知权限") { SwitchControl(value: $model.settings.notificationsEnabled) }
                if model.settings.notificationsEnabled {
                    Divider()
                    SettingsRow("开始处理请求时") { SwitchControl(value: $model.settings.notifyOnRequest) }
                    Divider()
                    SettingsRow("处理成功时") { SwitchControl(value: $model.settings.notifyOnSuccess) }
                    Divider()
                    SettingsRow("处理异常时", detail: "包括处理失败、部分完成和消息无效") { SwitchControl(value: $model.settings.notifyOnFailure) }
                }
            }
            SettingsSection("启动") {
                SettingsRow("登录 Mac 时打开审批台", detail: model.loginItemDetail) {
                    SwitchControl(value: Binding(get: { model.settings.launchAtLogin }, set: model.setLaunchAtLogin)).disabled(model.updatingLoginItem)
                }
                Divider()
                SettingsRow("打开应用后自动开始监听", detail: "关闭时，需要手动点击开始监听") { SwitchControl(value: $model.settings.startOnLaunch) }
                Divider()
                SettingsRow("打开应用时显示工作台", detail: "默认关闭，仅在菜单栏运行；点击菜单栏图标可打开工作台和设置") { SwitchControl(value: $model.settings.showWorkbenchOnLaunch) }
            }
            SettingsSection("活动记录") {
                SettingsRow("保留最近的请求", detail: "仅存于内存，退出后清空；排队和进行中的任务始终保留") { NumberControl(value: $model.settings.activityLimit, range: 50...3000, suffix: "条") }
            }
        }
    }

    private var advanced: some View {
        VStack(alignment: .leading, spacing: 22) {
            SettingsSection("等待与重试") {
                SettingsRow("默认命令超时") { NumberControl(value: $model.settings.commandTimeoutSeconds, range: 5...600, suffix: "秒") }
                Divider()
                SettingsRow("合并结果确认窗口") { NumberControl(value: $model.settings.mergeTimeoutSeconds, range: 5...3600, suffix: "秒") }
                Divider()
                SettingsRow("合并状态查询间隔") { NumberControl(value: $model.settings.mergePollSeconds, range: 1...60, suffix: "秒") }
                Divider()
                SettingsRow("最大尝试次数", detail: "包括第一次请求") { NumberControl(value: $model.settings.maxAttempts, range: 1...10, suffix: "次") }
                Divider()
                SettingsRow("重试等待间隔", detail: "按顺序使用，之后重复最后一个间隔") { DelayEditor(values: $model.settings.retryDelays) }
                Divider()
                SettingsRow("连接重试等待间隔", detail: "秒，英文逗号分隔") { DelayEditor(values: $model.settings.reconnectDelays) }
            }
            SettingsSection("运行组件") {
                executableRow("钉钉组件", value: $model.settings.dwsPath)
                Divider()
                executableRow("GitLab 组件", value: $model.settings.glabPath)
                Text("留空优先使用应用内置组件，也会查找本机常用安装位置。").font(.system(size: 10)).foregroundStyle(.secondary).padding(.vertical, 10)
            }
            SettingsSection("诊断") {
                SettingsRow("显示底层调试信息", detail: "只在运行详情中显示，不生成日志文件") { SwitchControl(value: $model.settings.debugOutput) }
            }
            SettingsSection("默认设置") {
                VStack(alignment: .leading, spacing: 11) {
                    Text("恢复执行策略、审批回执、通知和超时。保留账号、群聊、组件路径及启动选项。")
                        .font(.system(size: 11)).foregroundStyle(.secondary)
                    HStack {
                        Button("恢复行为默认值") { beforeRestore = model.settings; model.restoreBehaviorDefaults() }
                        if let previous = beforeRestore { Button("撤销恢复") { model.settings = previous; beforeRestore = nil }.buttonStyle(.borderless) }
                    }
                }.padding(.vertical, 14)
            }
        }
    }

    private func executableRow(_ title: String, value: Binding<String>) -> some View {
        VStack(alignment: .leading, spacing: 9) {
            Text(title).font(.system(size: 12, weight: .medium))
            HStack {
                TextField("自动选择", text: value).textFieldStyle(.roundedBorder).font(.system(size: 10, design: .monospaced))
                Button("选择") {
                    let panel = NSOpenPanel(); panel.canChooseDirectories = false; panel.allowsMultipleSelection = false
                    if panel.runModal() == .OK, let url = panel.url { value.wrappedValue = url.path }
                }
            }
        }.padding(.vertical, 12)
    }
}

struct SettingsSection<Content: View>: View {
    let title: String
    @ViewBuilder var content: Content
    init(_ title: String, @ViewBuilder content: () -> Content) { self.title = title; self.content = content() }
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(title).font(.system(size: 11, weight: .semibold)).foregroundStyle(.secondary).padding(.leading, 2)
            VStack(alignment: .leading, spacing: 0) { content }.frame(maxWidth: .infinity, alignment: .leading).padding(.horizontal, 18)
                .contentSurface()
        }
    }
}

struct SettingsRow<Control: View>: View {
    let title: String
    let detail: String
    @ViewBuilder var control: Control
    init(_ title: String, detail: String = "", @ViewBuilder control: () -> Control) { self.title = title; self.detail = detail; self.control = control() }
    var body: some View {
        HStack(spacing: 16) {
            VStack(alignment: .leading, spacing: 5) {
                Text(title).font(.system(size: 12, weight: .medium))
                if !detail.isEmpty { Text(detail).font(.system(size: 10)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true) }
            }.frame(maxWidth: .infinity, alignment: .leading)
            HStack(spacing: 10) { control }.font(.system(size: 11))
        }.padding(.vertical, 14).frame(minHeight: 51)
    }
}

struct SwitchControl: View {
    @Binding var value: Bool
    var body: some View { Toggle("", isOn: $value).toggleStyle(.switch).labelsHidden().controlSize(.small) }
}

struct NumberControl: View {
    @Binding var value: Int
    let range: ClosedRange<Int>
    let suffix: String
    var body: some View {
        HStack(spacing: 7) {
            TextField("", value: $value, format: .number.grouping(.never)).textFieldStyle(.roundedBorder).multilineTextAlignment(.trailing).frame(width: 62)
            Text(suffix).font(.system(size: 10)).foregroundStyle(.secondary).frame(minWidth: 18, alignment: .leading)
            Stepper("", value: $value, in: range).labelsHidden().fixedSize()
        }
    }
}

struct DelayEditor: View {
    @Binding var values: [Int]
    @State private var text = ""
    @State private var invalid = false
    var body: some View {
        VStack(alignment: .trailing, spacing: 4) {
            HStack {
                TextField("2, 5", text: $text).textFieldStyle(.roundedBorder).frame(width: 145).onSubmit(apply)
                Button(action: apply) { Image(systemName: "checkmark") }.help("应用间隔")
            }
            if invalid { Text("请输入 1–300 的秒数").font(.system(size: 9)).foregroundStyle(.orange) }
        }.onAppear { text = values.map(String.init).joined(separator: ", ") }
            .onChange(of: values) { _, newValue in text = newValue.map(String.init).joined(separator: ", "); invalid = false }
    }
    private func apply() {
        let components = text.split(separator: ",", omittingEmptySubsequences: false)
        let parsed = components.compactMap { Int($0.trimmingCharacters(in: .whitespaces)) }
        invalid = parsed.isEmpty || parsed.count != components.count || parsed.count > 10 || parsed.contains { !(1...300).contains($0) }
        if !invalid { values = parsed; text = parsed.map(String.init).joined(separator: ", ") }
    }
}

struct GroupPickerSheet: View {
    @EnvironmentObject var model: AppModel
    @Environment(\.dismiss) var dismiss
    var body: some View {
        VStack(alignment: .leading, spacing: 17) {
            HStack { Text("选择群聊").font(.system(size: 19, weight: .semibold)); Spacer(); Button("取消") { dismiss() }.keyboardShortcut(.cancelAction) }
            Text("使用当前钉钉账号搜索到的群聊。重名时请选择对应的群。").font(.system(size: 11)).foregroundStyle(.secondary)
            if model.searchingGroups { ProgressView("正在查找群聊").frame(maxWidth: .infinity, maxHeight: .infinity) }
            else if model.groupOptions.isEmpty {
                Text(model.errorMessage ?? "没有找到匹配的群聊，请调整名称后再试。") .font(.system(size: 12)).foregroundStyle(.secondary).frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                ScrollView { VStack(spacing: 8) { ForEach(model.groupOptions) { item in
                    Button { model.selectGroup(item) } label: {
                        HStack(spacing: 12) {
                            Image(systemName: "person.2").foregroundStyle(Color.brand).frame(width: 30)
                            VStack(alignment: .leading, spacing: 5) { Text(item.title).font(.system(size: 13, weight: .medium)); Text("\(item.count) 位成员 · \(item.id.suffix(12))").font(.system(size: 10)).foregroundStyle(.secondary) }
                            Spacer(); Image(systemName: "chevron.right").foregroundStyle(.tertiary)
                        }.padding(14).contentSurface(radius: 14)
                    }.buttonStyle(NavigationButtonStyle())
                } } }
            }
        }.padding(26).frame(width: 520, height: 380).background(GlassWindowBackground())
    }
}

struct AuthSheet: View {
    @EnvironmentObject var model: AppModel
    @State private var input = ""
    @State private var details = true
    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack {
                Text(model.authService == "dingtalk" ? "登录钉钉" : "登录 GitLab").font(.system(size: 20, weight: .semibold))
                Spacer()
                if model.authStatus == "running" { ProgressView().controlSize(.small) }
            }
            Text(model.authStatus == "running" ? "在浏览器中完成官方授权，授权结果会自动返回。" : model.authStatus == "completed" ? "授权流程已结束，可以检查连接。" : "授权未完成。可以查看返回信息，或直接在设置中填写 GitLab Token。")
                .font(.system(size: 12)).foregroundStyle(.secondary)
            ScrollView { Text(model.authText.isEmpty ? "正在打开授权流程…" : model.authText).font(.system(size: 10, design: .monospaced)).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading).padding(14) }
                .frame(height: 235).background(Color.subtle, in: RoundedRectangle(cornerRadius: 9))
            if model.authStatus == "running" {
                HStack {
                    TextField("授权流程需要输入时，在这里填写", text: $input).textFieldStyle(.roundedBorder).onSubmit(sendInput)
                    Button("回车", action: sendInput)
                }
            }
            HStack {
                if model.authStatus == "running" { Button("取消授权") { model.client.send("cancel_auth"); model.showAuth = false } }
                Spacer()
                Button("完成") { model.showAuth = false; if model.authStatus == "completed" { model.checkEnvironment() } }.deskButtonStyle(prominent: true).disabled(model.authStatus == "running")
            }
        }.padding(27).frame(width: 570).background(GlassWindowBackground())
    }
    private func sendInput() { model.client.send("auth_input", values: ["text": input + "\r"]); input = "" }
}
