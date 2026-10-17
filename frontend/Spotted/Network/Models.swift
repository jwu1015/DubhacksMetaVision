import CoreGraphics
import Foundation

// Field names follow the proposed backend contract in FRONTEND.md.
// Confirm them against the backend's DetectResponse / SearchResponse before wiring APIClient.

/// One object from the WebRTC "detections" data channel.
struct Detection: Codable, Identifiable, Hashable {
    let trackID: String
    /// Normalized x1, y1, x2, y2.
    let box: [Double]
    let label: String
    let score: Double

    var id: String { trackID }

    func rect(in size: CGSize) -> CGRect {
        CGRect(
            x: box[0] * size.width,
            y: box[1] * size.height,
            width: (box[2] - box[0]) * size.width,
            height: (box[3] - box[1]) * size.height
        )
    }

    enum CodingKeys: String, CodingKey {
        case trackID = "track_id", box, label, score
    }
}

struct DetectionsMessage: Codable {
    let type: String
    let ts: Int64
    let objects: [Detection]
}

struct DetectResponse: Codable, Hashable {
    let id: String
    let category: String
    let description: String
    let style: String
    let brandGuess: String?
    let box: [Double]
    let maskScore: Double?
    let cutoutURL: String
    let searchQueries: [String]
    let quickPhrase: String

    enum CodingKeys: String, CodingKey {
        case id, category, description, style, box
        case brandGuess = "brand_guess"
        case maskScore = "mask_score"
        case cutoutURL = "cutout_url"
        case searchQueries = "search_queries"
        case quickPhrase = "quick_phrase"
    }
}

struct Listing: Codable, Hashable, Identifiable {
    let title: String
    let source: String?
    let store: String
    let price: Double
    let currency: String?
    let url: String
    let thumbnail: String
    /// CLIP score, 0–100. Results under 40 are already dropped by the backend.
    let similarity: Int
    let condition: String

    var id: String { url }
    var isExactMatch: Bool { similarity >= 85 }
}

enum PriceVerdict: String, Codable {
    case low, fair, high

    var label: String {
        switch self {
        case .low: "Low price"
        case .fair: "Fair price"
        case .high: "High price"
        }
    }
}

struct Pricing: Codable, Hashable {
    let median: Double
    let p25: Double
    let p75: Double
    let verdict: PriceVerdict
}

struct SearchResponse: Codable, Hashable {
    let id: String
    let bestMatch: Listing?
    let dupes: [Listing]
    let used: [Listing]
    let pricing: Pricing
    let sourcesOK: [String]
    let spokenSummary: String

    func isAvailable(_ source: Source) -> Bool { sourcesOK.contains(source.rawValue) }

    enum CodingKeys: String, CodingKey {
        case id, dupes, used, pricing
        case bestMatch = "best_match"
        case sourcesOK = "sources_ok"
        case spokenSummary = "spoken_summary"
    }
}

enum Source: String, CaseIterable {
    case googleLens = "google_lens"
    case googleShopping = "google_shopping"
    case ebay

    var name: String {
        switch self {
        case .googleLens: "Google Lens"
        case .googleShopping: "Google Shopping"
        case .ebay: "eBay"
        }
    }

    /// Used in the compact "Checking 3 stores…" row.
    var shortName: String { self == .googleShopping ? "Shopping" : name }
}

enum ScanState: Equatable {
    case idle
    /// JPEG uploading / /detect running.
    case looking
    /// Quick phrase spoken, card in "searching".
    case detected(DetectResponse)
    /// Full card + verdict.
    case matched(DetectResponse, SearchResponse)
    /// Low confidence: ask "Which one?" (07).
    case ambiguous([Candidate])
    case failed(String)
}

// MARK: - App-side state

/// One A/B option on the "Which one?" card.
struct Candidate: Identifiable, Hashable {
    let detection: Detection
    let title: String
    let price: Double
    let store: String
    let image: String

    var id: String { detection.trackID }
}

struct SavedItem: Identifiable, Hashable {
    let id: String
    let title: String
    let store: String
    let price: Double
    let image: String
    let room: String
}

struct Room: Identifiable, Hashable {
    let name: String
    let visited: String
    var id: String { name }
}

struct CartItem: Identifiable, Hashable {
    let id: String
    let title: String
    /// "Lounge Chair" for the pay sheet's item list.
    let shortTitle: String
    let store: String
    let room: String
    let price: Double
    var quantity: Int
    let image: String
}

struct AgentLimit: Hashable {
    let amount: Double
    let cardLast4: String
}
