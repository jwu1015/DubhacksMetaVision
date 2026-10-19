import XCTest

final class SmokeTests: StorefrontUITestCase {
    func testHotspotToProductCard() {
        launch()
        openChairCard()
        snapshot("matched")
    }
}
