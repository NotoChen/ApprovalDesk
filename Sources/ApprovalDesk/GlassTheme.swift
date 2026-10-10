import AppKit
import SwiftUI

// Window-level vibrancy samples the real desktop. Controls use Liquid Glass on
// macOS 26; older systems keep the same hierarchy with native frosted materials.
struct GlassWindowBackground: View {
    @Environment(\.accessibilityReduceTransparency) private var reduceTransparency
    var transparentTitlebar = true

    var body: some View {
        Group {
            if reduceTransparency { Color.canvas }
            else { DesktopVibrancy() }
        }
        .ignoresSafeArea()
        .background(WindowTransparency(transparentTitlebar: transparentTitlebar))
        .allowsHitTesting(false)
    }
}

private struct DesktopVibrancy: NSViewRepresentable {
    func makeNSView(context: Context) -> NSVisualEffectView {
        let view = NSVisualEffectView()
        view.material = .underWindowBackground
        view.blendingMode = .behindWindow
        view.state = .active
        return view
    }
    func updateNSView(_ nsView: NSVisualEffectView, context: Context) {}
}

private struct WindowTransparency: NSViewRepresentable {
    let transparentTitlebar: Bool

    final class Probe: NSView {
        var transparentTitlebar = true
        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            configureWindow()
        }
        func configureWindow() {
            window?.isOpaque = false
            window?.backgroundColor = .clear
            window?.titlebarAppearsTransparent = transparentTitlebar
        }
    }
    func makeNSView(context: Context) -> Probe {
        let view = Probe()
        view.transparentTitlebar = transparentTitlebar
        return view
    }
    func updateNSView(_ nsView: Probe, context: Context) {
        nsView.transparentTitlebar = transparentTitlebar
        nsView.configureWindow()
    }
}

struct GlassGroup<Content: View>: View {
    private let content: Content
    init(@ViewBuilder content: () -> Content) { self.content = content() }
    var body: some View {
        if #available(macOS 26.0, *) {
            GlassEffectContainer(spacing: 8) { content.foregroundStyle(Color.ink) }
        } else { content.foregroundStyle(Color.ink) }
    }
}

private struct GlassChrome: ViewModifier {
    @Environment(\.accessibilityReduceTransparency) private var reduceTransparency
    let radius: CGFloat

    @ViewBuilder func body(content: Content) -> some View {
        let shape = RoundedRectangle(cornerRadius: radius, style: .continuous)
        if reduceTransparency {
            content.background(Color.surface, in: shape)
                .overlay(shape.strokeBorder(Color.primary.opacity(0.08), lineWidth: 0.5))
        } else if #available(macOS 26.0, *) {
            content.glassEffect(.regular, in: shape)
        } else {
            content.modifier(FrostedPanel(radius: radius))
        }
    }
}

private struct FrostedPanel: ViewModifier {
    @Environment(\.colorScheme) private var scheme
    @Environment(\.accessibilityReduceTransparency) private var reduceTransparency
    let radius: CGFloat

    func body(content: Content) -> some View {
        let shape = RoundedRectangle(cornerRadius: radius, style: .continuous)
        content
            .background {
                if reduceTransparency { shape.fill(Color.surface) }
                else {
                    shape.fill(.ultraThinMaterial).opacity(0.64)
                        .overlay(shape.fill(Color.white.opacity(scheme == .dark ? 0.015 : 0.06)))
                }
            }
            .overlay(shape.strokeBorder(
                LinearGradient(colors: [.white.opacity(scheme == .dark ? 0.16 : 0.7), .white.opacity(scheme == .dark ? 0.035 : 0.2)], startPoint: .topLeading, endPoint: .bottomTrailing),
                lineWidth: 0.75))
            .shadow(color: .black.opacity(scheme == .dark ? 0.1 : 0.025), radius: 12, y: 5)
    }
}

private struct ContentSurface: ViewModifier {
    @Environment(\.colorScheme) private var scheme
    @Environment(\.accessibilityReduceTransparency) private var reduceTransparency
    let radius: CGFloat

    func body(content: Content) -> some View {
        let shape = RoundedRectangle(cornerRadius: radius, style: .continuous)
        content
            .background(reduceTransparency ? Color.surface : Color.white.opacity(scheme == .dark ? 0.045 : 0.28), in: shape)
            .overlay(shape.strokeBorder(Color.white.opacity(scheme == .dark ? 0.09 : 0.64), lineWidth: 0.75))
    }
}

struct GlassSelection: View {
    @Environment(\.colorScheme) private var scheme
    var radius: CGFloat = 12
    var body: some View {
        RoundedRectangle(cornerRadius: radius, style: .continuous)
            .fill(Color.white.opacity(scheme == .dark ? 0.12 : 0.7))
            .overlay(RoundedRectangle(cornerRadius: radius, style: .continuous)
                .strokeBorder(Color.white.opacity(scheme == .dark ? 0.13 : 0.92), lineWidth: 0.75))
            .shadow(color: .black.opacity(scheme == .dark ? 0.10 : 0.045), radius: 4, y: 2)
    }
}

struct NavigationButtonStyle: ButtonStyle {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    func makeBody(configuration: Configuration) -> some View {
        NavigationButtonBody(configuration: configuration, reduceMotion: reduceMotion)
    }
    private struct NavigationButtonBody: View {
        let configuration: Configuration
        let reduceMotion: Bool
        @State private var hovered = false
        var body: some View {
            configuration.label
                .background(Color.primary.opacity(configuration.isPressed ? 0.07 : hovered ? 0.035 : 0), in: RoundedRectangle(cornerRadius: 12))
                .scaleEffect(configuration.isPressed && !reduceMotion ? 0.985 : 1)
                .onHover { hovered = $0 }
                .animation(reduceMotion ? nil : .easeOut(duration: 0.16), value: hovered)
                .animation(reduceMotion ? nil : .easeOut(duration: 0.12), value: configuration.isPressed)
        }
    }
}

extension View {
    func glassChrome(radius: CGFloat = 22) -> some View { modifier(GlassChrome(radius: radius)) }
    func frostedPanel(radius: CGFloat = 22) -> some View { modifier(FrostedPanel(radius: radius)) }
    func contentSurface(radius: CGFloat = 16) -> some View { modifier(ContentSurface(radius: radius)) }

    @ViewBuilder func deskButtonStyle(prominent: Bool = false) -> some View {
        if #available(macOS 26.0, *) {
            if prominent { buttonStyle(.glassProminent).tint(.action) }
            else { buttonStyle(.glass).tint(.ink).foregroundStyle(Color.ink) }
        } else {
            if prominent { buttonStyle(.borderedProminent).tint(.action) }
            else { buttonStyle(.bordered).tint(.primary) }
        }
    }
}
