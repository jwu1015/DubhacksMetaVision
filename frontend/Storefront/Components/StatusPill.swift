import SwiftUI

/// "● Glasses · Live" or "● Phone camera · Live" in the top-left of the Live view.
struct StatusPill: View {
    let connection: AppState.Connection
    let isPaused: Bool
    var isGlassesMode = false
    /// Why the glasses aren't sending frames yet, e.g. "Connecting to glasses…".
    var glassesNote: String?

    var body: some View {
        HStack(spacing: 8) {
            Icon(name: isLive ? "dot-live" : "dot-warning", size: 8)
            Icon(name: isGlassesMode ? "icon-glasses" : "icon-camera", size: 18)
            Text(text)
                .font(.storefrontFootnote.weight(.semibold))
                .foregroundStyle(.labelPrimary)
                .lineLimit(3)
        }
        .padding(.leading, 12)
        .padding(.trailing, 14)
        .padding(.vertical, 10)
        .glass(in: Capsule(), tint: 0.55)
        .accessibilityElement(children: .combine)
    }

    private var isLive: Bool { connection == .live && !isPaused && glassesNote == nil }

    private var text: String {
        if isPaused { return "Camera paused" }
        if let glassesNote { return glassesNote }
        switch connection {
        case .live: return isGlassesMode ? "Glasses · Live" : "Phone camera · Live"
        case .reconnecting: return "Reconnecting…"
        }
    }
}

/// Glass hint pill near the bottom of the Live view ("Tap the chair or say …").
struct HintPill: View {
    var icon = "icon-tap"
    let text: String

    var body: some View {
        HStack(spacing: 10) {
            Icon(name: icon, size: 20)
            Text(text)
                .font(.storefrontSubhead)
                .foregroundStyle(.labelPrimary)
        }
        .padding(.leading, 14)
        .padding(.trailing, 16)
        .padding(.vertical, 12)
        .glass(in: Capsule(), tint: 0.62)
    }
}
