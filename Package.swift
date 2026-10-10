// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "ApprovalDesk",
    platforms: [.macOS(.v14)],
    products: [.executable(name: "ApprovalDesk", targets: ["ApprovalDesk"])],
    targets: [
        .executableTarget(name: "ApprovalDesk", path: "Sources/ApprovalDesk"),
        .testTarget(name: "ApprovalDeskTests", dependencies: ["ApprovalDesk"], path: "Tests/ApprovalDeskTests")
    ],
    swiftLanguageModes: [.v5]
)
