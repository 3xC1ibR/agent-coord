import Foundation

struct NativePushStatus: Decodable {
    let available: Bool
    let enabled: Bool
    let error: String
}

enum NativePushError: LocalizedError {
    case unavailable, authorization, connection, rejected
    var errorDescription: String? {
        switch self {
        case .unavailable: return "Set up Apple push notifications on your Mac first."
        case .authorization: return "Reconnect to your Mac with a fresh pairing link."
        case .connection: return "Could not update notifications on your Mac. Check your connection and try again."
        case .rejected: return "Your Mac could not update this notification registration. Disable notifications, pair again, and retry."
        }
    }
}

final class NativePushClient: NSObject, URLSessionTaskDelegate {
    private let protocolClasses: [AnyClass]?
    init(protocolClasses: [AnyClass]? = nil) { self.protocolClasses = protocolClasses }
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil)
    }

    func call(server: ServerAddress, cookies: [HTTPCookie], action: String, body: [String: Any]? = nil) async throws -> Data {
        guard ["status", "subscribe", "unsubscribe", "test"].contains(action),
              let cookie = cookies.first(where: { $0.name == "__Host-agent-coord-device" && $0.domain == server.url.host && $0.path == "/" && $0.isSecure })
        else { throw NativePushError.authorization }
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = protocolClasses
        config.httpCookieStorage = nil
        config.httpShouldSetCookies = false
        config.urlCache = nil
        config.timeoutIntervalForRequest = 10
        config.timeoutIntervalForResource = 15
        let session = URLSession(configuration: config, delegate: self, delegateQueue: nil)
        defer { session.invalidateAndCancel() }
        func request(_ path: String) -> URLRequest {
            var value = URLRequest(url: server.url.appendingPathComponent(path))
            value.setValue(server.origin, forHTTPHeaderField: "Origin")
            value.setValue(HTTPCookie.requestHeaderFields(with: [cookie])["Cookie"], forHTTPHeaderField: "Cookie")
            return value
        }
        func send(_ request: URLRequest) async throws -> Data {
            let (data, response) = try await session.data(for: request)
            guard let http = response as? HTTPURLResponse, response.url == request.url else { throw NativePushError.connection }
            if http.statusCode == 401 || http.statusCode == 403 { throw NativePushError.authorization }
            guard (200...299).contains(http.statusCode) else { throw NativePushError.rejected }
            return data
        }
        do {
            var value = request("api/browser/native-push/" + action)
            if let body {
                let data = try await send(request("api/browser/config"))
                guard let config = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                      let token = config["token"] as? String else { throw NativePushError.authorization }
                value.httpMethod = "POST"
                value.setValue("application/json", forHTTPHeaderField: "Content-Type")
                value.setValue(token, forHTTPHeaderField: "X-Agent-Coord-Token")
                value.httpBody = try JSONSerialization.data(withJSONObject: body)
            }
            return try await send(value)
        } catch let error as NativePushError { throw error }
        catch { throw NativePushError.connection }
    }
}
