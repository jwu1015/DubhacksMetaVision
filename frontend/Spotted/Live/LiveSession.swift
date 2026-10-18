import Foundation
import LiveKit

/// Where the Live view's updates come from: the backend's room, or MockSession without a backend.
@MainActor
protocol UpdateSource: AnyObject {
    var onUpdate: ((Update) -> Void)? { get set }
    /// Picks an object. Answers its Look as it stands; later steps arrive as updates.
    func select(uid: String) async throws -> Look
}

private let vision = Participant.Identity(from: "vision")
private let updatesTopic = "updates"

/// The session's LiveKit room, shared with the backend ("vision") and, on demand, the voice agent.
/// Publishes the camera as one video track, passes the backend's messages on as updates and calls its RPCs.
/// When the room ends (the backend opened another one, or the connection dropped for good), it joins a
/// new room 3 s later.
@MainActor
final class LiveSession: NSObject, UpdateSource {
    /// Both feed the same track, so switching needs no republish: the other capturer just stops.
    enum VideoSource {
        case phone, glasses
    }

    var onUpdate: ((Update) -> Void)?
    var onConnectionChange: ((Bool) -> Void)?

    /// One room for the app's lifetime, rejoined as needed, so the handlers registered on it stay.
    let room = LiveKit.Room()
    let identity = Participant.Identity(from: "phone-" + UUID().uuidString.prefix(8).lowercased())
    let videoTrack: LocalVideoTrack
    /// Fed by GlassesManager while the glasses are the source.
    let glassesCapturer: GlassesCapturer

    private let api: APIClient
    /// What the backend tracks, sent with every token request (a rejoin opens a new room).
    private let category: String
    private let capturer: FeedCapturer
    private var activeSource: VideoSource = .phone
    private var isPaused = false
    private var mode: CaptureMode = .phone
    private var joinTask: Task<Void, Never>?

    static func make(api: APIClient, category: String) async -> LiveSession {
        // ~504×896 at 15 fps, per the stream spec in FRONTEND.md.
        let options = BufferCaptureOptions(dimensions: Dimensions(width: 504, height: 896), fps: 15)
        let track = await LocalVideoTrack.createBufferTrack(name: "feed", source: .camera, options: options)
        return LiveSession(api: api, category: category, track: track)
    }

    private init(api: APIClient, category: String, track: LocalVideoTrack) {
        self.api = api
        self.category = category
        videoTrack = track
        // createBufferTrack always makes a BufferCapturer.
        let buffer = track.capturer as! BufferCapturer
        capturer = FeedCapturer.make(for: buffer)
        glassesCapturer = GlassesCapturer(buffer: buffer)
        super.init()
        room.add(delegate: self)
    }

    /// Joins the room. Only the active source's capturer runs, so the track carries one camera's frames.
    func start() {
        if activeSource == .phone, !isPaused { capturer.start() }
        Task { [weak self, room] in
            do {
                try await room.registerTextStreamHandler(for: updatesTopic) { [weak self] reader, _ in
                    let text = try await reader.readAll()
                    await self?.receive(Data(text.utf8))
                }
            } catch {
                print("[Live] couldn't listen for looks: \(error)")
            }
        }
        join()
    }

    func select(uid: String) async throws -> Look {
        guard let look = try await rpc("select", ["uid": uid]).look else {
            throw APIError(detail: "That object isn't tracked any more.")
        }
        return look
    }

    /// Brings the voice agent into the room, or sends it away.
    func setVoice(_ on: Bool) async throws {
        _ = try await rpc("voice", ["on": on])
    }

    /// Camera privacy: pausing stops capture entirely, and the backend purges what it was tracking.
    func setPaused(_ paused: Bool) {
        guard paused != isPaused else { return }
        isPaused = paused
        if paused || activeSource == .glasses { capturer.stop() } else { capturer.start() }
        send("pause", ["paused": paused])
    }

    /// Glasses or phone: the backend forgets what it tracked and starts over with the new camera.
    func setMode(_ mode: CaptureMode) {
        guard mode != self.mode else { return }
        self.mode = mode
        send("mode", ["mode": Self.name(of: mode)])
    }

    /// Switches between the phone camera and the glasses. GlassesManager starts and stops the glasses stream.
    func use(_ source: VideoSource) {
        guard source != activeSource else { return }
        activeSource = source
        if source == .glasses || isPaused { capturer.stop() } else { capturer.start() }
    }

    // MARK: - Room

    private func join(after delay: Duration = .zero) {
        joinTask?.cancel()
        joinTask = Task { [weak self] in
            try? await Task.sleep(for: delay)
            guard let self, !Task.isCancelled else { return }
            do {
                let token = try await api.sessionToken(identity: identity.stringValue, category: category)
                try await room.connect(url: token.serverURL, token: token.participantToken)
                // One video track per session: a rejoin publishes it again only if the room dropped it.
                if room.localParticipant.localVideoTracks.isEmpty {
                    try await room.localParticipant.publish(videoTrack: videoTrack,
                                                            options: VideoPublishOptions(simulcast: false))
                }
                // The backend's state for this room starts now: tell it ours.
                send("mode", ["mode": Self.name(of: mode)])
                if isPaused { send("pause", ["paused": true]) }
            } catch {
                guard !Task.isCancelled else { return }
                print("[Live] joining failed: \(error)")
                await room.disconnect()
                join(after: .seconds(3))
            }
        }
    }

    private func receive(_ data: Data) {
        do {
            onUpdate?(try JSONDecoder().decode(Update.self, from: data))
        } catch {
            print("[Live] couldn't decode an update: \(error)")
        }
    }

    /// An RPC to the backend. Throws its detail when it answers ok: false.
    private func rpc(_ method: String, _ payload: some Encodable & Sendable) async throws -> RPCReply {
        let json = String(decoding: try JSONEncoder().encode(payload), as: UTF8.self)
        let answer = try await room.localParticipant.performRpc(destinationIdentity: vision, method: method,
                                                                payload: json)
        let reply = try JSONDecoder().decode(RPCReply.self, from: Data(answer.utf8))
        guard reply.ok else { throw APIError(detail: reply.detail ?? "The backend refused \(method).") }
        return reply
    }

    /// An RPC whose answer nobody waits for. Before the room is connected, join() sends the state instead.
    private func send(_ method: String, _ payload: some Encodable & Sendable) {
        guard room.connectionState == .connected else { return }
        Task {
            do {
                _ = try await rpc(method, payload)
            } catch {
                print("[Live] \(method) failed: \(error)")
            }
        }
    }

    private static func name(of mode: CaptureMode) -> String {
        mode == .glasses ? "glasses" : "phone"
    }
}

// MARK: - Room events (called off the main actor)

extension LiveSession: RoomDelegate {
    nonisolated func room(_ room: LiveKit.Room, participant: RemoteParticipant?, didReceiveData data: Data,
                          forTopic topic: String, encryptionType: EncryptionType) {
        guard topic == updatesTopic else { return }
        Task { @MainActor in self.receive(data) }
    }

    nonisolated func room(_ room: LiveKit.Room, didUpdateConnectionState connectionState: ConnectionState,
                          from oldConnectionState: ConnectionState) {
        let isConnected = connectionState == .connected
        Task { @MainActor in self.onConnectionChange?(isConnected) }
    }

    nonisolated func room(_ room: LiveKit.Room, didDisconnectWithError error: LiveKitError?) {
        Task { @MainActor in self.join(after: .seconds(3)) }
    }
}
