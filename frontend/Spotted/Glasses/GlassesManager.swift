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
        case failed(String)
    }

    private(set) var registration: RegistrationState
    private(set) var devices: [DeviceIdentifier]
    private(set) var camera: CameraState = .idle
    /// The last registration or permission error, in the SDK's words.
    var errorMessage: String?

    var isRegistered: Bool { registration == .registered }
    /// Registered and at least one pair of glasses connected.
    var hasGlasses: Bool { isRegistered && !devices.isEmpty }

    /// Called when `hasGlasses` changes, so the app can switch modes.
    @ObservationIgnored var onAvailabilityChange: ((Bool) -> Void)?

    @ObservationIgnored private let wearables: any WearablesInterface
    @ObservationIgnored private var session: DeviceSession?
    @ObservationIgnored private var activeCamera: Camera?
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
        registration = wearables.registrationState
        devices = wearables.devices
        watchers.append(Task { [weak self, wearables] in
            for await state in wearables.registrationStateStream() {
                self?.apply(registration: state)
            }
        })
        watchers.append(Task { [weak self, wearables] in
            for await devices in wearables.devicesStream() {
                self?.apply(devices: devices)
            }
        })
    }

    private func apply(registration: RegistrationState? = nil, devices: [DeviceIdentifier]? = nil) {
        let had = hasGlasses
        if let registration { self.registration = registration }
        if let devices { self.devices = devices }
        if hasGlasses != had { onAvailabilityChange?(hasGlasses) }
        if !hasGlasses { stopCamera() }
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

    /// Streams the glasses camera into `capturer`: raw frames, 504×896 at 15 fps (the backend's size).
    func startCamera(into capturer: GlassesCapturer) {
        guard hasGlasses, session == nil else { return }
        camera = .connecting
        Task { [weak self, weak capturer, wearables] in
            guard let self else { return }
            do {
                guard try await wearables.checkPermissionStatus(.camera) == .granted else {
                    camera = .failed("Allow camera access for Spotted in the Meta AI app.")
                    return
                }
                let session = try wearables.createSession(deviceSelector: AutoDeviceSelector(wearables: wearables))
                self.session = session
                try session.start()
                for await state in session.stateStream() {
                    if state == .started { break }
                    if state == .stopped { throw DeviceSessionError.sessionAlreadyStopped }
                }
                let config = StreamConfiguration(videoCodec: .raw, resolution: .medium, frameRate: 15)
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
                stream.videoFramePublisher.listen { [weak capturer] frame in
                    capturer?.send(frame)
                }.store(in: tokens)
                stream.start()
            } catch {
                log.error("Glasses camera failed: \(error.localizedDescription, privacy: .public)")
                stopCamera()
                camera = .failed(error.localizedDescription)
            }
        }
    }

    func stopCamera() {
        tokens.clear()
        activeCamera?.stop()
        activeCamera = nil
        session?.stop()
        session = nil
        if camera != .idle { camera = .idle }
    }

    private func streamChanged(_ state: StreamState) {
        switch state {
        case .streaming: camera = .streaming
        case .starting, .waitingForDevice: camera = .connecting
        case .stopped: if case .failed = camera {} else { camera = .idle }
        default: break
        }
    }
}
