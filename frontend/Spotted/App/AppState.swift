import CoreGraphics
import Foundation
import LiveKit
import Observation

@MainActor
@Observable
final class AppState {
    enum Tab: String, CaseIterable {
        case live = "Live", saved = "Saved", cart = "Cart", profile = "Profile"
    }

    enum Connection {
        case live, reconnecting
    }

    enum DetailTab: String, CaseIterable {
        case bestMatch = "Best match", dupes = "Dupes", used = "Used"
    }

    var tab: Tab = .live
    var connection: Connection {
        isConnected && status?.tracking != .offline ? .live : .reconnecting
    }
    var isCameraPaused = false {
        didSet {
            live?.setPaused(isCameraPaused)
            updateVideoSource()
        }
    }

    // Live view: what the server last sent. It tracks every object and runs every Look; the app only
    // keeps what's on screen.
    var objects: [LiveObject] = []
    /// The quick labels of the objects in `objects`.
    var labels: [String: LabelMessage] = [:]
    var scan: ScanState = .idle {
        didSet { scanChanged(from: oldValue) }
    }
    var selectedUID: String?
    var currentRoom = "Maya’s apartment"

    /// Where the video comes from, picked on the start screen.
    var captureMode: CaptureMode = .phone {
        didSet {
            guard captureMode != oldValue else { return }
            live?.setMode(captureMode)
            // The backend restarts tracking (new uids) for the new camera.
            if live != nil { clearObjects() }
            updateVideoSource()
        }
    }
    /// The Meta glasses (DAT SDK). Nil in previews and before launch configures the SDK.
    private(set) var glasses: GlassesManager?
    /// Every Look the user asked for: Spotted Today and the room recap.
    var captures: [Capture] = []
    var isShowingRoomRecap = false
    private let lookActivity = LookActivityController()

    // Sheets
    var isListening = false
    var isShowingDetail = false
    var detailTab: DetailTab = .bestMatch
    var isShowingCheckout = false

    // Saved + cart
    var rooms = MockData.rooms
    var saved: [SavedItem] = []
    var cart: [CartItem] = []
    var agentLimit = AgentLimit(amount: 2000, cardLast4: "4242")

    static let taxRate = 0.1035

    var selectedObject: LiveObject? {
        objects.first { $0.uid == selectedUID }
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

    /// Picks an object. The server already runs its Look (or starts it now): show the Look as it stands,
    /// then follow its updates.
    func select(_ object: LiveObject) {
        selectedUID = object.uid
        scan = .looking
        Task {
            do {
                let look = try await source.select(uid: object.uid)
                if selectedUID == object.uid { show(look) }
            } catch {
                guard selectedUID == object.uid else { return }
                scan = .failed((error as? APIError)?.detail ?? "Couldn't tell. Try again closer.")
            }
        }
    }

    func clearScan() {
        scan = .idle
        selectedUID = nil
    }

    func retry() {
        if let object = selectedObject ?? objects.first {
            select(object)
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
                             price: match?.price ?? 0, image: match?.imageURL ?? look.cropURL ?? "",
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
                                image: product.imageURL ?? currentLook?.cropURL ?? "")
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

    // MARK: - Live session (LiveKit)

    /// Set when Config.xcconfig has a backend URL. Nil runs everything on MockSession.
    private var live: LiveSession?
    private var source: UpdateSource
    private var isConnected = true
    /// The backend's last tracking status: its state, category and SAM's prompts.
    private var status: StatusMessage?
    private let speaker = Speaker()

    init() {
        let mock = MockSession()
        source = mock
        mock.onUpdate = { [weak self] in self?.handle($0) }
        mock.start()
    }

    /// The camera track to preview on a device. Nil in mock mode and in the Simulator, which shows
    /// (and streams) the mock room instead.
    var liveVideoTrack: LocalVideoTrack? {
        #if targetEnvironment(simulator)
        nil
        #else
        live?.videoTrack
        #endif
    }

    /// Width ÷ height of the previewed camera frames, for mapping boxes through aspect-fill.
    var previewAspect: CGFloat? { liveVideoTrack == nil ? nil : 9.0 / 16.0 }

    /// False until the start screen's button: nothing streams before that.
    private(set) var isStreaming = false

    /// What the backend tracks, picked on the start screen.
    private(set) var category = TrackingCategory.default.value

    /// Streaming, but the backend hasn't picked SAM's labels from the first frame yet: no object can
    /// show up until it has.
    var isFindingLabels: Bool {
        isStreaming && !isCameraPaused && (status?.prompts.isEmpty ?? true)
    }

    /// The start screen's button: join the session and stream from the chosen camera, tracking `category`.
    func startStreaming(from mode: CaptureMode, category: String) {
        guard !isStreaming else { return }
        captureMode = mode
        self.category = category
        isStreaming = true
        startLive()
    }

    private func startLive() {
        guard live == nil, let baseURL = AppConfig.baseURL else { return }
        clearObjects()
        status = nil
        isConnected = false
        Task { [weak self, category] in
            let session = await LiveSession.make(api: APIClient(baseURL: baseURL), category: category)
            guard let self else { return }
            session.onUpdate = { [weak self] in self?.handle($0) }
            session.onConnectionChange = { [weak self] connected in
                self?.isConnected = connected
                // Joining sends the current state again.
                if connected { self?.clearObjects() }
            }
            let voice = VoiceAssistant(session: session)
            voice.perform = { [weak self] action in
                await self?.perform(action) ?? VoiceActionReply(ok: false, detail: "The app isn't ready.")
            }
            self.voice = voice
            live = session
            source = session
            session.setMode(captureMode)
            session.setPaused(isCameraPaused)
            // Pick the source before starting, so the phone camera never starts in glasses mode.
            updateVideoSource()
            session.start()
        }
    }

    // MARK: - Meta glasses

    /// Starts the DAT SDK's glasses tracking, so the start screen knows whether glasses are there.
    func startGlasses() {
        guard glasses == nil, GlassesManager.configure() else { return }
        glasses = GlassesManager()
    }

    /// The glasses in glasses mode, unless the camera is paused; otherwise the phone camera. Glasses that
    /// drop out stay the source: their session waits for them and reconnects (see GlassesManager).
    private func updateVideoSource() {
        guard let live else { return }
        if captureMode == .glasses, let glasses, !isCameraPaused {
            live.use(.glasses)
            glasses.startCamera(into: live.glassesCapturer)
        } else {
            glasses?.stopCamera()
            live.use(.phone)
        }
    }

    private func handle(_ update: Update) {
        switch update {
        case let .objects(message):
            objects = message.objects
            let uids = Set(objects.map(\.uid))
            labels = labels.filter { uids.contains($0.key) }
        case let .label(label):
            labels[label.uid] = label
        case let .look(look):
            if look.uid == selectedUID { show(look) }
        case let .status(status):
            self.status = status
        case .other:
            break
        }
    }

    /// Spoken feedback, through the phone camera's mode only. With the glasses as the source the audio
    /// would play on them over the Bluetooth link their camera streams on, which can drop the stream.
    private func say(_ text: String) {
        guard captureMode == .phone else { return }
        speaker.say(text)
    }

    private func clearObjects() {
        objects = []
        labels = [:]
    }

    /// The selected object's Look, at its latest step.
    private func show(_ look: Look) {
        let previous = currentLook?.lookID == look.lookID ? currentLook : nil
        // The select's answer can arrive after a later step of the same Look.
        if let previous, previous.stage == .results, look.stage != .results { return }
        switch look.stage {
        case .started, .described:
            scan = .detected(look)
            if previous == nil { say(look.quickPhrase) }
        case .results:
            guard let search = look.search else { return }
            scan = .matched(look, search)
            if previous?.stage != .results { say(search.spokenSummary) }
        case .failed:
            let line = look.message ?? "Couldn't tell. Try again closer."
            scan = .failed(line)
            say(line)
        case .unknown:
            break
        }
    }

    // MARK: - Voice assistant (LiveKit)

    private(set) var voice: VoiceAssistant?

    /// Connect only while voice is in use: the app is in front and the Live view, the assistant
    /// sheet, results or the pay sheet is up. The mic is transcribed the whole time it's connected.
    /// Never with the glasses as the source: their mic would open a Bluetooth audio link next to the
    /// camera stream, which can drop it.
    func updateVoice(appIsActive: Bool) {
        guard let voice else { return }
        let inUse = tab == .live || isListening || isShowingDetail || isShowingCheckout || isShowingRoomRecap
        appIsActive && inUse && captureMode == .phone ? voice.connect() : voice.disconnect()
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
            guard let look = currentLook, action.captureID == nil || action.captureID == look.lookID else {
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

    /// Switches the camera mid-stream. The backend starts tracking over for the new camera.
    func setCaptureMode(_ mode: CaptureMode) {
        guard mode != captureMode else { return }
        captureMode = mode
        clearScan()
        if mode == .phone { lookActivity.end() }
    }

    /// Live Activities can only start while the app is in front, so re-sync when it becomes active.
    func appBecameActive() {
        if captureMode == .glasses { lookActivity.update(for: scan) }
    }

    private func scanChanged(from old: ScanState) {
        if case let .matched(look, search) = scan, !captures.contains(where: { $0.id == look.lookID }) {
            captures.insert(Capture(id: look.lookID, title: look.name.sentenceCased, price: search.bestMatch?.price,
                                    verdict: search.bestMatch == nil ? nil : search.priceInsight.bestMatchVerdict,
                                    image: search.bestMatch?.imageURL ?? look.cropURL ?? "", room: currentRoom,
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
        let isCurrent = captureID == nil || captureID == currentLook?.lookID
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
}
