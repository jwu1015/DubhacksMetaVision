import SwiftUI

/// Selected marker and brand mark. Corners lock inward when a product is identified.
/// The white style marks low-confidence A/B options (07).
struct DetectionBrackets: View {
    enum Style { case accent, white }

    var style: Style = .accent

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var isLocked = false

    var body: some View {
        ZStack {
            corner("tl", rotation: 0, alignment: .topLeading)
            corner("tr", rotation: 90, alignment: .topTrailing)
            corner("br", rotation: 180, alignment: .bottomTrailing)
            corner("bl", rotation: -90, alignment: .bottomLeading)
        }
        .shadow(color: shadowColor, radius: style == .accent ? 6 : 4)
        .scaleEffect(isLocked || reduceMotion ? 1 : 1.12)
        .opacity(isLocked || reduceMotion ? 1 : 0)
        .onAppear {
            withAnimation(.easeOut(duration: 0.2)) { isLocked = true }
        }
        .accessibilityHidden(true)
    }

    private var shadowColor: Color {
        style == .accent ? Color(hex: 0xC7FF3D, opacity: 0.55) : .black.opacity(0.5)
    }

    private func corner(_ position: String, rotation: Double, alignment: Alignment) -> some View {
        let prefix = style == .accent ? "bracket-" : "bracket-white-"
        return Image(prefix + position)
            .resizable()
            .frame(width: 40, height: 40)
            .rotationEffect(.degrees(rotation))
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: alignment)
    }
}

/// "Lounge chair · Likely" above the selected brackets. Confidence is always text.
struct DetectionTag: View {
    let item: String
    var confidence = "Likely"

    var body: some View {
        HStack(spacing: 6) {
            Text(item)
            Text("· \(confidence)")
        }
        .font(.spottedCaption)
        .foregroundStyle(.onAccent)
        .padding(.leading, 10)
        .padding(.trailing, 12)
        .padding(.vertical, 6)
        .background(.accent, in: Capsule())
        .accessibilityElement(children: .combine)
    }
}
