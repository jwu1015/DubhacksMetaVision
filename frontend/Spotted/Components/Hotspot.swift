import SwiftUI

/// Unselected detection marker. The item tag appears only when focused, which stands in for
/// Figma's Hover state: press-and-hold on the phone, gaze dwell on the glasses, or VoiceOver focus.
struct Hotspot: View {
    /// "Lounge chair"
    let item: String
    var isFocused = false

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var isPulsing = false

    var body: some View {
        Image(isFocused ? "hotspot-target-hover" : "hotspot-target")
            .resizable()
            .frame(width: 64, height: 64)
            .shadow(color: .black.opacity(0.35), radius: 3, y: 1)
            .scaleEffect(isPulsing ? 1.08 : 1)
            .overlay(alignment: .topLeading) {
                if isFocused {
                    tag
                        .fixedSize()
                        .offset(x: -56, y: -34)
                        .transition(.opacity)
                }
            }
            .animation(.easeOut(duration: 0.2), value: isFocused)
            .onAppear {
                guard !reduceMotion else { return }
                withAnimation(.easeInOut(duration: 1.2).repeatForever(autoreverses: true)) {
                    isPulsing = true
                }
            }
            .accessibilityElement()
            .accessibilityLabel(item)
            .accessibilityHint("Shows price and matches")
            .accessibilityAddTraits(.isButton)
    }

    private var tag: some View {
        HStack(spacing: 6) {
            Text(item).foregroundStyle(.labelPrimary)
            Text("· Tap to view").foregroundStyle(.labelSecondary)
        }
        .font(.spottedCaption)
        .padding(.leading, 10)
        .padding(.trailing, 12)
        .padding(.vertical, 6)
        .glass(in: Capsule(), tint: 0.72)
    }
}
