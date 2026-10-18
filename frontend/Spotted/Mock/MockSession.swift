import Foundation

/// Without a backend: plays the messages the server would send, so the Simulator, previews and UI tests
/// run the same code as the live app.
@MainActor
final class MockSession: UpdateSource {
    var onUpdate: ((Update) -> Void)?

    /// What joining the room sends first: the status (labels already picked), the objects in view and their
    /// quick labels.
    func start() {
        onUpdate?(.status(MockData.status))
        onUpdate?(.objects(MockData.objects))
        for label in MockData.labels { onUpdate?(.label(label)) }
    }

    func select(uid: String) async throws -> Look {
        guard let steps = MockData.looks[uid], let first = steps.first else {
            throw APIError(detail: "That object isn't tracked any more.")
        }
        Task {
            for step in steps.dropFirst() {
                try? await Task.sleep(for: .seconds(1.2))
                onUpdate?(.look(step))
            }
        }
        return first
    }
}
