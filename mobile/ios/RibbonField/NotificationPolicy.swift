import Foundation

struct NotificationDestination: Equatable {
    let server: ServerAddress
    let fragment: String
    init?(payload: [AnyHashable: Any]) {
        guard let data = payload["agentcoord"] as? [String: Any],
              let origin = data["origin"] as? String, let server = try? ServerAddress(origin),
              let route = data["url"] as? String else { return nil }
        if route == "/" { fragment = "" }
        else {
            guard route.hasPrefix("/#") else { return nil }
            let value = String(route.dropFirst(2))
            guard (1...200).contains(value.count), value.utf8.allSatisfy({
                (65...90).contains($0) || (97...122).contains($0) || (48...57).contains($0) || [45,46,95,126].contains($0)
            }) else { return nil }
            fragment = value
        }
        self.server = server
    }
}

enum APNsEnvironment {
    /// Read the profile's actual entitlement instead of guessing from Debug/Release.
    /// App Store installations have no embedded provisioning profile and use production.
    static func resolve(profile: Data?, simulator: Bool) -> String? {
        if simulator { return "development" }
        guard let profile else { return "production" }
        guard let start = profile.range(of: Data("<?xml".utf8)),
              let end = profile.range(of: Data("</plist>".utf8), in: start.lowerBound..<profile.endIndex),
              let plist = try? PropertyListSerialization.propertyList(from: profile[start.lowerBound..<end.upperBound], format: nil),
              let object = plist as? [String: Any], let entitlements = object["Entitlements"] as? [String: Any],
              let value = entitlements["aps-environment"] as? String,
              ["development", "production"].contains(value) else { return nil }
        return value
    }
}
