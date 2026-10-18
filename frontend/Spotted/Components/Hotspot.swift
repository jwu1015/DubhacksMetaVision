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
        .font(.spottedCaption)
        .lineLimit(1)
        .padding(.leading, 10)
        .padding(.trailing, 12)
        .padding(.vertical, 6)
    }
}
