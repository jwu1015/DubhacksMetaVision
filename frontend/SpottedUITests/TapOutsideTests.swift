import XCTest

/// Tapping outside a card or sheet closes it; tapping the card itself doesn't.
final class TapOutsideTests: SpottedUITestCase {
    private func tap(_ x: CGFloat, _ y: CGFloat) {
        app.coordinate(withNormalizedOffset: CGVector(dx: x, dy: y)).tap()
    }

    func testProductCardClosesOnFeedTapButNotOnCardTap() {
        launch()
        openChairCard()
        let add = app.buttons["Add to cart"]

        // A tap on the card itself (its price range line): stays open.
        app.staticTexts["typical $899–$1,349"].tap()
        XCTAssertTrue(add.waitForExistence(timeout: 2))

        // The feed, left of the chair: back to the hotspots.
        tap(0.08, 0.30)
        XCTAssertTrue(add.waitForNonExistence(timeout: 3))
        XCTAssertTrue(app.staticTexts["Tap the chair or say “What’s this?”"].waitForExistence(timeout: 3))
    }

    func testListeningSheetClosesOnTapAbove() {
        launch()
        let talk = app.buttons["Talk to Storefront"].firstMatch
        XCTAssertTrue(talk.waitForExistence(timeout: 5))
        talk.tap()
        let stop = app.buttons["Stop listening"]
        XCTAssertTrue(stop.waitForExistence(timeout: 5))
        tap(0.5, 0.15)
        XCTAssertTrue(stop.waitForNonExistence(timeout: 3))
    }
}
