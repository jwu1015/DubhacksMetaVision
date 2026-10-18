import XCTest
@testable import Spotted

/// The backend's messages, shaped as backend/storefront/models.py dumps them (nulls included), decode
/// into Models.swift.
final class ModelsTests: XCTestCase {
    private func update(_ json: String) throws -> Update {
        try JSONDecoder().decode(Update.self, from: Data(json.utf8))
    }

    func testObjects() throws {
        let json = """
        {"type": "objects", "session": 2, "age_ms": 180, "objects": [
          {"uid": "2.17", "box": [0.31, 0.22, 0.58, 0.71], "prompt": "lamp", "score": 0.83, "in_view": true,
           "last_seen_ts": 1792310000123},
          {"uid": "2.4", "box": [0.05, 0.6, 0.2, 0.9], "prompt": "bottle", "score": 0.61, "in_view": false,
           "last_seen_ts": 1792309990001}
        ]}
        """
        guard case let .objects(message) = try update(json) else { return XCTFail("not objects") }
        XCTAssertEqual(message.session, 2)
        XCTAssertEqual(message.ageMS, 180)
        XCTAssertEqual(message.objects.map(\.uid), ["2.17", "2.4"])
        XCTAssertEqual(message.objects[1].inView, false)
        XCTAssertEqual(message.objects[0].bbox.w, 0.27, accuracy: 1e-9)
    }

    func testLabel() throws {
        let json = """
        {"type": "label", "uid": "2.17", "box": [0.31, 0.22, 0.58, 0.71], "name": "brass arc floor lamp",
         "price_low": 150, "price_high": 240}
        """
        guard case let .label(label) = try update(json) else { return XCTFail("not a label") }
        XCTAssertEqual(label.name, "brass arc floor lamp")
        XCTAssertEqual(label.priceEstimate, "~$150–240")
    }

    func testLookStarted() throws {
        let json = """
        {"type": "look", "uid": "2.17", "look_id": "l_3f9a2c1d", "stage": "started", "label": "lamp",
         "box": [0.31, 0.22, 0.58, 0.71], "selected": false, "crop_url": null, "item": null, "search": null,
         "message": null}
        """
        guard case let .look(look) = try update(json) else { return XCTFail("not a look") }
        XCTAssertEqual(look.stage, .started)
        XCTAssertEqual(look.name, "lamp")
        XCTAssertEqual(look.quickPhrase, "Lamp. Finding prices.")
        XCTAssertNil(look.cropURL)
    }

    func testLookResults() throws {
        let item = """
        {"category": "brass arc floor lamp", "description": "Arc floor lamp with a brass shade", "style": "mid-century",
         "colors": ["brass"], "materials": ["brass", "marble"], "brand_guess": "West Elm", "confidence": 0.82,
         "search_queries": ["brass arc floor lamp"], "bbox": null, "quick_phrase": "Lamp. Finding prices."}
        """
        let product = """
        {"title": "Brass Arc Floor Lamp", "store": "West Elm", "price": 189.0, "currency": "USD",
         "url": "https://www.westelm.com/lamp", "image_url": null, "source": "shopping", "tier": "new",
         "source_rank": 1, "match_score": null, "savings_pct": null, "match_reason": "Same arc and brass shade"}
        """
        let json = """
        {"type": "look", "uid": "2.17", "look_id": "l_3f9a2c1d", "stage": "results", "label": "lamp",
         "box": [0.31, 0.22, 0.58, 0.71], "selected": true, "crop_url": "https://example.ngrok.app/uploads/l_3f9a2c1d.jpg",
         "item": \(item), "message": null,
         "search": {"capture_id": "l_3f9a2c1d", "best_match": \(product), "dupes": [], "used": [],
           "price_insight": {"typical_low": 150.0, "typical_high": 240.0, "median": 189.0, "sample_size": 7,
                             "best_match_verdict": "fair", "verdict_text": "West Elm is within the typical range."},
           "spoken_summary": "West Elm has it for $189. Fair price.", "timing_ms": {"search": 3100, "total": 4200},
           "item": \(item)}}
        """
        guard case let .look(look) = try update(json) else { return XCTFail("not a look") }
        XCTAssertEqual(look.stage, .results)
        XCTAssertTrue(look.selected)
        XCTAssertEqual(look.search?.captureID, look.lookID)
        XCTAssertEqual(look.search?.bestMatch?.store, "West Elm")
        XCTAssertEqual(look.name, "brass arc floor lamp")
    }

    func testLookFailed() throws {
        let json = """
        {"type": "look", "uid": "2.17", "look_id": "l_3f9a2c1d", "stage": "failed", "label": "lamp",
         "box": [0.31, 0.22, 0.58, 0.71], "selected": true, "crop_url": null, "item": null, "search": null,
         "message": "Couldn't get prices for that one."}
        """
        guard case let .look(look) = try update(json) else { return XCTFail("not a look") }
        XCTAssertEqual(look.stage, .failed)
        XCTAssertEqual(look.message, "Couldn't get prices for that one.")
    }

    func testStatus() throws {
        let json = """
        {"type": "status", "tracking": "ok", "category": "small things to buy", "prompts": ["lamp", "bottle"]}
        """
        guard case let .status(status) = try update(json) else { return XCTFail("not a status") }
        XCTAssertEqual(status.tracking, .ok)
        XCTAssertEqual(status.prompts, ["lamp", "bottle"])
    }

    func testUnknownTypeIsIgnored() throws {
        XCTAssertEqual(try update(#"{"type": "hello", "anything": 1}"#), .other("hello"))
    }

    func testRPCReplies() throws {
        let refused = try JSONDecoder().decode(RPCReply.self,
                                               from: Data(#"{"ok": false, "detail": "That object isn't tracked any more."}"#.utf8))
        XCTAssertFalse(refused.ok)
        XCTAssertEqual(refused.detail, "That object isn't tracked any more.")
        XCTAssertNil(refused.look)
        let done = try JSONDecoder().decode(RPCReply.self, from: Data(#"{"ok": true}"#.utf8))
        XCTAssertTrue(done.ok)
    }

    /// The voice agent's select_object tool asks the app to open an item (backend/storefront/voice/assistant.py).
    func testVoiceSelectItem() throws {
        let action = try JSONDecoder().decode(VoiceAction.self,
                                              from: Data(#"{"name": "select_item", "uid": "2.17"}"#.utf8))
        XCTAssertEqual(action.name, .selectItem)
        XCTAssertEqual(action.uid, "2.17")
    }

    /// picked_at tells the latest pick; backends without it still decode.
    func testLookPickedAt() throws {
        let json = """
        {"type": "look", "uid": "2.17", "look_id": "l_1", "stage": "started", "label": "lamp",
         "box": [0.31, 0.22, 0.58, 0.71], "selected": true, "picked_at": 1792300000123, "crop_url": null,
         "item": null, "search": null, "message": null}
        """
        guard case let .look(look) = try update(json) else { return XCTFail("not a look") }
        XCTAssertEqual(look.pickedAt, 1_792_300_000_123)
    }

    func testTokenRequestCarriesTheCategory() throws {
        let body = try JSONEncoder().encode(TokenRequest(participantIdentity: "phone-1a2b", category: "electronics"))
        let json = try XCTUnwrap(JSONSerialization.jsonObject(with: body) as? [String: String])
        XCTAssertEqual(json, ["participant_identity": "phone-1a2b", "category": "electronics"])
    }

    func testTokenResponse() throws {
        let json = #"{"server_url": "wss://storefront.livekit.cloud", "participant_token": "eyJhbGciOi"}"#
        let token = try JSONDecoder().decode(TokenResponse.self, from: Data(json.utf8))
        XCTAssertEqual(token.serverURL, "wss://storefront.livekit.cloud")
    }
}
