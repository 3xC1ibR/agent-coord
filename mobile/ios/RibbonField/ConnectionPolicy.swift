import Foundation

/// The only persistable connection value. Construction never retains a path or secret.
struct ServerAddress: Equatable {
    let url: URL
    var origin: String { String(url.absoluteString.dropLast()) }

    init(_ text: String) throws {
        guard let c = URLComponents(string: text), c.scheme?.lowercased() == "https",
              c.user == nil, c.password == nil, c.query == nil, c.fragment == nil,
              c.path.isEmpty || c.path == "/", let host = c.host?.lowercased(),
              Self.isValidHost(host), c.port == nil || (1...65535).contains(c.port!),
              !text.contains("\\"), !text.contains("%"), !text.contains(where: { $0.isWhitespace })
        else { throw ConnectionError.invalidAddress }
        var safe = URLComponents()
        safe.scheme = "https"
        safe.host = host
        safe.port = c.port == 443 ? nil : c.port
        safe.path = "/"
        guard let url = safe.url else { throw ConnectionError.invalidAddress }
        self.url = url
    }

    private static func isValidHost(_ host: String) -> Bool {
        guard !host.isEmpty, host.count <= 253 else { return false }
        // URLComponents validates IPv6 syntax when constructing its URL.
        if host.hasPrefix("["), host.hasSuffix("]") { return host.contains(":") }
        let labels = host.split(separator: ".", omittingEmptySubsequences: false)
        return labels.allSatisfy { label in
            !label.isEmpty && label.count <= 63 && label.first != "-" && label.last != "-" &&
            label.utf8.allSatisfy { (97...122).contains($0) || (48...57).contains($0) || $0 == 45 }
        }
    }

    func contains(_ url: URL) -> Bool {
        guard let c = URLComponents(url: url, resolvingAgainstBaseURL: false),
              c.user == nil, c.password == nil, c.scheme?.lowercased() == "https",
              c.host?.lowercased() == self.url.host,
              (c.port ?? 443) == (self.url.port ?? 443) else { return false }
        return true
    }
}

/// Short-lived value: never store this object or its input in defaults, logs or WebKit history.
struct PairingInvitation {
    let server: ServerAddress
    let token: String

    init(_ input: String) throws {
        let text = input.trimmingCharacters(in: .whitespacesAndNewlines)
        guard var c = URLComponents(string: text), c.percentEncodedPath == "/pair",
              c.query == nil, let fragment = c.fragment,
              fragment.hasPrefix("pair="), !text.contains("\\") else { throw ConnectionError.invalidInvitation }
        let token = String(fragment.dropFirst(5))
        guard (1...100).contains(token.count), token.utf8.allSatisfy({
            (65...90).contains($0) || (97...122).contains($0) || (48...57).contains($0) || $0 == 45 || $0 == 95
        }), c.percentEncodedFragment == fragment else { throw ConnectionError.invalidInvitation }
        c.path = "/"
        c.fragment = nil
        guard let address = c.string else { throw ConnectionError.invalidInvitation }
        server = try ServerAddress(address)
        self.token = token
    }

    /// Optional native handoff; standard HTTPS links may always be pasted instead.
    static func fromDeepLink(_ url: URL) throws -> PairingInvitation {
        guard let c = URLComponents(url: url, resolvingAgainstBaseURL: false),
              c.scheme == "agentcoord", c.host == "pair", c.path.isEmpty,
              c.user == nil, c.password == nil, c.port == nil, c.fragment == nil,
              let items = c.queryItems, items.count == 1, items[0].name == "url",
              let value = items[0].value else { throw ConnectionError.invalidInvitation }
        return try PairingInvitation(value)
    }
}

enum NavigationDecision: Equatable { case allow, external, block }

struct NavigationPolicy {
    let server: ServerAddress
    func decide(_ url: URL, userActivated: Bool, mainFrame: Bool) -> NavigationDecision {
        guard let c = URLComponents(url: url, resolvingAgainstBaseURL: false), c.user == nil,
              c.password == nil else { return .block }
        // Invitation secrets must never enter WebKit history, even on the paired origin.
        if c.path == "/pair" || c.fragment?.contains("pair=") == true { return .block }
        if server.contains(url) { return .allow }
        guard userActivated, mainFrame, c.scheme?.lowercased() == "https", c.host != nil else { return .block }
        return .external
    }
}

enum ConnectionError: LocalizedError {
    case invalidAddress, invalidInvitation, differentServer, pairingFailed, missingCookie
    var errorDescription: String? {
        switch self {
        case .invalidAddress: return "Use the HTTPS Tailscale address from Remote access on your Mac."
        case .invalidInvitation: return "Paste a complete, fresh pairing link from Remote access on your Mac."
        case .differentServer: return "Disconnect from the current Mac before pairing another Mac."
        case .pairingFailed: return "Pairing failed. Check Tailscale on both devices and create a fresh link on your Mac."
        case .missingCookie: return "The Mac did not return a device credential. Create a fresh pairing link and try again."
        }
    }
}
