import Foundation
import LiveKit
import Observation

/// The Spotted voice assistant over LiveKit. The mic goes to the assistant, which speaks each Look
/// itself ("Lamp. Finding prices.", then the price summary) and asks the app to act through the RPC
/// method "action". Only the voice uses LiveKit; the video stays on our WebRTC.
///
/// Connect only while voice is in use: the mic is transcribed the whole time the phone is in the room,
/// and the free plan has about 50 minutes of speech.
@MainActor
@Observable
final class VoiceAssistant {
    enum Status: Equatable {
        case off, connecting, connected
        case failed(String)
    }

    /// One transcript line, from the user or the assistant. Interim text is replaced until it's final.
    struct Line: Identifiable, Equatable {
        let id: String
        let isUser: Bool
        var text: String
        var isFinal: Bool
    }

    private(set) var status: Status = .off
    private(set) var lines: [Line] = []

    var isConnected: Bool { status == .connected }
    var lastUserLine: Line? { lines.last { $0.isUser } }
    var lastAssistantLine: Line? { lines.last { !$0.isUser } }

    /// Runs one action from the assistant. Set by AppState.
    var perform: ((VoiceAction) async -> VoiceActionReply)?

    private let api: APIClient
    private let identity = "phone-\(UUID().uuidString.prefix(8).lowercased())"
    private var room: LiveKit.Room?
    private var connectTask: Task<Void, Never>?

    init(api: APIClient) {
        self.api = api
    }

    func connect() {
        guard room == nil, connectTask == nil else { return }
        status = .connecting
        connectTask = Task { [weak self] in
            guard let self else { return }
            defer { connectTask = nil }
            let room = LiveKit.Room()
            do {
                let token = try await api.voiceToken(identity: identity)
                try await room.registerRpcMethod("action") { [weak self] data in
                    let action = try JSONDecoder().decode(VoiceAction.self, from: Data(data.payload.utf8))
                    let reply = await self?.run(action) ?? VoiceActionReply(ok: false, detail: "The app isn't ready.")
                    return String(decoding: try JSONEncoder().encode(reply), as: UTF8.self)
                }
                try await room.registerTextStreamHandler(for: "lk.transcription") { [weak self] reader, speaker in
                    try await self?.receive(reader, from: speaker)
                }
                try await room.connect(url: token.serverURL, token: token.participantToken,
                                       connectOptions: ConnectOptions(enableMicrophone: true))
                guard !Task.isCancelled else {
                    await room.disconnect()
                    return
                }
                self.room = room
                lines = []
                status = .connected
            } catch {
                print("[Voice] connect failed: \(error)")
                await room.disconnect()
                status = Task.isCancelled ? .off : .failed("Couldn't reach the assistant.")
            }
        }
    }

    func disconnect() {
        connectTask?.cancel()
        connectTask = nil
        let old = room
        room = nil
        status = .off
        Task { await old?.disconnect() }
    }

    /// A typed question, for when talking isn't possible.
    func send(_ text: String) {
        let text = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let room, !text.isEmpty else { return }
        lines.append(Line(id: UUID().uuidString, isUser: true, text: text, isFinal: true))
        Task {
            do {
                _ = try await room.localParticipant.sendText(text, for: "lk.chat")
            } catch {
                print("[Voice] sendText failed: \(error)")
            }
        }
    }

    private func run(_ action: VoiceAction) async -> VoiceActionReply {
        await perform?(action) ?? VoiceActionReply(ok: false, detail: "Not in the app yet.")
    }

    /// Both sides' speech arrives as text streams. Interim user text comes as new streams on the same
    /// segment, so key lines by segment and replace their text; "lk.transcription_final" marks the end.
    private func receive(_ reader: TextStreamReader, from speaker: Participant.Identity) async throws {
        let attributes = reader.info.attributes
        let id = attributes["lk.segment_id"] ?? reader.info.id
        let isUser = speaker.stringValue == identity
        var text = ""
        for try await chunk in reader {
            text += chunk
            update(id: id, isUser: isUser, text: text, isFinal: false)
        }
        update(id: id, isUser: isUser, text: text, isFinal: attributes["lk.transcription_final"] == "true")
    }

    private func update(id: String, isUser: Bool, text: String, isFinal: Bool) {
        if let index = lines.firstIndex(where: { $0.id == id }) {
            lines[index].text = text
            lines[index].isFinal = lines[index].isFinal || isFinal
        } else {
            lines.append(Line(id: id, isUser: isUser, text: text, isFinal: isFinal))
        }
        if lines.count > 40 { lines.removeFirst(lines.count - 40) }
    }
}
