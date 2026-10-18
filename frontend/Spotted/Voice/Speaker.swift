import AVFoundation

/// Speaks quick phrases and summaries through the current audio route (the glasses' speakers when
/// they're connected). A new line interrupts the previous one. Earcons and barge-in come later.
@MainActor
final class Speaker {
    private let synthesizer = AVSpeechSynthesizer()

    func say(_ text: String) {
        guard !text.isEmpty else { return }
        synthesizer.stopSpeaking(at: .immediate)
        synthesizer.speak(AVSpeechUtterance(string: text))
    }
}
