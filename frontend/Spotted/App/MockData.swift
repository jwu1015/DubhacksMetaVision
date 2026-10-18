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

    /// The finished item that results carry in SearchResponse.item: Claude's name and description.
    static let finishedItem = Item(
        category: "walnut lounge chair",
        description: "Mid-century lounge chair with a walnut shell and black leather cushions",
        style: "mid-century", colors: ["walnut", "black"], materials: ["walnut", "leather"],
        brandGuess: "Article", confidence: 0.86, searchQueries: [], bbox: chair.bbox,
        quickPhrase: "Lounge chair. Finding prices."
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
        sourcesOK: nil,
        item: finishedItem
    )

    // MARK: Glasses mode (G1–G7)

    /// G3: the lamp the user asked about by voice.
    static let lampDetect = DetectResponse(
        captureID: "cap_lamp",
        imageURL: "asset:art-arc-lamp",
        item: Item(category: "arc lamp", description: nil, style: nil, colors: [], materials: [],
                   brandGuess: nil, confidence: 0, searchQueries: [], bbox: lamp.bbox,
                   quickPhrase: "Lamp. Finding prices."),
        segmentation: Segmentation(method: .sam3, bbox: lamp.bbox, maskScore: 0.88, cutoutPath: "",
                                   cutoutURL: "asset:art-arc-lamp", maskPreviewURL: nil, latencyMS: 900),
        quickPhrase: "Lamp. Finding prices.",
        latencyMS: 1400
    )

    static let lampSearch = SearchResponse(
        captureID: "cap_lamp",
        bestMatch: Product(title: "Brass Arc Floor Lamp", store: "West Elm", price: 189, currency: "USD",
                           url: "https://www.westelm.com", imageURL: "asset:art-arc-lamp", source: .shopping,
                           tier: .new, sourceRank: 0, matchScore: nil, savingsPct: nil, matchReason: nil),
        dupes: [
            Product(title: "Arc Floor Lamp, Brass", store: "Amazon", price: 164, currency: "USD",
                    url: "https://www.amazon.com", imageURL: "asset:art-arc-lamp", source: .amazon,
                    tier: .dupe, sourceRank: 0, matchScore: nil, savingsPct: 13, matchReason: nil),
        ],
        used: [],
        priceInsight: PriceInsight(typicalLow: 150, typicalHigh: 240, median: 189, sampleSize: 7,
                                   bestMatchVerdict: .fair,
                                   verdictText: "West Elm is within the typical $150–$240 range."),
        spokenSummary: "West Elm has it for $189. Fair price.",
        timingMS: [:],
        sourcesOK: nil,
        item: Item(category: "brass arc lamp", description: "Arc floor lamp with a brass shade",
                   style: "mid-century", colors: ["brass", "white"], materials: ["brass", "marble"],
                   brandGuess: "West Elm", confidence: 0.82, searchQueries: [], bbox: lamp.bbox,
                   quickPhrase: "Lamp. Finding prices.")
    )

    /// G5 and G7: today's captures.
    static let captures: [Capture] = {
        let today = Calendar.current.startOfDay(for: .now)
        func at(_ hour: Int, _ minute: Int) -> Date { today.addingTimeInterval(TimeInterval(hour * 3600 + minute * 60)) }
        return [
            Capture(id: "cap_lamp", title: "Brass arc lamp", price: 189, verdict: .fair, image: "asset:art-arc-lamp",
                    room: "Maya’s apartment", date: at(16, 12), product: lampSearch.bestMatch),
            Capture(id: "cap_7f3a", title: "Walnut lounge chair", price: 1299, verdict: .fair,
                    image: "asset:art-lounge-chair", room: "Maya’s apartment", date: at(16, 8), product: search.bestMatch),
            Capture(id: "cap_vase", title: "Ceramic bud vase", price: 64, verdict: .low, image: "asset:art-ceramic-vase",
                    room: "Maya’s apartment", date: at(16, 1),
                    product: Product(title: "Ceramic Bud Vase", store: "Heath Ceramics", price: 64, currency: "USD",
                                     url: "https://www.heathceramics.com", imageURL: "asset:art-ceramic-vase",
                                     source: .shopping, tier: .new, sourceRank: 0, matchScore: nil, savingsPct: nil,
                                     matchReason: nil)),
            Capture(id: "cap_shoe", title: "Court runner", price: 135, verdict: .high, image: "asset:art-sneaker",
                    room: "Blue Bottle Coffee", date: at(11, 20),
                    product: Product(title: "Court Runner", store: "Allbirds", price: 135, currency: "USD",
                                     url: "https://www.allbirds.com", imageURL: "asset:art-sneaker",
                                     source: .amazon, tier: .new, sourceRank: 0, matchScore: nil, savingsPct: nil,
                                     matchReason: nil)),
        ]
    }()

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
