import AppKit
import SwiftUI

struct MainView: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        GlassGroup {
            HStack(spacing: 12) {
                SidebarView().frame(width: 205).glassChrome()
                VStack(spacing: 0) {
                    if model.isDemo {
                        HStack(spacing: 8) {
                            Image(systemName: "play.rectangle")
                            Text("演示模式").fontWeight(.medium)
                            Text("所有请求均为模拟数据").foregroundStyle(.secondary)
                            Spacer()
                            Button(model.locked ? "模拟解锁" : "模拟锁屏") { model.toggleDemoLock() }.deskButtonStyle()
                            Button("新增请求", systemImage: "plus") { model.addDemoMessage() }.deskButtonStyle()
                        }.font(.system(size: 11)).padding(.horizontal, 14).padding(.vertical, 9).contentSurface(radius: 14).padding(.bottom, 10)
                    }
                    if let error = model.errorMessage {
                        HStack(alignment: .top, spacing: 10) {
                            Image(systemName: "exclamationmark.circle.fill").foregroundStyle(.orange)
                            Text(error).font(.system(size: 12)).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
                            Button { model.errorMessage = nil } label: { Image(systemName: "xmark") }.buttonStyle(.plain).foregroundStyle(.secondary)
                        }.padding(14).background(Color.orange.opacity(0.08), in: RoundedRectangle(cornerRadius: 14)).padding(.bottom, 10)
                    }
                    if model.tasks.isEmpty { EmptyWorkspace().frostedPanel() }
                    else {
                        HSplitView {
                            TaskListView().frame(minWidth: 285, idealWidth: 315, maxWidth: 380).frostedPanel().padding(.trailing, 6)
                            if let task = model.selectedTask {
                                TaskDetailView(task: task).frame(minWidth: 420, maxWidth: .infinity, maxHeight: .infinity).frostedPanel().padding(.leading, 6)
                            } else {
                                VStack(spacing: 12) {
                                    Image(systemName: "doc.text.magnifyingglass").font(.system(size: 30, weight: .light)).foregroundStyle(.tertiary)
                                    Text("选择一条请求查看详情").font(.system(size: 13)).foregroundStyle(.secondary)
                                }.frame(minWidth: 420, maxWidth: .infinity, maxHeight: .infinity).frostedPanel().padding(.leading, 6)
                            }
                        }
                    }
                    HStack(spacing: 16) {
                        if model.isActive {
                            Label(model.locked ? "电脑已锁屏" : "电脑已解锁", systemImage: model.locked ? "lock" : "lock.open")
                        } else {
                            Label(statusTitle(model.status), systemImage: "circle.dotted")
                        }
                        Text(model.settings.processWhileLocked ? "锁屏时继续处理" : "锁屏时暂停处理")
                        Spacer()
                        Text("最多 \(model.settings.maxConcurrentMrs) 个 MR 同时处理")
                        Button { model.showDiagnostics = true } label: { Image(systemName: "text.alignleft") }.buttonStyle(.plain).help("运行详情")
                    }.font(.system(size: 10)).foregroundStyle(.secondary).padding(.horizontal, 10).frame(height: 28).padding(.top, 6)
                }
            }
            .padding(12)
        }
        .background(GlassWindowBackground())
        .frame(minWidth: 1020, minHeight: 640)
        .toolbar {
            ToolbarItem(placement: .principal) { Text("审批台").font(.system(size: 13, weight: .semibold)) }
            ToolbarItemGroup(placement: .primaryAction) {
                StatusBadge(status: model.status)
                if model.isActive {
                    Button { model.togglePause() } label: { Label(model.paused ? "恢复处理" : "暂停处理", systemImage: model.paused ? "play.fill" : "pause.fill") }
                        .help(model.paused ? "恢复处理" : "暂停处理，新消息按设置处置")
                    Button { model.stop() } label: { Image(systemName: "stop.fill") }.help("停止监听")
                } else {
                    Button { model.start() } label: { Label("开始监听", systemImage: "play.fill") }
                        .deskButtonStyle(prominent: true).disabled(model.isBusy || model.configurationIssue != nil)
                }
            }
        }
        .sheet(isPresented: $model.showDiagnostics) { DiagnosticsView().environmentObject(model) }
    }
}

struct SidebarView: View {
    @EnvironmentObject var model: AppModel
    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 10) {
                Image(nsImage: ApprovalMark.image).renderingMode(.template).resizable().frame(width: 23, height: 23).foregroundStyle(Color.brand)
                    .frame(width: 38, height: 38).contentSurface(radius: 12)
                VStack(alignment: .leading, spacing: 3) {
                    Text("审批台").font(.system(size: 17, weight: .semibold)).tracking(-0.4)
                    Text("ApprovalDesk").font(.system(size: 10)).foregroundStyle(.secondary)
                }
            }.padding(.horizontal, 18).padding(.top, 24).padding(.bottom, 31)
            Text("工作空间").font(.system(size: 10, weight: .medium)).foregroundStyle(.secondary).padding(.horizontal, 22).padding(.bottom, 10)
            VStack(spacing: 5) {
                ForEach(TaskFilter.allCases) { item in
                    Button {
                        model.filter = item
                        if !model.visibleTasks.contains(where: { $0.id == model.selectedTaskId }) { model.selectedTaskId = model.visibleTasks.first?.id }
                    } label: {
                        HStack(spacing: 10) {
                            Image(systemName: item.icon).font(.system(size: 14)).frame(width: 19)
                            Text(item.title).font(.system(size: 12, weight: model.filter == item ? .semibold : .regular))
                            Spacer()
                            Text("\(model.tasks.filter { item.includes($0) }.count)").font(.system(size: 11).monospacedDigit()).foregroundStyle(.secondary)
                        }.padding(.horizontal, 12).frame(height: 38)
                            .foregroundStyle(model.filter == item ? Color.ink : Color.ink.opacity(0.7))
                            .background { if model.filter == item { GlassSelection() } }
                    }.buttonStyle(NavigationButtonStyle())
                }
            }.padding(.horizontal, 10)
            if model.queueCount > 0 {
                VStack(alignment: .leading, spacing: 8) {
                    HStack { Text("待处理队列"); Spacer(); Text("\(model.queueCount)").monospacedDigit() }.font(.system(size: 11, weight: .medium))
                    Text(model.paused ? "手动恢复后继续处理" : model.status == "locked" ? "解锁后继续处理" : "将依次处理各条请求").font(.system(size: 10)).foregroundStyle(.secondary)
                    Button("清空待处理队列") { model.clearQueue() }.font(.system(size: 10)).buttonStyle(.borderless).foregroundStyle(.secondary)
                }.padding(12).contentSurface(radius: 14).padding(.horizontal, 13).padding(.top, 22)
            }
            Spacer(minLength: 24)
            VStack(alignment: .leading, spacing: 14) {
                connectionRow("钉钉", icon: "bubble.left.and.bubble.right", account: model.dingtalkAccount)
                connectionRow("GitLab", icon: "point.3.connected.trianglepath.dotted", account: model.gitlabAccount)
            }.padding(.horizontal, 21).padding(.bottom, 20)
            Divider().padding(.horizontal, 17)
            HStack {
                Button { model.openSettingsWindow?() } label: { Label("设置", systemImage: "gearshape").font(.system(size: 12)) }.buttonStyle(.plain)
                Spacer()
                Text(Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "开发版")
                    .font(.system(size: 10)).foregroundStyle(.tertiary)
            }.padding(.horizontal, 21).frame(height: 48)
        }
    }
    private func connectionRow(_ title: String, icon: String, account: AccountConnection) -> some View {
        HStack(spacing: 9) {
            Image(systemName: icon).font(.system(size: 13)).foregroundStyle(.secondary).frame(width: 17)
            VStack(alignment: .leading, spacing: 3) {
                Text(title).font(.system(size: 11, weight: .medium))
                Text(account.displayName).font(.system(size: 10)).foregroundStyle(.secondary).lineLimit(1)
            }
            Spacer()
            if account.status == .checking {
                ProgressView().controlSize(.mini).scaleEffect(0.7).frame(width: 10, height: 10)
            } else {
                Circle().fill(statusColor(account.status.rawValue).opacity(account.status == .unchecked ? 0.3 : 0.8)).frame(width: 5, height: 5)
            }
        }
        .help(account.explanation)
    }
}

struct TaskListView: View {
    @EnvironmentObject var model: AppModel
    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text(model.filter.title).font(.system(size: 19, weight: .semibold)).tracking(-0.4)
                Spacer()
                Menu {
                    Button("清除已结束的记录") { model.clearFinished() }
                    Button("查看运行详情") { model.showDiagnostics = true }
                } label: { Image(systemName: "ellipsis") }.menuStyle(.borderlessButton).frame(width: 22)
            }.padding(.horizontal, 20).padding(.top, 24).padding(.bottom, 17)
            HStack(spacing: 8) {
                Image(systemName: "magnifyingglass").foregroundStyle(.tertiary)
                TextField("搜索发送人、MR 或分支", text: $model.search).textFieldStyle(.plain).font(.system(size: 11))
                if !model.search.isEmpty { Button { model.search = "" } label: { Image(systemName: "xmark.circle.fill").foregroundStyle(.tertiary) }.buttonStyle(.plain) }
            }.padding(.horizontal, 11).padding(.vertical, 10).contentSurface(radius: 12).padding(.horizontal, 14).padding(.bottom, 12)
            if model.visibleTasks.isEmpty {
                VStack(spacing: 9) {
                    Image(systemName: "line.3.horizontal.decrease.circle").font(.system(size: 25, weight: .light))
                    Text("没有符合条件的请求").font(.system(size: 12))
                }.foregroundStyle(.secondary).frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                ScrollView {
                    LazyVStack(spacing: 6) {
                        ForEach(model.visibleTasks) { task in
                            Button { model.selectedTaskId = task.id } label: {
                                TaskRow(task: task, selected: model.selectedTaskId == task.id)
                            }.buttonStyle(NavigationButtonStyle())
                        }
                    }.padding(9)
                }
            }
        }
    }
}

struct TaskRow: View {
    let task: ApprovalTask
    let selected: Bool
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 9) {
                SenderAvatar(name: task.sender, url: task.senderAvatarURL, size: 28)
                Text(task.sender).font(.system(size: 13, weight: .semibold))
                    .lineLimit(1)
                Spacer()
                Text(task.receivedAt, style: .time).font(.system(size: 10)).foregroundStyle(.tertiary)
            }
            Text(task.mrs.first.map { $0.title.isEmpty ? $0.key : $0.title } ?? task.summary)
                .font(.system(size: 11)).foregroundStyle(.secondary).lineLimit(2).frame(maxWidth: .infinity, alignment: .leading)
            HStack {
                Text("\(task.mrs.count) 个 MR").font(.system(size: 10)).foregroundStyle(.secondary)
                if task.quoted { Image(systemName: "quote.bubble").font(.system(size: 10)).foregroundStyle(.tertiary) }
                Spacer()
                StatusBadge(status: task.status, compact: true)
            }
        }.padding(14).background { if selected { GlassSelection(radius: 15) } }
            .contentShape(Rectangle())
    }
}

struct TaskDetailView: View {
    @EnvironmentObject var model: AppModel
    let task: ApprovalTask
    @State private var showLogs = false
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 23) {
                HStack(alignment: .top, spacing: 13) {
                    SenderAvatar(name: task.sender, url: task.senderAvatarURL, size: 43)
                    VStack(alignment: .leading, spacing: 6) {
                        Text("\(task.sender) 的审批请求").font(.system(size: 20, weight: .semibold))
                        HStack(spacing: 7) {
                            Text(task.receivedAt, format: .dateTime.month().day().hour().minute())
                            Text("·")
                            Text(model.settings.approvalGroup)
                            if task.quoted { Text("· 引用消息") }
                        }.font(.system(size: 10)).foregroundStyle(.secondary)
                    }
                    Spacer(minLength: 5)
                    StatusBadge(status: task.status)
                }
                VStack(alignment: .leading, spacing: 12) {
                    Text(task.content.isEmpty ? "来自钉钉的审批请求" : task.content).font(.system(size: 12)).foregroundStyle(.secondary).lineLimit(4).textSelection(.enabled)
                    HStack(spacing: 20) {
                        Label("\(task.mrs.count) 个 MR", systemImage: "arrow.triangle.pull")
                        if task.elapsed > 0 { Label(String(format: "%.1f 秒", task.elapsed), systemImage: "clock") }
                        if task.received { Label("已添加收到", systemImage: "checkmark.bubble") .foregroundStyle(Color.brand) }
                    }.font(.system(size: 11))
                }.padding(16).frame(maxWidth: .infinity, alignment: .leading).contentSurface()
                if ["partial", "failed"].contains(task.status) {
                    HStack(alignment: .top, spacing: 9) {
                        Image(systemName: "exclamationmark.circle.fill").foregroundStyle(statusColor(task.status))
                        Text(task.summary).font(.system(size: 12)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                    }.padding(13).frame(maxWidth: .infinity, alignment: .leading)
                        .background(statusColor(task.status).opacity(0.055), in: RoundedRectangle(cornerRadius: 8))
                }
                if task.status == "discarded" || task.status == "invalid" {
                    Label(task.summary, systemImage: task.status == "discarded" ? "minus.circle" : "exclamationmark.circle")
                        .font(.system(size: 12)).foregroundStyle(.secondary)
                    if !task.error.isEmpty { Text(task.error).font(.system(size: 12)).foregroundStyle(.red).textSelection(.enabled) }
                }
                VStack(alignment: .leading, spacing: 12) {
                    HStack { Text("合并请求").font(.system(size: 12, weight: .semibold)); Spacer(); Text("按 MR 独立处理").font(.system(size: 10)).foregroundStyle(.tertiary) }
                    ForEach(task.mrs) { mr in MRCard(mr: mr, isDemo: model.isDemo) }
                }
                if !task.summary.isEmpty && !["partial", "failed"].contains(task.status) {
                    HStack(alignment: .top, spacing: 9) {
                        Image(systemName: task.hasErrors ? "exclamationmark.circle" : task.status == "completed" ? "checkmark.circle" : "info.circle").foregroundStyle(statusColor(task.status))
                        Text(task.summary).font(.system(size: 11)).foregroundStyle(.secondary).textSelection(.enabled)
                    }
                }
                DisclosureGroup(isExpanded: $showLogs) {
                    VStack(alignment: .leading, spacing: 9) {
                        let relevant = model.logs.filter { $0.taskId == task.id }
                        if relevant.isEmpty { Text("本条请求暂无运行详情").foregroundStyle(.tertiary) }
                        ForEach(relevant.suffix(80)) { log in
                            HStack(alignment: .top, spacing: 8) {
                                Text(log.date, style: .time).foregroundStyle(.tertiary)
                                Text(log.message).foregroundStyle(log.level == "error" ? Color.red : Color.secondary).textSelection(.enabled)
                            }
                        }
                    }.font(.system(size: 10, design: .monospaced)).frame(maxWidth: .infinity, alignment: .leading).padding(.top, 12)
                } label: { Text("运行详情").font(.system(size: 11)).foregroundStyle(.secondary) }
            }.padding(24).frame(maxWidth: 810, alignment: .leading).frame(maxWidth: .infinity, alignment: .topLeading)
        }
    }
}

struct MRCard: View {
    let mr: MRItem
    let isDemo: Bool
    var phase: Int { ["waiting": -1, "preflight": 0, "approving": 1, "merging": 2, "completed": 3, "failed": mr.approved ? 2 : 0][mr.status] ?? -1 }
    var body: some View {
        VStack(alignment: .leading, spacing: 15) {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 6) {
                    Text(mr.key).font(.system(size: 10, weight: .medium, design: .monospaced)).foregroundStyle(.secondary)
                    Text(mr.title.isEmpty ? "等待获取 MR 详情" : mr.title).font(.system(size: 13, weight: .semibold)).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 10)
                Button { if let url = URL(string: mr.url) { NSWorkspace.shared.open(url) } } label: { Image(systemName: "arrow.up.right").font(.system(size: 11)) }
                    .buttonStyle(.plain).foregroundStyle(.secondary).disabled(mr.url.isEmpty || isDemo)
                    .accessibilityLabel("在 GitLab 打开 \(mr.key)").help("在 GitLab 打开\n\(mr.url)")
            }
            if !mr.sourceBranch.isEmpty {
                HStack(spacing: 7) {
                    Image(systemName: "arrow.triangle.branch").foregroundStyle(.tertiary)
                    Text(mr.sourceBranch).lineLimit(1).truncationMode(.middle)
                    Image(systemName: "arrow.right").foregroundStyle(.tertiary)
                    Text(mr.targetBranch).lineLimit(1).truncationMode(.middle).fontWeight(.medium)
                }.font(.system(size: 10, design: .monospaced)).padding(9).frame(maxWidth: .infinity, alignment: .leading).background(Color.subtle, in: RoundedRectangle(cornerRadius: 9))
            }
            if !mr.changesCount.isEmpty {
                HStack(spacing: 18) {
                    meta("变更文件", mr.changesCount)
                    meta("提交", mr.commitCount.map(String.init) ?? "未知")
                    meta("执行账号", mr.actor.isEmpty ? "待确认" : mr.actor)
                    Spacer(minLength: 0)
                }
            }
            HStack(spacing: 0) {
                ForEach(Array(["检查", "审批", "合并"].enumerated()), id: \.offset) { index, title in
                    if index > 0 { Rectangle().fill(Color.primary.opacity(0.08)).frame(maxWidth: .infinity).frame(height: 1).padding(.horizontal, 10) }
                    let complete = phase > index
                    let current = phase == index
                    HStack(spacing: 5) {
                        Image(systemName: complete ? "checkmark.circle.fill" : current && mr.status == "failed" ? "xmark.circle.fill" : current ? "circle.inset.filled" : "circle")
                        Text(title)
                    }.font(.system(size: 10, weight: .medium)).foregroundStyle(complete ? Color.brand : current ? statusColor(mr.status) : Color.secondary.opacity(0.5))
                }
            }
            if !mr.error.isEmpty {
                Text(mr.error).font(.system(size: 11)).foregroundStyle(.red.opacity(0.9)).textSelection(.enabled).padding(10).frame(maxWidth: .infinity, alignment: .leading).background(Color.red.opacity(0.05), in: RoundedRectangle(cornerRadius: 7))
            }
            if mr.status == "completed" {
                HStack {
                    Text(mr.mergeSummary)
                    Spacer()
                    if mr.elapsed > 0 { Text(String(format: "%.1f 秒", mr.elapsed)).monospacedDigit() }
                }.font(.system(size: 10)).foregroundStyle(.secondary)
            }
        }.padding(18).contentSurface()
    }
    private func meta(_ title: String, _ value: String) -> some View {
        VStack(alignment: .leading, spacing: 4) { Text(title).foregroundStyle(.secondary).font(.system(size: 10)); Text(value).font(.system(size: 11)).lineLimit(1) }
    }
}

struct StatusBadge: View {
    let status: String
    var compact = false
    var body: some View {
        HStack(spacing: 5) {
            Circle().fill(statusColor(status)).frame(width: 5, height: 5)
            Text(statusTitle(status)).font(.system(size: compact ? 10 : 11, weight: .medium))
        }.foregroundStyle(Color.ink.opacity(0.75)).padding(.horizontal, compact ? 7 : 10).padding(.vertical, compact ? 4 : 6)
            .background(Color.primary.opacity(0.045), in: Capsule()).fixedSize()
    }
}

struct EmptyWorkspace: View {
    @EnvironmentObject var model: AppModel
    var body: some View {
        VStack(alignment: .leading, spacing: 24) {
            Group {
                if model.isActive {
                    Image(systemName: "bubble.left.and.bubble.right").font(.system(size: 32, weight: .light))
                } else {
                    Image(nsImage: ApprovalMark.image).renderingMode(.template).resizable().frame(width: 36, height: 36)
                }
            }.foregroundStyle(.primary).frame(width: 72, height: 72).glassChrome(radius: 22)
            VStack(alignment: .leading, spacing: 10) {
                Text(model.isActive ? "准备好接收下一条请求" : "让审批自然地完成").font(.system(size: 27, weight: .semibold))
                Text(model.isActive ? "\(model.settings.senderGroup)的成员在\(model.settings.approvalGroup)中 @你后，请求会出现在这里。" : "连接钉钉和 GitLab，选择消息来源。审批台会在菜单栏安静地等待，并把每一步的结果留在这里。")
                    .font(.system(size: 13)).foregroundStyle(.secondary).lineSpacing(5).fixedSize(horizontal: false, vertical: true)
            }
            if !model.isActive {
                HStack(spacing: 10) {
                    Button("配置工作空间", systemImage: "slider.horizontal.3") { model.openSettingsWindow?() }.deskButtonStyle(prominent: true).controlSize(.large)
                    Button("检查连接") { model.checkEnvironment() }.deskButtonStyle().controlSize(.large).disabled(model.isBusy)
                }
            }
            if !model.checks.isEmpty {
                VStack(spacing: 12) { ForEach(model.checks) { CheckRow(item: $0) } }.padding(18).contentSurface()
            } else {
                HStack(spacing: 28) {
                    Label("原生菜单栏", systemImage: "menubar.rectangle")
                    Label("按你的策略运行", systemImage: "slider.horizontal.3")
                    Label("实时处理结果", systemImage: "checkmark.circle")
                }.font(.system(size: 10)).foregroundStyle(.tertiary).padding(.top, 13)
            }
        }.frame(maxWidth: 590, alignment: .leading).padding(45).frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

struct CheckRow: View {
    let item: EnvironmentCheck
    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            if item.status == "checking" { ProgressView().controlSize(.small).frame(width: 14) }
            else { Image(systemName: item.status == "ready" ? "checkmark.circle.fill" : item.status == "optional" ? "minus.circle" : "exclamationmark.circle.fill").foregroundStyle(item.status == "error" ? Color.orange : item.status == "optional" ? Color.secondary : Color.brand).frame(width: 14) }
            VStack(alignment: .leading, spacing: 4) {
                Text(item.title).font(.system(size: 11, weight: .medium))
                Text(item.detail).font(.system(size: 10)).foregroundStyle(.secondary).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 0)
        }
    }
}

struct MenuPanel: View {
    @EnvironmentObject var model: AppModel
    var body: some View {
        GlassGroup {
            VStack(alignment: .leading, spacing: 18) {
                HStack(spacing: 9) {
                    Image(nsImage: ApprovalMark.image).renderingMode(.template).accessibilityHidden(true)
                    Text("审批台").font(.system(size: 16, weight: .semibold))
                    Spacer()
                    StatusBadge(status: model.status)
                }
                HStack(spacing: 25) {
                    metric("等待处理", model.queueCount)
                    metric("处理中", model.activeCount)
                    metric("已完成", model.completedCount)
                }
                if let error = model.errorMessage { Text(error).font(.system(size: 11)).foregroundStyle(.orange).lineLimit(4) }
                if model.isActive {
                    Button { model.togglePause() } label: { Label(model.paused ? "恢复处理" : "暂停处理", systemImage: model.paused ? "play.fill" : "pause.fill").frame(maxWidth: .infinity) }.deskButtonStyle().controlSize(.large)
                } else { Button { model.start() } label: { Label("开始监听", systemImage: "play.fill").frame(maxWidth: .infinity) }.deskButtonStyle(prominent: true).controlSize(.large).disabled(model.isBusy || model.configurationIssue != nil) }
                Divider()
                HStack {
                    Button("打开工作台") { model.openMainWindow?() }.buttonStyle(.borderless)
                    Spacer()
                    Button { model.openSettingsWindow?() } label: { Image(systemName: "gearshape") }.buttonStyle(.borderless)
                    Menu { Button("停止监听") { model.stop() }; Button("退出审批台") { NSApp.terminate(nil) } } label: { Image(systemName: "ellipsis") }.menuStyle(.borderlessButton).frame(width: 20)
                }.font(.system(size: 11))
            }.padding(21).frame(width: 315).glassChrome(radius: 24)
        }.background(GlassWindowBackground())
    }
    private func metric(_ title: String, _ value: Int) -> some View { VStack(alignment: .leading, spacing: 6) { Text("\(value)").font(.system(size: 23, weight: .medium, design: .rounded)); Text(title).font(.system(size: 10)).foregroundStyle(.secondary) } }
}

struct DiagnosticsView: View {
    @EnvironmentObject var model: AppModel
    @Environment(\.dismiss) var dismiss
    var body: some View {
        VStack(spacing: 0) {
            HStack { Text("运行详情").font(.headline); Spacer(); Text("仅保留在本次运行中").font(.caption).foregroundStyle(.secondary); Button("完成") { dismiss() }.keyboardShortcut(.cancelAction) }.padding(20)
            Divider()
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 11) {
                    if model.logs.isEmpty { Text("暂时没有运行记录").foregroundStyle(.secondary).padding(.top, 40) }
                    ForEach(model.logs) { log in
                        HStack(alignment: .top, spacing: 12) {
                            Text(log.date, style: .time).foregroundStyle(.tertiary)
                            Text(log.message).foregroundStyle(log.level == "error" ? Color.red : log.level == "warning" ? Color.orange : Color.primary.opacity(0.8)).textSelection(.enabled)
                        }
                    }
                }.font(.system(size: 11, design: .monospaced)).frame(maxWidth: .infinity, alignment: .leading).padding(20)
            }
        }.frame(width: 790, height: 520).background(GlassWindowBackground())
    }
}
