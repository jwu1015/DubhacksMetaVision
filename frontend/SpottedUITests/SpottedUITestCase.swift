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
        app.launch()
    }

    /// Taps the chair's hotspot and waits for the mocked results. The searching card also has
    /// "Add to cart", so wait for the results' price range instead.
    func openChairCard() {
        let hotspot = app.buttons["Chair"]
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
