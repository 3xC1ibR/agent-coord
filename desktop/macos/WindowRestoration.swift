import AppKit

/// Interface state only. The independent runtime continues to own all agents.
struct RestoredWindow: Codable {
    enum Kind: String, Codable { case workspace, conversation, settings, monitor }
    let id: String
    let folderSlot: Int
    let kind: Kind
    var route: String
    var frame: CGRect
    var screen: CGRect?
    var displayID: UInt32?
    var minimized: Bool
    var workspaceJSON: String?

    var workspaceState: [String: Any]? {
        guard let json = workspaceJSON, let data = json.data(using: .utf8) else { return nil }
        return (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
    }
    var valid: Bool {
        UUID(uuidString: id) != nil && folderSlot >= 0 && folderSlot < 1_000_000_000 && WindowRestorationStore.validRoute(route) &&
            [frame.minX, frame.minY, frame.width, frame.height].allSatisfy { $0.isFinite } &&
            abs(frame.minX) < 10_000_000 && abs(frame.minY) < 10_000_000 &&
            frame.width > 0 && frame.height > 0 && frame.width <= 100_000 && frame.height <= 100_000
    }
}

struct RestoredWindowSet: Codable {
    var version = 1
    let database: String
    var windows: [RestoredWindow]
    var focusedWindow: String?
    var nextFolderSlot: Int
}

final class WindowRestorationStore {
    let url: URL
    let database: String
    private var previous: Data?

    init(url: URL, database: String) {
        self.url = url
        self.database = URL(fileURLWithPath: database).standardizedFileURL.resolvingSymlinksInPath().path
    }

    static func validRoute(_ route: String) -> Bool {
        guard route.hasPrefix("/"), !route.hasPrefix("//"), !route.contains("\\"),
              let parts = URLComponents(string: route), parts.scheme == nil, parts.host == nil else { return false }
        return ["/", "/settings", "/monitor", "/activity", "/usage", "/html-preview"].contains(parts.path)
    }

    static func script(_ source: String, values: [String: String]) -> String {
        let pattern = "__AGENT_COORD_(?:PREFERENCES|WINDOW_STATE|FOLDER_SLOT|CONVERSATION_WINDOW|WINDOW_ID)__"
        guard let regex = try? NSRegularExpression(pattern: pattern) else { return source }
        var result = source
        // Replace original template tokens once; user drafts may themselves
        // contain these strings and must never become template instructions.
        for match in regex.matches(in: source, range: NSRange(source.startIndex..., in: source)).reversed() {
            guard let original = Range(match.range, in: source), let range = Range(match.range, in: result),
                  let value = values[String(source[original])] else { continue }
            result.replaceSubrange(range, with: value)
        }
        return result
    }

    func load() -> RestoredWindowSet? {
        guard let data = try? Data(contentsOf: url),
              var saved = try? JSONDecoder().decode(RestoredWindowSet.self, from: data),
              saved.version == 1, saved.database == database else { return nil }
        var ids = Set<String>(), slots = Set<Int>()
        saved.windows = saved.windows.filter {
            $0.valid && ids.insert($0.id).inserted && slots.insert($0.folderSlot).inserted
        }
        let followingSlot = (saved.windows.map(\.folderSlot).max() ?? -1) + 1
        saved.nextFolderSlot = saved.nextFolderSlot < 0 || saved.nextFolderSlot > 1_000_000_000
            ? followingSlot : max(saved.nextFolderSlot, followingSlot)
        previous = data
        return saved
    }

    func save(windows: [RestoredWindow], focusedWindow: String?, nextFolderSlot: Int) throws {
        let state = RestoredWindowSet(database: database, windows: windows,
            focusedWindow: focusedWindow, nextFolderSlot: nextFolderSlot)
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        let data = try encoder.encode(state)
        guard data != previous else { return }
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true,
            attributes: [.posixPermissions: 0o700])
        try data.write(to: url, options: .atomic)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
        previous = data
    }

    /// Preserve a usable title bar if a display was removed or rearranged.
    static func usableFrame(_ frame: CGRect, screens: [CGRect], minimum: CGSize) -> CGRect {
        guard !screens.isEmpty else { return frame }
        let target = screens.max { left, right in
            let a = frame.intersection(left), b = frame.intersection(right)
            return (a.isNull ? 0 : a.width * a.height) < (b.isNull ? 0 : b.width * b.height)
        } ?? screens[0]
        let width = min(target.width, max(minimum.width, frame.width))
        let height = min(target.height, max(minimum.height, frame.height))
        return CGRect(x: max(target.minX, min(frame.minX, target.maxX - width)),
            y: max(target.minY, min(frame.minY, target.maxY - height)), width: width, height: height)
    }
}
