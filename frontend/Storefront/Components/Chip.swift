import SwiftUI

/// Small info chip on the product card ("Exact match", "Used from $640").
struct Chip: View {
    let title: String
    var icon: String?

    var body: some View {
        HStack(spacing: 5) {
            if let icon { Icon(name: icon, size: 14) }
            Text(title)
                .font(.storefrontCaption)
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
                .font(.storefrontCaption)
                .foregroundStyle(color)
        }
        .padding(.leading, 8)
        .padding(.trailing, detailed ? 10 : 8)
        .padding(.vertical, detailed ? 3 : 2)
        .background(.white.opacity(0.1), in: RoundedRectangle(cornerRadius: 10, style: .continuous))
    }

    private var color: Color { verdict.color }

    @ViewBuilder private var marker: some View {
        switch (verdict, detailed) {
        case (.fair, false), (.unknown, false): Icon(name: "dot-verdict", size: 6)
        case (.fair, true), (.unknown, true): Icon(name: "icon-verdict-fair", size: 12)
        case (_, false): Circle().fill(color).frame(width: 6, height: 6)
        case (.low, true):
            Image(systemName: "arrow.down").font(.system(size: 10, weight: .bold)).foregroundStyle(color)
        case (_, true):
            Image(systemName: "arrow.up").font(.system(size: 10, weight: .bold)).foregroundStyle(color)
        }
    }
}

/// "Exact match" (accent fill), or "Closest match" / "Similar" (neutral).
struct MatchTag: View {
    let product: Product

    private var isExact: Bool { product.isExactMatch }

    var body: some View {
        Text(product.matchLabel)
            .font(.storefrontCaption)
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
                .font(.storefrontCaption)
                .tracking(0.72)
                .foregroundStyle(.labelTertiary)
            Text(text)
                .font(.storefrontFootnote)
                .foregroundStyle(.labelPrimary)
                .lineLimit(1)
        }
        .accessibilityElement(children: .combine)
    }
}

extension PriceVerdict {
    /// Low = success, Fair = secondary, High = warning. Always shown with the word.
    var color: Color {
        switch self {
        case .low: .success
        case .fair, .unknown: .labelSecondary
        case .high: .warning
        }
    }
}

/// "$189 · Fair price" with the verdict in its color.
struct PriceVerdictLine: View {
    let price: Double?
    let verdict: PriceVerdict?
    var suffix: String?

    var body: some View {
        Text(line).font(.storefrontFootnote)
    }

    private var line: AttributedString {
        var parts = AttributedString(price.map { Money.short($0) } ?? "No price yet")
        parts.foregroundColor = Color.labelSecondary
        if let verdict {
            var dot = AttributedString(" · ")
            dot.foregroundColor = Color.labelSecondary
            var word = AttributedString(verdict.label)
            word.foregroundColor = verdict.color
            parts += dot + word
        }
        if let suffix {
            var tail = AttributedString(" · \(suffix)")
            tail.foregroundColor = Color.labelSecondary
            parts += tail
        }
        return parts
    }
}

/// The assistant's spoken line, mirrored on the phone: "GLASSES WILL SAY  “Lamp. Finding prices.”"
struct AssistantLine: View {
    let label: String
    let text: String

    var body: some View {
        HStack(spacing: 10) {
            Icon(name: "icon-glasses-accent", size: 20)
            VStack(alignment: .leading, spacing: 2) {
                Text(label)
                    .font(.storefrontCaption)
                    .foregroundStyle(.labelTertiary)
                Text(text)
                    .font(.storefrontCallout)
                    .foregroundStyle(.labelPrimary)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(.leading, 14)
        .padding(.trailing, 16)
        .padding(.vertical, 12)
        .background(.white.opacity(0.08), in: RoundedRectangle(cornerRadius: 20, style: .continuous))
        .accessibilityElement(children: .combine)
    }
}

/// Room name with a pin, used by Saved (04) and Spotted Today (G5).
struct RoomHeader: View {
    let name: String
    let subtitle: String

    var body: some View {
        HStack(spacing: 10) {
            Icon(name: "icon-pin", size: 18)
                .frame(width: 40, height: 40)
                .background(.white.opacity(0.08), in: RoundedRectangle(cornerRadius: 12, style: .continuous))
            VStack(alignment: .leading, spacing: 1) {
                Text(name)
                    .font(.storefrontHeadline)
                    .foregroundStyle(.labelPrimary)
                Text(subtitle)
                    .font(.storefrontFootnote)
                    .foregroundStyle(.labelSecondary)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            Icon(name: "icon-chevron", size: 16)
        }
        .accessibilityElement(children: .combine)
    }
}
