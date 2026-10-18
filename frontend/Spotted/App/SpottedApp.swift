import SwiftUI

@main
struct SpottedApp: App {
    @State private var appState: AppState

    init() {
        let state = AppState()
        state.startLive()
        state.startGlasses()
        _appState = State(initialValue: state)
    }

    var body: some Scene {
        WindowGroup {
            RootView()
                .environment(appState)
                .preferredColorScheme(.dark)
                // spotted://save, add and talk, from the Live Activity (G6).
                .onOpenURL { appState.handle($0) }
        }
    }
}
