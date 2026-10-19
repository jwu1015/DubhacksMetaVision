import SwiftUI

/// A tracked object's quick label, centered on the object once the backend names it. Tap it to see
/// the object's prices; the selected one turns lime.
struct Hotspot: View {
    /// "Lounge chair"
    let item: String
    /// "~$30–50"
    let priceEstimate: String
    var isSelected = false

    var body: some View {
        Group {
            if isSelected {
                pill.background(Color.accent, in: Capsule())
            } else {
                pill.glass(in: Capsule(), tint: 0.72)
            }
        }
        .fixedSize()
        .shadow(color: .black.opacity(0.35), radius: 3, y: 1)
        .accessibilityElement()
        .accessibilityLabel("\(item), about \(priceEstimate.dropFirst())")
        .accessibilityHint("Shows price and matches")
        .accessibilityAddTraits(.isButton)
    }

    private var pill: some View {
        HStack(spacing: 6) {
            Text(item)
                .foregroundStyle(isSelected ? Color.onAccent : Color.labelPrimary)
            Text(priceEstimate)
                .foregroundStyle(isSelected ? Color.onAccent : Color.labelSecondary)
        }
        .font(.storefrontCaption)
        .lineLimit(1)
        .padding(.leading, 10)
        .padding(.trailing, 12)
        .padding(.vertical, 6)
    }
}

/// A tracked object outside the top labels: a small highlighted dot at the center of its box.
/// The visible dot is 6pt; the tap target around it is 28pt.
struct HotspotDot: View {
    /// What the object is, for VoiceOver.
    let name: String

    var body: some View {
        Circle()
            .fill(.white)
            .frame(width: 6, height: 6)
            // Keeps the dot visible on bright backgrounds.
            .shadow(color: .black.opacity(0.5), radius: 2, y: 0.5)
            .frame(width: 28, height: 28)
            .contentShape(Circle())
            .accessibilityElement()
            .accessibilityLabel(name)
            .accessibilityHint("Shows price and matches")
            .accessibilityAddTraits(.isButton)
    }
}
