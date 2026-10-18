import CoreGraphics
import Foundation
import Observation
import WebRTC

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
    var isCameraPaused = false {
        didSet { live?.setPaused(isCameraPaused) }
    }

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
    // Start empty so a first scan behaves like a real first run. The Saved, Cart and Checkout
    // mockup frames load the sample items (see show(_:)).
    var saved: [SavedItem] = []
    var cart: [CartItem] = []
    var agentLimit = AgentLimit(amount: 2000, cardLast4: "4242")

    static let taxRate = 0.1035

    var selectedDetection: Detection? {
        detections.first { $0.trackID == selectedTrackID }
    }

    var currentSearch: SearchResponse? {
        if case let .matched(_, search) = scan { return search }
        return nil
    }

    var currentLook: Look? {
        switch scan {
        case let .detected(look), let .matched(look, _): look
        default: nil
        }
    }

    // MARK: - Scanning

    /// Live: sends select on the channel and follows that Look (see handle(_:)).
    /// Mock: fakes detected → results with the latency budget from FRONTEND.md.
    func select(_ detection: Detection) {
        selectedTrackID = detection.trackID
        finishedSources = []
        currentCaptureID = nil
        scan = .looking
        if let live {
            isAwaitingLook = true
            live.select(trackID: detection.trackID)
            return
        }
        Task {
            try? await Task.sleep(for: .seconds(0.8))
            guard scan == .looking, selectedTrackID == detection.trackID else { return }
            scan = .detected(Look(MockData.detect))

            try? await Task.sleep(for: .seconds(0.9))
            guard case .detected = scan, selectedTrackID == detection.trackID else { return }
            finishedSources.insert(.lens)

            try? await Task.sleep(for: .seconds(1.6))
            guard case let .detected(look) = scan, selectedTrackID == detection.trackID else { return }
            finishedSources = Set(ProductSource.searchable)
            scan = .matched(look.finished(with: MockData.search), MockData.search)
        }
    }

    func clearScan() {
        scan = .idle
        selectedTrackID = nil
        finishedSources = []
        currentCaptureID = nil
        isAwaitingLook = false
    }

    func retry() {
        if let detection = selectedDetection ?? detections.first {
            select(detection)
        }
    }

    /// Re-runs /search for the current scan. Mocked: every source answers this time.
    func retrySearch() {
        guard live == nil else { return retry() }
        if case let .matched(look, _) = scan {
            scan = .matched(look, MockData.search)
        }
    }

    // MARK: - Saved

    func isSaved(_ title: String) -> Bool {
        saved.contains { $0.title == title }
    }

    func toggleSaveCurrent() {
        guard let look = currentLook else { return }
        let title = look.name.sentenceCased
        if isSaved(title) {
            saved.removeAll { $0.title == title }
        } else {
            let match = currentSearch?.bestMatch
            saved.insert(
                SavedItem(id: UUID().uuidString, title: title, store: match?.store ?? look.item?.brandGuess ?? "",
                          price: match?.price ?? 0, image: match?.imageURL ?? look.cutoutURL ?? "",
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
                                 image: match.imageURL ?? currentLook?.cutoutURL ?? ""))
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

    // MARK: - Live stream (WebRTC)

    /// Set when Config.xcconfig has a backend URL. Nil runs everything on mock data.
    private var live: WebRTCPublisher?
    /// True from a select until its "detected" (or "failed") arrives.
    private var isAwaitingLook = false
    /// The Look being followed. Messages with any other captureID are stale and ignored.
    private var currentCaptureID: String?
    private var lastDetectionsTS: Int64 = 0
    private let speaker = Speaker()

    /// The camera track to preview on a device. Nil in mock mode and in the Simulator, which shows
    /// (and streams) the mock room instead.
    var liveVideoTrack: RTCVideoTrack? {
        #if targetEnvironment(simulator)
        nil
        #else
        live?.videoTrack
        #endif
    }

    /// Width ÷ height of the previewed camera frames, for mapping boxes through aspect-fill.
    var previewAspect: CGFloat? { liveVideoTrack == nil ? nil : 9.0 / 16.0 }

    func startLive() {
        guard live == nil, let baseURL = AppConfig.baseURL else { return }
        let publisher = WebRTCPublisher(api: APIClient(baseURL: baseURL))
        publisher.onMessage = { [weak self] in self?.handle($0) }
        publisher.onStateChange = { [weak self] state in
            self?.connection = state == .connected ? .live : .reconnecting
        }
        live = publisher
        detections = []
        connection = .reconnecting
        publisher.start()
    }

    private func handle(_ message: ChannelMessage) {
        switch message {
        case let .detections(update):
            guard update.ts >= lastDetectionsTS else { return }
            lastDetectionsTS = update.ts
            detections = update.objects
        case let .look(look):
            handle(look)
        case .other:
            break
        }
    }

    /// Follows the "detected" whose trackID is the selected one, then only that captureID.
    private func handle(_ message: LookMessage) {
        if message.stage == .detected {
            guard isAwaitingLook, message.trackID == selectedTrackID else { return }
            isAwaitingLook = false
            currentCaptureID = message.captureID
            let look = Look(captureID: message.captureID,
                            label: message.label ?? selectedDetection?.label ?? "item",
                            quickPhrase: message.quickPhrase ?? "",
                            cutoutURL: message.cutoutPath.flatMap(AppConfig.url(for:))?.absoluteString,
                            item: nil)
            scan = .detected(look)
            speaker.say(look.quickPhrase)
            return
        }

        // A failure can arrive before "detected", so also accept it for the pending select.
        let isCurrent = message.captureID == currentCaptureID
            || (isAwaitingLook && message.trackID == selectedTrackID)
        guard isCurrent else { return }

        switch message.stage {
        case .described:
            if case var .detected(look) = scan, let item = message.item {
                look.item = item
                scan = .detected(look)
            }
        case .results:
            guard let search = message.search, let look = currentLook else { return }
            finishedSources = Set(ProductSource.searchable)
            scan = .matched(look.finished(with: search), search)
            speaker.say(search.spokenSummary)
        case .failed, .nothing:
            isAwaitingLook = false
            let line = message.message ?? "Couldn't tell. Try again closer."
            scan = .failed(line)
            speaker.say(line)
        case .detected, .unknown:
            break
        }
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
            scan = .detected(Look(MockData.detect))
        case .matched:
            selectedTrackID = MockData.chair.trackID
            scan = .matched(Look(MockData.detect).finished(with: MockData.search), MockData.search)
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
            scan = .matched(Look(MockData.detect).finished(with: MockData.search), MockData.searchShoppingTimeout)
            detailTab = .used
            isShowingDetail = true
        case .saved:
            loadSampleItems()
            tab = .saved
        case .cart:
            loadSampleItems()
            tab = .cart
        case .checkout:
            loadSampleItems()
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

    /// The saved items and cart drawn in the 04 / 05 / 06 mockups.
    private func loadSampleItems() {
        saved = MockData.saved
        cart = MockData.cart
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
