import AVFoundation
import LiveKit
import os

private let log = Logger(subsystem: "Spotted", category: "Audio")

/// Glasses mode audio: the glasses' mic and speakers over Bluetooth HFP, set up the way Meta's docs say
/// (microphones-and-speakers): mic permission, then HFP, then wait for the route to settle, and only then the
/// camera stream. HFP opened after the stream starts can drop it. If HFP doesn't come up, audio falls back to
/// the phone's mic and speaker. LiveKit's automatic audio setup is off meanwhile, so it doesn't change the route.
@MainActor
enum GlassesAudio {
    enum Route: Equatable {
        case glasses, phone
    }

    /// The route in use, or nil while LiveKit sets up audio itself (phone mode).
    private(set) static var route: Route?
    /// The setup under way. Callers share it, and a caller's cancellation doesn't cut its wait short.
    private static var setup: Task<Route, Error>?

    /// Sets up glasses mode audio, before the glasses stream starts. Takes about 2 s the first time.
    /// Throws only if no audio session could be set at all.
    static func start() async throws -> Route {
        if let route { return route }
        let setup = self.setup ?? Task { try await configure() }
        self.setup = setup
        let route = try await setup.value
        // stop() ran meanwhile: phone mode again.
        guard self.setup == setup else { throw CancellationError() }
        self.setup = nil
        self.route = route
        return route
    }

    /// Back to LiveKit's own audio setup (phone mode).
    static func stop() {
        guard route != nil || setup != nil else { return }
        route = nil
        setup = nil
        let session = AVAudioSession.sharedInstance()
        try? session.setPreferredInput(nil)
        try? session.setActive(false, options: .notifyOthersOnDeactivation)
        AudioManager.shared.audioSession.isAutomaticConfigurationEnabled = true
    }

    private static func configure() async throws -> Route {
        AudioManager.shared.audioSession.isAutomaticConfigurationEnabled = false
        do {
            let route = await requestMicPermission() ? try await startHFP() : .phone
            if route == .phone { try usePhone() }
            logRoute()
            return route
        } catch {
            log.error("Glasses audio failed: \(error.localizedDescription, privacy: .public)")
            AudioManager.shared.audioSession.isAutomaticConfigurationEnabled = true
            throw error
        }
    }

    /// The iOS mic permission. Asked for before HFP, as Meta's sample does. False if the user declined.
    static func requestMicPermission() async -> Bool {
        switch AVAudioApplication.shared.recordPermission {
        case .granted: return true
        case .denied: return false
        default: return await AVAudioApplication.requestRecordPermission()
        }
    }

    /// HFP in and out, with the glasses' mic picked. .glasses once the route has settled on it, else .phone.
    private static func startHFP() async throws -> Route {
        let session = AVAudioSession.sharedInstance()
        try session.setCategory(.playAndRecord, mode: .default, options: [.allowBluetoothHFP])
        try session.setActive(true, options: .notifyOthersOnDeactivation)
        if let hfp = session.availableInputs?.first(where: { $0.portType == .bluetoothHFP }) {
            try session.setPreferredInput(hfp)
        }
        // Meta's sample: "Allow the Bluetooth HFP route to settle."
        try? await Task.sleep(for: .seconds(2))
        let onGlasses = session.currentRoute.inputs.contains { $0.portType == .bluetoothHFP }
        if !onGlasses { log.error("No HFP route to the glasses; using the phone's mic and speaker") }
        return onGlasses ? .glasses : .phone
    }

    /// The phone's mic and speaker, no Bluetooth audio.
    private static func usePhone() throws {
        let session = AVAudioSession.sharedInstance()
        try session.setCategory(.playAndRecord, mode: .default, options: [.defaultToSpeaker])
        try session.setActive(true)
        if let mic = session.availableInputs?.first(where: { $0.portType == .builtInMic }) {
            try session.setPreferredInput(mic)
        }
    }

    private static func logRoute() {
        let route = AVAudioSession.sharedInstance().currentRoute
        log.info("""
            Audio in: \(route.inputs.map(\.portType.rawValue), privacy: .public), \
            out: \(route.outputs.map(\.portType.rawValue), privacy: .public)
            """)
    }
}
