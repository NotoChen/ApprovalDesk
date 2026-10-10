#if canImport(XCTest)
import XCTest
@testable import ApprovalDesk
#endif
import AppKit
import Foundation

private final class AvatarURLProtocol: URLProtocol {
    static let lock = NSLock()
    static var count = 0
    static var data = Data()
    static var statusCode = 200

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        Self.lock.lock()
        Self.count += 1
        let data = Self.data, code = Self.statusCode
        Self.lock.unlock()
        let response = HTTPURLResponse(url: request.url!, statusCode: code, httpVersion: nil,
                                       headerFields: ["Content-Type": "image/png"])!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: data)
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}

@MainActor
final class AvatarTests: XCTestCase {
    private func cache(data: Data, status: Int = 200) -> (AvatarImageCache, URLSession) {
        AvatarURLProtocol.count = 0
        AvatarURLProtocol.data = data
        AvatarURLProtocol.statusCode = status
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [AvatarURLProtocol.self]
        let session = URLSession(configuration: config)
        return (AvatarImageCache(session: session), session)
    }

    func testListAndDetailShareTheDownloadedImage() async throws {
        let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: 2, pixelsHigh: 2,
                                      bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true,
                                      isPlanar: false, colorSpaceName: .deviceRGB,
                                      bytesPerRow: 0, bitsPerPixel: 0)!
        let color = NSColor(deviceRed: 0.2, green: 0.4, blue: 0.7, alpha: 1)
        for x in 0..<2 { for y in 0..<2 { bitmap.setColor(color, atX: x, y: y) } }
        let (cache, session) = cache(data: bitmap.representation(using: .png, properties: [:])!)
        defer { session.invalidateAndCancel() }
        let url = URL(string: "https://avatar.test/person.png")!
        async let list = cache.image(for: url)
        async let detail = cache.image(for: url)
        let (first, second) = await (list, detail)
        XCTAssertNotNil(first)
        XCTAssertTrue(first === second)
        let third = await cache.image(for: url)
        XCTAssertTrue(first === third)
        XCTAssertEqual(AvatarURLProtocol.count, 1)
    }

    func testMissingOrInvalidImageReturnsThePlaceholderState() async throws {
        for code in [200, 404] {
            let (cache, session) = cache(data: Data("not an image".utf8), status: code)
            let image = await cache.image(for: URL(string: "https://avatar.test/missing.png")!)
            XCTAssertTrue(image == nil)
            session.invalidateAndCancel()
        }
    }
}
