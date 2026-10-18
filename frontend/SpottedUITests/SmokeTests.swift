import XCTest

final class SmokeTests: SpottedUITestCase {
    func testHotspotToProductCard() {
        launch()
        openChairCard()
        snapshot("matched")
    }
}
