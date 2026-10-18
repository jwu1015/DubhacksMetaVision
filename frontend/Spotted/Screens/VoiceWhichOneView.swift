import SwiftUI

/// G4 · Voice Which-One: the glasses asked "Chair or lamp?"; the phone mirrors it with two options
/// from scene memory. Answer out loud or tap.
struct VoiceWhichOneView: View {
    @Environment(AppState.self) private var app

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            GlassesTopBar()

            Group {
                AssistantLine(label: "GLASSES ASKED", text: "“\(question)”")
                    .padding(.top, 18)

                Text("Which one did you mean?")
                    .font(.spottedTitle2)
                    .foregroundStyle(.labelPrimary)
                    .padding(.top, 22)
                Text(prompt)
                    .font(.spottedFootnote)
                    .foregroundStyle(.labelSecondary)
                    .padding(.top, 4)

                HStack(alignment: .top, spacing: 13) {
                    ForEach(Array(candidates.prefix(2).enumerated()), id: \.element.id) { index, candidate in
                        option(candidate, letter: OptionBadge.letter(for: index))
                    }
                }
                .padding(.top, 22)

                SecondaryButton(title: "Neither — keep looking", action: app.clearScan)
                    .padding(.top, 26)

                HStack(spacing: 10) {
                    Waveform(scale: 0.5)
                    Text("Listening for your answer")
                        .font(.spottedFootnote)
                        .foregroundStyle(.labelSecondary)
                }
                .frame(maxWidth: .infinity)
                .padding(.top, 30)
            }
            .padding(.horizontal, 20)

            Spacer(minLength: 0)
        }
        .padding(.bottom, 86)
        .background(Color.ink)
    }

    private var candidates: [Candidate] {
        if case let .ambiguous(candidates) = app.scan { return candidates }
        return []
    }

    private var question: String {
        let labels = candidates.prefix(2).map(\.detection.label)
        guard labels.count == 2 else { return "Which one?" }
        return "\(labels[0].sentenceCased) or \(labels[1])?"
    }

    private var prompt: String {
        "Say " + candidates.prefix(2).map { "“\($0.detection.label)”" }.joined(separator: " or ") + ", or tap."
    }

    private func option(_ candidate: Candidate, letter: String) -> some View {
        Button { app.select(candidate.detection) } label: {
            VStack(alignment: .leading, spacing: 0) {
                ZStack(alignment: .topLeading) {
                    Color.surfaceRaised
                    ProductImage(source: candidate.image)
                        .frame(width: 128, height: 109)
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                    OptionBadge(letter: letter, size: 32)
                        .padding(10)
                }
                .frame(height: 150)
                VStack(alignment: .leading, spacing: 2) {
                    Text(candidate.title)
                        .font(.spottedCallout)
                        .foregroundStyle(.labelPrimary)
                        .minimumScaleFactor(0.85)
                    if let seen = candidate.seen {
                        Text(seen)
                            .font(.spottedFootnote)
                            .foregroundStyle(.labelSecondary)
                    }
                }
                .lineLimit(1)
                .padding(.horizontal, 12)
                .padding(.top, 10)
                .padding(.bottom, 12)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .background(.surface)
            .clipShape(RoundedRectangle(cornerRadius: 24, style: .continuous))
        }
        .buttonStyle(PressableStyle())
        .accessibilityLabel("Option \(letter): \(candidate.title)\(candidate.seen.map { ", \($0)" } ?? "")")
    }
}

#Preview("G4 · Voice Which-One") {
    let app = AppState()
    app.show(.voiceWhichOne)
    return RootView().environment(app)
}
