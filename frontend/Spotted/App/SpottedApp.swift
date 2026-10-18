import SwiftUI

@main
struct SpottedApp: App {
    @State private var appState: AppState

    init() {
        let state = AppState()
        var mockFrame: MockFrame?
        #if DEBUG
        // Launch straight into a Figma frame, e.g. SPOTTED_FRAME=matched (see MockFrame).
        if let name = ProcessInfo.processInfo.environment["SPOTTED_FRAME"],
           let frame = MockFrame.allCases.first(where: { "\($0)" == name }) {
            mockFrame = frame
            state.show(frame)
        }
        #endif
        // Stream to the backend when Config.xcconfig has its URL; mockup frames stay on mock data.
        if mockFrame == nil { state.startLive() }
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
