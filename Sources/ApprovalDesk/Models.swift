import Foundation
import SwiftUI

struct AppSettings: Codable, Equatable {
    var schemaVersion = 1
    var profile = ""
    var approvalGroup = "代码审批"
    var approvalGroupId = ""
    var senderGroup = "向钱冲"
    var senderGroupId = ""
    var gitlabHost = "git2.superboss.cc"
    var gitlabToken = ""
    var fallbackToken = ""
    var dwsPath = ""
    var glabPath = ""
    var processWhileLocked = false
    var lockedMessagePolicy = "discard"
    var pausedMessagePolicy = "discard"
    var queueCapacity = 100
    var queueOverflowPolicy = "drop_oldest"
    var maxQueueAgeSeconds = 0
    var maxConcurrentMrs = 3
    var maxMrsPerMessage = 30
    var parseQuotedMessages = true
    var markRead = true
    var sendReceived = true
    var receivedCondition = "all_succeeded_and_owned"
    var includeFallbackForReceived = true
    var useFallbackForOwnMr = true
    var commandTimeoutSeconds = 90
    var mergeTimeoutSeconds = 60
    var mergePollSeconds = 2
    var maxAttempts = 3
    var retryDelays = [2, 5]
    var reconnectDelays = [2, 5, 10, 20, 30]
    var notificationsEnabled = true
    var notifyOnRequest = true
    var notifyOnSuccess = true
    var notifyOnFailure = true
    var launchAtLogin = true
    var startOnLaunch = true
    var showWorkbenchOnLaunch = false
    var appearance = "system"
    var activityLimit = 300
    var debugOutput = false

    static var encoder: JSONEncoder {
        let result = JSONEncoder()
        result.keyEncodingStrategy = .convertToSnakeCase
        result.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
        return result
    }
    func dictionary() -> [String: Any] {
        (try? JSONSerialization.jsonObject(with: Self.encoder.encode(self))) as? [String: Any] ?? [:]
    }
    static func load(from url: URL) throws -> AppSettings {
        guard FileManager.default.fileExists(atPath: url.path) else { return AppSettings() }
        let source = try JSONSerialization.jsonObject(with: Data(contentsOf: url))
        guard let dictionary = source as? [String: Any] else {
            throw NSError(domain: "ApprovalDesk", code: 1, userInfo: [NSLocalizedDescriptionKey: "配置文件需要是 JSON 对象"])
        }
        let combined = AppSettings().dictionary().merging(dictionary) { _, new in new }
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let settings = try decoder.decode(AppSettings.self, from: JSONSerialization.data(withJSONObject: combined))
        if let issue = settings.validationIssue {
            throw NSError(domain: "ApprovalDesk", code: 2, userInfo: [NSLocalizedDescriptionKey: issue])
        }
        return settings
    }
    func save(to url: URL) throws {
        if let issue = validationIssue {
            throw NSError(domain: "ApprovalDesk", code: 2, userInfo: [NSLocalizedDescriptionKey: issue])
        }
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Self.encoder.encode(self).write(to: url, options: .atomic)
    }
    var validationIssue: String? {
        let options: [(String, Set<String>)] = [
            (lockedMessagePolicy, ["queue", "discard"]), (pausedMessagePolicy, ["queue", "discard"]),
            (queueOverflowPolicy, ["reject_new", "drop_oldest"]),
            (receivedCondition, ["any_owned_merge", "all_succeeded_and_owned"]),
            (appearance, ["system", "light", "dark"])
        ]
        if options.contains(where: { !$0.1.contains($0.0) }) { return "配置中包含不支持的选项值" }
        if approvalGroup.trimmingCharacters(in: .whitespaces).isEmpty || senderGroup.trimmingCharacters(in: .whitespaces).isEmpty { return "请填写审批群和人员来源群" }
        if gitlabHost.isEmpty || gitlabHost.contains(":") || gitlabHost.contains("/") || gitlabHost.contains(" ") { return "GitLab 地址只填写主机名，例如 git2.superboss.cc" }
        let ranges: [(Int, ClosedRange<Int>, String)] = [
            (queueCapacity, 1...10000, "队列容量"), (maxQueueAgeSeconds, 0...604800, "最长等待时间"),
            (maxConcurrentMrs, 1...16, "并发数量"), (maxMrsPerMessage, 1...200, "单条消息 MR 上限"),
            (commandTimeoutSeconds, 5...600, "命令超时"), (mergeTimeoutSeconds, 5...3600, "合并确认窗口"),
            (mergePollSeconds, 1...60, "合并查询间隔"), (maxAttempts, 1...10, "最大尝试次数"),
            (activityLimit, 50...3000, "活动保留数量")
        ]
        for (value, range, name) in ranges where !range.contains(value) { return "\(name)需在 \(range.lowerBound)–\(range.upperBound) 之间" }
        for values in [retryDelays, reconnectDelays] where values.isEmpty || values.count > 10 || values.contains(where: { !(1...300).contains($0) }) { return "等待间隔需要 1–10 个 1–300 秒的数值" }
        return nil
    }
    var connectionSignature: String {
        // Group selections are compared with the identities resolved by the listener.
        [profile, gitlabHost, gitlabToken, fallbackToken, dwsPath, glabPath].joined(separator: "\u{1F}")
    }
    var accountSignature: String {
        [profile, gitlabHost, gitlabToken, dwsPath, glabPath].joined(separator: "\u{1F}")
    }
}

struct ListeningGroup: Equatable {
    var id: String
    var title: String

    init?(_ value: Any?) {
        guard let data = value as? [String: Any], let id = data["id"] as? String, !id.isEmpty,
              let title = data["title"] as? String else { return nil }
        self.id = id
        self.title = title
    }
}

struct ListeningConfiguration: Equatable {
    let settings: AppSettings
    var approvalGroup: ListeningGroup?
    var senderGroup: ListeningGroup?

    func needsRestart(for next: AppSettings) -> Bool {
        settings.connectionSignature != next.connectionSignature ||
        !matches(name: next.approvalGroup, id: next.approvalGroupId,
                 originalName: settings.approvalGroup, originalID: settings.approvalGroupId, resolved: approvalGroup) ||
        !matches(name: next.senderGroup, id: next.senderGroupId,
                 originalName: settings.senderGroup, originalID: settings.senderGroupId, resolved: senderGroup)
    }

    private func matches(name: String, id: String, originalName: String, originalID: String, resolved: ListeningGroup?) -> Bool {
        if !id.isEmpty { return id == (resolved?.id ?? originalID) }
        let name = name.trimmingCharacters(in: .whitespacesAndNewlines)
        return name == originalName.trimmingCharacters(in: .whitespacesAndNewlines) ||
               name == resolved?.title.trimmingCharacters(in: .whitespacesAndNewlines)
    }
}

enum AccountStatus: String { case unchecked, checking, ready, error }

struct AccountConnection: Equatable {
    var status: AccountStatus = .unchecked
    var name = ""
    var detail = ""

    var displayName: String {
        switch status {
        case .unchecked: "等待读取"
        case .checking: "正在读取…"
        case .ready: name.isEmpty ? "已登录" : name
        case .error: "读取失败"
        }
    }
    var explanation: String {
        switch status {
        case .unchecked: "打开应用后自动读取当前登录账号"
        case .checking: "正在读取当前登录账号"
        case .ready: detail
        case .error: detail.isEmpty ? "暂时无法读取账号，请刷新重试" : detail
        }
    }
}

struct MRItem: Identifiable, Equatable {
    var id: String { key }
    var key: String
    var url: String
    var title = ""
    var status = "waiting"
    var sourceBranch = ""
    var targetBranch = ""
    var changesCount = ""
    var commitCount: Int?
    var author = ""
    var actor = ""
    var mergedBy = ""
    var sha = ""
    var error = ""
    var elapsed: Double = 0
    var approved = false
    var ownedMerge = false
    var createdAt = ""
    var updatedAt = ""
    var mergeSummary: String {
        mergedBy.isEmpty ? "已合并 · GitLab 未返回合并人" : "已由 \(mergedBy) 合并"
    }

    init(_ data: [String: Any]) {
        key = data["key"] as? String ?? ""
        url = data["url"] as? String ?? ""
        merge(data)
    }
    mutating func merge(_ data: [String: Any]) {
        if let v = data["url"] as? String, !v.isEmpty { url = v }
        if let v = data["title"] as? String { title = v }
        if let v = data["status"] as? String { status = v }
        if let v = data["source_branch"] as? String { sourceBranch = v }
        if let v = data["target_branch"] as? String { targetBranch = v }
        if let v = data["changes_count"] as? String { changesCount = v }
        if let v = data["commit_count"] as? Int { commitCount = v }
        if let v = data["author"] as? String { author = v }
        if let v = data["actor"] as? String { actor = v }
        if let v = data["merged_by"] as? String { mergedBy = v }
        if let v = data["sha"] as? String { sha = v }
        if let v = data["error"] as? String { error = v }
        if let v = data["elapsed"] as? Double { elapsed = v }
        if let v = data["approved"] as? Bool { approved = v }
        if let v = data["owned_merge"] as? Bool { ownedMerge = v }
        if let v = data["created_at"] as? String { createdAt = v }
        if let v = data["updated_at"] as? String { updatedAt = v }
    }
}

struct ApprovalTask: Identifiable, Equatable {
    var id: String
    var sender: String
    var senderAvatarURL = ""
    var receivedAt: Date
    var status = "queued"
    var content = ""
    var summary = ""
    var error = ""
    var mrs: [MRItem] = []
    var received = false
    var quoted = false
    var elapsed: Double = 0
    var approvedCount = 0
    var mergedCount = 0

    init(_ data: [String: Any]) {
        id = data["id"] as? String ?? UUID().uuidString
        sender = data["sender"] as? String ?? "未知发送人"
        receivedAt = Date(timeIntervalSince1970: data["received_at"] as? Double ?? Date().timeIntervalSince1970)
        content = data["content"] as? String ?? ""
        mrs = (data["mrs"] as? [[String: Any]] ?? []).map(MRItem.init)
        quoted = data["quoted"] as? Bool ?? false
        merge(data)
    }
    mutating func merge(_ data: [String: Any]) {
        if let v = data["sender_avatar_url"] as? String { senderAvatarURL = v }
        if let v = data["status"] as? String { status = v }
        if let v = data["summary"] as? String { summary = v }
        if let v = data["error"] as? String { error = v }
        if let v = data["received"] as? Bool { received = v }
        if let v = data["elapsed"] as? Double { elapsed = v }
        if let v = data["approved_count"] as? Int { approvedCount = v }
        if let v = data["merged_count"] as? Int { mergedCount = v }
    }
    var hasErrors: Bool { ["failed", "partial", "invalid"].contains(status) }
    var waiting: Bool { status == "queued" }
    var running: Bool { status == "running" }
}

struct ActivityLog: Identifiable {
    let id = UUID()
    var date = Date()
    var level: String
    var message: String
    var taskId: String
    var mrKey: String
}

struct EnvironmentCheck: Identifiable {
    var id: String
    var title: String
    var status: String
    var detail: String
}

struct GroupOption: Identifiable {
    var id: String
    var title: String
    var count: Int
}

struct ProfileOption: Identifiable {
    var id: String
    var name: String
    var organization: String
    var title: String { [name, organization].filter { !$0.isEmpty }.joined(separator: " · ") }
}

enum TaskFilter: String, CaseIterable, Identifiable {
    case all, queued, completed
    var id: String { rawValue }
    var title: String {
        switch self { case .all: "全部请求"; case .queued: "待处理"; case .completed: "已完成" }
    }
    var icon: String {
        switch self { case .all: "tray.full"; case .queued: "clock"; case .completed: "checkmark.circle" }
    }
    func includes(_ task: ApprovalTask) -> Bool {
        switch self { case .all: true; case .queued: task.waiting || task.running; case .completed: task.status == "completed" }
    }
}

func statusTitle(_ status: String) -> String {
    ["idle": "尚未监听", "ready": "准备就绪", "checking": "正在检查", "starting": "正在连接",
     "listening": "正在监听", "paused": "已手动暂停", "locked": "等待解锁", "reconnecting": "正在重连",
     "stopping": "正在停止", "stopped": "已停止", "error": "连接异常", "queued": "等待处理",
     "running": "处理中", "completed": "已完成", "partial": "部分完成", "failed": "处理失败",
     "discarded": "已跳过", "invalid": "消息无效", "waiting": "等待开始", "preflight": "检查 MR",
     "approving": "正在审批", "merging": "正在合并"][status] ?? status
}

extension Color {
    static let ink = Color(nsColor: NSColor(name: "ApprovalDeskInk") { appearance in
        appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
            ? NSColor(white: 0.94, alpha: 1)
            : NSColor(white: 0.12, alpha: 1)
    })
    static let action = Color(nsColor: NSColor(name: "ApprovalDeskAction") { appearance in
        appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
            ? NSColor(white: 0.88, alpha: 1)
            : NSColor(white: 0.19, alpha: 1)
    })
    static let brand = Color.ink
    static let canvas = Color(nsColor: .windowBackgroundColor)
    static let surface = Color(nsColor: .controlBackgroundColor)
    static let subtle = Color.primary.opacity(0.035)
}

func statusColor(_ status: String) -> Color {
    if status == "completed" { return .primary.opacity(0.72) }
    if ["listening", "ready"].contains(status) { return .blue }
    if ["failed", "error", "invalid"].contains(status) { return .red }
    if ["partial", "paused", "locked", "reconnecting"].contains(status) { return .orange }
    if ["running", "preflight", "approving", "merging", "starting", "checking"].contains(status) { return .blue }
    return .secondary
}
