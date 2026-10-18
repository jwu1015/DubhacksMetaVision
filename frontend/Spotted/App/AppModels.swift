import CoreGraphics
import Foundation

// App-side types and small helpers on top of the backend's API types. Network/Models.swift matches the
// server exactly; put app conveniences here instead.

// MARK: - Helpers on API types

extension ProductSource {
    /// The stores a Look searches, in display order.
    static let searchable: [ProductSource] = [.shopping, .amazon]

    /// Used in the compact "Checking 3 stores…" row.
    var shortName: String { self == .shopping ? "Shopping" : displayName }
}

extension Product {
    var isUsed: Bool { tier == .used }

    /// When nothing scores as the same product, the best match is the closest one instead.
    var isClosestMatch: Bool { matchReason?.hasPrefix("Closest match") == true }

    /// "Exact match" (matchScore ≥ 85), "Closest match" (not the exact item) or "Similar".
    var matchLabel: String {
        if isExactMatch { return "Exact match" }
        return isClosestMatch ? "Closest match" : "Similar"
    }
}

extension LabelMessage {
    /// "~$30–50"; "~" marks it as an estimate.
    var priceEstimate: String { priceLow == priceHigh ? "~$\(priceLow)" : "~$\(priceLow)–\(priceHigh)" }
}

// MARK: - Scan state

enum ScanState: Equatable {
    case idle
    /// Waiting for the select's answer.
    case looking
    /// The Look is running: card in "searching". Claude's item fills in when "described" arrives.
    case detected(Look)
    /// Full card + verdict.
    case matched(Look, SearchResponse)
    case failed(String)
}

// MARK: - Look

extension Look {
    /// Claude's name once it's known ("tripod floor lamp"), else SAM's prompt.
    var name: String {
        for category in [search?.item?.category, item?.category] {
            if let category, !category.isEmpty { return category }
        }
        return label
    }

    /// What the glasses say when the Look starts.
    var quickPhrase: String {
        if let phrase = item?.quickPhrase, !phrase.isEmpty { return phrase }
        return "\(name.sentenceCased). Finding prices."
    }
}

// MARK: - Glasses mode

/// Where the video comes from: the phone's camera or the Meta glasses.
enum CaptureMode {
    case phone, glasses
}

/// One Look the user asked for: Spotted Today (G5) and the room recap (G7).
struct Capture: Identifiable, Hashable {
    let id: String
    let title: String
    let price: Double?
    let verdict: PriceVerdict?
    let image: String
    let room: String
    let date: Date
    /// The best match, so it can be added to the cart from the feed.
    let product: Product?
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
