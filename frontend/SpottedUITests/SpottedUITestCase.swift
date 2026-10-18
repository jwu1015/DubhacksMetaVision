import XCTest

/// Base class for UI journeys. Launches the app, optionally on a Figma frame (see MockFrame),
/// and saves named screenshots into the test results.
class SpottedUITestCase: XCTestCase {
    var app: XCUIApplication!

    override func setUp() {
        continueAfterFailure = false
    }

    /// `frame` is a MockFrame case name, e.g. "matched" or "checkout". Nil starts at 01a.
    func launch(frame: String? = nil, environment: [String: String] = [:]) {
        app = XCUIApplication()
        if let frame { app.launchEnvironment["SPOTTED_FRAME"] = frame }
        app.launchEnvironment.merge(environment) { $1 }
        app.launch()
    }

    func snapshot(_ name: String) {
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }

    func tapTab(_ name: String) {
        app.buttons[name].firstMatch.tap()
    }
}
