import Foundation

enum AppConfig {
    /// The backend's ngrok URL, from the untracked Config.xcconfig (via Info.plist). Debug builds can
    /// also pass SPOTTED_API_BASE_URL in the scheme. Nil means no backend: the app runs on mock data.
    static let baseURL: URL? = {
        #if DEBUG
        if let override = ProcessInfo.processInfo.environment["SPOTTED_API_BASE_URL"],
           !override.isEmpty, let url = URL(string: override) {
            return url
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
