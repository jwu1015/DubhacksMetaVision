import Foundation

enum AppConfig {
    /// The backend's URL, from Spotted.xcconfig or the untracked Config.xcconfig (via Info.plist). Debug
    /// builds can override it with SPOTTED_API_BASE_URL; set it empty to run on mock data (the UI tests do).
    /// Nil means no backend: the app runs on mock data.
    static let baseURL: URL? = {
        #if DEBUG
        if let override = ProcessInfo.processInfo.environment["SPOTTED_API_BASE_URL"] {
            return URL(string: override)
        }
        #endif
        guard let value = Bundle.main.object(forInfoDictionaryKey: "SpottedAPIBaseURL") as? String,
              !value.isEmpty, !value.contains("$("), let url = URL(string: value) else { return nil }
        return url
    }()

    /// A server path such as a Look's cutoutPath, as a full URL.
    static func url(for path: String) -> URL? {
        guard let baseURL else { return nil }
        if let absolute = URL(string: path), absolute.scheme != nil { return absolute }
        return baseURL.appending(path: path.hasPrefix("/") ? String(path.dropFirst()) : path)
    }
}
