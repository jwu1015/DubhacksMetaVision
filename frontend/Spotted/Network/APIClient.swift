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

/// The backend's HTTP API: WebRTC signaling, the voice token and the cart. /detect, /search and
/// /checkout go here next (build steps 3 and 9 in FRONTEND.md).
struct APIClient {
    let baseURL: URL
    var session: URLSession = .shared

    func iceServers() async throws -> [ICEServer] {
        try await send(URLRequest(url: baseURL.appending(path: "ice-servers")), as: ICEServersResponse.self).iceServers
    }

    /// POST /offer: our SDP offer in, the backend's answer out.
    func offer(_ offer: SessionDescription) async throws -> SessionDescription {
        try await post("offer", body: offer)
    }

    /// POST /voice/token: a LiveKit token for a fresh room with the assistant in it.
    func voiceToken(identity: String) async throws -> VoiceToken {
        try await post("voice/token", body: VoiceTokenRequest(participantIdentity: identity))
    }

    /// POST /cart with a Product exactly as /search returned it.
    func addToCart(_ product: Product) async throws -> Cart {
        try await post("cart", body: AddToCartRequest(product: product))
    }

    /// DELETE /cart/{index}
    func removeFromCart(at index: Int) async throws -> Cart {
        var request = URLRequest(url: baseURL.appending(path: "cart/\(index)"))
        request.httpMethod = "DELETE"
        return try await send(request, as: Cart.self)
    }

    private func post<Body: Encodable, Response: Decodable>(_ path: String, body: Body) async throws -> Response {
        var request = URLRequest(url: baseURL.appending(path: path))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(body)
        return try await send(request, as: Response.self)
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
