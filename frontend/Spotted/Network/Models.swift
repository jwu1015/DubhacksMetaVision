// Models.swift — the Get the Look backend's API, exactly as the server sends it.
//
// Matches backend/models.py on the backend-skeleton branch (17 Oct 2026). Every type here was
// checked by decoding real responses from the running server, including the null cases.
//
// Decode with a plain JSONDecoder(): every key is mapped explicitly in CodingKeys, so don't set
// keyDecodingStrategy. Optionals really can be null at runtime; don't force-unwrap them.
//
// Endpoints (base URL = the backend's ngrok HTTPS URL):
//   POST   /detect             multipart form field "image" (JPEG)   -> DetectResponse
//                              optional fields "hint_box" (the object the user tapped: JSON
//                              {"x","y","w","h"} or [x1,y1,x2,y2], normalized) and "hint_label"
//   POST   /search/{captureID}  no body                               -> SearchResponse (404 if unknown id)
//   POST   /identify           multipart "image" (+ the same hints)  -> IdentifyResponse (/detect + /search)
//   GET    /ice-servers                                              -> {"iceServers": [...]} (WebRTC)
//   POST   /offer              SessionDescription (the offer)        -> SessionDescription (the answer)
//   POST   /checkout           CheckoutRequest (JSON)                -> CheckoutResponse
//   POST   /voice/token        VoiceTokenRequest (JSON, optional)    -> VoiceToken (LiveKit voice assistant)
//   GET    /health                                                   -> HealthResponse
//   Cart (in the search/cart PR, not on the live server yet):
//   POST   /cart               AddToCartRequest (JSON)               -> Cart
//   GET    /cart                                                     -> Cart
//   DELETE /cart/{index}                                             -> Cart
// Errors come back as {"detail": "..."} (APIError).

import CoreGraphics
import Foundation

// MARK: - Shared

/// Normalized box: x/y = top-left corner, w/h = size, all as fractions (0–1) of the photo or frame.
struct BBox: Codable, Hashable {
    let x: Double
    let y: Double
    let w: Double
    let h: Double

    func rect(in size: CGSize) -> CGRect {
        CGRect(x: x * size.width, y: y * size.height, width: w * size.width, height: h * size.height)
    }
}

extension BBox {
    /// From a detections-channel box [x1, y1, x2, y2], e.g. a tapped hotspot.
    init(corners b: [Double]) {
        self.init(x: b[0], y: b[1], w: b[2] - b[0], h: b[3] - b[1])
    }
}

/// Where a product came from.
enum ProductSource: String, Codable, Hashable {
    case lens, shopping, ebay, amazon, unknown

    init(from decoder: Decoder) throws {
        self = Self(rawValue: try decoder.singleValueContainer().decode(String.self)) ?? .unknown
    }

    var displayName: String {
        switch self {
        case .lens: "Google Lens"
        case .shopping: "Google Shopping"
        case .ebay: "eBay"
        case .amazon: "Amazon"
        case .unknown: "Other"
        }
    }
}

/// new = the best match (or any new listing), dupe = a cheaper lookalike, used = pre-owned.
enum Tier: String, Codable, Hashable {
    case new, dupe, used, unknown

    init(from decoder: Decoder) throws {
        self = Self(rawValue: try decoder.singleValueContainer().decode(String.self)) ?? .unknown
    }

    /// The design's "condition" label.
    var condition: String { self == .used ? "pre-owned" : "new" }
}

enum PriceVerdict: String, Codable, Hashable {
    case low, fair, high, unknown

    init(from decoder: Decoder) throws {
        self = Self(rawValue: try decoder.singleValueContainer().decode(String.self)) ?? .unknown
    }

    var label: String {
        switch self {
        case .low: "Low price"
        case .fair, .unknown: "Fair price"
        case .high: "High price"
        }
    }
}

enum SegmentationMethod: String, Codable, Hashable {
    case sam3
    case roboflowSam3 = "roboflow_sam3"
    case bboxFallback = "bbox_fallback"
    case unknown

    init(from decoder: Decoder) throws {
        self = Self(rawValue: try decoder.singleValueContainer().decode(String.self)) ?? .unknown
    }
}

// MARK: - POST /detect

/// What the backend saw. With SAM first (the normal case), /detect answers in ~1.5 s with SAM's
/// short label ("lamp"), and Claude's description arrives on the server side before /search runs,
/// so description, style, brandGuess and searchQueries are often empty here.
struct Item: Codable, Hashable {
    let category: String?        // nil = nothing shoppable in view
    let description: String?
    let style: String?
    let colors: [String]
    let materials: [String]
    let brandGuess: String?
    let confidence: Double       // 0–1; 0 when SAM answered first
    let searchQueries: [String]
    let bbox: BBox?              // where the item is in the photo
    let quickPhrase: String

    enum CodingKeys: String, CodingKey {
        case category, description, style, colors, materials, confidence, bbox
        case brandGuess = "brand_guess"
        case searchQueries = "search_queries"
        case quickPhrase = "quick_phrase"
    }
}

/// The cutout sent to the store search.
struct Segmentation: Codable, Hashable {
    let method: SegmentationMethod
    let bbox: BBox               // the item's box: SAM's tight box, or Claude's padded box on fallback
    let maskScore: Double?       // SAM's confidence; nil on the fallback crop
    let cutoutPath: String       // server-side path; use cutoutURL
    let cutoutURL: String        // a JPEG crop of the item (not on a white background)
    let maskPreviewURL: String?  // the photo with the crop drawn on it; for debugging
    let latencyMS: Int

    enum CodingKeys: String, CodingKey {
        case method, bbox
        case maskScore = "mask_score"
        case cutoutPath = "cutout_path"
        case cutoutURL = "cutout_url"
        case maskPreviewURL = "mask_preview_url"
        case latencyMS = "latency_ms"
    }
}

struct DetectResponse: Codable, Hashable, Identifiable {
    let captureID: String        // pass to /search/{captureID}; ignore responses for older ids
    let imageURL: String
    let item: Item
    let segmentation: Segmentation?  // nil when nothing was found
    let quickPhrase: String      // speak right away, e.g. "Lamp. Finding prices."
    let latencyMS: Int

    var id: String { captureID }
    var foundSomething: Bool { item.category != nil && segmentation != nil }

    enum CodingKeys: String, CodingKey {
        case item, segmentation
        case captureID = "capture_id"
        case imageURL = "image_url"
        case quickPhrase = "quick_phrase"
        case latencyMS = "latency_ms"
    }
}

// MARK: - POST /search/{captureID}

struct Product: Codable, Hashable, Identifiable {
    let title: String
    let store: String
    let price: Double
    let currency: String
    let url: String              // the store page (open it for "Buy")
    let imageURL: String?        // thumbnail; can be nil
    let source: ProductSource
    let tier: Tier
    let sourceRank: Int          // position in that store's results
    let matchScore: Int?         // 0–100 visual match; nil until CLIP scoring is live
    let savingsPct: Int?         // % cheaper than the best match; nil on the best match itself
    let matchReason: String?     // e.g. "Same tripod base and drum shade"

    var id: String { url }
    /// The design's "Exact match" badge (match_score >= 85). Without a score, show "Similar".
    var isExactMatch: Bool { (matchScore ?? 0) >= 85 }

    enum CodingKeys: String, CodingKey {
        case title, store, price, currency, url, source, tier
        case imageURL = "image_url"
        case sourceRank = "source_rank"
        case matchScore = "match_score"
        case savingsPct = "savings_pct"
        case matchReason = "match_reason"
    }
}

/// The typical price range of comparable new products, and how the best match compares.
struct PriceInsight: Codable, Hashable {
    let typicalLow: Double?      // 25th percentile; nil when there are no comparable prices
    let typicalHigh: Double?     // 75th percentile
    let median: Double?
    let sampleSize: Int
    let bestMatchVerdict: PriceVerdict  // "fair" when there are too few prices to judge
    let verdictText: String      // ready to show, e.g. "West Elm is above the typical $120–$329 range."

    enum CodingKeys: String, CodingKey {
        case median
        case typicalLow = "typical_low"
        case typicalHigh = "typical_high"
        case sampleSize = "sample_size"
        case bestMatchVerdict = "best_match_verdict"
        case verdictText = "verdict_text"
    }
}

struct SearchResponse: Codable, Hashable, Identifiable {
    let captureID: String
    let bestMatch: Product?      // nil when nothing was found for sale
    let dupes: [Product]         // cheaper lookalikes, cheapest savings first; can be empty
    let used: [Product]          // pre-owned listings; can be empty
    let priceInsight: PriceInsight
    let spokenSummary: String    // speak when the cards appear
    let timingMS: [String: Int]  // per-step timings, for debugging
    let sourcesOK: [ProductSource]?  // coming soon: which stores answered; nil until the backend sends it
    let item: Item?              // the finished item: Claude's name ("tripod floor lamp" where /detect
                                 // said "lamp"), description and style; show this name on the results.
                                 // nil only from an older backend

    var id: String { captureID }

    enum CodingKeys: String, CodingKey {
        case dupes, used, item
        case captureID = "capture_id"
        case bestMatch = "best_match"
        case priceInsight = "price_insight"
        case spokenSummary = "spoken_summary"
        case timingMS = "timing_ms"
        case sourcesOK = "sources_ok"
    }
}

// MARK: - POST /identify

/// /detect and /search in one call (handy for testing; the app should use the two calls).
struct IdentifyResponse: Codable, Hashable {
    let detect: DetectResponse
    let search: SearchResponse?  // nil when nothing was found
}

// MARK: - Cart and checkout

struct AddToCartRequest: Codable, Hashable {
    let product: Product         // send a Product exactly as /search returned it
}

struct Cart: Codable, Hashable {
    let items: [Product]
    let total: Double
    let spokenSummary: String    // e.g. "Added the Target item, 90 dollars. Your cart has 2 items, 439 dollars total."

    enum CodingKeys: String, CodingKey {
        case items, total
        case spokenSummary = "spoken_summary"
    }
}

struct CheckoutItem: Codable, Hashable {
    let title: String
    let price: Double
    let url: String
}

extension CheckoutItem {
    init(_ product: Product) {
        self.init(title: product.title, price: product.price, url: product.url)
    }
}

struct CheckoutRequest: Codable, Hashable {
    let items: [CheckoutItem]
}

struct CheckoutResponse: Codable, Hashable {
    let checkoutURL: String      // open in SFSafariViewController

    enum CodingKeys: String, CodingKey {
        case checkoutURL = "checkout_url"
    }
}

// MARK: - GET /health and errors

struct HealthResponse: Codable, Hashable {
    let ok: Bool
    let modelService: Bool       // the segmentation server is reachable
    let fixtures: Bool           // true = the backend is returning canned test data
    let demoMode: Bool

    enum CodingKeys: String, CodingKey {
        case ok, fixtures
        case modelService = "model_service"
        case demoMode = "demo_mode"
    }
}

/// Any non-2xx response: 400 (not an image), 404 (unknown capture id), 422 (bad request body), 500.
struct APIError: Decodable, Error, Hashable {
    let detail: String

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        // 422 validation errors send a list instead of a string.
        detail = (try? c.decode(String.self, forKey: .detail)) ?? "The request was invalid."
    }

    enum CodingKeys: String, CodingKey { case detail }
}

// MARK: - Live stream (WebRTC): the "detections" data channel carries every message below.

/// POST /offer body and response.
struct SessionDescription: Codable, Hashable {
    let sdp: String
    let type: String             // "offer" from the phone, "answer" from the backend
}

/// One object on the "detections" data channel (backend -> phone).
struct Detection: Codable, Identifiable, Hashable {
    let trackID: String          // stable while the object stays in view
    let box: [Double]            // normalized x1, y1, x2, y2
    let label: String
    let score: Double

    var id: String { trackID }
    var bbox: BBox { BBox(corners: box) }

    enum CodingKeys: String, CodingKey {
        case box, label, score
        case trackID = "track_id"
    }
}

struct DetectionsMessage: Codable, Hashable {
    let type: String             // "detections"
    let ts: Int64                // ms; ignore messages older than the last one you applied
    let objects: [Detection]
}

/// Optional, phone -> backend on the same channel: the user picked this object.
struct SelectMessage: Codable, Hashable {
    var type = "select"
    let trackID: String

    enum CodingKeys: String, CodingKey {
        case type
        case trackID = "track_id"
    }
}

/// Backend -> phone, after a select: the steps of that "Look", in order. Follow the "detected" whose
/// trackID is the one you selected, then only messages with its captureID.
///   detected:  label, quickPhrase (speak it), box, cutoutPath   at once
///   described: item (Claude's name and description)           ~2s later
///   results:   search, the same answer as POST /search/{id}    ~4–5s after the select
///   failed:    message
struct LookMessage: Codable, Hashable {
    let type: String             // "look"
    let stage: LookStage
    let captureID: String        // the same id POST /search/{captureID} takes
    let trackID: String?         // the selected object, "t3"
    let label: String?           // detected: a short label, e.g. "lamp"
    let quickPhrase: String?     // detected: e.g. "Lamp. Finding prices."
    let box: BBox?               // detected: where it is in the frame
    let cutoutPath: String?      // detected: the crop, at <base URL>/<cutoutPath>
    let item: Item?              // described: Claude's item ("floor lamp", description, style)
    let search: SearchResponse?  // results: best match, dupes, used, price insight, item
    let message: String?         // failed: why, in a short sentence

    enum CodingKeys: String, CodingKey {
        case type, stage, label, box, item, search, message
        case captureID = "capture_id"
        case trackID = "track_id"
        case quickPhrase = "quick_phrase"
        case cutoutPath = "cutout_path"
    }
}

enum LookStage: String, Codable, Hashable {
    case detected, described, results, failed, nothing, unknown

    init(from decoder: Decoder) throws {
        self = Self(rawValue: try decoder.singleValueContainer().decode(String.self)) ?? .unknown
    }
}

/// Any message on the "detections" channel: decode each one with this, then switch on it.
enum ChannelMessage: Decodable, Hashable {
    case detections(DetectionsMessage)
    case look(LookMessage)
    case other(String)           // a type this app doesn't know: ignore it

    private enum Key: String, CodingKey { case type }

    init(from decoder: Decoder) throws {
        let type = try decoder.container(keyedBy: Key.self).decode(String.self, forKey: .type)
        switch type {
        case "detections": self = .detections(try DetectionsMessage(from: decoder))
        case "look": self = .look(try LookMessage(from: decoder))
        default: self = .other(type)
        }
    }
}

// MARK: - Voice assistant (LiveKit)
//
// Mirrors backend/models.py (VoiceTokenRequest, VoiceTokenResponse) and backend/voice.py (the phone's
// RPC method "action"). The video stays on our WebRTC; LiveKit carries only the voice.

/// POST /voice/token body. Every field is optional; the body can be left out entirely.
struct VoiceTokenRequest: Codable, Hashable {
    var participantIdentity: String?
    var participantName: String?

    enum CodingKeys: String, CodingKey {
        case participantIdentity = "participant_identity"
        case participantName = "participant_name"
    }
}

/// Each token opens a fresh room with the assistant already in it.
struct VoiceToken: Codable, Hashable {
    let serverURL: String
    let participantToken: String

    enum CodingKeys: String, CodingKey {
        case serverURL = "server_url"
        case participantToken = "participant_token"
    }
}

/// The results tab the assistant asks for.
enum ResultsTab: String, Codable, Hashable {
    case bestMatch = "best_match"
    case dupes, used, unknown

    init(from decoder: Decoder) throws {
        self = Self(rawValue: try decoder.singleValueContainer().decode(String.self)) ?? .unknown
    }
}

/// The assistant -> phone RPC "action" payload. Reply with a VoiceActionReply within 15 seconds.
struct VoiceAction: Codable, Hashable {
    enum Name: String, Codable, Hashable {
        case addToCart = "add_to_cart"
        case saveItem = "save_item"
        case showResults = "show_results"
        case undoLast = "undo_last"
        case startCheckout = "start_checkout"
        case unknown

        init(from decoder: Decoder) throws {
            self = Self(rawValue: try decoder.singleValueContainer().decode(String.self)) ?? .unknown
        }
    }

    let name: Name
    let captureID: String?       // save_item: the Look to save
    let product: Product?        // add_to_cart: send it to POST /cart as-is
    let tab: ResultsTab?         // show_results

    enum CodingKeys: String, CodingKey {
        case name, product, tab
        case captureID = "capture_id"
    }
}

/// The phone's answer. On ok == false the assistant says `detail` aloud.
struct VoiceActionReply: Codable, Hashable {
    let ok: Bool
    var detail: String? = nil
}
