import CoreMedia
import Foundation
import MWDATCamera
import MWDATCore
import Observation
import OSLog

private let log = Logger(subsystem: "com.dubhacks.spotted", category: "Glasses")

/// Meta glasses through the Wearables Device Access Toolkit: registration with the Meta AI app,
/// which glasses are connected, and the glasses camera stream that feeds the session's video track.
///
/// Needs Developer Mode on in the Meta AI app (Settings › your glasses › Developer Mode) and a real
/// iPhone; there's no Bluetooth to the glasses in the Simulator.
@MainActor
@Observable
final class GlassesManager {
    enum CameraState: Equatable {
        case idle, connecting, streaming
        /// A tap on the glasses' touchpad (or putting them on) paused the stream; another tap resumes it.
        case paused
        case failed(String)
    }

    private(set) var registration: RegistrationState
    /// The glasses the SDK would stream from right now: registered, on and in range.
    private(set) var activeDevice: DeviceIdentifier?
    private(set) var camera: CameraState = .idle
    /// The last registration or permission error, in the SDK's words.
    var errorMessage: String?

    var isRegistered: Bool { registration == .registered }
    var hasGlasses: Bool { isRegistered && activeDevice != nil }

    @ObservationIgnored private let wearables: any WearablesInterface
    /// One selector for the manager's lifetime, as in Meta's CameraAccess sample: sessions use it to find
    /// the glasses, and its active device tells whether there are any.
    @ObservationIgnored private let deviceSelector: AutoDeviceSelector
    @ObservationIgnored private var session: DeviceSession?
    @ObservationIgnored private var activeCamera: Camera?
    /// The capturer the app wants the glasses' frames in, from startCamera() until stopCamera().
    @ObservationIgnored private weak var target: GlassesCapturer?
    @ObservationIgnored private let tokens = ListenerTokenBag()
    @ObservationIgnored private var watchers: [Task<Void, Never>] = []

    /// Call once at launch, before creating a GlassesManager.
    static func configure() -> Bool {
        do {
            try Wearables.configure()
            return true
        } catch {
            log.error("Couldn't configure the Wearables SDK: \(error.localizedDescription, privacy: .public)")
            return false
        }
    }

    init() {
        wearables = Wearables.shared
        deviceSelector = AutoDeviceSelector(wearables: wearables)
        registration = wearables.registrationState
        activeDevice = deviceSelector.activeDevice
        watchers.append(Task { [weak self, wearables] in
            for await state in wearables.registrationStateStream() {
                self?.apply(registration: state)
            }
        })
        watchers.append(Task { [weak self, deviceSelector] in
            for await device in deviceSelector.activeDeviceStream() {
                self?.apply(activeDevice: device)
            }
        })
    }

    /// The camera isn't stopped when the glasses drop out: its session waits for them and reconnects.
    private func apply(registration: RegistrationState? = nil, activeDevice: DeviceIdentifier?? = nil) {
        if let registration { self.registration = registration }
        if let activeDevice { self.activeDevice = activeDevice }
    }

    // MARK: - Registration and permission (each switches to the Meta AI app)

    func connect() {
        errorMessage = nil
        Task {
            do {
                if !isRegistered {
                    try await wearables.startRegistration()
                } else if try await wearables.checkPermissionStatus(.camera) != .granted {
                    _ = try await wearables.requestPermission(.camera)
                }
            } catch {
                errorMessage = error.localizedDescription
            }
        }
    }

    func disconnect() {
        stopCamera()
        Task {
            do { try await wearables.startUnregistration() } catch { errorMessage = error.localizedDescription }
        }
    }

    /// Meta AI's callback after registration or a permission prompt. Returns true if it was ours.
    func handle(_ url: URL) -> Bool {
        guard URLComponents(url: url, resolvingAgainstBaseURL: false)?
            .queryItems?.contains(where: { $0.name == "metaWearablesAction" }) == true else { return false }
        Task {
            do { _ = try await wearables.handleUrl(url) } catch { errorMessage = error.localizedDescription }
        }
        return true
    }

    // MARK: - Camera

    /// Streams the glasses camera into `capturer` (raw frames, 504×896 at 15 fps, the backend's size) until
    /// stopCamera(). Follows Meta's CameraAccess sample: start a DeviceSession, add the camera once it's
    /// started, start its stream. If the session stops on its own (the glasses went away for good, or an
    /// error), a new one starts 2 s later.
    func startCamera(into capturer: GlassesCapturer) {
        target = capturer
        guard session == nil else { return }
        camera = .connecting
        Task { [weak self] in
            guard let self else { return }
            do {
                guard try await wearables.checkPermissionStatus(.camera) == .granted else {
                    camera = .failed("Allow camera access for Spotted in the Meta AI app.")
                    return
                }
                guard target != nil, session == nil else { return }
                let session = try wearables.createSession(deviceSelector: deviceSelector)
                self.session = session
                // Subscribe before start() so no state change is missed.
                session.statePublisher.listen { [weak self] state in
                    Task { @MainActor in self?.sessionChanged(state) }
                }.store(in: tokens)
                session.errorPublisher.listen { [weak self] error in
                    let message = error.localizedDescription
                    Task { @MainActor in self?.errorMessage = message }
                }.store(in: tokens)
                try session.start()
            } catch {
                log.error("Glasses session failed: \(error.localizedDescription, privacy: .public)")
                teardown()
                camera = .failed(error.localizedDescription)
            }
        }
    }

    func stopCamera() {
        target = nil
        teardown()
        if camera != .idle { camera = .idle }
    }

    private func sessionChanged(_ state: DeviceSessionState) {
        log.info("Glasses session: \(state.description, privacy: .public)")
        switch state {
        case .started: addCamera()
        case .stopped: sessionStopped()
        default: break
        }
    }

    private func addCamera() {
        guard let session, activeCamera == nil, let target else { return }
        let config = StreamConfiguration(videoCodec: .raw, resolution: .medium, frameRate: 15)
        do {
            guard let newCamera = try session.addCamera(config: config) else {
                throw DeviceSessionError.capabilityNotFound
            }
            activeCamera = newCamera
            let stream = newCamera.stream
            stream.statePublisher.listen { [weak self] state in
                Task { @MainActor in self?.streamChanged(state) }
            }.store(in: tokens)
            stream.errorPublisher.listen { [weak self] error in
                let message = error.localizedDescription
                Task { @MainActor in self?.camera = .failed(message) }
            }.store(in: tokens)
            stream.videoFramePublisher.listen { [weak target] frame in
                target?.send(frame)
            }.store(in: tokens)
            stream.start()
        } catch {
            log.error("Glasses camera failed: \(error.localizedDescription, privacy: .public)")
            camera = .failed(error.localizedDescription)
        }
    }

    /// The SDK stopped the session itself. Start over while the app still wants the glasses' frames.
    private func sessionStopped() {
        guard session != nil else { return }
        teardown()
        guard target != nil else {
            camera = .idle
            return
        }
        camera = .connecting
        Task {
            try? await Task.sleep(for: .seconds(2))
            if let target, session == nil { startCamera(into: target) }
        }
    }

    private func teardown() {
        tokens.clear()
        activeCamera?.stop()
        activeCamera = nil
        session?.stop()
        session = nil
    }

    private func streamChanged(_ state: StreamState) {
        switch state {
        case .streaming: camera = .streaming
        case .starting, .waitingForDevice: camera = .connecting
        case .paused: camera = .paused
        case .stopped, .stopping: if case .failed = camera {} else if target != nil { camera = .connecting }
        }
    }
}
