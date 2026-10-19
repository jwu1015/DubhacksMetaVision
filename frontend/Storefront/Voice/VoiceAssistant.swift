import Foundation
import LiveKit
import Observation

/// The voice assistant, in the session's LiveKit room. Voice on brings the agent into the room and
/// publishes the mic; the agent asks the app to act through the RPC method "action".
///
/// Turn it on only while voice is in use: the mic is transcribed the whole time it's published, and the
/// free plan has about 50 minutes of speech.
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

    private let session: LiveSession
    private var connectTask: Task<Void, Never>?
    private var connectAttempt: UUID?

    init(session: LiveSession) {
        self.session = session
        session.onJoin = { [weak self] in self?.rejoined() }
        Task { [weak self] in
            do {
                try await session.room.registerRpcMethod("action") { [weak self] data in
                    NSLog("[Voice] action from %@: %@", data.callerIdentity.stringValue, data.payload)
                    let action = try JSONDecoder().decode(VoiceAction.self, from: Data(data.payload.utf8))
                    let reply = await self?.run(action) ?? VoiceActionReply(ok: false, detail: "The app isn't ready.")
                    NSLog("[Voice] action %@ -> ok: %@ %@", action.name.rawValue, reply.ok ? "true" : "false", reply.detail ?? "")
                    return String(decoding: try JSONEncoder().encode(reply), as: UTF8.self)
                }
                NSLog("[Voice] action handler registered")
                try await session.room.registerTextStreamHandler(for: "lk.transcription") { [weak self] reader, speaker in
                    try await self?.receive(reader, from: speaker)
                }
            } catch {
                print("[Voice] couldn't set up: \(error)")
            }
        }
    }

    func connect() {
        guard status != .connected, connectTask == nil else { return }
        lines = []
        start()
    }

    /// Asks for the agent in the room and publishes the mic. Only the latest attempt updates the state.
    private func start() {
        connectTask?.cancel()
        status = .connecting
        let attempt = UUID()
        connectAttempt = attempt
        connectTask = Task { [weak self] in
            guard let self else { return }
            defer { if connectAttempt == attempt { connectTask = nil } }
            guard await GlassesAudio.requestMicPermission() else {
                if connectAttempt == attempt { status = .failed("Allow the microphone for Storefront in Settings.") }
                return
            }
            do {
                try await session.setVoice(true)
                try await session.room.localParticipant.setMicrophone(enabled: true)
                guard !Task.isCancelled, connectAttempt == attempt else { return }
                status = .connected
                NSLog("[Voice] connected: agent asked for, mic on")
            } catch {
                guard !Task.isCancelled, connectAttempt == attempt else { return }
                NSLog("[Voice] connect failed: %@", String(describing: error))
                status = .failed("Couldn't reach the assistant.")
            }
        }
    }

    /// Each join is a new room: no agent in it and no mic published. If voice is wanted, bring both back.
    /// That also retries an attempt made before the first join finished, which failed with no room.
    private func rejoined() {
        guard status != .off else { return }
        start()
    }

    func disconnect() {
        connectTask?.cancel()
        connectTask = nil
        connectAttempt = nil
        guard status != .off else { return }
        status = .off
        Task { [session] in
            _ = try? await session.room.localParticipant.setMicrophone(enabled: false)
            try? await session.setVoice(false)
        }
    }

    /// A typed question, for when talking isn't possible.
    func send(_ text: String) {
        let text = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard isConnected, !text.isEmpty else { return }
        lines.append(Line(id: UUID().uuidString, isUser: true, text: text, isFinal: true))
        Task {
            do {
                _ = try await session.room.localParticipant.sendText(text, for: "lk.chat")
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
        let isUser = speaker == session.identity
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
