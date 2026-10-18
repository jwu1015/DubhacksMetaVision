import ActivityKit
import Foundation

/// G6 · The Live Activity for a Look in glasses mode: "Finding prices · lamp…" while it searches,
/// then the item, price and verdict. Compiled into both the app and the widget extension.
struct LookActivityAttributes: ActivityAttributes {
    struct ContentState: Codable, Hashable {
        /// "Brass arc lamp" once Claude has named it, else the short label ("lamp").
        var title: String
        /// "$189", nil while searching.
        var price: String?
        /// "Fair price", nil while searching.
        var verdict: String?
        /// A bundled product-art asset for the mock data; nil shows the glasses mark.
        var artAsset: String?

        var isSearching: Bool { price == nil }
    }

    /// The Look's captureID. Deep links carry it so the app acts on the right item.
    var captureID: String
}

/// Deep links from the Live Activity's buttons into the app.
enum LookLink {
    static func save(_ captureID: String) -> URL { URL(string: "spotted://save?capture=\(captureID)")! }
    static func add(_ captureID: String) -> URL { URL(string: "spotted://add?capture=\(captureID)")! }
    static let talk = URL(string: "spotted://talk")!
}
