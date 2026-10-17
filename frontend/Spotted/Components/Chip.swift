import SwiftUI

/// Small info chip on the product card ("Exact match", "Used from $640").
struct Chip: View {
    let title: String
    var icon: String?

    var body: some View {
        HStack(spacing: 5) {
            if let icon { Icon(name: icon, size: 14) }
            Text(title)
                .font(.spottedCaption)
                .foregroundStyle(.labelPrimary)
        }
        .padding(.leading, icon == nil ? 12 : 10)
        .padding(.trailing, 12)
        .padding(.vertical, 6)
        .background(.white.opacity(0.1), in: Capsule())
    }
}

/// Price verdict: always text plus color, never color alone.
struct VerdictChip: View {
    let verdict: PriceVerdict
    /// The card uses a dot; Product Detail uses an icon and a bit more padding.
    var detailed = false

    var body: some View {
        HStack(spacing: detailed ? 5 : 4) {
            marker
            Text(verdict.label)
                .font(.spottedCaption)
                .foregroundStyle(color)
        }
        .padding(.leading, 8)
        .padding(.trailing, detailed ? 10 : 8)
        .padding(.vertical, detailed ? 3 : 2)
        .background(.white.opacity(0.1), in: RoundedRectangle(cornerRadius: 10, style: .continuous))
    }

    private var color: Color {
        switch verdict {
        case .low: .success
        case .fair: .labelSecondary
        case .high: .warning
        }
    }

    @ViewBuilder private var marker: some View {
        switch (verdict, detailed) {
        case (.fair, false): Icon(name: "dot-verdict", size: 6)
        case (.fair, true): Icon(name: "icon-verdict-fair", size: 12)
        case (_, false): Circle().fill(color).frame(width: 6, height: 6)
        case (.low, true):
            Image(systemName: "arrow.down").font(.system(size: 10, weight: .bold)).foregroundStyle(color)
        case (_, true):
            Image(systemName: "arrow.up").font(.system(size: 10, weight: .bold)).foregroundStyle(color)
        }
    }
}

/// "Exact match" (similarity ≥ 85) or "Similar".
struct MatchTag: View {
    let isExact: Bool

    var body: some View {
        Text(isExact ? "Exact match" : "Similar")
            .font(.spottedCaption)
            .foregroundStyle(isExact ? Color.onAccent : Color.labelPrimary)
            .padding(.horizontal, 8)
            .padding(.vertical, 2)
            .background(isExact ? Color.accent : .white.opacity(0.12),
                        in: RoundedRectangle(cornerRadius: 8, style: .continuous))
    }
}

/// "GLASSES SAID  Looks like a walnut lounge chair."
struct GlassesSaidRow: View {
    let text: String

    var body: some View {
        HStack(spacing: 6) {
            Icon(name: "icon-glasses-small", size: 14)
            Text("GLASSES SAID")
                .font(.spottedCaption)
                .tracking(0.72)
                .foregroundStyle(.labelTertiary)
            Text(text)
                .font(.spottedFootnote)
                .foregroundStyle(.labelPrimary)
                .lineLimit(1)
        }
        .accessibilityElement(children: .combine)
    }
}
