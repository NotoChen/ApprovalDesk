#if canImport(XCTest)
import XCTest
@testable import ApprovalDesk
#else
import Foundation
// The same checks also run on Macs with Command Line Tools and no XCTest SDK.
class XCTestCase {}
func XCTAssertEqual<T: Equatable>(_ actual: T, _ expected: T, file: StaticString = #file, line: UInt = #line) {
    precondition(actual == expected, "Expected \(expected), got \(actual)", file: file, line: line)
}
func XCTAssertTrue(_ value: Bool, file: StaticString = #file, line: UInt = #line) { precondition(value, file: file, line: line) }
func XCTAssertNotNil<T>(_ value: T?, file: StaticString = #file, line: UInt = #line) { precondition(value != nil, file: file, line: line) }
#endif

final class SettingsTests: XCTestCase {
    func testJSONRoundTripKeepsIndependentPoliciesAndPlainToken() throws {
        var settings = AppSettings()
        settings.processWhileLocked = true
        settings.lockedMessagePolicy = "discard"
        settings.pausedMessagePolicy = "queue"
        settings.queueOverflowPolicy = "reject_new"
        settings.receivedCondition = "any_owned_merge"
        settings.launchAtLogin = false
        settings.startOnLaunch = false
        settings.showWorkbenchOnLaunch = true
        settings.gitlabToken = "fixture-token"
        let file = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString).appendingPathComponent("settings.json")
        defer { try? FileManager.default.removeItem(at: file.deletingLastPathComponent()) }
        try settings.save(to: file)
        let loaded = try AppSettings.load(from: file)
        XCTAssertEqual(loaded, settings)
        let text = try String(contentsOf: file, encoding: .utf8)
        XCTAssertTrue(text.contains("fixture-token"))
        XCTAssertTrue(text.contains("max_concurrent_mrs"))
        XCTAssertTrue(text.contains("use_fallback_for_own_mr"))
    }

    func testPartialConfigurationGetsDefaults() throws {
        let file = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + ".json")
        defer { try? FileManager.default.removeItem(at: file) }
        try Data("{\"locked_message_policy\":\"discard\"}".utf8).write(to: file)
        let loaded = try AppSettings.load(from: file)
        XCTAssertEqual(loaded.lockedMessagePolicy, "discard")
        XCTAssertEqual(loaded.pausedMessagePolicy, "discard")
        XCTAssertEqual(loaded.queueOverflowPolicy, "drop_oldest")
        XCTAssertEqual(loaded.receivedCondition, "all_succeeded_and_owned")
        XCTAssertTrue(loaded.launchAtLogin)
        XCTAssertTrue(loaded.startOnLaunch)
        XCTAssertEqual(loaded.showWorkbenchOnLaunch, false)
        XCTAssertEqual(loaded.maxConcurrentMrs, 3)
    }

    func testInvalidCapacityIsRejectedBeforeSaving() {
        var settings = AppSettings()
        settings.queueCapacity = 0
        XCTAssertNotNil(settings.validationIssue)
        let file = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + ".json")
        do { try settings.save(to: file); preconditionFailure("Invalid config must not be saved") }
        catch { XCTAssertTrue(!FileManager.default.fileExists(atPath: file.path)) }
    }

    func testTaskUpdatesRetainMRDetailsAndPartialSuccess() {
        var task = ApprovalTask(["id": "task", "sender": "测试", "mrs": [["key": "project!1", "url": "https://example.test", "title": "保留标题"]]])
        task.merge(["status": "partial", "received": true, "merged_count": 1, "error": "另一个 MR 失败"])
        XCTAssertEqual(task.mrs.first?.title, "保留标题")
        XCTAssertTrue(task.hasErrors)
        XCTAssertTrue(task.received)
        XCTAssertEqual(task.mergedCount, 1)
    }

    func testMRDetailsUseServerURLAndKeepItThroughProgressUpdates() {
        var mr = MRItem(["key": "erp/erp-core!42", "url": "https://git2.superboss.cc/erp/erp-core/-/merge_requests/42"])
        let serverURL = "https://git2.superboss.cc/erp/erp-core/merge_requests/42"
        mr.merge(["url": serverURL, "title": "修复页面跳转"])
        XCTAssertEqual(mr.url, serverURL)
        mr.merge(["status": "completed", "merged_by": "当前账号"])
        XCTAssertEqual(mr.url, serverURL)
        XCTAssertEqual(mr.title, "修复页面跳转")
        mr.merge(["url": ""])
        XCTAssertEqual(mr.url, serverURL)
    }

    func testSenderAvatarUpdatesKeepRequestAndMRState() {
        var task = ApprovalTask(["id": "task", "sender": "测试成员", "status": "running",
                                 "mrs": [["key": "project!1", "url": "https://example.test/mr"]]])
        XCTAssertEqual(task.senderAvatarURL, "")
        task.merge(["sender_avatar_url": "https://example.test/avatar.jpg"])
        XCTAssertEqual(task.senderAvatarURL, "https://example.test/avatar.jpg")
        XCTAssertEqual(task.status, "running")
        XCTAssertEqual(task.mrs.first?.key, "project!1")
        task.merge(["status": "completed"])
        XCTAssertEqual(task.senderAvatarURL, "https://example.test/avatar.jpg")
        XCTAssertEqual(task.sender, "测试成员")
    }

    func testConfirmedMergeReplacesErrorWithActualMerger() {
        var mr = MRItem(["key": "project!1", "status": "failed", "error": "HTTP 405", "actor": "当前账号"])
        mr.merge(["merged_by": "协作成员"])
        mr.merge(["status": "completed", "error": "", "owned_merge": false])
        XCTAssertEqual(mr.status, "completed")
        XCTAssertEqual(mr.error, "")
        XCTAssertEqual(mr.actor, "当前账号")
        XCTAssertEqual(mr.mergeSummary, "已由 协作成员 合并")
        XCTAssertEqual(mr.ownedMerge, false)
    }

    func testConfirmedMergeWithoutMergerKeepsIdentityUnknown() {
        let mr = MRItem(["key": "project!1", "status": "completed", "actor": "当前账号"])
        XCTAssertEqual(mr.mergeSummary, "已合并 · GitLab 未返回合并人")
        XCTAssertEqual(mr.ownedMerge, false)
    }
}

#if !canImport(XCTest)
@main struct ModelChecks {
    @MainActor static func main() async throws {
        let tests = SettingsTests()
        try tests.testJSONRoundTripKeepsIndependentPoliciesAndPlainToken()
        try tests.testPartialConfigurationGetsDefaults()
        tests.testInvalidCapacityIsRejectedBeforeSaving()
        tests.testTaskUpdatesRetainMRDetailsAndPartialSuccess()
        tests.testMRDetailsUseServerURLAndKeepItThroughProgressUpdates()
        tests.testSenderAvatarUpdatesKeepRequestAndMRState()
        tests.testConfirmedMergeReplacesErrorWithActualMerger()
        tests.testConfirmedMergeWithoutMergerKeepsIdentityUnknown()
        print("8 native model checks passed")
        try await AvatarTests().testListAndDetailShareTheDownloadedImage()
        try await AvatarTests().testMissingOrInvalidImageReturnsThePlaceholderState()
        print("2 native avatar loading checks passed")
        let app = AppModelTests()
        try app.testStartupReadsAccountsWithoutStartingListener()
        try app.testOneAccountFailureKeepsOtherIdentityAndErrorDetail()
        try app.testOpeningSettingsDuringStartupDefersProfileLoadUntilOperationFinishes()
        try app.testAutoStartWaitsForBothAccountsAndReleasedOperation()
        try app.testPendingProfilesLoadBeforeAutoStartAndListeningDoesNotStayBusy()
        try await app.testChangedProfileRejectsOldResultsAndRefreshesAfterDebounce()
        try app.testWorkerExitEndsLoadingAndRefreshRestartsHandshake()
        try app.testWorkerLaunchFailureDoesNotLeaveAccountsLoading()
        try app.testSuccessfulLoginRefreshesAccountsAfterOperationIsReleased()
        try app.testManualCheckAfterEngineExitWaitsForStartupRead()
        try app.testQuitDuringStartupDoesNotRunPendingAutoStartOrProfiles()
        try app.testRejectedAccountReadCanBeRetriedWithoutStuckLoading()
        try app.testMenuOnlyStartupStartsOnceWithoutShowingWorkbench()
        try app.testWorkbenchLaunchOptInOpensOnceAndKeepsManualListeningChoice()
        try await app.testStartupRegistersLoginItemAndDisablingItPersists()
        try await app.testLoginItemFailureDoesNotBlockAutoListening()
        try await app.testPendingLoginApprovalIsShownAsPending()
        print("17 native startup, login-item and account checks passed")
        try app.testBothGroupSelectorsDisplayCandidatesAndSaveTheSelectedGroup()
        try app.testFailedGroupSearchCanBeRetriedAndAnEmptyResultEndsLoading()
        try app.testGroupSelectionWhileActiveSavesForNextStartAndKeepsTasks()
        try app.testGroupSearchFailureKeepsListeningPauseAndLockState()
        try app.testGroupResultFromPreviousAccountCannotBeSelected()
        try app.testLateGroupResultsCannotEndOrReplaceTheNextSearch()
        try app.testFinishingGroupSearchAfterStopDoesNotResumeListening()
        try app.testGroupCompletionWaitsForListenerExitBeforePendingAccountRefresh()
        try app.testSelectingRunningGroupOnlyFillsIdentityWithoutRestart()
        try app.testDifferentGroupWithSameNameRequiresRestartAndSelectingOriginalClearsIt()
        try app.testGroupSearchCancellationDoesNotChangeSettingsOrRestartState()
        try app.testResolvedGroupsBelongToStartupSnapshotAndIgnoreOldSessions()
        try app.testGroupIdentityMatchingPreservesConnectionAndPolicyChanges()
        try app.testEditingSameGroupNameRetainsSelection()
        SettingsWindowTests().testHostedSettingsKeepsNativeCloseButtonReachableAndClosesOnlySettings()
        print("15 native group selection and settings-window checks passed")
    }
}
#endif
