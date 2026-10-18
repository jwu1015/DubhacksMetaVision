import ActivityKit
import Foundation
import OSLog

private let log = Logger(subsystem: "com.dubhacks.spotted", category: "LiveActivity")

/// G6 · Runs the Look's Live Activity in glasses mode: "Finding prices…" while it searches, then the
/// result. One activity at a time, keyed by captureID; it ends when the Look is cleared.
@MainActor
final class LookActivityController {
    private var activity: Activity<LookActivityAttributes>?

    func update(for scan: ScanState) {
        switch scan {
        case let .detected(look):
            show(look.captureID, LookActivityAttributes.ContentState(
                title: look.name.sentenceCased, price: nil, verdict: nil, artAsset: Self.asset(look.cutoutURL)))
        case let .matched(look, search):
            show(look.captureID, LookActivityAttributes.ContentState(
                title: look.name.sentenceCased,
                price: search.bestMatch.map { Money.short($0.price) },
                verdict: search.bestMatch == nil ? "No exact match" : search.priceInsight.bestMatchVerdict.label,
                artAsset: Self.asset(search.bestMatch?.imageURL ?? look.cutoutURL)))
        case .idle, .failed:
            end()
        case .looking, .ambiguous:
            break
        }
    }

    func end() {
        guard let activity else { return }
        self.activity = nil
        Task { await activity.end(nil, dismissalPolicy: .default) }
    }

    private func show(_ captureID: String, _ state: LookActivityAttributes.ContentState) {
        let content = ActivityContent(state: state, staleDate: nil)
        if let activity, activity.attributes.captureID == captureID {
            Task { await activity.update(content) }
            return
        }
        end()
        guard ActivityAuthorizationInfo().areActivitiesEnabled else {
            log.error("Live Activities are turned off for Spotted")
            return
        }
        do {
            activity = try Activity.request(attributes: LookActivityAttributes(captureID: captureID), content: content)
            log.info("Started for \(captureID, privacy: .public)")
        } catch {
            log.error("Couldn't start: \(error.localizedDescription, privacy: .public)")
        }
    }

    /// Mock product art ("asset:art-arc-lamp") is bundled in the widget too; real thumbnails aren't.
    private static func asset(_ source: String?) -> String? {
        guard let source, source.hasPrefix("asset:") else { return nil }
        return String(source.dropFirst("asset:".count))
    }
}
