import SwiftUI

/// Before anything streams: where the app will connect, and the button that starts the camera.
struct StartView: View {
    @Environment(AppState.self) private var app

    var body: some View {
        VStack(spacing: 0) {
            Spacer()
            VStack(spacing: 10) {
                Icon(name: "icon-glasses-accent", size: 44)
                Text("Spotted")
                    .font(.spottedLargeTitle)
                    .foregroundStyle(.labelPrimary)
                Text("Point your camera at something to see what it is and what it costs.")
                    .font(.spottedSubhead)
                    .foregroundStyle(.labelSecondary)
                    .multilineTextAlignment(.center)
            }
            .padding(.horizontal, 32)
            Spacer()
            VStack(spacing: 12) {
                PrimaryButton(title: "Start streaming", height: 56, action: app.startStreaming)
                Text(server)
                    .font(.spottedFootnote)
                    .foregroundStyle(.labelTertiary)
            }
            .padding(.horizontal, 20)
            .padding(.bottom, 24)
        }
        .frame(maxWidth: .infinity)
        .background(Color.ink)
    }

    private var server: String {
        AppConfig.baseURL?.host().map { "Connects to \($0)" } ?? "No backend set: demo data"
    }
}

#Preview("Start") {
    StartView().environment(AppState())
}
