import SwiftUI
import WebRTC

/// The outgoing camera track, shown full screen behind the Live view's markers.
struct LiveFeedView: UIViewRepresentable {
    let track: RTCVideoTrack

    func makeCoordinator() -> RTCVideoTrack { track }

    func makeUIView(context: Context) -> RTCMTLVideoView {
        let view = RTCMTLVideoView()
        view.videoContentMode = .scaleAspectFill
        view.clipsToBounds = true
        track.add(view)
        return view
    }

    func updateUIView(_ view: RTCMTLVideoView, context: Context) {}

    static func dismantleUIView(_ view: RTCMTLVideoView, coordinator: RTCVideoTrack) {
        coordinator.remove(view)
    }
}
