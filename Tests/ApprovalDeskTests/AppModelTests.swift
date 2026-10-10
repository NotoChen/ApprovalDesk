#if canImport(XCTest)
import XCTest
@testable import ApprovalDesk
#else
import Foundation
#endif
import ServiceManagement

@MainActor
private final class FakeLoginItemManager: LoginItemManaging {
    var status: SMAppService.Status = .notRegistered
    var requests: [Bool] = []
    var error: Error?
    var requiresApproval = false

    func setEnabled(_ enabled: Bool) async throws {
        requests.append(enabled)
        if let error { throw error }
        status = enabled ? (requiresApproval ? .requiresApproval : .enabled) : .notRegistered
    }
}

private final class FakeEngineConnection: EngineConnection {
    struct Command {
        var name: String
        var settings: AppSettings?
        var values: [String: Any]
    }
    var onEvent: (([String: Any]) -> Void)?
    var onExit: (() -> Void)?
    var isRunning = false
    var launchError: Error?
    var launchCount = 0
    var sent: [Command] = []
    var commands: [String] { sent.map(\.name) }
    var accountRequestID: String { sent.last(where: { $0.name == "accounts" })?.values["request_id"] as? String ?? "" }
    var groupRequestID: String { sent.last(where: { $0.name == "groups" })?.values["request_id"] as? String ?? "" }
    var startRequestID: String { sent.last(where: { $0.name == "start" })?.values["request_id"] as? String ?? "" }

    func launch(dataDirectory: URL) throws {
        launchCount += 1
        if let launchError { throw launchError }
        isRunning = true
    }
    func send(_ command: String, settings: AppSettings?, values: [String: Any]) {
        if isRunning { sent.append(Command(name: command, settings: settings, values: values)) }
    }
    func shutdown() { send("shutdown", settings: nil, values: [:]) }
}

@MainActor
final class AppModelTests: XCTestCase {
    private func fixture(autoStart: Bool = false, loginItem: FakeLoginItemManager? = nil) throws -> (AppModel, FakeEngineConnection, URL) {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        let config = folder.appendingPathComponent("settings.json")
        var settings = AppSettings()
        settings.startOnLaunch = autoStart
        settings.notificationsEnabled = false
        try settings.save(to: config)
        let client = FakeEngineConnection()
        return (AppModel(configURL: config, demo: false, client: client, loginItem: loginItem ?? FakeLoginItemManager()), client, folder)
    }

    private func ready(_ model: AppModel, _ client: FakeEngineConnection) {
        model.receive(["type": "identity", "service": "dingtalk", "name": "钉钉用户", "detail": "测试组织", "request_id": client.accountRequestID])
        model.receive(["type": "identity", "service": "gitlab", "name": "GitLab 用户", "detail": "gitlab-user", "request_id": client.accountRequestID])
    }

    private func finishAccounts(_ model: AppModel, _ client: FakeEngineConnection) {
        model.receive(["type": "operation_finished", "command": "accounts", "request_id": client.accountRequestID])
    }

    private func listeningEvent(_ client: FakeEngineConnection) -> [String: Any] {
        let settings = client.sent.last(where: { $0.name == "start" })?.settings ?? AppSettings()
        return ["type": "listening_configuration", "request_id": client.startRequestID,
                "approval_group": ["id": settings.approvalGroupId.isEmpty ? "current-approval" : settings.approvalGroupId,
                                   "title": settings.approvalGroup],
                "sender_group": ["id": settings.senderGroupId.isEmpty ? "current-sender" : settings.senderGroupId,
                                 "title": settings.senderGroup]]
    }

    private func chooseGroup(_ model: AppModel, _ client: FakeEngineConnection, kind: String, id: String, title: String) {
        model.beginGroupSearch(kind: kind, query: title)
        model.receive(["type": "groups", "request_id": client.groupRequestID, "kind": kind,
                       "groups": [["id": id, "title": title, "count": 9]]])
        model.receive(["type": "operation_finished", "command": "groups", "request_id": client.groupRequestID])
        XCTAssertEqual(model.groupOptions.count, 1)
        model.selectGroup(model.groupOptions[0])
    }

    func testStartupReadsAccountsWithoutStartingListener() throws {
        let (model, client, folder) = try fixture()
        defer { try? FileManager.default.removeItem(at: folder) }
        model.launch()
        XCTAssertTrue(client.commands.isEmpty)
        XCTAssertEqual(model.dingtalkAccount.status, .checking)
        model.receive(["type": "hello"])
        XCTAssertEqual(client.commands, ["configure", "accounts"])
        ready(model, client)
        XCTAssertEqual(model.dingtalkAccount.displayName, "钉钉用户")
        XCTAssertEqual(model.gitlabAccount.explanation, "gitlab-user")
        XCTAssertTrue(model.isBusy)
        finishAccounts(model, client)
        XCTAssertEqual(model.isBusy, false)
        XCTAssertEqual(model.connected, false)
        XCTAssertEqual(model.status, "idle")
        XCTAssertEqual(client.commands.contains("start"), false)
    }

    func testOneAccountFailureKeepsOtherIdentityAndErrorDetail() throws {
        let (model, client, folder) = try fixture()
        defer { try? FileManager.default.removeItem(at: folder) }
        model.launch(); model.receive(["type": "hello"])
        model.receive(["type": "account_state", "service": "dingtalk", "status": "error", "detail": "请求超时", "request_id": client.accountRequestID])
        model.receive(["type": "identity", "service": "gitlab", "name": "GitLab 用户", "request_id": client.accountRequestID])
        finishAccounts(model, client)
        XCTAssertEqual(model.dingtalkAccount.displayName, "读取失败")
        XCTAssertEqual(model.dingtalkAccount.explanation, "请求超时")
        XCTAssertEqual(model.gitlabAccount.status, .ready)
        XCTAssertEqual(model.isBusy, false)
    }

    func testOpeningSettingsDuringStartupDefersProfileLoadUntilOperationFinishes() throws {
        let (model, client, folder) = try fixture()
        defer { try? FileManager.default.removeItem(at: folder) }
        model.launch(); model.loadProfiles(); model.receive(["type": "hello"])
        ready(model, client)
        XCTAssertEqual(client.commands, ["configure", "accounts"])
        finishAccounts(model, client)
        XCTAssertEqual(client.commands, ["configure", "accounts", "profiles"])
        model.receive(["type": "profiles", "profiles": [["id": "corp:user", "name": "钉钉用户", "organization": "测试组织"]]])
        XCTAssertTrue(model.isBusy)
        model.receive(["type": "operation_finished", "command": "profiles"])
        XCTAssertEqual(model.isBusy, false)
        XCTAssertEqual(model.profileOptions.first?.id, "corp:user")
    }

    func testAutoStartWaitsForBothAccountsAndReleasedOperation() throws {
        let (model, client, folder) = try fixture(autoStart: true)
        defer { try? FileManager.default.removeItem(at: folder) }
        model.launch(); model.receive(["type": "hello"])
        ready(model, client)
        XCTAssertEqual(client.commands, ["configure", "accounts"])
        finishAccounts(model, client)
        XCTAssertEqual(client.commands, ["configure", "accounts", "start"])
        XCTAssertEqual(model.status, "starting")
        XCTAssertEqual(client.commands.filter { $0 == "start" }.count, 1)
    }

    func testMenuOnlyStartupStartsOnceWithoutShowingWorkbench() throws {
        let (model, client, folder) = try fixture(autoStart: true)
        defer { try? FileManager.default.removeItem(at: folder) }
        var opened = 0
        // The delegate can launch before any view has been displayed.
        model.launch(); model.launch(); model.receive(["type": "hello"])
        ready(model, client); finishAccounts(model, client)
        model.installMainWindowOpener { opened += 1 }
        model.installMainWindowOpener { opened += 1 }
        XCTAssertEqual(client.launchCount, 1)
        XCTAssertEqual(client.commands.filter { $0 == "start" }.count, 1)
        XCTAssertEqual(opened, 0)
        model.openMainWindow?()
        XCTAssertEqual(opened, 1)
    }

    func testWorkbenchLaunchOptInOpensOnceAndKeepsManualListeningChoice() throws {
        let (model, client, folder) = try fixture()
        defer { try? FileManager.default.removeItem(at: folder) }
        model.settings.showWorkbenchOnLaunch = true
        var opened = 0
        model.installMainWindowOpener { opened += 1 }
        model.installMainWindowOpener { opened += 1 }
        model.launch(); model.receive(["type": "hello"])
        ready(model, client); finishAccounts(model, client)
        XCTAssertEqual(opened, 1)
        XCTAssertEqual(client.commands.contains("start"), false)
        XCTAssertEqual(try AppSettings.load(from: model.configURL).showWorkbenchOnLaunch, true)
    }

    private func waitForLoginItem(_ model: AppModel) async throws {
        let deadline = Date().addingTimeInterval(2)
        while model.updatingLoginItem {
            precondition(Date() < deadline, "Login item update did not finish")
            try await Task.sleep(for: .milliseconds(5))
        }
    }

    func testStartupRegistersLoginItemAndDisablingItPersists() async throws {
        let loginItem = FakeLoginItemManager()
        let (model, _, folder) = try fixture(loginItem: loginItem)
        defer { try? FileManager.default.removeItem(at: folder) }
        model.launch()
        try await waitForLoginItem(model)
        XCTAssertEqual(loginItem.requests, [true])
        XCTAssertEqual(model.loginItemStatus, .enabled)
        XCTAssertEqual(model.loginItemIssue, nil)
        model.setLaunchAtLogin(false)
        try await waitForLoginItem(model)
        XCTAssertEqual(loginItem.requests, [true, false])
        XCTAssertEqual(model.loginItemStatus, .notRegistered)
        XCTAssertEqual(try AppSettings.load(from: model.configURL).launchAtLogin, false)
    }

    func testLoginItemFailureDoesNotBlockAutoListening() async throws {
        let loginItem = FakeLoginItemManager()
        loginItem.error = NSError(domain: "Fixture", code: 1, userInfo: [NSLocalizedDescriptionKey: "无法注册"])
        let (model, client, folder) = try fixture(autoStart: true, loginItem: loginItem)
        defer { try? FileManager.default.removeItem(at: folder) }
        model.launch(); model.receive(["type": "hello"])
        ready(model, client); finishAccounts(model, client)
        try await waitForLoginItem(model)
        XCTAssertEqual(model.loginItemStatus, .notRegistered)
        XCTAssertTrue(model.loginItemDetail.contains("无法注册"))
        XCTAssertEqual(client.commands.last, "start")
        XCTAssertTrue(model.settings.launchAtLogin)
    }

    func testPendingLoginApprovalIsShownAsPending() async throws {
        let loginItem = FakeLoginItemManager()
        loginItem.requiresApproval = true
        let (model, _, folder) = try fixture(loginItem: loginItem)
        defer { try? FileManager.default.removeItem(at: folder) }
        model.launch()
        try await waitForLoginItem(model)
        XCTAssertEqual(model.loginItemStatus, .requiresApproval)
        XCTAssertTrue(model.loginItemDetail.contains("请在系统设置"))
    }

    func testPendingProfilesLoadBeforeAutoStartAndListeningDoesNotStayBusy() throws {
        let (model, client, folder) = try fixture(autoStart: true)
        defer { try? FileManager.default.removeItem(at: folder) }
        model.launch(); model.loadProfiles(); model.receive(["type": "hello"])
        ready(model, client); finishAccounts(model, client)
        XCTAssertEqual(client.commands.last, "profiles")
        model.receive(["type": "profiles", "profiles": []])
        model.receive(["type": "operation_finished", "command": "profiles"])
        XCTAssertEqual(client.commands.last, "start")
        model.receive(["type": "state", "status": "listening", "connected": true])
        XCTAssertEqual(model.isBusy, false)
        model.start()
        XCTAssertEqual(client.commands.filter { $0 == "start" }.count, 1)
    }

    func testChangedProfileRejectsOldResultsAndRefreshesAfterDebounce() async throws {
        let (model, client, folder) = try fixture()
        defer { try? FileManager.default.removeItem(at: folder) }
        model.launch(); model.receive(["type": "hello"])
        let previousID = client.accountRequestID
        model.selectProfile("new-corp:new-user")
        model.receive(["type": "identity", "service": "dingtalk", "name": "过期账号", "request_id": previousID])
        XCTAssertEqual(model.dingtalkAccount.status, .checking)
        finishAccounts(model, client)
        try await Task.sleep(for: .milliseconds(650))
        XCTAssertEqual(client.commands.filter { $0 == "accounts" }.count, 2)
        XCTAssertEqual(client.sent.last?.settings?.profile, "new-corp:new-user")
        model.receive(["type": "identity", "service": "dingtalk", "name": "过期账号", "request_id": previousID])
        model.receive(["type": "operation_finished", "command": "accounts", "request_id": previousID])
        XCTAssertEqual(model.dingtalkAccount.status, .checking)
        XCTAssertTrue(model.isBusy)
        ready(model, client); finishAccounts(model, client)
        XCTAssertEqual(model.dingtalkAccount.displayName, "钉钉用户")
        XCTAssertEqual(model.isBusy, false)
    }

    func testWorkerExitEndsLoadingAndRefreshRestartsHandshake() throws {
        let (model, client, folder) = try fixture()
        defer { try? FileManager.default.removeItem(at: folder) }
        model.launch(); model.receive(["type": "hello"])
        client.isRunning = false
        model.receive(["type": "worker_exit"])
        XCTAssertEqual(model.isBusy, false)
        XCTAssertEqual(model.dingtalkAccount.status, .error)
        XCTAssertEqual(model.gitlabAccount.status, .error)
        model.refreshAccounts()
        XCTAssertEqual(client.launchCount, 2)
        model.receive(["type": "hello"])
        ready(model, client); finishAccounts(model, client)
        XCTAssertEqual(model.gitlabAccount.status, .ready)
        XCTAssertEqual(model.status, "idle")
        XCTAssertEqual(model.errorMessage, nil)
        XCTAssertEqual(client.commands.contains("start"), false)
    }

    func testWorkerLaunchFailureDoesNotLeaveAccountsLoading() throws {
        let (model, client, folder) = try fixture()
        defer { try? FileManager.default.removeItem(at: folder) }
        client.launchError = NSError(domain: "Fixture", code: 1, userInfo: [NSLocalizedDescriptionKey: "组件缺失"])
        model.launch()
        XCTAssertEqual(model.isBusy, false)
        XCTAssertEqual(model.dingtalkAccount.status, .error)
        XCTAssertEqual(model.gitlabAccount.explanation, "组件缺失")
    }

    func testSuccessfulLoginRefreshesAccountsAfterOperationIsReleased() throws {
        let (model, client, folder) = try fixture()
        defer { try? FileManager.default.removeItem(at: folder) }
        model.launch(); model.receive(["type": "hello"])
        ready(model, client); finishAccounts(model, client)
        model.login("dingtalk")
        model.receive(["type": "auth", "service": "dingtalk", "status": "completed"])
        XCTAssertEqual(client.commands.last, "login")
        model.receive(["type": "operation_finished", "command": "login"])
        XCTAssertEqual(client.commands.last, "accounts")
        ready(model, client); finishAccounts(model, client)
        XCTAssertEqual(client.commands.last, "profiles")
    }

    func testManualCheckAfterEngineExitWaitsForStartupRead() throws {
        let (model, client, folder) = try fixture()
        defer { try? FileManager.default.removeItem(at: folder) }
        model.launch(); model.receive(["type": "hello"])
        ready(model, client); finishAccounts(model, client)
        client.isRunning = false; model.receive(["type": "worker_exit"])
        model.checkEnvironment()
        XCTAssertEqual(client.commands.contains("check"), false)
        model.receive(["type": "hello"])
        ready(model, client); finishAccounts(model, client)
        XCTAssertEqual(client.commands.last, "check")
        XCTAssertEqual(client.commands.contains("start"), false)
    }

    func testQuitDuringStartupDoesNotRunPendingAutoStartOrProfiles() throws {
        let (model, client, folder) = try fixture(autoStart: true)
        defer { try? FileManager.default.removeItem(at: folder) }
        model.launch(); model.receive(["type": "hello"]); model.loadProfiles()
        model.requestQuit({})
        ready(model, client); finishAccounts(model, client)
        XCTAssertEqual(client.commands.contains("start"), false)
        XCTAssertEqual(client.commands.contains("profiles"), false)
        XCTAssertEqual(client.commands.last, "shutdown")
    }

    func testRejectedAccountReadCanBeRetriedWithoutStuckLoading() throws {
        let (model, client, folder) = try fixture()
        defer { try? FileManager.default.removeItem(at: folder) }
        model.launch(); model.receive(["type": "hello"])
        model.receive(["type": "error", "command": "accounts", "operation_rejected": true, "message": "无法连接执行引擎"])
        XCTAssertEqual(model.isBusy, false)
        XCTAssertEqual(model.dingtalkAccount.status, .error)
        model.refreshAccounts()
        XCTAssertEqual(client.commands.filter { $0 == "accounts" }.count, 2)
    }

    func testBothGroupSelectorsDisplayCandidatesAndSaveTheSelectedGroup() throws {
        for kind in ["approval", "sender"] {
            let (model, client, folder) = try fixture()
            defer { try? FileManager.default.removeItem(at: folder) }
            model.launch(); model.receive(["type": "hello"])
            ready(model, client); finishAccounts(model, client)
            let previous = model.settings
            model.beginGroupSearch(kind: kind, query: "同名群")
            XCTAssertEqual(client.commands.last, "groups")
            XCTAssertEqual(client.sent.last?.values["kind"] as? String, kind)
            XCTAssertTrue(model.showGroupPicker)
            XCTAssertTrue(model.searchingGroups)
            model.receive(["type": "groups", "request_id": client.groupRequestID, "kind": kind, "groups": [
                ["id": "group-1", "title": "同名群", "count": 3],
                ["id": "group-2", "title": "同名群", "count": 9]
            ]])
            model.receive(["type": "operation_finished", "command": "groups", "request_id": client.groupRequestID])
            XCTAssertEqual(model.searchingGroups, false)
            XCTAssertEqual(model.groupOptions.count, 2)
            model.selectGroup(model.groupOptions[1])
            let saved = try AppSettings.load(from: model.configURL)
            if kind == "approval" {
                XCTAssertEqual(saved.approvalGroupId, "group-2")
                XCTAssertEqual(saved.approvalGroup, "同名群")
                XCTAssertEqual(saved.senderGroupId, previous.senderGroupId)
                XCTAssertEqual(saved.senderGroup, previous.senderGroup)
            } else {
                XCTAssertEqual(saved.senderGroupId, "group-2")
                XCTAssertEqual(saved.senderGroup, "同名群")
                XCTAssertEqual(saved.approvalGroupId, previous.approvalGroupId)
                XCTAssertEqual(saved.approvalGroup, previous.approvalGroup)
            }
            XCTAssertEqual(model.showGroupPicker, false)
            XCTAssertEqual(client.commands.contains("start"), false)
        }
    }

    func testFailedGroupSearchCanBeRetriedAndAnEmptyResultEndsLoading() throws {
        let (model, client, folder) = try fixture()
        defer { try? FileManager.default.removeItem(at: folder) }
        model.launch(); model.receive(["type": "hello"])
        ready(model, client); finishAccounts(model, client)
        model.beginGroupSearch(kind: "approval", query: "测试群")
        model.receive(["type": "error", "command": "groups", "request_id": client.groupRequestID, "message": "群聊搜索请求超时"])
        model.receive(["type": "operation_finished", "command": "groups", "request_id": client.groupRequestID])
        XCTAssertEqual(model.isBusy, false)
        XCTAssertEqual(model.searchingGroups, false)
        XCTAssertEqual(model.errorMessage, "群聊搜索请求超时")
        model.beginGroupSearch(kind: "approval", query: "没有匹配的群")
        XCTAssertEqual(model.errorMessage, nil)
        model.receive(["type": "groups", "kind": "approval", "request_id": client.groupRequestID, "groups": []])
        model.receive(["type": "operation_finished", "command": "groups", "request_id": client.groupRequestID])
        XCTAssertEqual(model.isBusy, false)
        XCTAssertEqual(model.searchingGroups, false)
        XCTAssertTrue(model.groupOptions.isEmpty)
    }

    func testGroupSelectionWhileActiveSavesForNextStartAndKeepsTasks() throws {
        for state in ["listening", "paused", "locked", "reconnecting"] {
            for kind in ["approval", "sender"] {
                let (model, client, folder) = try fixture(autoStart: true)
                defer { try? FileManager.default.removeItem(at: folder) }
                model.launch(); model.receive(["type": "hello"])
                ready(model, client); finishAccounts(model, client)
                model.receive(["type": "state", "status": state, "connected": true, "paused": state == "paused",
                               "is_locked": state == "locked", "queue_count": 2, "active_count": 1])
                model.members = ["本次监听成员"]
                model.tasks = [ApprovalTask(["id": "running", "sender": "成员", "status": "running"]),
                               ApprovalTask(["id": "queued", "sender": "成员", "status": "queued"])]
                let tasks = model.tasks
                let configuration = model.listeningConfiguration
                let previous = model.settings
                model.beginGroupSearch(kind: kind, query: "新群")
                XCTAssertEqual(client.commands.last, "groups")
                XCTAssertTrue(model.showGroupPicker)
                model.receive(["type": "groups", "request_id": client.groupRequestID, "kind": kind,
                               "groups": [["id": "new-group", "title": "新群", "count": 9]]])
                model.receive(["type": "operation_finished", "command": "groups", "request_id": client.groupRequestID])
                XCTAssertEqual(model.groupOptions.count, 1)
                model.selectGroup(model.groupOptions[0])
                let saved = try AppSettings.load(from: model.configURL)
                XCTAssertEqual(kind == "approval" ? saved.approvalGroupId : saved.senderGroupId, "new-group")
                XCTAssertEqual(kind == "approval" ? saved.senderGroupId : saved.approvalGroupId,
                               kind == "approval" ? previous.senderGroupId : previous.approvalGroupId)
                XCTAssertEqual(model.listeningConfiguration, configuration)
                XCTAssertTrue(model.connectionNeedsRestart)
                XCTAssertEqual(model.status, state)
                XCTAssertEqual(model.paused, state == "paused")
                XCTAssertEqual(model.locked, state == "locked")
                XCTAssertEqual(model.queueCount, 2)
                XCTAssertEqual(model.activeCount, 1)
                XCTAssertEqual(model.tasks, tasks)
                XCTAssertEqual(model.members, ["本次监听成员"])
                XCTAssertTrue(model.connected)
                XCTAssertEqual(model.isBusy, false)
                XCTAssertEqual(client.commands.filter { $0 == "start" }.count, 1)
                XCTAssertEqual(client.commands.contains("stop"), false)
                XCTAssertEqual(client.commands.contains("clear_queue"), false)
            }
        }
    }

    func testGroupSearchFailureKeepsListeningPauseAndLockState() throws {
        for state in ["listening", "paused", "locked", "reconnecting"] {
            let (model, client, folder) = try fixture(autoStart: true)
            defer { try? FileManager.default.removeItem(at: folder) }
            model.launch(); model.receive(["type": "hello"])
            ready(model, client); finishAccounts(model, client)
            model.receive(["type": "state", "status": state, "connected": true, "paused": state == "paused", "is_locked": state == "locked"])
            model.beginGroupSearch(kind: "approval", query: "新群")
            let id = client.groupRequestID
            model.receive(["type": "error", "command": "groups", "request_id": id, "message": "查询超时"])
            model.receive(["type": "operation_finished", "command": "groups", "request_id": id])
            XCTAssertEqual(model.status, state)
            XCTAssertTrue(model.connected)
            XCTAssertEqual(model.paused, state == "paused")
            XCTAssertEqual(model.locked, state == "locked")
            XCTAssertEqual(model.dingtalkAccount.status, .ready)
            XCTAssertEqual(model.errorMessage, "查询超时")
            XCTAssertEqual(model.isBusy, false)
            model.beginGroupSearch(kind: "sender", query: "另一群")
            XCTAssertTrue(client.groupRequestID != id)
            XCTAssertTrue(model.searchingGroups)
        }
    }

    func testSelectingRunningGroupOnlyFillsIdentityWithoutRestart() throws {
        for state in ["listening", "paused", "locked", "reconnecting"] {
            for kind in ["approval", "sender"] {
                let (model, client, folder) = try fixture(autoStart: true)
                defer { try? FileManager.default.removeItem(at: folder) }
                model.launch(); model.receive(["type": "hello"])
                ready(model, client); finishAccounts(model, client)
                model.receive(listeningEvent(client))
                model.receive(["type": "state", "status": state, "connected": true])
                let title = kind == "approval" ? model.settings.approvalGroup : model.settings.senderGroup
                let currentID = "current-\(kind)"
                XCTAssertEqual(model.settings.approvalGroupId, "")
                XCTAssertEqual(model.settings.senderGroupId, "")
                chooseGroup(model, client, kind: kind, id: currentID, title: title)
                let saved = try AppSettings.load(from: model.configURL)
                XCTAssertEqual(kind == "approval" ? saved.approvalGroupId : saved.senderGroupId, currentID)
                XCTAssertEqual(model.connectionNeedsRestart, false)
                let writes = client.commands.filter { $0 == "update_settings" }.count
                chooseGroup(model, client, kind: kind, id: currentID, title: title)
                XCTAssertEqual(model.connectionNeedsRestart, false)
                XCTAssertEqual(client.commands.filter { $0 == "update_settings" }.count, writes)
                // A group's display name can change while its identity stays the same.
                chooseGroup(model, client, kind: kind, id: currentID, title: title + "（新名称）")
                XCTAssertEqual(model.connectionNeedsRestart, false)
                XCTAssertEqual(model.status, state)
                XCTAssertEqual(client.commands.filter { $0 == "start" }.count, 1)
                XCTAssertEqual(client.commands.contains("stop"), false)
            }
        }
    }

    func testDifferentGroupWithSameNameRequiresRestartAndSelectingOriginalClearsIt() throws {
        for kind in ["approval", "sender"] {
            let (model, client, folder) = try fixture(autoStart: true)
            defer { try? FileManager.default.removeItem(at: folder) }
            model.launch(); model.receive(["type": "hello"])
            ready(model, client); finishAccounts(model, client)
            model.receive(listeningEvent(client))
            model.receive(["type": "state", "status": "listening", "connected": true])
            let title = kind == "approval" ? model.settings.approvalGroup : model.settings.senderGroup
            chooseGroup(model, client, kind: kind, id: "different-\(kind)", title: title)
            XCTAssertTrue(model.connectionNeedsRestart)
            chooseGroup(model, client, kind: kind, id: "current-\(kind)", title: title)
            XCTAssertEqual(model.connectionNeedsRestart, false)
            XCTAssertEqual(model.listeningConfiguration?.settings.approvalGroupId, "")
            XCTAssertEqual(model.listeningConfiguration?.settings.senderGroupId, "")
        }
    }

    func testGroupSearchCancellationDoesNotChangeSettingsOrRestartState() throws {
        for kind in ["approval", "sender"] {
            let (model, client, folder) = try fixture(autoStart: true)
            defer { try? FileManager.default.removeItem(at: folder) }
            model.launch(); model.receive(["type": "hello"])
            ready(model, client); finishAccounts(model, client)
            model.receive(listeningEvent(client))
            model.receive(["type": "state", "status": "listening", "connected": true])
            let settings = model.settings
            let saved = try Data(contentsOf: model.configURL)
            model.beginGroupSearch(kind: kind, query: "候选群")
            model.showGroupPicker = false
            model.receive(["type": "groups", "request_id": client.groupRequestID,
                           "groups": [["id": "other-group", "title": "候选群", "count": 4]]])
            model.receive(["type": "operation_finished", "command": "groups", "request_id": client.groupRequestID])
            XCTAssertEqual(model.settings, settings)
            XCTAssertEqual(try Data(contentsOf: model.configURL), saved)
            XCTAssertEqual(model.connectionNeedsRestart, false)
            XCTAssertEqual(model.showGroupPicker, false)
            XCTAssertEqual(client.commands.contains("update_settings"), false)
        }
    }

    func testResolvedGroupsBelongToStartupSnapshotAndIgnoreOldSessions() throws {
        let (model, client, folder) = try fixture(autoStart: true)
        defer { try? FileManager.default.removeItem(at: folder) }
        model.launch(); model.receive(["type": "hello"])
        ready(model, client); finishAccounts(model, client)
        let oldEvent = listeningEvent(client)
        XCTAssertEqual(model.status, "starting")
        model.settings.approvalGroupId = "different-approval"
        model.receive(oldEvent)
        model.receive(["type": "state", "status": "listening", "connected": true])
        XCTAssertTrue(model.connectionNeedsRestart)
        XCTAssertEqual(model.listeningConfiguration?.settings.approvalGroupId, "")
        model.stop()
        model.receive(["type": "state", "status": "stopped", "connected": false])
        model.receive(["type": "operation_finished", "command": "start"])
        model.start()
        let newEvent = listeningEvent(client)
        XCTAssertTrue(oldEvent["request_id"] as? String != newEvent["request_id"] as? String)
        model.receive(oldEvent)
        XCTAssertEqual(model.listeningConfiguration?.approvalGroup, nil)
        model.receive(newEvent)
        model.receive(["type": "state", "status": "listening", "connected": true])
        XCTAssertEqual(model.connectionNeedsRestart, false)
        let running = model.listeningConfiguration
        model.receive(oldEvent)
        XCTAssertEqual(model.listeningConfiguration, running)
        model.receive(["type": "worker_exit"])
        model.receive(newEvent)
        XCTAssertEqual(model.listeningConfiguration, nil)
    }

    func testGroupIdentityMatchingPreservesConnectionAndPolicyChanges() throws {
        let (model, client, folder) = try fixture(autoStart: true)
        defer { try? FileManager.default.removeItem(at: folder) }
        model.launch(); model.receive(["type": "hello"])
        ready(model, client); finishAccounts(model, client)
        model.receive(listeningEvent(client))
        model.receive(["type": "state", "status": "listening", "connected": true])
        chooseGroup(model, client, kind: "approval", id: "current-approval", title: model.settings.approvalGroup)
        let original = model.settings
        for field in [\AppSettings.profile, \.gitlabHost, \.gitlabToken, \.fallbackToken, \.dwsPath, \.glabPath] {
            model.settings[keyPath: field] = "changed.example"
            XCTAssertTrue(model.connectionNeedsRestart)
            model.settings = original
            XCTAssertEqual(model.connectionNeedsRestart, false)
        }
        model.settings.processWhileLocked = true
        model.settings.queueCapacity = 250
        model.settings.pausedMessagePolicy = "queue"
        XCTAssertEqual(model.connectionNeedsRestart, false)
    }

    func testEditingSameGroupNameRetainsSelection() throws {
        for kind in ["approval", "sender"] {
            let (model, client, folder) = try fixture(autoStart: true)
            defer { try? FileManager.default.removeItem(at: folder) }
            model.launch(); model.receive(["type": "hello"])
            ready(model, client); finishAccounts(model, client)
            model.receive(listeningEvent(client))
            model.receive(["type": "state", "status": "listening", "connected": true])
            let title = kind == "approval" ? model.settings.approvalGroup : model.settings.senderGroup
            chooseGroup(model, client, kind: kind, id: "current-\(kind)", title: title)
            let saved = model.settings
            let writes = client.commands.filter { $0 == "update_settings" }.count
            model.updateGroupName(title, kind: kind)
            XCTAssertEqual(model.settings, saved)
            XCTAssertEqual(client.commands.filter { $0 == "update_settings" }.count, writes)
            model.updateGroupName("其他群", kind: kind)
            XCTAssertEqual(kind == "approval" ? model.settings.approvalGroupId : model.settings.senderGroupId, "")
            XCTAssertEqual(client.commands.filter { $0 == "update_settings" }.count, writes + 1)
            XCTAssertTrue(model.connectionNeedsRestart)
            model.updateGroupName(title, kind: kind)
            XCTAssertEqual(model.connectionNeedsRestart, false)
        }
    }

    func testGroupResultFromPreviousAccountCannotBeSelected() throws {
        let (model, client, folder) = try fixture(autoStart: true)
        defer { try? FileManager.default.removeItem(at: folder) }
        model.launch(); model.receive(["type": "hello"])
        ready(model, client); finishAccounts(model, client)
        model.receive(["type": "state", "status": "listening", "connected": true])
        let originalGroup = model.settings.approvalGroupId
        model.beginGroupSearch(kind: "approval", query: "新群")
        let id = client.groupRequestID
        model.settings.profile = "corp:new-account"
        model.receive(["type": "groups", "request_id": id, "groups": [["id": "old-group", "title": "旧账号的群", "count": 9]]])
        model.receive(["type": "operation_finished", "command": "groups", "request_id": id])
        XCTAssertTrue(model.groupOptions.isEmpty)
        XCTAssertEqual(model.isBusy, false)
        model.selectGroup(GroupOption(id: "old-group", title: "旧账号的群", count: 9))
        XCTAssertEqual(model.settings.approvalGroupId, originalGroup)
        XCTAssertEqual(model.settings.profile, "corp:new-account")
        XCTAssertEqual(model.status, "listening")
        XCTAssertTrue(model.errorMessage?.contains("重新查找") == true)
    }

    func testLateGroupResultsCannotEndOrReplaceTheNextSearch() throws {
        let (model, client, folder) = try fixture()
        defer { try? FileManager.default.removeItem(at: folder) }
        model.launch(); model.receive(["type": "hello"])
        ready(model, client); finishAccounts(model, client)
        model.beginGroupSearch(kind: "approval", query: "第一群")
        let firstID = client.groupRequestID
        model.receive(["type": "groups", "request_id": firstID, "groups": []])
        model.receive(["type": "operation_finished", "command": "groups", "request_id": firstID])
        model.beginGroupSearch(kind: "sender", query: "第二群")
        let secondID = client.groupRequestID
        model.receive(["type": "groups", "request_id": firstID, "groups": [["id": "wrong", "title": "第一群", "count": 1]]])
        model.receive(["type": "error", "command": "groups", "request_id": firstID, "operation_rejected": true, "message": "旧查询错误"])
        model.receive(["type": "operation_finished", "command": "groups", "request_id": firstID])
        XCTAssertTrue(model.searchingGroups)
        XCTAssertTrue(model.isBusy)
        XCTAssertTrue(model.groupOptions.isEmpty)
        model.receive(["type": "groups", "request_id": secondID, "groups": [["id": "right", "title": "第二群", "count": 2]]])
        model.receive(["type": "operation_finished", "command": "groups", "request_id": secondID])
        XCTAssertEqual(model.groupOptions.first?.id, "right")
        XCTAssertEqual(model.isBusy, false)
    }

    func testFinishingGroupSearchAfterStopDoesNotResumeListening() throws {
        let (model, client, folder) = try fixture(autoStart: true)
        defer { try? FileManager.default.removeItem(at: folder) }
        model.launch(); model.receive(["type": "hello"])
        ready(model, client); finishAccounts(model, client)
        model.receive(["type": "state", "status": "listening", "connected": true])
        model.beginGroupSearch(kind: "approval", query: "新群")
        let id = client.groupRequestID
        model.stop()
        model.receive(["type": "state", "status": "stopped", "connected": false])
        model.receive(["type": "operation_finished", "command": "start"])
        model.receive(["type": "error", "command": "groups", "request_id": id, "message": "查询超时"])
        model.receive(["type": "operation_finished", "command": "groups", "request_id": id])
        XCTAssertEqual(model.status, "stopped")
        XCTAssertEqual(model.connected, false)
        XCTAssertEqual(model.isBusy, false)
        XCTAssertEqual(client.commands.filter { $0 == "start" }.count, 1)
    }

    func testGroupCompletionWaitsForListenerExitBeforePendingAccountRefresh() throws {
        let (model, client, folder) = try fixture(autoStart: true)
        defer { try? FileManager.default.removeItem(at: folder) }
        model.launch(); model.receive(["type": "hello"])
        ready(model, client); finishAccounts(model, client)
        model.receive(["type": "state", "status": "listening", "connected": true])
        model.beginGroupSearch(kind: "approval", query: "新群")
        let id = client.groupRequestID
        model.settings.profile = "corp:new-account"
        model.stop()
        model.receive(["type": "state", "status": "stopped", "connected": false])
        model.receive(["type": "groups", "request_id": id, "groups": []])
        model.receive(["type": "operation_finished", "command": "groups", "request_id": id])
        XCTAssertEqual(client.commands.filter { $0 == "accounts" }.count, 1)
        model.receive(["type": "operation_finished", "command": "start"])
        XCTAssertEqual(client.commands.filter { $0 == "accounts" }.count, 2)
        XCTAssertEqual(client.commands.last, "accounts")
    }
}
