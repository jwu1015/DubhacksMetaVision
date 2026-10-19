import AVFoundation
import LiveKit
import MWDATCamera
import SwiftUI

/// Pushes frames into the session's video track: the phone's back camera on a device, the mock room in
/// the Simulator (which has no camera), or the Meta glasses (GlassesCapturer).
/// Frames are sent upright in portrait, 9:16, with no rotation metadata.
class FeedCapturer: NSObject, @unchecked Sendable {
    static func make(for buffer: BufferCapturer) -> FeedCapturer {
        #if targetEnvironment(simulator)
        MockRoomCapturer(buffer: buffer)
        #else
        CameraCapturer(buffer: buffer)
        #endif
    }

    let buffer: BufferCapturer

    init(buffer: BufferCapturer) {
        self.buffer = buffer
    }

    func start() {}
    func stop() {}

    func send(_ pixelBuffer: CVPixelBuffer, timeStampNs: Int64) {
        buffer.capture(pixelBuffer, timeStampNs: timeStampNs)
    }

    /// Draws an image into a new BGRA pixel buffer.
    static func pixelBuffer(from image: CGImage) -> CVPixelBuffer? {
        let width = image.width, height = image.height
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

/// The Meta glasses camera, fed by GlassesManager's DAT stream (raw frames, 504×896, upright).
/// GlassesManager starts and stops the stream, so start() and stop() do nothing here.
final class GlassesCapturer: FeedCapturer, @unchecked Sendable {
    /// Called on the SDK's queue for every frame.
    func send(_ frame: MWDATCamera.VideoFrame) {
        let sampleBuffer = frame.sampleBuffer
        let time = CMSampleBufferGetPresentationTimeStamp(sampleBuffer)
        let timeStampNs = time.isValid ? Int64(CMTimeGetSeconds(time) * 1_000_000_000)
                                       : Int64(CACurrentMediaTime() * 1_000_000_000)
        // LiveKit drops frames in a pixel format WebRTC can't take (no preview, nothing streamed), so
        // redraw those, and frames without a pixel buffer, from the SDK's image.
        if let pixelBuffer = CMSampleBufferGetImageBuffer(sampleBuffer), Self.isSupported(pixelBuffer) {
            send(pixelBuffer, timeStampNs: timeStampNs)
        } else if let image = frame.makeUIImage()?.cgImage, let pixelBuffer = Self.pixelBuffer(from: image) {
            send(pixelBuffer, timeStampNs: timeStampNs)
        }
    }

    private static func isSupported(_ pixelBuffer: CVPixelBuffer) -> Bool {
        let format = CVPixelBufferGetPixelFormatType(pixelBuffer)
        return VideoCapturer.supportedPixelFormats.contains { $0.uint32Value == format }
    }
}

/// The phone's back camera at 720p, 15 fps, rotated to portrait at capture.
final class CameraCapturer: FeedCapturer, AVCaptureVideoDataOutputSampleBufferDelegate, @unchecked Sendable {
    private let session = AVCaptureSession()
    private let queue = DispatchQueue(label: "storefront.camera")
    private var isConfigured = false
    /// What start() and stop() last asked for, on `queue`. Camera access is granted asynchronously, so a
    /// stop() can arrive before its start() takes effect; this keeps the camera off then.
    private var isWanted = false

    override func start() {
        queue.async { [self] in isWanted = true }
        AVCaptureDevice.requestAccess(for: .video) { [self] granted in
            guard granted else { return }
            queue.async { [self] in
                guard isWanted else { return }
                if !isConfigured { configure() }
                if !session.isRunning { session.startRunning() }
            }
        }
    }

    override func stop() {
        queue.async { [self] in
            isWanted = false
            session.stopRunning()
        }
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
        return pixelBuffer(from: image)
    }
}
