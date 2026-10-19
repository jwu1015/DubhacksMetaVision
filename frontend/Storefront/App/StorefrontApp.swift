import SwiftUI

@main
struct StorefrontApp: App {
    @State private var appState: AppState

    init() {
        let state = AppState()
        state.startGlasses()
        _appState = State(initialValue: state)
    }

    var body: some Scene {
        WindowGroup {
            Group {
                if appState.isStreaming {
                    RootView()
                } else {
                    StartView()
                }
            }
            .environment(appState)
            .preferredColorScheme(.dark)
            // spotted://save, add and talk, from the Live Activity (G6).
            .onOpenURL { appState.handle($0) }
        }
    }
}
