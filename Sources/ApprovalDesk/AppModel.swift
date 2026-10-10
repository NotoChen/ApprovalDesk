import AppKit
import Foundation
import SwiftUI
import UserNotifications
import ServiceManagement

@MainActor
final class AppModel: ObservableObject {
    @Published var settings: AppSettings { didSet { saveSettings(previous: oldValue) } }
    @Published var tasks: [ApprovalTask] = []
    @Published var logs: [ActivityLog] = []
    @Published var checks: [EnvironmentCheck] = []
    @Published var filter: TaskFilter = .all
    @Published var search = ""
    @Published var selectedTaskId: String?
    @Published var status = "idle"
    @Published var connected = false
    @Published var locked = false
    @Published var paused = false
    @Published var queueCount = 0
    @Published var activeCount = 0
    @Published var errorMessage: String?
    @Published var configurationIssue: String?
    @Published var savedAt: Date?
    @Published var dingtalkAccount = AccountConnection()
    @Published var gitlabAccount = AccountConnection()
    @Published var members: [String] = []
    @Published var groupOptions: [GroupOption] = []
    @Published var profileOptions: [ProfileOption] = []
    @Published var loadingProfiles = false
    @Published var groupSearchKind = "approval"
    @Published var searchingGroups = false
    @Published var showGroupPicker = false
    @Published var authStatus = ""
    @Published var authService = ""
    @Published var authText = ""
    @Published var showAuth = false
    @Published var showDiagnostics = false
    @Published var engineAvailable = false
    @Published private(set) var listeningConfiguration: ListeningConfiguration?
    @Published private(set) var updatingLoginItem = false
    @Published private(set) var loginItemStatus: SMAppService.Status = .notRegistered
    @Published private(set) var loginItemIssue: String?
    @Published private var currentOperation: String?
    @Published private var groupOperationID: String?
    let configURL: URL
    let isDemo: Bool
    let client: EngineConnection
    private let loginItem: LoginItemManaging
    var openMainWindow: (() -> Void)?
    var openSettingsWindow: (() -> Void)?
    private var launched = false
    private var windowStartupApplied = false
    private var quitting = false
    private var quitCompletion: (() -> Void)?
    private var demoRun: Task<Void, Never>?
    private var demoRunID = UUID()
    private var pendingAfterLaunch: String?
    private var pendingAccountRefresh = false
    private var pendingProfiles = false
    private var accountRequest: (id: String, signature: String)?
    private var startRequestID: String?
    private var groupRequest: (id: String, kind: String, profile: String)?
    private var operationAccountSignature = ""
    private var accountRefreshTask: Task<Void, Never>?

    init(configURL: URL? = nil, demo: Bool? = nil, client: EngineConnection? = nil, loginItem: LoginItemManaging? = nil) {
        self.client = client ?? EngineClient()
        self.loginItem = loginItem ?? SystemLoginItemManager()
        let arguments = ProcessInfo.processInfo.arguments
        self.isDemo = demo ?? arguments.contains("--demo")
        let argumentURL: URL? = arguments.firstIndex(of: "--config").flatMap { arguments.indices.contains($0 + 1) ? URL(fileURLWithPath: arguments[$0 + 1]) : nil }
        self.configURL = configURL ?? argumentURL ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/ApprovalDesk/settings.json")
        do { settings = try AppSettings.load(from: self.configURL) }
        catch { settings = AppSettings(); configurationIssue = "配置读取失败：\(error.localizedDescription)" }
        self.client.onEvent = { [weak self] event in self?.receive(event) }
        self.client.onExit = { [weak self] in
            guard let self else { return }
            self.engineAvailable = false
            if self.quitting { self.quitCompletion?() }
        }
        if isDemo { loadDemo() }
    }

    var isActive: Bool { ["listening", "paused", "locked", "reconnecting"].contains(status) }
    var isBusy: Bool {
        let operationBusy = currentOperation != nil && !(currentOperation == "start" && isActive)
        return operationBusy || groupOperationID != nil || ["checking", "starting", "stopping"].contains(status) || loadingProfiles || searchingGroups || authStatus == "running"
    }
    var selectedTask: ApprovalTask? { tasks.first { $0.id == selectedTaskId } }
    var connectionNeedsRestart: Bool { isActive && listeningConfiguration?.needsRestart(for: settings) == true }
    var visibleTasks: [ApprovalTask] {
        tasks.filter { filter.includes($0) && (search.isEmpty || ($0.sender + $0.content + $0.mrs.map { $0.key + $0.title + $0.sourceBranch + $0.targetBranch }.joined()).localizedCaseInsensitiveContains(search)) }
    }
    var completedCount: Int { tasks.filter { $0.status == "completed" }.count }

    func launch() {
        guard !launched else { return }
        launched = true
        applyAppearance()
        if configurationIssue == nil && !FileManager.default.fileExists(atPath: configURL.path) { try? settings.save(to: configURL) }
        if isDemo { engineAvailable = true; return }
        if configurationIssue == nil { setLaunchAtLogin(settings.launchAtLogin) }
        pendingAfterLaunch = settings.startOnLaunch ? "auto_start" : nil
        launchWorker()
    }

    func installMainWindowOpener(_ action: @escaping () -> Void) {
        openMainWindow = action
        guard !windowStartupApplied else { return }
        windowStartupApplied = true
        if settings.showWorkbenchOnLaunch { action() }
    }

    private func launchWorker() {
        guard !client.isRunning else { return }
        engineAvailable = false
        status = "idle"
        errorMessage = nil
        currentOperation = "launch"
        dingtalkAccount = AccountConnection(status: .checking)
        gitlabAccount = AccountConnection(status: .checking)
        do { try client.launch(dataDirectory: configURL.deletingLastPathComponent()) }
        catch {
            currentOperation = nil
            pendingAfterLaunch = nil
            failPendingAccounts(error.localizedDescription)
            errorMessage = error.localizedDescription
            status = "error"
        }
    }

    private func sendOperation(_ command: String, values: [String: Any] = [:]) {
        currentOperation = command
        operationAccountSignature = settings.accountSignature
        client.send(command, settings: settings, values: values)
    }

    func refreshAccounts() {
        guard !isDemo else { return }
        if !isActive, !isBusy { errorMessage = nil; if status == "error" { status = "idle" } }
        accountRefreshTask?.cancel(); accountRefreshTask = nil
        pendingAccountRefresh = true
        if !client.isRunning { launchWorker() }
        runPendingOperations()
    }

    private func scheduleAccountRefresh() {
        accountRefreshTask?.cancel(); accountRefreshTask = nil
        pendingAccountRefresh = true
        guard !isActive else { return }
        dingtalkAccount = AccountConnection(status: .checking)
        gitlabAccount = AccountConnection(status: .checking)
        accountRefreshTask = Task { [weak self] in
            do { try await Task.sleep(for: .milliseconds(500)) }
            catch { return }
            guard let self, !Task.isCancelled else { return }
            self.accountRefreshTask = nil
            self.runPendingOperations()
        }
    }

    private func runPendingOperations() {
        guard !quitting, engineAvailable, !isBusy, !isActive,
              configurationIssue == nil, settings.validationIssue == nil, accountRefreshTask == nil else { return }
        if pendingAccountRefresh {
            pendingAccountRefresh = false
            let id = UUID().uuidString
            accountRequest = (id, settings.accountSignature)
            dingtalkAccount = AccountConnection(status: .checking)
            gitlabAccount = AccountConnection(status: .checking)
            sendOperation("accounts", values: ["request_id": id])
        } else if pendingProfiles {
            pendingProfiles = false
            loadingProfiles = true
            sendOperation("profiles")
        } else if let command = pendingAfterLaunch {
            pendingAfterLaunch = nil
            if command == "check" { checkEnvironment() }
            else if command == "start" || settings.startOnLaunch { start() }
            else { runPendingOperations() }
        }
    }

    private func failPendingAccounts(_ message: String) {
        if dingtalkAccount.status == .checking { dingtalkAccount = AccountConnection(status: .error, detail: message) }
        if gitlabAccount.status == .checking { gitlabAccount = AccountConnection(status: .error, detail: message) }
    }

    private func acceptsAccountEvent(_ event: [String: Any]) -> Bool {
        if let id = event["request_id"] as? String {
            return id == accountRequest?.id && accountRequest?.signature == settings.accountSignature
        }
        return !["check", "start"].contains(currentOperation ?? "") || isActive || operationAccountSignature == settings.accountSignature
    }

    private func setAccount(_ account: AccountConnection, service: String) {
        if service == "dingtalk" { dingtalkAccount = account }
        else if service == "gitlab" { gitlabAccount = account }
    }

    private func saveSettings(previous: AppSettings) {
        guard settings != previous else { return }
        if let issue = settings.validationIssue {
            configurationIssue = issue
            if !isDemo, !isActive, settings.accountSignature != previous.accountSignature {
                accountRefreshTask?.cancel(); accountRefreshTask = nil
                pendingAccountRefresh = true
                dingtalkAccount = AccountConnection(status: .error, detail: "请先修正配置：\(issue)")
                gitlabAccount = AccountConnection(status: .error, detail: "请先修正配置：\(issue)")
            }
            return
        }
        do {
            try settings.save(to: configURL)
            configurationIssue = nil
            savedAt = Date()
            if !isDemo { client.send("update_settings", settings: settings) }
            if !isDemo, settings.accountSignature != previous.accountSignature { scheduleAccountRefresh() }
            else if !isDemo { runPendingOperations() }
            if settings.appearance != previous.appearance { applyAppearance() }
            if settings.notificationsEnabled && !previous.notificationsEnabled && isActive { requestNotifications() }
            if isDemo {
                if isActive { status = paused ? "paused" : locked && !settings.processWhileLocked ? "locked" : "listening" }
                runDemoQueue()
            }
        } catch { configurationIssue = "配置保存失败：\(error.localizedDescription)" }
    }

    func applyAppearance() {
        NSApp?.appearance = settings.appearance == "dark" ? NSAppearance(named: .darkAqua) : settings.appearance == "light" ? NSAppearance(named: .aqua) : nil
    }

    func start() {
        guard settings.validationIssue == nil, !isBusy, !isActive else { return }
        errorMessage = nil
        if isDemo {
            listeningConfiguration = demoListeningConfiguration
            status = "listening"; connected = true; paused = false
            addDemoMessage()
            return
        }
        if !engineAvailable || !client.isRunning {
            pendingAfterLaunch = "start"
            launchWorker()
            return
        }
        status = "starting"
        requestNotifications()
        let id = UUID().uuidString
        startRequestID = id
        listeningConfiguration = ListeningConfiguration(settings: settings)
        sendOperation("start", values: ["request_id": id])
    }

    func checkEnvironment() {
        guard !isBusy, !isActive else { return }
        errorMessage = nil
        checks = []
        if isDemo {
            checks = [EnvironmentCheck(id: "demo", title: "演示环境", status: "ready", detail: "模拟数据，无外部连接")]
            return
        }
        if !engineAvailable || !client.isRunning {
            pendingAfterLaunch = "check"
            launchWorker()
            return
        }
        status = "checking"
        sendOperation("check")
    }

    func togglePause() {
        if isDemo {
            paused.toggle(); status = paused ? "paused" : locked && !settings.processWhileLocked ? "locked" : "listening"
            if !paused { runDemoQueue() }
        } else { client.send(paused ? "resume" : "pause") }
    }

    func stop() {
        if isDemo {
            demoRun?.cancel(); demoRun = nil; demoRunID = UUID()
            for i in tasks.indices where tasks[i].running || tasks[i].waiting {
                tasks[i].status = tasks[i].running ? "stopped" : "discarded"
                tasks[i].summary = "监听已停止"
            }
            status = "stopped"; connected = false; paused = false; activeCount = 0; queueCount = 0
            return
        }
        status = "stopping"
        client.send("stop")
    }

    func clearQueue() {
        if isDemo {
            for index in tasks.indices where tasks[index].waiting { tasks[index].status = "discarded"; tasks[index].summary = "已手动清空队列" }
            queueCount = 0
        } else { client.send("clear_queue") }
    }

    func clearFinished() {
        tasks.removeAll { !$0.waiting && !$0.running }
        if !tasks.contains(where: { $0.id == selectedTaskId }) { selectedTaskId = tasks.first?.id }
    }

    func beginGroupSearch(kind: String, query: String) {
        guard !isBusy, !quitting else { return }
        errorMessage = nil
        groupSearchKind = kind
        let id = UUID().uuidString
        groupRequest = (id, kind, settings.profile)
        groupOptions = []
        searchingGroups = true
        showGroupPicker = true
        if isDemo {
            let current = kind == "approval" ? listeningConfiguration?.approvalGroup : listeningConfiguration?.senderGroup
            let groupID = current.flatMap { $0.title == query ? $0.id : nil } ?? "demo-new-\(kind)"
            groupOptions = [GroupOption(id: groupID, title: query, count: 9)]
            searchingGroups = false
        } else {
            groupOperationID = id
            client.send("groups", settings: settings, values: ["kind": kind, "query": query, "request_id": id])
        }
    }

    func selectGroup(_ option: GroupOption) {
        guard groupContextIsCurrent, groupOptions.contains(where: { $0.id == option.id }) else {
            errorMessage = "钉钉账号或查询结果已变化，请重新查找群聊"
            return
        }
        var next = settings
        if groupSearchKind == "approval" { next.approvalGroup = option.title; next.approvalGroupId = option.id }
        else { next.senderGroup = option.title; next.senderGroupId = option.id }
        settings = next
        showGroupPicker = false
    }

    func updateGroupName(_ name: String, kind: String) {
        var next = settings
        if kind == "approval" {
            guard name != next.approvalGroup else { return }
            next.approvalGroup = name; next.approvalGroupId = ""
        } else {
            guard name != next.senderGroup else { return }
            next.senderGroup = name; next.senderGroupId = ""
        }
        settings = next
    }

    private var groupContextIsCurrent: Bool {
        groupRequest?.profile == settings.profile && groupRequest?.kind == groupSearchKind
    }

    private func acceptsGroupEvent(_ event: [String: Any]) -> Bool {
        guard let request = groupRequest else { return false }
        return event["request_id"] as? String == request.id
    }

    func loadProfiles() {
        if isDemo { profileOptions = [ProfileOption(id: "demo", name: "演示账号", organization: "工作空间")]; return }
        guard !isActive, !loadingProfiles else { return }
        pendingProfiles = true
        if !client.isRunning { launchWorker() }
        runPendingOperations()
    }

    func selectProfile(_ profile: String) {
        var next = settings
        next.profile = profile; next.approvalGroupId = ""; next.senderGroupId = ""
        settings = next
        if !isActive { members = []; checks = [] }
    }

    func showTask(_ taskId: String) {
        filter = .all; search = ""
        if tasks.contains(where: { $0.id == taskId }) { selectedTaskId = taskId }
        openMainWindow?()
        NSApp.activate(ignoringOtherApps: true)
    }

    func restoreBehaviorDefaults() {
        var defaults = AppSettings()
        defaults.profile = settings.profile
        defaults.approvalGroup = settings.approvalGroup; defaults.approvalGroupId = settings.approvalGroupId
        defaults.senderGroup = settings.senderGroup; defaults.senderGroupId = settings.senderGroupId
        defaults.gitlabHost = settings.gitlabHost; defaults.gitlabToken = settings.gitlabToken; defaults.fallbackToken = settings.fallbackToken
        defaults.dwsPath = settings.dwsPath; defaults.glabPath = settings.glabPath
        defaults.launchAtLogin = settings.launchAtLogin; defaults.startOnLaunch = settings.startOnLaunch
        defaults.showWorkbenchOnLaunch = settings.showWorkbenchOnLaunch
        settings = defaults
    }

    func login(_ service: String) {
        guard !isBusy, !isActive else { return }
        authService = service; authText = ""; authStatus = "running"; showAuth = true
        if isDemo { authText = "演示模式不发起真实授权。"; authStatus = "completed"; return }
        sendOperation("login", values: ["service": service])
    }

    func setLaunchAtLogin(_ enabled: Bool) {
        if isDemo { settings.launchAtLogin = enabled; return }
        guard !updatingLoginItem else { return }
        updatingLoginItem = true
        loginItemIssue = nil
        Task {
            defer { updatingLoginItem = false }
            do {
                try await loginItem.setEnabled(enabled)
                settings.launchAtLogin = enabled
                loginItemStatus = loginItem.status
                if enabled && (loginItemStatus == .notFound || loginItemStatus == .notRegistered) {
                    loginItemIssue = "macOS 尚未注册登录启动项，请重新开启此选项"
                }
            } catch {
                loginItemStatus = loginItem.status
                loginItemIssue = "登录启动设置未完成：\(error.localizedDescription)"
            }
        }
    }

    var loginItemDetail: String {
        if let loginItemIssue { return loginItemIssue }
        if updatingLoginItem { return "正在更新 macOS 登录启动项…" }
        if !isDemo && loginItemStatus == .requiresApproval { return "请在系统设置 → 通用 → 登录项中允许审批台" }
        if !isDemo && loginItemStatus == .enabled { return "已加入 macOS 登录项" }
        return "登录后自动运行，窗口是否显示由下方选项决定"
    }

    func requestQuit(_ completion: @escaping () -> Void) {
        quitting = true
        groupRequest = nil
        groupOperationID = nil
        quitCompletion = completion
        demoRun?.cancel()
        accountRefreshTask?.cancel(); accountRefreshTask = nil
        pendingAfterLaunch = nil; pendingAccountRefresh = false; pendingProfiles = false
        guard client.isRunning else { completion(); return }
        status = "stopping"
        client.shutdown()
    }

    func receive(_ event: [String: Any]) {
        switch event["type"] as? String {
        case "hello":
            engineAvailable = true
            if currentOperation == "launch" { currentOperation = nil }
            guard !quitting else { return }
            pendingAccountRefresh = true
            if let issue = configurationIssue ?? settings.validationIssue {
                failPendingAccounts("请先修正配置：\(issue)")
                return
            }
            client.send("configure", settings: settings)
            runPendingOperations()
        case "operation_finished":
            let command = event["command"] as? String ?? ""
            if command == "groups" {
                guard acceptsGroupEvent(event) else { return }
                groupOperationID = nil
            }
            if command == "accounts" {
                guard event["request_id"] as? String == accountRequest?.id else { return }
                if accountRequest?.signature == settings.accountSignature {
                    failPendingAccounts("未获取到账号信息，请刷新重试")
                }
                accountRequest = nil
            }
            if currentOperation == command { currentOperation = nil }
            if command == "start" { startRequestID = nil; listeningConfiguration = nil }
            if command == "profiles" { loadingProfiles = false }
            if command == "groups" { searchingGroups = false }
            runPendingOperations()
        case "state":
            if let value = event["status"] as? String { status = value }
            if let value = event["connected"] as? Bool { connected = value }
            if let value = event["is_locked"] as? Bool { locked = value }
            if let value = event["paused"] as? Bool { paused = value }
            if let value = event["queue_count"] as? Int { queueCount = value }
            if let value = event["active_count"] as? Int { activeCount = value }
            if let value = event["error"] as? String, !value.isEmpty { errorMessage = value }
        case "task":
            guard let data = event["task"] as? [String: Any] else { return }
            let task = ApprovalTask(data)
            if let index = tasks.firstIndex(where: { $0.id == task.id }) { tasks[index] = task }
            else { tasks.insert(task, at: 0) }
            if selectedTaskId == nil { selectedTaskId = task.id }
            trimActivity()
        case "task_update":
            if let id = event["id"] as? String, let index = tasks.firstIndex(where: { $0.id == id }) { tasks[index].merge(event) }
        case "mr":
            if let taskId = event["task_id"] as? String, let key = event["key"] as? String,
               let index = tasks.firstIndex(where: { $0.id == taskId }), let mr = tasks[index].mrs.firstIndex(where: { $0.key == key }) { tasks[index].mrs[mr].merge(event) }
        case "log":
            logs.append(ActivityLog(level: event["level"] as? String ?? "info", message: event["message"] as? String ?? "", taskId: event["task_id"] as? String ?? "", mrKey: event["mr_key"] as? String ?? ""))
            if logs.count > 2000 { logs.removeFirst(logs.count - 2000) }
        case "check":
            let item = EnvironmentCheck(id: event["id"] as? String ?? "", title: event["title"] as? String ?? "", status: event["status"] as? String ?? "", detail: event["detail"] as? String ?? "")
            if let index = checks.firstIndex(where: { $0.id == item.id }) { checks[index] = item } else { checks.append(item) }
            if acceptsAccountEvent(event), ["dws_auth", "gitlab_auth"].contains(item.id), ["checking", "error"].contains(item.status) {
                setAccount(AccountConnection(status: item.status == "error" ? .error : .checking, detail: item.detail),
                           service: item.id == "dws_auth" ? "dingtalk" : "gitlab")
            }
        case "account_state":
            guard acceptsAccountEvent(event), let service = event["service"] as? String,
                  let value = event["status"] as? String, let state = AccountStatus(rawValue: value) else { return }
            setAccount(AccountConnection(status: state, detail: event["detail"] as? String ?? ""), service: service)
        case "identity":
            guard acceptsAccountEvent(event), let service = event["service"] as? String else { return }
            setAccount(AccountConnection(status: .ready, name: event["name"] as? String ?? "", detail: event["detail"] as? String ?? ""), service: service)
        case "members": members = (event["members"] as? [[String: Any]] ?? []).compactMap { $0["name"] as? String }
        case "listening_configuration":
            guard !quitting, let id = startRequestID, event["request_id"] as? String == id,
                  var configuration = listeningConfiguration,
                  let approval = ListeningGroup(event["approval_group"]),
                  let sender = ListeningGroup(event["sender_group"]) else { return }
            configuration.approvalGroup = approval
            configuration.senderGroup = sender
            listeningConfiguration = configuration
        case "groups":
            guard acceptsGroupEvent(event) else { return }
            guard groupContextIsCurrent else {
                groupOptions = []
                searchingGroups = false
                errorMessage = "钉钉账号已变化，请重新查找群聊"
                return
            }
            groupOptions = (event["groups"] as? [[String: Any]] ?? []).map { GroupOption(id: $0["id"] as? String ?? "", title: $0["title"] as? String ?? "", count: $0["count"] as? Int ?? 0) }
            searchingGroups = false
        case "profiles":
            profileOptions = (event["profiles"] as? [[String: Any]] ?? []).map {
                ProfileOption(id: $0["id"] as? String ?? "", name: $0["name"] as? String ?? "", organization: $0["organization"] as? String ?? "")
            }
            loadingProfiles = false
        case "auth":
            authStatus = event["status"] as? String ?? ""
            if let message = event["message"] as? String { authText += "\n" + message }
            if authStatus == "completed" {
                pendingAccountRefresh = true
                if event["service"] as? String == "dingtalk" { pendingProfiles = true }
            }
        case "auth_output": authText += event["text"] as? String ?? ""
        case "notification":
            deliverNotification(title: event["title"] as? String ?? "审批台", message: event["message"] as? String ?? "", subtitle: event["subtitle"] as? String ?? "", taskId: event["task_id"] as? String ?? "")
        case "error":
            if event["command"] as? String == "groups" {
                guard acceptsGroupEvent(event) else { return }
                groupOptions = []
                searchingGroups = false
                errorMessage = groupContextIsCurrent ? event["message"] as? String : "钉钉账号已变化，请重新查找群聊"
                if event["operation_rejected"] as? Bool == true {
                    groupOperationID = nil
                    runPendingOperations()
                }
                return
            }
            errorMessage = event["message"] as? String
            failPendingAccounts(errorMessage ?? "账号读取未完成，请重试")
            searchingGroups = false
            loadingProfiles = false
            if isBusy { status = connected ? "listening" : "error" }
            if authStatus == "running" { authStatus = "failed" }
            if event["operation_rejected"] as? Bool == true, event["command"] as? String == currentOperation {
                currentOperation = nil
                accountRequest = nil
                pendingAfterLaunch = nil
            }
        case "worker_exit":
            engineAvailable = false; connected = false
            currentOperation = nil; accountRequest = nil
            startRequestID = nil; listeningConfiguration = nil
            groupRequest = nil; groupOperationID = nil; groupOptions = []
            loadingProfiles = false; searchingGroups = false
            pendingAfterLaunch = nil; pendingAccountRefresh = false; pendingProfiles = false
            accountRefreshTask?.cancel(); accountRefreshTask = nil
            failPendingAccounts("执行引擎已退出，请刷新账号重试")
            if authStatus == "running" { authStatus = "failed" }
            if !quitting { status = "error"; errorMessage = "执行引擎已退出，可在连接与账号中刷新重试" }
        default: break
        }
    }

    private func trimActivity() {
        while tasks.count > settings.activityLimit, let index = tasks.lastIndex(where: { !$0.waiting && !$0.running && $0.id != selectedTaskId }) { tasks.remove(at: index) }
    }

    private func requestNotifications() {
        guard settings.notificationsEnabled, !isDemo, Bundle.main.bundleIdentifier != nil else { return }
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { _, _ in }
    }

    private func deliverNotification(title: String, message: String, subtitle: String, taskId: String) {
        guard settings.notificationsEnabled, !isDemo, Bundle.main.bundleIdentifier != nil else { return }
        let content = UNMutableNotificationContent()
        content.title = title; content.subtitle = subtitle; content.body = message
        content.userInfo = ["task_id": taskId]
        UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil)) { _ in }
    }

    private var demoListeningConfiguration: ListeningConfiguration {
        ListeningConfiguration(settings: settings,
            approvalGroup: ListeningGroup(["id": settings.approvalGroupId.isEmpty ? "demo-approval" : settings.approvalGroupId,
                                           "title": settings.approvalGroup]),
            senderGroup: ListeningGroup(["id": settings.senderGroupId.isEmpty ? "demo-sender" : settings.senderGroupId,
                                         "title": settings.senderGroup]))
    }

    func loadDemo() {
        listeningConfiguration = demoListeningConfiguration
        dingtalkAccount = AccountConnection(status: .ready, name: "演示账号", detail: "工作空间")
        gitlabAccount = AccountConnection(status: .ready, name: "演示账号", detail: "demo")
        members = ["杨恒", "付家旺", "陈城", "余传今"]
        status = "listening"; connected = true
        let samples: [(String, String, String, String, Int)] = [
            ("demo-1", "杨恒", "completed", "审批完成，已为原消息添加收到", 3),
            ("demo-2", "付家旺", "partial", "1 个 MR 已合并，另 1 个存在冲突", 2),
            ("demo-3", "陈城", "queued", "等待处理", 1),
            ("demo-4", "余传今", "discarded", "手动暂停期间已丢弃", 1)
        ]
        tasks = samples.enumerated().map { index, sample in
            let titles = ["优化订单合并后的库存回写", "补全出库取消回调处理", "调整仓库日志的查询条件"]
            let mrs: [[String: Any]] = (0..<sample.4).map { n in
                let project = "erp/\(n == 0 ? "erp-core" : "erp-warehouse")"
                return ["key": "\(project)!\(60238 + index * 3 + n)",
                 "url": "https://git2.superboss.cc/\(project)/merge_requests/\(60238 + index * 3 + n)",
                 "title": titles[n % titles.count], "status": sample.2 == "partial" && n == 1 ? "failed" : sample.2 == "completed" || sample.2 == "partial" ? "completed" : "waiting",
                 "source_branch": "feature/inventory-writeback", "target_branch": "gray3/261008",
                 "changes_count": String(4 + n * 2), "commit_count": n + 2, "author": sample.1,
                 "actor": "演示账号", "merged_by": "演示账号", "elapsed": Double(6 + n * 2),
                 "sha": "e42a78c", "approved": true,
                 "error": sample.2 == "partial" && n == 1 ? "目标分支存在代码冲突，需要在 GitLab 中解决后重新提交请求。" : ""]
            }
            return ApprovalTask(["id": sample.0, "sender": sample.1,
                "received_at": Date().timeIntervalSince1970 - Double(index * 720 + 80),
                "status": sample.2, "content": "请帮忙处理这些 MR，谢谢。", "mrs": mrs,
                "summary": sample.3, "received": sample.2 == "completed" || (sample.2 == "partial" && settings.receivedCondition == "any_owned_merge"),
                "elapsed": 11.4, "approved_count": sample.4, "merged_count": sample.2 == "partial" ? 1 : sample.4])
        }
        selectedTaskId = tasks.first?.id
        queueCount = 1
        checks = [EnvironmentCheck(id: "demo", title: "演示环境", status: "ready", detail: "模拟任务，仅用于预览交互")]
    }

    func toggleDemoLock() {
        locked.toggle()
        status = paused ? "paused" : locked && !settings.processWhileLocked ? "locked" : "listening"
        if !locked { runDemoQueue() }
    }

    func addDemoMessage() {
        let shouldDiscard = paused ? settings.pausedMessagePolicy == "discard" : locked && !settings.processWhileLocked && settings.lockedMessagePolicy == "discard"
        let id = UUID().uuidString
        tasks.insert(ApprovalTask(["id": id, "sender": "演示请求", "received_at": Date().timeIntervalSince1970,
            "status": shouldDiscard ? "discarded" : "queued", "content": "新增一条演示审批请求",
            "summary": shouldDiscard ? "按当前策略直接丢弃" : "等待处理",
            "mrs": [["key": "erp/erp-core!演示", "url": "", "title": "验证当前执行与消息处置策略", "status": "waiting"]]]), at: 0)
        selectedTaskId = id
        queueCount = tasks.filter(\.waiting).count
        if !shouldDiscard { runDemoQueue() }
    }

    private func runDemoQueue() {
        guard demoRun == nil, !paused, !(locked && !settings.processWhileLocked), isActive else { return }
        let runID = UUID()
        demoRunID = runID
        demoRun = Task { [weak self] in
            guard let self else { return }
            defer {
                if self.demoRunID == runID { self.demoRun = nil; self.activeCount = 0; self.queueCount = self.tasks.filter(\.waiting).count }
            }
            while let id = self.tasks.last(where: \.waiting)?.id {
                guard !Task.isCancelled, !self.paused, !(self.locked && !self.settings.processWhileLocked), let index = self.tasks.firstIndex(where: { $0.id == id }) else { return }
                self.tasks[index].status = "running"; self.tasks[index].summary = "正在处理演示任务"; self.activeCount = 1
                self.queueCount = self.tasks.filter(\.waiting).count
                for stage in ["preflight", "approving", "merging", "completed"] {
                    guard !Task.isCancelled else { return }
                    while self.paused || self.locked && !self.settings.processWhileLocked {
                        try? await Task.sleep(nanoseconds: 200_000_000)
                        if Task.isCancelled { return }
                    }
                    guard let i = self.tasks.firstIndex(where: { $0.id == id }) else { return }
                    for m in self.tasks[i].mrs.indices { self.tasks[i].mrs[m].status = stage }
                    try? await Task.sleep(nanoseconds: 650_000_000)
                }
                guard !Task.isCancelled else { return }
                if let i = self.tasks.firstIndex(where: { $0.id == id }) {
                    self.tasks[i].status = "completed"; self.tasks[i].summary = "演示处理完成"; self.tasks[i].received = self.settings.sendReceived
                }
                self.activeCount = 0; self.queueCount = self.tasks.filter(\.waiting).count
            }
        }
    }
}
