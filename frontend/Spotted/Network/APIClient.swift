import Foundation

/// The backend's HTTP API: the session token.
struct APIClient {
    let baseURL: URL
    var session: URLSession = .shared

    /// POST /session/token: a token for a new LiveKit room with the backend in it.
    func sessionToken(identity: String) async throws -> TokenResponse {
        var request = URLRequest(url: baseURL.appending(path: "session/token"))
        request.httpMethod = "POST"
        request.timeoutInterval = 15
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(TokenRequest(participantIdentity: identity))
        let (data, response) = try await session.data(for: request)
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard (200..<300).contains(status) else {
            throw (try? JSONDecoder().decode(APIError.self, from: data)) ?? URLError(.badServerResponse)
        }
        return try JSONDecoder().decode(TokenResponse.self, from: data)
    }
}
