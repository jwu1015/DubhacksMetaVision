import CoreGraphics
import Foundation

// App-side types and small helpers on top of the backend's API types.
// Network/Models.swift is owned by the backend team and matches the server exactly. Don't edit it;
// put app conveniences here instead.

// MARK: - Helpers on API types

extension DetectResponse {
    /// SAM's label, e.g. "lamp". Falls back to "item" when nothing was found.
    var category: String { item.category ?? "item" }

    /// A crop of the photo (not the item on white). Nil when nothing was found.
    var cutoutURL: String? { segmentation?.cutoutURL }

    /// Where the item is: SAM's tight box, else Claude's box.
    var bbox: BBox? { segmentation?.bbox ?? item.bbox }
}

extension ProductSource {
    /// The stores /search queries, in display order. Excludes `.unknown`.
    static let searchable: [ProductSource] = [.lens, .shopping, .ebay]

    /// Used in the compact "Checking 3 stores…" row.
    var shortName: String { self == .shopping ? "Shopping" : displayName }
}

extension SearchResponse {
    /// `sourcesOK` isn't sent yet and decodes as nil, which means every store answered.
    func isAvailable(_ source: ProductSource) -> Bool { sourcesOK?.contains(source) ?? true }

    var storeCount: Int { sourcesOK?.count ?? ProductSource.searchable.count }
}

extension Product {
    var isUsed: Bool { tier == .used }
}

extension Detection {
    func rect(in size: CGSize) -> CGRect { bbox.rect(in: size) }
}

// MARK: - Scan state

enum ScanState: Equatable {
    case idle
    /// JPEG uploading / /detect running.
    case looking
    /// Quick phrase spoken, card in "searching". Claude's item fills in when "described" arrives.
    case detected(Look)
    /// Full card + verdict.
    case matched(Look, SearchResponse)
    /// Low confidence: ask "Which one?" (07).
    case ambiguous([Candidate])
    case failed(String)
}

// MARK: - Look

/// What the app knows about the selected object while its Look runs. Built from the "detections"
/// channel's look messages (detected → described → results), or from a DetectResponse in mock mode.
struct Look: Equatable {
    let captureID: String
    /// SAM's short label from the detected stage, e.g. "lamp".
    var label: String
    var quickPhrase: String
    var cutoutURL: String?
    /// Claude's item from the described stage, replaced by SearchResponse.item when results arrive.
    var item: Item?

    /// Claude's name once it's known ("tripod floor lamp"), else the short label.
    var name: String {
        if let category = item?.category, !category.isEmpty { return category }
        return label
    }
}

extension Look {
    init(_ detect: DetectResponse) {
        self.init(captureID: detect.captureID, label: detect.category, quickPhrase: detect.quickPhrase,
                  cutoutURL: detect.cutoutURL, item: detect.item)
    }

    /// With the results' finished item, so the card shows Claude's name.
    func finished(with search: SearchResponse) -> Look {
        var look = self
        look.item = search.item ?? item
        return look
    }
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
