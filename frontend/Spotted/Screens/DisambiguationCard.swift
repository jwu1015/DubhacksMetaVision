import SwiftUI

/// 07 · Which one? Low confidence: offer two, never guess silently.
struct DisambiguationCard: View {
    let candidates: [Candidate]
    let onPick: (Candidate) -> Void
    let onNeither: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            VStack(alignment: .leading, spacing: 2) {
                Text("Which one did you mean?")
                    .font(.spottedHeadline)
                    .foregroundStyle(.labelPrimary)
                Text(prompt)
                    .font(.spottedFootnote)
                    .foregroundStyle(.labelSecondary)
            }

            ForEach(Array(candidates.enumerated()), id: \.element.id) { index, candidate in
                Button { onPick(candidate) } label: {
                    HStack(spacing: 12) {
                        ProductThumbnail(source: candidate.image, size: 48, radius: 14)
                        VStack(alignment: .leading, spacing: 1) {
                            Text(candidate.title)
                                .font(.spottedCallout)
                                .foregroundStyle(.labelPrimary)
                            Text("\(Money.short(candidate.price)) · \(candidate.store)")
                                .font(.spottedFootnote)
                                .foregroundStyle(.labelSecondary)
                        }
                        .lineLimit(1)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        OptionBadge(letter: OptionBadge.letter(for: index), size: 28)
                    }
                    .padding(.leading, 8)
                    .padding(.trailing, 14)
                    .padding(.vertical, 8)
                    .fill(in: RoundedRectangle(cornerRadius: 20, style: .continuous), opacity: 0.1, stroke: 0.14)
                }
                .buttonStyle(PressableStyle())
                .accessibilityLabel("Option \(OptionBadge.letter(for: index)): \(candidate.title), \(Money.short(candidate.price))")
            }

            Button(action: onNeither) {
                Text("Neither — keep looking")
                    .font(.spottedCallout)
                    .foregroundStyle(.labelPrimary)
                    .frame(maxWidth: .infinity, minHeight: 44)
            }
            .buttonStyle(PressableStyle())
        }
        .padding(16)
        .glass(in: RoundedRectangle(cornerRadius: 32, style: .continuous))
    }

    private var prompt: String {
        let names = candidates.map { "“\($0.detection.label)”" }
        return "Say \(names.joined(separator: " or ")), or tap."
    }
}

/// White A/B badge on the feed and in the card.
struct OptionBadge: View {
    let letter: String
    let size: CGFloat

    static func letter(for index: Int) -> String {
        String(UnicodeScalar(UInt8(65 + index)))
    }

    var body: some View {
        Text(letter)
            .font(.spottedHeadline)
            .foregroundStyle(.onAccent)
            .frame(width: size, height: size)
            .background(.white, in: Circle())
            .accessibilityHidden(true)
    }
}

#Preview("07 · Which One?") {
    let app = AppState()
    app.show(.whichOne)
    return RootView().environment(app)
}
