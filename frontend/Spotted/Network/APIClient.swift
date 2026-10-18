import Foundation

/// One entry of GET /ice-servers. "urls" may be a single string or a list.
struct ICEServer: Decodable, Hashable {
    let urls: [String]
    let username: String?
    let credential: String?

    enum CodingKeys: String, CodingKey { case urls, username, credential }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        if let list = try? c.decode([String].self, forKey: .urls) {
            urls = list
        } else {
            urls = [try c.decode(String.self, forKey: .urls)]
        }
        username = try c.decodeIfPresent(String.self, forKey: .username)
        credential = try c.decodeIfPresent(String.self, forKey: .credential)
    }
}

private struct ICEServersResponse: Decodable {
    let iceServers: [ICEServer]
}

/// The backend's HTTP API. For now only the WebRTC signaling routes; /detect, /search and /checkout
/// go here next (build steps 3 and 9 in FRONTEND.md).
struct APIClient {
    let baseURL: URL
    var session: URLSession = .shared

    func iceServers() async throws -> [ICEServer] {
        try await send(URLRequest(url: baseURL.appending(path: "ice-servers")), as: ICEServersResponse.self).iceServers
    }

    /// POST /offer: our SDP offer in, the backend's answer out.
    func offer(_ offer: SessionDescription) async throws -> SessionDescription {
        var request = URLRequest(url: baseURL.appending(path: "offer"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(offer)
        return try await send(request, as: SessionDescription.self)
    }

    private func send<Response: Decodable>(_ request: URLRequest, as type: Response.Type) async throws -> Response {
        var request = request
        request.timeoutInterval = 15
        let (data, response) = try await session.data(for: request)
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard (200..<300).contains(status) else {
            throw (try? JSONDecoder().decode(APIError.self, from: data)) ?? URLError(.badServerResponse)
        }
        return try JSONDecoder().decode(Response.self, from: data)
    }
}
