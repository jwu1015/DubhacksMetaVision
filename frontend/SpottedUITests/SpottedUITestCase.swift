import XCTest

/// Base class for UI journeys. Launches the app on mock data and saves named screenshots into the
/// test results.
class SpottedUITestCase: XCTestCase {
    var app: XCUIApplication!

    override func setUp() {
        continueAfterFailure = false
    }

    func launch() {
        app = XCUIApplication()
        // No backend: the app plays its mock session.
        app.launchEnvironment["SPOTTED_API_BASE_URL"] = ""
        app.launch()
        let start = app.buttons["Start streaming"]
        XCTAssertTrue(start.waitForExistence(timeout: 5))
        start.tap()
    }

    /// Taps the chair's hotspot and waits for the mocked results. The searching card also has
    /// "Add to cart", so wait for the results' price range instead.
    func openChairCard() {
        // Its quick label names it "Lounge chair", with a price estimate after.
        let hotspot = app.buttons.matching(NSPredicate(format: "label BEGINSWITH 'Lounge chair'")).firstMatch
        XCTAssertTrue(hotspot.waitForExistence(timeout: 5))
        hotspot.tap()
        XCTAssertTrue(app.staticTexts["typical $899–$1,349"].waitForExistence(timeout: 8))
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
