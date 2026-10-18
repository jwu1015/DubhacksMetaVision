import SwiftUI

/// 02 · Listening: waveform, live transcript, what the assistant said, suggestions and typed input.
/// In glasses mode it's G2 · Voice Command: what the assistant understood and the earcon instead of
/// suggestions. Live from the LiveKit voice assistant when it's connected; the Figma mock otherwise.
struct AgentSheet: View {
    @Environment(AppState.self) private var app
    @State private var heard = "Add it to my cart "
    @State private var pending = "and save the lamp"
    @State private var typed = ""

    private let suggestions = ["Tell me more", "Cheaper?", "Save it", "Compare", "Never mind"]

    var body: some View {
        VStack(spacing: 18) {
            Capsule()
                .fill(.white.opacity(0.3))
                .frame(width: 36, height: 5)

            Waveform()

            Text(status)
                .font(.spottedFootnote)
                .foregroundStyle(.labelSecondary)

            Text(transcript)
                .font(.spottedTranscript)
                .multilineTextAlignment(.center)
                .frame(maxWidth: .infinity)

            if isGlasses, let request = app.parsedRequest {
                VStack(alignment: .leading, spacing: 8) {
                    Text("UNDERSTOOD")
                        .font(.spottedCaption)
                        .foregroundStyle(.labelTertiary)
                    HStack(spacing: 8) {
                        Text(request.action)
                            .font(.spottedCaption)
                            .foregroundStyle(.onAccent)
                            .padding(.horizontal, 12)
                            .padding(.vertical, 6)
                            .background(.accent, in: Capsule())
                        parsedChip("Target · \(request.target)")
                        if let context = request.context { parsedChip(context) }
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .accessibilityElement(children: .combine)
            }

            AssistantLine(label: app.voice == nil ? "GLASSES WILL SAY" : "SPOTTED SAID", text: reply)

            if isGlasses {
                HStack(spacing: 8) {
                    Circle().fill(.accent).frame(width: 6, height: 6)
                    Text("Earcon played · listening")
                        .font(.spottedFootnote)
                        .foregroundStyle(.labelTertiary)
                }
                .accessibilityElement(children: .combine)
            } else {
            VStack(alignment: .leading, spacing: 18) {
                Text("Try saying")
                    .font(.spottedFootnote)
                    .foregroundStyle(.labelSecondary)
                FlowLayout(spacing: 8) {
                    ForEach(suggestions, id: \.self) { suggestion in
                        Button {
                            if suggestion == "Never mind" {
                                app.isListening = false
                            } else if let voice = app.voice, voice.isConnected {
                                voice.send(suggestion)
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

            if let voice = app.voice, voice.isConnected {
                typedInput(voice)
            }
            }

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

    private var status: String {
        guard let voice = app.voice else { return "Listening…" }
        return switch voice.status {
        case .off, .connecting: "Connecting to Storefront…"
        case .connected: "Storefront assistant · AI · Listening…"
        case let .failed(message): message
        }
    }

    private var isGlasses: Bool { app.captureMode == .glasses }

    private func parsedChip(_ text: String) -> some View {
        Text(text)
            .font(.spottedCaption)
            .foregroundStyle(.labelPrimary)
            .padding(.horizontal, 12)
            .padding(.vertical, 6)
            .fill(in: Capsule(), opacity: 0.1, stroke: 0.14)
    }

    private var reply: String {
        guard let voice = app.voice else {
            return isGlasses ? "“Lamp. Finding prices.”" : "“Chair added. Lamp saved. Undo?”"
        }
        return voice.lastAssistantLine.map { "“\($0.text)”" } ?? "Look at something and ask."
    }

    /// For when talking isn't possible. Sent to the assistant as chat.
    private func typedInput(_ voice: VoiceAssistant) -> some View {
        HStack(spacing: 8) {
            TextField("Type a question", text: $typed)
                .font(.spottedSubhead)
                .foregroundStyle(.labelPrimary)
                .submitLabel(.send)
                .onSubmit { send(typed, with: voice) }
                .padding(.horizontal, 16)
                .frame(height: 44)
                .fill(in: Capsule(), opacity: 0.1, stroke: 0.14)
            CircleButton(icon: "icon-check", size: 44, iconSize: 18, label: "Send") {
                send(typed, with: voice)
            }
            .disabled(typed.trimmingCharacters(in: .whitespaces).isEmpty)
        }
    }

    private func send(_ text: String, with voice: VoiceAssistant) {
        voice.send(text)
        typed = ""
    }

    private var transcript: AttributedString {
        if let voice = app.voice {
            guard let line = voice.lastUserLine else {
                var hint = AttributedString("“How much is that?”")
                hint.foregroundColor = .white.opacity(0.5)
                return hint
            }
            var said = AttributedString("“\(line.text)”")
            said.foregroundColor = line.isFinal ? .white : .white.opacity(0.7)
            return highlightingTarget(said)
        }
        if isGlasses {
            var said = AttributedString("“Storefront, how much is that lamp?”")
            said.foregroundColor = .white
            return highlightingTarget(said)
        }
        var said = AttributedString("“\(heard)")
        said.foregroundColor = .white
        var rest = AttributedString("\(pending)”")
        rest.foregroundColor = .white.opacity(0.5)
        return said + rest
    }
}

extension AgentSheet {
    /// G2: the parsed target ("that lamp") in lime, so the user sees what the assistant will look up.
    fileprivate func highlightingTarget(_ text: AttributedString) -> AttributedString {
        guard let target = app.parsedRequest?.target else { return text }
        var text = text
        let plain = String(text.characters)
        let phrase = plain.range(of: "that \(target)", options: .caseInsensitive)
            ?? plain.range(of: target, options: .caseInsensitive)
        if let phrase, let range = Range(NSRange(phrase, in: plain), in: text) {
            text[range].foregroundColor = Color.accent
        }
        return text
    }
}

/// Lime level meter. Animates while listening; static under Reduce Motion.
struct Waveform: View {
    /// 1 for the Listening sheet, 0.5 for G4's small "Listening for your answer".
    var scale: CGFloat = 1
    private let heights: [CGFloat] = [10, 18, 28, 40, 26, 46, 34, 22, 38, 16, 28, 12, 20, 8]

    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        TimelineView(.animation(paused: reduceMotion)) { context in
            let t = context.date.timeIntervalSinceReferenceDate
            HStack(spacing: 4 * scale) {
                ForEach(heights.indices, id: \.self) { index in
                    let wobble = reduceMotion ? 1 : 0.7 + 0.3 * sin(t * 6 + Double(index) * 0.9)
                    RoundedRectangle(cornerRadius: 3 * scale)
                        .fill(.accent)
                        .frame(width: 5 * scale, height: heights[index] * scale * wobble)
                }
            }
            .frame(height: 46 * scale)
        }
        .accessibilityHidden(true)
    }
}

#Preview("02 · Listening") {
    let app = AppState()
    app.show(.listening)
    return RootView().environment(app)
}
