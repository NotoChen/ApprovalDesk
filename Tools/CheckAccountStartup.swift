import Foundation
import ServiceManagement

// Account probes never register or unregister the installed app's login item.
@MainActor
private final class ProbeLoginItemManager: LoginItemManaging {
    var status: SMAppService.Status = .notRegistered
    func setEnabled(_ enabled: Bool) async throws { status = enabled ? .enabled : .notRegistered }
}

// Uses the production AppModel and EngineClient, with a read-only command gate.
// The probe bundle points its Resources at the installed application's resources.
private final class StartupProbeConnection: EngineConnection {
    private let engine = EngineClient()
    var onEvent: (([String: Any]) -> Void)?
    var onExit: (() -> Void)?
    var isRunning: Bool { engine.isRunning }
    var commands: [String] = []

    init() {
        engine.onEvent = { [weak self] event in self?.onEvent?(event) }
        engine.onExit = { [weak self] in self?.onExit?() }
    }
    func launch(dataDirectory: URL) throws { try engine.launch(dataDirectory: dataDirectory) }
    func send(_ command: String, settings: AppSettings?, values: [String: Any]) {
        precondition(["configure", "accounts", "profiles", "groups"].contains(command), "Startup probe received unexpected command: \(command)")
        commands.append(command)
        engine.send(command, settings: settings, values: values)
    }
    func shutdown() { engine.shutdown() }
}

@main
struct CheckAccountStartup {
    @MainActor static func main() async throws {
        let config = URL(fileURLWithPath: CommandLine.arguments[1])
        let checkGroups = CommandLine.arguments.dropFirst(2).contains("--groups")
        guard FileManager.default.fileExists(atPath: config.path) else { throw failure("配置文件不存在") }
        let settings = try AppSettings.load(from: config)
        guard !settings.startOnLaunch else { throw failure("此只读验证要求当前配置关闭自动监听") }
        let connection = StartupProbeConnection()
        let model = AppModel(configURL: config, demo: false, client: connection, loginItem: ProbeLoginItemManager())
        defer { if connection.isRunning { connection.shutdown() } }
        model.launch()
        // Reproduce opening Settings while the worker is still starting.
        model.loadProfiles()
        let timeout = Double(settings.commandTimeoutSeconds * settings.maxAttempts + settings.retryDelays.reduce(0, +) + 15)
        try await waitUntilIdle(model, timeout: timeout)
        var groupReports: [[String: Any]] = []
        if checkGroups {
            for (kind, query) in [("approval", settings.approvalGroup), ("sender", settings.senderGroup)] {
                model.beginGroupSearch(kind: kind, query: query)
                try await waitUntilIdle(model, timeout: timeout)
                if let error = model.errorMessage { throw failure("\(query)：\(error)") }
                guard !model.searchingGroups, !model.groupOptions.isEmpty else { throw failure("\(query)：未收到群聊候选列表") }
                groupReports.append([
                    "kind": kind, "query": query, "candidate_count": model.groupOptions.count,
                    "exact_matches": model.groupOptions.filter { $0.title == query }.map { $0.title }
                ])
                // Reading candidates does not select a group or change settings.
                model.showGroupPicker = false
            }
        }
        let report: [String: Any] = [
            "dingtalk": ["status": model.dingtalkAccount.status.rawValue, "name": model.dingtalkAccount.displayName, "detail": model.dingtalkAccount.explanation],
            "gitlab": ["status": model.gitlabAccount.status.rawValue, "name": model.gitlabAccount.displayName, "detail": model.gitlabAccount.explanation],
            "listener_connected": model.connected,
            "listener_status": model.status,
            "task_count": model.tasks.count,
            "profile_count": model.profileOptions.count,
            "commands": connection.commands,
            "group_searches": groupReports,
        ]
        print(String(decoding: try JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted, .sortedKeys]), as: UTF8.self))
        guard model.dingtalkAccount.status == .ready, model.gitlabAccount.status == .ready,
              !model.connected, model.tasks.isEmpty,
              connection.commands == ["configure", "accounts", "profiles"] + (checkGroups ? ["groups", "groups"] : []) else {
            throw failure("原生启动状态验证未通过")
        }
        var exited = false
        model.requestQuit { exited = true }
        let exitDeadline = Date().addingTimeInterval(10)
        while !exited {
            if Date() >= exitDeadline { throw failure("执行引擎未正常退出") }
            try await Task.sleep(for: .milliseconds(50))
        }
        print("Native account startup passed; worker exited cleanly")
    }

    @MainActor static func waitUntilIdle(_ model: AppModel, timeout: TimeInterval) async throws {
        let deadline = Date().addingTimeInterval(timeout)
        while !model.engineAvailable || model.isBusy {
            if Date() >= deadline { throw failure("等待原生只读操作超时") }
            if model.status == "error" && !model.engineAvailable { throw failure(model.errorMessage ?? "执行引擎未启动") }
            try await Task.sleep(for: .milliseconds(50))
        }
    }

    static func failure(_ message: String) -> NSError {
        NSError(domain: "ApprovalDeskStartupProbe", code: 1, userInfo: [NSLocalizedDescriptionKey: message])
    }
}
