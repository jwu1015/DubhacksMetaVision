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
        didSet {
            live?.setPaused(isCameraPaused)
            updateVideoSource()
        }
    }

    // Live view
    var detections: [Detection] = MockData.detections
    var scan: ScanState = .idle {
        didSet { scanChanged(from: oldValue) }
    }
    var selectedTrackID: String?
    /// Sources that have answered while /search runs.
    var finishedSources: Set<ProductSource> = []
    var currentRoom = "Maya’s apartment"

    // Glasses mode (G1–G7)
    var captureMode: CaptureMode = .phone {
        didSet {
            guard captureMode != oldValue else { return }
            live?.setMode(captureMode == .glasses ? .glasses : .phone)
            // The backend restarts tracking (new ids) for the new camera.
            if live != nil { detections = [] }
            updateVideoSource()
        }
    }
    /// The Meta glasses (DAT SDK). Nil in previews and before launch configures the SDK.
    private(set) var glasses: GlassesManager?
    /// "Glasses connected. Say “Storefront” to capture." on Glasses Home for a few seconds.
    var modeBanner: String?
    /// Every Look the user asked for: Spotted Today and the room recap.
    var captures: [Capture] = []
    /// What the assistant understood (G2). Mock only until the agent reports it.
    var parsedRequest: ParsedRequest?
    var isShowingRoomRecap = false
    private let lookActivity = LookActivityController()

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
        let isLamp = detection.label == "lamp"
        let (mockDetect, mockSearch) = isLamp ? (MockData.lampDetect, MockData.lampSearch) : (MockData.detect, MockData.search)
        Task {
            try? await Task.sleep(for: .seconds(0.8))
            guard scan == .looking, selectedTrackID == detection.trackID else { return }
            scan = .detected(Look(mockDetect))

            try? await Task.sleep(for: .seconds(0.9))
            guard case .detected = scan, selectedTrackID == detection.trackID else { return }
            finishedSources.insert(.shopping)

            try? await Task.sleep(for: .seconds(1.6))
            guard case let .detected(look) = scan, selectedTrackID == detection.trackID else { return }
            finishedSources = Set(ProductSource.searchable)
            scan = .matched(look.finished(with: mockSearch), mockSearch)
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

    /// Adds a product to the local cart (the cart lives only on the phone), with "Added · Undo".
    @discardableResult
    func add(_ product: Product) async -> VoiceActionReply {
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
        undoStack.append(.added(cartItemID: id))
        showToast("Added · \(product.title)", canUndo: true)
        return VoiceActionReply(ok: true)
    }

    // MARK: - Undo and toasts

    private enum UndoEntry {
        case added(cartItemID: String)
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
        case let .added(cartItemID):
            if let item = cart.first(where: { $0.id == cartItemID }) {
                changeQuantity(of: item, by: -1)
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
        publisher.setMode(captureMode == .glasses ? .glasses : .phone)
        publisher.start()
        updateVideoSource()
    }

    // MARK: - Meta glasses

    /// Starts the DAT SDK's glasses tracking. Glasses connecting switches to glasses mode and video;
    /// disconnecting switches back to the phone camera.
    func startGlasses() {
        guard glasses == nil, GlassesManager.configure() else { return }
        let manager = GlassesManager()
        manager.onAvailabilityChange = { [weak self] available in
            self?.setCaptureMode(available ? .glasses : .phone)
            self?.updateVideoSource()
        }
        glasses = manager
        if manager.hasGlasses { setCaptureMode(.glasses) }
    }

    /// Glasses video only in glasses mode with real glasses connected and the camera not paused;
    /// otherwise the phone camera (so "simulate glasses" without hardware still streams something).
    private func updateVideoSource() {
        guard let live else { return }
        if captureMode == .glasses, let glasses, glasses.hasGlasses, !isCameraPaused {
            live.use(.glasses)
            glasses.startCamera(into: live.glassesCapturer)
        } else {
            glasses?.stopCamera()
            live.use(.phone)
        }
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
        let inUse = tab == .live || isListening || isShowingDetail || isShowingCheckout || isShowingRoomRecap
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

    // MARK: - Glasses mode

    /// Switches modes from the glasses' connection state, with the toast from FRONTEND.md.
    func setCaptureMode(_ mode: CaptureMode) {
        guard mode != captureMode else { return }
        captureMode = mode
        clearScan()
        switch mode {
        case .glasses:
            showModeBanner()
        case .phone:
            lookActivity.end()
            showToast("Glasses disconnected. Using your phone camera.", canUndo: false)
        }
    }

    private func showModeBanner() {
        let text = "Glasses connected. Say “Storefront” to capture."
        modeBanner = text
        Task {
            try? await Task.sleep(for: .seconds(6))
            if modeBanner == text { modeBanner = nil }
        }
    }

    /// The next-best object for "Not this one" (G3): the most confident one that isn't selected.
    var nextBestDetection: Detection? {
        detections.filter { $0.trackID != selectedTrackID }.max { $0.score < $1.score }
    }

    func notThisOne() {
        guard let next = nextBestDetection else { return clearScan() }
        select(next)
    }

    /// Live Activities can only start while the app is in front, so re-sync when it becomes active.
    func appBecameActive() {
        if captureMode == .glasses { lookActivity.update(for: scan) }
    }

    private func scanChanged(from old: ScanState) {
        if case let .matched(look, search) = scan, !captures.contains(where: { $0.id == look.captureID }) {
            captures.insert(Capture(id: look.captureID, title: look.name.sentenceCased, price: search.bestMatch?.price,
                                    verdict: search.bestMatch == nil ? nil : search.priceInsight.bestMatchVerdict,
                                    image: search.bestMatch?.imageURL ?? look.cutoutURL ?? "", room: currentRoom,
                                    date: .now, product: search.bestMatch), at: 0)
        }
        if captureMode == .glasses {
            lookActivity.update(for: scan)
        }
    }

    func isSaved(_ capture: Capture) -> Bool { isSaved(capture.title) }

    func toggleSave(_ capture: Capture) {
        if isSaved(capture) {
            saved.removeAll { $0.title == capture.title }
        } else {
            save(capture)
        }
    }

    private func save(_ capture: Capture) {
        guard !isSaved(capture) else { return }
        let item = SavedItem(id: UUID().uuidString, title: capture.title, store: capture.product?.store ?? "",
                             price: capture.price ?? 0, image: capture.image, room: capture.room)
        saved.insert(item, at: 0)
        undoStack.append(.saved(itemID: item.id))
        showToast("Saved · \(capture.title)", canUndo: true)
    }

    /// The room recap (G7): what the user looked at in the current room.
    var recapCaptures: [Capture] { captures.filter { $0.room == currentRoom } }

    func saveRecap(_ ids: Set<String>) {
        for capture in recapCaptures where ids.contains(capture.id) { save(capture) }
        isShowingRoomRecap = false
    }

    /// Deep links from the Live Activity: spotted://save, spotted://add (with ?capture=ID), spotted://talk.
    func handle(_ url: URL) {
        // Meta AI's registration and permission callbacks share the spotted:// scheme.
        if glasses?.handle(url) == true { return }
        guard url.scheme == "spotted" else { return }
        let captureID = URLComponents(url: url, resolvingAgainstBaseURL: false)?
            .queryItems?.first { $0.name == "capture" }?.value
        let isCurrent = captureID == nil || captureID == currentLook?.captureID
        let capture = captures.first { $0.id == captureID }
        switch url.host() {
        case "talk":
            tab = .live
            isListening = true
        case "save":
            if isCurrent, let look = currentLook { save(look) } else if let capture { save(capture) }
        case "add":
            if let product = isCurrent ? currentSearch?.bestMatch : capture?.product {
                Task { await add(product) }
            }
        default:
            break
        }
    }

    // MARK: - Mockup states

    /// Jumps straight to one of the Figma frames. Used by the Profile tab's state list and previews.
    func show(_ frame: MockFrame) {
        connection = .live
        captureMode = .phone
        modeBanner = nil
        parsedRequest = nil
        isShowingRoomRecap = false
        lookActivity.end()
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
        case .glassesHome:
            enterGlasses()
            modeBanner = "Glasses connected. Say “Storefront” to capture."
        case .voiceCommand:
            show(.glassesHome)
            parsedRequest = MockData.parsedRequest
            isListening = true
        case .captureCard, .liveActivity:
            enterGlasses()
            selectedTrackID = MockData.lamp.trackID
            scan = .matched(Look(MockData.lampDetect).finished(with: MockData.lampSearch), MockData.lampSearch)
        case .voiceWhichOne:
            enterGlasses()
            scan = .ambiguous(MockData.candidates)
        case .spottedToday:
            enterGlasses()
            tab = .saved
        case .roomRecap:
            show(.glassesHome)
            isShowingRoomRecap = true
        }
    }

    private func enterGlasses() {
        clearScan()
        captureMode = .glasses
        captures = MockData.captures
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
    case glassesHome = "G1 · Glasses Home"
    case voiceCommand = "G2 · Voice Command"
    case captureCard = "G3 · Capture Card"
    case voiceWhichOne = "G4 · Voice Which-One"
    case spottedToday = "G5 · Spotted Today"
    case liveActivity = "G6 · Live Activity"
    case roomRecap = "G7 · Room Recap"

    var id: String { rawValue }
}
