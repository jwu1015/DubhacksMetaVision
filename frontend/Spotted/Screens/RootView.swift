import SwiftUI

/// Tabs, the floating glass tab bar + mic, and the sheets that float above them.
struct RootView: View {
    @Environment(AppState.self) private var app
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        @Bindable var app = app

        ZStack(alignment: .bottom) {
            Color.ink.ignoresSafeArea()

            switch app.tab {
            case .live: LiveView()
            case .saved: SavedView()
            case .cart: CartView()
            case .profile: ProfileView()
            }

            if !app.isListening {
                HStack(spacing: 8) {
                    GlassTabBar(selection: $app.tab)
                    MicButton { app.isListening = true }
                }
            }

            if app.isListening {
                // The Live view dims its own feed under the sheet.
                if app.tab != .live {
                    Color.black.opacity(0.35).ignoresSafeArea().transition(.opacity)
                }
                AgentSheet()
                    .transition(.move(edge: .bottom))
            }

            if app.isShowingCheckout {
                Color.black.opacity(0.35)
                    .ignoresSafeArea()
                    .onTapGesture { app.isShowingCheckout = false }
                    .transition(.opacity)
                CheckoutSheet()
                    .transition(.move(edge: .bottom))
            }
        }
        .animation(reduceMotion ? nil : .easeOut(duration: 0.25), value: app.isListening)
        .animation(reduceMotion ? nil : .easeOut(duration: 0.25), value: app.isShowingCheckout)
        // Above the tab bar (62) + 12pt, out of the pay sheet's way.
        .toastOverlay(app, bottomPadding: app.isShowingCheckout || app.isListening ? 8 : 74)
        .fullScreenCover(isPresented: $app.isShowingDetail) {
            ProductDetail()
                .environment(app)
                .toastOverlay(app, bottomPadding: 92)
        }
        // The voice assistant connects only while it's in use.
        .onChange(of: voiceInUse, initial: true) { _, _ in
            app.updateVoice(appIsActive: scenePhase == .active)
        }
    }

    private var voiceInUse: [Bool] {
        [scenePhase == .active, app.tab == .live, app.isListening, app.isShowingDetail, app.isShowingCheckout]
    }
}
