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

            switch (app.tab, app.captureMode) {
            case (.live, _): LiveView()
            case (.saved, .phone): SavedView()
            case (.saved, .glasses): SpottedTodayView()
            case (.cart, _): CartView()
            case (.profile, _): ProfileView()
            }

            if !app.isListening {
                HStack(spacing: 8) {
                    GlassTabBar(selection: $app.tab)
                    MicButton { app.isListening = true }
                }
            }

            if app.isShowingRoomRecap {
                Color.black.opacity(0.5)
                    .ignoresSafeArea()
                    .onTapGesture { app.isShowingRoomRecap = false }
                    .transition(.opacity)
                RoomRecapSheet()
                    .transition(.move(edge: .bottom))
            }

            if app.isListening {
                // The phone Live view dims its own feed under the sheet, so it only needs a tap target.
                let dims = app.tab != .live
                Color.black.opacity(dims ? 0.35 : 0.001)
                    .ignoresSafeArea()
                    .onTapGesture { app.isListening = false }
                    .accessibilityHidden(true)
                    .transition(.opacity)
                AgentSheet()
                    .accessibilityAction(.escape) { app.isListening = false }
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
        .animation(reduceMotion ? nil : .easeOut(duration: 0.25), value: app.isShowingRoomRecap)
        .animation(reduceMotion ? nil : .easeOut(duration: 0.2), value: app.captureMode)
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
        .onChange(of: scenePhase, initial: true) { _, phase in
            if phase == .active { app.appBecameActive() }
        }
        // The screen stays on while the camera streams: auto-lock would background the app and end it.
        .onChange(of: app.isCameraPaused, initial: true) { _, isPaused in
            UIApplication.shared.isIdleTimerDisabled = !isPaused
        }
    }

    private var voiceInUse: [Bool] {
        [scenePhase == .active, app.tab == .live, app.isListening, app.isShowingDetail, app.isShowingCheckout,
         app.isShowingRoomRecap, app.captureMode == .phone, app.isGlassesAudioReady, app.voice != nil]
    }
}
