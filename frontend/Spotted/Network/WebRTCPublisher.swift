import Foundation
import WebRTC

/// Publishes the camera feed to the backend's WebRTC page and carries the "detections" data channel:
/// detections and Look steps come in, the user's select goes out. Reconnects on its own.
///
/// Signaling: GET /ice-servers, then POST /offer with a non-trickle offer (every ICE candidate
/// gathered first, which aiortc expects). Until TURN is set up, the phone has to be on the same
/// Wi-Fi as the laptop.
final class WebRTCPublisher: NSObject, @unchecked Sendable {
    enum State {
        case connecting, connected, disconnected
    }

    /// Where the video comes from. Both feed the same source and track, so switching needs no
    /// renegotiation: the other capturer just stops sending frames.
    enum VideoSource {
        case phone, glasses
    }

    /// Both are called on the main actor.
    var onMessage: (@MainActor (ChannelMessage) -> Void)?
    var onStateChange: (@MainActor (State) -> Void)?

    /// The outgoing video, for the local preview.
    let videoTrack: RTCVideoTrack
    /// Fed by GlassesManager while the glasses are the source.
    let glassesCapturer: GlassesCapturer
    private(set) var activeSource: VideoSource = .phone
    private var isPaused = false

    private static let factory: RTCPeerConnectionFactory = {
        RTCInitializeSSL()
        return RTCPeerConnectionFactory(encoderFactory: RTCDefaultVideoEncoderFactory(),
                                        decoderFactory: RTCDefaultVideoDecoderFactory())
    }()

    private let api: APIClient
    private let videoSource: RTCVideoSource
    private let capturer: FeedCapturer
    private var peer: RTCPeerConnection?
    private var channel: RTCDataChannel?
    private var connectTask: Task<Void, Never>?

    init(api: APIClient) {
        self.api = api
        videoSource = Self.factory.videoSource()
        // ~504×896 at 15 fps, per the stream spec in FRONTEND.md.
        videoSource.adaptOutputFormat(toWidth: 504, height: 896, fps: 15)
        videoTrack = Self.factory.videoTrack(with: videoSource, trackId: "feed")
        capturer = FeedCapturer.make(for: videoSource)
        glassesCapturer = GlassesCapturer(source: videoSource)
        super.init()
    }

    func start() {
        capturer.start()
        connect()
    }

    /// Camera privacy: pausing stops capture entirely and sends nothing.
    func setPaused(_ paused: Bool) {
        isPaused = paused
        videoTrack.isEnabled = !paused
        if paused || activeSource == .glasses { capturer.stop() } else { capturer.start() }
    }

    /// Switches between the phone camera and the glasses. The glasses stream itself is started and
    /// stopped by GlassesManager.
    func use(_ source: VideoSource) {
        guard source != activeSource else { return }
        activeSource = source
        if source == .glasses || isPaused { capturer.stop() } else { capturer.start() }
    }

    /// Tells the backend the user picked this object; its Look steps follow on the same channel.
    func select(trackID: String) {
        guard let channel, channel.readyState == .open,
              let data = try? JSONEncoder().encode(SelectMessage(trackID: trackID)) else { return }
        channel.sendData(RTCDataBuffer(data: data, isBinary: false))
    }

    // MARK: - Connection

    private func connect(after delay: Duration = .zero) {
        connectTask?.cancel()
        connectTask = Task { [weak self] in
            try? await Task.sleep(for: delay)
            guard let self, !Task.isCancelled else { return }
            self.report(.connecting)
            do {
                try await self.negotiate()
            } catch {
                print("[WebRTC] connect failed: \(error)")
                self.report(.disconnected)
                self.reconnectSoon()
            }
        }
    }

    private func reconnectSoon() {
        let old = peer
        peer = nil
        channel = nil
        old?.close()
        connect(after: .seconds(3))
    }

    private func negotiate() async throws {
        let config = RTCConfiguration()
        config.iceServers = try await api.iceServers().map {
            RTCIceServer(urlStrings: $0.urls, username: $0.username, credential: $0.credential)
        }
        config.sdpSemantics = .unifiedPlan
        let constraints = RTCMediaConstraints(mandatoryConstraints: nil, optionalConstraints: nil)
        guard let peer = Self.factory.peerConnection(with: config, constraints: constraints, delegate: self) else {
            throw URLError(.cannotConnectToHost)
        }
        self.peer = peer

        let transceiver = RTCRtpTransceiverInit()
        transceiver.direction = .sendOnly
        transceiver.streamIds = ["feed"]
        peer.addTransceiver(with: videoTrack, init: transceiver)

        let channelConfig = RTCDataChannelConfiguration()
        channelConfig.isOrdered = true
        let channel = peer.dataChannel(forLabel: "detections", configuration: channelConfig)
        channel?.delegate = self
        self.channel = channel

        let offer = try await createOffer(on: peer, constraints: constraints)
        try await setLocalDescription(offer, on: peer)
        // Non-trickle: wait (up to 5 s) until every candidate is in the local description.
        for _ in 0..<50 where peer.iceGatheringState != .complete {
            try await Task.sleep(for: .milliseconds(100))
        }
        guard let local = peer.localDescription else { throw URLError(.cannotConnectToHost) }
        let answer = try await api.offer(SessionDescription(sdp: local.sdp, type: "offer"))
        try await setRemoteDescription(RTCSessionDescription(type: .answer, sdp: answer.sdp), on: peer)
    }

    private func report(_ state: State) {
        Task { @MainActor in self.onStateChange?(state) }
    }

    // MARK: - Completion handlers → async

    private func createOffer(on peer: RTCPeerConnection, constraints: RTCMediaConstraints) async throws -> RTCSessionDescription {
        try await withCheckedThrowingContinuation { continuation in
            peer.offer(for: constraints) { sdp, error in
                if let sdp {
                    continuation.resume(returning: sdp)
                } else {
                    continuation.resume(throwing: error ?? URLError(.cannotConnectToHost))
                }
            }
        }
    }

    private func setLocalDescription(_ sdp: RTCSessionDescription, on peer: RTCPeerConnection) async throws {
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            peer.setLocalDescription(sdp) { error in
                if let error { continuation.resume(throwing: error) } else { continuation.resume() }
            }
        }
    }

    private func setRemoteDescription(_ sdp: RTCSessionDescription, on peer: RTCPeerConnection) async throws {
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            peer.setRemoteDescription(sdp) { error in
                if let error { continuation.resume(throwing: error) } else { continuation.resume() }
            }
        }
    }
}

// MARK: - Peer connection

extension WebRTCPublisher: RTCPeerConnectionDelegate {
    func peerConnection(_ peerConnection: RTCPeerConnection, didChange newState: RTCPeerConnectionState) {
        guard peerConnection === peer else { return }
        switch newState {
        case .failed:
            report(.disconnected)
            reconnectSoon()
        case .disconnected:
            // Often recovers by itself; show "Reconnecting…" meanwhile.
            report(.connecting)
        default:
            break
        }
    }

    func peerConnection(_ peerConnection: RTCPeerConnection, didChange stateChanged: RTCSignalingState) {}
    func peerConnection(_ peerConnection: RTCPeerConnection, didAdd stream: RTCMediaStream) {}
    func peerConnection(_ peerConnection: RTCPeerConnection, didRemove stream: RTCMediaStream) {}
    func peerConnectionShouldNegotiate(_ peerConnection: RTCPeerConnection) {}
    func peerConnection(_ peerConnection: RTCPeerConnection, didChange newState: RTCIceConnectionState) {}
    func peerConnection(_ peerConnection: RTCPeerConnection, didChange newState: RTCIceGatheringState) {}
    func peerConnection(_ peerConnection: RTCPeerConnection, didGenerate candidate: RTCIceCandidate) {}
    func peerConnection(_ peerConnection: RTCPeerConnection, didRemove candidates: [RTCIceCandidate]) {}
    func peerConnection(_ peerConnection: RTCPeerConnection, didOpen dataChannel: RTCDataChannel) {}
}

// MARK: - Data channel

extension WebRTCPublisher: RTCDataChannelDelegate {
    func dataChannelDidChangeState(_ dataChannel: RTCDataChannel) {
        guard dataChannel === channel else { return }
        switch dataChannel.readyState {
        case .open: report(.connected)
        case .closed: report(.connecting)
        default: break
        }
    }

    func dataChannel(_ dataChannel: RTCDataChannel, didReceiveMessageWith buffer: RTCDataBuffer) {
        do {
            let message = try JSONDecoder().decode(ChannelMessage.self, from: buffer.data)
            Task { @MainActor in self.onMessage?(message) }
        } catch {
            print("[WebRTC] couldn't decode channel message: \(error)")
        }
    }
}
