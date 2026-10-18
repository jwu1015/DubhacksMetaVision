import XCTest

final class SmokeTests: SpottedUITestCase {
    /// Tap the chair's hotspot and wait for the mocked pipeline to fill the product card.
    func testHotspotToProductCard() {
        launch()
        let hotspot = app.buttons["Chair"]
        XCTAssertTrue(hotspot.waitForExistence(timeout: 5))
        hotspot.tap()
        XCTAssertTrue(app.buttons["Add to cart"].waitForExistence(timeout: 6))
        snapshot("matched")
    }
}
