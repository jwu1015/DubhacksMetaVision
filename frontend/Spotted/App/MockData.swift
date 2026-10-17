import Foundation

/// Mock responses that reproduce the Figma mockups. Swap for APIClient once the backend is reachable.
/// Images prefixed with "asset:" load from Assets.xcassets; anything else is treated as a URL.
enum MockData {
    static let chair = Detection(trackID: "t3", box: [0.219, 0.340, 0.819, 0.589], label: "chair", score: 0.88)
    static let lamp = Detection(trackID: "t4", box: [0.720, 0.400, 0.930, 0.572], label: "lamp", score: 0.74)
    static let detections = [chair, lamp]

    static let detect = DetectResponse(
        id: "det_7f3a",
        category: "lounge chair",
        description: "walnut & leather",
        style: "mid-century",
        brandGuess: "Article",
        box: chair.box,
        maskScore: 0.91,
        cutoutURL: "asset:art-lounge-chair",
        searchQueries: ["walnut leather lounge chair", "article lounge chair", "mid-century lounge chair"],
        quickPhrase: "Looks like a walnut lounge chair."
    )

    static let search = SearchResponse(
        id: "det_7f3a",
        bestMatch: Listing(
            title: "Walnut Lounge Chair", source: "google_lens", store: "Article", price: 1299,
            currency: "USD", url: "https://www.article.com", thumbnail: "asset:art-lounge-chair",
            similarity: 92, condition: "new"
        ),
        dupes: [
            Listing(title: "Mid-Century Lounge Chair", source: "google_shopping", store: "Wayfair", price: 489,
                    currency: "USD", url: "https://www.wayfair.com", thumbnail: "asset:art-lounge-chair",
                    similarity: 71, condition: "new"),
            Listing(title: "Leather Accent Chair", source: "google_shopping", store: "Target", price: 329,
                    currency: "USD", url: "https://www.target.com", thumbnail: "asset:art-lounge-chair",
                    similarity: 62, condition: "new"),
        ],
        used: [
            Listing(title: "Walnut Lounge Chair", source: "ebay", store: "eBay", price: 640, currency: "USD",
                    url: "https://www.ebay.com/1", thumbnail: "asset:art-lounge-chair", similarity: 84,
                    condition: "pre-owned"),
            Listing(title: "Walnut Lounge Chair", source: "ebay", store: "eBay", price: 715, currency: "USD",
                    url: "https://www.ebay.com/2", thumbnail: "asset:art-lounge-chair", similarity: 89,
                    condition: "pre-owned"),
            Listing(title: "Walnut Lounge Chair", source: "ebay", store: "eBay", price: 820, currency: "USD",
                    url: "https://www.ebay.com/3", thumbnail: "asset:art-lounge-chair", similarity: 71,
                    condition: "pre-owned"),
        ],
        pricing: Pricing(median: 1180, p25: 899, p75: 1349, verdict: .fair),
        sourcesOK: Source.allCases.map(\.rawValue),
        spokenSummary: "Article has it for $1,299. Fair price. Used from $640."
    )

    /// 03b: Google Shopping timed out.
    static let searchShoppingTimeout = SearchResponse(
        id: search.id, bestMatch: search.bestMatch, dupes: [], used: search.used, pricing: search.pricing,
        sourcesOK: [Source.googleLens.rawValue, Source.ebay.rawValue], spokenSummary: search.spokenSummary
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
