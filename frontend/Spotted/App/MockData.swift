import Foundation

/// Mock responses that reproduce the Figma mockups. Swap for APIClient once the backend is reachable.
/// Images prefixed with "asset:" load from Assets.xcassets; anything else is treated as a URL.
enum MockData {
    static let chair = Detection(trackID: "t3", box: [0.219, 0.340, 0.819, 0.589], label: "chair", score: 0.88)
    static let lamp = Detection(trackID: "t4", box: [0.720, 0.400, 0.930, 0.572], label: "lamp", score: 0.74)
    static let detections = [chair, lamp]

    /// Shaped like a real /detect answer: SAM responds first, so description, style, brand and
    /// search queries are empty. The cutout is a crop of the photo.
    static let detect = DetectResponse(
        captureID: "cap_7f3a",
        imageURL: "asset:art-lounge-chair",
        item: Item(category: "lounge chair", description: nil, style: nil, colors: [], materials: [],
                   brandGuess: nil, confidence: 0, searchQueries: [], bbox: chair.bbox,
                   quickPhrase: "Lounge chair. Finding prices."),
        segmentation: Segmentation(method: .sam3, bbox: chair.bbox, maskScore: 0.91, cutoutPath: "",
                                   cutoutURL: "asset:art-lounge-chair", maskPreviewURL: nil, latencyMS: 900),
        quickPhrase: "Lounge chair. Finding prices.",
        latencyMS: 1500
    )

    private static func product(_ title: String, store: String, price: Double, url: String,
                                source: ProductSource, tier: Tier, rank: Int, savings: Int? = nil,
                                reason: String? = nil) -> Product {
        // matchScore is nil until the backend's visual scoring is live, so everything shows "Similar".
        Product(title: title, store: store, price: price, currency: "USD", url: url,
                imageURL: "asset:art-lounge-chair", source: source, tier: tier, sourceRank: rank,
                matchScore: nil, savingsPct: savings, matchReason: reason)
    }

    static let search = SearchResponse(
        captureID: "cap_7f3a",
        bestMatch: product("Walnut Lounge Chair", store: "Article", price: 1299, url: "https://www.article.com",
                           source: .lens, tier: .new, rank: 1, reason: "Same walnut shell and leather cushions"),
        dupes: [
            product("Mid-Century Lounge Chair", store: "Wayfair", price: 489, url: "https://www.wayfair.com",
                    source: .shopping, tier: .dupe, rank: 1, savings: 62),
            product("Leather Accent Chair", store: "Target", price: 329, url: "https://www.target.com",
                    source: .shopping, tier: .dupe, rank: 2, savings: 75),
        ],
        used: [
            product("Walnut Lounge Chair", store: "eBay", price: 640, url: "https://www.ebay.com/1",
                    source: .ebay, tier: .used, rank: 1),
            product("Walnut Lounge Chair", store: "eBay", price: 715, url: "https://www.ebay.com/2",
                    source: .ebay, tier: .used, rank: 2),
            product("Walnut Lounge Chair", store: "eBay", price: 820, url: "https://www.ebay.com/3",
                    source: .ebay, tier: .used, rank: 3),
        ],
        priceInsight: PriceInsight(typicalLow: 899, typicalHigh: 1349, median: 1180, sampleSize: 9,
                                   bestMatchVerdict: .fair,
                                   verdictText: "Article is within the typical $899–$1,349 range."),
        spokenSummary: "Article has it for $1,299. Fair price. Used from $640.",
        timingMS: [:],
        sourcesOK: nil
    )

    /// 03b: Google Shopping timed out.
    static let searchShoppingTimeout = SearchResponse(
        captureID: search.captureID, bestMatch: search.bestMatch, dupes: [], used: search.used,
        priceInsight: search.priceInsight, spokenSummary: search.spokenSummary, timingMS: [:],
        sourcesOK: [.lens, .ebay]
    )

    /// 07 uses tighter boxes than the stream so both options clear the taller card.
    static let candidates = [
        Candidate(detection: Detection(trackID: "t3", box: [0.234, 0.350, 0.763, 0.566], label: "chair", score: 0.52),
                  title: "Walnut Lounge Chair", price: 1299, store: "Article", image: "asset:art-lounge-chair"),
        Candidate(detection: Detection(trackID: "t4", box: [0.799, 0.371, 0.982, 0.561], label: "lamp", score: 0.48),
                  title: "Brass Arc Lamp", price: 189, store: "West Elm", image: "asset:art-arc-lamp"),
    ]

    static let rooms = [
        Room(name: "Maya’s apartment", visited: "Today · 2:41 PM"),
        Room(name: "Blue Bottle Coffee", visited: "Yesterday"),
    ]

    static let saved = [
        SavedItem(id: "s1", title: "Walnut Lounge Chair", store: "Article", price: 1299,
                  image: "asset:art-lounge-chair", room: "Maya’s apartment"),
        SavedItem(id: "s2", title: "Brass Arc Lamp", store: "West Elm", price: 189,
                  image: "asset:art-arc-lamp", room: "Maya’s apartment"),
        SavedItem(id: "s3", title: "Ceramic Bud Vase", store: "Heath Ceramics", price: 64,
                  image: "asset:art-ceramic-vase", room: "Blue Bottle Coffee"),
        SavedItem(id: "s4", title: "Court Runner", store: "Allbirds", price: 135,
                  image: "asset:art-sneaker", room: "Blue Bottle Coffee"),
    ]

    static let cart = [
        CartItem(id: "c1", title: "Walnut Lounge Chair", shortTitle: "Lounge Chair", store: "Article",
                 room: "Maya’s apartment", price: 1299, quantity: 1, image: "asset:art-lounge-chair"),
        CartItem(id: "c2", title: "Brass Arc Lamp", shortTitle: "Arc Lamp", store: "West Elm",
                 room: "Maya’s apartment", price: 189, quantity: 1, image: "asset:art-arc-lamp"),
    ]
}
