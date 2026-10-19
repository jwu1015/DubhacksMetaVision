import Foundation

enum AppConfig {
    /// The backend's URL, from Storefront.xcconfig or the untracked Config.xcconfig (via Info.plist). Debug
    /// builds can override it with STOREFRONT_API_BASE_URL; set it empty to run on mock data (the UI tests do).
    /// Nil means no backend: the app runs on mock data.
    static let baseURL: URL? = {
        #if DEBUG
        if let override = ProcessInfo.processInfo.environment["STOREFRONT_API_BASE_URL"] {
            return URL(string: override)
        }
        #endif
        return setting("StorefrontAPIBaseURL").flatMap(URL.init(string:))
    }()

    /// The Cloudflare Access service token for the tunnel in front of the backend, from Config.xcconfig.
    private static let accessClientID = setting("CFAccessClientID")
    private static let accessClientSecret = setting("CFAccessClientSecret")

    /// A request to `url`. Requests to the backend carry the Cloudflare Access service token; requests to
    /// anyone else (store images) never do.
    static func request(_ url: URL) -> URLRequest {
        var request = URLRequest(url: url)
        if url.host() == baseURL?.host(), let accessClientID, let accessClientSecret {
            request.setValue(accessClientID, forHTTPHeaderField: "CF-Access-Client-Id")
            request.setValue(accessClientSecret, forHTTPHeaderField: "CF-Access-Client-Secret")
        }
        return request
    }

    /// An Info.plist value set from the build settings. Nil when it's empty or its setting is undefined.
    private static func setting(_ key: String) -> String? {
        guard let value = Bundle.main.object(forInfoDictionaryKey: key) as? String,
              !value.isEmpty, !value.contains("$(") else { return nil }
        return value
    }
}
