import Foundation

struct WorkspaceFile: Decodable {
    let name: String
    let path: String
    let directory: Bool
    let symlink: Bool
}

struct WorkspaceListing: Decodable {
    let entries: [WorkspaceFile]
    let truncated: Bool
}

struct WorkspacePreview: Decodable {
    let text: String
    let truncated: Bool
    let size: Int
    let binary: Bool
}

// UI identities are workspace-relative paths, never client filesystem URLs.
// A remote provider implements this same contract and leaves localURL nil.
@MainActor protocol WorkspaceFileProvider: AnyObject {
    func list(root: String, path: String, hidden: Bool) async throws -> WorkspaceListing
    func preview(root: String, path: String) async throws -> WorkspacePreview
    func localURL(root: String, path: String) -> URL?
}

@MainActor final class BackendWorkspaceFiles: WorkspaceFileProvider {
    let baseURL: URL
    let local: Bool
    private var token: String?
    init(baseURL: URL, local: Bool) { self.baseURL = baseURL; self.local = local }

    private func request<T: Decodable>(_ operation: String, body: [String: Any]) async throws -> T {
        if token == nil {
            let (data, response) = try await URLSession.shared.data(from: baseURL.appendingPathComponent("api/browser/config"))
            guard (response as? HTTPURLResponse)?.statusCode == 200,
                  let config = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let value = config["token"] as? String else { throw failure("Reconnect to the workspace.") }
            token = value
        }
        var request = URLRequest(url: baseURL.appendingPathComponent("api/browser/files/" + operation))
        request.httpMethod = "POST"
        request.timeoutInterval = 15
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue(token, forHTTPHeaderField: "X-Agent-Coord-Token")
        request.httpBody = try JSONSerialization.data(withJSONObject: body)
        let (data, response) = try await URLSession.shared.data(for: request)
        guard (response as? HTTPURLResponse)?.statusCode == 200 else {
            if (response as? HTTPURLResponse)?.statusCode == 403 { token = nil }
            let message = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["error"] as? String
            throw failure(message ?? "Could not read the workspace. Try Refresh.")
        }
        return try JSONDecoder().decode(T.self, from: data)
    }
    private func failure(_ text: String) -> NSError {
        NSError(domain: "WorkspaceFiles", code: 1, userInfo: [NSLocalizedDescriptionKey: text])
    }
    func list(root: String, path: String, hidden: Bool) async throws -> WorkspaceListing {
        try await request("list", body: ["root": root, "path": path, "hidden": hidden])
    }
    func preview(root: String, path: String) async throws -> WorkspacePreview {
        try await request("preview", body: ["root": root, "path": path])
    }
    func localURL(root: String, path: String) -> URL? {
        local ? URL(fileURLWithPath: root, isDirectory: true).appendingPathComponent(path) : nil
    }
}
