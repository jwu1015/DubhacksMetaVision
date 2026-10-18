import SwiftUI

/// The Live tab in glasses mode: no video is shown. Home at rest (G1), the capture card once a Look
/// starts (G3), and the spoken which-one (G4) when it's unsure.
struct GlassesLiveView: View {
    @Environment(AppState.self) private var app
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        Group {
            switch app.scan {
            case .idle:
                GlassesHomeView()
            case .ambiguous:
                VoiceWhichOneView()
            case .looking, .detected, .matched, .failed:
                CaptureCardView()
            }
        }
        .animation(reduceMotion ? nil : .easeOut(duration: 0.2), value: app.scan)
    }
}

/// G1 · Glasses Home: push-to-talk, the camera's privacy status and the latest capture.
struct GlassesHomeView: View {
    @Environment(AppState.self) private var app

    var body: some View {
        VStack(spacing: 0) {
            GlassesTopBar()

            ZStack {
                if let banner = app.modeBanner {
                    HStack(spacing: 10) {
                        Icon(name: "icon-glasses-accent", size: 20)
                        Text(banner)
                            .font(.spottedFootnote)
                            .foregroundStyle(.labelPrimary)
                    }
                    .padding(.leading, 14)
                    .padding(.trailing, 16)
                    .padding(.vertical, 11)
                    .glass(in: Capsule(), tint: 0.7)
                    .transition(.opacity)
                }
            }
            .frame(height: 42)
            .padding(.top, 16)
            .animation(.easeOut(duration: 0.2), value: app.modeBanner)

            Spacer(minLength: 12)

            PushToTalkButton { app.isListening = true }

            VStack(spacing: 6) {
                Text("Look at something and ask")
                    .font(.spottedTitle2)
                    .foregroundStyle(.labelPrimary)
                Text("Say “Spotted, how much is that lamp?”")
                    .font(.spottedSubhead)
                    .foregroundStyle(.labelSecondary)
                Text("Hold to talk · or press the Action Button")
                    .font(.spottedFootnote)
                    .foregroundStyle(.labelTertiary)
            }
            .multilineTextAlignment(.center)
            .padding(.horizontal, 32)
            .padding(.top, 28)

            Spacer(minLength: 16)

            CameraStatusCard()
                .padding(.horizontal, 20)

            latestCapture
                .padding(.horizontal, 20)
                .padding(.top, 18)
        }
        // Tab bar (62) + 12pt gap, as in the other Live screens.
        .padding(.bottom, 86)
        .background(Color.ink)
    }

    @ViewBuilder private var latestCapture: some View {
        VStack(spacing: 10) {
            HStack {
                Text("Spotted today")
                    .font(.spottedHeadline)
                    .foregroundStyle(.labelPrimary)
                Spacer()
                Button("See all") { app.tab = .saved }
                    .font(.spottedCallout)
                    .foregroundStyle(.labelSecondary)
            }
            if let capture = app.captures.first {
                Button { app.tab = .saved } label: {
                    HStack(spacing: 12) {
                        ProductThumbnail(source: capture.image, size: 52, radius: 14)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(capture.title)
                                .font(.spottedCallout)
                                .foregroundStyle(.labelPrimary)
                            PriceVerdictLine(price: capture.price, verdict: capture.verdict,
                                             suffix: capture.date.formatted(.relative(presentation: .named)))
                        }
                        .lineLimit(1)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        Icon(name: "icon-chevron", size: 16)
                    }
                    .padding(.leading, 12)
                    .padding(.trailing, 14)
                    .padding(.vertical, 10)
                    .background(.surface, in: RoundedRectangle(cornerRadius: 22, style: .continuous))
                }
                .buttonStyle(PressableStyle())
            } else {
                Text("Nothing yet. What you ask about shows up here.")
                    .font(.spottedFootnote)
                    .foregroundStyle(.labelTertiary)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }
}

/// "● Glasses · Connected" and Pause, shared by G1, G3 and G4.
struct GlassesTopBar: View {
    @Environment(AppState.self) private var app

    var body: some View {
        HStack(alignment: .top) {
            StatusPill(connection: app.connection, isPaused: app.isCameraPaused, isGlassesMode: true)
            Spacer()
            GlassIconButton(icon: "icon-pause", iconSize: 18,
                            label: app.isCameraPaused ? "Resume glasses camera" : "Pause glasses camera") {
                app.isCameraPaused.toggle()
            }
        }
        .padding(.horizontal, 16)
    }
}

/// The big lime mic with its halo. Opens the voice sheet (G2).
struct PushToTalkButton: View {
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            ZStack {
                Circle().fill(Color.accent.opacity(0.07)).frame(width: 208, height: 208)
                Circle().fill(Color.accent.opacity(0.12)).frame(width: 168, height: 168)
                Icon(name: "icon-mic-active", size: 48)
                    .frame(width: 128, height: 128)
                    .background(.accent, in: Circle())
                    .shadow(color: Color(hex: 0xC7FF3D, opacity: 0.45), radius: 14)
            }
        }
        .buttonStyle(PressableStyle())
        .accessibilityLabel("Talk to Spotted")
        .accessibilityHint("Ask about what you're looking at")
    }
}

/// Privacy out loud: the glasses camera streams even though nothing is shown.
struct CameraStatusCard: View {
    @Environment(AppState.self) private var app

    var body: some View {
        HStack(spacing: 12) {
            Icon(name: app.isCameraPaused ? "dot-warning" : "dot-live", size: 8)
            VStack(alignment: .leading, spacing: 2) {
                Text(app.isCameraPaused ? "Glasses camera paused" : "Glasses camera on")
                    .font(.spottedCallout)
                    .foregroundStyle(.labelPrimary)
                Text(app.isCameraPaused ? "Nothing is being sent" : "Not recording · nothing is stored")
                    .font(.spottedFootnote)
                    .foregroundStyle(.labelSecondary)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            Button(app.isCameraPaused ? "Resume" : "Pause") { app.isCameraPaused.toggle() }
                .font(.spottedCallout)
                .foregroundStyle(.labelPrimary)
                .padding(.horizontal, 16)
                .frame(height: 40)
                .background(.white.opacity(0.12), in: Capsule())
                .buttonStyle(PressableStyle())
        }
        .padding(.leading, 16)
        .padding(.trailing, 12)
        .padding(.vertical, 12)
        .background(.surface, in: RoundedRectangle(cornerRadius: 22, style: .continuous))
        .accessibilityElement(children: .contain)
    }
}

#Preview("G1 · Glasses Home") {
    let app = AppState()
    app.show(.glassesHome)
    return RootView().environment(app)
}
