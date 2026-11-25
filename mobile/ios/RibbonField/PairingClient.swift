import Foundation
import Security

/// Durable Keychain storage for the paired-device `__Host-agent-coord-device` cookie.
enum PairingCookieKeychain {
    static let service = "com.agentcoord.ios.pairingCookie"
    private static let account = "pairedDeviceCookie"

    private struct StoredRecord: Codable {
        let origin: String
        let name: String
        let value: String
        let domain: String
        let path: String
        let expiresAt: TimeInterval
        let isSecure: Bool
        let isHTTPOnly: Bool
        let sameSite: String?
    }

    private static func isSafeCookieValue(_ value: String) -> Bool {
        !value.isEmpty && value.count <= 4096 && value.unicodeScalars.allSatisfy { scalar in
            scalar.value >= 0x21 && scalar.value <= 0x7E &&
            scalar != ";" && scalar != "\"" && scalar != "\\"
        }
    }

    private static func baseQuery(service: String) -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
    }

    @discardableResult
    static func save(_ cookie: HTTPCookie, for server: ServerAddress, service: String = Self.service) -> Bool {
        guard cookie.name == "__Host-agent-coord-device",
              cookie.isSecure, cookie.isHTTPOnly, cookie.path == "/",
              cookie.domain == server.url.host,
              let expiresDate = cookie.expiresDate, expiresDate > Date(),
              isSafeCookieValue(cookie.value) else { return false }
        let record = StoredRecord(
            origin: server.origin,
            name: cookie.name,
            value: cookie.value,
            domain: cookie.domain,
            path: cookie.path,
            expiresAt: expiresDate.timeIntervalSince1970,
            isSecure: cookie.isSecure,
            isHTTPOnly: cookie.isHTTPOnly,
            sameSite: cookie.sameSitePolicy?.rawValue
        )
        guard let data = try? JSONEncoder().encode(record) else { return false }
        SecItemDelete(baseQuery(service: service) as CFDictionary)
        var attributes = baseQuery(service: service)
        attributes[kSecValueData as String] = data
        attributes[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        let status = SecItemAdd(attributes as CFDictionary, nil)
        return status == errSecSuccess
    }

    static func load(for server: ServerAddress, now: Date = Date(), service: String = Self.service) -> HTTPCookie? {
        var query = baseQuery(service: service)
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var item: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &item)
        guard status == errSecSuccess, let data = item as? Data,
              let record = try? JSONDecoder().decode(StoredRecord.self, from: data) else { return nil }
        guard record.origin == server.origin,
              record.name == "__Host-agent-coord-device",
              record.domain == server.url.host,
              record.path == "/",
              record.isSecure, record.isHTTPOnly,
              isSafeCookieValue(record.value) else {
            clear(service: service)
            return nil
        }
        let remaining = record.expiresAt - now.timeIntervalSince1970
        guard remaining > 1 else {
            clear(service: service)
            return nil
        }
        let maxAge = max(1, Int(remaining.rounded(.down)))
        var header = "\(record.name)=\(record.value); Path=/; Secure; HttpOnly; Max-Age=\(maxAge)"
        if let sameSite = record.sameSite, ["Strict", "Lax"].contains(sameSite) {
            header += "; SameSite=\(sameSite)"
        } else {
            header += "; SameSite=Strict"
        }
        return HTTPCookie.cookies(withResponseHeaderFields: ["Set-Cookie": header], for: server.url).first {
            $0.name == "__Host-agent-coord-device" && $0.isSecure && $0.isHTTPOnly &&
            $0.path == "/" && $0.domain == server.url.host &&
            ($0.expiresDate ?? .distantPast) > now
        }
    }

    @discardableResult
    static func clear(service: String = Self.service) -> Bool {
        let status = SecItemDelete(baseQuery(service: service) as CFDictionary)
        return status == errSecSuccess || status == errSecItemNotFound
    }
}

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
