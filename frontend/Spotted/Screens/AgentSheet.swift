import SwiftUI

/// 02 · Listening: waveform, live transcript, what the glasses will say, and suggestions.
/// Transcript and reply are mocked until SpeechInput and AgentClient exist.
struct AgentSheet: View {
    @Environment(AppState.self) private var app
    @State private var heard = "Add it to my cart "
    @State private var pending = "and save the lamp"

    private let suggestions = ["Tell me more", "Cheaper?", "Save it", "Compare", "Never mind"]

    var body: some View {
        VStack(spacing: 18) {
            Capsule()
                .fill(.white.opacity(0.3))
                .frame(width: 36, height: 5)

            Waveform()

            Text("Listening…")
                .font(.spottedFootnote)
                .foregroundStyle(.labelSecondary)

            Text(transcript)
                .font(.spottedTranscript)
                .multilineTextAlignment(.center)
                .frame(maxWidth: .infinity)

            HStack(spacing: 10) {
                Icon(name: "icon-glasses-accent", size: 20)
                VStack(alignment: .leading, spacing: 2) {
                    Text("GLASSES WILL SAY")
                        .font(.spottedCaption)
                        .foregroundStyle(.labelTertiary)
                    Text("“Chair added. Lamp saved. Undo?”")
                        .font(.spottedCallout)
                        .foregroundStyle(.labelPrimary)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .padding(.leading, 14)
            .padding(.trailing, 16)
            .padding(.vertical, 12)
            .background(.white.opacity(0.08), in: RoundedRectangle(cornerRadius: 20, style: .continuous))
            .accessibilityElement(children: .combine)

            VStack(alignment: .leading, spacing: 18) {
                Text("Try saying")
                    .font(.spottedFootnote)
                    .foregroundStyle(.labelSecondary)
                FlowLayout(spacing: 8) {
                    ForEach(suggestions, id: \.self) { suggestion in
                        Button {
                            if suggestion == "Never mind" {
                                app.isListening = false
                            } else {
                                heard = suggestion
                                pending = ""
                            }
                        } label: {
                            Text(suggestion)
                                .font(.spottedFootnote)
                                .foregroundStyle(.labelPrimary)
                                .padding(.horizontal, 14)
                                .padding(.vertical, 8)
                                .fill(in: Capsule(), opacity: 0.1, stroke: 0.14)
                        }
                        .buttonStyle(PressableStyle())
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)

            HStack(spacing: 16) {
                CircleButton(icon: "icon-close", size: 56, label: "Cancel") {
                    app.isListening = false
                }
                Button {
                    app.isListening = false
                } label: {
                    Icon(name: "icon-mic-active", size: 30)
                        .frame(width: 76, height: 76)
                        .background(.accent, in: Circle())
                        .shadow(color: Color(hex: 0xC7FF3D, opacity: 0.45), radius: 14)
                }
                .buttonStyle(PressableStyle())
                .accessibilityLabel("Stop listening")
                CircleButton(icon: "icon-check", size: 56, label: "Send") {
                    app.isListening = false
                }
            }
        }
        .padding(.top, 10)
        .padding(.bottom, 28)
        .padding(.horizontal, 20)
        .glass(in: RoundedRectangle(cornerRadius: 40, style: .continuous), tint: 0.68)
        .padding(.horizontal, 8)
        .padding(.bottom, 8)
        .ignoresSafeArea(edges: .bottom)
    }

    private var transcript: AttributedString {
        var said = AttributedString("“\(heard)")
        said.foregroundColor = .white
        var rest = AttributedString("\(pending)”")
        rest.foregroundColor = .white.opacity(0.5)
        return said + rest
    }
}

/// Lime level meter. Animates while listening; static under Reduce Motion.
private struct Waveform: View {
    private let heights: [CGFloat] = [10, 18, 28, 40, 26, 46, 34, 22, 38, 16, 28, 12, 20, 8]

    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        TimelineView(.animation(paused: reduceMotion)) { context in
            let t = context.date.timeIntervalSinceReferenceDate
            HStack(spacing: 4) {
                ForEach(heights.indices, id: \.self) { index in
                    let wobble = reduceMotion ? 1 : 0.7 + 0.3 * sin(t * 6 + Double(index) * 0.9)
                    RoundedRectangle(cornerRadius: 3)
                        .fill(.accent)
                        .frame(width: 5, height: heights[index] * wobble)
                }
            }
            .frame(height: 46)
        }
        .accessibilityHidden(true)
    }
}

#Preview("02 · Listening") {
    let app = AppState()
    app.show(.listening)
    return RootView().environment(app)
}
