import SwiftUI

/// 08 · Glasses disconnected: banner at the top of the Live view.
struct DisconnectedBanner: View {
    var body: some View {
        HStack(spacing: 12) {
            Icon(name: "icon-warning", size: 22)
            VStack(alignment: .leading, spacing: 1) {
                Text("Glasses disconnected")
                    .font(.spottedHeadline)
                    .foregroundStyle(.labelPrimary)
                Text("Reconnecting… keep them close to your phone.")
                    .font(.spottedFootnote)
                    .foregroundStyle(.labelSecondary)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(.leading, 14)
        .padding(.trailing, 10)
        .padding(.vertical, 12)
        .glass(in: RoundedRectangle(cornerRadius: 24, style: .continuous), tint: 0.7)
        .accessibilityElement(children: .combine)
    }
}

/// 08 · The paused-feed message in the middle of the Live view.
struct DisconnectedView: View {
    @Environment(AppState.self) private var app

    var body: some View {
        VStack(spacing: 10) {
            Icon(name: "icon-glasses-large", size: 44)
                .frame(width: 96, height: 96)
                .background(.white.opacity(0.06), in: Circle())
                .overlay(Circle().strokeBorder(Color.warning.opacity(0.6), style: StrokeStyle(lineWidth: 2, dash: [6, 4])))
            Text("Feed paused")
                .font(.spottedTitle2)
                .foregroundStyle(.labelPrimary)
            Text("Last frame 12 seconds ago. Your cart and saved items are safe.")
                .font(.spottedSubhead)
                .foregroundStyle(.labelSecondary)
                .multilineTextAlignment(.center)
                .frame(width: 280)
            HStack(spacing: 10) {
                PrimaryButton(title: "Try again", icon: "icon-refresh", iconSize: 18,
                              horizontalPadding: 20, expands: false) {
                    app.connection = .live
                }
                // TODO: switch FrameBuffer to the phone camera once capture is wired up.
                SecondaryButton(title: "Use phone camera", horizontalPadding: 18, expands: false) {
                    app.connection = .live
                }
            }
            .padding(.top, 8)
        }
        .padding(.horizontal, 16)
    }
}

#Preview("08 · Glasses Disconnected") {
    let app = AppState()
    app.show(.disconnected)
    return RootView().environment(app)
}
