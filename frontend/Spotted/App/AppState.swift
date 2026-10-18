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
            finishedSources.insert(.shopping)

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
            save(look)
        }
    }

    /// Saves the Look if it isn't saved yet. Undoable, with "Saved · Undo".
    private func save(_ look: Look) {
        let title = look.name.sentenceCased
        guard !isSaved(title) else { return }
        let match = currentSearch?.bestMatch
        let item = SavedItem(id: UUID().uuidString, title: title, store: match?.store ?? look.item?.brandGuess ?? "",
                             price: match?.price ?? 0, image: match?.imageURL ?? look.cutoutURL ?? "",
                             room: currentRoom)
        saved.insert(item, at: 0)
        undoStack.append(.saved(itemID: item.id))
        showToast("Saved · \(title)", canUndo: true)
    }

    func items(in room: Room) -> [SavedItem] {
        saved.filter { $0.room == room.name }
    }

    // MARK: - Cart

    func addCurrentToCart() {
        guard let match = currentSearch?.bestMatch else { return }
        Task { await add(match) }
    }

    /// Adds a product: POST /cart when live, then the local cart, with "Added · Undo".
    @discardableResult
    func add(_ product: Product) async -> VoiceActionReply {
        var serverIndex: Int?
        if let api {
            do {
                serverIndex = try await api.addToCart(product).items.count - 1
            } catch {
                print("[Cart] POST /cart failed: \(error)")
                showToast("Couldn't add it. Try again.", canUndo: false)
                return VoiceActionReply(ok: false, detail: "Couldn't add it. Try again.")
            }
        }
        let id: String
        if let index = cart.firstIndex(where: { $0.title == product.title && $0.store == product.store }) {
            cart[index].quantity += 1
            id = cart[index].id
        } else {
            let short = product.title.split(separator: " ").dropFirst().joined(separator: " ")
            let item = CartItem(id: UUID().uuidString, title: product.title,
                                shortTitle: short.isEmpty ? product.title : short, store: product.store,
                                room: currentRoom, price: product.price, quantity: 1,
                                image: product.imageURL ?? currentLook?.cutoutURL ?? "")
            cart.append(item)
            id = item.id
        }
        undoStack.append(.added(cartItemID: id, serverIndex: serverIndex))
        showToast("Added · \(product.title)", canUndo: true)
        return VoiceActionReply(ok: true)
    }

    // MARK: - Undo and toasts

    private enum UndoEntry {
        case added(cartItemID: String, serverIndex: Int?)
        case saved(itemID: String)
    }

    struct Toast: Identifiable, Equatable {
        let id = UUID()
        let text: String
        let canUndo: Bool
    }

    /// "Added · Undo". Save and add are instant but undoable for 10 s.
    var toast: Toast?
    private var undoStack: [UndoEntry] = []

    private func showToast(_ text: String, canUndo: Bool) {
        let toast = Toast(text: text, canUndo: canUndo)
        self.toast = toast
        Task {
            try? await Task.sleep(for: .seconds(10))
            if self.toast?.id == toast.id { self.toast = nil }
        }
    }

    /// Undoes the last add or save.
    @discardableResult
    func undoLast() async -> VoiceActionReply {
        toast = nil
        guard let entry = undoStack.popLast() else {
            return VoiceActionReply(ok: false, detail: "Nothing to undo.")
        }
        switch entry {
        case let .added(cartItemID, serverIndex):
            if let item = cart.first(where: { $0.id == cartItemID }) {
                changeQuantity(of: item, by: -1)
            }
            if let api, let serverIndex {
                _ = try? await api.removeFromCart(at: serverIndex)
            }
        case let .saved(itemID):
            saved.removeAll { $0.id == itemID }
        }
        return VoiceActionReply(ok: true)
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
        let api = APIClient(baseURL: baseURL)
        self.api = api
        let voice = VoiceAssistant(api: api)
        voice.perform = { [weak self] action in
            await self?.perform(action) ?? VoiceActionReply(ok: false, detail: "The app isn't ready.")
        }
        self.voice = voice
        let publisher = WebRTCPublisher(api: api)
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
            say(look.quickPhrase)
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
            say(search.spokenSummary)
        case .failed, .nothing:
            isAwaitingLook = false
            let line = message.message ?? "Couldn't tell. Try again closer."
            scan = .failed(line)
            say(line)
        case .detected, .unknown:
            break
        }
    }

    /// The app's own speech, only when the voice assistant isn't connected (it speaks the Look itself).
    private func say(_ text: String) {
        guard voice?.isConnected != true else { return }
        speaker.say(text)
    }

    // MARK: - Voice assistant (LiveKit)

    private var api: APIClient?
    private(set) var voice: VoiceAssistant?

    /// Connect only while voice is in use: the app is in front and the Live view, the assistant
    /// sheet, results or the pay sheet is up. The mic is transcribed the whole time it's connected.
    func updateVoice(appIsActive: Bool) {
        guard let voice else { return }
        let inUse = tab == .live || isListening || isShowingDetail || isShowingCheckout
        appIsActive && inUse ? voice.connect() : voice.disconnect()
    }

    /// One action the assistant asks for through the "action" RPC. It waits at most 15 s for the reply.
    private func perform(_ action: VoiceAction) async -> VoiceActionReply {
        switch action.name {
        case .addToCart:
            guard let product = action.product ?? currentSearch?.bestMatch else {
                return VoiceActionReply(ok: false, detail: "Nothing to add yet.")
            }
            return await add(product)
        case .saveItem:
            guard let look = currentLook, action.captureID == nil || action.captureID == look.captureID else {
                return VoiceActionReply(ok: false, detail: "That item isn't on screen anymore.")
            }
            save(look)
            return VoiceActionReply(ok: true)
        case .showResults:
            guard currentSearch != nil else {
                return VoiceActionReply(ok: false, detail: "No prices yet.")
            }
            detailTab = switch action.tab {
            case .dupes: .dupes
            case .used: .used
            default: .bestMatch
            }
            isShowingDetail = true
            return VoiceActionReply(ok: true)
        case .undoLast:
            return await undoLast()
        case .startCheckout:
            guard !cart.isEmpty else {
                return VoiceActionReply(ok: false, detail: "Your cart is empty.")
            }
            // Reply as soon as the sheet is up; Face ID and payment stay with the user.
            isShowingDetail = false
            tab = .cart
            isShowingCheckout = true
            return VoiceActionReply(ok: true)
        case .unknown:
            return VoiceActionReply(ok: false, detail: "Not in the app yet.")
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
            finishedSources = [.shopping]
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
