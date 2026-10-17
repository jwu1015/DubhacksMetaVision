import SwiftUI

/// Tabs, the floating glass tab bar + mic, and the sheets that float above them.
struct RootView: View {
    @Environment(AppState.self) private var app
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

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
        .fullScreenCover(isPresented: $app.isShowingDetail) {
            ProductDetail()
                .environment(app)
        }
    }
}
