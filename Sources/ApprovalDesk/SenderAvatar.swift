import AppKit
import ImageIO
import SwiftUI

@MainActor
final class AvatarImageCache {
    static let shared = AvatarImageCache()
    private let images = NSCache<NSURL, NSImage>()
    private var requests: [URL: Task<NSImage?, Never>] = [:]
    private let session: URLSession

    init(session: URLSession = .shared) {
        self.session = session
        images.countLimit = 256
        images.totalCostLimit = 16 * 1024 * 1024
    }

    func image(for url: URL) async -> NSImage? {
        if let image = images.object(forKey: url as NSURL) { return image }
        if let pending = requests[url] { return await pending.value }
        let pending = Task<NSImage?, Never> {
            do {
                let request = URLRequest(url: url, cachePolicy: .useProtocolCachePolicy, timeoutInterval: 10)
                let (data, response) = try await session.data(for: request)
                guard let http = response as? HTTPURLResponse, http.statusCode == 200,
                      let source = CGImageSourceCreateWithData(data as CFData, nil),
                      let bitmap = CGImageSourceCreateThumbnailAtIndex(source, 0, [
                        kCGImageSourceCreateThumbnailFromImageAlways: true,
                        kCGImageSourceCreateThumbnailWithTransform: true,
                        kCGImageSourceThumbnailMaxPixelSize: 128
                      ] as CFDictionary) else { return nil }
                return NSImage(cgImage: bitmap, size: .zero)
            } catch { return nil }
        }
        requests[url] = pending
        let image = await pending.value
        requests[url] = nil
        if let image { images.setObject(image, forKey: url as NSURL, cost: 128 * 128 * 4) }
        return image
    }
}

struct SenderAvatar: View {
    let name: String
    let url: String
    var size: CGFloat = 32
    @State private var image: NSImage?
    @State private var loadedURL = ""
    @Environment(\.colorScheme) private var scheme

    var body: some View {
        let shape = RoundedRectangle(cornerRadius: size * 0.3, style: .continuous)
        ZStack {
            if loadedURL == url, let image {
                Image(nsImage: image).resizable().scaledToFill()
            } else {
                Text(String(name.prefix(1))).font(.system(size: size * 0.42, weight: .medium))
                    .foregroundStyle(Color.brand)
            }
        }
        .frame(width: size, height: size)
        .background(Color.primary.opacity(scheme == .dark ? 0.055 : 0.035))
        .clipShape(shape)
        .overlay(shape.strokeBorder(Color.primary.opacity(scheme == .dark ? 0.1 : 0.07), lineWidth: 0.5))
        .accessibilityHidden(true)
        .task(id: url) {
            guard let target = URL(string: url), target.scheme == "https" else {
                image = nil; loadedURL = url; return
            }
            let result = await AvatarImageCache.shared.image(for: target)
            guard !Task.isCancelled else { return }
            image = result; loadedURL = url
        }
    }
}
