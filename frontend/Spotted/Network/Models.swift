// Models.swift — the backend's API, exactly as the server sends it. Mirrors backend/storefront/models.py;
// change them together.
//
// Decode with a plain JSONDecoder(): every key is mapped explicitly in CodingKeys, so don't set
// keyDecodingStrategy. Optionals really can be null at runtime; don't force-unwrap them.
//
//   POST /session/token   TokenRequest (JSON, optional)  -> TokenResponse: a new LiveKit room with the backend
//                                                            ("vision") in it
//   The room, topic "updates": "objects", "label" and "status" as data packets, "look" as text streams (Update)
//   RPCs to "vision": select {uid}, mode {mode}, pause {paused}, voice {on}; each answers an RPCReply
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
    /// From a live message's box [x1, y1, x2, y2].
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

/// Claude's description of an object, from a Look's "described" stage on.
struct Item: Codable, Hashable {
    let category: String?        // nil = nothing shoppable in view
    let description: String?
    let style: String?
    let colors: [String]
    let materials: [String]
    let brandGuess: String?
    let confidence: Double       // 0–1
    let searchQueries: [String]
    let bbox: BBox?              // Claude's rough box
    let quickPhrase: String

    enum CodingKeys: String, CodingKey {
        case category, description, style, colors, materials, confidence, bbox
        case brandGuess = "brand_guess"
        case searchQueries = "search_queries"
        case quickPhrase = "quick_phrase"
    }
}

// MARK: - Search results

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
    let captureID: String        // the Look's id
    let bestMatch: Product?      // nil when nothing was found for sale
    let dupes: [Product]         // cheaper lookalikes, cheapest savings first; can be empty
    let used: [Product]          // pre-owned listings; can be empty
    let priceInsight: PriceInsight
    let spokenSummary: String    // the results in one line
    let timingMS: [String: Int]  // per-step timings, for debugging
    let item: Item?              // the finished item: Claude's name, description and style

    var id: String { captureID }

    enum CodingKeys: String, CodingKey {
        case dupes, used, item
        case captureID = "capture_id"
        case bestMatch = "best_match"
        case priceInsight = "price_insight"
        case spokenSummary = "spoken_summary"
        case timingMS = "timing_ms"
    }
}

// MARK: - Errors

/// A failed request or RPC: a non-2xx response's {"detail": "..."}, or an RPC answering ok: false.
struct APIError: Decodable, Error, Hashable {
    let detail: String

    init(detail: String) {
        self.detail = detail
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        // 422 validation errors send a list instead of a string.
        detail = (try? c.decode(String.self, forKey: .detail)) ?? "The request was invalid."
    }

    enum CodingKeys: String, CodingKey { case detail }
}

// MARK: - Live messages: the backend -> the phone, on topic "updates"

/// One object the backend tracks. Objects that left the view in the last 15 s are still listed (inView false).
struct LiveObject: Codable, Identifiable, Hashable {
    let uid: String              // never repeats within a session; opaque, don't parse it
    let box: [Double]            // [x1, y1, x2, y2], the last one seen
    let prompt: String           // SAM's prompt that matched, e.g. "lamp"
    let score: Double
    let inView: Bool
    let lastSeenTs: Int64        // ms since epoch

    var id: String { uid }
    var bbox: BBox { BBox(corners: box) }

    enum CodingKeys: String, CodingKey {
        case uid, box, prompt, score
        case inView = "in_view"
        case lastSeenTs = "last_seen_ts"
    }
}

/// Every tracking result: the objects to draw, replacing the last message's.
struct ObjectsMessage: Codable, Hashable {
    let session: Int             // goes up when tracking restarts and uids start over
    let ageMS: Int               // how old the frame behind these boxes is
    let objects: [LiveObject]    // in view first

    enum CodingKeys: String, CodingKey {
        case session, objects
        case ageMS = "age_ms"
    }
}

/// Once per object, soon after it appears: a quick name and a rough price.
struct LabelMessage: Codable, Hashable {
    let uid: String
    let box: [Double]
    let name: String             // e.g. "black insulated water bottle"
    let priceLow: Int            // whole dollars
    let priceHigh: Int

    enum CodingKeys: String, CodingKey {
        case uid, box, name
        case priceLow = "price_low"
        case priceHigh = "price_high"
    }
}

enum LookStage: String, Codable, Hashable {
    case started, described, results, failed, unknown

    init(from decoder: Decoder) throws {
        self = Self(rawValue: try decoder.singleValueContainer().decode(String.self)) ?? .unknown
    }
}

/// One step of an object's Look: what it is and where to buy it. The backend runs one Look per object;
/// each step arrives as a "look" message with the Look's latest state.
struct Look: Codable, Hashable {
    let uid: String
    let lookID: String
    let stage: LookStage
    let label: String            // SAM's prompt, until Claude's item names it
    let box: [Double]
    let selected: Bool           // the user picked this object
    let cropURL: String?         // the crop, once the user picked the object
    let item: Item?              // from "described" on
    let search: SearchResponse?  // "results"
    let message: String?         // "failed": why, in a short sentence
    var pickedAt: Int64? = nil   // when it was last picked (Unix ms), by a tap or by voice; the latest pick shows

    enum CodingKeys: String, CodingKey {
        case uid, stage, label, box, selected, item, search, message
        case lookID = "look_id"
        case pickedAt = "picked_at"
        case cropURL = "crop_url"
    }
}

/// When tracking, its category or its prompts change.
struct StatusMessage: Codable, Hashable {
    enum Tracking: String, Codable, Hashable {
        case stopped, starting, ok, offline, unknown

        init(from decoder: Decoder) throws {
            self = Self(rawValue: try decoder.singleValueContainer().decode(String.self)) ?? .unknown
        }
    }

    let tracking: Tracking
    let category: String
    let prompts: [String]
}

/// Any message on topic "updates": decode each one with this, then switch on it.
enum Update: Decodable, Hashable {
    case objects(ObjectsMessage)
    case label(LabelMessage)
    case look(Look)
    case status(StatusMessage)
    case other(String)           // a type this app doesn't know: ignore it

    private enum Key: String, CodingKey { case type }

    init(from decoder: Decoder) throws {
        let type = try decoder.container(keyedBy: Key.self).decode(String.self, forKey: .type)
        switch type {
        case "objects": self = .objects(try ObjectsMessage(from: decoder))
        case "label": self = .label(try LabelMessage(from: decoder))
        case "look": self = .look(try Look(from: decoder))
        case "status": self = .status(try StatusMessage(from: decoder))
        default: self = .other(type)
        }
    }
}

/// Every RPC to "vision" answers this. select's answer carries the object's Look.
struct RPCReply: Decodable, Hashable {
    let ok: Bool
    let detail: String?
    let look: Look?
}

// MARK: - Session token

/// POST /session/token body. The backend makes sure the identity starts with "phone-".
struct TokenRequest: Codable, Hashable {
    var participantIdentity: String?
    /// What to track, e.g. "electronics": SAM's labels are picked within it from the first frame.
    /// nil: the backend's default ("small things to buy").
    var category: String?

    enum CodingKeys: String, CodingKey {
        case category
        case participantIdentity = "participant_identity"
    }
}

struct TokenResponse: Codable, Hashable {
    let serverURL: String
    let participantToken: String

    enum CodingKeys: String, CodingKey {
        case serverURL = "server_url"
        case participantToken = "participant_token"
    }
}

// MARK: - Voice assistant: its RPC "action" to the phone

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
        case selectItem = "select_item"
        case unknown

        init(from decoder: Decoder) throws {
            self = Self(rawValue: try decoder.singleValueContainer().decode(String.self)) ?? .unknown
        }
    }

    let name: Name
    let captureID: String?       // save_item: the Look to save
    let product: Product?        // add_to_cart: send it to POST /cart as-is
    let tab: ResultsTab?         // show_results
    let uid: String?             // select_item: the object to open, as a tap on its label does

    enum CodingKeys: String, CodingKey {
        case name, product, tab, uid
        case captureID = "capture_id"
    }
}

/// The phone's answer. On ok == false the assistant says `detail` aloud.
struct VoiceActionReply: Codable, Hashable {
    let ok: Bool
    var detail: String? = nil
}
