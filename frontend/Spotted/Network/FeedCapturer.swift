import AVFoundation
import SwiftUI
import WebRTC

/// Pushes frames into the WebRTC video source: the phone's back camera on a device, the mock room in
/// the Simulator (which has no camera). Swap in GlassesVideoCapturer once the DAT SDK is in.
/// Frames are sent upright in portrait, 9:16, with no rotation metadata.
class FeedCapturer: NSObject, @unchecked Sendable {
    static func make(for source: RTCVideoSource) -> FeedCapturer {
        #if targetEnvironment(simulator)
        MockRoomCapturer(source: source)
        #else
        CameraCapturer(source: source)
        #endif
    }

    let source: RTCVideoSource
    let rtcCapturer: RTCVideoCapturer

    init(source: RTCVideoSource) {
        self.source = source
        rtcCapturer = RTCVideoCapturer(delegate: source)
    }

    func start() {}
    func stop() {}

    func send(_ pixelBuffer: CVPixelBuffer, timeStampNs: Int64) {
        let frame = RTCVideoFrame(buffer: RTCCVPixelBuffer(pixelBuffer: pixelBuffer), rotation: ._0, timeStampNs: timeStampNs)
        source.capturer(rtcCapturer, didCapture: frame)
    }
}

/// The phone's back camera at 720p, 15 fps, rotated to portrait at capture.
final class CameraCapturer: FeedCapturer, AVCaptureVideoDataOutputSampleBufferDelegate, @unchecked Sendable {
    private let session = AVCaptureSession()
    private let queue = DispatchQueue(label: "spotted.camera")
    private var isConfigured = false

    override func start() {
        AVCaptureDevice.requestAccess(for: .video) { [self] granted in
            guard granted else { return }
            queue.async { [self] in
                if !isConfigured { configure() }
                if !session.isRunning { session.startRunning() }
            }
        }
    }

    override func stop() {
        queue.async { [self] in session.stopRunning() }
    }

    private func configure() {
        isConfigured = true
        session.beginConfiguration()
        defer { session.commitConfiguration() }
        session.sessionPreset = .hd1280x720
        guard let camera = AVCaptureDevice.default(.builtInWideAngleCamera, for: .video, position: .back),
              let input = try? AVCaptureDeviceInput(device: camera), session.canAddInput(input) else { return }
        session.addInput(input)

        let output = AVCaptureVideoDataOutput()
        output.videoSettings = [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_420YpCbCr8BiPlanarFullRange]
        output.alwaysDiscardsLateVideoFrames = true
        output.setSampleBufferDelegate(self, queue: queue)
        guard session.canAddOutput(output) else { return }
        session.addOutput(output)

        if let connection = output.connection(with: .video), connection.isVideoRotationAngleSupported(90) {
            connection.videoRotationAngle = 90
        }
        if (try? camera.lockForConfiguration()) != nil {
            camera.activeVideoMinFrameDuration = CMTime(value: 1, timescale: 15)
            camera.activeVideoMaxFrameDuration = CMTime(value: 1, timescale: 15)
            camera.unlockForConfiguration()
        }
    }

    func captureOutput(_ output: AVCaptureOutput, didOutput sampleBuffer: CMSampleBuffer, from connection: AVCaptureConnection) {
        guard let pixelBuffer = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        let seconds = CMTimeGetSeconds(CMSampleBufferGetPresentationTimeStamp(sampleBuffer))
        send(pixelBuffer, timeStampNs: Int64(seconds * 1_000_000_000))
    }
}

/// Simulator only: streams a still of the mock living room, so the backend has something to detect.
final class MockRoomCapturer: FeedCapturer, @unchecked Sendable {
    private var timer: Timer?
    private var frame: CVPixelBuffer?

    override func start() {
        Task { @MainActor [weak self] in
            guard let self else { return }
            if frame == nil { frame = Self.renderRoom() }
            timer?.invalidate()
            timer = Timer.scheduledTimer(withTimeInterval: 1.0 / 15, repeats: true) { [weak self] _ in
                guard let self, let frame = self.frame else { return }
                self.send(frame, timeStampNs: Int64(CACurrentMediaTime() * 1_000_000_000))
            }
        }
    }

    override func stop() {
        Task { @MainActor in
            timer?.invalidate()
            timer = nil
        }
    }

    @MainActor
    private static func renderRoom(width: Int = 504, height: Int = 896) -> CVPixelBuffer? {
        let renderer = ImageRenderer(content: MockCameraFeed().frame(width: CGFloat(width), height: CGFloat(height)))
        renderer.scale = 1
        guard let image = renderer.cgImage else { return nil }

        var buffer: CVPixelBuffer?
        let attributes = [kCVPixelBufferIOSurfacePropertiesKey: [:]] as CFDictionary
        CVPixelBufferCreate(kCFAllocatorDefault, width, height, kCVPixelFormatType_32BGRA, attributes, &buffer)
        guard let buffer else { return nil }
        CVPixelBufferLockBaseAddress(buffer, [])
        defer { CVPixelBufferUnlockBaseAddress(buffer, []) }
        let context = CGContext(
            data: CVPixelBufferGetBaseAddress(buffer), width: width, height: height, bitsPerComponent: 8,
            bytesPerRow: CVPixelBufferGetBytesPerRow(buffer), space: CGColorSpaceCreateDeviceRGB(),
            bitmapInfo: CGImageAlphaInfo.premultipliedFirst.rawValue | CGBitmapInfo.byteOrder32Little.rawValue
        )
        context?.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
        return buffer
    }
}
