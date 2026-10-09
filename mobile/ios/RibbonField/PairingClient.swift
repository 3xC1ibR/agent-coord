import Foundation

/// Ephemeral transport rejects every redirect so a POST body cannot escape its chosen origin.
final class PairingClient: NSObject, URLSessionTaskDelegate {
    private let protocolClasses: [AnyClass]?
    init(protocolClasses: [AnyClass]? = nil) {
        self.protocolClasses = protocolClasses
        super.init()
    }
    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest,
                    completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil)
    }

    func redeem(_ invitation: PairingInvitation, name: String) async throws -> HTTPCookie {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = protocolClasses
        configuration.httpCookieStorage = nil
        configuration.urlCache = nil
        configuration.httpShouldSetCookies = false
        configuration.timeoutIntervalForRequest = 20
        configuration.timeoutIntervalForResource = 30
        let session = URLSession(configuration: configuration, delegate: self, delegateQueue: nil)
        defer { session.invalidateAndCancel() }
        var request = URLRequest(url: invitation.server.url.appendingPathComponent("api/remote/pair"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue(invitation.server.origin, forHTTPHeaderField: "Origin")
        request.httpBody = try JSONSerialization.data(withJSONObject: ["token": invitation.token, "name": name])
        let (_, response) = try await session.data(for: request)
        guard let response = response as? HTTPURLResponse, (200...299).contains(response.statusCode),
              response.url == request.url else { throw ConnectionError.pairingFailed }
        let headers = response.allHeaderFields.reduce(into: [String: String]()) { result, entry in
            if let key = entry.key as? String, let value = entry.value as? String { result[key] = value }
        }
        guard let cookie = HTTPCookie.cookies(withResponseHeaderFields: headers, for: request.url!).first(where: {
            $0.name == "__Host-agent-coord-device" && $0.isSecure && $0.isHTTPOnly &&
            $0.path == "/" && $0.domain == invitation.server.url.host &&
            ($0.expiresDate ?? .distantPast) > Date()
        }) else { throw ConnectionError.missingCookie }
        return cookie
    }
}
