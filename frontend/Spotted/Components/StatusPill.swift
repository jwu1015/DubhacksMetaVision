import SwiftUI

/// "● Glasses · Live" in the top-left of the Live view.
struct StatusPill: View {
    let connection: AppState.Connection
    let isPaused: Bool
    var isGlassesMode = false

    var body: some View {
        HStack(spacing: 8) {
            Icon(name: isLive ? "dot-live" : "dot-warning", size: 8)
            Icon(name: "icon-glasses", size: 18)
            Text(text)
                .font(.spottedFootnote.weight(.semibold))
                .foregroundStyle(.labelPrimary)
        }
        .padding(.leading, 12)
        .padding(.trailing, 14)
        .padding(.vertical, 10)
        .glass(in: Capsule(), tint: 0.55)
        .accessibilityElement(children: .combine)
    }

    private var isLive: Bool { connection == .live && !isPaused }

    private var text: String {
        if isPaused { return "Camera paused" }
        switch connection {
        case .live: return isGlassesMode ? "Glasses · Connected" : "Glasses · Live"
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
                .font(.spottedSubhead)
                .foregroundStyle(.labelPrimary)
        }
        .padding(.leading, 14)
        .padding(.trailing, 16)
        .padding(.vertical, 12)
        .glass(in: Capsule(), tint: 0.62)
    }
}
