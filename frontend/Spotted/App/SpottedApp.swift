import SwiftUI

@main
struct SpottedApp: App {
    @State private var appState: AppState

    init() {
        let state = AppState()
        #if DEBUG
        // Launch straight into a Figma frame, e.g. SPOTTED_FRAME=matched (see MockFrame).
        if let name = ProcessInfo.processInfo.environment["SPOTTED_FRAME"],
           let frame = MockFrame.allCases.first(where: { "\($0)" == name }) {
            state.show(frame)
        }
        #endif
        _appState = State(initialValue: state)
    }

    var body: some Scene {
        WindowGroup {
            RootView()
                .environment(appState)
                .preferredColorScheme(.dark)
        }
    }
}
