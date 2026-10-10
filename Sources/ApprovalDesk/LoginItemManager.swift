import ServiceManagement

@MainActor
protocol LoginItemManaging {
    var status: SMAppService.Status { get }
    func setEnabled(_ enabled: Bool) async throws
}

@MainActor
final class SystemLoginItemManager: LoginItemManaging {
    var status: SMAppService.Status { SMAppService.mainApp.status }

    func setEnabled(_ enabled: Bool) async throws {
        if enabled {
            // An enabled or approval-pending item is already registered.
            if status == .notRegistered || status == .notFound {
                try SMAppService.mainApp.register()
            }
        } else if status == .enabled || status == .requiresApproval {
            try await SMAppService.mainApp.unregister()
        }
    }
}
