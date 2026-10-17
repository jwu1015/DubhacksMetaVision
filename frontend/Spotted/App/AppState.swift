import Foundation
import Observation

@MainActor
@Observable
final class AppState {
    enum Tab: String, CaseIterable {
        case live = "Live", saved = "Saved", cart = "Cart", profile = "Profile"
    }

    enum Connection {
        case live, reconnecting, disconnected
    }

    enum DetailTab: String, CaseIterable {
        case bestMatch = "Best match", dupes = "Dupes", used = "Used"
    }

    var tab: Tab = .live
    var connection: Connection = .live
    var isCameraPaused = false

    // Live view
    var detections: [Detection] = MockData.detections
    var scan: ScanState = .idle
    var selectedTrackID: String?
    /// Sources that have answered while /search runs.
    var finishedSources: Set<ProductSource> = []
    var currentRoom = "Maya’s apartment"

    // Sheets
    var isListening = false
    var isShowingDetail = false
    var detailTab: DetailTab = .bestMatch
    var isShowingCheckout = false

    // Saved + cart
    var rooms = MockData.rooms
    var saved = MockData.saved
    var cart = MockData.cart
    var agentLimit = AgentLimit(amount: 2000, cardLast4: "4242")

    static let taxRate = 0.1035

    var selectedDetection: Detection? {
        detections.first { $0.trackID == selectedTrackID }
    }

    var currentSearch: SearchResponse? {
        if case let .matched(_, search) = scan { return search }
        return nil
    }

    var currentDetect: DetectResponse? {
        switch scan {
        case let .detected(detect), let .matched(detect, _): detect
        default: nil
        }
    }

    // MARK: - Scanning

    /// Mock of the select → /detect → /search pipeline with the latency budget from FRONTEND.md.
    func select(_ detection: Detection) {
        selectedTrackID = detection.trackID
        finishedSources = []
        scan = .looking
        Task {
            try? await Task.sleep(for: .seconds(0.8))
            guard scan == .looking, selectedTrackID == detection.trackID else { return }
            scan = .detected(MockData.detect)

            try? await Task.sleep(for: .seconds(0.9))
            guard case .detected = scan else { return }
            finishedSources.insert(.lens)

            try? await Task.sleep(for: .seconds(1.6))
            guard case .detected = scan else { return }
            finishedSources = Set(ProductSource.searchable)
            scan = .matched(MockData.detect, MockData.search)
        }
    }

    func clearScan() {
        scan = .idle
        selectedTrackID = nil
        finishedSources = []
    }

    func retry() {
        if let detection = selectedDetection ?? detections.first {
            select(detection)
        }
    }

    /// Re-runs /search for the current scan. Mocked: every source answers this time.
    func retrySearch() {
        if case let .matched(detect, _) = scan {
            scan = .matched(detect, MockData.search)
        }
    }

    // MARK: - Saved

    func isSaved(_ title: String) -> Bool {
        saved.contains { $0.title == title }
    }

    func toggleSaveCurrent() {
        guard let detect = currentDetect else { return }
        let title = currentSearch?.bestMatch?.title ?? detect.category.sentenceCased
        if isSaved(title) {
            saved.removeAll { $0.title == title }
        } else {
            let match = currentSearch?.bestMatch
            saved.insert(
                SavedItem(id: UUID().uuidString, title: title, store: match?.store ?? detect.item.brandGuess ?? "",
                          price: match?.price ?? 0, image: match?.imageURL ?? detect.cutoutURL ?? "",
                          room: currentRoom),
                at: 0
            )
        }
    }

    func items(in room: Room) -> [SavedItem] {
        saved.filter { $0.room == room.name }
    }

    // MARK: - Cart

    func addCurrentToCart() {
        guard let match = currentSearch?.bestMatch else { return }
        if let index = cart.firstIndex(where: { $0.title == match.title }) {
            cart[index].quantity += 1
        } else {
            let short = match.title.split(separator: " ").dropFirst().joined(separator: " ")
            cart.append(CartItem(id: UUID().uuidString, title: match.title,
                                 shortTitle: short.isEmpty ? match.title : short, store: match.store,
                                 room: currentRoom, price: match.price, quantity: 1,
                                 image: match.imageURL ?? currentDetect?.cutoutURL ?? ""))
        }
    }

    func changeQuantity(of item: CartItem, by delta: Int) {
        guard let index = cart.firstIndex(of: item) else { return }
        let quantity = cart[index].quantity + delta
        if quantity <= 0 {
            cart.remove(at: index)
        } else {
            cart[index].quantity = quantity
        }
    }

    var subtotal: Double { cart.reduce(0) { $0 + $1.price * Double($1.quantity) } }
    var tax: Double { (subtotal * Self.taxRate * 100).rounded() / 100 }
    var total: Double { subtotal + tax }
    var isWithinLimit: Bool { total <= agentLimit.amount }
    var merchants: String { orderedUnique(cart.map(\.store)).joined(separator: ", ") }

    private func orderedUnique(_ values: [String]) -> [String] {
        var seen = Set<String>()
        return values.filter { seen.insert($0).inserted }
    }

    // MARK: - Mockup states

    /// Jumps straight to one of the Figma frames. Used by the Profile tab's state list and previews.
    func show(_ frame: MockFrame) {
        connection = .live
        isListening = false
        isShowingDetail = false
        isShowingCheckout = false
        detailTab = .bestMatch
        tab = .live
        switch frame {
        case .unselected: clearScan()
        case .searching:
            selectedTrackID = MockData.chair.trackID
            finishedSources = [.lens]
            scan = .detected(MockData.detect)
        case .matched:
            selectedTrackID = MockData.chair.trackID
            scan = .matched(MockData.detect, MockData.search)
        case .couldntTell:
            selectedTrackID = nil
            scan = .failed("Couldn't tell. Try again closer.")
        case .reconnecting:
            clearScan()
            connection = .reconnecting
        case .listening:
            show(.matched)
            isListening = true
        case .productDetail:
            show(.matched)
            isShowingDetail = true
        case .usedTimeout:
            selectedTrackID = MockData.chair.trackID
            scan = .matched(MockData.detect, MockData.searchShoppingTimeout)
            detailTab = .used
            isShowingDetail = true
        case .saved: tab = .saved
        case .cart: tab = .cart
        case .checkout:
            tab = .cart
            isShowingCheckout = true
        case .whichOne:
            selectedTrackID = nil
            scan = .ambiguous(MockData.candidates)
        case .disconnected:
            clearScan()
            connection = .disconnected
        }
    }
}

enum MockFrame: String, CaseIterable, Identifiable {
    case unselected = "01a · Object Detected"
    case searching = "01b · Searching"
    case matched = "01 · Live View"
    case couldntTell = "01c · Couldn't Tell"
    case reconnecting = "01d · Stream Reconnecting"
    case listening = "02 · Listening"
    case productDetail = "03 · Product Detail"
    case usedTimeout = "03b · Dupes & Used"
    case saved = "04 · Saved"
    case cart = "05 · Cart"
    case checkout = "06 · Checkout"
    case whichOne = "07 · Which One?"
    case disconnected = "08 · Glasses Disconnected"

    var id: String { rawValue }
}
